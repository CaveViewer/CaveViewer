"""Architecture routing tests for the macOS platform adapter."""

import pytest

from caveviewer.gui.platform import macos


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("arm64", "arm64"),
        ("aarch64", "arm64"),
        ("x86_64", "x86_64"),
        ("AMD64", "x86_64"),
        ("x64", "x86_64"),
        ("powerpc", None),
    ],
)
def test_macos_process_architecture_normalizes_supported_names(reported, expected):
    assert macos._macos_process_architecture(reported) == expected


@pytest.mark.parametrize("architecture", ["arm64", "x86_64"])
def test_default_manifest_url_uses_process_architecture(monkeypatch, architecture):
    monkeypatch.setattr(macos.platform, "machine", lambda: architecture)
    adapter = macos.MacOSSplashPlatformAdapter()

    assert adapter.default_update_manifest_url("owner/repo", "main") == (
        "https://raw.githubusercontent.com/owner/repo/main/"
        f"updates/macos/{architecture}/stable.json"
    )


def test_unknown_macos_architecture_does_not_receive_arm64_update(monkeypatch):
    monkeypatch.setattr(macos.platform, "machine", lambda: "unknown")
    adapter = macos.MacOSSplashPlatformAdapter()

    assert adapter.default_update_manifest_url("owner/repo", "release/test") == (
        "https://raw.githubusercontent.com/owner/repo/release/test/"
        "updates/macos/unsupported/stable.json"
    )
