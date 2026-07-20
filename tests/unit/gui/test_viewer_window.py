"""Tests for viewer-window startup sizing."""

from __future__ import annotations

import logging
import sys
import queue
from types import SimpleNamespace

import numpy as np
import pytest

from caveviewer.core.map import cache_paths
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


class FakeImportThread:
    def __init__(self, alive=True):
        self._alive = alive
        self.join_calls = []

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)
        self._alive = False


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


def _recording_window():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._recording_countdown_started_at = None
    window._recording_countdown_until = None
    window._recording_process = None
    window._recording_output_path = None
    window._recording_size = None
    window._recording_viewport = None
    window._recording_next_frame_time = None
    window._recording_frame_queue = None
    window._recording_writer_thread = None
    window._recording_stderr_thread = None
    window._recording_writer_error = None
    window._recording_dropped_frames = 0
    window._recording_stderr_lock = viewer_window.threading.Lock()
    window._recording_stderr_parts = []
    window._recording_stop_results = queue.Queue()
    window._recording_stop_thread = None
    window._recording_status_message = None
    window._recording_status_detail = None
    window._recording_status_kind = None
    window._recording_status_until = None
    return window


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


def test_optional_ms_formatter_reports_disabled_timer():
    assert viewer_window.CaveViewerWindow._format_optional_ms(None) == "n/a"
    assert viewer_window.CaveViewerWindow._format_optional_ms(9.34) == "9.3ms"


def test_recording_countdown_hides_picker_and_manual_help(monkeypatch):
    calls = []
    monkeypatch.setattr(viewer_window.time, "perf_counter", lambda: 40.0)
    window = _recording_window()
    window._has_map_loaded = True
    window._resolve_ffmpeg_path = lambda: "/usr/bin/ffmpeg"
    window.color_picker = SimpleNamespace(hide=lambda: calls.append("hide_picker"))
    window.controls_overlay = SimpleNamespace(
        is_manual_mode=True,
        hide_help=lambda: calls.append("hide_help"),
    )

    window._start_recording_countdown()

    assert calls == ["hide_picker", "hide_help"]
    assert window._recording_countdown_started_at == 40.0
    assert window._recording_countdown_until == 44.0


def test_recording_toggle_cancels_existing_countdown(monkeypatch):
    monkeypatch.setattr(viewer_window.time, "perf_counter", lambda: 10.0)
    window = _recording_window()
    window._recording_countdown_started_at = 7.0
    window._recording_countdown_until = 11.0

    window._toggle_recording()

    assert window._recording_countdown_started_at is None
    assert window._recording_countdown_until is None
    assert window._recording_status_message == "Recording canceled"
    assert window._recording_status_kind == "cancel"
    assert window._recording_status_until == pytest.approx(12.8)


def test_recording_signal_writer_stop_replaces_full_frame_with_sentinel():
    window = _recording_window()
    frame_queue = queue.Queue(maxsize=1)
    frame_queue.put_nowait(b"old-frame")

    window._recording_signal_writer_stop(frame_queue)

    assert frame_queue.get_nowait() is None


def test_recording_enqueue_frame_reports_encoder_backpressure_once(monkeypatch):
    logger = FakeLogger()
    monkeypatch.setattr(viewer_window, "_LOG", logger)
    window = _recording_window()
    window._recording_frame_queue = queue.Queue(maxsize=1)
    window._recording_frame_queue.put_nowait(b"queued-frame")

    assert window._recording_enqueue_frame(b"new-frame", frames_due=2) is True
    assert window._recording_enqueue_frame(b"newer-frame", frames_due=1) is True

    assert window._recording_dropped_frames == 2
    assert logger.warning_messages == [
        "Recording encoder is falling behind; dropping video frames."
    ]


