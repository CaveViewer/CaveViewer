"""Tests for shared post-save feedback across generated user artifacts."""

from __future__ import annotations

from caveviewer.gui.artifact_capture_controller import (
    ArtifactCapturePresentationController,
)


def test_save_feedback_is_consistent_and_reveals_after_confirmation():
    controller = ArtifactCapturePresentationController()

    saving = controller.saving_status("Video")
    saved = controller.saved_status(
        "Dive trace",
        "/maps/Cave/_guided_dives/trace.jsonl",
        now=30.0,
    )

    assert saving.message == "Saving video…"
    assert saving.detail == "Finishing the file. Keep CaveViewer open."
    assert saving.duration is None
    assert saved.message == "Dive trace saved"
    assert saved.detail == "Opening its location…"
    assert saved.duration == 3.0
    assert controller.take_due_reveals(now=32.99) == ()
    assert controller.take_due_reveals(now=33.0)[0].output_path == (
        "/maps/Cave/_guided_dives/trace.jsonl"
    )


def test_failure_or_non_revealed_save_never_queues_native_reveal():
    controller = ArtifactCapturePresentationController()

    failed = controller.failed_status("Video", "Disk may be full")
    saved = controller.saved_status(
        "Dive trace",
        "/maps/Cave/_guided_dives/trace.jsonl",
        now=30.0,
        reveal=False,
    )

    assert failed.message == "Could not save video"
    assert failed.detail == "Disk may be full"
    assert saved.detail is None
    assert controller.take_due_reveals(now=40.0) == ()
