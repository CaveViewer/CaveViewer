"""Tests for viewer-window startup sizing."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from caveviewer import app
from caveviewer.core import cache_paths
from caveviewer.gui import viewer_window
from caveviewer.gui.platform.app_identity import tk_root_options


class FakeImportInhibitor:
    def __init__(self, calls):
        self._calls = calls

    def close(self):
        self._calls.append(("close_inhibitor",))


def _import_window():
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._import_active = False
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
    assert calls[0][1]["window_size_fraction"] == 0.8
    assert calls[0][1]["fallback_window_size"] == (1600, 1000)
    assert calls[0][1]["force_resizable_window"] is True


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
    manifest = {"chunks": {}}

    def acquire(map_name):
        calls.append(("acquire_inhibitor", map_name))
        return FakeImportInhibitor(calls)

    def fake_import(model_descriptor, textures_dir, **options):
        calls.append(
            (
                "import",
                model_descriptor,
                textures_dir,
                options["force_rebuild"],
            )
        )
        options["extra_progress_cb"]("building cache", 0.5)
        return "/cache/cave"

    monkeypatch.setattr(viewer_window, "_acquire_map_import_inhibitor", acquire)
    monkeypatch.setattr(viewer_window.chunker, "cache_is_valid", lambda _path: False)
    monkeypatch.setattr(viewer_window.chunker, "load_manifest", lambda _path: manifest)
    monkeypatch.setattr(app, "import_and_cache_any", fake_import)

    window = _import_window()
    window._start_import_async(
        descriptor, "/maps", "cave.glb", is_startup=True
    )
    _wait_for_import_worker(window)

    assert calls == [
        ("acquire_inhibitor", "cave.glb"),
        ("import", descriptor, "/maps", False),
        ("close_inhibitor",),
    ]
    assert _queued_import_messages(window) == [
        ("progress", "starting import", 0.0),
        ("progress", "building cache", 0.5),
        ("done", "/cache/cave", "/cache/cave", manifest),
    ]


def test_cached_import_worker_does_not_request_desktop_inhibitor(monkeypatch):
    descriptor = {"obj_path": "/maps/cave.obj"}
    manifest = {"chunks": {}}

    monkeypatch.setattr(
        viewer_window,
        "_acquire_map_import_inhibitor",
        lambda _map_name: (_ for _ in ()).throw(
            AssertionError("cached map loads should not inhibit the desktop")
        ),
    )
    monkeypatch.setattr(viewer_window.chunker, "cache_is_valid", lambda _path: True)
    monkeypatch.setattr(viewer_window.chunker, "get_cache_dir", lambda _path: "/cache/cave")
    monkeypatch.setattr(viewer_window.chunker, "load_manifest", lambda _path: manifest)
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
        ("done", "/cache/cave", "/textures/cave", manifest),
    ]


def test_uncached_import_releases_desktop_inhibitor_after_failure(monkeypatch):
    calls = []
    descriptor = {"glb_path": "/maps/broken.glb"}

    def acquire(map_name):
        calls.append(("acquire_inhibitor", map_name))
        return FakeImportInhibitor(calls)

    def fail_import(*_args, **_options):
        calls.append(("import",))
        raise RuntimeError("parse failed")

    monkeypatch.setattr(viewer_window, "_acquire_map_import_inhibitor", acquire)
    monkeypatch.setattr(viewer_window.chunker, "cache_is_valid", lambda _path: False)
    monkeypatch.setattr(app, "import_and_cache_any", fail_import)

    window = _import_window()
    window._start_import_async(
        descriptor, "/maps", "broken.glb", is_startup=True
    )
    _wait_for_import_worker(window)

    assert calls == [
        ("acquire_inhibitor", "broken.glb"),
        ("import",),
        ("close_inhibitor",),
    ]
    assert _queued_import_messages(window) == [
        ("progress", "starting import", 0.0),
        ("error", "parse failed"),
    ]
