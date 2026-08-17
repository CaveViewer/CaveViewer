"""Exercise the process-owned update state machine and worker lifecycle."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

from caveviewer.core.capabilities import (
    DesktopNotificationRoute,
    DesktopNotificationTarget,
    IdleSuspendInhibitionRoute,
    IdleSuspendInhibitionTarget,
    UpdatePackageRevealRoute,
)
from caveviewer.gui.download_transport import DownloadCancelled
from caveviewer.gui.features import FeatureState
from caveviewer.gui import update_manager
from caveviewer.gui.update_checker import (
    UpdateArtifact,
    UpdateAvailable,
    UpdateCheckFailed,
    UpdateNotAvailable,
)
from caveviewer.gui.platform.probes.updates import select_update_profile
from caveviewer.gui.platform.runtime import create_platform_runtime
from caveviewer.gui.update_manager import UpdateManager, UpdateState


class FakePlatformAdapter:
    def __init__(self, downloads_dir: Path):
        self.downloads_dir = downloads_dir
        self.revealed_paths = []
        self.persisted_payloads = []

    def persist_downloaded_payload(self, temp_payload_path, download_url):
        self.persisted_payloads.append((temp_payload_path, download_url))
        self.downloads_dir.mkdir(exist_ok=True)
        destination = self.downloads_dir / Path(download_url).name
        Path(temp_payload_path).replace(destination)
        return str(destination)

    def download_reveal_action_label(self):
        return "Show Test Package"

    def reveal_downloaded_payload(self, payload_path):
        self.revealed_paths.append(payload_path)


class FakeRuntimePlatformAdapter(FakePlatformAdapter):
    pass


class FakeUpdatePackageRevealAdapter:
    def __init__(
        self,
        *,
        route: UpdatePackageRevealRoute = UpdatePackageRevealRoute.DESKTOP_SERVICE,
    ):
        self._route = route
        self.revealed_paths = []

    def reveal_route(self):
        return self._route

    def reveal_action_label(self):
        return "Show Verified Test Package"

    def reveal_verified_package(self, payload_path):
        self.revealed_paths.append(payload_path)


class FakeUpdatePackageStorageAdapter:
    def __init__(self, destination: Path, *, error: Exception | None = None):
        self.destination = destination
        self.error = error
        self.persisted_payloads = []

    def persist_verified_package(self, temporary_payload_path, download_url):
        self.persisted_payloads.append((temporary_payload_path, download_url))
        if self.error is not None:
            raise self.error
        self.destination.parent.mkdir(exist_ok=True)
        Path(temporary_payload_path).replace(self.destination)
        return str(self.destination)


class FakeDesktopInhibitor:
    def __init__(self, calls):
        self._calls = calls

    def close(self):
        self._calls.append(("close_inhibitor",))

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()


class FakeDesktopServices:
    def __init__(self, *, fail_notify=False, fail_inhibit=False):
        self.calls = []
        self.fail_notify = fail_notify
        self.fail_inhibit = fail_inhibit

    def notify(self, notification_id, title, body="", *, priority="normal"):
        self.calls.append(("notify", notification_id, title, body, priority))
        if self.fail_notify:
            raise RuntimeError("notification service unavailable")

    def withdraw_notification(self, notification_id):
        self.calls.append(("withdraw", notification_id))
        if self.fail_notify:
            raise RuntimeError("notification service unavailable")

    def inhibit_idle_suspend(self, reason, *, parent=None):
        self.calls.append(("inhibit", reason, parent))
        if self.fail_inhibit:
            raise RuntimeError("inhibit service unavailable")
        return FakeDesktopInhibitor(self.calls)


class FakeLogger:
    def __init__(self):
        self.info_messages = []
        self.warning_messages = []

    @staticmethod
    def _format(message, args):
        return message % args if args else str(message)

    def info(self, message, *args):
        self.info_messages.append(self._format(message, args))

    def warning(self, message, *args):
        self.warning_messages.append(self._format(message, args))


def _available_outcome():
    return UpdateAvailable(
        current_version="1.0.63",
        artifact=UpdateArtifact(
            version="1.0.64",
            download_url="https://example.invalid/CaveViewer-1.0.64.zip",
            size_bytes=7,
            sha256="a" * 64,
            package_kind="zip",
        ),
    )


def _runtime(adapter, desktop_services):
    return create_platform_runtime(
        platform_adapter=adapter,
        desktop_services=desktop_services,
        environment={},
    )


def _checked_manager(tmp_path, download_update, *, desktop_services=None):
    adapter = FakePlatformAdapter(tmp_path / "Downloads")
    services = desktop_services or FakeDesktopServices()
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=_runtime(adapter, services),
        check_for_update=lambda *_args, **_kwargs: _available_outcome(),
        download_update=download_update,
        temp_root=str(tmp_path),
    )
    assert manager.check_for_updates()
    assert manager.wait_for_background_task(1)
    assert manager.snapshot().state == UpdateState.AVAILABLE
    return manager, adapter


def test_manager_requires_a_process_owned_runtime(tmp_path):
    with pytest.raises(TypeError, match="platform_runtime"):
        UpdateManager("1.0.63", temp_root=str(tmp_path))


def test_check_transitions_through_checking_to_available(tmp_path):
    check_started = threading.Event()
    release_check = threading.Event()

    def check_for_update(*_args, **_kwargs):
        check_started.set()
        assert release_check.wait(1)
        return _available_outcome()

    adapter = FakePlatformAdapter(tmp_path / "Downloads")
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=_runtime(adapter, FakeDesktopServices()),
        check_for_update=check_for_update,
        temp_root=str(tmp_path),
    )
    try:
        assert manager.check_for_updates()
        assert check_started.wait(1)
        assert manager.snapshot().state == UpdateState.CHECKING

        release_check.set()
        assert manager.wait_for_background_task(1)
        snapshot = manager.snapshot()
        assert snapshot.state == UpdateState.AVAILABLE
        assert snapshot.available_version == "1.0.64"
        assert isinstance(manager._available_update, UpdateAvailable)
        assert manager._available_update.artifact.sha256 == "a" * 64
    finally:
        manager.shutdown()


def test_up_to_date_result_is_checked_only_once_per_process(tmp_path):
    checks = []

    def check_for_update(current_version, **_kwargs):
        checks.append(current_version)
        return UpdateNotAvailable(
            current_version=current_version,
            latest_version=current_version,
        )

    adapter = FakePlatformAdapter(tmp_path / "Downloads")
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=_runtime(adapter, FakeDesktopServices()),
        check_for_update=check_for_update,
        temp_root=str(tmp_path),
    )
    try:
        assert manager.check_for_updates()
        assert manager.wait_for_background_task(1)
        assert manager.snapshot().state == UpdateState.UP_TO_DATE
        assert manager.snapshot().available_version is None
        assert manager._available_update is None
        assert not manager.start_download()

        assert not manager.check_for_updates()
        assert checks == ["1.0.63"]
    finally:
        manager.shutdown()


def test_failed_check_does_not_expose_a_download_action(tmp_path):
    adapter = FakePlatformAdapter(tmp_path / "Downloads")
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=_runtime(adapter, FakeDesktopServices()),
        check_for_update=lambda current_version, **_kwargs: UpdateCheckFailed(
            current_version=current_version,
            error="Update manifest signature could not be verified.",
        ),
        temp_root=str(tmp_path),
    )
    try:
        assert manager.check_for_updates()
        assert manager.wait_for_background_task(1)

        snapshot = manager.snapshot()
        assert snapshot.state is UpdateState.IDLE
        assert snapshot.available_version is None
        assert snapshot.error == "Update manifest signature could not be verified."
        assert manager._available_update is None
        assert not manager.start_download()
    finally:
        manager.shutdown()


def test_bare_artifact_is_not_an_available_check_outcome(tmp_path):
    adapter = FakePlatformAdapter(tmp_path / "Downloads")
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=_runtime(adapter, FakeDesktopServices()),
        check_for_update=lambda *_args, **_kwargs: _available_outcome().artifact,
        temp_root=str(tmp_path),
    )
    try:
        assert manager.check_for_updates()
        assert manager.wait_for_background_task(1)

        snapshot = manager.snapshot()
        assert snapshot.state is UpdateState.IDLE
        assert snapshot.available_version is None
        assert snapshot.error == "Update check returned an invalid outcome."
        assert manager._available_update is None
        assert not manager.start_download()
    finally:
        manager.shutdown()


def test_download_reports_progress_verifies_persists_and_cleans_temp_dir(tmp_path):
    observed_states = []
    manager = None

    def download_update(
        _url,
        _expected_size,
        destination,
        *,
        expected_sha256,
        progress_cb,
        cancel_cb,
        phase_cb,
        **_kwargs,
    ):
        assert expected_sha256 == "a" * 64
        assert not cancel_cb()
        Path(destination).write_bytes(b"payload")
        progress_cb(4, 8)
        observed_states.append(manager.snapshot())
        phase_cb("verifying")
        observed_states.append(manager.snapshot())

    manager, adapter = _checked_manager(tmp_path, download_update)
    try:
        assert manager.reveal_action_label == "Show Test Package"
        assert manager.update_package_reveal_decision.state is FeatureState.ENABLED
        assert manager.start_download()
        assert manager.wait_for_background_task(1)

        assert [item.state for item in observed_states] == [
            UpdateState.DOWNLOADING,
            UpdateState.VERIFYING,
        ]
        assert observed_states[0].progress_percent == 50

        snapshot = manager.snapshot()
        assert snapshot.state == UpdateState.READY
        assert snapshot.payload_path is not None
        assert Path(snapshot.payload_path).read_bytes() == b"payload"
        assert len(adapter.persisted_payloads) == 1
        temporary_payload_path, download_url = adapter.persisted_payloads[0]
        assert Path(temporary_payload_path).name == "update_payload.bin"
        assert download_url == _available_outcome().artifact.download_url
        assert not list(tmp_path.glob("caveviewer_update_*"))

        assert manager.reveal_download(automatic=True)
        assert not manager.reveal_download(automatic=True)
        assert manager.reveal_download()
        assert adapter.revealed_paths == [
            snapshot.payload_path,
            snapshot.payload_path,
        ]
    finally:
        manager.shutdown()


def test_download_uses_desktop_notification_and_inhibit(tmp_path):
    desktop_services = FakeDesktopServices()

    def download_update(
        _url,
        _expected_size,
        destination,
        *,
        phase_cb,
        **_kwargs,
    ):
        Path(destination).write_bytes(b"payload")
        phase_cb("verifying")

    manager, _adapter = _checked_manager(
        tmp_path,
        download_update,
        desktop_services=desktop_services,
    )
    try:
        assert manager.start_download()
        assert manager.wait_for_background_task(1)
        assert manager.snapshot().state == UpdateState.READY
    finally:
        manager.shutdown()

    assert desktop_services.calls == [
        (
            "notify",
            "caveviewer.update-download",
            "Update Download Started",
            "Downloading version 1.0.64",
            "normal",
        ),
        ("inhibit", "CaveViewer is downloading an update", None),
        (
            "notify",
            "caveviewer.update-download",
            "Update Ready",
            "The update package finished downloading",
            "normal",
        ),
        ("close_inhibitor",),
    ]


def test_foreground_update_surface_suppresses_duplicate_desktop_notifications(
    tmp_path,
):
    desktop_services = FakeDesktopServices()

    def download_update(
        _url,
        _expected_size,
        destination,
        *,
        phase_cb,
        **_kwargs,
    ):
        Path(destination).write_bytes(b"payload")
        phase_cb("verifying")

    manager, _adapter = _checked_manager(
        tmp_path,
        download_update,
        desktop_services=desktop_services,
    )
    try:
        manager.set_foreground_update_surface_active(True)
        assert manager.start_download()
        assert manager.wait_for_background_task(1)
        assert manager.snapshot().state == UpdateState.READY
    finally:
        manager.set_foreground_update_surface_active(False)
        manager.shutdown()

    assert desktop_services.calls == [
        ("inhibit", "CaveViewer is downloading an update", None),
        ("close_inhibitor",),
    ]


def test_desktop_notification_and_inhibit_failures_do_not_break_download(tmp_path):
    desktop_services = FakeDesktopServices(fail_notify=True, fail_inhibit=True)

    def download_update(_url, _expected_size, destination, **_kwargs):
        Path(destination).write_bytes(b"payload")

    manager, _adapter = _checked_manager(
        tmp_path,
        download_update,
        desktop_services=desktop_services,
    )
    try:
        assert manager.start_download()
        assert manager.wait_for_background_task(1)
        snapshot = manager.snapshot()
        assert snapshot.state == UpdateState.READY
        assert snapshot.payload_path is not None
        assert Path(snapshot.payload_path).read_bytes() == b"payload"
    finally:
        manager.shutdown()


def test_unavailable_notification_route_does_not_break_update_download(tmp_path):
    class NoopNotificationDesktopServices(FakeDesktopServices):
        def desktop_notification_target(self):
            return DesktopNotificationTarget(DesktopNotificationRoute.NOOP)

    desktop_services = NoopNotificationDesktopServices()

    def download_update(_url, _expected_size, destination, **_kwargs):
        Path(destination).write_bytes(b"payload")

    manager, _adapter = _checked_manager(
        tmp_path,
        download_update,
        desktop_services=desktop_services,
    )
    try:
        assert manager.start_download()
        assert manager.wait_for_background_task(1)
        assert manager.snapshot().state is UpdateState.READY
    finally:
        manager.shutdown()

    assert desktop_services.calls == [
        ("inhibit", "CaveViewer is downloading an update", None),
        ("close_inhibitor",),
    ]


def test_unavailable_inhibition_route_does_not_break_update_download(tmp_path):
    class NoopInhibitionDesktopServices(FakeDesktopServices):
        def idle_suspend_inhibition_target(self):
            return IdleSuspendInhibitionTarget(IdleSuspendInhibitionRoute.NOOP)

    desktop_services = NoopInhibitionDesktopServices()

    def download_update(_url, _expected_size, destination, **_kwargs):
        Path(destination).write_bytes(b"payload")

    manager, _adapter = _checked_manager(
        tmp_path,
        download_update,
        desktop_services=desktop_services,
    )
    try:
        assert manager.start_download()
        assert manager.wait_for_background_task(1)
        assert manager.snapshot().state is UpdateState.READY
    finally:
        manager.shutdown()

    assert not any(call[0] == "inhibit" for call in desktop_services.calls)
    assert not any(call[0] == "close_inhibitor" for call in desktop_services.calls)


def test_failed_download_can_retry_without_retaining_partial_files(tmp_path):
    attempts = 0

    def download_update(
        _url,
        _expected_size,
        destination,
        *,
        phase_cb,
        **_kwargs,
    ):
        nonlocal attempts
        attempts += 1
        Path(destination).write_bytes(b"partial" if attempts == 1 else b"payload")
        if attempts == 1:
            raise OSError("connection reset")
        phase_cb("verifying")

    manager, _adapter = _checked_manager(tmp_path, download_update)
    try:
        assert manager.start_download()
        assert manager.wait_for_background_task(1)
        assert manager.snapshot().state == UpdateState.FAILED
        assert manager.snapshot().error == "connection reset"
        assert not list(tmp_path.glob("caveviewer_update_*"))

        assert manager.start_download()
        assert manager.wait_for_background_task(1)
        assert manager.snapshot().state == UpdateState.READY
        assert attempts == 2
        assert not list(tmp_path.glob("caveviewer_update_*"))
    finally:
        manager.shutdown()


def test_cancel_download_reuses_worker_cleanup_and_permits_retry(tmp_path):
    download_started = threading.Event()
    release_download = threading.Event()
    attempts = 0

    def download_update(
        _url,
        _expected_size,
        destination,
        *,
        cancel_cb,
        phase_cb,
        **_kwargs,
    ):
        nonlocal attempts
        attempts += 1
        Path(destination).write_bytes(b"partial" if attempts == 1 else b"payload")
        if attempts == 1:
            download_started.set()
            assert release_download.wait(1)
            assert cancel_cb()
            raise DownloadCancelled("cancelled")
        phase_cb("verifying")

    desktop_services = FakeDesktopServices()
    manager, adapter = _checked_manager(
        tmp_path,
        download_update,
        desktop_services=desktop_services,
    )
    try:
        assert manager.start_download()
        assert download_started.wait(1)
        assert manager.snapshot().state is UpdateState.DOWNLOADING
        assert list(tmp_path.glob("caveviewer_update_*"))

        assert manager.cancel_download()
        assert not manager.cancel_download()
        # The requesting thread only signals the worker; cleanup and the
        # AVAILABLE transition happen after the worker leaves its boundary.
        assert manager.snapshot().state is UpdateState.DOWNLOADING
        assert list(tmp_path.glob("caveviewer_update_*"))
        assert adapter.persisted_payloads == []

        release_download.set()
        assert manager.wait_for_background_task(1)

        snapshot = manager.snapshot()
        assert snapshot.state is UpdateState.AVAILABLE
        assert snapshot.available_version == "1.0.64"
        assert snapshot.payload_path is None
        assert snapshot.error is None
        assert not list(tmp_path.glob("caveviewer_update_*"))
        assert ("withdraw", "caveviewer.update-download") in desktop_services.calls
        assert ("close_inhibitor",) in desktop_services.calls

        assert manager.start_download()
        assert manager.wait_for_background_task(1)
        assert manager.snapshot().state is UpdateState.READY
        assert attempts == 2
    finally:
        manager.shutdown()


def test_cancel_during_verification_prevents_package_persistence(tmp_path):
    verification_started = threading.Event()
    release_verification = threading.Event()

    def download_update(
        _url,
        _expected_size,
        destination,
        *,
        cancel_cb,
        phase_cb,
        **_kwargs,
    ):
        Path(destination).write_bytes(b"verified staging payload")
        phase_cb("verifying")
        verification_started.set()
        assert release_verification.wait(1)
        # Return normally after the cancellation signal to exercise the
        # manager's pre-persistence check rather than transport behavior.
        assert cancel_cb()

    desktop_services = FakeDesktopServices()
    manager, adapter = _checked_manager(
        tmp_path,
        download_update,
        desktop_services=desktop_services,
    )
    try:
        assert manager.start_download()
        assert verification_started.wait(1)
        assert manager.snapshot().state is UpdateState.VERIFYING

        assert manager.cancel_download()
        assert manager.snapshot().state is UpdateState.VERIFYING
        assert adapter.persisted_payloads == []
        assert list(tmp_path.glob("caveviewer_update_*"))

        release_verification.set()
        assert manager.wait_for_background_task(1)

        snapshot = manager.snapshot()
        assert snapshot.state is UpdateState.AVAILABLE
        assert snapshot.payload_path is None
        assert adapter.persisted_payloads == []
        assert not list(tmp_path.glob("caveviewer_update_*"))
        assert not (tmp_path / "Downloads").exists()
        assert ("withdraw", "caveviewer.update-download") in desktop_services.calls
        assert ("close_inhibitor",) in desktop_services.calls
    finally:
        manager.shutdown()


@pytest.mark.parametrize(
    "state",
    (
        UpdateState.IDLE,
        UpdateState.CHECKING,
        UpdateState.UP_TO_DATE,
        UpdateState.AVAILABLE,
        UpdateState.READY,
        UpdateState.FAILED,
        UpdateState.SHUTDOWN,
    ),
)
def test_cancel_download_rejects_nonactive_states(tmp_path, state):
    adapter = FakePlatformAdapter(tmp_path / "Downloads")
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=_runtime(adapter, FakeDesktopServices()),
        temp_root=str(tmp_path),
    )
    try:
        with manager._lock:
            manager._state = state

        assert not manager.cancel_download()
    finally:
        manager.shutdown()


def test_cancel_download_preserves_an_already_verified_package(tmp_path):
    payload_path = tmp_path / "Downloads" / "CaveViewer-1.0.64.zip"
    payload_path.parent.mkdir()
    payload_path.write_bytes(b"verified package")
    adapter = FakePlatformAdapter(tmp_path / "Downloads")
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=_runtime(adapter, FakeDesktopServices()),
        temp_root=str(tmp_path),
    )
    try:
        with manager._lock:
            manager._state = UpdateState.READY
            manager._payload_path = str(payload_path)

        assert not manager.cancel_download()
        assert manager.snapshot().state is UpdateState.READY
        assert manager.snapshot().payload_path == str(payload_path)
        assert payload_path.read_bytes() == b"verified package"
    finally:
        manager.shutdown()


def test_shutdown_waits_for_download_cancellation_and_partial_cleanup(tmp_path):
    download_started = threading.Event()
    release_download = threading.Event()
    shutdown_returned = threading.Event()

    def download_update(
        _url,
        _expected_size,
        destination,
        *,
        cancel_cb,
        **_kwargs,
    ):
        Path(destination).write_bytes(b"partial")
        download_started.set()
        assert release_download.wait(1)
        if cancel_cb():
            raise DownloadCancelled("cancelled")
        raise AssertionError("shutdown should request cancellation")

    desktop_services = FakeDesktopServices()
    manager, _adapter = _checked_manager(
        tmp_path, download_update, desktop_services=desktop_services
    )
    assert manager.start_download()
    assert download_started.wait(1)
    cancel_event = manager._cancel_event
    assert cancel_event is not None

    def shutdown_manager():
        manager.shutdown()
        shutdown_returned.set()

    shutdown_thread = threading.Thread(target=shutdown_manager)
    shutdown_thread.start()
    assert cancel_event.wait(1)
    assert manager.snapshot().state == UpdateState.SHUTDOWN
    assert not shutdown_returned.is_set()

    release_download.set()
    assert shutdown_returned.wait(1)
    shutdown_thread.join()

    assert not list(tmp_path.glob("caveviewer_update_*"))
    assert ("withdraw", "caveviewer.update-download") in desktop_services.calls
    assert ("close_inhibitor",) in desktop_services.calls


def test_shutdown_timeout_returns_while_download_cleanup_is_blocked(
    monkeypatch,
    tmp_path,
):
    download_started = threading.Event()
    release_download = threading.Event()
    fake_log = FakeLogger()
    monkeypatch.setattr(update_manager, "_LOG", fake_log)

    def download_update(
        _url,
        _expected_size,
        destination,
        *,
        cancel_cb,
        **_kwargs,
    ):
        Path(destination).write_bytes(b"partial")
        download_started.set()
        assert release_download.wait(1)
        if cancel_cb():
            raise DownloadCancelled("cancelled")
        raise AssertionError("shutdown should request cancellation")

    desktop_services = FakeDesktopServices()
    manager, _adapter = _checked_manager(
        tmp_path, download_update, desktop_services=desktop_services
    )
    assert manager.start_download()
    assert download_started.wait(1)
    cancel_event = manager._cancel_event
    assert cancel_event is not None

    manager.shutdown(timeout=0.0)

    assert cancel_event.is_set()
    assert manager.snapshot().state == UpdateState.SHUTDOWN
    assert any(
        message.startswith("Update download shutdown timed out after 0.0s")
        for message in fake_log.warning_messages
    )
    assert list(tmp_path.glob("caveviewer_update_*"))

    release_download.set()
    assert manager.wait_for_background_task(1)

    assert not list(tmp_path.glob("caveviewer_update_*"))
    assert ("withdraw", "caveviewer.update-download") in desktop_services.calls
    assert ("close_inhibitor",) in desktop_services.calls


def test_shutdown_rejects_negative_timeout(tmp_path):
    adapter = FakePlatformAdapter(tmp_path / "Downloads")
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=_runtime(adapter, FakeDesktopServices()),
        temp_root=str(tmp_path),
    )

    with pytest.raises(ValueError, match="shutdown timeout must be non-negative"):
        manager.shutdown(timeout=-0.1)


def test_non_actionable_states_reject_download_and_reveal(tmp_path):
    adapter = FakePlatformAdapter(tmp_path / "Downloads")
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=_runtime(adapter, FakeDesktopServices()),
        temp_root=str(tmp_path),
    )
    try:
        assert not manager.start_download()
        assert not manager.reveal_download()
    finally:
        manager.shutdown()


def test_runtime_package_reveal_adapter_controls_label_and_action(tmp_path):
    payload_path = tmp_path / "CaveViewer.zip"
    payload_path.write_bytes(b"verified package")
    platform_adapter = FakeRuntimePlatformAdapter(tmp_path / "Downloads")
    reveal_adapter = FakeUpdatePackageRevealAdapter()
    runtime = create_platform_runtime(
        platform_adapter=platform_adapter,
        desktop_services=FakeDesktopServices(),
        update_package_reveal_adapter=reveal_adapter,
        environment={},
        platform_name="freebsd",
    )
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=runtime,
        temp_root=str(tmp_path),
    )
    try:
        with manager._lock:
            manager._state = UpdateState.READY
            manager._payload_path = str(payload_path)

        assert manager.reveal_action_label == "Show Verified Test Package"
        assert manager.update_package_reveal_decision.state is FeatureState.ENABLED
        assert manager.reveal_download()
        assert reveal_adapter.revealed_paths == [str(payload_path)]
        assert platform_adapter.revealed_paths == []
    finally:
        manager.shutdown()


def test_runtime_storage_adapter_owns_verified_package_persistence(tmp_path):
    platform_adapter = FakeRuntimePlatformAdapter(tmp_path / "Downloads")
    storage_adapter = FakeUpdatePackageStorageAdapter(
        tmp_path / "Stored Packages" / "CaveViewer-1.0.64.zip"
    )
    runtime = create_platform_runtime(
        platform_adapter=platform_adapter,
        desktop_services=FakeDesktopServices(),
        update_package_storage_adapter=storage_adapter,
        environment={},
    )

    def download_update(_url, _expected_size, destination, *, phase_cb, **_kwargs):
        Path(destination).write_bytes(b"payload")
        phase_cb("verifying")

    manager = UpdateManager(
        "1.0.63",
        platform_runtime=runtime,
        check_for_update=lambda *_args, **_kwargs: _available_outcome(),
        download_update=download_update,
        temp_root=str(tmp_path),
    )
    try:
        assert manager.check_for_updates()
        assert manager.wait_for_background_task(1)
        assert manager.start_download()
        assert manager.wait_for_background_task(1)

        snapshot = manager.snapshot()
        assert snapshot.state is UpdateState.READY
        assert snapshot.payload_path == str(storage_adapter.destination)
        assert storage_adapter.destination.read_bytes() == b"payload"
        assert len(storage_adapter.persisted_payloads) == 1
        temporary_payload_path, download_url = storage_adapter.persisted_payloads[0]
        assert Path(temporary_payload_path).name == "update_payload.bin"
        assert download_url == _available_outcome().artifact.download_url
        assert platform_adapter.persisted_payloads == []
        assert not list(tmp_path.glob("caveviewer_update_*"))
    finally:
        manager.shutdown()


def test_storage_adapter_failure_is_an_ordinary_update_workflow_failure(tmp_path):
    platform_adapter = FakeRuntimePlatformAdapter(tmp_path / "Downloads")
    storage_adapter = FakeUpdatePackageStorageAdapter(
        tmp_path / "Stored Packages" / "CaveViewer-1.0.64.zip",
        error=OSError("downloads directory is unavailable"),
    )
    runtime = create_platform_runtime(
        platform_adapter=platform_adapter,
        desktop_services=FakeDesktopServices(),
        update_package_storage_adapter=storage_adapter,
        environment={},
    )

    def download_update(_url, _expected_size, destination, **_kwargs):
        Path(destination).write_bytes(b"payload")

    manager = UpdateManager(
        "1.0.63",
        platform_runtime=runtime,
        check_for_update=lambda *_args, **_kwargs: _available_outcome(),
        download_update=download_update,
        temp_root=str(tmp_path),
    )
    try:
        assert manager.check_for_updates()
        assert manager.wait_for_background_task(1)
        assert manager.start_download()
        assert manager.wait_for_background_task(1)

        snapshot = manager.snapshot()
        assert snapshot.state is UpdateState.FAILED
        assert snapshot.error == "downloads directory is unavailable"
        assert len(storage_adapter.persisted_payloads) == 1
        assert platform_adapter.persisted_payloads == []
        assert not list(tmp_path.glob("caveviewer_update_*"))
    finally:
        manager.shutdown()


def test_disabled_runtime_package_reveal_gate_blocks_native_action(tmp_path):
    payload_path = tmp_path / "CaveViewer.bin"
    payload_path.write_bytes(b"verified package")
    platform_adapter = FakeRuntimePlatformAdapter(tmp_path / "Downloads")
    runtime = create_platform_runtime(
        platform_adapter=platform_adapter,
        desktop_services=FakeDesktopServices(),
        environment={},
        platform_name="freebsd",
    )
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=runtime,
        temp_root=str(tmp_path),
    )
    try:
        with manager._lock:
            manager._state = UpdateState.READY
            manager._payload_path = str(payload_path)

        snapshot = manager.snapshot()
        assert snapshot.update_package_reveal is not None
        assert snapshot.update_package_reveal.state is FeatureState.DISABLED
        assert (
            snapshot.update_package_reveal.reason_code
            == "update_package_reveal_route_unsupported"
        )
        assert not manager.reveal_download()
        assert platform_adapter.revealed_paths == []
    finally:
        manager.shutdown()


def test_disabled_runtime_gate_starts_no_update_workers_or_downloads(tmp_path):
    adapter = FakeRuntimePlatformAdapter(tmp_path / "Downloads")
    update_profile = replace(
        select_update_profile(platform_name="linux", machine="x86_64"),
        supports_automatic_update=False,
    )
    runtime = create_platform_runtime(
        platform_adapter=adapter,
        desktop_services=FakeDesktopServices(),
        update_profile=update_profile,
        environment={},
    )
    checks = []
    downloads = []
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=runtime,
        check_for_update=lambda *_args, **_kwargs: checks.append(True),
        download_update=lambda *_args, **_kwargs: downloads.append(True),
        temp_root=str(tmp_path),
    )
    try:
        assert not manager.check_for_updates()
        assert checks == []
        assert manager.snapshot().state is UpdateState.IDLE
        assert (
            manager.snapshot().automatic_update.reason_code
            == "automatic_update_target_unsupported"
        )

        with manager._lock:
            manager._state = UpdateState.AVAILABLE
            manager._available_update = _available_outcome()
        assert not manager.start_download()
        assert downloads == []
    finally:
        manager.shutdown()


def test_runtime_target_is_passed_to_the_default_update_client(
    monkeypatch, tmp_path
):
    adapter = FakeRuntimePlatformAdapter(tmp_path / "Downloads")
    runtime = create_platform_runtime(
        platform_adapter=adapter,
        desktop_services=FakeDesktopServices(),
        environment={"CAVEVIEWER_UPDATE_BRANCH": "release-candidate"},
    )
    calls = []

    def check_for_update(current_version, **kwargs):
        calls.append((current_version, kwargs))
        return UpdateNotAvailable(
            current_version=current_version,
            latest_version=current_version,
        )

    monkeypatch.setattr(
        update_manager.update_checker,
        "check_for_update_target",
        check_for_update,
    )
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=runtime,
        temp_root=str(tmp_path),
    )
    try:
        assert manager.check_for_updates()
        assert manager.wait_for_background_task(1)
    finally:
        manager.shutdown()

    assert calls == [
        (
            "1.0.63",
            {
                "update_target": runtime.automatic_update_target,
                "tls_trust_adapter": runtime.tls_trust_adapter,
            },
        )
    ]


def test_runtime_tls_adapter_is_passed_to_default_update_download(
    monkeypatch, tmp_path
):
    adapter = FakeRuntimePlatformAdapter(tmp_path / "Downloads")

    class FakeTlsTrustAdapter:
        def augment_ssl_context(self, _context):
            raise AssertionError("the fake download must not create an SSL context")

    tls_trust_adapter = FakeTlsTrustAdapter()
    runtime = create_platform_runtime(
        platform_adapter=adapter,
        desktop_services=FakeDesktopServices(),
        tls_trust_adapter=tls_trust_adapter,
        environment={},
    )
    calls = []

    def download_update(_url, _expected_size, destination, **kwargs):
        calls.append(kwargs)
        Path(destination).write_bytes(b"payload")
        kwargs["phase_cb"]("verifying")

    monkeypatch.setattr(
        update_manager.update_checker,
        "download_update_target",
        download_update,
    )
    manager = UpdateManager(
        "1.0.63",
        platform_runtime=runtime,
        check_for_update=lambda *_args, **_kwargs: _available_outcome(),
        temp_root=str(tmp_path),
    )
    try:
        assert manager.check_for_updates()
        assert manager.wait_for_background_task(1)
        assert manager.start_download()
        assert manager.wait_for_background_task(1)
    finally:
        manager.shutdown()

    assert calls[0]["update_target"] is runtime.automatic_update_target
    assert calls[0]["tls_trust_adapter"] is tls_trust_adapter
