from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Sequence
from typing import Optional

_CONFIGURED = False
_PROGRESS_LOCK = threading.RLock()
_ACTIVE_PROGRESS_LINE: str | None = None
_ACTIVE_PROGRESS_WIDTH = 0
_ACTIVE_PROGRESS_STREAM = None


class _MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = int(max_level)

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


def _safe_stream_write(stream, text: str) -> bool:
    if stream is None:
        return False
    try:
        stream.write(text)
        stream.flush()
        return True
    except Exception:
        return False


def _clear_progress_line_locked() -> None:
    stream = _ACTIVE_PROGRESS_STREAM or sys.stdout
    width = max(_ACTIVE_PROGRESS_WIDTH, len(_ACTIVE_PROGRESS_LINE or ""))
    if width > 0:
        _safe_stream_write(stream, "\r" + (" " * width) + "\r")


def _redraw_progress_line_locked() -> None:
    if _ACTIVE_PROGRESS_LINE is None:
        return
    stream = _ACTIVE_PROGRESS_STREAM or sys.stdout
    _safe_stream_write(stream, "\r" + _ACTIVE_PROGRESS_LINE)


class _ProgressAwareStreamHandler(logging.StreamHandler):
    """StreamHandler that keeps an active carriage-return progress line intact."""

    def emit(self, record: logging.LogRecord) -> None:
        with _PROGRESS_LOCK:
            had_progress = _ACTIVE_PROGRESS_LINE is not None
            if had_progress:
                _clear_progress_line_locked()
            try:
                super().emit(record)
            finally:
                if had_progress and _ACTIVE_PROGRESS_LINE is not None:
                    _redraw_progress_line_locked()


def format_console_progress_line(
    stage: str,
    fraction: float,
    *,
    bar_width: int = 40,
    stage_width: int = 28,
) -> str:
    """Return CaveViewer's single-line terminal progress bar."""
    fraction = max(0.0, min(1.0, float(fraction)))
    filled = int(bar_width * fraction)
    bar = "#" * filled + "-" * (bar_width - filled)
    return f"  [{bar}] {fraction * 100:5.1f}%  {str(stage):<{stage_width}}"


def set_console_progress_line(text: str, *, stream=None) -> None:
    """Render or update an active single-line progress display."""
    global _ACTIVE_PROGRESS_LINE, _ACTIVE_PROGRESS_WIDTH, _ACTIVE_PROGRESS_STREAM
    target_stream = sys.stdout if stream is None else stream
    line = str(text)
    with _PROGRESS_LOCK:
        if _ACTIVE_PROGRESS_LINE is not None and _ACTIVE_PROGRESS_STREAM is not target_stream:
            _clear_progress_line_locked()
            _ACTIVE_PROGRESS_WIDTH = 0
        padding = " " * max(0, _ACTIVE_PROGRESS_WIDTH - len(line))
        if _safe_stream_write(target_stream, "\r" + line + padding):
            _ACTIVE_PROGRESS_LINE = line
            _ACTIVE_PROGRESS_WIDTH = len(line)
            _ACTIVE_PROGRESS_STREAM = target_stream


def set_console_progress(stage: str, fraction: float, *, stream=None) -> None:
    """Render CaveViewer's standard terminal progress bar."""
    set_console_progress_line(
        format_console_progress_line(stage, fraction),
        stream=stream,
    )


def finish_console_progress_line(*, stream=None) -> bool:
    """Commit the active progress line and disable redraws; return True if active."""
    global _ACTIVE_PROGRESS_LINE, _ACTIVE_PROGRESS_WIDTH, _ACTIVE_PROGRESS_STREAM
    with _PROGRESS_LOCK:
        if _ACTIVE_PROGRESS_LINE is None:
            return False
        target_stream = stream or _ACTIVE_PROGRESS_STREAM or sys.stdout
        _safe_stream_write(target_stream, "\n")
        _ACTIVE_PROGRESS_LINE = None
        _ACTIVE_PROGRESS_WIDTH = 0
        _ACTIVE_PROGRESS_STREAM = None
        return True


def _resolve_level(explicit_level: Optional[int | str] = None) -> int:
    if explicit_level is not None:
        if isinstance(explicit_level, int):
            return explicit_level
        return getattr(logging, str(explicit_level).upper(), logging.INFO)

    env_level = os.getenv("CAVEVIEWER_LOG_LEVEL", "").strip().upper()
    if env_level:
        return getattr(logging, env_level, logging.INFO)
    return logging.INFO


def configure_logging(
    level: Optional[int | str] = None,
    *,
    force: bool = False,
    handlers: Sequence[logging.Handler] | None = None,
) -> None:
    """Configure CaveViewer logging once with a parse-friendly component prefix."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    log_level = _resolve_level(level)

    formatter = logging.Formatter(
        "[%(component)s] %(levelname)s: %(message)s",
        defaults={"component": "CaveViewer"},
    )

    if handlers is None:
        stdout_handler = _ProgressAwareStreamHandler(stream=sys.stdout)
        stdout_handler.setLevel(logging.DEBUG)
        stdout_handler.addFilter(_MaxLevelFilter(logging.WARNING))
        stdout_handler.setFormatter(formatter)

        stderr_handler = _ProgressAwareStreamHandler(stream=sys.stderr)
        stderr_handler.setLevel(logging.ERROR)
        stderr_handler.setFormatter(formatter)
        handlers = (stdout_handler, stderr_handler)
    else:
        for handler in handlers:
            if handler.formatter is None:
                handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)

    _CONFIGURED = True


def get_logger(component: str) -> logging.LoggerAdapter:
    """Return a logger that emits with a stable [Component] prefix."""
    return logging.LoggerAdapter(logging.getLogger("caveviewer"), {"component": component})
