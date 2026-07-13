"""Process-lifetime update state, download ownership, and package reveal actions.

The manager deliberately has no Tk or OpenGL dependencies. UI surfaces poll an
immutable snapshot on their owning thread, while the manager's workers perform
network and filesystem work. Closing a splash therefore cannot accidentally
cancel a download that should continue while a map is open.
"""

from __future__ import annotations

import enum
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable

from caveviewer.core.logging_utils import get_logger
from caveviewer.gui import update_checker
from caveviewer.gui.platform import get_desktop_services, get_platform_adapter
from caveviewer.gui.platform.base import SplashPlatformAdapter
from caveviewer.gui.platform.desktop_services import DesktopInhibitor, DesktopServices
from caveviewer.gui.update_checker import (
    DownloadCancelled,
    UpdateCheckResult,
)
from caveviewer.version import APP_NAME


_LOG = get_logger("UpdateManager")
_UPDATE_DOWNLOAD_NOTIFICATION_ID = "caveviewer.update-download"


class UpdateState(enum.Enum):
    """Every meaningful state in the application-owned update workflow."""

    IDLE = "idle"
    CHECKING = "checking"
    UP_TO_DATE = "up_to_date"
    AVAILABLE = "available"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    READY = "ready"
    FAILED = "failed"
    SHUTDOWN = "shutdown"


_ALLOWED_TRANSITIONS = {
    UpdateState.IDLE: {UpdateState.CHECKING, UpdateState.SHUTDOWN},
    UpdateState.CHECKING: {
        UpdateState.IDLE,
        UpdateState.UP_TO_DATE,
        UpdateState.AVAILABLE,
        UpdateState.SHUTDOWN,
    },
    UpdateState.UP_TO_DATE: {UpdateState.SHUTDOWN},
    UpdateState.AVAILABLE: {UpdateState.DOWNLOADING, UpdateState.SHUTDOWN},
    UpdateState.DOWNLOADING: {
        UpdateState.AVAILABLE,
        UpdateState.VERIFYING,
        UpdateState.READY,
        UpdateState.FAILED,
        UpdateState.SHUTDOWN,
    },
    UpdateState.VERIFYING: {
        UpdateState.AVAILABLE,
        UpdateState.READY,
        UpdateState.FAILED,
        UpdateState.SHUTDOWN,
    },
    UpdateState.READY: {UpdateState.SHUTDOWN},
    UpdateState.FAILED: {UpdateState.DOWNLOADING, UpdateState.SHUTDOWN},
    UpdateState.SHUTDOWN: set(),
}


@dataclass(frozen=True)
class UpdateSnapshot:
    """Immutable state copied safely from the manager for a UI refresh."""

    state: UpdateState
    current_version: str
    available_version: str | None = None
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    payload_path: str | None = None
    error: str | None = None

    @property
    def progress_percent(self) -> int:
        if not self.total_bytes or self.total_bytes <= 0:
            return 0
        fraction = self.downloaded_bytes / self.total_bytes
        return max(0, min(100, int(fraction * 100)))