def test_stop_recording_kills_encoder_after_timeout_and_reports_failure(monkeypatch):
    logger = FakeLogger()
    monkeypatch.setattr(viewer_window, "_LOG", logger)
    monkeypatch.setattr(viewer_window.time, "perf_counter", lambda: 20.0)

    class TimeoutProcess:
        stdin = None
        returncode = None

        def __init__(self):
            self.killed = False
            self.wait_calls = []

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if len(self.wait_calls) == 1:
                raise viewer_window.subprocess.TimeoutExpired("ffmpeg", timeout)
            self.returncode = -9

        def kill(self):
            self.killed = True

    process = TimeoutProcess()
    window = _recording_window()
    window._recording_process = process
    window._recording_output_path = "/recordings/cave.mp4"
    window._recording_size = (640, 480)
    window._recording_viewport = (0, 0, 640, 480)
    window._recording_next_frame_time = 20.0
    window._recording_frame_queue = queue.Queue(maxsize=1)
    window._recording_stderr_parts = ["No space left on device"]

    window._stop_recording(show_message=True)

    assert window._recording_status_message == "Finishing recording"
    assert window._recording_stop_thread is not None
    window._recording_stop_thread.join(timeout=1.0)
    assert not window._recording_stop_thread.is_alive()
    window._drain_recording_stop_results()

    assert process.killed is True
    assert process.wait_calls == [8.0, None]
    assert window._recording_process is None
    assert window._recording_output_path is None
    assert window._recording_frame_queue is None
    assert window._recording_status_message == "Recording failed"
    assert window._recording_status_detail == "Disk may be full"
    assert window._recording_status_kind == "error"
    assert window._recording_stop_thread is None
    assert any("Recording encoder exited with code -9" in message for message in logger.warning_messages)


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


def test_initial_chunk_readiness_waits_for_startup_wanted_cells():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.world = SimpleNamespace(config=SimpleNamespace(max_loaded_chunks=100))

    assert window._initial_chunk_load_is_ready(
        {
            "loaded_wanted": 6,
            "total_available": 1655,
            "wanted": 27,
        }
    ) is False
    assert window._initial_chunk_load_is_ready(
        {
            "loaded_wanted": 27,
            "total_available": 1655,
            "wanted": 27,
        }
    ) is True


def test_initial_chunk_readiness_counts_failed_wanted_chunks():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.world = SimpleNamespace(config=SimpleNamespace(max_loaded_chunks=100))

    assert window._initial_chunk_load_is_ready(
        {
            "loaded_wanted": 2,
            "failed_wanted": 1,
            "total_available": 1655,
            "wanted": 3,
        }
    ) is True


def test_startup_upload_limits_are_boosted_until_initial_load_is_ready():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._upload_chunks_per_frame = 1
    window._upload_groups_per_frame = 1
    window._upload_time_budget_ms = 3.0
    window._initial_chunks_loaded = False
    window.controls_overlay = SimpleNamespace(is_waiting_for_begin=True)

    chunks, operations, budget_ms = window._streaming_upload_limits()

    assert chunks >= 4
    assert operations >= 8
    assert budget_ms >= 12.0

    window._initial_chunks_loaded = True

    assert window._streaming_upload_limits() == (1, 1, 3.0)


def test_upload_limits_boost_while_current_wanted_set_is_incomplete():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._upload_chunks_per_frame = 1
    window._upload_groups_per_frame = 1
    window._upload_time_budget_ms = 3.0
    window._initial_chunks_loaded = True
    window.controls_overlay = SimpleNamespace(is_waiting_for_begin=False)

    chunks, operations, budget_ms = window._streaming_upload_limits(
        {
            "ready": 2,
            "wanted": 10,
            "loaded_wanted": 4,
            "failed_wanted": 0,
        }
    )

    assert chunks >= 2
    assert operations >= 8
    assert budget_ms >= 8.0

    assert window._streaming_upload_limits(
        {
            "ready": 0,
            "wanted": 10,
            "loaded_wanted": 4,
            "failed_wanted": 0,
        }
    ) == (1, 1, 3.0)


def test_drain_streaming_worker_failures_logs_bounded_batch(monkeypatch):
    logger = FakeLogger()
    monkeypatch.setattr(viewer_window, "_LOG", logger)
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._STREAMING_FAILURES_PER_FRAME = 1
    failure = SimpleNamespace(
        fatal=True,
        cell=(1, 0, 0),
        stage="load_chunk_file",
        thread_name="test-worker",
        error_type="ValueError",
        message="bad chunk",
    )
    world = SimpleNamespace(
        drain_worker_failures=lambda *, max_items: [failure][:max_items]
    )
    window.world = world

    window._drain_streaming_worker_failures()

    assert logger.error_messages == [
        "Streaming worker failed for chunk (1, 0, 0) during "
        "load_chunk_file on test-worker: ValueError: bad chunk"
    ]


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


