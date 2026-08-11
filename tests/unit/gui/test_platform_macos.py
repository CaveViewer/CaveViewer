"""macOS platform presentation behavior."""

import pytest

from caveviewer.gui.platform import macos


def test_macos_tk_text_scale_has_readability_floor():
    adapter = macos.MacOSSplashPlatformAdapter()

    assert adapter.tk_text_scale(12) == pytest.approx(1.4)
    assert adapter.tk_text_scale(18) == pytest.approx(1.5)


def test_macos_splash_policy_uses_desktop_readability_size():
    policy = macos.MacOSSplashPlatformAdapter().splash_layout_policy()

    assert policy.window_width == 1100
    assert policy.min_height == 680
