"""Tests for viewer-window startup sizing."""

from __future__ import annotations

import logging
import sys
import queue
from types import SimpleNamespace

import numpy as np
import pytest

from caveviewer.core import cache_paths
from caveviewer.gui import viewer_window
from caveviewer.gui.platform.app_identity import tk_root_options


class FakeImportInhibitor:
    def __init__(self, calls):
        self._calls = calls

    def close(self):
        self._calls.append(("close_inhibitor",))


class FakeImportProcess:
    def __init__(self, calls=None, exitcode=None):
        self._calls = [] if calls is None else calls
        self.exitcode = exitcode
        self.joined = False
        self.terminated = False
        self.killed = False

    def is_alive(self):
        return self.exitcode is None and not self.terminated

    def join(self, timeout=None):
        self._calls.append(("join_process", timeout))
        self.joined = True
        if self.exitcode is None:
            self.exitcode = 0

    def terminate(self):
        self._calls.append(("terminate_process",))
        self.terminated = True
        self.exitcode = -15

    def kill(self):
        self._calls.append(("kill_process",))
        self.killed = True
        self.exitcode = -9


class FakeLogger:
    def __init__(self):
        self.error_messages = []
        self.info_messages = []
        self.warning_messages = []
        self.debug_messages = []

    @staticmethod
    def _format(message, args):
        return message % args if args else str(message)

    def error(self, message, *args):
        self.error_messages.append(self._format(message, args))

    def info(self, message, *args):
        self.info_messages.append(self._format(message, args))

    def warning(self, message, *args):
        self.warning_messages.append(self._format(message, args))

    def debug(self, message, *args):
        self.debug_messages.append(self._format(message, args))


def _import_window():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._import_active = False
    window._import_is_startup = False
    window._import_thread = None
    window._import_process = None
    window._import_command_queue = None
    window._import_queue = None
    window._import_cache_dir = None
    window._import_stop_event = None
    window._import_pause_requested = False
    window._import_model_format = None
    window._import_map_name = ""
    window._import_progress_stage = ""
    window._import_progress_fraction = 0.0
    window._import_progress_title = ""
    window._import_progress_note = ""
    window._import_resuming_from_checkpoint = False
    window._import_pause_notice_until = None
    window._import_pause_notice_close_after = False
    window._import_pause_notice_map_name = ""
    window._import_pause_notice_title = "Import paused"
    window._import_pause_notice_stage = "resume point saved"
    window._import_pause_notice_note = ""
    window._has_map_loaded = False
    window._pending_import_started = False
    window._pending_import_splash_rendered = False
    return window


def _wait_for_import_worker(window):
    window._import_thread.join(timeout=2.0)
    assert not window._import_thread.is_alive()


def _queued_import_messages(window):
    messages = []
    while not window._import_queue.empty():
        messages.append(window._import_queue.get_nowait())
    return messages


def test_desktop_relative_window_size_uses_eighty_percent_per_axis(monkeypatch):
    class FakeRoot:
        def __init__(self):
            self.withdrawn = False
            self.destroyed = False

        def withdraw(self):
            self.withdrawn = True

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

        def destroy(self):
            self.destroyed = True

    root = FakeRoot()
    root_options = []
    monkeypatch.setitem(
        sys.modules,
        "tkinter",
        SimpleNamespace(Tk=lambda **options: root_options.append(options) or root),
    )

    assert viewer_window._desktop_relative_window_size() == (1536, 864)
    assert root_options == [tk_root_options()]
    assert root.withdrawn is True
    assert root.destroyed is True


def test_window_pixel_ratio_uses_framebuffer_size():
    window = SimpleNamespace(size=(1000, 700), buffer_size=(2000, 1400))

    assert viewer_window._window_pixel_ratio(window) == 2.0


def test_window_pixel_ratio_falls_back_for_missing_backend_data():
    assert viewer_window._window_pixel_ratio(SimpleNamespace(size=(1000, 700))) == 1.0


def test_viewer_ui_scale_grows_on_large_viewer_surfaces():
    assert viewer_window._viewer_ui_scale_for_window_size((1536, 864), {}) == 1.0
    assert viewer_window._viewer_ui_scale_for_window_size((2048, 1152), {}) == pytest.approx(
        4 / 3
    )
    assert viewer_window._viewer_ui_scale_for_window_size((3840, 2160), {}) == 1.45