def test_startup_streaming_radius_matches_revealed_render_distance():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.render_distance_stepper = SimpleNamespace(value=6)
    window.controls_overlay = SimpleNamespace(is_waiting_for_begin=True)
    window._initial_chunks_loaded = False

    assert window._target_streaming_load_radius() == 6


def test_streaming_radius_uses_stepper_after_begin_screen_is_dismissed():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.render_distance_stepper = SimpleNamespace(value=6)
    window.controls_overlay = SimpleNamespace(is_waiting_for_begin=False)
    window._initial_chunks_loaded = True

    assert window._target_streaming_load_radius() == 6


def test_streaming_cell_priority_prefers_camera_forward_cells():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.world = SimpleNamespace(config=SimpleNamespace(chunk_size=1.0))
    window.wnd = SimpleNamespace(size=(1600, 1000))
    window.camera = SimpleNamespace(
        position=np.array([0.0, 0.0, 0.0], dtype=np.float64),
        fov_deg=75.0,
        forward=lambda: np.array([1.0, 0.0, 0.0], dtype=np.float64),
    )

    priority = window._streaming_cell_priority_key()

    assert priority((5, 0, 0)) < priority((0, 5, 0))
    assert priority((5, 0, 0)) < priority((-5, 0, 0))


class _FakeGpuResource:
    def __init__(self, context=None):
        self._context = context
        self.writes = []
        self.released = False

    def write(self, data):
        byte_count = len(data)
        self.writes.append(byte_count)
        if self._context is not None:
            self._context.buffer_write_sizes.append(byte_count)

    def release(self):
        self.released = True


class _FakeViewerContext:
    def __init__(self):
        self.buffer_sizes = []
        self.buffer_reserves = []
        self.buffer_write_sizes = []

    def buffer(self, data=None, *, reserve=None):
        resource = _FakeGpuResource(self)
        if reserve is not None:
            self.buffer_reserves.append(reserve)
            return resource
        self.buffer_sizes.append(len(data))
        resource.write(data)
        return resource

    def vertex_array(self, *_args):
        return _FakeGpuResource()


class _FakeTextureManager:
    def __init__(self):
        self.acquires = []
        self.releases = []

    def acquire(self, _material_name):
        self.acquires.append(_material_name)
        return _FakeGpuResource()

    def release(self, _material_name):
        self.releases.append(_material_name)


def _drain_chunk_ready(window, chunk_data, *, max_calls=32):
    for _ in range(max_calls):
        if window._on_chunk_ready(chunk_data):
            return True
    return False


def test_chunk_aabbs_are_tracked_only_for_loaded_chunks():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.ctx = _FakeViewerContext()
    window.program = object()
    window.texture_manager = _FakeTextureManager()
    window.render_mode_buttons = SimpleNamespace(smooth_shading_enabled=True)
    window._upload_groups_per_frame = 1
    window._upload_time_budget_ms = 100.0
    window._streaming_frame_timing = None
    window._chunk_gpu_objects = {}
    window._chunk_upload_states = {}
    window._chunk_normal_cache = {}
    window._chunk_aabbs = {}
    cell = (1, 2, 3)
    positions = np.zeros((3, 3), dtype=np.float32)
    uvs = np.zeros((3, 2), dtype=np.float32)
    normals = np.tile(np.array([[0.0, 1.0, 0.0]], dtype=np.float32), (3, 1))
    chunk_data = SimpleNamespace(
        cell=cell,
        bounds_min=np.array([1.0, 2.0, 3.0], dtype=np.float64),
        bounds_max=np.array([4.0, 5.0, 6.0], dtype=np.float64),
        upload_groups=[
            viewer_window.chunker.ChunkUploadGroup(
                material_name="mat",
                positions=positions,
                uvs=uvs,
                smooth_normals=normals,
            )
        ],
    )

    assert _drain_chunk_ready(window, chunk_data)

    assert set(window._chunk_aabbs) == {cell}
    assert window._chunk_aabbs[cell][0].dtype == np.float32

    window._on_chunk_unload(cell)

    assert window._chunk_aabbs == {}


