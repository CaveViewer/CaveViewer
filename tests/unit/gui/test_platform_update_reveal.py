"""Verify that platform adapters reveal packages without executing them."""

from __future__ import annotations

import plistlib
from types import SimpleNamespace

import pytest

from caveviewer.gui.platform import default, linux, macos, windows


def test_windows_reveals_package_with_explorer_selection(tmp_path, monkeypatch):
    payload = tmp_path / "CaveViewer.zip"
    payload.write_bytes(b"package")
    launched = []
    monkeypatch.setattr(
        windows.subprocess,
        "Popen",
        lambda command: launched.append(command),
    )

    adapter = windows.WindowsSplashPlatformAdapter()
    adapter.reveal_downloaded_payload(str(payload))

    assert adapter.download_reveal_action_label() == "Show in Explorer"
    assert launched == [["explorer", f"/select,{payload}"]]


def test_windows_reveals_saved_file_with_explorer_selection(
    tmp_path,
    monkeypatch,
):
    payload = tmp_path / "CaveViewerDive.mp4"
    payload.write_bytes(b"video")
    launched = []
    monkeypatch.setattr(
        windows.subprocess,
        "Popen",
        lambda command: launched.append(command),
    )

    windows.WindowsSplashPlatformAdapter().reveal_file(str(payload))

    assert launched == [["explorer", f"/select,{payload}"]]


def test_linux_opens_download_directory_without_launching_package(
    tmp_path, monkeypatch
):
    payload = tmp_path / "CaveViewer.AppImage"
    payload.write_bytes(b"package")
    revealed = []

    class FakeDesktopServices:
        def reveal_path(self, path, *, parent=None):
            revealed.append((path, parent))

    adapter = linux.LinuxSplashPlatformAdapter(
        desktop_services=FakeDesktopServices()
    )
    adapter.reveal_downloaded_payload(str(payload))

    assert adapter.download_reveal_action_label() == "Open Download Folder"
    assert revealed == [(str(payload), None)]


def test_linux_reveals_saved_file_with_desktop_services(tmp_path):
    payload = tmp_path / "CaveViewerDive.mp4"
    payload.write_bytes(b"video")
    revealed = []

    class FakeDesktopServices:
        def reveal_path(self, path, *, parent=None):
            revealed.append((path, parent))

    adapter = linux.LinuxSplashPlatformAdapter(
        desktop_services=FakeDesktopServices()
    )
    adapter.reveal_file(str(payload))

    assert revealed == [(str(payload), None)]


def test_macos_mounts_dmg_read_only_and_reuses_mount_for_finder(
    tmp_path, monkeypatch
):
    payload = tmp_path / "CaveViewer.dmg"
    payload.write_bytes(b"package")
    mountpoint = tmp_path / "mounted"
    app_path = mountpoint / "CaveViewer.app"
    app_path.mkdir(parents=True)
    attach_calls = []
    finder_calls = []

    def attach(command, **options):
        attach_calls.append((command, options))
        return SimpleNamespace(
            stdout=plistlib.dumps(
                {"system-entities": [{"mount-point": str(mountpoint)}]}
            )
        )

    monkeypatch.setattr(macos.subprocess, "run", attach)
    monkeypatch.setattr(
        macos.subprocess,
        "Popen",
        lambda command: finder_calls.append(command),
    )

    adapter = macos.MacOSSplashPlatformAdapter()
    adapter.reveal_downloaded_payload(str(payload))
    adapter.reveal_downloaded_payload(str(payload))

    assert adapter.download_reveal_action_label() == "Show in Finder"
    assert attach_calls == [
        (
            [
                "hdiutil",
                "attach",
                str(payload),
                "-nobrowse",
                "-readonly",
                "-plist",
            ],
            {"check": True, "capture_output": True},
        )
    ]
    assert finder_calls == [
        ["open", "-R", str(app_path)],
        ["open", "-R", str(app_path)],
    ]


def test_macos_reveals_non_dmg_package_without_executing_it(
    tmp_path, monkeypatch
):
    payload = tmp_path / "CaveViewer.pkg"
    payload.write_bytes(b"package")
    finder_calls = []
    monkeypatch.setattr(
        macos.subprocess,
        "Popen",
        lambda command: finder_calls.append(command),
    )

    macos.MacOSSplashPlatformAdapter().reveal_downloaded_payload(str(payload))

    assert finder_calls == [["open", "-R", str(payload)]]


def test_macos_reveals_saved_file_without_mounting(tmp_path, monkeypatch):
    payload = tmp_path / "CaveViewerDive.mp4"
    payload.write_bytes(b"video")
    finder_calls = []
    monkeypatch.setattr(
        macos.subprocess,
        "Popen",
        lambda command: finder_calls.append(command),
    )

    macos.MacOSSplashPlatformAdapter().reveal_file(str(payload))

    assert finder_calls == [["open", "-R", str(payload)]]


def test_default_adapter_fails_safely_on_unsupported_platform(tmp_path):
    payload = tmp_path / "CaveViewer.bin"
    payload.write_bytes(b"package")

    with pytest.raises(RuntimeError, match="unsupported"):
        default.DefaultSplashPlatformAdapter().reveal_downloaded_payload(
            str(payload)
        )

    with pytest.raises(RuntimeError, match="unsupported"):
        default.DefaultSplashPlatformAdapter().reveal_file(str(payload))
