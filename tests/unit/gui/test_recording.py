"""Tests for recording encoder process helpers."""

from __future__ import annotations

import queue
from types import SimpleNamespace

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


def test_start_encoder_session_starts_process_and_worker_threads(monkeypatch):
    popen_calls = []
    created_threads = []

    class FakeProcess:
        stdin = SimpleNamespace(write=lambda _frame: None, close=lambda: None)
        stderr = SimpleNamespace(readline=lambda: b"")

        def poll(self):
            return None

    class FakeThread:
        def __init__(self, *, target, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name
            self.started = False
            created_threads.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(
        recording.subprocess,
        "Popen",
        lambda command, **kwargs: popen_calls.append((command, kwargs)) or FakeProcess(),
    )
    monkeypatch.setattr(recording.threading, "Thread", FakeThread)

    session = recording.start_encoder_session(
        ffmpeg_path="/usr/bin/ffmpeg",
        output_path="/recordings/cave.mp4",
        output_size=(1280, 720),
        viewport=(0, 0, 1280, 720),
        fps=30,
        crf=23,
        raw_pix_fmt="rgb24",
        popen_startup_kwargs={"startupinfo": object()},
    )

    command, popen_kwargs = popen_calls[0]
    assert command[command.index("-s") + 1] == "1280x720"
    assert command[-1] == "/recordings/cave.mp4"
    assert popen_kwargs["stdin"] is recording.subprocess.PIPE
    assert "startupinfo" in popen_kwargs
    assert session.output_size == (1280, 720)
    assert session.viewport == (0, 0, 1280, 720)
    assert session.writer_thread is created_threads[0]
    assert session.stderr_thread is created_threads[1]
    assert [thread.started for thread in created_threads] == [True, True]


def test_recording_encoder_session_keeps_bounded_stderr_and_writer_error():
    process = SimpleNamespace(poll=lambda: None)
    session = recording.RecordingEncoderSession(
        process=process,
        output_path="/recordings/cave.mp4",
        output_size=(2, 2),
        viewport=(0, 0, 2, 2),
        frame_queue=queue.Queue(maxsize=1),
    )
    error = BrokenPipeError("closed")

    session.set_writer_error(error)
    session.append_stderr("x" * 17000)

    assert session.writer_error is error
    assert session.stderr_text() == "x" * 16384
    assert session.stopped_before_finalization() is True
    assert session.stop_work(show_message=True).writer_thread is None


def test_recording_failure_detail_keeps_messages_concise():
    assert recording.recording_failure_detail("No space left on device") == (
        "Disk may be full"
    )
    assert recording.recording_failure_detail("ENOSPC") == "Disk may be full"
    assert recording.recording_failure_detail("encoder failed") == (
        "Video could not be saved"
    )