class UpdateManager:
    """Own the update state machine for the lifetime of one app process."""

    def __init__(
        self,
        current_version: str,
        *,
        platform_adapter: SplashPlatformAdapter | None = None,
        check_for_update: Callable[..., UpdateCheckResult] | None = None,
        download_update: Callable[..., None] | None = None,
        desktop_services: DesktopServices | None = None,
        temp_root: str | None = None,
    ):
        self._current_version = current_version
        self._platform_adapter = platform_adapter or get_platform_adapter()
        self._desktop_services = desktop_services or get_desktop_services()
        self._check_for_update = check_for_update or update_checker.check_for_update
        self._download_update = download_update or update_checker.download_update
        self._temp_root = temp_root

        self._lock = threading.RLock()
        self._state = UpdateState.IDLE
        self._result: UpdateCheckResult | None = None
        self._downloaded_bytes = 0
        self._total_bytes: int | None = None
        self._payload_path: str | None = None
        self._error: str | None = None
        self._automatic_reveal_done = False
        self._foreground_update_surface_active = False

        self._cancel_event: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._worker_kind: str | None = None
        self._task_done = threading.Event()
        self._task_done.set()

    def _transition_locked(self, next_state: UpdateState) -> None:
        if next_state == self._state:
            return
        if next_state not in _ALLOWED_TRANSITIONS[self._state]:
            raise RuntimeError(
                f"Invalid update transition: {self._state.value} -> "
                f"{next_state.value}"
            )
        _LOG.info(
            "Update state changed: %s -> %s",
            self._state.value,
            next_state.value,
        )
        self._state = next_state

    def snapshot(self) -> UpdateSnapshot:
        with self._lock:
            available_version = (
                self._result.latest_version if self._result is not None else None
            )
            return UpdateSnapshot(
                state=self._state,
                current_version=self._current_version,
                available_version=available_version,
                downloaded_bytes=self._downloaded_bytes,
                total_bytes=self._total_bytes,
                payload_path=self._payload_path,
                error=self._error,
            )

    @property
    def reveal_action_label(self) -> str:
        return self._platform_adapter.download_reveal_action_label()

    def set_foreground_update_surface_active(self, active: bool) -> None:
        """Tell the manager whether an in-app update surface is visible.

        When a visible splash is already presenting update progress and
        actions, desktop notifications would duplicate the same feedback.
        Background downloads still notify normally after the foreground
        surface is closed.
        """
        with self._lock:
            self._foreground_update_surface_active = bool(active)

    def check_for_updates(self) -> bool:
        """Start the process's asynchronous check when the manager is idle."""
        with self._lock:
            if self._state != UpdateState.IDLE:
                return False
            self._transition_locked(UpdateState.CHECKING)
            self._error = None
            done_event = threading.Event()
            worker = threading.Thread(
                target=self._run_check,
                args=(done_event,),
                name="caveviewer-update-check",
                # A manifest request has no partial files and already has a short
                # timeout, so app shutdown need not wait for it.
                daemon=True,
            )
            self._worker = worker
            self._worker_kind = "check"
            self._task_done = done_event

            # Starting while holding the lifecycle lock prevents shutdown from
            # slipping between worker registration and Thread.start(). The new
            # worker can run immediately, but its state publication waits for
            # this short critical section to finish.
            try:
                worker.start()
            except RuntimeError as exc:
                self._transition_locked(UpdateState.IDLE)
                self._error = str(exc)
                done_event.set()
                _LOG.warning("Could not start update check worker: %s", exc)
                return False
            return True

    def _run_check(self, done_event: threading.Event) -> None:
        result: UpdateCheckResult | None = None
        unexpected_error: Exception | None = None
        try:
            result = self._check_for_update(
                self._current_version,
                install_channel=self._platform_adapter.install_channel(),
            )
        except Exception as exc:
            unexpected_error = exc
            _LOG.exception("Update check worker failed: %s", exc)

        with self._lock:
            if self._state != UpdateState.SHUTDOWN:
                if unexpected_error is not None:
                    self._error = str(unexpected_error)
                    self._transition_locked(UpdateState.IDLE)
                elif result is None:
                    self._error = "Update check returned no result."
                    self._transition_locked(UpdateState.IDLE)
                elif result.error:
                    # Automatic checks remain quiet on ordinary network errors;
                    # returning to IDLE permits a later splash to try again.
                    self._result = result
                    self._error = result.error
                    self._transition_locked(UpdateState.IDLE)
                elif result.update_available:
                    self._result = result
                    self._error = None
                    self._transition_locked(UpdateState.AVAILABLE)
                else:
                    self._result = result
                    self._error = None
                    self._transition_locked(UpdateState.UP_TO_DATE)
            done_event.set()

    def start_download(self) -> bool:
        """Start or retry the available update without blocking the caller."""
        with self._lock:
            if self._state not in {UpdateState.AVAILABLE, UpdateState.FAILED}:
                return False
            if self._result is None or not self._result.download_url:
                return False

            result = self._result
            self._transition_locked(UpdateState.DOWNLOADING)
            self._downloaded_bytes = 0
            self._total_bytes = result.download_size_bytes
            self._payload_path = None
            self._error = None
            self._automatic_reveal_done = False

            cancel_event = threading.Event()
            done_event = threading.Event()
            worker = threading.Thread(
                target=self._run_download,
                args=(result, cancel_event, done_event),
                name="caveviewer-update-download",
                # A partial file must reach its finally block before process exit.
                daemon=False,
            )
            self._cancel_event = cancel_event
            self._worker = worker
            self._worker_kind = "download"
            self._task_done = done_event

            # See check_for_updates(): registration and start are one lifecycle
            # operation, so shutdown always observes a started download worker.
            try:
                worker.start()
            except RuntimeError as exc:
                self._transition_locked(UpdateState.FAILED)
                self._error = str(exc)
                done_event.set()
                _LOG.warning("Could not start update download worker: %s", exc)
                return False
            return True

    def _notify_download(
        self,
        title: str,
        body: str = "",
        *,
        priority: str = "normal",
    ) -> None:
        """Best-effort desktop notification for update download progress."""
        with self._lock:
            if self._foreground_update_surface_active:
                return
        try:
            self._desktop_services.notify(
                _UPDATE_DOWNLOAD_NOTIFICATION_ID,
                title,
                body,
                priority=priority,
            )
        except Exception as exc:
            _LOG.debug("Desktop update notification unavailable: %s", exc)

    def _withdraw_download_notification(self) -> None:
        """Remove stale update notifications without affecting update state."""
        try:
            self._desktop_services.withdraw_notification(
                _UPDATE_DOWNLOAD_NOTIFICATION_ID
            )
        except Exception as exc:
            _LOG.debug("Could not withdraw desktop update notification: %s", exc)

    def _inhibit_update_download(self) -> DesktopInhibitor | None:
        """Best-effort idle/suspend inhibitor while an update payload downloads."""
        try:
            return self._desktop_services.inhibit_idle_suspend(
                f"{APP_NAME} is downloading an update"
            )
        except Exception as exc:
            _LOG.debug("Desktop idle/suspend inhibit unavailable for update: %s", exc)
            return None

    @staticmethod
    def _close_desktop_inhibitor(inhibitor: DesktopInhibitor | None) -> None:
        """Release a best-effort desktop inhibitor."""
        if inhibitor is None:
            return
        try:
            inhibitor.close()
        except Exception as exc:
            _LOG.debug("Could not release desktop update inhibitor: %s", exc)

    def _run_download(
        self,
        result: UpdateCheckResult,
        cancel_event: threading.Event,
        done_event: threading.Event,
    ) -> None:
        download_dir: str | None = None
        next_state = UpdateState.FAILED
        final_payload_path: str | None = None
        error: str | None = None
        inhibitor: DesktopInhibitor | None = None

        def on_progress(downloaded_bytes: int, total_bytes: int | None) -> None:
            with self._lock:
                if self._state != UpdateState.DOWNLOADING:
                    return
                self._downloaded_bytes = max(0, int(downloaded_bytes))
                if total_bytes:
                    self._total_bytes = max(0, int(total_bytes))

        def on_phase(phase: str) -> None:
            if phase != "verifying":
                return
            with self._lock:
                if self._state == UpdateState.DOWNLOADING:
                    self._transition_locked(UpdateState.VERIFYING)

        try:
            if result.latest_version:
                download_body = f"Downloading version {result.latest_version}"
            else:
                download_body = "Downloading the available update"
            self._notify_download(
                "Update Download Started",
                download_body,
            )
            inhibitor = self._inhibit_update_download()
            download_dir = tempfile.mkdtemp(
                prefix="caveviewer_update_",
                dir=self._temp_root,
            )
            payload_path = os.path.join(download_dir, "update_payload.bin")
            self._download_update(
                result.download_url,
                result.download_size_bytes,
                payload_path,
                expected_sha256=result.download_sha256,
                progress_cb=on_progress,
                cancel_cb=cancel_event.is_set,
                phase_cb=on_phase,
            )
            if cancel_event.is_set():
                raise DownloadCancelled("Download cancelled")
            final_payload_path = (
                self._platform_adapter.persist_downloaded_payload(
                    payload_path,
                    result.download_url,
                )
            )
            next_state = UpdateState.READY
            self._notify_download(
                "Update Ready",
                "The update package finished downloading",
            )
        except DownloadCancelled:
            next_state = UpdateState.AVAILABLE
            self._withdraw_download_notification()
            _LOG.info("Update download cancelled.")
        except Exception as exc:
            error = str(exc)
            next_state = UpdateState.FAILED
            self._notify_download(
                "Update Download Failed",
                "The update package could not be downloaded",
                priority="high",
            )
            _LOG.warning(
                "Update download workflow failed: %s: %s",
                type(exc).__name__,
                exc,
            )
        finally:
            self._close_desktop_inhibitor(inhibitor)
            if download_dir:
                try:
                    shutil.rmtree(download_dir)
                except FileNotFoundError:
                    pass
                except OSError as cleanup_exc:
                    _LOG.warning(
                        "Could not remove update temporary directory %s: %s",
                        download_dir,
                        cleanup_exc,
                    )

        with self._lock:
            if self._state != UpdateState.SHUTDOWN:
                self._payload_path = final_payload_path
                self._error = error
                self._transition_locked(next_state)
            done_event.set()

    def reveal_download(self, *, automatic: bool = False) -> bool:
        """Reveal the verified package without executing or installing it."""
        with self._lock:
            if self._state != UpdateState.READY or not self._payload_path:
                return False
            if automatic and self._automatic_reveal_done:
                return False
            if automatic:
                # Mark the automatic attempt before invoking OS integration so a
                # failure cannot produce a new Finder/Explorer window every poll.
                self._automatic_reveal_done = True
            payload_path = self._payload_path

        try:
            self._platform_adapter.reveal_downloaded_payload(payload_path)
        except Exception as exc:
            _LOG.warning("Could not reveal downloaded update %s: %s", payload_path, exc)
            return False
        return True

    def wait_for_background_task(self, timeout: float | None = None) -> bool:
        """Wait for the currently registered check or download task."""
        with self._lock:
            done_event = self._task_done
        return done_event.wait(timeout)

    def shutdown(self, *, wait: bool = True) -> None:
        """Cancel an active download and optionally wait for temp cleanup."""
        with self._lock:
            if self._state != UpdateState.SHUTDOWN:
                self._transition_locked(UpdateState.SHUTDOWN)
            if self._cancel_event is not None:
                self._cancel_event.set()
            worker = self._worker if self._worker_kind == "download" else None

        if (
            wait
            and worker is not None
            and worker.is_alive()
            and worker is not threading.current_thread()
        ):
            worker.join()
