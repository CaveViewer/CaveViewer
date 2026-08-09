"""Static GUI presentation conventions and action-time font resolution.

``PresentationProfile`` is immutable process metadata selected from the
platform name.  It owns fonts, layout, shortcut, input, and scaling
conventions, but never creates Tk widgets, probes displays, starts a process,
or activates a native window.  Native presentation actions remain separate
from this profile while compatibility adapters are retired incrementally.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, replace

from .base import (
    DialogLayoutPolicy,
    PreferencesDialogLayoutPolicy,
    SplashLayoutPolicy,
)


_DEFAULT_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)

_WINDOWS_FONT_CANDIDATES = (
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    "C:/Windows/Fonts/verdana.ttf",
    "C:/Windows/Fonts/consola.ttf",
)

_MACOS_FONT_CANDIDATES = (
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/SFNSDisplay.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
)

_LINUX_FONT_CANDIDATES = (
    "/usr/share/caveviewer/fonts/CaveViewerUI-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation2/LiberationSans-Regular.ttf",
)


@dataclass(frozen=True, slots=True)
class PresentationProfile:
    """Process-stable UI conventions for one selected platform.

    Methods on this value are pure transforms of their supplied arguments.
    The profile deliberately excludes About-menu registration, DPI setup, and
    native viewer activation because those are action-time effects.
    """

    platform_name: str
    ui_font_family: str
    font_candidates: tuple[str, ...]
    uses_fontconfig_fallback: bool
    splash_layout: SplashLayoutPolicy
    preferences_dialog_layout: PreferencesDialogLayoutPolicy
    dialog_layout: DialogLayoutPolicy
    bookmark_save_modifier: str
    primary_shortcut_modifier_label: str
    tk_primary_modifier_name: str
    mouse_look_button_name: str
    compact_manual_controls_layout: bool
    default_text_antialiasing_mode: str
    viewer_overlay_text_scale_factor: float
    minimum_tk_text_scale: float
    supports_tk_display_scaling: bool
    suppress_startup_focus_when_frozen: bool
    command_modifier_uses_control_fallback: bool
    shift_digit_bookmark_save_fallback: bool
    option_left_mouse_look_enabled: bool
    viewer_uses_glfw_native_initial_size: bool

    def __post_init__(self) -> None:
        platform_name = str(self.platform_name or "unsupported").strip().lower()
        object.__setattr__(self, "platform_name", platform_name or "unsupported")
        font_family = str(self.ui_font_family or "").strip()
        if not font_family:
            raise ValueError("presentation font family must be non-empty")
        object.__setattr__(self, "ui_font_family", font_family)
        candidates: list[str] = []
        for candidate in self.font_candidates:
            if candidate is None:
                continue
            normalized_candidate = str(candidate).strip()
            if normalized_candidate:
                candidates.append(normalized_candidate)
        if not candidates:
            raise ValueError("presentation font candidates must be non-empty")
        object.__setattr__(self, "font_candidates", tuple(candidates))
        if self.viewer_overlay_text_scale_factor <= 0:
            raise ValueError("overlay text scale factor must be positive")
        if self.minimum_tk_text_scale <= 0:
            raise ValueError("minimum Tk text scale must be positive")

    def viewer_overlay_text_scale(self, base_scale: float) -> float:
        """Return the platform-adjusted default viewer overlay text scale."""
        return float(base_scale) * self.viewer_overlay_text_scale_factor

    def tk_text_scale(self, default_font_points: float) -> float:
        """Return the platform-adjusted scale for fixed-size Tk font tokens."""
        try:
            default_scale = max(1.0, float(default_font_points) / 12.0)
        except (TypeError, ValueError):
            default_scale = 1.0
        return max(default_scale, self.minimum_tk_text_scale)

    def suppress_forced_startup_focus(
        self,
        *,
        is_frozen: bool,
        force_requested: bool,
    ) -> bool:
        """Return whether frozen-bundle startup should avoid focus forcing."""
        return bool(
            self.suppress_startup_focus_when_frozen
            and is_frozen
            and not force_requested
        )


_DEFAULT_PRESENTATION_PROFILE = PresentationProfile(
    platform_name="unsupported",
    ui_font_family="Segoe UI",
    font_candidates=_DEFAULT_FONT_CANDIDATES,
    uses_fontconfig_fallback=False,
    splash_layout=SplashLayoutPolicy(
        app_icon_resource_name="app_icon_macos.png",
        reuse_existing_root=False,
        destroy_root_on_close=True,
        windows_layout=False,
        linux_layout=False,
        window_width=940,
        min_height=680,
        extra_bottom_slack=0,
        secondary_link_row_bottom_gap=36,
        footer_credits_bottom_pad=36,
        title_to_action_gap=28,
        browse_button_bottom_gap=16,
        instruction_bottom_gap=0,
        secondary_link_row_top_gap=16,
    ),
    preferences_dialog_layout=PreferencesDialogLayoutPolicy(
        windows_layout=False,
        macos_layout=False,
        linux_layout=False,
        wrap_length=460,
        text_entry_width=36,
        body_pad_x=24,
        min_width=760,
        row_pad_x=18,
        row_pad_y=12,
        control_row_top_pad_y=14,
        tab_pad_x=14,
        tab_pad_y=7,
        tab_bottom_pad_y=18,
        button_row_top_pad_y=18,
        tab_highlight_thickness=1,
        notice_wrap_length=720,
        resizable_vertical=False,
    ),
    dialog_layout=DialogLayoutPolicy(
        body_pad_x=24,
        use_label_action_buttons=False,
    ),
    bookmark_save_modifier="control",
    primary_shortcut_modifier_label="Ctrl",
    tk_primary_modifier_name="Control",
    mouse_look_button_name="left",
    compact_manual_controls_layout=True,
    default_text_antialiasing_mode="normal",
    viewer_overlay_text_scale_factor=1.0,
    minimum_tk_text_scale=1.0,
    supports_tk_display_scaling=False,
    suppress_startup_focus_when_frozen=False,
    command_modifier_uses_control_fallback=False,
    shift_digit_bookmark_save_fallback=False,
    option_left_mouse_look_enabled=False,
    viewer_uses_glfw_native_initial_size=False,
)


def select_presentation_profile(*, platform_name: str) -> PresentationProfile:
    """Select immutable UI conventions from an injected platform name.

    No native platform adapter is created here.  This keeps unit tests exact
    and allows ``PlatformRuntime`` to compose presentation policy alongside
    other process-stable platform facts.
    """
    normalized_platform = str(platform_name or "").strip().lower()
    if normalized_platform == "darwin":
        return replace(
            _DEFAULT_PRESENTATION_PROFILE,
            platform_name="darwin",
            ui_font_family="Helvetica Neue",
            font_candidates=_MACOS_FONT_CANDIDATES,
            splash_layout=SplashLayoutPolicy(
                app_icon_resource_name="app_icon_macos.png",
                reuse_existing_root=True,
                destroy_root_on_close=False,
                windows_layout=False,
                linux_layout=False,
                window_width=1100,
                min_height=680,
                extra_bottom_slack=36,
                secondary_link_row_bottom_gap=28,
                footer_credits_bottom_pad=24,
                title_to_action_gap=48,
                browse_button_bottom_gap=28,
                instruction_bottom_gap=16,
                secondary_link_row_top_gap=24,
            ),
            preferences_dialog_layout=PreferencesDialogLayoutPolicy(
                windows_layout=False,
                macos_layout=True,
                linux_layout=False,
                wrap_length=300,
                text_entry_width=24,
                body_pad_x=12,
                min_width=430,
                row_pad_x=14,
                row_pad_y=5,
                control_row_top_pad_y=5,
                tab_pad_x=10,
                tab_pad_y=6,
                tab_bottom_pad_y=8,
                button_row_top_pad_y=8,
                tab_highlight_thickness=0,
                notice_wrap_length=390,
                resizable_vertical=False,
            ),
            dialog_layout=DialogLayoutPolicy(
                body_pad_x=18,
                use_label_action_buttons=True,
            ),
            bookmark_save_modifier="command",
            primary_shortcut_modifier_label="Cmd",
            tk_primary_modifier_name="Command",
            mouse_look_button_name="right",
            compact_manual_controls_layout=False,
            default_text_antialiasing_mode="light",
            viewer_overlay_text_scale_factor=1.15,
            minimum_tk_text_scale=1.4,
            suppress_startup_focus_when_frozen=True,
            command_modifier_uses_control_fallback=True,
            shift_digit_bookmark_save_fallback=True,
            option_left_mouse_look_enabled=True,
        )

    if normalized_platform.startswith("win"):
        return replace(
            _DEFAULT_PRESENTATION_PROFILE,
            platform_name="windows",
            font_candidates=_WINDOWS_FONT_CANDIDATES,
            splash_layout=SplashLayoutPolicy(
                app_icon_resource_name="app_icon_windows.png",
                reuse_existing_root=False,
                destroy_root_on_close=True,
                windows_layout=True,
                linux_layout=False,
                window_width=940,
                min_height=680,
                extra_bottom_slack=0,
                secondary_link_row_bottom_gap=36,
                footer_credits_bottom_pad=36,
                title_to_action_gap=58,
                browse_button_bottom_gap=32,
                instruction_bottom_gap=20,
                secondary_link_row_top_gap=30,
            ),
            preferences_dialog_layout=PreferencesDialogLayoutPolicy(
                windows_layout=True,
                macos_layout=False,
                linux_layout=False,
                wrap_length=520,
                text_entry_width=42,
                body_pad_x=32,
                min_width=860,
                row_pad_x=18,
                row_pad_y=12,
                control_row_top_pad_y=14,
                tab_pad_x=14,
                tab_pad_y=7,
                tab_bottom_pad_y=18,
                button_row_top_pad_y=18,
                tab_highlight_thickness=1,
                notice_wrap_length=720,
                resizable_vertical=True,
            ),
            dialog_layout=DialogLayoutPolicy(
                body_pad_x=32,
                use_label_action_buttons=False,
            ),
            supports_tk_display_scaling=True,
        )

    if normalized_platform.startswith("linux"):
        return replace(
            _DEFAULT_PRESENTATION_PROFILE,
            platform_name="linux",
            ui_font_family="sans-serif",
            font_candidates=_LINUX_FONT_CANDIDATES,
            uses_fontconfig_fallback=True,
            splash_layout=SplashLayoutPolicy(
                app_icon_resource_name="app_icon_macos.png",
                reuse_existing_root=False,
                destroy_root_on_close=True,
                windows_layout=False,
                linux_layout=True,
                window_width=940,
                min_height=680,
                extra_bottom_slack=0,
                secondary_link_row_bottom_gap=36,
                footer_credits_bottom_pad=36,
                title_to_action_gap=72,
                browse_button_bottom_gap=42,
                instruction_bottom_gap=30,
                secondary_link_row_top_gap=40,
            ),
            preferences_dialog_layout=PreferencesDialogLayoutPolicy(
                windows_layout=False,
                macos_layout=False,
                linux_layout=True,
                wrap_length=460,
                text_entry_width=36,
                body_pad_x=32,
                min_width=860,
                row_pad_x=18,
                row_pad_y=12,
                control_row_top_pad_y=14,
                tab_pad_x=14,
                tab_pad_y=7,
                tab_bottom_pad_y=18,
                button_row_top_pad_y=18,
                tab_highlight_thickness=1,
                notice_wrap_length=720,
                resizable_vertical=False,
            ),
            default_text_antialiasing_mode="light",
            supports_tk_display_scaling=True,
            viewer_uses_glfw_native_initial_size=True,
        )

    return _DEFAULT_PRESENTATION_PROFILE


def get_presentation_profile(
    *,
    platform_name: str | None = None,
) -> PresentationProfile:
    """Return a pure compatibility profile for callers outside a runtime."""
    return select_presentation_profile(platform_name=platform_name or sys.platform)


def _fontconfig_sans_font() -> str | None:
    """Resolve Linux's optional fontconfig fallback without changing policy."""
    try:
        result = subprocess.run(
            ["fc-match", "--format=%{file}", "sans-serif:style=Regular"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    path = result.stdout.strip()
    if path and path.lower().endswith((".ttf", ".otf", ".ttc")):
        return path
    return None


def font_candidates_for_profile(profile: PresentationProfile) -> tuple[str, ...]:
    """Return static candidates plus an optional action-time fontconfig match."""
    candidates = profile.font_candidates
    if not profile.uses_fontconfig_fallback:
        return candidates
    fontconfig_match = _fontconfig_sans_font()
    return candidates + ((fontconfig_match,) if fontconfig_match else ())
