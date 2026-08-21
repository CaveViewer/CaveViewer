"""Bounded Windows diagnostics for the path to the first visible splash.

``StartupDiagnostics`` is owned by the process entry point. It creates one
user-owned text log before ``caveviewer.app`` is imported, attaches that log to
the application's root logger while startup is in progress, and owns one
one-shot ``faulthandler`` watchdog. The splash marks startup complete only
after ``Tk.deiconify()``; that cancels the watchdog and closes the log. This
keeps diagnostics available for a pre-UI hang without retaining a background
timer or verbose file logging during ordinary map viewing.

The module is core-only and uses no Tk, OpenGL, or GUI imports so it can be
used before the interactive application composition starts.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from caveviewer.storage_paths import resolve_application_paths


STARTUP_LOG_FILENAME = "startup.log"
STARTUP_WATCHDOG_SECONDS = 20.0

_ACTIVE_STARTUP_DIAGNOSTICS: "StartupDiagnostics | None" = None
_ACTIVE_STARTUP_DIAGNOSTICS_LOCK = threading.RLock()


class StartupDiagnostics:
    """Own one process' pre-splash log handler and one-shot stack watchdog."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        watchdog_seconds: float = STARTUP_WATCHDOG_SECONDS,
        fault_handler: Any = faulthandler,
    ) -> None:
        self.path = Path(path)
        self._watchdog_seconds = float(watchdog_seconds)
        self._fault_handler = fault_handler
        self._handler = logging.FileHandler(self.path, mode="w", encoding="utf-8")
        self._handler.setLevel(logging.DEBUG)
        self._handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        self._lock = threading.RLock()
        self._watchdog_armed = False
        self._attached_to_root_logger = False
        self._closed = False

    def record(self, stage: str, **context: object) -> bool:
        """Write one flushed, safe startup checkpoint without raising."""

        message = _format_stage_message(stage, context)
        return self._emit(logging.INFO, message)

    def record_exception(self, stage: str, error: BaseException) -> bool:
        """Write one bounded import/bootstrap failure with its traceback."""

        return self._emit(
            logging.ERROR,
            _format_stage_message(
                stage,
                {"error_type": type(error).__name__, "error": str(error)},
            ),
            exc_info=(type(error), error, error.__traceback__),
        )

    def arm_watchdog(self) -> None:
        """Request one all-thread traceback if the splash is not visible in time."""

        with self._lock:
            if self._closed or self._watchdog_armed or self._watchdog_seconds <= 0:
                return
            try:
                self._fault_handler.dump_traceback_later(
                    self._watchdog_seconds,
                    repeat=False,
                    file=self._handler.stream,
                    exit=False,
                )
            except Exception as error:
                self.record(
                    "watchdog_unavailable",
                    error_type=type(error).__name__,
                )
                return
            self._watchdog_armed = True
            self.record("watchdog_armed", timeout_seconds=self._watchdog_seconds)

    def attach_to_root_logger(self) -> None:
        """Include normal application log records while pre-splash startup runs."""

        with self._lock:
            if self._closed or self._attached_to_root_logger:
                return
            root_logger = logging.getLogger()
            root_logger.addHandler(self._handler)
            self._attached_to_root_logger = True
            self.record("application_logging_attached")

    def mark_splash_visible(self) -> None:
        """Finish diagnostics once the first non-OpenGL Tk surface is visible."""

        self._finish("splash_visible")

    def close(self) -> None:
        """Release the watchdog and log handler if startup exits early."""

        self._finish("startup_finished")

    def _finish(self, stage: str) -> None:
        with self._lock:
            if self._closed:
                return
            self.record(stage)
            if self._watchdog_armed:
                try:
                    self._fault_handler.cancel_dump_traceback_later()
                except Exception:
                    pass
                self._watchdog_armed = False
            if self._attached_to_root_logger:
                logging.getLogger().removeHandler(self._handler)
                self._attached_to_root_logger = False
            try:
                self._handler.close()
            finally:
                self._closed = True

    def _emit(self, level: int, message: str, *, exc_info=None) -> bool:
        with self._lock:
            if self._closed:
                return False
            record = logging.LogRecord(
                name="caveviewer.startup",
                level=level,
                pathname=__file__,
                lineno=0,
                msg=message,
                args=(),
                exc_info=exc_info,
            )
            try:
                self._handler.handle(record)
            except Exception:
                return False
            return True


def create_startup_diagnostics(
    *,
    platform_name: str | None = None,
    path: str | os.PathLike[str] | None = None,
    watchdog_seconds: float = STARTUP_WATCHDOG_SECONDS,
    fault_handler: Any = faulthandler,
) -> StartupDiagnostics | None:
    """Start Windows-only pre-UI diagnostics without changing startup on failure."""

    active_platform = sys.platform if platform_name is None else platform_name
    if not active_platform.startswith("win"):
        return None

    try:
        resolved_path = (
            Path(path)
            if path is not None
            else resolve_application_paths(platform_name=active_platform).state_dir
            / "diagnostics"
            / STARTUP_LOG_FILENAME
        )
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        diagnostics = StartupDiagnostics(
            resolved_path,
            watchdog_seconds=watchdog_seconds,
            fault_handler=fault_handler,
        )
    except Exception:
        return None

    diagnostics.record("bootstrap_started", process_id=os.getpid())
    diagnostics.arm_watchdog()
    return diagnostics


def set_active_startup_diagnostics(
    diagnostics: StartupDiagnostics | None,
) -> None:
    """Publish the entry-point-owned diagnostics sink for pre-splash callers."""

    global _ACTIVE_STARTUP_DIAGNOSTICS
    with _ACTIVE_STARTUP_DIAGNOSTICS_LOCK:
        _ACTIVE_STARTUP_DIAGNOSTICS = diagnostics


def get_active_startup_diagnostics() -> StartupDiagnostics | None:
    """Return the current process' pre-splash diagnostic owner, if any."""

    with _ACTIVE_STARTUP_DIAGNOSTICS_LOCK:
        return _ACTIVE_STARTUP_DIAGNOSTICS


def record_startup_stage(stage: str, **context: object) -> bool:
    """Record one stage from app or GUI composition when diagnostics are active."""

    diagnostics = get_active_startup_diagnostics()
    if diagnostics is None:
        return False
    return diagnostics.record(stage, **context)


def mark_startup_splash_visible() -> None:
    """Cancel the active watchdog after Tk has made the splash visible."""

    diagnostics = get_active_startup_diagnostics()
    if diagnostics is not None:
        diagnostics.mark_splash_visible()


def _format_stage_message(stage: str, context: dict[str, object]) -> str:
    normalized_stage = str(stage).strip() or "unspecified"
    if not context:
        return f"stage={normalized_stage}"
    details = " ".join(f"{key}={context[key]!r}" for key in sorted(context))
    return f"stage={normalized_stage} {details}"