def test_viewer_ui_scale_env_override_is_developer_only_escape_hatch():
    assert viewer_window._viewer_ui_scale_for_window_size(
        (1536, 864), {"CAVEVIEWER_VIEWER_UI_SCALE": "1.25"}
    ) == 1.25
    assert viewer_window._viewer_ui_scale_for_window_size(
        (1536, 864), {"CAVEVIEWER_VIEWER_UI_SCALE": "bad"}
    ) == 1.0


def test_window_shortcut_closes_viewer_on_control_w(monkeypatch):
    monkeypatch.setattr(viewer_window.sys, "platform", "linux")
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.wnd = SimpleNamespace(keys=SimpleNamespace(W=87, O=79))
    window._keys_down = set()
    window._key_resolve_cache = {}
    closed = []
    window.on_close = lambda: closed.append("closed")

    assert window._handle_window_shortcut(87, SimpleNamespace(ctrl=True)) is True
    assert closed == ["closed"]


def test_window_shortcut_opens_map_only_when_loaded(monkeypatch):
    monkeypatch.setattr(viewer_window.sys, "platform", "linux")
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.wnd = SimpleNamespace(keys=SimpleNamespace(W=87, O=79))
    window._keys_down = set()
    window._key_resolve_cache = {}
    calls = []
    window._handle_open_button_click = lambda: calls.append("open")

    window._has_map_loaded = False
    window._import_active = False
    assert window._handle_window_shortcut(79, SimpleNamespace(ctrl=True)) is True
    assert calls == []

    window._has_map_loaded = True
    window._import_active = False
    assert window._handle_window_shortcut(79, SimpleNamespace(ctrl=True)) is True
    assert calls == ["open"]


def test_window_shortcut_uses_command_modifier_on_macos(monkeypatch):
    monkeypatch.setattr(viewer_window.sys, "platform", "darwin")
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.wnd = SimpleNamespace(keys=SimpleNamespace(W=87, O=79))
    window._keys_down = set()
    window._key_resolve_cache = {}
    window._raw_command_modifier_down = lambda: False
    closed = []
    window.on_close = lambda: closed.append("closed")

    assert window._handle_window_shortcut(87, SimpleNamespace(command=True)) is True
    assert window._handle_window_shortcut(87, SimpleNamespace()) is False
    assert closed == ["closed"]


def test_linux_launch_defers_sizing_to_glfw_workarea(monkeypatch):
    calls = []
    monkeypatch.setattr(viewer_window.sys, "platform", "linux")
    monkeypatch.setattr(
        viewer_window,
        "_desktop_relative_window_size",
        lambda: (_ for _ in ()).throw(
            AssertionError("Linux sizing must not mix Tk and GLFW coordinates")
        ),
    )
    monkeypatch.setattr(
        viewer_window,
        "run_window_config",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    viewer_window._launch_viewer_window()

    assert viewer_window.CaveViewerWindow.window_size == (1600, 1000)
    assert calls[0][0] == (viewer_window.CaveViewerWindow,)
    assert calls[0][1]["runner"] is viewer_window._run_moderngl_window_config
    assert calls[0][1]["window_size_fraction"] == 0.8
    assert calls[0][1]["fallback_window_size"] == (1600, 1000)
    assert calls[0][1]["force_resizable_window"] is True


def test_moderngl_runner_closes_and_destroys_window_on_keyboard_interrupt(monkeypatch):
    calls = []

    class FakeWindow:
        is_closing = False

        def close(self):
            calls.append("close")
            self.is_closing = True

        def destroy(self):
            calls.append("destroy")

    fake_window = FakeWindow()
    fake_config = SimpleNamespace(wnd=fake_window)
    fake_config_class = type("FakeConfigClass", (), {})
    created = []

    def create_window_config_instance(config_class, args=None):
        created.append((config_class, args))
        return fake_config

    def run_window_config_instance(config):
        assert config is fake_config
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        viewer_window.mglw,
        "create_window_config_instance",
        create_window_config_instance,
    )
    monkeypatch.setattr(
        viewer_window.mglw,
        "run_window_config_instance",
        run_window_config_instance,
    )

    with pytest.raises(KeyboardInterrupt):
        viewer_window._run_moderngl_window_config(
            fake_config_class,
            args=["--window", "glfw"],
        )

    assert created == [(fake_config_class, ["--window", "glfw"])]
    assert calls == ["close", "destroy"]


