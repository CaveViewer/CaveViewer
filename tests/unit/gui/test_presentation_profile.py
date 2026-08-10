"""Verify typed static GUI presentation profiles and native action facades."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from caveviewer.gui.platform.default import DefaultSplashPlatformAdapter
from caveviewer.gui.platform.presentation import (
    font_candidates_for_profile,
    select_presentation_profile,
)
from caveviewer.gui.platform.presentation_actions import (
    create_presentation_actions_adapter,
)
from caveviewer.gui.platform.runtime import create_platform_runtime


@pytest.mark.parametrize(
    (
        "platform_name",
        "profile_name",
        "font_family",
        "shortcut_label",
        "look_button",
        "overlay_scale",
        "uses_glfw_size",
    ),
    [
        ("unsupported", "unsupported", "Segoe UI", "Ctrl", "left", 1.0, False),
        ("darwin", "darwin", "Helvetica Neue", "Cmd", "right", 1.15, False),
        ("win32", "windows", "Segoe UI", "Ctrl", "left", 1.0, False),
        ("linux", "linux", "sans-serif", "Ctrl", "left", 1.0, True),
    ],
)
def test_presentation_profile_selects_static_platform_conventions(
    platform_name,
    profile_name,
    font_family,
    shortcut_label,
    look_button,
    overlay_scale,
    uses_glfw_size,
):
    profile = select_presentation_profile(platform_name=platform_name)

    assert profile.platform_name == profile_name
    assert profile.ui_font_family == font_family
    assert profile.primary_shortcut_modifier_label == shortcut_label
    assert profile.mouse_look_button_name == look_button
    assert profile.viewer_overlay_text_scale(2.0) == pytest.approx(
        2.0 * overlay_scale
    )
    assert profile.viewer_uses_glfw_native_initial_size is uses_glfw_size
    assert profile.font_candidates


def test_macos_profile_keeps_its_static_layout_and_input_fallbacks():
    profile = select_presentation_profile(platform_name="darwin")

    assert profile.splash_layout.reuse_existing_root is True
    assert profile.splash_layout.destroy_root_on_close is False
    assert profile.preferences_dialog_layout.macos_layout is True
    assert profile.dialog_layout.use_label_action_buttons is True
    assert profile.bookmark_save_modifier == "command"
    assert profile.tk_primary_modifier_name == "Command"
    assert profile.command_modifier_uses_control_fallback is True
    assert profile.shift_digit_bookmark_save_fallback is True
    assert profile.option_left_mouse_look_enabled is True
    assert profile.tk_text_scale(15.0) == pytest.approx(1.4)
    assert profile.suppress_forced_startup_focus(
        is_frozen=True,
        force_requested=False,
    ) is True


def test_linux_profile_leaves_semantic_font_density_to_tk_dpi_scaling():
    profile = select_presentation_profile(platform_name="linux")

    assert profile.supports_tk_display_scaling is True
    assert profile.uses_tk_default_font_scale is False
    assert profile.tk_text_scale(18.0) == pytest.approx(1.0)


def test_presentation_profile_is_immutable():
    profile = select_presentation_profile(platform_name="linux")

    with pytest.raises(FrozenInstanceError):
        profile.ui_font_family = "Different"  # type: ignore[misc]


def test_fontconfig_is_an_action_time_fallback(monkeypatch):
    profile = select_presentation_profile(platform_name="linux")
    monkeypatch.setattr(
        "caveviewer.gui.platform.presentation._fontconfig_sans_font",
        lambda: "/fonts/ChosenSans.ttf",
    )

    assert font_candidates_for_profile(profile) == (
        *profile.font_candidates,
        "/fonts/ChosenSans.ttf",
    )


def test_presentation_actions_facade_delegates_only_native_actions():
    calls: list[tuple[object, ...]] = []

    class FakePlatformAdapter:
        def configure_process_dpi_awareness(self):
            calls.append(("dpi",))

        def install_about_handler(self, root, program_name, version):
            calls.append(("about", root, program_name, version))

        def focus_viewer_window(self, window):
            calls.append(("focus", window))

    adapter = create_presentation_actions_adapter(FakePlatformAdapter())
    root = object()
    window = object()

    adapter.configure_process_dpi_awareness()
    adapter.install_about_handler(root, "CaveViewer", "1.2.3")
    adapter.focus_viewer_window(window)

    assert calls == [
        ("dpi",),
        ("about", root, "CaveViewer", "1.2.3"),
        ("focus", window),
    ]


def test_runtime_composes_injected_presentation_values():
    profile = select_presentation_profile(platform_name="darwin")
    actions = object()
    runtime = create_platform_runtime(
        platform_adapter=DefaultSplashPlatformAdapter(),
        desktop_services=object(),
        environment={},
        platform_name="darwin",
        presentation_profile=profile,
        presentation_actions_adapter=actions,
    )

    assert runtime.presentation_profile is profile
    assert runtime.presentation_actions_adapter is actions
