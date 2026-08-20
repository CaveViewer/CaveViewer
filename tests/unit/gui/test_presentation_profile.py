"""Verify typed static GUI presentation profiles and native action facades."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from caveviewer.gui.platform import presentation_actions
from caveviewer.gui.platform.default import DefaultSplashPlatformAdapter
from caveviewer.gui.platform.presentation import (
    font_candidates_for_profile,
    select_presentation_profile,
)
from caveviewer.gui.platform.presentation_actions import (
    DefaultPresentationActionsAdapter,
    MacOSPresentationActionsAdapter,
    WindowsPresentationActionsAdapter,
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


@pytest.mark.parametrize(
    ("platform_name", "adapter_type"),
    [
        ("darwin", MacOSPresentationActionsAdapter),
        ("win32", WindowsPresentationActionsAdapter),
        ("linux", DefaultPresentationActionsAdapter),
        ("freebsd", DefaultPresentationActionsAdapter),
    ],
)
def test_presentation_actions_select_direct_native_implementation(
    platform_name,
    adapter_type,
):
    assert isinstance(
        create_presentation_actions_adapter(platform_name=platform_name),
        adapter_type,
    )


def test_default_presentation_actions_keep_best_effort_focus_order():
    calls: list[str] = []

    class Target:
        def __init__(self, name: str) -> None:
            self._name = name

        def switch_to(self) -> None:
            calls.append(f"{self._name}.switch")

        def activate(self) -> None:
            calls.append(f"{self._name}.activate")

    class Window(Target):
        def __init__(self) -> None:
            super().__init__("window")
            self._window = Target("native")

    DefaultPresentationActionsAdapter().focus_viewer_window(Window())

    assert calls == [
        "window.switch",
        "window.activate",
        "native.switch",
        "native.activate",
    ]


def test_macos_presentation_actions_keep_native_focus_preference():
    calls: list[str] = []

    class NativeWindow:
        def activate(self) -> None:
            calls.append("native.activate")

    class Window:
        _window = NativeWindow()

        def activate(self) -> None:
            calls.append("window.activate")

    MacOSPresentationActionsAdapter().focus_viewer_window(Window())

    assert calls == ["native.activate"]


def test_macos_presentation_actions_register_tcl_only_about_handler():
    class FakeRoot:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []
            self.scripts: list[str] = []

        def call(self, *args: str) -> None:
            self.calls.append(args)

        def eval(self, script: str) -> None:
            self.scripts.append(script)

    root = FakeRoot()

    MacOSPresentationActionsAdapter().install_about_handler(root, "CaveViewer", "1.2.3")

    assert root.calls == [
        ("set", "_cv_about_title", "About CaveViewer"),
        ("set", "_cv_about_msg", "CaveViewer\nVersion 1.2.3"),
        (
            "set",
            "_cv_about_detail",
            "CaveViewer created by Brian Deatherage & Zsolt Zsabo of\n"
            "BottomLine Projects Scientific Dive Team and other volunteers.\n\n"
            "Licensed under the GNU General Public License v3.0.",
        ),
    ]
    assert "proc ::tk::mac::ShowAbout" in root.scripts[0]
    assert "proc tkAboutDialog {} { ::tk::mac::ShowAbout }" in root.scripts[0]
    assert presentation_actions._macos_about_root_ref is root


def test_windows_presentation_actions_prefer_per_monitor_v2_dpi(monkeypatch):
    calls: list[tuple[str, int | None]] = []
    fake_ctypes = SimpleNamespace(
        c_void_p=lambda value: value,
        windll=SimpleNamespace(
            user32=SimpleNamespace(
                SetProcessDpiAwarenessContext=lambda value: calls.append(("v2", value))
                or 1,
                SetProcessDPIAware=lambda: calls.append(("vista", None)),
            ),
            shcore=SimpleNamespace(
                SetProcessDpiAwareness=lambda value: calls.append(("v81", value))
            ),
        ),
    )
    monkeypatch.setattr(presentation_actions, "ctypes", fake_ctypes)

    WindowsPresentationActionsAdapter().configure_process_dpi_awareness()

    assert calls == [("v2", -4)]


def test_windows_presentation_actions_keep_dpi_fallbacks(monkeypatch):
    calls: list[tuple[str, int | None]] = []

    def unavailable_v2(value: int) -> int:
        calls.append(("v2", value))
        return 0

    def unavailable_v81(value: int) -> None:
        calls.append(("v81", value))
        raise OSError("not available")

    fake_ctypes = SimpleNamespace(
        c_void_p=lambda value: value,
        windll=SimpleNamespace(
            user32=SimpleNamespace(
                SetProcessDpiAwarenessContext=unavailable_v2,
                SetProcessDPIAware=lambda: calls.append(("vista", None)),
            ),
            shcore=SimpleNamespace(SetProcessDpiAwareness=unavailable_v81),
        ),
    )
    monkeypatch.setattr(presentation_actions, "ctypes", fake_ctypes)

    WindowsPresentationActionsAdapter().configure_process_dpi_awareness()

    assert calls == [("v2", -4), ("v81", 2), ("vista", None)]


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


def test_runtime_composes_direct_presentation_actions_without_broad_adapter_methods():
    runtime = create_platform_runtime(
        platform_adapter=object(),
        desktop_services=object(),
        environment={},
        platform_name="darwin",
    )

    assert isinstance(
        runtime.presentation_actions_adapter,
        MacOSPresentationActionsAdapter,
    )