def test_moderngl_runner_does_not_close_window_after_normal_loop(monkeypatch):
    calls = []

    class FakeWindow:
        is_closing = False

        def close(self):
            calls.append("close")

        def destroy(self):
            calls.append("destroy")

    fake_config = SimpleNamespace(wnd=FakeWindow())

    monkeypatch.setattr(
        viewer_window.mglw,
        "create_window_config_instance",
        lambda _config_class, args=None: fake_config,
    )
    monkeypatch.setattr(
        viewer_window.mglw,
        "run_window_config_instance",
        lambda _config: calls.append("run"),
    )

    viewer_window._run_moderngl_window_config(type("FakeConfigClass", (), {}))

    assert calls == ["run"]


class _ScaledStepperProbe:
    BUTTON_SIZE = viewer_window.StepperControl.BUTTON_SIZE
    VALUE_BOX_WIDTH = viewer_window.StepperControl.VALUE_BOX_WIDTH
    GAP = viewer_window.StepperControl.GAP

    def __init__(self, label: str = "BRIGHTNESS"):
        self.label = label
        self._geometry_scale = viewer_window.CaveViewerWindow.RIGHT_COLUMN_PANEL_SCALE
        self._text_scale = viewer_window.CaveViewerWindow.RIGHT_COLUMN_PANEL_TEXT_SCALE
        self._label_text_scale = (
            viewer_window.CaveViewerWindow.RIGHT_COLUMN_PANEL_LABEL_TEXT_SCALE
        )

    def set_scale(
        self,
        *,
        text_scale: float,
        geometry_scale: float,
        label_text_scale: float | None = None,
    ) -> None:
        self._text_scale = text_scale
        self._label_text_scale = (
            text_scale if label_text_scale is None else label_text_scale
        )
        self._geometry_scale = geometry_scale

    def total_width(self):
        return (
            self.BUTTON_SIZE * self._geometry_scale * 2
            + self.VALUE_BOX_WIDTH * self._geometry_scale
            + self.GAP * self._geometry_scale * 2
        )

    def total_height(self):
        return self.BUTTON_SIZE * self._geometry_scale


def _right_column_probe_window():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._layout_cache_size = None
    window._layout_cache_result = None
    window._viewer_ui_scale = 1.0
    window._right_column_panel_scale = viewer_window.CaveViewerWindow.RIGHT_COLUMN_PANEL_SCALE
    window._right_column_panel_text_scale = (
        viewer_window.CaveViewerWindow.RIGHT_COLUMN_PANEL_TEXT_SCALE
    )
    window._right_column_panel_label_text_scale = (
        viewer_window.CaveViewerWindow.RIGHT_COLUMN_PANEL_LABEL_TEXT_SCALE
    )
    window.light_stepper = _ScaledStepperProbe("BRIGHTNESS")
    window.ambient_stepper = _ScaledStepperProbe("GLOBAL LIGHT")
    window.render_distance_stepper = _ScaledStepperProbe("DISTANCE")
    window.render_mode_buttons = object.__new__(viewer_window.RenderModeButtons)
    window.render_mode_buttons._geometry_scale = (
        viewer_window.CaveViewerWindow.RIGHT_COLUMN_PANEL_SCALE
    )
    window.render_mode_buttons._text_scale = (
        viewer_window.CaveViewerWindow.RIGHT_COLUMN_PANEL_BUTTON_TEXT_SCALE
    )
    window.render_mode_buttons._render_cache_key = None
    return window


def test_right_column_panel_uses_compact_default_footprint():
    window = _right_column_probe_window()

    window_size = (1536, 864)
    column = window._right_column_layout(window_size)
    x0, y0, x1, y1 = window._right_column_panel_rect(window_size, column)

    assert 0 <= x0 < x1 <= window_size[0]
    assert 0 <= y0 < y1 <= window_size[1]
    assert x1 - x0 <= 135
    assert y1 - y0 <= 455