def test_chunk_upload_can_be_split_across_group_frames():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window.ctx = _FakeViewerContext()
    window.program = object()
    window.texture_manager = _FakeTextureManager()
    window.render_mode_buttons = SimpleNamespace(smooth_shading_enabled=True)
    window._upload_groups_per_frame = 1
    window._upload_time_budget_ms = 100.0
    window._streaming_frame_timing = None
    window._chunk_gpu_objects = {}
    window._chunk_upload_states = {}
    window._chunk_normal_cache = {}
    window._chunk_aabbs = {}
    cell = (1, 2, 3)
    positions = np.zeros((3, 3), dtype=np.float32)
    uvs = np.zeros((3, 2), dtype=np.float32)
    normals = np.tile(np.array([[0.0, 1.0, 0.0]], dtype=np.float32), (3, 1))
    chunk_data = SimpleNamespace(
        cell=cell,
        bounds_min=np.array([1.0, 2.0, 3.0], dtype=np.float64),
        bounds_max=np.array([4.0, 5.0, 6.0], dtype=np.float64),
        upload_groups=[
            viewer_window.chunker.ChunkUploadGroup(
                material_name="mat_a",
                positions=positions,
                uvs=uvs,
                smooth_normals=normals,
            ),
            viewer_window.chunker.ChunkUploadGroup(
                material_name="mat_b",
                positions=positions,
                uvs=uvs,
                smooth_normals=normals,
            ),
        ],
    )

    assert window._on_chunk_ready(chunk_data) is False

    assert cell not in window._chunk_gpu_objects
    assert cell in window._chunk_upload_states

    assert window._on_chunk_ready(chunk_data) is False
    assert len(window._chunk_gpu_objects[cell]) == 1
    assert len(window._chunk_normal_cache[cell]) == 1
    assert window._chunk_aabbs[cell][0].dtype == np.float32

    assert window._on_chunk_ready(chunk_data) is False
    assert window._on_chunk_ready(chunk_data) is True

    assert cell not in window._chunk_upload_states
    assert len(window._chunk_gpu_objects[cell]) == 2
    assert window._chunk_aabbs[cell][0].dtype == np.float32


def test_partial_chunk_upload_unloads_published_slices_once():
    window = object.__new__(viewer_window.CaveViewerWindow)
    texture_manager = _FakeTextureManager()
    window.ctx = _FakeViewerContext()
    window.program = object()
    window.texture_manager = texture_manager
    window.render_mode_buttons = SimpleNamespace(smooth_shading_enabled=True)
    window._upload_groups_per_frame = 1
    window._upload_time_budget_ms = 100.0
    window._streaming_frame_timing = None
    window._chunk_gpu_objects = {}
    window._chunk_upload_states = {}
    window._chunk_normal_cache = {}
    window._chunk_aabbs = {}
    cell = (1, 2, 3)
    positions = np.zeros((3, 3), dtype=np.float32)
    uvs = np.zeros((3, 2), dtype=np.float32)
    normals = np.tile(np.array([[0.0, 1.0, 0.0]], dtype=np.float32), (3, 1))
    chunk_data = SimpleNamespace(
        cell=cell,
        bounds_min=np.array([1.0, 2.0, 3.0], dtype=np.float64),
        bounds_max=np.array([4.0, 5.0, 6.0], dtype=np.float64),
        upload_groups=[
            viewer_window.chunker.ChunkUploadGroup(
                material_name="mat_a",
                positions=positions,
                uvs=uvs,
                smooth_normals=normals,
            ),
            viewer_window.chunker.ChunkUploadGroup(
                material_name="mat_b",
                positions=positions,
                uvs=uvs,
                smooth_normals=normals,
            ),
        ],
    )

    assert window._on_chunk_ready(chunk_data) is False
    assert window._on_chunk_ready(chunk_data) is False
    assert len(window._chunk_gpu_objects[cell]) == 1

    window._on_chunk_unload(cell)

    assert cell not in window._chunk_upload_states
    assert cell not in window._chunk_gpu_objects
    assert cell not in window._chunk_normal_cache
    assert cell not in window._chunk_aabbs
    assert texture_manager.releases == ["mat_a"]


