"""Process lifecycle and exception diagnostics for CaveViewer.

The application diagnostic writer is deliberately independent of the GUI. It
can therefore record failures from the main thread and from worker threads
when an optional diagnostic consumer binds an append-only JSONL file.
"""

from __future__ import annotations

import json
import math
import os
import signal
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Callable

from caveviewer.core.diagnostics.logging import get_logger


APPLICATION_DIAGNOSTICS_SCHEMA_VERSION = 1
_MAX_TRACEBACK_LENGTH = 64 * 1024

_LOG = get_logger("ApplicationDiagnostics")
_ACTIVE_DIAGNOSTICS: "ApplicationDiagnostics | None" = None
_ACTIVE_LOCK = threading.RLock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_LOCK = threading.Lock()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _path_lock(path: str) -> threading.RLock:
    normalized = os.path.abspath(os.fspath(path))
    with _PATH_LOCKS_LOCK:
        lock = _PATH_LOCKS.get(normalized)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[normalized] = lock
        return lock


def append_jsonl_record(
    path: str | os.PathLike[str],
    record: Mapping[str, Any],
    *,
    sync: bool = False,
) -> bool:
    """Append one safe JSON record using the per-path diagnostic lock.

    Application lifecycle writers and optional diagnostic consumers use this
    function. Sharing the lock prevents records from interleaving when a
    worker exception arrives while another thread is writing diagnostics.
    """
    normalized = os.path.abspath(os.fspath(path))
    try:
        line = json.dumps(
            _json_safe(record),
            sort_keys=True,
            separators=(",", ":"),
        )
        os.makedirs(os.path.dirname(normalized), exist_ok=True)
        with _path_lock(normalized):
            with open(normalized, "a", encoding="utf-8") as file_obj:
                file_obj.write(line)
                file_obj.write("\n")
                file_obj.flush()
                if sync:
                    os.fsync(file_obj.fileno())
        return True
    except Exception:
        # Diagnostics must never change application behavior, including during
        # shutdown when the cache directory may already be unavailable.
        return False


def set_active_application_diagnostics(
    diagnostics: "ApplicationDiagnostics | None",
) -> None:
    """Publish the process-owned diagnostic sink for GUI/core boundaries."""
    global _ACTIVE_DIAGNOSTICS
    with _ACTIVE_LOCK:
        _ACTIVE_DIAGNOSTICS = diagnostics


def get_active_application_diagnostics() -> "ApplicationDiagnostics | None":
    """Return the current process diagnostic sink, if the app installed one."""
    with _ACTIVE_LOCK:
        return _ACTIVE_DIAGNOSTICS


