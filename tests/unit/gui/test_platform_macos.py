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


def test_macos_tk_text_scale_has_readability_floor():
    adapter = macos.MacOSSplashPlatformAdapter()

    assert adapter.tk_text_scale(12) == pytest.approx(1.4)
    assert adapter.tk_text_scale(18) == pytest.approx(1.5)


def test_macos_splash_policy_uses_desktop_readability_size():
    policy = macos.MacOSSplashPlatformAdapter().splash_layout_policy()

    assert policy.window_width == 1100
    assert policy.min_height == 680
