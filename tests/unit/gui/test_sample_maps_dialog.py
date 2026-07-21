"""Exercise sample-map dialog focus, action-state, and cancellation workflows."""

from __future__ import annotations

import inspect
import queue
import sys
import threading
from types import SimpleNamespace

import pytest

from caveviewer.gui import sample_maps_dialog
from caveviewer.gui.platform import DirectorySelection


class FakeOwner:
    def __init__(self, topmost=False):
        self.topmost = topmost
        self.exists = True
        self.calls = []

    def attributes(self, name, *value):
        assert name == "-topmost"
        if not value:
            self.calls.append(("get_topmost", self.topmost))
            return self.topmost
        self.topmost = value[0]
        self.calls.append(("set_topmost", self.topmost))

    def lift(self):
        self.calls.append(("lift",))

    def focus_force(self):
        self.calls.append(("focus_force",))

    def update_idletasks(self):
        self.calls.append(("update_idletasks",))

    def winfo_exists(self):
        return self.exists


class FakeDesktopServices:
    def __init__(self, owner, result="/chosen/folder"):
        self.owner = owner
        self.result = result
        self.options = None

    def choose_directory(self, **options):
        assert self.owner.topmost is False
        self.options = options
        return DirectorySelection.from_path(self.result) if self.result else None


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


def test_sample_map_row_copy_uses_folder_and_download_actions():
    downloadable = SimpleNamespace(
        display_name="Devils Eye",
        download_url="https://example.test/devils-eye.zip",
        size_bytes=52 * 1024 * 1024,
    )
    unavailable = SimpleNamespace(
        display_name="Devils Eye",
        download_url=None,
        size_bytes=None,
    )

    assert sample_maps_dialog._sample_detail_text(
        downloadable, downloaded=False
    ) == "52 MB"
    assert sample_maps_dialog._sample_action_text(
        downloadable, downloaded=False
    ) == "Download…"
    assert sample_maps_dialog._sample_action_enabled(
        downloadable, downloaded=False
    )

    assert sample_maps_dialog._sample_detail_text(
        downloadable, downloaded=True
    ) == "Ready to open"
    assert sample_maps_dialog._sample_action_text(
        downloadable, downloaded=True
    ) == "Open Map"

    assert sample_maps_dialog._sample_detail_text(
        unavailable, downloaded=False
    ) == "Download unavailable"
    assert sample_maps_dialog._sample_action_text(
        unavailable, downloaded=False
    ) == "Unavailable"
    assert not sample_maps_dialog._sample_action_enabled(
        unavailable, downloaded=False
    )


def test_sample_catalog_notice_keeps_download_failure_non_blocking():
    notice = sample_maps_dialog._sample_catalog_notice_text("offline")

    assert "already downloaded" in notice
    assert "new downloads need the internet" in notice


def test_sample_maps_dialog_keeps_curated_list_out_of_scroll_container():
    source = inspect.getsource(sample_maps_dialog.show_sample_maps_dialog)

    assert "Scrollbar(" not in source
    assert "yscrollcommand" not in source


def test_sample_maps_dialog_catalog_load_uses_after_polling():
    source = inspect.getsource(sample_maps_dialog.show_sample_maps_dialog)

    assert "queue.Queue" in source
    assert "dialog.after(0, _poll_catalog_fetch)" in source
    assert "dialog.update()" not in source
    assert ".update()" not in source
    assert "time.sleep(" not in source


def test_sample_maps_dialog_download_uses_worker_and_after_polling():
    source = inspect.getsource(sample_maps_dialog.show_sample_maps_dialog)

    assert "_start_sample_download_worker(" in source
    assert "_poll_download_queue" in source
    assert "_cancel_active_download_for_close" in source
    assert "cancel_event.set()" in source
    assert "\"Cancel\"" in source
    assert "\"Cancelling…\"" in source
    assert "progress_bar_canvas.update()" not in source


def test_sample_maps_dialog_is_modal_and_has_initial_focus_policy():
    source = inspect.getsource(sample_maps_dialog.show_sample_maps_dialog)

    assert "dialog.withdraw()" in source
    assert "dialog.deiconify()" in source
    assert "dialog.grab_set()" in source
    assert "dialog.wait_visibility()" in source
    assert "focus_set()" in source
    assert "create_dialog_notice(" in source
    assert "set_dialog_notice(" in source
    assert "create_dialog_action_button(" in source
    assert "set_dialog_action_button(" in source