class ApplicationDiagnostics:
    """Best-effort process lifecycle and exception recorder.

    The sink has no output path until an optional diagnostics consumer binds
    one. This allows process hooks to be installed before a viewer session
    without creating map-cache files during ordinary viewing.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], str] | None = None,
        monotonic: Callable[[], float] | None = None,
        session_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self._clock = clock or _utc_timestamp
        self._monotonic = monotonic or time.monotonic
        self._started_at = self._monotonic()
        self._metadata = dict(metadata or {})
        self._path: str | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._hooks_installed = False
        self._startup_recorded = False
        self._shutdown_recorded = False
        self._process_exit_recorded = False
        self._previous_excepthook: Callable[..., Any] | None = None
        self._previous_threading_excepthook: Callable[..., Any] | None = None
        self._installed_excepthook: Callable[..., Any] | None = None
        self._installed_threading_excepthook: Callable[..., Any] | None = None
        self._previous_signal_handlers: dict[int, Any] = {}
        self._installed_signal_handlers: dict[int, Callable[..., Any]] = {}
        self._atexit_registered = False

    @property
    def path(self) -> str | None:
        """Return the currently bound diagnostic path, if any."""
        with self._lock:
            return self._path

    def bind_path(self, path: str | os.PathLike[str], **context: Any) -> None:
        """Bind application events to an optional JSONL diagnostic path."""
        normalized = os.path.abspath(os.fspath(path))
        with self._lock:
            if self._closed:
                return
            changed = self._path != normalized
            self._path = normalized
        if not changed:
            return

        if not self._startup_recorded:
            self._startup_recorded = True
            self.record(
                "application_started",
                **self._metadata,
                process_id=os.getpid(),
            )
        self.record(
            "application_diagnostics_bound",
            log_path=normalized,
            **context,
            sync=True,
        )

    def record(self, event: str, *, sync: bool = False, **payload: Any) -> bool:
        """Append one process event without allowing diagnostics to fail work."""
        with self._lock:
            if self._closed or self._path is None:
                return False
            path = self._path
            thread = threading.current_thread()
            record = {
                "ts": self._clock(),
                "session_id": self.session_id,
                "scope": "application",
                "event": str(event),
                "process_id": os.getpid(),
                "thread_name": thread.name,
                "thread_id": thread.ident,
                "schema_version": APPLICATION_DIAGNOSTICS_SCHEMA_VERSION,
                **payload,
            }
        return append_jsonl_record(path, record, sync=sync)

    def record_exception(
        self,
        event: str,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback,
        *,
        fatal: bool,
        thread: threading.Thread | None = None,
        **context: Any,
    ) -> bool:
        """Record an exception with a bounded, human-readable traceback."""
        formatted = "".join(
            traceback.format_exception(exc_type, exc_value, exc_traceback)
        )
        if len(formatted) > _MAX_TRACEBACK_LENGTH:
            formatted = (
                formatted[:_MAX_TRACEBACK_LENGTH]
                + "\n...[traceback truncated]"
            )
        return self.record(
            event,
            exception_type=getattr(exc_type, "__name__", str(exc_type)),
            exception_message=str(exc_value),
            traceback=formatted,
            fatal=bool(fatal),
            exception_thread_name=None if thread is None else thread.name,
            exception_thread_id=None if thread is None else thread.ident,
            **context,
            sync=True,
        )

    def install_hooks(self, *, install_signals: bool = False) -> None:
        """Install exception hooks, optional termination hooks, and atexit."""
        with self._lock:
            if self._closed or self._hooks_installed:
                return
            self._previous_excepthook = sys.excepthook
            self._previous_threading_excepthook = getattr(
                threading,
                "excepthook",
                None,
            )
            self._installed_excepthook = self._handle_main_exception
            sys.excepthook = self._installed_excepthook
            if self._previous_threading_excepthook is not None:
                self._installed_threading_excepthook = self._handle_thread_exception
                threading.excepthook = self._installed_threading_excepthook
            if (
                install_signals
                and threading.current_thread() is threading.main_thread()
            ):
                for signum in (signal.SIGINT, signal.SIGTERM):
                    try:
                        previous = signal.getsignal(signum)
                        handler = self._make_signal_handler(signum)
                        signal.signal(signum, handler)
                    except (OSError, RuntimeError, ValueError):
                        continue
                    self._previous_signal_handlers[signum] = previous
                    self._installed_signal_handlers[signum] = handler
            self._hooks_installed = True
            if not self._atexit_registered:
                import atexit

                atexit.register(self._handle_atexit)
                self._atexit_registered = True

    def finalize(
        self,
        *,
        outcome: str,
        exit_code: int | None,
        reason: str | None = None,
    ) -> None:
        """Record explicit shutdown and process-exit events, then restore hooks."""
        with self._lock:
            if self._closed:
                return
            already_shutdown = self._shutdown_recorded
            self._shutdown_recorded = True

        if not already_shutdown:
            self.record(
                "application_shutdown_started",
                outcome=str(outcome),
                exit_code=exit_code,
                reason=reason,
                elapsed_s=max(0.0, self._monotonic() - self._started_at),
                sync=True,
            )

        with self._lock:
            already_exited = self._process_exit_recorded
            self._process_exit_recorded = True
        if not already_exited:
            self.record(
                "application_process_exit",
                outcome=str(outcome),
                exit_code=exit_code,
                reason=reason,
                explicit=True,
                elapsed_s=max(0.0, self._monotonic() - self._started_at),
                sync=True,
            )

        self._restore_hooks()
        with self._lock:
            self._closed = True

    def _handle_main_exception(self, exc_type, exc_value, exc_traceback) -> None:
        if exc_type is KeyboardInterrupt:
            self.record(
                "application_interrupted",
                reason="uncaught_keyboard_interrupt",
                sync=True,
            )
        else:
            self.record_exception(
                "application_uncaught_exception",
                exc_type,
                exc_value,
                exc_traceback,
                fatal=True,
            )
        previous = self._previous_excepthook
        if previous is not None and previous is not self._handle_main_exception:
            try:
                previous(exc_type, exc_value, exc_traceback)
            except Exception:
                _LOG.exception("Previous uncaught-exception hook failed.")

    def _handle_thread_exception(self, args) -> None:
        self.record_exception(
            "application_thread_exception",
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            fatal=False,
            thread=args.thread,
        )
        previous = self._previous_threading_excepthook
        if previous is not None and previous is not self._handle_thread_exception:
            try:
                previous(args)
            except Exception:
                _LOG.exception("Previous thread-exception hook failed.")

    def _handle_atexit(self) -> None:
        self.finalize(
            outcome="implicit_process_exit",
            exit_code=None,
            reason="atexit",
        )

    def _make_signal_handler(self, signum: int) -> Callable[..., Any]:
        def handle(received_signum: int, frame) -> None:
            try:
                signal_name = signal.Signals(received_signum).name
            except ValueError:
                signal_name = str(received_signum)
            self.record(
                "application_signal_received",
                signal_name=signal_name,
                signal_number=int(received_signum),
                sync=True,
            )
            previous = self._previous_signal_handlers.get(received_signum)
            if previous is signal.SIG_IGN:
                return
            if previous is signal.SIG_DFL:
                if received_signum == signal.SIGINT:
                    raise KeyboardInterrupt
                self.finalize(
                    outcome="signal",
                    exit_code=128 + int(received_signum),
                    reason=signal_name,
                )
                signal.signal(received_signum, signal.SIG_DFL)
                os.kill(os.getpid(), received_signum)
                return
            if callable(previous):
                previous(received_signum, frame)

        return handle

    def _restore_hooks(self) -> None:
        with self._lock:
            if not self._hooks_installed:
                return
            if sys.excepthook is self._installed_excepthook:
                previous = self._previous_excepthook
                if previous is not None:
                    sys.excepthook = previous
            previous_threading = self._previous_threading_excepthook
            if (
                previous_threading is not None
                and getattr(threading, "excepthook", None)
                is self._installed_threading_excepthook
            ):
                threading.excepthook = previous_threading
            for signum, handler in self._installed_signal_handlers.items():
                if signal.getsignal(signum) is handler:
                    previous = self._previous_signal_handlers.get(signum)
                    if previous is not None:
                        try:
                            signal.signal(signum, previous)
                        except (OSError, RuntimeError, ValueError):
                            pass
            self._hooks_installed = False
            self._installed_excepthook = None
            self._installed_threading_excepthook = None
            self._installed_signal_handlers.clear()
            self._previous_signal_handlers.clear()


def _json_safe(value: Any) -> Any:
    """Convert diagnostic payloads to bounded standard-library JSON values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, BaseException):
        return {
            "type": type(value).__name__,
            "message": str(value),
        }
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.hex()
    return str(value)
