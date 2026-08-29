"""Durable runtime diagnostics for interactive viewer sessions.

Packaged applications may have no useful console, and platform-specific state
directories can be difficult for users to locate. This module owns one
user-profile log for the application's lifetime on every supported desktop,
plus an optional faulthandler target for fatal native failures such as an
access violation.

It deliberately remains GUI-free: application composition decides when to
create it, while GUI code may record narrow viewer-window checkpoints through
the active singleton.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

from caveviewer.core.diagnostics.catalog import (
    DEFAULT_SESSION_LOG_MAX_AGE_SECONDS,
    DEFAULT_SESSION_LOG_RETENTION,
    SESSION_LOG_PREFIX,
    application_log_directory,
    prune_session_logs,
)


RUNTIME_DIAGNOSTICS_LOG_PREFIX = SESSION_LOG_PREFIX

_ACTIVE_RUNTIME_DIAGNOSTICS: "RuntimeDiagnostics | None" = None
_ACTIVE_RUNTIME_DIAGNOSTICS_LOCK = threading.RLock()


class RuntimeDiagnostics:
    """Own one durable text log and optional native-fault traceback sink."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        fault_handler: Any = faulthandler,
    ) -> None:
        self.path = Path(path)
        self.jsonl_path = self.path.with_suffix(".jsonl")
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
        self._attached_to_root_logger = False
        self._fault_handler_enabled = False
        self._closed = False

    def record(self, stage: str, **context: object) -> bool:
        """Write one flushed checkpoint without changing application behavior."""

        return self._emit(logging.INFO, _format_stage_message(stage, context))

    def record_exception(
        self,
        stage: str,
        error: BaseException,
        **context: object,
    ) -> bool:
        """Write a stage, exception details, and traceback to the session log."""

        details = {
            "error_type": type(error).__name__,
            "error": str(error),
            **context,
        }
        return self._emit(
            logging.ERROR,
            _format_stage_message(stage, details),
            exc_info=(type(error), error, error.__traceback__),
        )

    def attach_to_root_logger(self) -> None:
        """Persist normal runtime log records alongside direct checkpoints."""

        with self._lock:
            if self._closed or self._attached_to_root_logger:
                return
            logging.getLogger().addHandler(self._handler)
            self._attached_to_root_logger = True
        self.record("application_logging_attached")

    def enable_fault_handler(self) -> bool:
        """Send fatal Python/native tracebacks to the durable session log."""

        with self._lock:
            if self._closed or self._fault_handler_enabled:
                return False
            is_enabled = getattr(self._fault_handler, "is_enabled", None)
            if callable(is_enabled):
                try:
                    if is_enabled():
                        self.record("fault_handler_already_enabled")
                        return False
                except Exception:
                    pass
            try:
                self._fault_handler.enable(
                    file=self._handler.stream,
                    all_threads=True,
                )
            except Exception as error:
                self.record(
                    "fault_handler_unavailable",
                    error_type=type(error).__name__,
                    error=str(error),
                )
                return False
            self._fault_handler_enabled = True
        self.record("fault_handler_enabled", all_threads=True)
        return True

    def close(self) -> None:
        """Detach the file handler and release the process-owned fault target."""

        with self._lock:
            if self._closed:
                return
            self.record("runtime_diagnostics_closing")
            if self._fault_handler_enabled:
                try:
                    self._fault_handler.disable()
                except Exception as error:
                    self.record(
                        "fault_handler_disable_failed",
                        error_type=type(error).__name__,
                    )
                self._fault_handler_enabled = False
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
                name="caveviewer.runtime",
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


def create_runtime_diagnostics(
    *,
    session_id: str,
    platform_name: str | None = None,
    path: str | os.PathLike[str] | None = None,
    fault_handler: Any = faulthandler,
    retained_session_logs: int = DEFAULT_SESSION_LOG_RETENTION,
    session_log_max_age_seconds: float = DEFAULT_SESSION_LOG_MAX_AGE_SECONDS,
) -> RuntimeDiagnostics | None:
    """Create cross-platform session diagnostics without blocking startup."""

    active_platform = sys.platform if platform_name is None else platform_name
    try:
        resolved_path = (
            Path(path)
            if path is not None
            else application_log_directory(platform_name=active_platform)
            / f"{RUNTIME_DIAGNOSTICS_LOG_PREFIX}{_safe_session_id(session_id)}.log"
        )
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        diagnostics = RuntimeDiagnostics(
            resolved_path,
            fault_handler=fault_handler,
        )
    except Exception:
        return None

    diagnostics.record(
        "runtime_diagnostics_created",
        process_id=os.getpid(),
        session_id=session_id,
    )
    prune_session_logs(
        resolved_path.parent,
        keep=retained_session_logs,
        preserve=(resolved_path,),
        max_age_seconds=session_log_max_age_seconds,
    )
    return diagnostics


def set_active_runtime_diagnostics(
    diagnostics: RuntimeDiagnostics | None,
) -> None:
    """Publish the current process' viewer-session diagnostics sink."""

    global _ACTIVE_RUNTIME_DIAGNOSTICS
    with _ACTIVE_RUNTIME_DIAGNOSTICS_LOCK:
        _ACTIVE_RUNTIME_DIAGNOSTICS = diagnostics


def get_active_runtime_diagnostics() -> RuntimeDiagnostics | None:
    """Return the current process' viewer-session diagnostics sink, if any."""

    with _ACTIVE_RUNTIME_DIAGNOSTICS_LOCK:
        return _ACTIVE_RUNTIME_DIAGNOSTICS


def record_runtime_stage(stage: str, **context: object) -> bool:
    """Write a viewer/native-window checkpoint when runtime diagnostics exist."""

    diagnostics = get_active_runtime_diagnostics()
    if diagnostics is None:
        return False
    return diagnostics.record(stage, **context)


def record_runtime_exception(
    stage: str,
    error: BaseException,
    **context: object,
) -> bool:
    """Write a viewer/native-window exception when runtime diagnostics exist."""

    diagnostics = get_active_runtime_diagnostics()
    if diagnostics is None:
        return False
    return diagnostics.record_exception(stage, error, **context)


def _safe_session_id(value: object) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip())
    return normalized.strip(".-") or "unknown"


def _format_stage_message(stage: str, context: dict[str, object]) -> str:
    normalized_stage = str(stage).strip() or "unspecified"
    if not context:
        return f"stage={normalized_stage}"
    details = " ".join(f"{key}={context[key]!r}" for key in sorted(context))
    return f"stage={normalized_stage} {details}"
