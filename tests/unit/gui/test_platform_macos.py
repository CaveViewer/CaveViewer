"""macOS platform presentation behavior."""

import pytest

from caveviewer.gui.platform.presentation import select_presentation_profile


def test_macos_tk_text_scale_follows_tk_default_without_extra_floor():
    profile = select_presentation_profile(platform_name="darwin")

    assert profile.tk_text_scale(12) == pytest.approx(1.0)
    assert profile.tk_text_scale(15) == pytest.approx(1.25)
    assert profile.tk_text_scale(18) == pytest.approx(1.5)


def test_macos_splash_policy_uses_compact_desktop_size():
    policy = select_presentation_profile(platform_name="darwin").splash_layout

    assert policy.window_width == 1080
    assert policy.min_height == 740
    assert policy.resize_min_width == 840
    assert policy.resize_min_height == 600
