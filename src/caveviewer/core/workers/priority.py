"""Best-effort scheduling priority controls for core-owned worker threads."""

from __future__ import annotations

import os
import sys
import threading

from caveviewer.core.diagnostics.logging import get_logger


_LOG = get_logger("CaveViewer")

STREAMING_WORKER_NICE_ENV_VAR = "CAVEVIEWER_IO_NICE"
DEFAULT_STREAMING_WORKER_NICE_INCREMENT = 5
_MAX_LINUX_NICE = 19


def _resolve_nice_increment(
    raw_value: str | None,
    default: int,
) -> int:
    try:
        increment = int(raw_value.strip()) if raw_value else int(default)
    except (AttributeError, TypeError, ValueError):
        increment = int(default)
    return max(0, increment)


def lower_current_thread_priority(
    *,
    nice_increment: int = DEFAULT_STREAMING_WORKER_NICE_INCREMENT,
    environ: dict[str, str] | None = None,
) -> bool:
    """Lower only the calling worker thread's CPU scheduling priority.

    ``os.nice()`` changes the whole process on POSIX, which would also make
    the GUI/render thread less responsive.  Linux exposes each native thread
    as a schedulable task, so ``setpriority(PRIO_PROCESS, native_tid, ...)``
    can adjust the loader thread without touching the rest of CaveViewer.
    Other platforms are left unchanged until they have an equally safe,
    per-thread implementation.

    The positive increment may be overridden with ``CAVEVIEWER_IO_NICE``.
    Zero disables the adjustment.  This is deliberately best effort: lack of
    platform support or permission must never prevent a cave from loading.
    """
    env = os.environ if environ is None else environ
    increment = _resolve_nice_increment(
        env.get(STREAMING_WORKER_NICE_ENV_VAR),
        nice_increment,
    )
    if increment <= 0 or not sys.platform.startswith("linux"):
        return False

    getpriority = getattr(os, "getpriority", None)
    setpriority = getattr(os, "setpriority", None)
    process_kind = getattr(os, "PRIO_PROCESS", None)
    get_native_id = getattr(threading, "get_native_id", None)
    if not all((getpriority, setpriority, process_kind is not None, get_native_id)):
        return False

    try:
        native_id = get_native_id()
        current_priority = int(getpriority(process_kind, native_id))
        target_priority = min(_MAX_LINUX_NICE, current_priority + increment)
        if target_priority <= current_priority:
            return False
        setpriority(process_kind, native_id, target_priority)
    except Exception as exc:
        _LOG.debug(
            "Could not lower cave-streaming thread priority: %s",
            exc,
        )
        return False

    _LOG.info(
        "Cave-streaming worker %s (native id %d) nice value changed from %d to %d.",
        threading.current_thread().name,
        native_id,
        current_priority,
        target_priority,
    )
    return True
