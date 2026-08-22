"""Verify focused package reveal and separate saved-file reveal behavior."""

from __future__ import annotations

import plistlib
from types import SimpleNamespace

import pytest

from caveviewer.gui.platform import saved_artifact_reveal, update_package_reveal


def test_windows_reveals_package_with_explorer_selection(tmp_path, monkeypatch):
    payload = tmp_path / "CaveViewer.zip"
    payload.write_bytes(b"package")
    launched = []
    monkeypatch.setattr(
        update_package_reveal.subprocess,
        "Popen",
        lambda command: launched.append(command),
    )

    adapter = update_package_reveal.WindowsUpdatePackageRevealAdapter()
    adapter.reveal_verified_package(str(payload))

    assert adapter.reveal_action_label() == "Show in Explorer"
    assert launched == [["explorer", "/select,", str(payload)]]


def test_windows_reveals_saved_file_with_explorer_selection(
    tmp_path,
    monkeypatch,
):
    payload = tmp_path / "CaveViewerDive.mp4"
    payload.write_bytes(b"video")
    launched = []
    monkeypatch.setattr(
        saved_artifact_reveal.subprocess,
        "Popen",
        lambda command: launched.append(command),
    )

    saved_artifact_reveal.WindowsSavedArtifactRevealAdapter().reveal_saved_artifact(
        str(payload)
    )

    assert launched == [["explorer", "/select,", str(payload)]]


def test_windows_reveal_keeps_explorer_selector_outside_a_whitespace_path(
    tmp_path,
    monkeypatch,
):
    payload = tmp_path / "Devils Eye" / "guided_dive_manual_trace.jsonl"
    payload.parent.mkdir()
    payload.write_text("trace", encoding="utf-8")
    launched = []
    monkeypatch.setattr(
        saved_artifact_reveal.subprocess,
        "Popen",
        lambda command: launched.append(command),
    )

    saved_artifact_reveal.WindowsSavedArtifactRevealAdapter().reveal_saved_artifact(
        str(payload)
    )

    assert launched == [["explorer", "/select,", str(payload)]]
    assert saved_artifact_reveal.subprocess.list2cmdline(launched[0]) == (
        f'explorer /select, "{payload}"'
    )


def test_linux_opens_download_directory_without_launching_package(tmp_path):
    payload = tmp_path / "CaveViewer.AppImage"
    payload.write_bytes(b"package")
    revealed = []

    class FakeDesktopServices:
        def reveal_path(self, path, *, parent=None):
            revealed.append((path, parent))

    adapter = update_package_reveal.LinuxUpdatePackageRevealAdapter(
        desktop_services=FakeDesktopServices()
    )
    adapter.reveal_verified_package(str(payload))

    assert adapter.reveal_action_label() == "Open Download Folder"
    assert revealed == [(str(payload), None)]


def test_linux_reveals_saved_file_with_desktop_services(tmp_path):
    payload = tmp_path / "CaveViewerDive.mp4"
    payload.write_bytes(b"video")
    revealed = []

    class FakeDesktopServices:
        def reveal_path(self, path, *, parent=None):
            revealed.append((path, parent))

    adapter = saved_artifact_reveal.LinuxSavedArtifactRevealAdapter(
        desktop_services=FakeDesktopServices()
    )
    adapter.reveal_saved_artifact(str(payload))

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

    monkeypatch.setattr(update_package_reveal.subprocess, "run", attach)
    monkeypatch.setattr(
        update_package_reveal.subprocess,
        "Popen",
        lambda command: finder_calls.append(command),
    )

    adapter = update_package_reveal.MacOSUpdatePackageRevealAdapter()
    adapter.reveal_verified_package(str(payload))
    adapter.reveal_verified_package(str(payload))

    assert adapter.reveal_action_label() == "Show in Finder"
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
        update_package_reveal.subprocess,
        "Popen",
        lambda command: finder_calls.append(command),
    )

    update_package_reveal.MacOSUpdatePackageRevealAdapter().reveal_verified_package(
        str(payload)
    )

    assert finder_calls == [["open", "-R", str(payload)]]


def test_macos_reveals_saved_file_without_mounting(tmp_path, monkeypatch):
    payload = tmp_path / "CaveViewerDive.mp4"
    payload.write_bytes(b"video")
    finder_calls = []
    monkeypatch.setattr(
        saved_artifact_reveal.subprocess,
        "Popen",
        lambda command: finder_calls.append(command),
    )

    saved_artifact_reveal.MacOSSavedArtifactRevealAdapter().reveal_saved_artifact(
        str(payload)
    )

    assert finder_calls == [["open", "-R", str(payload)]]


def test_unsupported_package_reveal_fails_safely_without_affecting_saved_files(
    tmp_path,
):
    payload = tmp_path / "CaveViewer.bin"
    payload.write_bytes(b"package")
    adapter = update_package_reveal.UnsupportedUpdatePackageRevealAdapter()

    assert adapter.reveal_route() is None
    with pytest.raises(RuntimeError, match="unsupported"):
        adapter.reveal_verified_package(str(payload))

    with pytest.raises(RuntimeError, match="unsupported"):
        saved_artifact_reveal.UnsupportedSavedArtifactRevealAdapter().reveal_saved_artifact(
            str(payload)
        )