def test_right_column_panel_scales_up_on_large_viewer_surfaces():
    baseline = _right_column_probe_window()
    large = _right_column_probe_window()

    base_column = baseline._right_column_layout((1536, 864))
    base_rect = baseline._right_column_panel_rect((1536, 864), base_column)
    large_column = large._right_column_layout((2048, 1152))
    large_rect = large._right_column_panel_rect((2048, 1152), large_column)

    assert large._right_column_ui_scale() == pytest.approx(4 / 3)
    assert large.light_stepper._geometry_scale == pytest.approx(
        viewer_window.CaveViewerWindow.RIGHT_COLUMN_PANEL_SCALE * 4 / 3
    )
    assert large.light_stepper._text_scale == pytest.approx(
        viewer_window.CaveViewerWindow.RIGHT_COLUMN_PANEL_TEXT_SCALE
    )
    assert large.light_stepper._label_text_scale == pytest.approx(
        viewer_window.CaveViewerWindow.RIGHT_COLUMN_PANEL_LABEL_TEXT_SCALE
    )
    assert large.light_stepper._label_text_scale > large.light_stepper._text_scale
    assert large.render_mode_buttons._text_scale == pytest.approx(
        viewer_window.CaveViewerWindow.RIGHT_COLUMN_PANEL_BUTTON_TEXT_SCALE
    )
    assert large.render_mode_buttons._text_scale < large.light_stepper._text_scale
    assert large_rect[2] - large_rect[0] > base_rect[2] - base_rect[0]
    assert large_rect[3] - large_rect[1] > base_rect[3] - base_rect[1]
    assert 0 <= large_rect[0] < large_rect[2] <= 2048
    assert 0 <= large_rect[1] < large_rect[3] <= 1152


def test_initial_chunk_readiness_respects_budget_limited_wanted_count():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.world = SimpleNamespace(config=SimpleNamespace(max_loaded_chunks=100))

    assert window._initial_chunk_load_is_ready(
        {"loaded": 3, "total_available": 1655, "wanted": 3}
    ) is True
    assert window._initial_chunk_load_is_ready(
        {"loaded": 2, "total_available": 1655, "wanted": 3}
    ) is False


def test_initial_compilation_completion_is_logged_once(monkeypatch):
    logger = FakeLogger()
    monkeypatch.setattr(viewer_window, "_LOG", logger)
    monkeypatch.setattr(viewer_window.time, "perf_counter", lambda: 12.25)
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._initial_compilation_started_at = 10.0
    window._initial_compilation_logged = False

    stats = {"loaded": 6, "pending": 1, "ready": 0, "wanted": 7}
    window._log_initial_compilation_complete(stats)
    window._log_initial_compilation_complete(stats)

    assert logger.info_messages == [
        "Initial map compilation completed in 2.25s "
        "(loaded=6 pending=1 ready=0 wanted=7)."
    ]


def test_startup_streaming_radius_is_capped_until_begin_screen_is_dismissed():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.render_distance_stepper = SimpleNamespace(value=6)
    window.controls_overlay = SimpleNamespace(is_waiting_for_begin=True)
    window._initial_chunks_loaded = True

    assert window._target_streaming_load_radius() == 1


def test_streaming_radius_uses_stepper_after_begin_screen_is_dismissed():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.render_distance_stepper = SimpleNamespace(value=6)
    window.controls_overlay = SimpleNamespace(is_waiting_for_begin=False)
    window._initial_chunks_loaded = True

    assert window._target_streaming_load_radius() == 6


class _FakeGpuResource:
    def release(self):
        pass


class _FakeViewerContext:
    def buffer(self, _data):
        return _FakeGpuResource()

    def vertex_array(self, *_args):
        return _FakeGpuResource()


class _FakeTextureManager:
    def acquire(self, _material_name):
        return _FakeGpuResource()

    def release(self, _material_name):
        pass


def test_chunk_aabbs_are_tracked_only_for_loaded_chunks():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.ctx = _FakeViewerContext()
    window.program = object()
    window.texture_manager = _FakeTextureManager()
    window.render_mode_buttons = SimpleNamespace(smooth_shading_enabled=True)
    window._chunk_gpu_objects = {}
    window._chunk_normal_cache = {}
    window._chunk_aabbs = {}
    cell = (1, 2, 3)
    chunk_data = SimpleNamespace(
        cell=cell,
        bounds_min=np.array([1.0, 2.0, 3.0], dtype=np.float64),
        bounds_max=np.array([4.0, 5.0, 6.0], dtype=np.float64),
        upload_groups=[
            SimpleNamespace(
                material_name="mat",
                smooth_vertex_bytes=b"\x00" * 96,
                flat_vertex_bytes=b"\x00" * 96,
            )
        ],
    )

    window._on_chunk_ready(chunk_data)

    assert set(window._chunk_aabbs) == {cell}
    assert window._chunk_aabbs[cell][0].dtype == np.float32

    window._on_chunk_unload(cell)

    assert window._chunk_aabbs == {}


