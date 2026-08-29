"""Exercise the viewer-facing Ctrl+C slice lifecycle without OpenGL."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caveviewer.core.map import slicing as map_slicing
from caveviewer.gui import map_history
from caveviewer.gui import preferences
from caveviewer.gui import viewer_window
from caveviewer.gui.platform.presentation import select_presentation_profile


def _slice_window(tmp_path):
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._has_map_loaded = True
    window.cache_dir = str(tmp_path / "parent-cache")
    (tmp_path / "parent-cache").mkdir()
    window.manifest = {"source_obj": "Ginnie Springs.obj"}
    window.color_picker = SimpleNamespace(hide=lambda: None)
    window.controls_overlay = SimpleNamespace(
        is_manual_mode=True,
        hide_help=lambda: None,
    )
    maps_directory = tmp_path / "maps"
    maps_directory.mkdir()
    window._slice_storage_directory = lambda: str(maps_directory)
    window.camera = SimpleNamespace(position=(1.0, 2.0, 3.0))
    return window


def _begin_exit_capture_finalization(
    window,
    *,
    status_presented_at: float | None = None,
):
    """Prepare a lightweight slice viewer double for close-time workflows."""
    workflow = window._ensure_capture_workflow()
    workflow.begin_exit_finalization()
    workflow.exit_status_presented_at = status_presented_at
    return workflow


def test_ctrl_c_workflow_arms_shared_countdown_then_cancels(monkeypatch, tmp_path):
    window = _slice_window(tmp_path)
    calls = []
    window.color_picker = SimpleNamespace(hide=lambda: calls.append("hide_picker"))
    window.controls_overlay = SimpleNamespace(
        is_manual_mode=True,
        hide_help=lambda: calls.append("hide_help"),
    )
    monkeypatch.setattr(viewer_window.time, "perf_counter", lambda: 40.0)
    monkeypatch.setattr(map_slicing, "validate_slice_source", lambda _path: {})

    assert window._toggle_slice()

    controller = window._ensure_slice_selection_controller()
    assert calls == ["hide_picker", "hide_help"]
    assert controller.countdown_active
    assert controller.countdown_started_at == 40.0
    assert controller.countdown_until == 44.0
    assert window._slice_storage_parent == str(tmp_path / "maps")
    assert window._slice_display_base == "Ginnie Springs - Segment 1"

    assert window._toggle_slice()
    assert not controller.countdown_active
    assert window._recording_status_message == "Slice canceled"
    assert window._recording_status_kind == "cancel"


def test_slice_countdown_uses_the_next_segment_for_the_current_cave(monkeypatch, tmp_path):
    window = _slice_window(tmp_path)
    (tmp_path / "maps" / "Ginnie Springs - Segment 1").mkdir()
    monkeypatch.setattr(map_slicing, "validate_slice_source", lambda _path: {})

    assert window._start_slice_countdown()

    assert window._slice_display_base == "Ginnie Springs - Segment 2"
    assert window._slice_root_cave_name == "Ginnie Springs"


def test_slice_cannot_start_while_video_owns_capture(tmp_path):
    window = _slice_window(tmp_path)
    window._recording_session = object()
    window._recording_stop_thread = None

    assert window._start_slice_countdown() is False

    assert not window._ensure_slice_selection_controller().countdown_active
    assert window._recording_status_message == "Capture in progress"
    assert window._recording_status_detail == (
        "Finish or cancel the current video recording before starting a new cave slice."
    )


def test_slice_cannot_start_while_dive_trace_owns_capture(tmp_path):
    window = _slice_window(tmp_path)
    window._manual_dive_trace = object()

    assert window._start_slice_countdown() is False

    assert not window._ensure_slice_selection_controller().countdown_active
    assert window._recording_status_message == "Capture in progress"
    assert window._recording_status_detail == (
        "Finish or cancel the current dive trace before starting a new cave slice."
    )


def test_slice_prefers_visible_parent_map_name_over_opaque_source_model(
    monkeypatch,
    tmp_path,
):
    window = _slice_window(tmp_path)
    parent_map = tmp_path / "Devil s Eye at Ginnie Springs"
    parent_map.mkdir()
    window.map_root = str(parent_map)
    window.manifest = {"source_obj": "D5.obj"}
    monkeypatch.setattr(map_slicing, "validate_slice_source", lambda _path: {})

    assert window._start_slice_countdown()

    assert window._slice_root_cave_name == "Devil s Eye at Ginnie Springs"
    assert window._slice_display_base == (
        "Devil s Eye at Ginnie Springs - Segment 1"
    )


def test_slice_start_anchor_is_captured_only_after_countdown(tmp_path):
    window = _slice_window(tmp_path)
    controller = window._ensure_slice_selection_controller()
    controller.start_countdown(now=10.0, start_number=3)
    window._slice_source_cache_dir = window.cache_dir
    window._slice_storage_parent = str(tmp_path / "maps")
    window._slice_display_base = "Ginnie Springs - Segment 1"
    window._slice_root_cave_name = "Ginnie Springs"

    window._update_slice_export(now=13.99)
    assert controller.start_anchor is None

    window.camera.position = (4.0, 5.0, 6.0)
    window._update_slice_export(now=14.0)

    assert controller.selection_active
    assert controller.start_anchor == (4.0, 5.0, 6.0)


def test_finish_slice_uses_current_camera_as_end_anchor_and_starts_export(tmp_path):
    window = _slice_window(tmp_path)
    selection = window._ensure_slice_selection_controller()
    selection.start_countdown(now=0.0, start_number=0)
    assert selection.begin_selection((1.0, 2.0, 3.0))
    window.camera.position = (7.0, 8.0, 9.0)
    window._slice_source_cache_dir = window.cache_dir
    window._slice_storage_parent = str(tmp_path / "maps")
    window._slice_display_base = "Ginnie Springs - Segment 1"
    window._slice_root_cave_name = "Ginnie Springs"
    launched = []
    statuses = []

    class FakeExporter:
        active = False

        def start(self, request):
            launched.append(request)
            return None

    window.__dict__["_slice_export_controller"] = FakeExporter()
    window._show_artifact_capture_status = lambda status, **_kwargs: statuses.append(status)

    assert window._finish_active_slice()

    assert launched[0].entry_position == (1.0, 2.0, 3.0)
    assert launched[0].bounds.maximum[0] >= 7.0
    assert launched[0].output_dir == str(tmp_path / "maps" / "Ginnie Springs - Segment 1")
    assert launched[0].display_name == "Ginnie Springs - Segment 1"
    assert launched[0].root_cave_name == "Ginnie Springs"
    assert selection.saving
    assert statuses[-1].message == "Saving slice…"
    assert statuses[-1].detail == (
        "Finishing the file. Press Esc to cancel. Keep CaveViewer open."
    )


def test_escape_discards_active_slice_selection_and_context(monkeypatch, tmp_path):
    monkeypatch.setattr(viewer_window.time, "perf_counter", lambda: 10.0)
    window = _slice_window(tmp_path)
    selection = window._ensure_slice_selection_controller()
    selection.start_countdown(now=0.0, start_number=0)
    assert selection.begin_selection((1.0, 2.0, 3.0))
    window._slice_source_cache_dir = window.cache_dir
    window._slice_storage_parent = str(tmp_path / "maps")
    window._slice_display_base = "Ginnie Springs - Segment 1"
    window._slice_root_cave_name = "Ginnie Springs"

    assert window._cancel_active_capture() is True

    assert not selection.selection_active
    assert selection.start_anchor is None
    assert window._slice_source_cache_dir is None
    assert window._slice_storage_parent is None
    assert window._recording_status_message == "Slice canceled"
    assert window._recording_status_detail == "No slice was saved."
    assert window._recording_status_until == pytest.approx(13.0)


def test_escape_requests_slice_export_cleanup(tmp_path):
    window = _slice_window(tmp_path)
    cancel_requests = []

    class FakeExporter:
        active = True

        def request_cancel(self):
            cancel_requests.append(True)
            return True

    window.__dict__["_slice_export_controller"] = FakeExporter()

    assert window._cancel_active_capture() is True

    assert cancel_requests == [True]
    assert window._recording_status_message == "Canceling slice…"
    assert window._recording_status_detail == (
        "Stopping capture and removing partial files. "
        "CaveViewer will close automatically."
    )


def test_slice_storage_directory_uses_the_preferences_map_library(monkeypatch, tmp_path):
    window = object.__new__(viewer_window.CaveViewerWindow)
    configured = tmp_path / "downloaded-maps"
    monkeypatch.setattr(
        preferences,
        "load_preferences",
        lambda: {"map_library_dir": str(configured)},
    )

    assert window._slice_storage_directory() == str(configured)
    assert configured.is_dir()


def test_close_active_slice_uses_last_camera_position_and_defers_window_close(tmp_path):
    window = _slice_window(tmp_path)
    selection = window._ensure_slice_selection_controller()
    selection.start_countdown(now=0.0, start_number=0)
    assert selection.begin_selection((1.0, 2.0, 3.0))
    window._closing_requested = False
    window._recording_session = None
    window._recording_stop_thread = None
    window._manual_dive_trace = None
    window._manual_dive_trace_writers = []
    window.wnd = SimpleNamespace(mouse_exclusivity=True, is_closing=True)
    window._reset_transient_input_state = lambda _reason: None
    launched = []
    window._finish_active_slice = lambda *, closing: launched.append(closing) or True

    window.on_close()

    assert launched == [True]
    assert window._exit_capture_finalization_active()
    assert window.wnd.is_closing is False
    assert window._recording_status_message == "Finishing slice"


def test_close_active_slice_uses_last_camera_position_for_the_export_request(tmp_path):
    window = _slice_window(tmp_path)
    selection = window._ensure_slice_selection_controller()
    selection.start_countdown(now=0.0, start_number=0)
    assert selection.begin_selection((1.0, 2.0, 3.0))
    window.camera.position = (7.0, 8.0, 9.0)
    window._slice_source_cache_dir = window.cache_dir
    window._slice_storage_parent = str(tmp_path / "maps")
    window._slice_display_base = "Ginnie Springs - Segment 1"
    window._slice_root_cave_name = "Ginnie Springs"
    window._closing_requested = False
    window._recording_session = None
    window._recording_stop_thread = None
    window._manual_dive_trace = None
    window._manual_dive_trace_writers = []
    window.wnd = SimpleNamespace(mouse_exclusivity=True, is_closing=True)
    window._reset_transient_input_state = lambda _reason: None
    launched = []

    class FakeExporter:
        active = False

        def start(self, request):
            self.active = True
            launched.append(request)
            return None

    window.__dict__["_slice_export_controller"] = FakeExporter()

    window.on_close()

    assert len(launched) == 1
    request = launched[0]
    assert request.entry_position == (1.0, 2.0, 3.0)
    assert request.bounds.maximum == (12.0, 13.0, 14.0)
    assert window._exit_capture_finalization_active()


def test_slice_of_a_slice_keeps_the_original_cave_name(tmp_path):
    window = _slice_window(tmp_path)
    window.manifest = {
        "source_obj": "Ginnie Springs v1.2 - Segment 1.cvslice",
        "slice": {"root_cave_name": "Ginnie Springs v1.2"},
    }

    assert window._slice_cave_name() == "Ginnie Springs v1.2"


def test_close_during_slice_export_leaves_the_worker_running(tmp_path):
    window = _slice_window(tmp_path)
    window._closing_requested = False
    window._recording_session = None
    window._recording_stop_thread = None
    window._manual_dive_trace = None
    window._manual_dive_trace_writers = []
    window.wnd = SimpleNamespace(mouse_exclusivity=True, is_closing=True)
    window._reset_transient_input_state = lambda _reason: None
    cancel_requests = []

    class FakeExporter:
        active = True

        def request_cancel(self):
            cancel_requests.append(True)
            return True

    window.__dict__["_slice_export_controller"] = FakeExporter()

    window.on_close()

    assert cancel_requests == []
    assert window._exit_capture_finalization_active()
    assert window._recording_status_message == "Finishing slice"


def test_slice_success_uses_the_shared_delayed_directory_reveal(monkeypatch, tmp_path):
    window = _slice_window(tmp_path)
    output = tmp_path / "maps" / "Slice"
    request = map_slicing.SliceExportRequest(
        source_cache_dir=window.cache_dir,
        output_dir=str(output),
        bounds=map_slicing.SliceBounds((0.0, 0.0, 0.0), (2.0, 3.0, 4.0)),
        entry_position=(1.0, 2.0, 3.0),
    )
    success = viewer_window.SliceExportSucceeded(
        request=request,
        output_dir=str(output),
        triangle_count=3,
        chunk_count=1,
        texture_count=1,
    )

    class FakeExporter:
        active = False

        @staticmethod
        def poll():
            return (success,)

    monkeypatch.setattr(map_history, "remember_recent_map_path", lambda _path: None)
    window.__dict__["_slice_export_controller"] = FakeExporter()
    revealed = []
    window._reveal_saved_output = lambda path, **kwargs: revealed.append((path, kwargs))

    window._update_slice_export(now=10.0)
    assert window._recording_status_message == "Slice saved"
    assert window._recording_status_detail == "Opening its location…"

    window._drain_due_saved_artifact_reveals(now=12.99)
    assert revealed == []
    window._drain_due_saved_artifact_reveals(now=13.0)
    assert revealed == [(str(output), {"output_kind": "slice"})]


def test_close_time_slice_success_reveals_before_finishing_shutdown(monkeypatch, tmp_path):
    window = _slice_window(tmp_path)
    output = tmp_path / "maps" / "Slice"
    request = map_slicing.SliceExportRequest(
        source_cache_dir=window.cache_dir,
        output_dir=str(output),
        bounds=map_slicing.SliceBounds((0.0, 0.0, 0.0), (2.0, 3.0, 4.0)),
        entry_position=(1.0, 2.0, 3.0),
    )
    success = viewer_window.SliceExportSucceeded(
        request=request,
        output_dir=str(output),
        triangle_count=3,
        chunk_count=1,
        texture_count=1,
    )

    class FakeExporter:
        active = False

        @staticmethod
        def poll():
            return (success,)

    monkeypatch.setattr(map_history, "remember_recent_map_path", lambda _path: None)
    window.__dict__["_slice_export_controller"] = FakeExporter()
    _begin_exit_capture_finalization(window, status_presented_at=0.0)
    window._recording_session = None
    window._recording_stop_thread = None
    window._manual_dive_trace = None
    window._manual_dive_trace_writers = []
    revealed = []
    closed = []
    window._reveal_saved_output = lambda path, **kwargs: revealed.append((path, kwargs))
    window._complete_window_close = lambda: closed.append(True)

    window._update_slice_export(now=10.0)

    assert window._slice_reveal_before_close
    assert window._complete_exit_capture_finalization_if_ready(
        allow_unpresented_status=True
    )
    assert revealed == [(str(output), {"output_kind": "slice"})]
    assert closed == [True]


def test_slice_map_initial_position_prefers_exported_entry_point():
    position = viewer_window._map_initial_camera_position(
        {
            "slice": {"entry_position": [7.0, 8.0, 9.0]},
            "chunks": {},
        }
    )

    assert tuple(position) == (7.0, 8.0, 9.0)


@pytest.mark.parametrize(
    ("presentation_profile", "primary_modifiers"),
    [
        (select_presentation_profile(platform_name="unsupported"), SimpleNamespace(ctrl=True)),
        (select_presentation_profile(platform_name="darwin"), SimpleNamespace(command=True)),
    ],
)
def test_slice_hotkey_uses_the_platform_primary_modifier(
    presentation_profile,
    primary_modifiers,
):
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._presentation_profile = presentation_profile
    window._has_map_loaded = True
    window.wnd = SimpleNamespace(keys=SimpleNamespace(C=67))
    window._keys_down = set()
    window._key_resolve_cache = {}
    window._raw_command_modifier_down = lambda: False
    calls = []
    window._toggle_slice = lambda: calls.append("toggle") or True

    assert window._handle_slice_hotkey(67, primary_modifiers) is True
    assert calls == ["toggle"]
    assert window._handle_slice_hotkey(68, primary_modifiers) is False
    assert window._handle_slice_hotkey(67, SimpleNamespace(shift=True)) is False
