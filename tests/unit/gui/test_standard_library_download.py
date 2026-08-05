"""Exercise GUI-owned standard-library map download coordination."""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest

from caveviewer.core.capabilities import (
    DesktopNotificationRoute,
    DesktopNotificationTarget,
    IdleSuspendInhibitionRoute,
    IdleSuspendInhibitionTarget,
)
from caveviewer.gui import standard_library_download
from caveviewer.gui.platform import DirectorySelection


class FakeInhibitor:
    def __init__(self, calls):
        self._calls = calls

    def close(self):
        self._calls.append(("close_inhibitor",))


class FakeActivityDesktopServices:
    def __init__(self, *, fail_notify=False, fail_inhibit=False):
        self.calls = []
        self.fail_notify = fail_notify
        self.fail_inhibit = fail_inhibit

    def notify(self, notification_id, title, body, *, priority):
        if self.fail_notify:
            raise RuntimeError("notifications unavailable")
        self.calls.append(("notify", notification_id, title, body, priority))

    def withdraw_notification(self, notification_id):
        self.calls.append(("withdraw_notification", notification_id))

    def inhibit_idle_suspend(self, reason, *, parent=None):
        if self.fail_inhibit:
            raise RuntimeError("inhibit unavailable")
        self.calls.append(("inhibit", reason, parent))
        return FakeInhibitor(self.calls)


def _sample():
    return SimpleNamespace(
        display_name="Devils Eye",
        asset_name="Devils.Eye.3D.Map.zip",
    )


def test_standard_library_download_worker_queues_progress_and_success(
    monkeypatch, tmp_path
):
    sample = _sample()
    result_queue = queue.Queue()
    cancel_event = threading.Event()
    calls = []

    def fake_download(save_dir, sample_arg, **options):
        calls.append((save_dir, sample_arg, set(options)))
        assert not options["cancel_cb"]()
        options["progress_cb"](5, 10)
        return "/downloaded/devils-eye"

    monkeypatch.setattr(
        standard_library_download,
        "download_and_extract_to_selected_directory",
        fake_download,
    )

    save_dir = DirectorySelection.from_path(str(tmp_path))
    standard_library_download.run_standard_library_download_worker(
        save_dir,
        sample,
        cancel_event,
        result_queue,
    )

    assert calls == [
        (save_dir, sample, {"progress_cb", "cancel_cb"}),
    ]
    assert result_queue.get_nowait() == (
        standard_library_download.StandardLibraryDownloadProgress(5, 10)
    )
    assert result_queue.get_nowait() == (
        standard_library_download.StandardLibraryDownloadSucceeded(
            "/downloaded/devils-eye"
        )
    )
    with pytest.raises(queue.Empty):
        result_queue.get_nowait()


def test_standard_library_download_worker_queues_failure(monkeypatch, tmp_path):
    sample = _sample()
    result_queue = queue.Queue()
    cancel_event = threading.Event()

    def fake_download(*_args, **_options):
        raise RuntimeError("network failed")

    monkeypatch.setattr(
        standard_library_download,
        "download_and_extract_to_selected_directory",
        fake_download,
    )

    standard_library_download.run_standard_library_download_worker(
        DirectorySelection.from_path(str(tmp_path)),
        sample,
        cancel_event,
        result_queue,
    )

    message = result_queue.get_nowait()
    assert isinstance(
        message, standard_library_download.StandardLibraryDownloadFailed
    )
    assert str(message.error) == "network failed"
    with pytest.raises(queue.Empty):
        result_queue.get_nowait()


def test_standard_library_download_worker_progress_observes_cancel(
    monkeypatch, tmp_path
):
    from caveviewer.gui.standard_library_maps import DownloadCancelled

    sample = _sample()
    result_queue = queue.Queue()
    cancel_event = threading.Event()

    def fake_download(_save_dir, _sample, **options):
        cancel_event.set()
        options["progress_cb"](5, 10)

    monkeypatch.setattr(
        standard_library_download,
        "download_and_extract_to_selected_directory",
        fake_download,
    )

    standard_library_download.run_standard_library_download_worker(
        DirectorySelection.from_path(str(tmp_path)),
        sample,
        cancel_event,
        result_queue,
    )

    message = result_queue.get_nowait()
    assert isinstance(
        message, standard_library_download.StandardLibraryDownloadFailed
    )
    assert isinstance(message.error, DownloadCancelled)
    with pytest.raises(queue.Empty):
        result_queue.get_nowait()


