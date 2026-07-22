"""Platform-specific process-priority helpers for background GUI workers."""

from __future__ import annotations

import os

from caveviewer.core.diagnostics.logging import get_logger


_LOG = get_logger("CaveViewer")


def lower_current_process_priority(
    *,
    nice_increment: int = 5,
    environ: dict[str, str] | None = None,
) -> bool:
    """Best-effort lower priority for the current worker process."""
    env = os.environ if environ is None else environ
    if os.name == "nt":
        return _lower_windows_process_priority()
    return _lower_posix_process_priority(nice_increment=nice_increment, environ=env)


def _lower_windows_process_priority() -> bool:
    """Best-effort lower process priority on Windows."""
    try:
        import ctypes

        below_normal_priority_class = 0x00004000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetCurrentProcess()
        if kernel32.SetPriorityClass(handle, below_normal_priority_class):
            _LOG.info("Import child process priority set to below normal.")
            return True
    except Exception as exc:
        _LOG.debug("Could not lower Windows import process priority: %s", exc)
    return False


def _lower_posix_process_priority(
    *,
    nice_increment: int,
    environ: dict[str, str],
) -> bool:
    """Best-effort lower process priority on POSIX desktops."""
    if not hasattr(os, "nice"):
        return False

    raw_increment = environ.get("CAVEVIEWER_IMPORT_NICE", "").strip()
    try:
        increment = int(raw_increment) if raw_increment else nice_increment
    except ValueError:
        increment = nice_increment
    if increment <= 0:
        return False

    try:
        os.nice(increment)
        _LOG.info("Import child process nice value increased by %d.", increment)
        return True
    except OSError as exc:
        _LOG.debug("Could not lower POSIX import process priority: %s", exc)
        return False
