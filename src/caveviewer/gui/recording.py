"""Recording encoder process and worker-thread helpers for the viewer."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import os
import queue
import shutil
import subprocess
import threading
from typing import Any


RECORDING_MAX_HEIGHT_ENV_VAR = "CAVEVIEWER_RECORDING_MAX_HEIGHT"
# Keep the default conservative until the 1080p render/readback path is fast
# enough to avoid recording-frame skips. Set the env var to 1080 to opt back in.
RECORDING_DEFAULT_MAX_HEIGHT = 720
RECORDING_MIN_OUTPUT_HEIGHT = 240
RECORDING_MAX_OUTPUT_HEIGHT = 4320


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
class RecordingEncoderSession:
    """Own one active ffmpeg encoder process and its worker-thread state."""

    process: subprocess.Popen
    output_path: str
    output_size: tuple[int, int]
    viewport: tuple[int, int, int, int]
    frame_queue: queue.Queue
    writer_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None
    _writer_error: Exception | None = None
    _stderr_parts: list[str] = field(default_factory=list)
    _stderr_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def writer_error(self) -> Exception | None:
        """Return the first encoder-writer failure observed by the session."""
        return self._writer_error

    def set_writer_error(self, exc: Exception) -> None:
        """Record a writer-thread failure for render-thread polling."""
        self._writer_error = exc

    def append_stderr(self, text: str) -> None:
        """Append bounded stderr text emitted by the encoder."""
        with self._stderr_lock:
            self._stderr_parts.append(text)
            joined = "".join(self._stderr_parts)
            if len(joined) > 16384:
                self._stderr_parts = [joined[-16384:]]

    def stderr_text(self) -> str:
        """Return collected encoder stderr text without surrounding whitespace."""
        with self._stderr_lock:
            return "".join(self._stderr_parts).strip()

    def writer_loop(self) -> None:
        """Run the session-owned raw-frame writer worker."""
        writer_loop(
            self.process,
            self.frame_queue,
            set_writer_error=self.set_writer_error,
        )

    def stderr_reader(self) -> None:
        """Run the session-owned stderr reader worker."""
        stderr_reader(self.process, append_stderr=self.append_stderr)

    def start_workers(self) -> None:
        """Start encoder helper threads owned by this session."""
        self.writer_thread = threading.Thread(
            target=self.writer_loop,
            daemon=True,
        )
        self.writer_thread.start()

        self.stderr_thread = threading.Thread(
            target=self.stderr_reader,
            daemon=True,
        )
        self.stderr_thread.start()

    def signal_writer_stop(self) -> None:
        """Ask the writer worker to finish after queued frames are drained."""
        signal_writer_stop(self.frame_queue)

    def stopped_before_finalization(self) -> bool:
        """Return whether the encoder exited or its writer failed early."""
        return self.writer_error is not None or self.process.poll() is not None

    def stop_work(self, *, show_message: bool) -> RecordingStopWork:
        """Package this active session for asynchronous stop finalization."""
        return RecordingStopWork(
            process=self.process,
            output_path=self.output_path,
            frame_queue=self.frame_queue,
            writer_thread=self.writer_thread,
            stderr_thread=self.stderr_thread,
            show_message=show_message,
        )


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


def start_encoder_session(
    *,
    ffmpeg_path: str,
    output_path: str,
    output_size: tuple[int, int],
    viewport: tuple[int, int, int, int],
    fps: int,
    crf: int,
    raw_pix_fmt: str,
    popen_startup_kwargs: Mapping[str, Any] | None = None,
    frame_queue_size: int = 2,
) -> RecordingEncoderSession:
    """Start ffmpeg and return the session that owns its helper workers."""
    command = build_ffmpeg_command(
        ffmpeg_path=ffmpeg_path,
        output_size=output_size,
        fps=fps,
        crf=crf,
        raw_pix_fmt=raw_pix_fmt,
        output_path=output_path,
    )
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
    }
    if popen_startup_kwargs:
        popen_kwargs.update(popen_startup_kwargs)

    process = subprocess.Popen(command, **popen_kwargs)
    frame_queue = queue.Queue(maxsize=frame_queue_size)
    session = RecordingEncoderSession(
        process=process,
        output_path=output_path,
        output_size=output_size,
        viewport=viewport,
        frame_queue=frame_queue,
    )
    try:
        session.start_workers()
    except Exception:
        signal_writer_stop(frame_queue)
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.kill()
        except OSError:
            pass
        raise
    return session


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


def start_stop_finalizer(
    work: RecordingStopWork,
    *,
    result_queue: queue.Queue,
    stderr_text: Callable[[], str],
    writer_error: Callable[[], Exception | None],
    dropped_frames: Callable[[], int],
    logger,
) -> threading.Thread | None:
    """Start the worker that finalizes a stopped encoder session."""

    def run_finalizer() -> None:
        result = finalize_stop_worker(
            work,
            stderr_text=stderr_text,
            writer_error=writer_error,
            dropped_frames=dropped_frames,
            logger=logger,
        )
        result_queue.put(result)

    thread = threading.Thread(
        target=run_finalizer,
        name="CaveViewer-recording-finalizer",
        daemon=False,
    )
    try:
        thread.start()
    except RuntimeError as exc:
        logger.warning("Could not start recording finalizer thread: %s", exc)
        run_finalizer()
        return None
    return thread
