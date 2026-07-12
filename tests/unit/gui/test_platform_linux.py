"""Linux platform adapter update-channel behavior."""

from __future__ import annotations

from caveviewer.gui.platform import linux


class FakeDesktopServices:
    def reveal_path(self, path, *, parent=None):
        pass


def test_linux_update_manifest_uses_x86_64_channel(monkeypatch):
    monkeypatch.setattr(linux.platform, "machine", lambda: "x86_64")

    adapter = linux.LinuxSplashPlatformAdapter(
        desktop_services=FakeDesktopServices()
    )

    assert adapter.default_update_manifest_url("owner/repo", "main") == (
        "https://raw.githubusercontent.com/owner/repo/main/"
        "updates/linux/x86_64/stable.json"
    )
    assert adapter.supports_install_channel("linux_app")


def test_linux_arm64_updates_are_unsupported_without_arm_manifest(monkeypatch):
    monkeypatch.setattr(linux.platform, "machine", lambda: "aarch64")

    adapter = linux.LinuxSplashPlatformAdapter(
        desktop_services=FakeDesktopServices()
    )

    assert adapter.default_update_manifest_url("owner/repo", "main") == ""
    assert not adapter.supports_install_channel("linux_app")
    assert "only for x86_64" in adapter.unsupported_install_channel_message(
        "linux_app"
    )
    assert "aarch64" in adapter.unsupported_install_channel_message("linux_app")