def test_sample_download_worker_queues_progress_and_success(monkeypatch, tmp_path):
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
        sample_maps_dialog,
        "_download_and_extract_to_selected_directory",
        fake_download,
    )

    save_dir = DirectorySelection.from_path(str(tmp_path))
    sample_maps_dialog._run_sample_download_worker(
        save_dir,
        sample,
        cancel_event,
        result_queue,
    )

    assert calls == [
        (save_dir, sample, {"progress_cb", "cancel_cb"}),
    ]
    assert result_queue.get_nowait() == sample_maps_dialog._SampleDownloadProgress(
        5, 10
    )
    assert result_queue.get_nowait() == sample_maps_dialog._SampleDownloadSucceeded(
        "/downloaded/devils-eye"
    )
    with pytest.raises(queue.Empty):
        result_queue.get_nowait()


def test_sample_download_worker_queues_failure(monkeypatch, tmp_path):
    sample = _sample()
    result_queue = queue.Queue()
    cancel_event = threading.Event()

    def fake_download(*_args, **_options):
        raise RuntimeError("network failed")

    monkeypatch.setattr(
        sample_maps_dialog,
        "_download_and_extract_to_selected_directory",
        fake_download,
    )

    sample_maps_dialog._run_sample_download_worker(
        DirectorySelection.from_path(str(tmp_path)),
        sample,
        cancel_event,
        result_queue,
    )

    message = result_queue.get_nowait()
    assert isinstance(message, sample_maps_dialog._SampleDownloadFailed)
    assert str(message.error) == "network failed"
    with pytest.raises(queue.Empty):
        result_queue.get_nowait()


def test_sample_download_worker_progress_observes_cancel(monkeypatch, tmp_path):
    from caveviewer.gui.sample_maps import DownloadCancelled

    sample = _sample()
    result_queue = queue.Queue()
    cancel_event = threading.Event()

    def fake_download(_save_dir, _sample, **options):
        cancel_event.set()
        options["progress_cb"](5, 10)

    monkeypatch.setattr(
        sample_maps_dialog,
        "_download_and_extract_to_selected_directory",
        fake_download,
    )

    sample_maps_dialog._run_sample_download_worker(
        DirectorySelection.from_path(str(tmp_path)),
        sample,
        cancel_event,
        result_queue,
    )

    message = result_queue.get_nowait()
    assert isinstance(message, sample_maps_dialog._SampleDownloadFailed)
    assert isinstance(message.error, DownloadCancelled)
    with pytest.raises(queue.Empty):
        result_queue.get_nowait()


def test_start_sample_download_worker_uses_owned_non_daemon_thread(
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
        sample_maps_dialog,
        "_run_sample_download_worker",
        fake_run,
    )

    worker = sample_maps_dialog._start_sample_download_worker(
        save_dir,
        sample,
        cancel_event,
        result_queue,
    )
    worker.join(timeout=1.0)

    assert worker.name == "CaveViewer-sample-map-download"
    assert worker.daemon is False
    assert done.is_set()
    assert calls == [(save_dir, sample, cancel_event, result_queue)]


def test_save_directory_chooser_is_owned_focused_and_not_left_topmost():
    owner = FakeOwner(topmost=False)
    desktop_services = FakeDesktopServices(owner)

    result = sample_maps_dialog._ask_directory_in_front(
        desktop_services,
        owner,
        title="Save Test Cave to…",
        initial_dir="/maps",
    )

    assert result == DirectorySelection.from_path("/chosen/folder")
    assert desktop_services.options == {
        "title": "Save Test Cave to…",
        "initial_dir": "/maps",
        "parent": owner,
    }
    assert owner.topmost is False
    assert owner.calls == [
        ("get_topmost", False),
        ("set_topmost", True),
        ("lift",),
        ("focus_force",),
        ("update_idletasks",),
        ("set_topmost", False),
        ("update_idletasks",),
        ("set_topmost", False),
        ("lift",),
        ("focus_force",),
    ]


def test_save_directory_chooser_restores_topmost_state_after_failure():
    owner = FakeOwner(topmost=True)

    class FailingDesktopServices:
        def choose_directory(self, **_options):
            assert owner.topmost is False
            raise RuntimeError("native chooser failed")

    try:
        sample_maps_dialog._ask_directory_in_front(
            FailingDesktopServices(),
            owner,
            title="Save Test Cave to…",
            initial_dir="/maps",
        )
    except RuntimeError as error:
        assert str(error) == "native chooser failed"
    else:
        raise AssertionError("chooser failure should propagate")

    assert owner.topmost is True