def test_uncached_import_holds_desktop_inhibitor_until_import_finishes(
    monkeypatch,
):
    calls = []
    descriptor = {"glb_path": "/maps/cave.glb"}

    def acquire(map_name):
        calls.append(("acquire_inhibitor", map_name))
        return FakeImportInhibitor(calls)

    def start_process(model_descriptor, textures_dir):
        calls.append(("start_process", model_descriptor, textures_dir))
        events = queue.Queue()
        events.put(("progress", "building cache", 0.5))
        events.put(("done", "/cache/cave", "/cache/cave"))
        return SimpleNamespace(process=FakeImportProcess(calls), events=events)

    monkeypatch.setattr(viewer_window, "_acquire_map_import_inhibitor", acquire)
    monkeypatch.setattr(viewer_window.chunker, "cache_is_valid", lambda _path: False)
    monkeypatch.setattr(viewer_window, "start_import_process", start_process)

    window = _import_window()
    window._start_import_async(
        descriptor, "/maps", "cave.glb", is_startup=True
    )
    _wait_for_import_worker(window)

    assert calls == [
        ("acquire_inhibitor", "cave.glb"),
        ("start_process", descriptor, "/maps"),
        ("join_process", 1.0),
        ("close_inhibitor",),
    ]
    assert _queued_import_messages(window) == [
        ("progress", "starting import", 0.0),
        ("progress", "building cache", 0.5),
        ("done", "/cache/cave", "/cache/cave"),
    ]


def test_uncached_import_relays_child_heartbeat(monkeypatch):
    descriptor = {"glb_path": "/maps/cave.glb"}

    def start_process(_model_descriptor, _textures_dir):
        events = queue.Queue()
        events.put(("log", logging.INFO, "ImportProcess", "child import started"))
        events.put(("heartbeat", "building cache", 0.5, 12.0, 3_000, 8_000))
        events.put(("done", "/cache/cave", "/cache/cave"))
        return SimpleNamespace(
            process=FakeImportProcess(),
            events=events,
            cache_dir="/cache/cave",
        )

    monkeypatch.setattr(
        viewer_window,
        "_acquire_map_import_inhibitor",
        lambda _map_name: FakeImportInhibitor([]),
    )
    monkeypatch.setattr(viewer_window.chunker, "cache_is_valid", lambda _path: False)
    monkeypatch.setattr(viewer_window, "start_import_process", start_process)

    window = _import_window()
    window._start_import_async(
        descriptor, "/maps", "cave.glb", is_startup=True
    )
    _wait_for_import_worker(window)

    assert _queued_import_messages(window) == [
        ("progress", "starting import", 0.0),
        ("heartbeat", "building cache", 0.5, 12.0, 3_000, 8_000),
        ("done", "/cache/cave", "/cache/cave"),
    ]


def test_uncached_import_relays_child_keyboard_interrupt_as_cancelled(monkeypatch):
    calls = []
    descriptor = {"glb_path": "/maps/cancelled.glb"}

    def start_process(model_descriptor, textures_dir):
        calls.append(("start_process", model_descriptor, textures_dir))
        events = queue.Queue()
        events.put(("cancelled",))
        return SimpleNamespace(
            process=FakeImportProcess(calls),
            events=events,
            cache_dir="/cache/cancelled",
        )

    monkeypatch.setattr(
        viewer_window,
        "_acquire_map_import_inhibitor",
        lambda _map_name: FakeImportInhibitor(calls),
    )
    monkeypatch.setattr(viewer_window.chunker, "cache_is_valid", lambda _path: False)
    monkeypatch.setattr(viewer_window, "start_import_process", start_process)

    window = _import_window()
    window._start_import_async(
        descriptor, "/maps", "cancelled.glb", is_startup=True
    )
    _wait_for_import_worker(window)

    assert calls == [
        ("start_process", descriptor, "/maps"),
        ("join_process", 1.0),
        ("close_inhibitor",),
    ]
    assert _queued_import_messages(window) == [
        ("progress", "starting import", 0.0),
        ("cancelled",),
    ]


def test_drain_import_queue_heartbeat_updates_visible_progress():
    window = _import_window()
    window._import_queue = queue.Queue()
    window._import_model_format = "obj"
    window._import_progress_stage = "starting import"
    window._import_progress_fraction = 0.0
    window._import_queue.put(
        ("heartbeat", "building cache", 0.5, 12.0, 3_000, 8_000)
    )

    window._drain_import_queue()

    assert window._import_progress_stage == "building cache"
    assert window._import_progress_fraction == 0.5
    assert window._import_progress_title == ""
    assert (
        window._import_progress_note
        == "First-time setup in progress. Next time, this map will open faster."
    )


