"""Exercise sample-map dialog focus, action-state, and cancellation workflows."""

from __future__ import annotations

import inspect
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


def test_download_start_reuses_action_area_as_cancel_button_without_prompt():
    action_button = object()
    configured_actions = []

    def set_action_button(button, text, command):
        configured_actions.append((button, text, command))

    cancel_event = sample_maps_dialog._activate_download_cancel_button(
        action_button, set_action_button
    )

    assert len(configured_actions) == 1
    configured_button, text, command = configured_actions[0]
    assert configured_button is action_button
    assert text == "Cancel"
    assert not cancel_event.is_set()

    command()

    assert cancel_event.is_set()


def test_save_directory_chooser_is_owned_focused_and_not_left_topmost():
    owner = FakeOwner(topmost=False)
    desktop_services = FakeDesktopServices(owner)

    result = sample_maps_dialog._ask_directory_in_front(
        desktop_services,
        owner,
        title="Save Test Cave to…",
        initial_dir="/maps",
    )

    assert result.path == "/chosen/folder"
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
            "CaveViewer is downloading a sample map",
            "Downloading Devils Eye…",
            "normal",
        ),
        ("inhibit", "Downloading Devils Eye", parent),
        ("close_inhibitor",),
        (
            "notify",
            notification_id,
            "CaveViewer sample map is ready",
            "Devils Eye finished downloading.",
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
        call[0] == "notify" and call[2] == "CaveViewer sample map is ready"
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
        "CaveViewer sample map download failed",
        "Couldn't download Devils Eye.",
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
    state_home = tmp_path / "state"
    sample_root = tmp_path / "downloads"
    sample_root.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    sample_maps_dialog._save_last_sample_maps_dir(str(sample_root))

    state_file = state_home / "caveviewer" / "last_sample_maps_dir"
    assert state_file.read_text(encoding="utf-8") == str(sample_root)
    assert sample_maps_dialog._load_last_sample_maps_dir() == str(sample_root)
