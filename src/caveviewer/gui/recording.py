"""Recording encoder process and worker-thread helpers for the viewer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
import queue
import shutil
import subprocess
import threading
from typing import Any


@dataclass(frozen=True)
class RecordingStopWork:
    """State needed to finalize an active recording encoder."""

    process: subprocess.Popen
    output_path: str | None
    frame_queue: queue.Queue | None
    writer_thread: threading.Thread | None
    stderr_thread: threading.Thread | None
    show_message: bool


@dataclass(frozen=True)
class RecordingStopResult:
    """Terminal status emitted by the recording finalizer worker."""

    output_path: str | None
    returncode: int | None
    stderr_text: str
    writer_error: Exception | None
    dropped_frames: int
    show_message: bool


@dataclass
class RecordingReadbackSlot:
    """One render-thread-owned pixel-buffer slot used for async readback."""

    buffer: Any
    in_flight: bool = False


def resolve_ffmpeg_path(environ: dict[str, str] | None = None) -> str | None:
    """Return the configured, system, or bundled ffmpeg executable path."""
    env = os.environ if environ is None else environ
    configured = env.get("CAVEVIEWER_FFMPEG", "").strip()
    if configured:
        return configured
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def recording_output_size(width: int, height: int, max_height: int) -> tuple[int, int]:
    """Return the even video output size preserving framebuffer aspect ratio."""
    output_height = min(int(height), int(max_height))
    output_height = max(2, (output_height // 2) * 2)
    output_width = max(2, int(round((width * output_height) / max(height, 1))))
    output_width = (output_width // 2) * 2
    return output_width, output_height


def build_ffmpeg_command(
    *,
    ffmpeg_path: str,
    output_size: tuple[int, int],
    fps: int,
    crf: int,
    raw_pix_fmt: str,
    output_path: str,
) -> list[str]:
    """Build the ffmpeg command for raw framebuffer frames."""
    output_width, output_height = output_size
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        raw_pix_fmt,
        "-s",
        f"{output_width}x{output_height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-an",
        "-vf",
        "vflip",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        output_path,
    ]


def recording_display_path(path: str | None) -> str | None:
    """Return a compact user-facing path for recording status text."""
    if not path:
        return None
    home = os.path.expanduser("~")
    try:
        rel = os.path.relpath(path, home)
    except ValueError:
        return path
    if rel.startswith(".."):
        return path
    return os.path.join("~", rel)


def recording_failure_detail(stderr_text: str) -> str:
    """Return concise user-facing detail for a failed recording encoder."""
    lowered = stderr_text.lower()
    if "no space left" in lowered or "disk full" in lowered or "enospc" in lowered:
        return "Disk may be full"
    return "Video could not be saved"


def signal_writer_stop(frame_queue: queue.Queue | None) -> None:
    """Queue a sentinel for the writer thread, dropping one frame if needed."""
    if frame_queue is None:
        return
    try:
        frame_queue.put_nowait(None)
        return
    except queue.Full:
        pass

    try:
        frame_queue.get_nowait()
    except queue.Empty:
        pass

    try:
        frame_queue.put_nowait(None)
    except queue.Full:
        pass


def writer_loop(
    process: subprocess.Popen,
    frame_queue: queue.Queue,
    *,
    set_writer_error: Callable[[Exception], None],
) -> None:
    """Write queued raw frames to the encoder stdin until stopped."""
    try:
        while True:
            frame = frame_queue.get()
            if frame is None:
                break
            stdin = process.stdin
            if stdin is None:
                break
            try:
                stdin.write(frame)
            except (BrokenPipeError, OSError) as exc:
                set_writer_error(exc)
                break
    finally:
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass


def stderr_reader(
    process: subprocess.Popen,
    *,
    append_stderr: Callable[[str], None],
) -> None:
    """Read bounded encoder stderr text through a callback owned by the viewer."""
    pipe = process.stderr
    if pipe is None:
        return
    try:
        for line in iter(pipe.readline, b""):
            if not line:
                break
            append_stderr(line.decode("utf-8", errors="replace"))
    except OSError:
        return


def finalize_stop_worker(
    work: RecordingStopWork,
    *,
    stderr_text: Callable[[], str],
    writer_error: Callable[[], Exception | None],
    dropped_frames: Callable[[], int],
    logger,
) -> RecordingStopResult:
    """Finalize the encoder process and return its terminal result."""
    process = work.process
    finalize_error: Exception | None = None
    try:
        if work.writer_thread is not None:
            work.writer_thread.join(timeout=2.0)
            if work.writer_thread.is_alive():
                logger.warning(
                    "Recording writer did not finish promptly; forcing encoder shutdown."
                )
                try:
                    if process.stdin:
                        process.stdin.close()
                except OSError:
                    pass

        try:
            process.wait(timeout=8.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if work.writer_thread is not None and work.writer_thread.is_alive():
            work.writer_thread.join(timeout=1.0)

        if work.stderr_thread is not None:
            work.stderr_thread.join(timeout=1.0)
    except Exception as exc:
        finalize_error = exc
        logger.warning("Recording finalizer failed: %s", exc)

    return RecordingStopResult(
        output_path=work.output_path,
        returncode=process.returncode,
        stderr_text=stderr_text(),
        writer_error=finalize_error or writer_error(),
        dropped_frames=dropped_frames(),
        show_message=work.show_message,
    )