def test_import_progress_message_switches_for_resume(monkeypatch):
    monkeypatch.setattr(viewer_window.sys, "platform", "linux")
    window = _import_window()
    window._import_model_format = "obj"

    window._update_import_progress_message_for_stage("resuming import")

    assert window._import_resuming_from_checkpoint is True
    assert window._import_progress_title == "Resuming import"
    assert window._import_progress_note == "Using saved work from the previous session."


def test_pending_import_splash_renders_logo_before_import_starts(monkeypatch):
    rendered = []

    class FakeImportProgressPanel:
        def render(self, window_size, map_name, stage, fraction, *, title, note):
            rendered.append((window_size, map_name, stage, fraction, title, note))

    monkeypatch.setattr(
        viewer_window.CaveViewerWindow,
        "cave_pending_import",
        {"model_descriptor": {"obj_path": "/maps/cave.obj"}},
    )

    window = _import_window()
    window.wnd = SimpleNamespace(size=(800, 600))
    window.import_progress_panel = FakeImportProgressPanel()

    window._render_pending_import_splash()

    assert rendered == [
        (
            (800, 600),
            "cave.obj",
            "starting import",
            0.0,
            "",
            "First-time setup in progress. Next time, this map will open faster.",
        )
    ]


def test_present_pending_import_splash_swaps_when_backend_supports_it(monkeypatch):
    rendered = []
    calls = []

    class FakeImportProgressPanel:
        def render(self, window_size, map_name, stage, fraction, *, title, note):
            rendered.append((window_size, map_name, stage, fraction, title, note))

    monkeypatch.setattr(
        viewer_window.CaveViewerWindow,
        "cave_pending_import",
        {"model_descriptor": {"obj_path": "/maps/cave.obj"}},
    )

    window = _import_window()
    window.wnd = SimpleNamespace(
        size=(800, 600),
        swap_buffers=lambda: calls.append("swap"),
    )
    window.import_progress_panel = FakeImportProgressPanel()

    assert window._present_pending_import_splash_now() is True
    assert calls == ["swap"]
    assert rendered[0][1:5] == ("cave.obj", "starting import", 0.0, "")


def test_present_pending_import_splash_renders_without_swap_support(monkeypatch):
    rendered = []

    class FakeImportProgressPanel:
        def render(self, window_size, map_name, stage, fraction, *, title, note):
            rendered.append((window_size, map_name, stage, fraction, title, note))

    monkeypatch.setattr(
        viewer_window.CaveViewerWindow,
        "cave_pending_import",
        {"model_descriptor": {"obj_path": "/maps/cave.obj"}},
    )

    window = _import_window()
    window.wnd = SimpleNamespace(size=(800, 600))
    window.import_progress_panel = FakeImportProgressPanel()

    assert window._present_pending_import_splash_now() is False
    assert rendered[0][1:5] == ("cave.obj", "starting import", 0.0, "")


def test_startup_render_presents_splash_before_starting_import():
    calls = []
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._window_setup_complete = True
    window.wnd = SimpleNamespace(size=(800, 600), buffer_size=(800, 600))
    window._closing_requested = False
    window._startup_focus_enabled = False
    window._is_iconified = False
    window._has_map_loaded = False
    window._pending_import_started = False
    window._pending_import_splash_rendered = False
    window._sync_render_mode_loading_policy = lambda: None
    window._query_runtime_iconified_state = lambda: False
    window._set_background_pause = lambda _should_pause, _reason: None
    window._render_import_pause_notice_if_active = lambda: False
    window._render_pending_import_splash = lambda: calls.append("splash")
    window._run_pending_import = lambda: calls.append("start import")

    window.on_render(0.0, 0.0)

    assert calls == ["splash"]
    assert window._pending_import_splash_rendered is True
    assert window._pending_import_started is False

    window.on_render(0.0, 0.0)

    assert calls == ["splash", "splash", "start import"]
    assert window._pending_import_started is True


def test_startup_render_starts_import_when_splash_was_already_presented():
    calls = []
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._window_setup_complete = True
    window.wnd = SimpleNamespace(size=(800, 600), buffer_size=(800, 600))
    window._closing_requested = False
    window._startup_focus_enabled = False
    window._is_iconified = False
    window._has_map_loaded = False
    window._pending_import_started = False
    window._pending_import_splash_rendered = True
    window._sync_render_mode_loading_policy = lambda: None
    window._query_runtime_iconified_state = lambda: False
    window._set_background_pause = lambda _should_pause, _reason: None
    window._render_import_pause_notice_if_active = lambda: False
    window._render_pending_import_splash = lambda: calls.append("splash")
    window._run_pending_import = lambda: calls.append("start import")

    window.on_render(0.0, 0.0)

    assert calls == ["splash", "start import"]
    assert window._pending_import_started is True


