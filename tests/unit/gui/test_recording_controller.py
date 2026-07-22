"""Tests for the recording workflow state controller."""

from __future__ import annotations

import pytest

from caveviewer.gui.recording_controller import RecordingStateController


def test_countdown_start_display_and_cancel_status():
    controller = RecordingStateController()

    controller.start_countdown(now=40.0, start_number=3)

    assert controller.countdown_started_at == 40.0
    assert controller.countdown_until == 44.0
    assert controller.countdown_ready(now=43.99) is False
    assert controller.countdown_ready(now=44.0) is True

    display = controller.countdown_display(now=42.1, start_number=3)

    assert display.number == 1
    assert display.progress == pytest.approx(2.1 / 4.0)

    controller.cancel_countdown(now=45.0)

    assert controller.countdown_started_at is None
    assert controller.countdown_until is None
    assert controller.status_message == "Recording canceled"
    assert controller.status_kind == "cancel"
    assert controller.status_until == pytest.approx(47.8)


def test_status_expires_when_read():
    controller = RecordingStateController()

    controller.show_status(
        "Recording saved",
        detail="~/Movies/CaveViewer/out.mp4",
        kind="success",
        now=10.0,
        duration=3.2,
    )

    status = controller.active_status(now=12.0)

    assert status is not None
    assert status.message == "Recording saved"
    assert status.detail == "~/Movies/CaveViewer/out.mp4"
    assert status.kind == "success"

    assert controller.active_status(now=13.2) is None
    assert controller.status_message is None
    assert controller.status_kind == "info"
    assert controller.status_until is None


def test_drop_frames_returns_true_only_for_first_warning():
    controller = RecordingStateController()

    assert controller.drop_frames(0) is False
    assert controller.drop_frames(2) is True
    assert controller.drop_frames(1) is False
    assert controller.dropped_frames == 3


def test_capture_schedule_advances_late_frame_slots():
    controller = RecordingStateController(frame_interval=1.0, next_frame_time=10.0)

    assert controller.due_frame_slots(now=12.2, next_frame_time=controller.next_frame_time) == 3

    controller.advance_next_frame_time(now=12.2, frame_slots=3)

    assert controller.next_frame_time == pytest.approx(13.0)


def test_mark_encoder_started_clears_countdown_and_resets_counters():
    controller = RecordingStateController(
        countdown_started_at=1.0,
        countdown_until=5.0,
        next_frame_time=2.0,
        last_stage_ms=8.0,
        last_drain_ms=4.0,
        dropped_frames=6,
    )

    controller.mark_encoder_started(now=6.5)

    assert controller.countdown_started_at is None
    assert controller.countdown_until is None
    assert controller.next_frame_time == 6.5
    assert controller.last_stage_ms == 0.0
    assert controller.last_drain_ms == 0.0
    assert controller.dropped_frames == 0
