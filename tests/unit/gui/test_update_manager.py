"""Exercise the process-owned update state machine and worker lifecycle."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from caveviewer.gui.update_checker import DownloadCancelled, UpdateCheckResult
from caveviewer.gui import update_manager
from caveviewer.gui.update_manager import UpdateManager, UpdateState


class FakePlatformAdapter:
    def __init__(self, downloads_dir: Path):
        self.downloads_dir = downloads_dir
        self.revealed_paths = []

    def install_channel(self):
        return "test_app"

    def persist_downloaded_payload(self, temp_payload_path, download_url):
        self.downloads_dir.mkdir(exist_ok=True)
        destination = self.downloads_dir / Path(download_url).name
        Path(temp_payload_path).replace(destination)
        return str(destination)

    def download_reveal_action_label(self):
        return "Show Test Package"

    def reveal_downloaded_payload(self, payload_path):
        self.revealed_paths.append(payload_path)


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


def _available_result():
    return UpdateCheckResult(
        update_available=True,
        current_version="1.0.63",
        latest_version="1.0.64",
        download_url="https://example.invalid/CaveViewer-1.0.64.zip",
        download_size_bytes=7,
        download_sha256="expected",
        package_kind="zip",
    )


def _checked_manager(tmp_path, download_update, *, desktop_services=None):
    adapter = FakePlatformAdapter(tmp_path / "Downloads")
    manager = UpdateManager(
        "1.0.63",
        platform_adapter=adapter,
        check_for_update=lambda *_args, **_kwargs: _available_result(),
        download_update=download_update,
        desktop_services=desktop_services or FakeDesktopServices(),
        temp_root=str(tmp_path),
    )
    assert manager.check_for_updates()
    assert manager.wait_for_background_task(1)
    assert manager.snapshot().state == UpdateState.AVAILABLE
    return manager, adapter


def test_check_transitions_through_checking_to_available(tmp_path):
    check_started = threading.Event()
    release_check = threading.Event()

    def check_for_update(*_args, **_kwargs):
        check_started.set()
        assert release_check.wait(1)
        return _available_result()

    manager = UpdateManager(
        "1.0.63",
        platform_adapter=FakePlatformAdapter(tmp_path / "Downloads"),
        check_for_update=check_for_update,
        desktop_services=FakeDesktopServices(),
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
    finally:
        manager.shutdown()


def test_up_to_date_result_is_checked_only_once_per_process(tmp_path):
    checks = []

    def check_for_update(current_version, **_kwargs):
        checks.append(current_version)
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            latest_version=current_version,
        )

    manager = UpdateManager(
        "1.0.63",
        platform_adapter=FakePlatformAdapter(tmp_path / "Downloads"),
        check_for_update=check_for_update,
        desktop_services=FakeDesktopServices(),
        temp_root=str(tmp_path),
    )
    try:
        assert manager.check_for_updates()
        assert manager.wait_for_background_task(1)
        assert manager.snapshot().state == UpdateState.UP_TO_DATE

        assert not manager.check_for_updates()
        assert checks == ["1.0.63"]
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
    ):
        assert expected_sha256 == "expected"
        assert not cancel_cb()
        Path(destination).write_bytes(b"payload")
        progress_cb(4, 8)
        observed_states.append(manager.snapshot())
        phase_cb("verifying")
        observed_states.append(manager.snapshot())

    manager, adapter = _checked_manager(tmp_path, download_update)
    try:
        assert manager.reveal_action_label == "Show Test Package"
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
    manager = UpdateManager(
        "1.0.63",
        platform_adapter=FakePlatformAdapter(tmp_path / "Downloads"),
        desktop_services=FakeDesktopServices(),
        temp_root=str(tmp_path),
    )

    with pytest.raises(ValueError, match="shutdown timeout must be non-negative"):
        manager.shutdown(timeout=-0.1)


def test_non_actionable_states_reject_download_and_reveal(tmp_path):
    manager = UpdateManager(
        "1.0.63",
        platform_adapter=FakePlatformAdapter(tmp_path / "Downloads"),
        desktop_services=FakeDesktopServices(),
        temp_root=str(tmp_path),
    )
    try:
        assert not manager.start_download()
        assert not manager.reveal_download()
    finally:
        manager.shutdown()