def test_download_uses_selected_directory_path(monkeypatch, tmp_path):
    from caveviewer.gui import sample_maps

    sample = object()
    progress_cb = object()
    cancel_cb = object()
    calls = []

    def fake_download(install_dir, sample_arg, **options):
        calls.append((install_dir, sample_arg, options))
        return "/downloaded/sample"

    monkeypatch.setattr(
        sample_maps, "download_and_extract_sample_map", fake_download
    )

    result = sample_maps_dialog._download_and_extract_to_selected_directory(
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


def test_sample_download_uses_desktop_notification_and_inhibit(
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
        sample_maps_dialog,
        "_download_and_extract_to_selected_directory",
        fake_download,
    )

    result = sample_maps_dialog._download_sample_with_desktop_activity(
        services,
        parent,
        DirectorySelection.from_path(str(tmp_path)),
        sample,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
    )

    notification_id = sample_maps_dialog._sample_download_notification_id(sample)
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
            "Sample Map Download Started",
            "Downloading Devils Eye",
            "normal",
        ),
        ("inhibit", "Downloading Devils Eye", parent),
        ("close_inhibitor",),
        (
            "notify",
            notification_id,
            "Sample Map Ready",
            "Devils Eye finished downloading",
            "normal",
        ),
    ]


def test_sample_download_can_use_foreground_dialog_without_desktop_notifications(
    monkeypatch, tmp_path
):
    sample = _sample()
    parent = object()
    services = FakeActivityDesktopServices()

    def fake_download(*_args, **_options):
        return "/downloaded/devils-eye"

    monkeypatch.setattr(
        sample_maps_dialog,
        "_download_and_extract_to_selected_directory",
        fake_download,
    )

    result = sample_maps_dialog._download_sample_with_desktop_activity(
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


def test_sample_download_withdraws_notification_on_cancel(
    monkeypatch, tmp_path
):
    from caveviewer.gui.sample_maps import DownloadCancelled

    sample = _sample()
    services = FakeActivityDesktopServices()

    def fake_download(*_args, **_options):
        raise DownloadCancelled("cancelled")

    monkeypatch.setattr(
        sample_maps_dialog,
        "_download_and_extract_to_selected_directory",
        fake_download,
    )

    with pytest.raises(DownloadCancelled):
        sample_maps_dialog._download_sample_with_desktop_activity(
            services,
            object(),
            DirectorySelection.from_path(str(tmp_path)),
            sample,
        )

    notification_id = sample_maps_dialog._sample_download_notification_id(sample)
    assert ("close_inhibitor",) in services.calls
    assert ("withdraw_notification", notification_id) in services.calls
    assert not any(
        call[0] == "notify" and call[2] == "Sample Map Ready"
        for call in services.calls
    )


def test_sample_download_reports_failure_to_desktop(
    monkeypatch, tmp_path
):
    sample = _sample()
    services = FakeActivityDesktopServices()

    def fake_download(*_args, **_options):
        raise RuntimeError("network failed")

    monkeypatch.setattr(
        sample_maps_dialog,
        "_download_and_extract_to_selected_directory",
        fake_download,
    )

    with pytest.raises(RuntimeError, match="network failed"):
        sample_maps_dialog._download_sample_with_desktop_activity(
            services,
            object(),
            DirectorySelection.from_path(str(tmp_path)),
            sample,
        )

    notification_id = sample_maps_dialog._sample_download_notification_id(sample)
    assert ("close_inhibitor",) in services.calls
    assert (
        "notify",
        notification_id,
        "Sample Map Download Failed",
        "Couldn’t download Devils Eye",
        "high",
    ) in services.calls


def test_sample_download_continues_when_desktop_activity_is_unavailable(
    monkeypatch, tmp_path
):
    sample = _sample()
    services = FakeActivityDesktopServices(fail_notify=True, fail_inhibit=True)
    download_calls = []

    def fake_download(save_dir, sample_arg, **options):
        download_calls.append((save_dir, sample_arg, options))
        return "/downloaded/devils-eye"

    monkeypatch.setattr(
        sample_maps_dialog,
        "_download_and_extract_to_selected_directory",
        fake_download,
    )

    result = sample_maps_dialog._download_sample_with_desktop_activity(
        services,
        object(),
        DirectorySelection.from_path(str(tmp_path)),
        sample,
    )

    assert result == "/downloaded/devils-eye"
    assert len(download_calls) == 1


def test_last_sample_maps_directory_uses_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    state_home = tmp_path / "state"
    sample_root = tmp_path / "downloads"
    sample_root.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    sample_maps_dialog._save_last_sample_maps_dir(str(sample_root))

    state_file = state_home / "caveviewer" / "last_sample_maps_dir"
    assert state_file.read_text(encoding="utf-8") == str(sample_root)
    assert sample_maps_dialog._load_last_sample_maps_dir() == str(sample_root)
