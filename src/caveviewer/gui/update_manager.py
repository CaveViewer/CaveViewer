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

from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.features import FeatureDecision
from caveviewer.gui import update_checker
from caveviewer.gui.download_transport import DownloadCancelled
from caveviewer.gui.platform.desktop_services import DesktopInhibitor
from caveviewer.gui.platform.desktop_notifications import (
    send_desktop_notification,
    withdraw_desktop_notification,
)
from caveviewer.gui.platform.desktop_inhibition import (
    acquire_idle_suspend_inhibitor,
    release_desktop_inhibitor,
)
from caveviewer.gui.platform.runtime import PlatformRuntime
from caveviewer.gui.platform.update_package_install import (
    UpdateInstallationCancelled,
)
from caveviewer.gui.update_checker import (
    UpdateArtifact,
    UpdateAvailable,
    UpdateCheckFailed,
    UpdateCheckOutcome,
    UpdateNotAvailable,
)
from caveviewer.version import APP_NAME


_LOG = get_logger("UpdateManager")
_UPDATE_DOWNLOAD_NOTIFICATION_ID = "caveviewer.update-download"
_UPDATE_DOWNLOAD_SHUTDOWN_TIMEOUT_SECONDS = 2.0


class UpdateState(enum.Enum):
    """Every meaningful state in the application-owned update workflow."""

    IDLE = "idle"
    CHECKING = "checking"
    UP_TO_DATE = "up_to_date"
    AVAILABLE = "available"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    READY = "ready"
    HANDOFF_VERIFYING = "handoff_verifying"
    INSTALLING = "installing"
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
    UpdateState.READY: {UpdateState.HANDOFF_VERIFYING, UpdateState.SHUTDOWN},
    UpdateState.HANDOFF_VERIFYING: {
        UpdateState.READY,
        UpdateState.INSTALLING,
        UpdateState.SHUTDOWN,
    },
    UpdateState.INSTALLING: {UpdateState.SHUTDOWN},
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
    reveal_action_label: str = "Show update"
    install_action_label: str | None = None
    install_requested: bool = False
    error: str | None = None
    automatic_update: FeatureDecision | None = None
    update_package_reveal: FeatureDecision | None = None

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
        platform_runtime: PlatformRuntime,
        check_for_update: Callable[..., UpdateCheckOutcome] | None = None,
        download_update: Callable[..., None] | None = None,
        temp_root: str | None = None,
    ):
        self._current_version = current_version
        self._platform_runtime = platform_runtime
        self._desktop_services = platform_runtime.desktop_services
        self._update_package_reveal_adapter = (
            platform_runtime.update_package_reveal_adapter
        )
        self._update_package_storage_adapter = (
            platform_runtime.update_package_storage_adapter
        )
        self._update_package_installer_adapter = (
            platform_runtime.update_package_installer_adapter
        )
        self._check_for_update = (
            check_for_update or update_checker.check_for_update_target
        )
        self._download_update = (
            download_update or update_checker.download_update_target
        )
        self._update_target = platform_runtime.automatic_update_target
        self._automatic_update_decision = platform_runtime.automatic_update_decision
        self._update_package_reveal_decision = (
            platform_runtime.update_package_reveal_decision
        )
        self._temp_root = temp_root

        self._lock = threading.RLock()
        self._state = UpdateState.IDLE
        self._available_update: UpdateAvailable | None = None
        self._downloaded_bytes = 0
        self._total_bytes: int | None = None
        self._payload_path: str | None = None
        self._error: str | None = None
        self._automatic_reveal_done = False
        self._install_after_download_requested = False
        self._foreground_update_surface_active = False

        self._cancel_event: threading.Event | None = None
        self._persistence_started = False
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
                self._available_update.artifact.version
                if self._available_update is not None
                else None
            )
            return UpdateSnapshot(
                state=self._state,
                current_version=self._current_version,
                available_version=available_version,
                downloaded_bytes=self._downloaded_bytes,
                total_bytes=self._total_bytes,
                payload_path=self._payload_path,
                reveal_action_label=self.reveal_action_label,
                install_action_label=self._install_action_label_locked(),
                install_requested=self._install_after_download_requested,
                error=self._error,
                automatic_update=self._automatic_update_decision,
                update_package_reveal=self._update_package_reveal_decision,
            )

    @property
    def reveal_action_label(self) -> str:
        return self._update_package_reveal_adapter.reveal_action_label()

    def _install_action_label_locked(self) -> str | None:
        """Return the explicit EXE handoff label only for its safe contract."""
        available_update = self._available_update
        if available_update is None:
            return None
        artifact = available_update.artifact
        try:
            supported = self._update_package_installer_adapter.supports_package_kind(
                artifact.package_kind,
                authenticode_certificate_subject=(
                    artifact.authenticode_certificate_subject
                ),
                authenticode_status=artifact.authenticode_status,
            )
            label = self._update_package_installer_adapter.install_action_label()
        except Exception as error:
            _LOG.warning("Could not resolve update install action: %s", error)
            return None
        if not supported:
            return None
        normalized_label = str(label).strip()
        return normalized_label or None

    @property
    def automatic_update_decision(self) -> FeatureDecision:
        """Return the static gate enforced for this manager's update workflow."""
        return self._automatic_update_decision

    @property
    def update_package_reveal_decision(self) -> FeatureDecision:
        """Return the static gate enforced before a verified package is revealed."""
        return self._update_package_reveal_decision

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
            if not self._automatic_update_decision.allows_execution:
                _LOG.info(
                    "Automatic update check is gated: reason=%s",
                    self._automatic_update_decision.reason_code,
                )
                return False
            if self._state != UpdateState.IDLE:
                return False
            self._transition_locked(UpdateState.CHECKING)
            self._available_update = None
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
        outcome: UpdateCheckOutcome | None = None
        unexpected_error: Exception | None = None
        try:
            assert self._update_target is not None
            outcome = self._check_for_update(
                self._current_version,
                update_target=self._update_target,
                tls_trust_adapter=self._platform_runtime.tls_trust_adapter,
            )
        except Exception as exc:
            unexpected_error = exc
            _LOG.exception("Update check worker failed: %s", exc)

        with self._lock:
            if self._state != UpdateState.SHUTDOWN:
                if unexpected_error is not None:
                    self._available_update = None
                    self._error = str(unexpected_error)
                    self._transition_locked(UpdateState.IDLE)
                elif outcome is None:
                    self._available_update = None
                    self._error = "Update check returned no result."
                    self._transition_locked(UpdateState.IDLE)
                elif isinstance(outcome, UpdateCheckFailed):
                    # Automatic checks remain quiet on ordinary network errors;
                    # returning to IDLE permits a later splash to try again.
                    self._available_update = None
                    self._error = outcome.error
                    self._transition_locked(UpdateState.IDLE)
                elif isinstance(outcome, UpdateAvailable):
                    self._available_update = outcome
                    self._error = None
                    self._transition_locked(UpdateState.AVAILABLE)
                elif isinstance(outcome, UpdateNotAvailable):
                    self._available_update = None
                    self._error = None
                    self._transition_locked(UpdateState.UP_TO_DATE)
                else:
                    self._available_update = None
                    self._error = "Update check returned an invalid outcome."
                    self._transition_locked(UpdateState.IDLE)
            done_event.set()

    def start_download(self) -> bool:
        """Start or retry the available update without blocking the caller."""
        with self._lock:
            return self._start_download_locked(install_after_download=False)

    def _start_download_locked(self, *, install_after_download: bool) -> bool:
        """Register a download while preserving the caller's explicit intent."""
        if not self._automatic_update_decision.allows_execution:
            _LOG.info(
                "Automatic update download is gated: reason=%s",
                self._automatic_update_decision.reason_code,
            )
            return False
        if self._state not in {UpdateState.AVAILABLE, UpdateState.FAILED}:
            return False
        available_update = self._available_update
        if available_update is None:
            _LOG.error(
                "Update state %s has no validated update artifact.",
                self._state.value,
            )
            return False

        artifact = available_update.artifact
        self._transition_locked(UpdateState.DOWNLOADING)
        self._downloaded_bytes = 0
        self._total_bytes = artifact.size_bytes
        self._payload_path = None
        self._error = None
        self._automatic_reveal_done = False
        self._install_after_download_requested = install_after_download
        self._persistence_started = False

        cancel_event = threading.Event()
        done_event = threading.Event()
        worker = threading.Thread(
            target=self._run_download,
            args=(artifact, cancel_event, done_event),
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
        except RuntimeError as error:
            self._transition_locked(UpdateState.FAILED)
            self._error = str(error)
            self._install_after_download_requested = False
            done_event.set()
            _LOG.warning("Could not start update download worker: %s", error)
            return False
        return True

    def start_installation(self) -> bool:
        """Download an eligible EXE or start its delayed install handoff."""
        with self._lock:
            if self._state in {UpdateState.AVAILABLE, UpdateState.FAILED}:
                if self._install_action_label_locked() is None:
                    return False
                return self._start_download_locked(install_after_download=True)
            if self._state != UpdateState.READY:
                return False

        return self.install_downloaded_update()

    def install_downloaded_update(self) -> bool:
        """Verify and launch a ready EXE from a worker before closing the GUI."""
        with self._lock:
            if self._state != UpdateState.READY or not self._payload_path:
                return False
            available_update = self._available_update
            if available_update is None or self._install_action_label_locked() is None:
                return False

            artifact = available_update.artifact
            payload_path = self._payload_path
            self._transition_locked(UpdateState.HANDOFF_VERIFYING)
            self._error = None
            self._install_after_download_requested = True
            cancel_event = threading.Event()
            done_event = threading.Event()
            worker = threading.Thread(
                target=self._run_install_handoff,
                args=(artifact, payload_path, cancel_event, done_event),
                name="caveviewer-update-install-handoff",
                # The UI only closes after Popen succeeds. Shutdown cancels this
                # short verification worker before it can launch a process.
                daemon=True,
            )
            self._cancel_event = cancel_event
            self._worker = worker
            self._worker_kind = "handoff"
            self._task_done = done_event
            try:
                worker.start()
            except RuntimeError as error:
                self._transition_locked(UpdateState.READY)
                self._install_after_download_requested = False
                self._error = str(error)
                done_event.set()
                _LOG.warning("Could not start update install handoff worker: %s", error)
                return False
            return True

    def _run_install_handoff(
        self,
        artifact: UpdateArtifact,
        payload_path: str,
        cancel_event: threading.Event,
        done_event: threading.Event,
    ) -> None:
        """Run slow execution-boundary verification away from the Tk thread."""
        next_state = UpdateState.READY
        error: str | None = None
        try:
            if cancel_event.is_set():
                raise UpdateInstallationCancelled("Update installation was cancelled.")
            self._update_package_installer_adapter.install_verified_package(
                payload_path,
                version=artifact.version,
                expected_size_bytes=artifact.size_bytes,
                expected_sha256=artifact.sha256,
                authenticode_certificate_subject=(
                    artifact.authenticode_certificate_subject or ""
                ),
                authenticode_status=artifact.authenticode_status,
                parent_process_id=os.getpid(),
                cancellation_requested=cancel_event.is_set,
            )
            next_state = UpdateState.INSTALLING
        except UpdateInstallationCancelled:
            _LOG.info("Windows update install handoff was cancelled before launch.")
        except Exception as caught_error:
            error = str(caught_error)
            _LOG.warning("Could not start Windows update installation: %s", caught_error)

        with self._lock:
            if self._state != UpdateState.SHUTDOWN:
                self._install_after_download_requested = False
                self._error = error
                self._transition_locked(next_state)
            done_event.set()

    def cancel_download(self) -> bool:
        """Request cooperative cancellation before package persistence begins.

        The worker owns state publication and cleanup, so this method only
        signals its event.  Acquiring the lifecycle lock makes cancellation
        race safely with the hand-off from verification to Downloads.
        """
        with self._lock:
            if self._state not in {UpdateState.DOWNLOADING, UpdateState.VERIFYING}:
                return False
            cancel_event = self._cancel_event
            if (
                self._worker_kind != "download"
                or cancel_event is None
                or cancel_event.is_set()
                or self._persistence_started
            ):
                return False
            cancel_event.set()
            self._install_after_download_requested = False
            _LOG.info("Update download cancellation requested.")
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
        send_desktop_notification(
            self._desktop_services,
            _UPDATE_DOWNLOAD_NOTIFICATION_ID,
            title,
            body,
            priority=priority,
            platform_runtime=self._platform_runtime,
        )

    def _withdraw_download_notification(self) -> None:
        """Remove stale update notifications without affecting update state."""
        withdraw_desktop_notification(
            self._desktop_services,
            _UPDATE_DOWNLOAD_NOTIFICATION_ID,
            platform_runtime=self._platform_runtime,
        )

    def _inhibit_update_download(self) -> DesktopInhibitor | None:
        """Best-effort idle/suspend inhibitor while an update payload downloads."""
        return acquire_idle_suspend_inhibitor(
            self._desktop_services,
            f"{APP_NAME} is downloading an update",
            platform_runtime=self._platform_runtime,
        )

    @staticmethod
    def _close_desktop_inhibitor(inhibitor: DesktopInhibitor | None) -> None:
        """Release a best-effort desktop inhibitor."""
        release_desktop_inhibitor(inhibitor)

    def _run_download(
        self,
        artifact: UpdateArtifact,
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
            self._notify_download(
                "Update Download Started",
                f"Downloading version {artifact.version}",
            )
            inhibitor = self._inhibit_update_download()
            download_dir = tempfile.mkdtemp(
                prefix="caveviewer_update_",
                dir=self._temp_root,
            )
            payload_path = os.path.join(download_dir, "update_payload.bin")
            assert self._update_target is not None
            download_kwargs = {
                "expected_sha256": artifact.sha256,
                "progress_cb": on_progress,
                "cancel_cb": cancel_event.is_set,
                "phase_cb": on_phase,
                "update_target": self._update_target,
                "tls_trust_adapter": self._platform_runtime.tls_trust_adapter,
            }
            self._download_update(
                artifact.download_url,
                artifact.size_bytes,
                payload_path,
                **download_kwargs,
            )
            # Cancellation and promotion are mutually exclusive: if a
            # cancellation request obtains the lifecycle lock first, the
            # verified staging payload is never copied into Downloads.
            with self._lock:
                if cancel_event.is_set():
                    raise DownloadCancelled("Download cancelled")
                self._persistence_started = True
            final_payload_path = (
                self._update_package_storage_adapter.persist_verified_package(
                    payload_path,
                    artifact.download_url,
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
            self._persistence_started = False
            if self._state != UpdateState.SHUTDOWN:
                self._payload_path = final_payload_path
                self._error = error
                self._transition_locked(next_state)
            done_event.set()

    def reveal_download(self, *, automatic: bool = False) -> bool:
        """Reveal the verified package without executing or installing it."""
        if not self._update_package_reveal_decision.allows_execution:
            _LOG.info(
                "Update package reveal is gated: reason=%s",
                self._update_package_reveal_decision.reason_code,
            )
            return False
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
            self._update_package_reveal_adapter.reveal_verified_package(payload_path)
        except Exception as exc:
            _LOG.warning("Could not reveal downloaded update %s: %s", payload_path, exc)
            return False
        return True

    def wait_for_background_task(self, timeout: float | None = None) -> bool:
        """Wait for the currently registered check or download task."""
        with self._lock:
            done_event = self._task_done
        return done_event.wait(timeout)

    def shutdown(
        self,
        *,
        wait: bool = True,
        timeout: float | None = _UPDATE_DOWNLOAD_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        """Cancel an active download and optionally wait briefly for temp cleanup."""
        if timeout is not None and timeout < 0.0:
            raise ValueError("shutdown timeout must be non-negative")

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
            worker.join(timeout=timeout)
            if worker.is_alive():
                _LOG.warning(
                    "Update download shutdown timed out after %.1fs; "
                    "download cleanup will continue in the background.",
                    0.0 if timeout is None else timeout,
                )
