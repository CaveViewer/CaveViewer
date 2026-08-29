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


def test_cancelable_save_and_cleanup_feedback_keep_escape_visible():
    controller = ArtifactCapturePresentationController()

    saving = controller.saving_status("Slice", cancelable=True)
    canceling = controller.canceling_status("Video")
    canceled_video = controller.canceled_status("Video", after_escape=True)
    canceled_trace = controller.canceled_status("Dive trace", after_escape=True)
    canceled_slice = controller.canceled_status("Slice", after_escape=True)
    shortcut_canceled = controller.canceled_status("Video")
    failed = controller.cancellation_failed_status("Dive trace", "permission denied")

    assert saving.message == "Saving slice…"
    assert saving.detail == (
        "Finishing the file. Press Esc to cancel. Keep CaveViewer open."
    )
    assert saving.duration is None
    assert canceling.message == "Canceling video…"
    assert canceling.detail == (
        "Stopping capture and removing partial files. "
        "CaveViewer will close automatically."
    )
    assert canceling.duration is None
    assert canceled_video.message == "Video canceled"
    assert canceled_video.detail == "No video was saved."
    assert canceled_trace.detail == "No dive trace was saved."
    assert canceled_slice.detail == "No slice was saved."
    assert canceled_video.duration == controller.escape_confirmation_seconds == 3.0
    assert shortcut_canceled.duration == controller.confirmation_seconds == 3.0
    assert failed.message == "Could not cancel dive trace"
    assert failed.detail == "permission denied"
    assert failed.duration == controller.escape_confirmation_seconds


def test_exit_save_feedback_names_the_artifact_and_suppresses_file_reveals():
    controller = ArtifactCapturePresentationController()
    controller.saved_status("Video", "/recordings/cave.mp4", now=10.0)

    video = controller.exit_saving_status(("Video",))
    trace = controller.exit_saving_status(("Dive trace",))
    slice_status = controller.exit_saving_status(("Slice",))
    both = controller.exit_saving_status(("Video", "Dive trace"))
    video_and_slice = controller.exit_saving_status(("Video", "Slice"))
    controller.discard_pending_reveals()

    assert video.message == "Finishing video"
    assert video.detail == "Saving the last frames. CaveViewer will close automatically."
    assert trace.message == "Finishing dive trace"
    assert trace.detail == "Saving the final trace. CaveViewer will close automatically."
    assert slice_status.message == "Finishing slice"
    assert slice_status.detail == "Saving the final slice. CaveViewer will close automatically."
    assert both.message == "Finishing captures"
    assert both.duration is None
    assert video_and_slice.detail == (
        "Saving the video and slice. CaveViewer will close automatically."
    )
    assert controller.take_due_reveals(now=20.0) == ()
