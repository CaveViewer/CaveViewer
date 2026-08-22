"""Test direct platform adapters for revealing saved artifacts."""

from __future__ import annotations

import subprocess

import pytest

from caveviewer.gui.platform import saved_artifact_reveal


def test_windows_reveal_selects_native_path_in_explorer(tmp_path, monkeypatch):
    output_path = tmp_path / "Devils Eye" / "cave.mp4"
    output_path.parent.mkdir()
    output_path.write_bytes(b"video")
    launched = []
    monkeypatch.setattr(
        saved_artifact_reveal.subprocess,
        "Popen",
        lambda command: launched.append(command),
    )

    adapter = saved_artifact_reveal.create_saved_artifact_reveal_adapter(
        platform_name="win32"
    )
    adapter.reveal_saved_artifact(str(output_path))

    assert launched == [["explorer", "/select,", str(output_path)]]
    assert subprocess.list2cmdline(launched[0]) == (
        f'explorer /select, "{output_path}"'
    )


def test_macos_reveal_selects_native_path_in_finder(tmp_path, monkeypatch):
    output_path = tmp_path / "cave.mp4"
    output_path.write_bytes(b"video")
    launched = []
    monkeypatch.setattr(
        saved_artifact_reveal.subprocess,
        "Popen",
        lambda command: launched.append(command),
    )

    adapter = saved_artifact_reveal.create_saved_artifact_reveal_adapter(
        platform_name="darwin"
    )
    adapter.reveal_saved_artifact(str(output_path))

    assert launched == [["open", "-R", str(output_path)]]


def test_linux_reveal_uses_injected_desktop_service(tmp_path):
    output_path = tmp_path / "cave.mp4"
    revealed = []

    class FakeDesktopServices:
        def reveal_path(self, path, *, parent=None):
            revealed.append((path, parent))

    adapter = saved_artifact_reveal.create_saved_artifact_reveal_adapter(
        platform_name="linux",
        desktop_services=FakeDesktopServices(),
    )
    adapter.reveal_saved_artifact(str(output_path))

    assert revealed == [(str(output_path), None)]


def test_unsupported_reveal_fails_without_opening_artifact(tmp_path):
    output_path = tmp_path / "cave.mp4"
    adapter = saved_artifact_reveal.create_saved_artifact_reveal_adapter(
        platform_name="unsupported"
    )

    with pytest.raises(RuntimeError, match="unsupported"):
        adapter.reveal_saved_artifact(str(output_path))
