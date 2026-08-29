"""Test child-export event ownership without spawning a real process."""

from __future__ import annotations

import queue
from types import SimpleNamespace

from caveviewer.core.map.slicing import SliceBounds, SliceExportRequest
from caveviewer.gui.slice_export_controller import (
    SliceExportCanceled,
    SliceExportController,
    SliceExportFailed,
    SliceExportState,
    SliceExportSucceeded,
)


def _request() -> SliceExportRequest:
    return SliceExportRequest(
        source_cache_dir="/maps/parent",
        output_dir="/maps/output",
        bounds=SliceBounds((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),
        entry_position=(0.5, 0.5, 0.5),
    )


def test_slice_export_controller_reports_progress_and_success():
    events = queue.Queue()
    handle = SimpleNamespace(
        events=events,
        process=SimpleNamespace(exitcode=None, join=lambda **_kwargs: None),
    )
    controller = SliceExportController(start_process=lambda *_args, **_kwargs: handle)

    assert controller.start(_request()) is None
    events.put(("progress", "copying slice chunks", 0.5))
    events.put(("done", "/maps/output", 9, 2, 1))

    updates = controller.poll()

    assert updates[0].stage == "copying slice chunks"
    assert isinstance(updates[-1], SliceExportSucceeded)
    assert updates[-1].triangle_count == 9
    assert controller.state is SliceExportState.SUCCEEDED


def test_slice_export_controller_requests_cooperative_cancellation():
    events = queue.Queue()
    cancellation_requests = []
    handle = SimpleNamespace(
        events=events,
        process=SimpleNamespace(exitcode=None, join=lambda **_kwargs: None),
    )
    controller = SliceExportController(
        start_process=lambda *_args, **_kwargs: handle,
        request_cancel=lambda value: cancellation_requests.append(value) or True,
    )

    assert controller.start(_request()) is None
    assert controller.request_cancel()
    events.put(("cancelled",))

    updates = controller.poll()

    assert cancellation_requests == [handle]
    assert isinstance(updates[-1], SliceExportCanceled)
    assert controller.state is SliceExportState.CANCELED


def test_slice_export_controller_removes_private_staging_after_a_crashed_child(
    monkeypatch,
):
    events = queue.Queue()
    handle = SimpleNamespace(
        events=events,
        process=SimpleNamespace(exitcode=1, pid=1234, join=lambda **_kwargs: None),
    )
    controller = SliceExportController(start_process=lambda *_args, **_kwargs: handle)
    cleaned = []
    monkeypatch.setattr(
        "caveviewer.gui.slice_export_controller.cleanup_slice_staging_dirs",
        lambda output_dir, *, process_id: cleaned.append((output_dir, process_id)),
    )

    request = _request()
    assert controller.start(request) is None
    assert controller.poll() == ()
    assert controller.poll() == ()
    updates = controller.poll()

    assert isinstance(updates[-1], SliceExportFailed)
    assert cleaned == [(request.output_dir, 1234)]
    assert controller.state is SliceExportState.FAILED
