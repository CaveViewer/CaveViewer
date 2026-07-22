"""Tests for recording encoder process helpers."""

from __future__ import annotations

import queue

from caveviewer.gui import recording


def test_recording_output_size_preserves_aspect_and_even_dimensions():
    assert recording.recording_output_size(4000, 2000, 1000) == (2000, 1000)
    assert recording.recording_output_size(1280, 720, 1080) == (1280, 720)
    assert recording.recording_output_size(101, 51, 50) == (98, 50)


def test_build_ffmpeg_command_uses_output_size_without_scale_filter():
    command = recording.build_ffmpeg_command(
        ffmpeg_path="/usr/bin/ffmpeg",
        output_size=(1280, 720),
        fps=30,
        crf=23,
        raw_pix_fmt="rgb24",
        output_path="/recordings/cave.mp4",
    )

    assert command[:2] == ["/usr/bin/ffmpeg", "-hide_banner"]
    assert command[command.index("-s") + 1] == "1280x720"
    assert command[command.index("-pix_fmt") + 1] == "rgb24"
    assert "scale=" not in command
    assert command[-1] == "/recordings/cave.mp4"


def test_signal_writer_stop_replaces_full_frame_with_sentinel():
    frame_queue = queue.Queue(maxsize=1)
    frame_queue.put_nowait(b"old-frame")

    recording.signal_writer_stop(frame_queue)

    assert frame_queue.get_nowait() is None


def test_recording_failure_detail_keeps_messages_concise():
    assert recording.recording_failure_detail("No space left on device") == (
        "Disk may be full"
    )
    assert recording.recording_failure_detail("ENOSPC") == "Disk may be full"
    assert recording.recording_failure_detail("encoder failed") == (
        "Video could not be saved"
    )