def test_render_during_window_setup_returns_before_full_state_exists():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._window_setup_complete = False

    window.on_render(0.0, 0.0)


def test_request_import_pause_sends_child_command(monkeypatch):
    logger = FakeLogger()
    commands = queue.Queue()
    monkeypatch.setattr(viewer_window, "_LOG", logger)
    window = _import_window()
    window._import_active = True
    window._import_model_format = "obj"
    window._import_command_queue = commands
    window._import_pause_requested = False
    window._import_progress_stage = "building cache"

    window._request_import_pause()

    assert window._import_pause_requested is True
    assert window._import_progress_stage == "pausing import"
    assert window._import_progress_title == "Pausing import"
    assert window._import_progress_note == "Saving a resume point."
    assert commands.get_nowait() == ("pause",)
    assert "Import pause requested" in logger.info_messages[-1]


def test_request_import_pause_warns_for_non_obj_import(monkeypatch):
    logger = FakeLogger()
    commands = queue.Queue()
    monkeypatch.setattr(viewer_window, "_LOG", logger)
    window = _import_window()
    window._import_active = True
    window._import_model_format = "glb"
    window._import_command_queue = commands
    window._import_pause_requested = False

    window._request_import_pause()

    assert window._import_pause_requested is False
    assert commands.empty()
    assert "only for .obj maps" in logger.warning_messages[-1]


def test_drain_import_queue_handles_paused_import(monkeypatch):
    logger = FakeLogger()
    monkeypatch.setattr(viewer_window, "_LOG", logger)
    window = _import_window()
    window._has_map_loaded = True
    window._import_active = True
    window._import_is_startup = False
    window._import_queue = queue.Queue()
    window._import_thread = object()
    window._import_process = object()
    window._import_command_queue = object()
    window._import_cache_dir = "/cache/cave"
    window._import_stop_event = viewer_window.threading.Event()
    window._import_pause_requested = True
    window._import_model_format = "obj"
    window._import_queue.put(("paused", "/cache/.cave.resume-123"))

    window._drain_import_queue()

    assert window._import_active is False
    assert window._import_queue is None
    assert window._import_command_queue is None
    assert window._import_pause_requested is False
    assert window._import_model_format is None
    assert window._recording_status_message == "Import paused"
    assert (
        window._recording_status_detail
        == "Resume point saved. Open this map again to continue."
    )
    assert any("Resume checkpoint" in message for message in logger.info_messages)


def test_drain_import_queue_paused_startup_sets_visible_notice(monkeypatch):
    logger = FakeLogger()
    monkeypatch.setattr(viewer_window, "_LOG", logger)
    monkeypatch.setattr(viewer_window.time, "perf_counter", lambda: 100.0)
    window = _import_window()
    window._has_map_loaded = False
    window._import_active = True
    window._import_is_startup = True
    window._import_map_name = "cave.obj"
    window._import_queue = queue.Queue()
    window._import_thread = object()
    window._import_process = object()
    window._import_command_queue = object()
    window._import_cache_dir = "/cache/cave"
    window._import_stop_event = viewer_window.threading.Event()
    window._import_pause_requested = True
    window._import_model_format = "obj"
    window._import_queue.put(("paused", "/cache/.cave.resume-123"))

    window._drain_import_queue()

    assert window._import_active is False
    assert window._import_pause_notice_until == 106.0
    assert window._import_pause_notice_close_after is True
    assert window._import_pause_notice_map_name == "cave.obj"
    assert window._import_pause_notice_title == "Import paused"
    assert window._import_pause_notice_stage == "resume point saved"
    assert (
        window._import_pause_notice_note
        == "This window will close shortly; open this map again to continue."
    )


def test_drain_import_queue_loads_manifest_once_on_render_thread(monkeypatch):
    manifest = {"chunks": {}}
    loaded = []

    monkeypatch.setattr(
        viewer_window.chunker,
        "load_manifest",
        lambda path: loaded.append(("manifest", path)) or manifest,
    )

    window = _import_window()
    window._import_active = True
    window._import_is_startup = False
    window._import_queue = queue.Queue()
    window._import_thread = object()
    window._import_process = object()
    window._import_cache_dir = "/cache/cave"
    window._import_stop_event = viewer_window.threading.Event()
    window.load_new_map = lambda cache_dir, textures_dir, loaded_manifest: loaded.append(
        ("load", cache_dir, textures_dir, loaded_manifest)
    )
    window._import_queue.put(("done", "/cache/cave", "/textures/cave"))

    window._drain_import_queue()

    assert loaded == [
        ("manifest", "/cache/cave"),
        ("load", "/cache/cave", "/textures/cave", manifest),
    ]
    assert window._import_active is False
    assert window._import_queue is None