def test_large_group_upload_is_sliced_into_small_vbos():
    window = object.__new__(viewer_window.CaveViewerWindow)
    context = _FakeViewerContext()
    texture_manager = _FakeTextureManager()
    window.ctx = context
    window.program = object()
    window.texture_manager = texture_manager
    window.render_mode_buttons = SimpleNamespace(smooth_shading_enabled=True)
    window._upload_groups_per_frame = 1
    window._upload_time_budget_ms = 100.0
    window._vbo_upload_slice_bytes = 3 * 8 * np.dtype(np.float32).itemsize
    window._texture_upload_slice_bytes = 1024
    window._streaming_frame_timing = None
    window._chunk_gpu_objects = {}
    window._chunk_upload_states = {}
    window._chunk_normal_cache = {}
    window._chunk_aabbs = {}
    cell = (1, 2, 3)
    positions = np.zeros((9, 3), dtype=np.float32)
    uvs = np.zeros((9, 2), dtype=np.float32)
    normals = np.tile(np.array([[0.0, 1.0, 0.0]], dtype=np.float32), (9, 1))
    chunk_data = SimpleNamespace(
        cell=cell,
        bounds_min=np.array([1.0, 2.0, 3.0], dtype=np.float64),
        bounds_max=np.array([4.0, 5.0, 6.0], dtype=np.float64),
        upload_groups=[
            viewer_window.chunker.ChunkUploadGroup(
                material_name="mat",
                positions=positions,
                uvs=uvs,
                smooth_normals=normals,
            )
        ],
    )

    assert window._on_chunk_ready(chunk_data) is False
    assert cell not in window._chunk_gpu_objects
    assert window._on_chunk_ready(chunk_data) is False
    assert len(window._chunk_gpu_objects[cell]) == 1
    for _ in range(3):
        assert window._on_chunk_ready(chunk_data) is False
    assert window._on_chunk_ready(chunk_data) is True

    assert context.buffer_sizes == []
    assert context.buffer_reserves == [96, 96, 96]
    assert context.buffer_write_sizes == [96, 96, 96]
    assert len(window._chunk_gpu_objects[cell]) == 3
    assert texture_manager.acquires == ["mat", "mat", "mat"]

    window._on_chunk_unload(cell)

    assert texture_manager.releases == ["mat", "mat", "mat"]


def test_upload_slice_size_shrinks_after_measured_stall():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._upload_time_budget_ms = 3.0
    window._vbo_upload_slice_bytes = 1024 * 1024
    window._texture_upload_slice_bytes = 1024 * 1024
    timing = viewer_window.CaveViewerWindow._new_streaming_frame_timing()

    window._adapt_upload_slice_size(
        kind="texture",
        elapsed_ms=30.0,
        byte_count=1024 * 1024,
        timing=timing,
    )
    window._adapt_upload_slice_size(
        kind="vbo",
        elapsed_ms=30.0,
        byte_count=1024 * 1024,
        timing=timing,
    )

    assert window._texture_upload_slice_bytes < 1024 * 1024
    assert window._vbo_upload_slice_bytes < 1024 * 1024
    assert timing["upload_stalls"] == 2
    assert timing["texture_upload_slice_bytes"] == window._texture_upload_slice_bytes
    assert timing["vbo_upload_slice_bytes"] == window._vbo_upload_slice_bytes


def test_upload_slice_size_uses_current_boosted_time_budget():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._upload_time_budget_ms = 3.0
    window._current_upload_time_budget_ms = 8.0
    window._vbo_upload_slice_bytes = 1024 * 1024
    timing = viewer_window.CaveViewerWindow._new_streaming_frame_timing()

    window._adapt_upload_slice_size(
        kind="vbo",
        elapsed_ms=5.0,
        byte_count=1024 * 1024,
        timing=timing,
    )

    assert window._vbo_upload_slice_bytes == 1024 * 1024
    assert timing["upload_stalls"] == 0