def test_start_standard_library_download_worker_uses_owned_non_daemon_thread(
    monkeypatch, tmp_path
):
    sample = _sample()
    save_dir = DirectorySelection.from_path(str(tmp_path))
    result_queue = queue.Queue()
    cancel_event = threading.Event()
    done = threading.Event()
    calls = []

    def fake_run(save_dir_arg, sample_arg, cancel_event_arg, result_queue_arg):
        calls.append(
            (save_dir_arg, sample_arg, cancel_event_arg, result_queue_arg)
        )
        done.set()

    monkeypatch.setattr(
        standard_library_download,
        "run_standard_library_download_worker",
        fake_run,
    )

    worker = standard_library_download.start_standard_library_download_worker(
        save_dir,
        sample,
        cancel_event,
        result_queue,
    )
    worker.join(timeout=1.0)

    assert worker.name == "CaveViewer-map-library-download"
    assert worker.daemon is False
    assert done.is_set()
    assert calls == [(save_dir, sample, cancel_event, result_queue)]


def test_download_uses_selected_directory_path(monkeypatch, tmp_path):
    from caveviewer.gui import standard_library_maps

    sample = object()
    progress_cb = object()
    cancel_cb = object()
    calls = []

    def fake_download(install_dir, sample_arg, **options):
        calls.append((install_dir, sample_arg, options))
        return "/downloaded/sample"

    monkeypatch.setattr(
        standard_library_maps,
        "download_and_extract_standard_library_map",
        fake_download,
    )

    result = standard_library_download.download_and_extract_to_selected_directory(
        DirectorySelection.from_path(str(tmp_path)),
        sample,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
    )

    assert result == "/downloaded/sample"
    assert calls == [
        (
            str(tmp_path),
            sample,
            {"progress_cb": progress_cb, "cancel_cb": cancel_cb},
        )
    ]


def test_standard_library_download_uses_desktop_notification_and_inhibit(
    monkeypatch, tmp_path
):
    sample = _sample()
    parent = object()
    services = FakeActivityDesktopServices()
    progress_cb = object()
    cancel_cb = object()
    download_calls = []

    def fake_download(save_dir, sample_arg, **options):
        download_calls.append((save_dir, sample_arg, options))
        return "/downloaded/devils-eye"

    monkeypatch.setattr(
        standard_library_download,
        "download_and_extract_to_selected_directory",
        fake_download,
    )

    result = standard_library_download.download_standard_library_with_desktop_activity(
        services,
        parent,
        DirectorySelection.from_path(str(tmp_path)),
        sample,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
    )

    notification_id = (
        standard_library_download.standard_library_download_notification_id(
            sample
        )
    )
    assert result == "/downloaded/devils-eye"
    assert download_calls == [
        (
            DirectorySelection.from_path(str(tmp_path)),
            sample,
            {"progress_cb": progress_cb, "cancel_cb": cancel_cb},
        )
    ]
    assert services.calls == [
        (
            "notify",
            notification_id,
            "Map Library Download Started",
            "Downloading Devils Eye",
            "normal",
        ),
        ("inhibit", "Downloading Devils Eye", parent),
        ("close_inhibitor",),
        (
            "notify",
            notification_id,
            "Map Library Download Ready",
            "Devils Eye finished downloading",
            "normal",
        ),
    ]


def test_standard_library_download_can_use_foreground_dialog_without_desktop_notifications(
    monkeypatch, tmp_path
):
    sample = _sample()
    parent = object()
    services = FakeActivityDesktopServices()

    def fake_download(*_args, **_options):
        return "/downloaded/devils-eye"

    monkeypatch.setattr(
        standard_library_download,
        "download_and_extract_to_selected_directory",
        fake_download,
    )

    result = standard_library_download.download_standard_library_with_desktop_activity(
        services,
        parent,
        DirectorySelection.from_path(str(tmp_path)),
        sample,
        notify_desktop=False,
    )

    assert result == "/downloaded/devils-eye"
    assert services.calls == [
        ("inhibit", "Downloading Devils Eye", parent),
        ("close_inhibitor",),
    ]