def test_drain_import_queue_logs_actionable_error_without_traceback(monkeypatch):
    logger = FakeLogger()
    monkeypatch.setattr(viewer_window, "_LOG", logger)

    window = _import_window()
    window._import_active = True
    window._import_is_startup = False
    window._import_queue = queue.Queue()
    window._import_thread = object()
    window._import_process = object()
    window._import_cache_dir = "/cache/cave"
    window._import_stop_event = viewer_window.threading.Event()
    window._import_queue.put(
        (
            "error",
            "Not enough available system RAM to import 'DevilsEye Start.obj'.",
            "",
            "Close memory-heavy applications and retry.",
        )
    )

    window._drain_import_queue()

    assert logger.error_messages == [
        "Import failed: Not enough available system RAM to import "
        "'DevilsEye Start.obj'.",
        "Suggestion: Close memory-heavy applications and retry.",
    ]
    assert window._import_active is False
    assert window._import_queue is None
    assert not any("traceback" in message.lower() for message in logger.error_messages)


def test_cached_import_worker_does_not_request_desktop_inhibitor(monkeypatch):
    descriptor = {"obj_path": "/maps/cave.obj"}

    monkeypatch.setattr(
        viewer_window,
        "_acquire_map_import_inhibitor",
        lambda _map_name: (_ for _ in ()).throw(
            AssertionError("cached map loads should not inhibit the desktop")
        ),
    )
    monkeypatch.setattr(viewer_window.chunker, "cache_is_valid", lambda _path: True)
    monkeypatch.setattr(viewer_window.chunker, "get_cache_dir", lambda _path: "/cache/cave")
    monkeypatch.setattr(
        cache_paths,
        "map_texture_dir",
        lambda _source_path, _cache_dir, _textures_dir: "/textures/cave",
    )

    window = _import_window()
    window._start_import_async(
        descriptor, "/maps", "cave.obj", is_startup=False
    )
    _wait_for_import_worker(window)

    assert _queued_import_messages(window) == [
        ("progress", "loading cached map", 1.0),
        ("done", "/cache/cave", "/textures/cave"),
    ]


def test_uncached_import_releases_desktop_inhibitor_after_failure(monkeypatch):
    calls = []
    descriptor = {"glb_path": "/maps/broken.glb"}

    def acquire(map_name):
        calls.append(("acquire_inhibitor", map_name))
        return FakeImportInhibitor(calls)

    def start_process(model_descriptor, textures_dir):
        calls.append(("start_process", model_descriptor, textures_dir))
        events = queue.Queue()
        events.put(("error", "parse failed", "traceback text"))
        return SimpleNamespace(process=FakeImportProcess(calls), events=events)

    monkeypatch.setattr(viewer_window, "_acquire_map_import_inhibitor", acquire)
    monkeypatch.setattr(viewer_window.chunker, "cache_is_valid", lambda _path: False)
    monkeypatch.setattr(viewer_window, "start_import_process", start_process)

    window = _import_window()
    window._start_import_async(
        descriptor, "/maps", "broken.glb", is_startup=True
    )
    _wait_for_import_worker(window)

    assert calls == [
        ("acquire_inhibitor", "broken.glb"),
        ("start_process", descriptor, "/maps"),
        ("join_process", 1.0),
        ("close_inhibitor",),
    ]
    assert _queued_import_messages(window) == [
        ("progress", "starting import", 0.0),
        ("error", "parse failed", "traceback text"),
    ]


def test_cancel_active_import_passes_cache_dir_to_termination(monkeypatch):
    calls = []
    process = object()
    window = _import_window()
    window._import_stop_event = viewer_window.threading.Event()
    window._import_process = process
    window._import_cache_dir = "/cache/cave"
    window._import_thread = None

    monkeypatch.setattr(
        viewer_window,
        "terminate_import_process",
        lambda process, **kwargs: calls.append((process, kwargs)),
    )

    window._cancel_active_import()

    assert window._import_stop_event.is_set()
    assert calls == [(process, {"cache_dir": "/cache/cave"})]