def test_streaming_timing_format_splits_drain_and_upload_details():
    timing = viewer_window.CaveViewerWindow._new_streaming_frame_timing()
    timing.update(
        {
            "drain_ms": 12.0,
            "ready_drain_ms": 9.0,
            "chunk_ready_ms": 5.0,
            "unload_ms": 1.0,
            "failure_drain_ms": 2.0,
            "buffer_alloc_ms": 1.5,
            "buffer_write_ms": 2.5,
            "texture_alloc_ms": 0.5,
            "texture_upload_ms": 3.5,
            "vbo_upload_slice_bytes": 256 * 1024,
            "texture_upload_slice_bytes": 128 * 1024,
            "upload_stalls": 1,
        }
    )

    detail = viewer_window.CaveViewerWindow._format_streaming_frame_timing(timing)

    assert "ready_drain=9.0ms" in detail
    assert "ready_other=3.0ms" in detail
    assert "failures=2.0ms" in detail
    assert "drain_other=1.0ms" in detail
    assert "buffer_alloc=1.5ms" in detail
    assert "buffer_write=2.5ms" in detail
    assert "tex_alloc=0.5ms" in detail
    assert "tex_upload=3.5ms" in detail
    assert "slices=vbo:256KB/tex:128KB" in detail
    assert "stalls=1" in detail


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
    window._import_active = False
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
    window._import_active = False
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


def test_key_event_during_window_setup_returns_before_full_state_exists():
    window = object.__new__(viewer_window.CaveViewerWindow)

    window.on_key_event(0, 0, None)


def test_mouse_motion_during_window_setup_returns_before_full_state_exists():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._window_setup_complete = False

    window.on_mouse_position_event(10, 20, 1, -1)


def test_mouse_motion_after_color_picker_release_is_noop():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._window_setup_complete = True
    window._closing_requested = False
    window.color_picker = None
    window._mouse_look_active = False
    window._option_look_active = lambda: False

    window.on_mouse_position_event(10, 20, 1, -1)


def test_map_switch_teardown_uses_bounded_streaming_shutdown():
    timeouts = []
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._has_map_loaded = True
    window._stop_recording = lambda: None
    window.world = SimpleNamespace(
        shutdown=lambda *, timeout=None: timeouts.append(timeout)
    )
    window._chunk_upload_states = {}
    window._chunk_gpu_objects = {}
    window._chunk_normal_cache = {}
    window._chunk_aabbs = {}
    window.texture_manager = None
    window.minimap = None

    window._teardown_current_map()

    assert timeouts == [2.0]


def test_final_teardown_joins_streaming_workers_without_timeout():
    timeouts = []
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._has_map_loaded = True
    window._stop_recording = lambda: None
    window.world = SimpleNamespace(
        shutdown=lambda *, timeout=None: timeouts.append(timeout)
    )
    window._chunk_upload_states = {}
    window._chunk_gpu_objects = {}
    window._chunk_normal_cache = {}
    window._chunk_aabbs = {}
    window.texture_manager = None
    window.minimap = None

    window._teardown_current_map(final_shutdown=True)

    assert timeouts == [None]


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


def test_cancel_active_import_uses_zero_timeout_cleanup_when_relay_is_gone(monkeypatch):
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
    assert calls == [(process, {"timeout": 0.0, "cache_dir": "/cache/cave"})]


def test_cancel_active_import_does_not_wait_for_live_import_thread(monkeypatch):
    calls = []
    import_thread = FakeImportThread(alive=True)
    window = _import_window()
    window._import_stop_event = viewer_window.threading.Event()
    window._import_process = None
    window._import_thread = import_thread

    monkeypatch.setattr(
        viewer_window,
        "terminate_import_process",
        lambda *_args, **_kwargs: calls.append("terminate"),
    )

    window._cancel_active_import()

    assert window._import_stop_event.is_set()
    assert import_thread.join_calls == []
    assert import_thread.is_alive()
    assert calls == []