def test_standard_library_download_withdraws_notification_on_cancel(
    monkeypatch, tmp_path
):
    from caveviewer.gui.standard_library_maps import DownloadCancelled

    sample = _sample()
    services = FakeActivityDesktopServices()

    def fake_download(*_args, **_options):
        raise DownloadCancelled("cancelled")

    monkeypatch.setattr(
        standard_library_download,
        "download_and_extract_to_selected_directory",
        fake_download,
    )

    with pytest.raises(DownloadCancelled):
        standard_library_download.download_standard_library_with_desktop_activity(
            services,
            object(),
            DirectorySelection.from_path(str(tmp_path)),
            sample,
        )

    notification_id = (
        standard_library_download.standard_library_download_notification_id(
            sample
        )
    )
    assert ("close_inhibitor",) in services.calls
    assert ("withdraw_notification", notification_id) in services.calls
    assert not any(
        call[0] == "notify" and call[2] == "Map Library Download Ready"
        for call in services.calls
    )


def test_standard_library_download_reports_failure_to_desktop(
    monkeypatch, tmp_path
):
    sample = _sample()
    services = FakeActivityDesktopServices()

    def fake_download(*_args, **_options):
        raise RuntimeError("network failed")

    monkeypatch.setattr(
        standard_library_download,
        "download_and_extract_to_selected_directory",
        fake_download,
    )

    with pytest.raises(RuntimeError, match="network failed"):
        standard_library_download.download_standard_library_with_desktop_activity(
            services,
            object(),
            DirectorySelection.from_path(str(tmp_path)),
            sample,
        )

    notification_id = (
        standard_library_download.standard_library_download_notification_id(
            sample
        )
    )
    assert ("close_inhibitor",) in services.calls
    assert (
        "notify",
        notification_id,
        "Map Library Download Failed",
        "Couldn’t download Devils Eye",
        "high",
    ) in services.calls


def test_standard_library_download_continues_when_desktop_activity_is_unavailable(
    monkeypatch, tmp_path
):
    sample = _sample()
    services = FakeActivityDesktopServices(fail_notify=True, fail_inhibit=True)
    download_calls = []

    def fake_download(save_dir, sample_arg, **options):
        download_calls.append((save_dir, sample_arg, options))
        return "/downloaded/devils-eye"

    monkeypatch.setattr(
        standard_library_download,
        "download_and_extract_to_selected_directory",
        fake_download,
    )

    result = standard_library_download.download_standard_library_with_desktop_activity(
        services,
        object(),
        DirectorySelection.from_path(str(tmp_path)),
        sample,
    )

    assert result == "/downloaded/devils-eye"
    assert len(download_calls) == 1


def test_standard_library_download_continues_when_notification_route_is_unavailable(
    monkeypatch, tmp_path
):
    class NoopNotificationDesktopServices(FakeActivityDesktopServices):
        def desktop_notification_target(self):
            return DesktopNotificationTarget(DesktopNotificationRoute.NOOP)

    sample = _sample()
    services = NoopNotificationDesktopServices()
    parent = object()

    def fake_download(*_args, **_options):
        return "/downloaded/devils-eye"

    monkeypatch.setattr(
        standard_library_download,
        "download_and_extract_to_selected_directory",
        fake_download,
    )

    result = standard_library_download.download_standard_library_with_desktop_activity(
        services,
        parent,
        DirectorySelection.from_path(str(tmp_path)),
        sample,
    )

    assert result == "/downloaded/devils-eye"
    assert services.calls == [
        ("inhibit", "Downloading Devils Eye", parent),
        ("close_inhibitor",),
    ]


def test_standard_library_download_continues_when_inhibition_route_is_unavailable(
    monkeypatch, tmp_path
):
    class NoopInhibitionDesktopServices(FakeActivityDesktopServices):
        def idle_suspend_inhibition_target(self):
            return IdleSuspendInhibitionTarget(IdleSuspendInhibitionRoute.NOOP)

    sample = _sample()
    services = NoopInhibitionDesktopServices()

    def fake_download(*_args, **_options):
        return "/downloaded/devils-eye"

    monkeypatch.setattr(
        standard_library_download,
        "download_and_extract_to_selected_directory",
        fake_download,
    )

    result = standard_library_download.download_standard_library_with_desktop_activity(
        services,
        object(),
        DirectorySelection.from_path(str(tmp_path)),
        sample,
    )

    assert result == "/downloaded/devils-eye"
    assert not any(call[0] == "inhibit" for call in services.calls)
    assert not any(call[0] == "close_inhibitor" for call in services.calls)
