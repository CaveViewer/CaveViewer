"""Tk startup surface for map selection, preferences, and updates.

The very first thing shown when CaveViewer launches: a small landing
window with the program name/version, the skull logo, and a Map Library
action for opening local map folders -- replacing the old behavior of jumping
straight into a bare native folder-picker dialog with zero context about what
the program even is.

Built with Tkinter (ships with standard Python on Windows/Mac, same
reasoning as the existing native folder-picker dialog already used
elsewhere in caveviewer.app -- no extra install needed). Styled to loosely
match the in-program overlays' dark background + amber accent look,
though Tkinter's native widgets can only approximate that so closely --
this is a real OS window with title bar and native buttons, not a custom-
drawn OpenGL overlay like the rest of the program's UI.

This is intentionally a SEPARATE function from the quick native chooser
helpers in caveviewer.app -- the splash screen is for the very first launch,
when the person hasn't seen the program yet and benefits from the context;
the OPEN button mid-session (see viewer_window.py) is for someone already
using the program, where a quick plain dialog is the better fit and a full
splash screen would just be unnecessary ceremony.

This window presents the process-owned update manager's state. Downloads may
continue after this Tk window closes; a registered Windows installer can also
perform one explicit verified install-and-restart handoff. Other packages stay
manual reveal-only.
"""

from __future__ import annotations

import enum
import math
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from caveviewer.version import APP_NAME, APP_VERSION
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.diagnostics.startup import (
    mark_startup_splash_visible,
    record_startup_stage,
)
from caveviewer.core.preferences.runtime_settings import RuntimeSettings
from caveviewer.gui.preferences_dialog import (
    PreferencesPanel,
    PreferencesPanelSnapshot,
)
from caveviewer.gui.preferences_workflow import (
    PreferencesCloseAction,
    resolve_preferences_close,
)
from caveviewer.gui.dpi_utils import (
    TkDisplayMetrics,
    TkWindowGeometry,
    configure_process_dpi_awareness,
    display_scale_changed,
    resolve_tk_display_metrics,
    scale_window_geometry,
    synchronize_tk_point_scale,
)
from caveviewer.gui.cache_rebuild_controller import CacheRebuildJobController
from caveviewer.gui.cave_metadata import (
    CaveMetadata,
    load_bundled_cave_metadata_catalog,
)
from caveviewer.gui.cave_metadata_panel import (
    CaveMetadataPanel,
    CaveMetadataPanelStyle,
)
from caveviewer.gui.controls_catalog import keyboard_control_sections
from caveviewer.gui.help_panel import HelpPanel, HelpPanelStyle
from caveviewer.core.diagnostics.catalog import application_log_directory
from caveviewer.gui.platform.diagnostic_log_reveal import (
    create_diagnostic_log_reveal_adapter,
)
from caveviewer.gui.troubleshooting_logs import TroubleshootingLogController
from caveviewer.gui.map_library_controller import MapLibraryController
from caveviewer.gui.map_history import load_recent_map_paths
from caveviewer.gui.map_library_panel import (
    MapLibraryPanel,
    MapLibraryPanelStyle,
)
from caveviewer.gui.map_library_workflow import (
    MapLibraryActionDependencies,
    MapLibraryCacheRebuildDependencies,
    MapLibraryCatalogDependencies,
    MapLibraryComposition,
    MapLibraryWorkflow,
)
from caveviewer.gui.map_selection import (
    validate_selected_map_folder as _validate_selected_map_folder,
)
from caveviewer.gui.loading_progress import (
    monotonic_progress,
    progress_segments,
    routine_progress_layout,
)
from caveviewer.gui.modal_dialog import (
    MODAL_CONTENT_PAD_X,
    MODAL_CONTENT_PAD_Y,
    MODAL_MIN_HEIGHT,
    MODAL_MIN_WIDTH,
)
from caveviewer.gui.platform.directory_selection import (
    choose_authorized_directory,
    directory_selection_preflight,
)
from caveviewer.gui.platform import (
    DesktopServiceError,
    DesktopServices,
    get_desktop_services,
    tk_root_options,
)
from caveviewer.gui.platform.presentation import (
    PresentationProfile,
    get_presentation_profile,
)
from caveviewer.gui.platform.presentation_actions import (
    PresentationActionsAdapter,
    create_presentation_actions_adapter,
)
from caveviewer.gui.preference_paths import migrate_state_file, write_text_atomic
from caveviewer.gui.splash_controller import (
    SplashController,
    SplashScheduler,
    StartupReadinessGate,
)
from caveviewer.gui.splash_visuals import (
    VectorEllipse,
    VectorPath,
    VectorPolygon,
    vector_icon_photo,
)
from caveviewer.gui.tk_feedback import (
    ERROR_FEEDBACK_MS,
    SUCCESS_FEEDBACK_MS,
    show_feedback,
)
from caveviewer.gui.tk_shortcuts import bind_primary_shortcut
from caveviewer.gui.tk_theme import DARK_THEME
from caveviewer.gui.tk_typography import TkTypography, create_tk_typography
from caveviewer.gui.update_manager import (
    UpdateManager,
    UpdateSnapshot,
    UpdateState,
)

if TYPE_CHECKING:
    from caveviewer.branding import BrandingAssets
    from caveviewer.gui.platform.runtime import PlatformRuntime


_LOGO_PATH: str | None = None
_PRESENTATION_PROFILE = get_presentation_profile()
_SPLASH_LAYOUT_POLICY = _PRESENTATION_PROFILE.splash_layout
_APP_ICON_PATH: str | None = None


@dataclass(frozen=True, slots=True)
class _SplashResumeState:
    """Tk shell state carried across a monitor-triggered recomposition."""

    geometry: TkWindowGeometry
    active_surface: str
    preferences: PreferencesPanelSnapshot | None = None
    map_scroll_fraction: float = 0.0
    cave: CaveMetadata | None = None
    display_metrics: TkDisplayMetrics | None = None
    window_state: str = "normal"


@dataclass(frozen=True, slots=True)
class _SplashRecomposeRequest:
    """Private result consumed by the public splash trampoline."""

    resume_state: _SplashResumeState


@dataclass(slots=True)
class _SettledNormalWindowGeometry:
    """Retain source-monitor bounds without accepting DPI-adjusted events."""

    geometry: TkWindowGeometry

    def observe(self, geometry: TkWindowGeometry, *, window_state: str) -> None:
        """Remember normal bounds; maximized bounds are not restorable."""
        if window_state == "normal":
            self.geometry = geometry


def _returning_library_needs_topmost(profile: PresentationProfile) -> bool:
    """Keep macOS focus recovery without rebuilding Windows' native frame."""
    return profile.platform_name == "darwin"


def _last_browse_path_file() -> str:
    """Resolve state lazily so environment overrides apply to this process."""
    return migrate_state_file("last_browse_path", ".caveviewer_last_browse_path")


def _tk_root_exists(root) -> bool:
    """Return whether a Tk root-like object is still usable."""
    if root is None:
        return False
    try:
        return bool(root.winfo_exists())
    except Exception:
        return False


def _destroy_tk_children(root) -> None:
    """Remove old splash widgets before rebuilding a reused root."""
    try:
        children = list(root.winfo_children())
    except Exception:
        return
    for child in children:
        try:
            child.destroy()
        except Exception:
            pass


def _create_splash_root(
    tk,
    *,
    presentation_profile: PresentationProfile | None = None,
):
    """
    Return the process Tk root for the splash screen.

    Retained-root platforms keep the Tk application alive while the native
    viewer runs. Reuse it on the next library cycle instead of creating a
    second Tk interpreter in the same process.
    """
    layout = (
        presentation_profile.splash_layout
        if presentation_profile is not None
        else _SPLASH_LAYOUT_POLICY
    )
    if layout.reuse_existing_root:
        existing_root = getattr(tk, "_default_root", None)
        if _tk_root_exists(existing_root):
            _destroy_tk_children(existing_root)
            return existing_root
    return tk.Tk(**tk_root_options())

# URL for example maps link -- empty/None means link is disabled
_EXAMPLE_MAPS_URL = None
_LOG = get_logger("CaveViewer")

_BG_COLOR = DARK_THEME.background
_PANEL_COLOR = DARK_THEME.panel
_TITLE_COLOR = DARK_THEME.title
_LIBRARY_FORMER_MAP_TITLE_COLOR = DARK_THEME.secondary_text
_SUBTITLE_COLOR = DARK_THEME.body_text
_INSTRUCTION_COLOR = DARK_THEME.secondary_text
_BUTTON_BG = DARK_THEME.primary_button
# The About wordmark uses a quieter gold that matches the refined About mark
# without changing the brighter amber used for interactive controls.
_ABOUT_WORDMARK_ACCENT = "#D99524"
_COPYRIGHT_SYMBOL = "©"
_WORDMARK_COPYRIGHT_GAP = 4
_WORDMARK_COPYRIGHT_OPTICAL_OFFSET = 2
_BUTTON_BORDER_COLOR = DARK_THEME.primary_button_border
# Navigation uses a location marker rather than a button treatment.  The
# background shift stays deliberately quiet; the amber rail and stronger label
# carry the selected-state meaning.
_NAVIGATION_HOVER_BG = DARK_THEME.entry_background
# Keep navigation entries distinct without making the rail read as a stack of
# separate cards. This shared spacing also scales with the active display.
_NAVIGATION_ITEM_GAP = 4
_EMBEDDED_PANEL_TEXT_SCALE_FACTOR = 1.0
_WINDOWS_SPLASH_LAYOUT = _SPLASH_LAYOUT_POLICY.windows_layout
_LINUX_SPLASH_LAYOUT = _SPLASH_LAYOUT_POLICY.linux_layout
_UI_FONT_FAMILY = _PRESENTATION_PROFILE.ui_font_family
_TK_TEXT_SCALE = 1.0
_CACHE_REBUILD_CLOSE_PAUSE_ATTEMPTS = 25
_UPDATE_READY_ACTION_DELAY_MS = 3_000
_MIN_LAUNCH_SPLASH_MS = 3_000
_LAUNCH_PROGRESS_INTERVAL_MS = 40
_PREFERENCES_SHELL_FIT_MAX_PASSES = 3

_TYPOGRAPHY: TkTypography = create_tk_typography(
    _UI_FONT_FAMILY,
    text_scale=_TK_TEXT_SCALE,
)
_SPLASH_WINDOW_WIDTH = _SPLASH_LAYOUT_POLICY.window_width
_SPLASH_WINDOW_MIN_HEIGHT = _SPLASH_LAYOUT_POLICY.min_height
_SPLASH_RESIZE_MIN_WIDTH = _SPLASH_LAYOUT_POLICY.resize_min_width
_SPLASH_RESIZE_MIN_HEIGHT = _SPLASH_LAYOUT_POLICY.resize_min_height
_SPLASH_WINDOW_EXTRA_BOTTOM_SLACK = _SPLASH_LAYOUT_POLICY.extra_bottom_slack
_CREDITS_TEXT = (
    "Concept by Brian Deatherage and Zsolt Szabo of\n"
    "BottomLine Projects Scientific Dive Team.\n"
    "Engineering and design by magic mr_v.\n\n"
    "Licensed under GNU AGPLv3-only.\n")
_CAVEVIEWER_WEBSITE_URL = "https://www.caveviewer.com"
_BOTTOMLINE_PROJECTS_WEBSITE_URL = "https://www.bottomlineprojects.com"
_ABOUT_WEBSITE_LINKS = (
    ("www.caveviewer.com", _CAVEVIEWER_WEBSITE_URL),
    ("www.bottomlineprojects.com", _BOTTOMLINE_PROJECTS_WEBSITE_URL),
)
_ABOUT_CREDITS_WRAP_LENGTH = 430
_LIBRARY_PANEL_COLOR = _BG_COLOR
_LIBRARY_PANEL_BORDER_COLOR = _BG_COLOR
_LIBRARY_FEATURED_ACTION_BG = "#202025"
_LIBRARY_FEATURED_ACTION_HOVER_BG = "#28282e"
_LIBRARY_METADATA_COLOR = "#5a5d68"
_LIBRARY_METADATA_STATUS_COLOR = DARK_THEME.secondary_text
_LIBRARY_METADATA_ERROR_COLOR = DARK_THEME.error_text
_LIBRARY_METADATA_STATUS_DURATION_MS = SUCCESS_FEEDBACK_MS
_LIBRARY_METADATA_ERROR_DURATION_MS = ERROR_FEEDBACK_MS
# Match cave-loading progress: a subdued empty track fills with the amber
# accent as work completes.
_LIBRARY_PROGRESS_TRACK_COLOR = DARK_THEME.entry_background
_LIBRARY_PROGRESS_FILL_COLOR = DARK_THEME.primary_button
# The circular retry arrow is visually denser than the neighboring chevron and
# download glyphs, so it deliberately receives a smaller optical footprint.
_LIBRARY_ACTION_RETRY_ICON_DIAMETER = 16
_LIBRARY_ACTION_STOP_SIZE = 6
_LIBRARY_ACTION_BUTTON_SIZE = 28
_LIBRARY_ACTION_ICON_STROKE_WIDTH = 2
_LIBRARY_OVERFLOW_BUTTON_SIZE = 24
_LIBRARY_OVERFLOW_FG = "#606370"
_LIBRARY_OVERFLOW_HOVER_FG = _INSTRUCTION_COLOR
_LIBRARY_OVERFLOW_HOVER_BG = DARK_THEME.secondary_button
_LIBRARY_MENU_BG = DARK_THEME.entry_background
_LIBRARY_MENU_BORDER = DARK_THEME.secondary_button_border
_LIBRARY_MENU_HOVER_BG = DARK_THEME.secondary_button_hover
_LIBRARY_MENU_TEXT = DARK_THEME.body_text
_LINUX_TK_SANS_FAMILIES = (
    "Adwaita Sans",
    "Cantarell",
    "Ubuntu Sans",
    "Ubuntu",
    "Noto Sans",
    "DejaVu Sans",
    "Liberation Sans",
    "sans-serif",
    "Sans",
)


def _presentation_profile_for_runtime(
    platform_runtime: PlatformRuntime | None,
) -> PresentationProfile:
    """Return the process profile, preserving direct splash callers."""
    profile = (
        getattr(platform_runtime, "presentation_profile", None)
        if platform_runtime is not None
        else None
    )
    return profile or get_presentation_profile()


def _branding_assets_for_runtime(
    platform_runtime: PlatformRuntime | None,
) -> BrandingAssets:
    """Return injected brand assets, with a default for direct GUI callers."""
    assets = (
        getattr(platform_runtime, "branding_assets", None)
        if platform_runtime is not None
        else None
    )
    if assets is not None:
        return assets
    from caveviewer.branding import resolve_branding_assets

    return resolve_branding_assets(environ={})


def _presentation_actions_adapter_for_runtime(
    platform_runtime: PlatformRuntime | None,
) -> PresentationActionsAdapter:
    """Return native presentation actions without using static adapter values."""
    actions = (
        getattr(platform_runtime, "presentation_actions_adapter", None)
        if platform_runtime is not None
        else None
    )
    if actions is not None:
        return actions
    return create_presentation_actions_adapter()


def _select_tk_font_family(
    available: dict[str, str],
    default_family: str,
    preferred: list[str],
    *,
    linux_layout: bool,
) -> str:
    """Choose a Tk-visible font family without spawning platform helpers."""
    for family in preferred:
        if not family:
            continue
        resolved_family = available.get(family.lower())
        if resolved_family:
            return resolved_family

    if linux_layout and str(default_family).lower() == "nimbus sans l":
        return "sans-serif"
    return default_family


def _refresh_tk_font_tokens() -> None:
    """Rebuild semantic Tk typography after selecting family or text scaling."""
    global _TYPOGRAPHY

    _TYPOGRAPHY = create_tk_typography(
        _UI_FONT_FAMILY,
        text_scale=_TK_TEXT_SCALE,
    )


def _activate_presentation_profile(
    profile: PresentationProfile,
    *,
    branding_assets: BrandingAssets,
    app_icon_path_override: str | None = None,
    platform_name: str,
) -> None:
    """Apply a runtime profile to legacy splash rendering tokens.

    The splash remains module-oriented for Tk callbacks, but each visible
    instance activates the process-owned immutable profile before it creates
    any widgets. This keeps static presentation choices out of the broad
    platform adapter while preserving the existing callback structure.
    """
    global _PRESENTATION_PROFILE, _SPLASH_LAYOUT_POLICY, _APP_ICON_PATH, _LOGO_PATH
    global _WINDOWS_SPLASH_LAYOUT, _LINUX_SPLASH_LAYOUT
    global _UI_FONT_FAMILY, _TK_TEXT_SCALE
    global _SPLASH_WINDOW_WIDTH, _SPLASH_WINDOW_MIN_HEIGHT
    global _SPLASH_RESIZE_MIN_WIDTH, _SPLASH_RESIZE_MIN_HEIGHT
    global _SPLASH_WINDOW_EXTRA_BOTTOM_SLACK

    _PRESENTATION_PROFILE = profile
    _SPLASH_LAYOUT_POLICY = profile.splash_layout
    _APP_ICON_PATH = app_icon_path_override or str(
        branding_assets.application_icon_for(platform_name)
    )
    _LOGO_PATH = str(branding_assets.about_mark)
    _WINDOWS_SPLASH_LAYOUT = _SPLASH_LAYOUT_POLICY.windows_layout
    _LINUX_SPLASH_LAYOUT = _SPLASH_LAYOUT_POLICY.linux_layout
    _UI_FONT_FAMILY = profile.ui_font_family
    _TK_TEXT_SCALE = 1.0
    _SPLASH_WINDOW_WIDTH = _SPLASH_LAYOUT_POLICY.window_width
    _SPLASH_WINDOW_MIN_HEIGHT = _SPLASH_LAYOUT_POLICY.min_height
    _SPLASH_RESIZE_MIN_WIDTH = _SPLASH_LAYOUT_POLICY.resize_min_width
    _SPLASH_RESIZE_MIN_HEIGHT = _SPLASH_LAYOUT_POLICY.resize_min_height
    _SPLASH_WINDOW_EXTRA_BOTTOM_SLACK = _SPLASH_LAYOUT_POLICY.extra_bottom_slack
    _refresh_tk_font_tokens()


def _configure_runtime_tk_fonts(
    root,
    *,
    presentation_profile: PresentationProfile | None = None,
    density_scale: float = 1.0,
) -> None:
    """Resolve the UI font against fonts Tk can actually render."""
    global _UI_FONT_FAMILY, _TK_TEXT_SCALE

    profile = presentation_profile or _PRESENTATION_PROFILE
    splash_layout = profile.splash_layout

    default_font_points = 12.0
    try:
        import tkinter.font as tkfont

        available = {family.lower(): family for family in tkfont.families(root)}
        preferred = [profile.ui_font_family]
        if splash_layout.linux_layout:
            # Keep splash startup on the Tk path free of subprocess waits.
            # Prefer families Tk already knows instead of asking fontconfig.
            preferred.extend(_LINUX_TK_SANS_FAMILIES)

        default_font = tkfont.nametofont("TkDefaultFont")
        fallback_family = default_font.actual("family")
        default_font_points = abs(float(default_font.actual("size") or default_font_points))
        resolved_family = _select_tk_font_family(
            available,
            fallback_family,
            preferred,
            linux_layout=splash_layout.linux_layout,
        )

        if resolved_family:
            _UI_FONT_FAMILY = resolved_family
            if splash_layout.linux_layout:
                _LOG.info(f"Using Tk UI font family: {_UI_FONT_FAMILY}")
    except Exception as exc:
        _LOG.warning(f"could not resolve Tk UI font family ({exc}); using {_UI_FONT_FAMILY}.")

    _TK_TEXT_SCALE = profile.tk_text_scale(default_font_points) * density_scale
    _refresh_tk_font_tokens()


def _map_library_panel_style(
    branding_assets: BrandingAssets | None = None,
) -> MapLibraryPanelStyle:
    """Return the splash-owned style tokens for the Map Library panel."""
    assets = branding_assets or _branding_assets_for_runtime(None)
    progress_tokens = assets.loading_progress
    return MapLibraryPanelStyle(
        panel_color=_LIBRARY_PANEL_COLOR,
        panel_border_color=_LIBRARY_PANEL_BORDER_COLOR,
        title_color=_TITLE_COLOR,
        former_map_title_color=_LIBRARY_FORMER_MAP_TITLE_COLOR,
        instruction_color=_INSTRUCTION_COLOR,
        title_font=_TYPOGRAPHY.body_strong,
        body_font=_TYPOGRAPHY.body,
        supporting_font=_TYPOGRAPHY.supporting,
        section_font=_TYPOGRAPHY.section,
        button_bg=_LIBRARY_PANEL_COLOR,
        button_fg=_BUTTON_BG,
        button_hover_bg=DARK_THEME.secondary_button,
        featured_action_bg=_LIBRARY_FEATURED_ACTION_BG,
        featured_action_hover_bg=_LIBRARY_FEATURED_ACTION_HOVER_BG,
        button_border_color=_BUTTON_BORDER_COLOR,
        disabled_button_bg=_LIBRARY_PANEL_COLOR,
        disabled_button_fg=DARK_THEME.placeholder_text,
        disabled_button_border=DARK_THEME.entry_border,
        empty_note_color="#5f606b",
        metadata_color=_LIBRARY_METADATA_COLOR,
        metadata_error_color=_LIBRARY_METADATA_ERROR_COLOR,
        metadata_status_color=_LIBRARY_METADATA_STATUS_COLOR,
        metadata_status_duration_ms=_LIBRARY_METADATA_STATUS_DURATION_MS,
        metadata_error_duration_ms=_LIBRARY_METADATA_ERROR_DURATION_MS,
        progress_track_color=progress_tokens.track_color,
        progress_fill_color=progress_tokens.fill_color,
        action_retry_icon_diameter=_LIBRARY_ACTION_RETRY_ICON_DIAMETER,
        action_stop_size=_LIBRARY_ACTION_STOP_SIZE,
        action_button_size=_LIBRARY_ACTION_BUTTON_SIZE,
        action_icon_stroke_width=_LIBRARY_ACTION_ICON_STROKE_WIDTH,
        overflow_button_size=_LIBRARY_OVERFLOW_BUTTON_SIZE,
        overflow_fg=_LIBRARY_OVERFLOW_FG,
        overflow_hover_fg=_LIBRARY_OVERFLOW_HOVER_FG,
        overflow_hover_bg=_LIBRARY_OVERFLOW_HOVER_BG,
        menu_bg=_LIBRARY_MENU_BG,
        menu_border=_LIBRARY_MENU_BORDER,
        menu_hover_bg=_LIBRARY_MENU_HOVER_BG,
        menu_text=_LIBRARY_MENU_TEXT,
    )


def _cave_metadata_panel_style() -> CaveMetadataPanelStyle:
    """Return the splash-owned style tokens for in-panel cave details."""
    return CaveMetadataPanelStyle(
        background_color=_BG_COLOR,
        title_color=_TITLE_COLOR,
        subtitle_color=_SUBTITLE_COLOR,
        section_color=_LIBRARY_METADATA_COLOR,
        body_color=_SUBTITLE_COLOR,
        divider_color=_LIBRARY_PANEL_BORDER_COLOR,
        link_color=_BUTTON_BG,
        link_hover_color=DARK_THEME.primary_button_hover,
        title_font=_TYPOGRAPHY.display,
        subtitle_font=_TYPOGRAPHY.body,
        section_font=_TYPOGRAPHY.section,
        body_strong_font=_TYPOGRAPHY.body_strong,
        body_font=_TYPOGRAPHY.body,
        small_font=_TYPOGRAPHY.supporting,
    )


def _embedded_panel_typography() -> TkTypography:
    """Return the compact type scale shared by Preferences and Help."""
    return create_tk_typography(
        _UI_FONT_FAMILY,
        text_scale=_TK_TEXT_SCALE * _EMBEDDED_PANEL_TEXT_SCALE_FACTOR,
    )


def _help_panel_style() -> HelpPanelStyle:
    """Return the splash-owned style tokens for the quiet Keys table."""
    typography = _embedded_panel_typography()
    return HelpPanelStyle(
        background_color=_BG_COLOR,
        tab_active_color=_BUTTON_BG,
        tab_focus_color=DARK_THEME.entry_focus_border,
        section_color=DARK_THEME.body_text,
        keycap_background_color=DARK_THEME.entry_background,
        keycap_border_color=DARK_THEME.secondary_button_border,
        keycap_text_color=DARK_THEME.body_text,
        action_color=DARK_THEME.body_text,
        detail_color=DARK_THEME.secondary_text,
        content_pad_x=_PRESENTATION_PROFILE.preferences_dialog_layout.body_pad_x,
        tab_font=typography.body_strong,
        section_font=typography.section,
        keycap_font=typography.body_strong,
        action_font=typography.body,
        overview_font=typography.body_strong,
        detail_font=typography.supporting,
        error_font=("Courier", typography.supporting[1]),
    )


def _suppress_amber_logo_pixels(logo_img):
    """Return an RGBA logo copy with shader-equivalent amber pixels hidden."""
    rgba_image = logo_img.convert("RGBA")
    filtered_pixels = []
    for red, green, blue, alpha in rgba_image.get_flattened_data():
        # Use a wider warm-color envelope than the OpenGL shader so resized,
        # antialiased edges of the logo's rings and tick marks cannot survive
        # as small dashes around a clean launch-progress treatment. The blue cave
        # artwork remains well outside these ratios.
        is_amber = (
            alpha > 5
            and red > 80
            and green > 40
            and red > blue * 1.25
            and green > blue * 1.05
        )
        filtered_pixels.append(
            (red, green, blue, 0) if is_amber else (red, green, blue, alpha)
        )
    rgba_image.putdata(filtered_pixels)
    return rgba_image


def _load_brand_logo(
    parent,
    *,
    px,
    max_dimension: int,
    suppress_amber: bool = False,
):
    """Load the About/launch brand image as a root-owned Tk photo."""
    logo_photo = None
    if _LOGO_PATH:
        try:
            from PIL import Image, ImageTk

            logo_img = Image.open(_LOGO_PATH)
            max_logo_dim = px(max_dimension)
            scale = min(
                max_logo_dim / logo_img.width,
                max_logo_dim / logo_img.height,
                1.0,
            )
            if scale < 1.0:
                logo_img = logo_img.resize(
                    (int(logo_img.width * scale), int(logo_img.height * scale)),
                    Image.LANCZOS,
                )
            if suppress_amber:
                logo_img = _suppress_amber_logo_pixels(logo_img)
            logo_photo = ImageTk.PhotoImage(
                logo_img,
                master=parent.winfo_toplevel(),
            )
        except Exception as exc:
            _LOG.warning("Could not load brand presentation logo: %s", exc)
    return logo_photo


def _launch_canvas_dimensions(canvas) -> tuple[int, int]:
    """Return usable canvas dimensions before and after Tk maps the launch UI."""
    dimensions = []
    for widget_method, option in (
        ("winfo_width", "width"),
        ("winfo_height", "height"),
    ):
        try:
            value = int(getattr(canvas, widget_method)())
        except Exception:
            value = 0
        if value <= 1:
            try:
                value = int(float(canvas.cget(option)))
            except Exception:
                value = 1
        dimensions.append(max(1, value))
    return tuple(dimensions)


def _canvas_text_metrics(canvas, *, text: str, font, px) -> tuple[float, float]:
    """Measure Canvas text from Tk's actual selected font metrics."""
    try:
        import tkinter.font as tkfont

        tk_font = tkfont.Font(root=canvas, font=font)
        return (
            max(1, tk_font.measure(text)),
            max(1, tk_font.metrics("linespace")),
        )
    except Exception:
        return max(1, px(len(text) * 10)), max(1, px(22))


def _render_launch_content(canvas, *, progress: float, px) -> None:
    """Render the shared loading bar beneath the centered product wordmark."""
    width, height = _launch_canvas_dimensions(canvas)
    canvas.delete("launch_content")
    wordmark_font = _TYPOGRAPHY.display
    cave_width, wordmark_height = _canvas_text_metrics(
        canvas,
        text="Cave",
        font=wordmark_font,
        px=px,
    )
    viewer_width, _ = _canvas_text_metrics(
        canvas,
        text="Viewer",
        font=wordmark_font,
        px=px,
    )
    copyright_font = _TYPOGRAPHY.supporting
    copyright_width, copyright_height = _canvas_text_metrics(
        canvas,
        text=_COPYRIGHT_SYMBOL,
        font=copyright_font,
        px=px,
    )
    copyright_gap = px(_WORDMARK_COPYRIGHT_GAP)
    layout = routine_progress_layout(
        center_x=width / 2,
        center_y=height / 2,
        title_height=wordmark_height,
        scale=px(1),
    )
    wordmark_left = width / 2 - (
        cave_width + viewer_width + copyright_gap + copyright_width
    ) / 2
    canvas.create_text(
        wordmark_left,
        layout.title_top,
        text="Cave",
        font=wordmark_font,
        fill=_ABOUT_WORDMARK_ACCENT,
        anchor="nw",
        tags="launch_content",
    )
    canvas.create_text(
        wordmark_left + cave_width,
        layout.title_top,
        text="Viewer",
        font=wordmark_font,
        fill=_TITLE_COLOR,
        anchor="nw",
        tags="launch_content",
    )
    canvas.create_text(
        wordmark_left + cave_width + viewer_width + copyright_gap,
        layout.title_top
        + max(0, (wordmark_height - copyright_height) / 2)
        + px(_WORDMARK_COPYRIGHT_OPTICAL_OFFSET),
        text=_COPYRIGHT_SYMBOL,
        font=copyright_font,
        fill=_SUBTITLE_COLOR,
        anchor="nw",
        tags="launch_content",
    )
    canvas.create_rectangle(
        layout.bar_left,
        layout.bar_top,
        layout.bar_right,
        layout.bar_bottom,
        fill=getattr(canvas, "_cv_progress_track_color", _LIBRARY_PROGRESS_TRACK_COLOR),
        outline="",
        tags="launch_content",
    )
    for left, right in progress_segments(
        layout.bar_left,
        layout.bar_right,
        progress,
    ):
        canvas.create_rectangle(
            left,
            layout.bar_top,
            right,
            layout.bar_bottom,
            fill=getattr(canvas, "_cv_progress_fill_color", _LIBRARY_PROGRESS_FILL_COLOR),
            outline="",
            tags="launch_content",
        )


def _build_launch_surface(
    parent,
    *,
    px,
    branding_assets: BrandingAssets | None = None,
):
    """Build the branded Void launch surface and return its canvas."""
    import tkinter as tk

    launch_canvas = tk.Canvas(
        parent,
        width=px(_SPLASH_WINDOW_WIDTH),
        height=px(_SPLASH_WINDOW_MIN_HEIGHT),
        bg=_BG_COLOR,
        borderwidth=0,
        highlightthickness=0,
    )
    launch_canvas.pack(fill="both", expand=True)
    launch_canvas._cv_launch_progress = 0.0
    progress_tokens = (
        branding_assets or _branding_assets_for_runtime(None)
    ).loading_progress
    launch_canvas._cv_progress_track_color = progress_tokens.track_color
    launch_canvas._cv_progress_fill_color = progress_tokens.fill_color

    def _refresh_launch_surface(_event=None) -> None:
        _render_launch_content(
            launch_canvas,
            progress=launch_canvas._cv_launch_progress,
            px=px,
        )

    launch_canvas.bind("<Configure>", _refresh_launch_surface, add="+")
    _refresh_launch_surface()
    return launch_canvas


def _settle_launch_layout(root, *, passes: int = 3) -> None:
    """Run a fixed, bounded number of Tk geometry-only settlement passes."""
    for _pass in range(max(0, int(passes))):
        root.update_idletasks()


def _preferred_shell_height_for_preferences(
    *,
    shell_height: int | float,
    viewport_height: int | float,
    content_height: int | float,
    minimum_height: int | float,
    available_height: int | float,
) -> int:
    """Grow a normal shell enough for its measured Preferences viewport."""
    current_height = max(1, int(round(shell_height)))
    viewport = max(0, int(round(viewport_height)))
    content = max(0, int(round(content_height)))
    minimum = max(1, int(round(minimum_height)))
    available = max(1, int(round(available_height)))
    required_height = max(minimum, current_height + max(0, content - viewport))
    return min(required_height, available)


def _fit_shell_height_to_preferences(
    *,
    shell_height: int | float,
    minimum_height: int | float,
    available_height: int | float,
    measure: Callable[
        [],
        tuple[int | float, int | float, int | float],
    ],
    apply_height: Callable[[int], None],
) -> int:
    """Converge a normal shell height with its settled Preferences viewport."""
    fitted_height = max(1, int(round(shell_height)))
    fit_confirmed = False
    for _pass in range(_PREFERENCES_SHELL_FIT_MAX_PASSES):
        actual_shell_height, viewport_height, content_height = measure()
        actual_height = max(0, int(round(actual_shell_height)))
        viewport = max(0, int(round(viewport_height)))
        content = max(0, int(round(content_height)))
        if actual_height <= 0 or viewport <= 0 or viewport > actual_height:
            # A retained Tk root can briefly report the old monitor's child
            # geometry after its requested destination bounds have changed.
            # Reject physically impossible measurements and give the bounded
            # settlement loop another chance instead of accepting a false fit.
            continue
        fitted_height = max(fitted_height, actual_height)
        if content <= viewport or fitted_height >= available_height:
            fit_confirmed = True
            break
        next_height = _preferred_shell_height_for_preferences(
            shell_height=actual_height,
            viewport_height=viewport,
            content_height=content,
            minimum_height=minimum_height,
            available_height=available_height,
        )
        next_height = max(fitted_height, next_height)
        if next_height <= fitted_height:
            # The requested root growth has not reached its children yet.
            # Keep settling rather than confirming the previous viewport.
            continue
        fitted_height = next_height
        apply_height(fitted_height)
    if not fit_confirmed:
        # The work-area bound is the only trustworthy geometry left. Prefer a
        # larger normal window over preserving an avoidable Preferences bar.
        fallback_height = max(
            fitted_height,
            max(1, int(round(available_height))),
        )
        if fallback_height > fitted_height:
            fitted_height = fallback_height
            apply_height(fitted_height)
    return fitted_height


def _navigation_gear_points(center: float, px) -> tuple[tuple[float, float], ...]:
    """Return the alternating outer/inner vertices for the Preferences gear."""
    return tuple(
        (
            center
            + math.cos(math.radians(index * 22.5 - 90))
            * px(11 if index % 2 == 0 else 8),
            center
            + math.sin(math.radians(index * 22.5 - 90))
            * px(11 if index % 2 == 0 else 8),
        )
        for index in range(16)
    )


def _build_themed_about_content(
    parent,
    *,
    program_name: str,
    version: str,
    px,
    on_close: Callable[[], None],
    on_open_website: Callable[[str], None] | None = None,
    center_vertically: bool = False,
    show_close: bool = True,
):
    """Build the shared About presentation inside either kind of host."""
    import tkinter as tk

    content = tk.Frame(parent, bg=_BG_COLOR)
    if center_vertically:
        content.pack(expand=True, padx=px(32), pady=px(28))
    else:
        content.pack(fill="both", expand=True, padx=px(32), pady=px(28))

    identity_lockup = tk.Frame(content, bg=_BG_COLOR)
    identity_lockup.pack(pady=(0, px(18)))

    logo_photo = _load_brand_logo(parent, px=px, max_dimension=112)
    if logo_photo is not None:
        logo_label = tk.Label(
            identity_lockup,
            image=logo_photo,
            bg=_BG_COLOR,
            borderwidth=0,
        )
        logo_label.image = logo_photo
        logo_label.pack(side="left", padx=(0, px(14)))

    identity_text = tk.Frame(identity_lockup, bg=_BG_COLOR)
    identity_text.pack(side="left")
    # Keep the product name visually unified while giving the two semantic
    # halves distinct brand emphasis.  Adjacent labels avoid introducing a
    # layout gap or changing the selected system typeface.
    wordmark = tk.Frame(identity_text, bg=_BG_COLOR)
    wordmark.pack(anchor="w")
    if program_name == "CaveViewer":
        tk.Label(
            wordmark,
            text="Cave",
            font=_TYPOGRAPHY.heading,
            fg=_ABOUT_WORDMARK_ACCENT,
            bg=_BG_COLOR,
            borderwidth=0,
        ).pack(side="left")
        tk.Label(
            wordmark,
            text="Viewer",
            font=_TYPOGRAPHY.heading,
            fg=_TITLE_COLOR,
            bg=_BG_COLOR,
            borderwidth=0,
        ).pack(side="left")
        # The asymmetric top inset moves the Pack-centered glyph down by half
        # its value, aligning its visible circle with the lowercase wordmark.
        tk.Label(
            wordmark,
            text=_COPYRIGHT_SYMBOL,
            font=_TYPOGRAPHY.supporting,
            fg=_SUBTITLE_COLOR,
            bg=_BG_COLOR,
            borderwidth=0,
        ).pack(
            side="left",
            padx=(px(_WORDMARK_COPYRIGHT_GAP), 0),
            pady=(px(_WORDMARK_COPYRIGHT_OPTICAL_OFFSET * 2), 0),
        )
    else:
        tk.Label(
            wordmark,
            text=program_name,
            font=_TYPOGRAPHY.heading,
            fg=_TITLE_COLOR,
            bg=_BG_COLOR,
            borderwidth=0,
        ).pack(side="left")
    tk.Label(
        identity_text,
        text=f"Version {version}",
        font=_TYPOGRAPHY.supporting,
        fg=_SUBTITLE_COLOR,
        bg=_BG_COLOR,
    ).pack(anchor="w", pady=(px(2), 0))

    tk.Label(
        content,
        text=_CREDITS_TEXT.strip(),
        font=_TYPOGRAPHY.body,
        fg=_SUBTITLE_COLOR,
        bg=_BG_COLOR,
        justify="center",
        wraplength=px(_ABOUT_CREDITS_WRAP_LENGTH),
    ).pack(fill="x")

    for index, (label_text, website_url) in enumerate(_ABOUT_WEBSITE_LINKS):
        website_label = tk.Label(
            content,
            text=label_text,
            font=_TYPOGRAPHY.body,
            fg=_BUTTON_BG if on_open_website is not None else _SUBTITLE_COLOR,
            bg=_BG_COLOR,
            takefocus=on_open_website is not None,
            highlightthickness=1,
            highlightbackground=_BG_COLOR,
            highlightcolor=_BUTTON_BG,
        )
        if on_open_website is not None:
            def open_website(_event=None, *, url=website_url):
                on_open_website(url)
                return "break"

            for sequence in ("<Button-1>", "<Return>", "<space>"):
                website_label.bind(sequence, open_website)
        website_label.pack(pady=(px(12) if index == 0 else px(6), 0))

    close_button = content
    if show_close:
        close_button = tk.Label(
            content,
            text="Close",
            font=_TYPOGRAPHY.body_strong,
            fg=DARK_THEME.primary_button_text,
            bg=_BUTTON_BG,
            takefocus=True,
            padx=px(24),
            pady=px(8),
            highlightthickness=1,
            highlightbackground=_BG_COLOR,
            highlightcolor=_BUTTON_BORDER_COLOR,
        )

        def close_about(_event=None):
            on_close()
            return "break"

        def set_close_button_hovered(hovered: bool) -> None:
            close_button.config(
                bg=(DARK_THEME.primary_button_hover if hovered else _BUTTON_BG),
            )

        for sequence in ("<Button-1>", "<Return>", "<space>"):
            close_button.bind(sequence, close_about)
        close_button.bind(
            "<Enter>",
            lambda _event: set_close_button_hovered(True),
        )
        close_button.bind(
            "<Leave>",
            lambda _event: set_close_button_hovered(False),
        )
        close_button.pack(pady=(px(20), 0))

    return close_button


def _show_themed_about_dialog(
    root,
    *,
    program_name: str,
    version: str,
    px,
    dialog_ref: list[object | None],
) -> None:
    """Show the reusable About presentation in a standalone modal."""
    active_dialog = dialog_ref[0]
    if _tk_root_exists(active_dialog):
        try:
            active_dialog.deiconify()
            active_dialog.lift(root)
            active_dialog.focus_force()
        except Exception:
            pass
        return

    import tkinter as tk

    dialog = tk.Toplevel(root)
    dialog_ref[0] = dialog
    dialog.withdraw()
    dialog.title(f"About {program_name}")
    dialog.configure(bg=_BG_COLOR)
    dialog.resizable(False, False)
    dialog.transient(root)
    _set_tk_window_icon(dialog)

    def close_dialog() -> None:
        if dialog_ref[0] is dialog:
            dialog_ref[0] = None
        try:
            dialog.grab_release()
        except tk.TclError:
            pass
        try:
            dialog.destroy()
        except tk.TclError:
            pass

    close_button = _build_themed_about_content(
        dialog,
        program_name=program_name,
        version=version,
        px=px,
        on_close=close_dialog,
    )

    def close_dialog_event(_event=None):
        close_dialog()
        return "break"

    dialog.bind("<Escape>", close_dialog_event)
    dialog.bind("<Return>", close_dialog_event)
    dialog.protocol("WM_DELETE_WINDOW", close_dialog)

    dialog.update_idletasks()
    dialog_width = max(px(430), dialog.winfo_reqwidth())
    dialog_height = max(px(380), dialog.winfo_reqheight())
    try:
        screen_width = dialog.winfo_screenwidth()
        screen_height = dialog.winfo_screenheight()
        x = root.winfo_rootx() + (root.winfo_width() - dialog_width) // 2
        y = root.winfo_rooty() + (root.winfo_height() - dialog_height) // 2
        x = max(0, min(x, screen_width - dialog_width))
        y = max(0, min(y, screen_height - dialog_height))
        dialog.geometry(f"{dialog_width}x{dialog_height}+{x}+{y}")
    except tk.TclError:
        dialog.geometry(f"{dialog_width}x{dialog_height}")

    dialog.deiconify()
    dialog.lift(root)
    try:
        dialog.grab_set()
        close_button.focus_set()
    except tk.TclError:
        pass


def _show_unsaved_preferences_dialog(
    root,
    *,
    px,
    dialog_ref: list[object | None],
    on_save: Callable[[], bool],
    on_discard: Callable[[], None],
    on_continue: Callable[[], None],
) -> None:
    """Offer save, discard, or continued editing before leaving Preferences."""
    active_dialog = dialog_ref[0]
    if _tk_root_exists(active_dialog):
        try:
            active_dialog.deiconify()
            active_dialog.lift(root)
            active_dialog.focus_force()
        except Exception:
            pass
        return

    import tkinter as tk
    from caveviewer.gui.modal_dialog import create_semantic_heading

    dialog = tk.Toplevel(root)
    dialog_ref[0] = dialog
    dialog.withdraw()
    dialog.title("Save changes to preferences?")
    dialog.configure(bg=_BG_COLOR)
    dialog.resizable(False, False)
    dialog.transient(root)
    _set_tk_window_icon(dialog)

    content = tk.Frame(dialog, bg=_BG_COLOR)
    content.pack(
        fill="both",
        expand=True,
        padx=px(MODAL_CONTENT_PAD_X),
        pady=px(MODAL_CONTENT_PAD_Y),
    )
    create_semantic_heading(
        content,
        title="Save changes to preferences?",
        kind="warning",
        px=px,
        font=_TYPOGRAPHY.body_strong,
        background=_BG_COLOR,
    ).pack(fill="x")
    tk.Label(
        content,
        text=(
            "Your Preferences changes have not been saved. "
            "Save or discard them before leaving Preferences."
        ),
        font=_TYPOGRAPHY.body,
        fg=_SUBTITLE_COLOR,
        bg=_BG_COLOR,
        justify="left",
        anchor="w",
        wraplength=px(360),
    ).pack(fill="x", pady=(px(8), px(20)))

    button_row = tk.Frame(content, bg=_BG_COLOR)
    button_row.pack(side="bottom", fill="x")

    def _close_dialog(_event=None):
        if dialog_ref[0] is dialog:
            dialog_ref[0] = None
        try:
            dialog.grab_release()
        except tk.TclError:
            pass
        try:
            dialog.destroy()
        except tk.TclError:
            pass
        return "break"

    def _discard(_event=None):
        _close_dialog()
        on_discard()
        return "break"

    def _save(_event=None):
        if not on_save():
            return "break"
        _close_dialog()
        on_continue()
        return "break"

    def _make_button(text: str, callback, *, primary: bool):
        normal_bg = _BUTTON_BG if primary else DARK_THEME.secondary_button
        hover_bg = (
            DARK_THEME.primary_button_hover
            if primary
            else DARK_THEME.secondary_button_hover
        )
        button = tk.Label(
            button_row,
            text=text,
            font=_TYPOGRAPHY.body_strong,
            fg=DARK_THEME.primary_button_text if primary else _TITLE_COLOR,
            bg=normal_bg,
            takefocus=True,
            padx=px(14),
            pady=px(7),
            highlightthickness=1,
            highlightbackground=_BG_COLOR,
            highlightcolor=_BUTTON_BORDER_COLOR,
        )
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            button.bind(sequence, callback)
        button.bind("<Enter>", lambda _event: button.config(bg=hover_bg))
        button.bind("<Leave>", lambda _event: button.config(bg=normal_bg))
        return button

    save_button = _make_button("Save", _save, primary=True)
    discard_button = _make_button("Discard", _discard, primary=False)
    keep_button = _make_button("Keep", _close_dialog, primary=False)
    save_button.pack(side="right")
    discard_button.pack(side="right", padx=(0, px(8)))
    keep_button.pack(side="right", padx=(0, px(8)))

    dialog.bind("<Escape>", _close_dialog)
    dialog.protocol("WM_DELETE_WINDOW", _close_dialog)
    dialog.update_idletasks()
    dialog_width = max(px(MODAL_MIN_WIDTH), dialog.winfo_reqwidth())
    dialog_height = max(px(MODAL_MIN_HEIGHT), dialog.winfo_reqheight())
    try:
        screen_width = dialog.winfo_screenwidth()
        screen_height = dialog.winfo_screenheight()
        x = root.winfo_rootx() + (root.winfo_width() - dialog_width) // 2
        y = root.winfo_rooty() + (root.winfo_height() - dialog_height) // 2
        x = max(0, min(x, screen_width - dialog_width))
        y = max(0, min(y, screen_height - dialog_height))
        dialog.geometry(f"{dialog_width}x{dialog_height}+{x}+{y}")
    except tk.TclError:
        dialog.geometry(f"{dialog_width}x{dialog_height}")

    dialog.deiconify()
    dialog.lift(root)
    try:
        dialog.grab_set()
        save_button.focus_set()
    except tk.TclError:
        pass


def _set_tk_window_icon(window) -> None:
    if not _APP_ICON_PATH:
        return
    try:
        from PIL import Image, ImageTk
        icon_img = Image.open(_APP_ICON_PATH)
        icon_photo = ImageTk.PhotoImage(icon_img, master=window)
        window.iconphoto(True, icon_photo)
        window._cv_app_icon_photo = icon_photo
    except Exception as e:
        _LOG.warning(f"could not set application window icon ({e}); continuing without it.")


class _UpdateAction(enum.Enum):
    DOWNLOAD = "download"
    INSTALL = "install"
    RETRY = "retry"
    CANCEL = "cancel"
    REVEAL = "reveal"


@dataclass(frozen=True)
class _UpdatePresentation:
    status_text: str = ""
    action_text: str = ""
    action: _UpdateAction | None = None
    status_action: _UpdateAction | None = None
    action_replaces_status_after_delay: bool = False
    progress_visible: bool = False
    progress_fraction: float | None = 0.0
    error: bool = False


def _invoke_update_action(update_manager: UpdateManager, action: _UpdateAction) -> None:
    """Route a compact splash action to the process-owned update manager."""
    if action == _UpdateAction.DOWNLOAD:
        update_manager.start_download()
    elif action == _UpdateAction.INSTALL:
        update_manager.start_installation()
    elif action == _UpdateAction.RETRY:
        if not update_manager.start_installation():
            update_manager.start_download()
    elif action == _UpdateAction.CANCEL:
        update_manager.cancel_download()
    elif action == _UpdateAction.REVEAL:
        update_manager.reveal_download()


def _bind_update_label_action(
    label,
    update_manager: UpdateManager,
    action: _UpdateAction | None,
) -> None:
    """Bind pointer and keyboard activation for an optional update action."""
    for sequence in ("<Button-1>", "<Return>", "<space>"):
        label.unbind(sequence)
    enabled = action is not None
    label.config(takefocus=enabled)
    if not enabled:
        return

    def invoke(_event=None):
        _invoke_update_action(update_manager, action)
        return "break"

    label.bind("<Button-1>", invoke)
    label.bind("<Return>", invoke)
    label.bind("<space>", invoke)


def _update_presentation(snapshot: UpdateSnapshot) -> _UpdatePresentation:
    """Map manager states to the exact compact labels rendered by the splash."""
    if (
        snapshot.automatic_update is not None
        and not snapshot.automatic_update.allows_execution
    ):
        return _UpdatePresentation(status_text=snapshot.automatic_update.explanation)
    if snapshot.state == UpdateState.AVAILABLE:
        action_text = "Update"
        if snapshot.available_version:
            action_text = f"Update to version {snapshot.available_version}"
        return _UpdatePresentation(
            action_text=action_text,
            action=(
                _UpdateAction.INSTALL
                if snapshot.install_action_label
                else _UpdateAction.DOWNLOAD
            ),
        )
    if snapshot.state == UpdateState.DOWNLOADING:
        return _UpdatePresentation(
            status_text=f"Downloading… {snapshot.progress_percent}%",
            action_text="Cancel",
            action=_UpdateAction.CANCEL,
            progress_visible=True,
            progress_fraction=snapshot.progress_percent / 100.0,
        )
    if snapshot.state == UpdateState.VERIFYING:
        return _UpdatePresentation(
            status_text="Verifying…",
            action_text="Cancel",
            action=_UpdateAction.CANCEL,
            progress_visible=True,
            progress_fraction=None,
        )
    if snapshot.state == UpdateState.READY:
        if snapshot.install_action_label:
            if snapshot.install_requested:
                return _UpdatePresentation(status_text="Preparing update…")
            status_text = "Update ready"
            if snapshot.error:
                status_text = "Installer could not start"
            action_text = snapshot.install_action_label
            return _UpdatePresentation(
                status_text=status_text,
                action_text=action_text,
                action=_UpdateAction.INSTALL,
            )
        if (
            snapshot.update_package_reveal is not None
            and not snapshot.update_package_reveal.allows_execution
        ):
            return _UpdatePresentation(
                status_text=snapshot.update_package_reveal.explanation
            )
        return _UpdatePresentation(
            status_text="Update ready",
            action_text=snapshot.reveal_action_label,
            action=_UpdateAction.REVEAL,
            action_replaces_status_after_delay=True,
        )
    if snapshot.state == UpdateState.HANDOFF_VERIFYING:
        return _UpdatePresentation(
            status_text="Verifying installer…",
            progress_visible=True,
            progress_fraction=None,
        )
    if snapshot.state == UpdateState.INSTALLING:
        return _UpdatePresentation(status_text="Starting update…")
    if snapshot.state == UpdateState.FAILED:
        if snapshot.install_action_label and snapshot.install_requested:
            return _UpdatePresentation(
                status_text="Update download failed",
                action_text="Retry installation",
                action=_UpdateAction.RETRY,
                error=True,
            )
        return _UpdatePresentation(
            status_text="Download failed",
            action_text="Retry",
            action=_UpdateAction.RETRY,
            error=True,
        )
    return _UpdatePresentation()


def _update_status_label(
    presentation: _UpdatePresentation,
    *,
    show_delayed_action: bool = False,
) -> tuple[str, str, _UpdateAction | None]:
    """Return the one status label's current text, color, and action."""
    if (
        presentation.action_replaces_status_after_delay
        and show_delayed_action
    ):
        return presentation.action_text, _BUTTON_BG, presentation.action
    return (
        presentation.status_text,
        "#ff9b90" if presentation.error else _INSTRUCTION_COLOR,
        presentation.status_action,
    )


def show_splash_screen(
    program_name: str = APP_NAME,
    version: str = APP_VERSION,
    *,
    update_manager: UpdateManager,
    desktop_services: DesktopServices | None = None,
    platform_runtime: PlatformRuntime | None = None,
    runtime_settings_provider: Callable[[], RuntimeSettings] | None = None,
    on_preferences_saved: Callable[[object], object] | None = None,
    show_launch_overlay: bool = True,
    map_open_error_details: str | None = None,
) -> str | None:
    """Run responsive shell compositions without exposing recompose results."""
    resume_state = None
    launch_overlay = show_launch_overlay
    while True:
        result = _show_splash_composition(
            program_name=program_name,
            version=version,
            update_manager=update_manager,
            desktop_services=desktop_services,
            platform_runtime=platform_runtime,
            runtime_settings_provider=runtime_settings_provider,
            on_preferences_saved=on_preferences_saved,
            show_launch_overlay=launch_overlay,
            map_open_error_details=map_open_error_details,
            resume_state=resume_state,
        )
        if not isinstance(result, _SplashRecomposeRequest):
            return result
        resume_state = result.resume_state
        launch_overlay = False
        map_open_error_details = None


def _show_splash_composition(
    program_name: str = APP_NAME,
    version: str = APP_VERSION,
    *,
    update_manager: UpdateManager,
    desktop_services: DesktopServices | None = None,
    platform_runtime: PlatformRuntime | None = None,
    runtime_settings_provider: Callable[[], RuntimeSettings] | None = None,
    on_preferences_saved: Callable[[object], object] | None = None,
    show_launch_overlay: bool = True,
    map_open_error_details: str | None = None,
    resume_state: _SplashResumeState | None = None,
) -> str | None | _SplashRecomposeRequest:
    """
    Builds the Map Library and blocks until the person either picks a folder
    (Browse -> select a folder -> OK) or closes the window. The branded launch
    overlay is optional so later library sessions can be composed off-screen
    and revealed directly. Returns the selected folder path, or None if the
    window was closed without picking one. Update work belongs to app.py and
    may outlive this particular UI instance.
    """
    record_startup_stage("splash_function_entered")
    record_startup_stage("tkinter_import_begin")
    import tkinter as tk
    record_startup_stage("tkinter_import_complete")

    if desktop_services is None:
        desktop_services = (
            platform_runtime.desktop_services
            if platform_runtime is not None
            else get_desktop_services()
        )
    runtime_settings = (
        runtime_settings_provider()
        if runtime_settings_provider is not None
        else getattr(platform_runtime, "runtime_settings", None)
    )

    def current_runtime_settings() -> RuntimeSettings | None:
        return (
            runtime_settings_provider()
            if runtime_settings_provider is not None
            else runtime_settings
        )

    def current_map_library_configuration():
        """Resolve Map Library inputs from the latest application snapshot."""
        from caveviewer.gui.standard_library_maps import (
            default_map_library_configuration,
            map_library_configuration_from_runtime_settings,
        )

        active_settings = current_runtime_settings()
        if active_settings is None:
            return default_map_library_configuration()
        return map_library_configuration_from_runtime_settings(
            active_settings.map_library_configuration()
        )

    def current_import_runtime_settings():
        """Return the latest child-process settings for a Map Library rebuild."""

        active_settings = current_runtime_settings()
        if active_settings is None:
            return None
        return active_settings.import_configuration()

    viewer_settings = (
        runtime_settings.viewer_configuration()
        if runtime_settings is not None
        else None
    )
    presentation_profile = _presentation_profile_for_runtime(platform_runtime)
    branding_assets = _branding_assets_for_runtime(platform_runtime)
    presentation_actions_adapter = _presentation_actions_adapter_for_runtime(
        platform_runtime
    )
    _activate_presentation_profile(
        presentation_profile,
        branding_assets=branding_assets,
        app_icon_path_override=(
            viewer_settings.app_icon if viewer_settings is not None else None
        ),
        platform_name=(
            platform_runtime.profile.platform_name
            if platform_runtime is not None
            else presentation_profile.platform_name
        ),
    )

    record_startup_stage("splash_root_create_begin")
    configure_process_dpi_awareness(
        presentation_actions_adapter=presentation_actions_adapter
    )
    root = _create_splash_root(
        tk,
        presentation_profile=presentation_profile,
    )
    splash_controller = SplashController(
        SplashScheduler(root.after, root.after_cancel, root.after_idle)
    )
    splash_controller.start()
    record_startup_stage("splash_root_create_complete")
    carried_display_metrics = (
        resume_state.display_metrics if resume_state is not None else None
    )
    display_metrics = carried_display_metrics or resolve_tk_display_metrics(
        root,
        presentation_profile=presentation_profile,
        presentation_actions_adapter=presentation_actions_adapter,
        scale_override=(viewer_settings.tk_scale if viewer_settings is not None else None),
    )
    applied_tk_point_scale = display_metrics.tk_point_scale
    if carried_display_metrics is not None:
        applied_tk_point_scale = synchronize_tk_point_scale(root, display_metrics)
    _configure_runtime_tk_fonts(
        root,
        presentation_profile=presentation_profile,
        density_scale=display_metrics.density_scale,
    )
    splash_scale = display_metrics.layout_scale
    try:
        tk_patchlevel = str(root.tk.call("info", "patchlevel"))
    except Exception:
        tk_patchlevel = "unknown"
    _LOG.info(
        "Tk display metrics: platform=%s, awareness=%s, native_dpi=%s, "
        "geometry_scale=%.4f, monitor_diagonal_inches=%s, density_scale=%.4f, "
        "layout_scale=%.4f, tk_scaling_observed=%.4f, "
        "tk_scaling_target=%s, tk_scaling_applied=%.4f, tk=%s, override=%s.",
        presentation_profile.platform_name,
        (
            "per-monitor-v2-with-fallbacks"
            if presentation_profile.platform_name == "windows"
            else "platform-default"
        ),
        (
            f"{display_metrics.native_dpi:.1f}"
            if display_metrics.native_dpi is not None
            else "unavailable"
        ),
        display_metrics.geometry_scale,
        (
            f"{display_metrics.monitor_diagonal_inches:.1f}"
            if display_metrics.monitor_diagonal_inches is not None
            else "unavailable"
        ),
        display_metrics.density_scale,
        display_metrics.layout_scale,
        display_metrics.tk_point_scale,
        (
            f"{display_metrics.target_tk_point_scale:.4f}"
            if display_metrics.target_tk_point_scale is not None
            else "unchanged"
        ),
        applied_tk_point_scale,
        tk_patchlevel,
        display_metrics.override_active,
    )

    def px(value: float) -> int:
        return int(round(value * splash_scale))

    # Keep hidden until final geometry is set to avoid a visible corner->center jump.
    root.withdraw()
    root.title(program_name)
    root.configure(bg=_BG_COLOR)
    # Establish the final native frame style before the first reveal. Toggling
    # resize capability during the launch-to-library handoff makes Windows
    # rebuild its non-client frame and produces a visible flash.
    root.resizable(True, True)
    _set_tk_window_icon(root)

    presentation_actions_adapter.install_about_handler(root, program_name, version)

    # Initial startup is centered; monitor-triggered recomposition restores
    # the already ratio-scaled bounds on the destination monitor.
    root.update_idletasks()
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    display_margin = px(80)
    if display_metrics.work_area is None:
        available_width = max(1, screen_w - display_margin)
        available_height = max(1, screen_h - display_margin)
    else:
        work_left, work_top, work_right, work_bottom = display_metrics.work_area
        available_width = max(1, work_right - work_left)
        available_height = max(1, work_bottom - work_top)
    if resume_state is None:
        window_w = min(px(_SPLASH_WINDOW_WIDTH), available_width)
        window_h = min(px(_SPLASH_WINDOW_MIN_HEIGHT), available_height)
        pos_x = (screen_w - window_w) // 2
        pos_y = (screen_h - window_h) // 3
    else:
        restored = resume_state.geometry
        window_w = min(restored.width, available_width)
        window_h = min(restored.height, available_height)
        pos_x = restored.x
        pos_y = restored.y
    # Compact displays must never receive a minimum larger than the initial
    # display-clamped window. Normal displays retain the shared shell minimum.
    root.minsize(
        min(px(_SPLASH_RESIZE_MIN_WIDTH), available_width),
        min(px(_SPLASH_RESIZE_MIN_HEIGHT), available_height),
    )
    root.geometry(f"{window_w}x{window_h}+{pos_x}+{pos_y}")

    root.grid_rowconfigure(0, weight=1)
    root.grid_columnconfigure(0, weight=1)
    launch_surface = None
    launch_indicator = None
    launch_visible_at = 0.0
    if show_launch_overlay:
        launch_surface = tk.Frame(root, bg=_BG_COLOR)
        launch_surface.grid(row=0, column=0, sticky="nsew")
        launch_indicator = _build_launch_surface(
            launch_surface,
            px=px,
            branding_assets=branding_assets,
        )

    content_frame = tk.Frame(root, bg=_BG_COLOR)
    content_frame.grid(
        row=0,
        column=0,
        sticky="nsew",
        padx=px(18),
        pady=px(14),
    )
    recomposition_cover = None
    recomposition_alpha_hidden = False
    if show_launch_overlay:
        launch_surface.tkraise()
        launch_visible_at = time.monotonic()
        root.deiconify()
        root.lift()
        root.focus_force()
        root.attributes("-topmost", True)
        splash_controller.schedule(
            200, lambda: root.attributes("-topmost", False)
        )
        _settle_launch_layout(root, passes=1)

    readiness_gate = StartupReadinessGate(
        visible_at=launch_visible_at,
        minimum_ms=_MIN_LAUNCH_SPLASH_MS,
    )

    def _advance_launch_progress(fraction: float) -> None:
        """Paint one monotonic composition milestone while startup is visible."""
        progress = readiness_gate.advance(fraction)
        if launch_indicator is None:
            return
        launch_indicator._cv_launch_progress = progress
        _render_launch_content(launch_indicator, progress=progress, px=px)
        root.update_idletasks()

    if show_launch_overlay:
        _advance_launch_progress(0.08)

    # The splash is organized as a stable navigation rail beside an active
    # content surface. Keeping the rail a fixed width prevents map-library
    # and Preferences content from jumping as users navigate.
    left_frame = tk.Frame(content_frame, bg=_BG_COLOR, width=px(190))
    left_frame.pack(side="left", fill="y")
    left_frame.pack_propagate(False)

    right_frame = tk.Frame(content_frame, bg=_BG_COLOR)
    right_frame.pack(
        side="left",
        fill="both",
        expand=True,
        padx=(px(24), 0),
    )
    right_frame.grid_rowconfigure(0, weight=1)
    right_frame.grid_columnconfigure(0, weight=1)
    map_library_surface = tk.Frame(right_frame, bg=_BG_COLOR)
    preferences_surface = tk.Frame(right_frame, bg=_BG_COLOR)
    help_surface = tk.Frame(right_frame, bg=_BG_COLOR)
    about_surface = tk.Frame(right_frame, bg=_BG_COLOR)
    cave_metadata_surface = tk.Frame(right_frame, bg=_BG_COLOR)
    for surface in (
        map_library_surface,
        preferences_surface,
        help_surface,
        about_surface,
        cave_metadata_surface,
    ):
        surface.grid(row=0, column=0, sticky="nsew")
    stacked_surfaces = {
        "map_library": map_library_surface,
        "preferences": preferences_surface,
        "help": help_surface,
        "about": about_surface,
        "cave_metadata": cave_metadata_surface,
    }
    map_library_surface.tkraise()

    navigation_frame = tk.Frame(left_frame, bg=_BG_COLOR)
    navigation_frame.pack(fill="x", pady=(px(18), 0))

    app_status_frame = tk.Frame(left_frame, bg=_BG_COLOR)
    app_status_frame.pack(
        side="bottom",
        fill="x",
        padx=px(12),
        pady=(0, px(12)),
    )
    last_update_presentation: list[_UpdatePresentation | None] = [None]
    map_library_workflow_ref: list[MapLibraryWorkflow | None] = [None]
    map_library_panel_ref: list[MapLibraryPanel | None] = [None]
    preferences_panel_ref: list[PreferencesPanel | None] = [None]
    help_panel_ref: list[HelpPanel | None] = [None]
    about_surface_initialized = [False]
    discard_preferences_dialog_ref: list[object | None] = [None]
    active_surface = ["map_library"]
    active_cave: list[CaveMetadata | None] = [None]
    recompose_request: list[_SplashRecomposeRequest | None] = [None]

    # The status frame stays anchored to the lower-left rail and remains
    # completely quiet until an update has a meaningful state.
    update_cluster = tk.Frame(app_status_frame, bg=_BG_COLOR)
    update_status_row = tk.Frame(update_cluster, bg=_BG_COLOR)
    update_status_row.pack(anchor="w", fill="x")

    update_label = tk.Label(
        update_status_row,
        text="",
        font=_TYPOGRAPHY.supporting,
        fg=_INSTRUCTION_COLOR,
        bg=_BG_COLOR,
        takefocus=False,
        highlightthickness=1,
        highlightbackground=_BG_COLOR,
        highlightcolor=_BUTTON_BG,
        wraplength=px(192),
        justify="left",
        anchor="w",
    )

    update_action_label = tk.Label(
        update_status_row,
        text="",
        # Footer actions are links to a follow-on update task, not the primary
        # action of the active panel. Keep their hierarchy with the status
        # text; amber color and interaction behavior provide the affordance.
        font=_TYPOGRAPHY.supporting,
        fg=_BUTTON_BG,
        bg=_BG_COLOR,
        takefocus=False,
        highlightthickness=1,
        highlightbackground=_BG_COLOR,
        highlightcolor=_BUTTON_BG,
        wraplength=px(192),
        justify="left",
        anchor="w",
    )

    update_progress_bar = tk.Canvas(
        update_cluster,
        width=px(192),
        height=max(1, px(3)),
        bg=_BG_COLOR,
        borderwidth=0,
        highlightthickness=0,
        takefocus=False,
    )
    update_progress_bar._cv_progress_phase = 0.0
    update_progress_bar._cv_progress_after_id = None
    update_progress_bar._cv_display_fraction = 0.0
    update_progress_bar._cv_progress_visible = False
    update_progress_bar.pack(anchor="w", fill="x", pady=(px(6), 0))

    def _cancel_update_progress_animation(_event=None) -> None:
        after_id = update_progress_bar._cv_progress_after_id
        update_progress_bar._cv_progress_after_id = None
        if after_id is not None:
            try:
                root.after_cancel(after_id)
            except tk.TclError:
                pass

    def _draw_update_progress_bar(progress_fraction: float | None) -> None:
        """Draw shared update progress without coupling it to Cancel."""
        update_progress_bar.delete("all")
        if not update_progress_bar._cv_progress_visible:
            return
        width = max(1, update_progress_bar.winfo_width())
        height = max(1, int(float(update_progress_bar.cget("height"))))
        progress_tokens = branding_assets.loading_progress
        update_progress_bar.create_rectangle(
            0,
            0,
            width,
            height,
            fill=progress_tokens.track_color,
            outline="",
        )
        for left, right in progress_segments(
            0.0,
            float(width),
            progress_fraction,
            phase=update_progress_bar._cv_progress_phase,
        ):
            update_progress_bar.create_rectangle(
                left,
                0,
                right,
                height,
                fill=progress_tokens.fill_color,
                outline="",
            )

    def _animate_update_progress() -> None:
        update_progress_bar._cv_progress_phase = (
            update_progress_bar._cv_progress_phase + 0.045
        ) % 1.0
        _draw_update_progress_bar(None)
        try:
            update_progress_bar._cv_progress_after_id = root.after(
                40,
                _animate_update_progress,
            )
        except tk.TclError:
            update_progress_bar._cv_progress_after_id = None

    update_progress_bar.bind(
        "<Destroy>",
        _cancel_update_progress_animation,
        add="+",
    )

    def _set_update_cluster_visible(visible: bool) -> None:
        if visible:
            if not update_cluster.winfo_manager():
                update_cluster.pack(anchor="w", fill="x", pady=(px(10), 0))
            return
        update_cluster.pack_forget()

    def _layout_update_cluster(presentation: _UpdatePresentation) -> None:
        """Update content without changing the reserved cluster geometry."""
        _cancel_update_progress_animation()
        update_label.pack_forget()
        update_action_label.pack_forget()
        update_progress_bar._cv_progress_visible = presentation.progress_visible
        if not presentation.progress_visible:
            update_progress_bar._cv_display_fraction = 0.0
            _draw_update_progress_bar(None)

        if presentation.status_text:
            update_label.pack(side="left", anchor="w", fill="x", expand=True)
        if (
            presentation.action_text
            and not presentation.action_replaces_status_after_delay
        ):
            update_action_label.pack(
                side="left",
                anchor="w",
                padx=(px(6), 0) if presentation.status_text else 0,
            )
        if presentation.progress_visible:
            display_fraction = presentation.progress_fraction
            if display_fraction is not None:
                update_progress_bar._cv_display_fraction = monotonic_progress(
                    update_progress_bar._cv_display_fraction,
                    display_fraction,
                )
                display_fraction = update_progress_bar._cv_display_fraction
            _draw_update_progress_bar(display_fraction)
            if presentation.progress_fraction is None:
                _animate_update_progress()

        _set_update_cluster_visible(
            bool(
                presentation.status_text
                or (
                    presentation.action_text
                    and not presentation.action_replaces_status_after_delay
                )
                or presentation.progress_visible
            )
        )

    def _show_delayed_update_action(presentation: _UpdatePresentation) -> None:
        """Replace the completion status with its single follow-up action."""
        if last_update_presentation[0] != presentation:
            return
        label_text, label_color, label_action = _update_status_label(
            presentation,
            show_delayed_action=True,
        )
        update_label.config(text=label_text, fg=label_color)
        _bind_update_label_action(update_label, update_manager, label_action)

    def _apply_update_presentation(presentation: _UpdatePresentation) -> None:
        label_text, label_color, label_action = _update_status_label(presentation)
        update_label.config(
            text=label_text,
            fg=label_color,
        )
        update_action_label.config(text=presentation.action_text)
        _bind_update_label_action(update_label, update_manager, label_action)
        _bind_update_label_action(
            update_action_label,
            update_manager,
            presentation.action,
        )
        _layout_update_cluster(presentation)
        if presentation.action_replaces_status_after_delay:
            splash_controller.schedule(
                _UPDATE_READY_ACTION_DELAY_MS,
                lambda: _show_delayed_update_action(presentation),
            )

    def _refresh_update_presentation() -> None:
        if splash_controller.closing:
            return
        snapshot = update_manager.snapshot()
        presentation = _update_presentation(snapshot)
        if presentation != last_update_presentation[0]:
            last_update_presentation[0] = presentation
            _apply_update_presentation(presentation)
        if (
            snapshot.state == UpdateState.READY
            and snapshot.install_requested
            and snapshot.install_action_label
        ):
            update_manager.install_downloaded_update()
        elif (
            snapshot.state == UpdateState.READY
            and not snapshot.install_action_label
            and (
                snapshot.update_package_reveal is None
                or snapshot.update_package_reveal.allows_execution
            )
        ):
            # Only a visible splash performs the one automatic file-manager
            # reveal; downloads completing inside the viewer stay unobtrusive.
            update_manager.reveal_download(automatic=True)
        if snapshot.state == UpdateState.INSTALLING:
            _leave_splash()
            return
        splash_controller.schedule(100, _refresh_update_presentation)

    close_waiting_for_rebuild_pause = [False]
    monitor_check_after_id: list[str | None] = [None]
    monitor_configure_binding_id: list[str | None] = [None]

    def _detach_monitor_transition_observer() -> None:
        splash_controller.cancel_scheduled_callback(monitor_check_after_id[0])
        monitor_check_after_id[0] = None
        binding_id = monitor_configure_binding_id[0]
        if binding_id is None:
            return
        monitor_configure_binding_id[0] = None
        try:
            root.unbind("<Configure>", binding_id)
        except Exception:
            pass

    def _finalize_leave_splash() -> None:
        _detach_monitor_transition_observer()
        workflow = map_library_workflow_ref[0]
        if workflow is not None:
            workflow.close()
        splash_controller.close()
        root.withdraw()
        root.quit()

    def _leave_splash() -> None:
        workflow = map_library_workflow_ref[0]
        if (
            workflow is None
            or not workflow.cache_rebuild_controller.active
        ):
            _finalize_leave_splash()
            return
        if close_waiting_for_rebuild_pause[0]:
            return
        if not workflow.request_cache_rebuild_pause():
            _finalize_leave_splash()
            return

        close_waiting_for_rebuild_pause[0] = True
        show_feedback(
            root,
            "Pausing cache rebuild…",
            kind="info",
            duration_ms=None,
            font=_TYPOGRAPHY.body,
        )
        attempts = [0]

        def wait_for_rebuild_pause() -> None:
            if not workflow.cache_rebuild_controller.active:
                _finalize_leave_splash()
                return
            attempts[0] += 1
            if attempts[0] >= _CACHE_REBUILD_CLOSE_PAUSE_ATTEMPTS:
                _LOG.warning(
                    "Timed out waiting for cache rebuild pause; leaving its "
                    "non-daemon child to save or finish safely."
                )
                _finalize_leave_splash()
                return
            try:
                splash_controller.schedule(100, wait_for_rebuild_pause)
            except Exception:
                _finalize_leave_splash()

        splash_controller.schedule(100, wait_for_rebuild_pause)

    # -- map selection and navigation actions --------------------------------------
    def _show_invalid_map_feedback(message: str) -> None:
        show_feedback(
            root,
            f"Unable to open this folder: {message}",
            kind="error",
            duration_ms=ERROR_FEEDBACK_MS,
            font=_TYPOGRAPHY.body,
        )

    def on_open_map_folder() -> None:
        preflight = directory_selection_preflight(
            desktop_services,
            platform_runtime=platform_runtime,
        )
        decision = preflight.decision
        if not decision.allows_execution:
            _show_invalid_map_feedback(decision.explanation)
            return

        last_dir = _load_last_browse_dir()
        try:
            selection = choose_authorized_directory(
                preflight,
                desktop_services,
                title="Open Map Folder",
                initial_dir=last_dir,
                parent=root,
            )
        except DesktopServiceError as exc:
            _LOG.warning("Map folder selection failed: %s", exc)
            _show_invalid_map_feedback(str(exc))
            return
        if selection:
            is_valid, error_message = _validate_selected_map_folder(selection.path)
            if not is_valid:
                _show_invalid_map_feedback(error_message)
                return

            splash_controller.select_folder(selection.path)
            _save_last_browse_dir(selection.path)
            _leave_splash()

    def _open_guided_dive_from_splash(trace_path: str) -> None:
        """Leave splash only after Map Library has preflighted this trace."""
        splash_controller.select_folder(trace_path)
        _save_last_browse_dir(os.path.dirname(trace_path))
        _leave_splash()

    def on_close(_event=None):
        _request_leave_preferences(_leave_splash)

    def _invoke_and_break(callback):
        callback()
        return "break"

    def _bind_activation(widget, callback) -> None:
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            widget.bind(
                sequence,
                lambda _event, cb=callback: _invoke_and_break(cb),
            )

    def _on_preferences_applied(preferences) -> None:
        if on_preferences_saved is not None:
            on_preferences_saved(preferences)
        workflow = map_library_workflow_ref[0]
        if workflow is None:
            return
        from caveviewer.gui.standard_library_maps import (
            default_map_library_install_dir,
        )

        workflow.set_map_library_root_dir(
            default_map_library_install_dir(current_map_library_configuration())
        )

    def _show_map_library_surface() -> None:
        """Reveal the existing Map Library without rebuilding its catalog."""
        _prepare_surface_change("map_library")
        if active_surface[0] != "map_library":
            map_library_surface.tkraise()
            active_surface[0] = "map_library"
        _set_active_navigation("Map Library")
        panel = map_library_panel_ref[0]
        if panel is not None:
            panel.focus_content()

    def _discard_preferences_and_show_map_library() -> None:
        panel = preferences_panel_ref[0]
        if panel is not None:
            panel.discard_changes()
        _show_map_library_surface()

    def _request_leave_preferences(next_action: Callable[[], None]) -> None:
        """Keep navigation from silently throwing away edited Preferences."""
        panel = preferences_panel_ref[0]
        preferences_active = active_surface[0] == "preferences" and panel is not None
        close_action = resolve_preferences_close(
            bool(preferences_active and panel.has_unsaved_changes)
        )
        if close_action is PreferencesCloseAction.LEAVE:
            next_action()
            return

        def _discard_and_continue() -> None:
            panel.discard_changes()
            next_action()

        _show_unsaved_preferences_dialog(
            root,
            px=px,
            dialog_ref=discard_preferences_dialog_ref,
            on_save=panel.apply,
            on_discard=_discard_and_continue,
            on_continue=next_action,
        )

    def _prepare_surface_change(next_surface: str) -> None:
        """Let the outgoing panel discard transient presentation state."""
        if active_surface[0] == next_surface:
            return
        if active_surface[0] == "preferences":
            panel = preferences_panel_ref[0]
            if panel is not None:
                panel.on_hidden()
        elif active_surface[0] == "help":
            panel = help_panel_ref[0]
            if panel is not None:
                panel.on_hidden()

    def _ensure_preferences_panel() -> PreferencesPanel:
        panel = preferences_panel_ref[0]
        if panel is not None:
            return panel

        panel = PreferencesPanel(
            preferences_surface,
            ui_font_family=_UI_FONT_FAMILY,
            desktop_services=desktop_services,
            platform_runtime=platform_runtime,
            typography=_embedded_panel_typography(),
            on_applied=_on_preferences_applied,
            on_cancel=_show_map_library_surface,
            initial_snapshot=(
                resume_state.preferences if resume_state is not None else None
            ),
        )
        preferences_panel_ref[0] = panel
        return panel

    def _show_preferences_surface() -> None:
        panel = _ensure_preferences_panel()
        _prepare_surface_change("preferences")
        if active_surface[0] != "preferences":
            preferences_surface.tkraise()
            active_surface[0] = "preferences"
        _set_active_navigation("Preferences")
        panel.focus_content()

    def _on_preferences_click():
        _show_preferences_surface()

    def _ensure_help_panel() -> HelpPanel:
        panel = help_panel_ref[0]
        if panel is not None:
            return panel
        log_reveal_adapter = (
            platform_runtime.diagnostic_log_reveal_adapter
            if platform_runtime is not None
            else create_diagnostic_log_reveal_adapter(
                desktop_services=desktop_services
            )
        )
        panel = HelpPanel(
            help_surface,
            px=px,
            style=_help_panel_style(),
            sections=keyboard_control_sections(presentation_profile),
            troubleshooting_controller=TroubleshootingLogController(
                directory=application_log_directory(
                    platform_name=(
                        platform_runtime.profile.platform_name
                        if platform_runtime is not None
                        else None
                    )
                ),
                reveal_adapter=log_reveal_adapter,
            ),
        )
        panel.create()
        help_panel_ref[0] = panel
        return panel

    def _show_help_surface() -> None:
        panel = _ensure_help_panel()
        _prepare_surface_change("help")
        if active_surface[0] != "help":
            help_surface.tkraise()
            active_surface[0] = "help"
        _set_active_navigation("Help")
        panel.focus_content()

    def _on_help_click() -> None:
        _request_leave_preferences(_show_help_surface)

    def _open_about_website(url: str) -> None:
        try:
            desktop_services.open_uri(url, parent=root)
        except Exception as exc:
            _LOG.warning("Could not open About website %s: %s", url, exc)
            show_feedback(
                root,
                "Couldn’t open that website.",
                kind="error",
                duration_ms=ERROR_FEEDBACK_MS,
                font=_TYPOGRAPHY.body,
                max_wraplength=420,
            )

    def _ensure_about_surface():
        if about_surface_initialized[0]:
            return
        _build_themed_about_content(
            about_surface,
            program_name=program_name,
            version=version,
            px=px,
            on_close=_show_map_library_surface,
            on_open_website=_open_about_website,
            center_vertically=True,
            show_close=False,
        )
        about_surface_initialized[0] = True

    def _show_about_surface() -> None:
        _ensure_about_surface()
        _prepare_surface_change("about")
        if active_surface[0] != "about":
            about_surface.tkraise()
            active_surface[0] = "about"
        _set_active_navigation("About")

    def _on_about_click() -> None:
        _request_leave_preferences(_show_about_surface)

    def _focus_map_library() -> None:
        _request_leave_preferences(_show_map_library_surface)

    def _create_navigation_icon(parent, icon_name: str):
        """Create a small, scalable outline icon for one navigation row."""
        size = px(24)
        icon = tk.Canvas(
            parent,
            width=size,
            height=size,
            bg=_BG_COLOR,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )

        def redraw(background: str, foreground: str) -> None:
            icon.configure(bg=background)
            icon.delete("navigation-icon")
            stroke = max(1, px(1.5))
            center = size / 2
            paths: tuple[VectorPath, ...] = ()
            polygons: tuple[VectorPolygon, ...] = ()
            ellipses: tuple[VectorEllipse, ...] = ()

            if icon_name == "map":
                paths = (
                    VectorPath(
                        points=(
                            (px(3), px(5)),
                            (px(9), px(3)),
                            (px(15), px(5)),
                            (px(21), px(3)),
                            (px(21), px(19)),
                            (px(15), px(22)),
                            (px(9), px(19)),
                            (px(3), px(22)),
                        ),
                        color=foreground,
                        width=stroke,
                        closed=True,
                    ),
                    VectorPath(
                        points=((px(9), px(3)), (px(9), px(19))),
                        color=foreground,
                        width=stroke,
                    ),
                    VectorPath(
                        points=((px(15), px(5)), (px(15), px(22))),
                        color=foreground,
                        width=stroke,
                    ),
                )
            elif icon_name == "preferences":
                polygons = (
                    VectorPolygon(
                        points=_navigation_gear_points(center, px),
                        outline_color=foreground,
                        outline_width=stroke,
                    ),
                )
                ellipses = (
                    VectorEllipse(
                        bounds=(
                            center - px(3),
                            center - px(3),
                            center + px(3),
                            center + px(3),
                        ),
                        outline_color=foreground,
                        outline_width=stroke,
                    ),
                )
            elif icon_name == "help":
                ellipses = (
                    VectorEllipse(
                        bounds=(px(2), px(2), px(22), px(22)),
                        outline_color=foreground,
                        outline_width=stroke,
                    ),
                )
            else:
                ellipses = (
                    VectorEllipse(
                        bounds=(px(2), px(2), px(22), px(22)),
                        outline_color=foreground,
                        outline_width=stroke,
                    ),
                )
            icon_photo = vector_icon_photo(
                icon,
                image_size=(size, size),
                paths=paths,
                polygons=polygons,
                ellipses=ellipses,
            )
            icon._cv_navigation_icon_photo = icon_photo
            icon.create_image(
                center,
                center,
                image=icon_photo,
                tags="navigation-icon",
            )
            if icon_name == "help":
                icon.create_text(
                    center,
                    center,
                    text="?",
                    font=_TYPOGRAPHY.body_strong,
                    fill=foreground,
                    tags="navigation-icon",
                )
            elif icon_name != "map" and icon_name != "preferences":
                icon.create_text(
                    center,
                    center,
                    text="i",
                    font=_TYPOGRAPHY.body_strong,
                    fill=foreground,
                    tags="navigation-icon",
                )

        icon._cv_set_appearance = redraw
        return icon

    def _create_navigation_item(
        text: str,
        callback,
        *,
        icon_name: str,
        selected: bool = False,
    ):
        """Create one keyboard-accessible action in the persistent nav rail."""
        item_row = tk.Frame(navigation_frame, bg=_BG_COLOR)
        icon = _create_navigation_icon(item_row, icon_name)
        icon.pack(side="left", padx=(px(11), px(6)))
        item = tk.Label(
            item_row,
            text=text,
            font=_TYPOGRAPHY.body_strong if selected else _TYPOGRAPHY.body,
            fg=_TITLE_COLOR if selected else _SUBTITLE_COLOR,
            bg=_BG_COLOR,
            anchor="w",
            padx=0,
            pady=px(7),
            takefocus=True,
            highlightthickness=1,
            highlightbackground=_BG_COLOR,
            highlightcolor=_BUTTON_BORDER_COLOR,
        )
        item.pack(side="left", fill="both", expand=True, padx=(0, px(9)))
        state = {
            "selected": selected,
            "hovered": False,
            "focused": False,
        }

        def refresh_visual() -> None:
            active = state["hovered"] or state["focused"]
            background = _NAVIGATION_HOVER_BG if active else _BG_COLOR
            item_row.config(bg=background)
            item.config(
                bg=background,
                fg=(
                    _TITLE_COLOR
                    if state["selected"] or active
                    else _SUBTITLE_COLOR
                ),
                font=(
                    _TYPOGRAPHY.body_strong
                    if state["selected"]
                    else _TYPOGRAPHY.body
                ),
                highlightbackground=background,
            )
            icon._cv_set_appearance(
                background,
                _TITLE_COLOR if state["selected"] or active else _SUBTITLE_COLOR,
            )

        def set_selected(is_selected: bool) -> None:
            state["selected"] = is_selected
            refresh_visual()

        def on_enter(_event) -> None:
            state["hovered"] = True
            refresh_visual()

        def on_leave(_event) -> None:
            state["hovered"] = False
            refresh_visual()

        def on_focus_in(_event) -> None:
            state["focused"] = True
            refresh_visual()

        def on_focus_out(_event) -> None:
            state["focused"] = False
            refresh_visual()

        _bind_activation(item, callback)
        _bind_activation(icon, callback)
        item.bind("<Enter>", on_enter)
        item.bind("<Leave>", on_leave)
        item.bind("<FocusIn>", on_focus_in)
        item.bind("<FocusOut>", on_focus_out)
        icon.bind("<Enter>", on_enter)
        icon.bind("<Leave>", on_leave)
        refresh_visual()
        item_row.pack(fill="x", pady=(0, px(_NAVIGATION_ITEM_GAP)))
        item._cv_set_selected = set_selected
        return item

    navigation_items: dict[str, object] = {}
    map_library_navigation_item = _create_navigation_item(
        "Map Library",
        _focus_map_library,
        icon_name="map",
        selected=True,
    )
    navigation_items["Map Library"] = map_library_navigation_item
    preferences_navigation_item = _create_navigation_item(
        "Preferences",
        _on_preferences_click,
        icon_name="preferences",
    )
    navigation_items["Preferences"] = preferences_navigation_item
    help_navigation_item = _create_navigation_item(
        "Help",
        _on_help_click,
        icon_name="help",
    )
    navigation_items["Help"] = help_navigation_item
    about_navigation_item = _create_navigation_item(
        "About",
        _on_about_click,
        icon_name="about",
    )
    navigation_items["About"] = about_navigation_item

    def _set_active_navigation(active_name: str) -> None:
        for name, item in navigation_items.items():
            item._cv_set_selected(name == active_name)

    def _widget_exists(widget) -> bool:
        if widget is None:
            return False
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def _splash_exists() -> bool:
        return not splash_controller.closing and _widget_exists(root)

    def _splash_is_foreground() -> bool:
        """Return whether the splash is already presenting inline feedback."""
        if not _splash_exists():
            return False
        try:
            return root.focus_displayof() is not None
        except tk.TclError:
            return False

    def _open_library_map_from_splash(path: str) -> None:
        is_valid, error_message = _validate_selected_map_folder(path)
        if not is_valid:
            _show_invalid_map_feedback(error_message)
            return

        splash_controller.select_folder(path)
        _save_last_browse_dir(path)
        _leave_splash()

    def _open_cave_metadata_source(url: str) -> None:
        """Open a user-selected, catalog-validated cave reference in the browser."""
        try:
            desktop_services.open_uri(url, parent=root)
        except Exception as exc:
            _LOG.warning("Could not open cave metadata source %s: %s", url, exc)
            show_feedback(
                root,
                "Couldn’t open that source.",
                kind="error",
                duration_ms=ERROR_FEEDBACK_MS,
                font=_TYPOGRAPHY.body,
                max_wraplength=420,
            )

    def _show_cave_metadata(cave: CaveMetadata) -> None:
        """Replace the right surface with one cave's descriptive information."""
        active_cave[0] = cave
        _prepare_surface_change("cave_metadata")
        for child in cave_metadata_surface.winfo_children():
            child.destroy()
        panel = CaveMetadataPanel(
            cave_metadata_surface,
            cave=cave,
            px=px,
            bind_activation=_bind_activation,
            style=_cave_metadata_panel_style(),
            on_back=_show_map_library_surface,
            on_open_source=_open_cave_metadata_source,
        )
        panel.create()
        if active_surface[0] != "cave_metadata":
            cave_metadata_surface.tkraise()
            active_surface[0] = "cave_metadata"
        _set_active_navigation("Map Library")
        panel.focus_content()

    try:
        cave_metadata_catalog = load_bundled_cave_metadata_catalog()
    except Exception as exc:
        _LOG.warning("Could not load bundled cave metadata: %s", exc)
        cave_metadata_catalog = None

    from caveviewer.gui.map_library_sources import MapLibraryCatalogService
    from caveviewer.gui.standard_library_maps import (
        GitHubReleaseMapLibrarySource,
        default_map_library_install_dir,
        load_initial_standard_library_catalog,
    )

    map_library_configuration = current_map_library_configuration()
    map_library_root_dir = default_map_library_install_dir(map_library_configuration)
    recent_map_paths = _load_library_recent_map_paths()
    standard_library_maps = load_initial_standard_library_catalog(
        map_library_configuration
    )
    map_library_controller = MapLibraryController(standard_library_maps)
    map_library_panel = MapLibraryPanel(
        root,
        px=px,
        bind_activation=_bind_activation,
        widget_exists=lambda widget: _widget_exists(widget),
        logger=_LOG,
        style=_map_library_panel_style(branding_assets),
        open_map_folder=on_open_map_folder,
    )
    map_library_panel_ref[0] = map_library_panel
    cache_rebuild_controller = CacheRebuildJobController(
        runtime_settings_provider=current_import_runtime_settings,
    )

    def _show_map_library_feedback(
        message: str,
        *,
        kind: str,
        duration_ms: int,
        max_wraplength: int | None = None,
    ) -> None:
        show_feedback(
            root,
            message,
            kind=kind,
            duration_ms=duration_ms,
            font=_TYPOGRAPHY.body,
            max_wraplength=520 if max_wraplength is None else max_wraplength,
        )

    map_library_workflow = MapLibraryWorkflow(
        composition=MapLibraryComposition(
            root=root,
            controller=map_library_controller,
            panel=map_library_panel,
            standard_library_maps=standard_library_maps,
            map_library_root_dir=map_library_root_dir,
            desktop_services=desktop_services,
            platform_runtime=platform_runtime,
            splash_exists=_splash_exists,
            show_feedback=_show_map_library_feedback,
            logger=_LOG,
            map_library_root_dir_provider=lambda: default_map_library_install_dir(
                current_map_library_configuration()
            ),
        ),
        actions=MapLibraryActionDependencies(
            open_map=_open_library_map_from_splash,
            open_guided_dive=_open_guided_dive_from_splash,
            cave_metadata_catalog=cave_metadata_catalog,
            show_cave_metadata=_show_cave_metadata,
        ),
        catalog=MapLibraryCatalogDependencies(
            fetch_catalog=MapLibraryCatalogService(
                (GitHubReleaseMapLibrarySource(map_library_configuration),)
            ).fetch_catalogs,
        ),
        cache_rebuild=MapLibraryCacheRebuildDependencies(
            controller=cache_rebuild_controller,
            splash_is_foreground=_splash_is_foreground,
        ),
    )
    map_library_workflow_ref[0] = map_library_workflow

    def _create_map_library_panel(parent) -> None:
        # The workflow owns catalog/download state transitions; splash only
        # supplies the parent widget and session-level callbacks.
        map_library_workflow.populate_panel(parent, recent_map_paths)

    _create_map_library_panel(map_library_surface)
    map_library_surface.tkraise()
    _advance_launch_progress(0.18)

    # Keep deferred navigation surfaces out of the initial Tk geometry pass.
    # Preferences creates only its active tab on first use and coalesces the
    # resulting geometry work, but it still does not belong on the critical
    # path to the first splash paint.

    map_library_navigation_item.focus_set()
    root.update_idletasks()
    if resume_state is None:
        final_height = max(
            px(_SPLASH_WINDOW_MIN_HEIGHT),
            root.winfo_reqheight() + px(_SPLASH_WINDOW_EXTRA_BOTTOM_SLACK),
        )
        final_height = min(final_height, available_height)
        pos_y = (screen_h - final_height) // 3
    else:
        final_height = window_h
    root.geometry(f"{window_w}x{final_height}+{pos_x}+{pos_y}")

    # Compose Preferences behind the launch surface while every stacked panel
    # already owns its final mapped width. A normal shell verifies every tab
    # before first reveal so fixed form content does not begin inside a
    # needlessly scrollable viewport.
    preferences_panel = _ensure_preferences_panel()
    if resume_state is not None:
        if resume_state.active_surface == "preferences":
            _show_preferences_surface()
        elif resume_state.active_surface == "help":
            _show_help_surface()
        elif resume_state.active_surface == "about":
            _show_about_surface()
        elif (
            resume_state.active_surface == "cave_metadata"
            and resume_state.cave is not None
        ):
            _show_cave_metadata(resume_state.cave)
        else:
            _show_map_library_surface()
        if resume_state.map_scroll_fraction > 0.0:
            splash_controller.schedule_idle(
                lambda: map_library_panel.restore_scroll_fraction(
                    resume_state.map_scroll_fraction
                )
            )
    _advance_launch_progress(0.30)
    if resume_state is not None and resume_state.window_state != "zoomed":
        # A withdrawn retained root keeps child sizes from its source monitor.
        # Map it behind an app-owned cover at the destination so Windows and Tk
        # establish the new DPI-aware client geometry before Preferences is
        # measured. Alpha prevents even the cover from flashing when supported.
        recomposition_cover = tk.Frame(root, bg=_BG_COLOR)
        recomposition_cover.grid(row=0, column=0, sticky="nsew")
        recomposition_cover.tkraise()
        try:
            root.attributes("-alpha", 0.0)
            recomposition_alpha_hidden = True
        except Exception:
            pass
        root.deiconify()
    _settle_launch_layout(root, passes=3)
    if resume_state is None or resume_state.window_state != "zoomed":
        intended_surface_key = active_surface[0]
        initial_fit_height = final_height
        initial_actual_height = root.winfo_height()

        # Tk only supplies the final configured width and wrapping geometry to
        # the raised stacked surface. Stage Preferences behind the launch
        # overlay (or on the still-withdrawn recovery root) while fitting, then
        # restore the user's intended surface before the shell is revealed.
        preferences_surface.tkraise()
        preferences_panel.on_shown()
        _settle_launch_layout(root, passes=3)

        def _measure_preferences_viewport() -> tuple[int, int, int]:
            _settle_launch_layout(root, passes=1)
            return (
                root.winfo_height(),
                preferences_panel.page_canvas.winfo_height(),
                preferences_panel.measure_preferred_page_height(),
            )

        def _apply_preferences_shell_height(next_height: int) -> None:
            nonlocal pos_y
            if resume_state is None:
                pos_y = (screen_h - next_height) // 3
            root.geometry(f"{window_w}x{next_height}+{pos_x}+{pos_y}")
            _settle_launch_layout(root, passes=3)

        final_height = _fit_shell_height_to_preferences(
            shell_height=final_height,
            minimum_height=px(_SPLASH_WINDOW_MIN_HEIGHT),
            available_height=available_height,
            measure=_measure_preferences_viewport,
            apply_height=_apply_preferences_shell_height,
        )
        (
            final_actual_height,
            final_viewport_height,
            final_preferences_height,
        ) = _measure_preferences_viewport()
        final_measurement_valid = (
            final_actual_height > 0
            and final_viewport_height > 0
            and final_viewport_height <= final_actual_height
        )
        preferences_overflow = max(
            0,
            final_preferences_height - final_viewport_height,
        )
        _LOG.debug(
            "Preferences shell fit: requested_initial_height=%s, "
            "actual_initial_height=%s, requested_final_height=%s, "
            "actual_final_height=%s, "
            "viewport_height=%s, content_height=%s, available_height=%s, "
            "overflow=%s, measurement_valid=%s, work_area_clamped=%s.",
            initial_fit_height,
            initial_actual_height,
            final_height,
            final_actual_height,
            final_viewport_height,
            final_preferences_height,
            available_height,
            preferences_overflow,
            final_measurement_valid,
            preferences_overflow > 1 and final_height >= available_height,
        )
        stacked_surfaces.get(
            intended_surface_key,
            map_library_surface,
        ).tkraise()
        _settle_launch_layout(root, passes=1)
    settled_normal_geometry = _SettledNormalWindowGeometry(
        TkWindowGeometry(
            width=window_w,
            height=final_height,
            x=pos_x,
            y=pos_y,
        )
    )
    readiness_gate.mark_ready()

    map_open_error_presented = [False]

    def _reveal_composed_main_surface() -> None:
        """Reveal the fully painted main surface in one non-repeating handoff."""
        if not readiness_gate.ready:
            return
        remaining_ms = readiness_gate.remaining_delay_ms(time.monotonic())
        if remaining_ms:
            # Tk should not dispatch an ``after`` callback early, but guard the
            # boundary so coarse platform clocks cannot bypass the 3 s policy.
            splash_controller.schedule(
                remaining_ms,
                _reveal_composed_main_surface,
            )
            return
        content_frame.tkraise()
        if launch_surface is not None:
            launch_surface.destroy()
            mark_startup_splash_visible()
        if recomposition_cover is not None:
            recomposition_cover.destroy()
        root.deiconify()
        if recomposition_alpha_hidden:
            try:
                root.attributes("-alpha", 1.0)
            except Exception:
                pass
        if resume_state is not None and resume_state.window_state == "zoomed":
            try:
                root.state("zoomed")
            except Exception:
                pass
        root.lift()
        root.focus_force()
        if (
            not show_launch_overlay
            and _returning_library_needs_topmost(presentation_profile)
        ):
            # macOS does not reliably transfer focus from the just-closed
            # native viewer. On Windows, toggling this native frame state
            # creates a visible flash, so its regular activation path is used.
            root.attributes("-topmost", True)
            splash_controller.schedule(
                200, lambda: root.attributes("-topmost", False)
            )
        if map_open_error_details and not map_open_error_presented[0]:
            # The library must be mapped before its owned modal starts a nested
            # Tk wait loop, otherwise the recovered application appears absent.
            map_open_error_presented[0] = True
            root.update_idletasks()
            from caveviewer.gui.modal_dialog import show_copyable_error

            show_copyable_error(
                root,
                title="Couldn’t open map",
                message="CaveViewer could not open this map due to an error.",
                details=map_open_error_details,
            )

    def _animate_launch_progress() -> None:
        """Advance the visible bar smoothly until readiness permits handoff."""
        if launch_indicator is None or splash_controller.closing:
            return
        now = time.monotonic()
        progress = readiness_gate.visual_progress(now)
        launch_indicator._cv_launch_progress = progress
        _render_launch_content(launch_indicator, progress=progress, px=px)
        if readiness_gate.can_reveal(now):
            # Leave the completed frame visible briefly instead of replacing it
            # in the same event-loop turn that first paints 100 percent.
            splash_controller.schedule(50, _reveal_composed_main_surface)
            return
        splash_controller.schedule(
            _LAUNCH_PROGRESS_INTERVAL_MS,
            _animate_launch_progress,
        )

    if show_launch_overlay:
        splash_controller.schedule(
            _LAUNCH_PROGRESS_INTERVAL_MS,
            _animate_launch_progress,
        )
    else:
        # Returning from a native viewer already occurs after the replacement
        # shell is fully composed. Reveal it before entering Tk's mainloop;
        # deferring the first map of a withdrawn root can leave Windows with no
        # visible application window after the viewer closes.
        _reveal_composed_main_surface()
    # The app-owned manager survives this Tk window and any intervening viewer.
    # Polling immutable snapshots keeps every widget mutation on the Tk thread.
    splash_controller.schedule(50, _refresh_update_presentation)
    if resume_state is None:
        splash_controller.schedule(350, update_manager.check_for_updates)

    def _handle_root_return(_event=None):
        if active_surface[0] == "preferences":
            panel = preferences_panel_ref[0]
            if panel is not None:
                panel.apply()
            return "break"
        if active_surface[0] in {"about", "help"}:
            _show_map_library_surface()
            return "break"
        on_open_map_folder()
        return "break"

    def _cancel_preferences_or_close(_event=None):
        if active_surface[0] == "preferences":
            _request_leave_preferences(_discard_preferences_and_show_map_library)
            return "break"
        if active_surface[0] in {"about", "help"}:
            _show_map_library_surface()
            return "break"
        on_close()
        return "break"

    def _monitor_recomposition_is_deferred() -> bool:
        try:
            if root.grab_current() is not None:
                return True
        except Exception:
            pass
        workflow = map_library_workflow_ref[0]
        if workflow is None:
            return False
        if workflow.cache_rebuild_controller.active:
            return True
        active_download = getattr(workflow.controller, "active_download", None)
        return bool(getattr(active_download, "in_progress", False))

    def _check_monitor_transition() -> None:
        monitor_check_after_id[0] = None
        if splash_controller.closing or recompose_request[0] is not None:
            return
        try:
            if not root.winfo_ismapped():
                return
        except Exception:
            return
        if _monitor_recomposition_is_deferred():
            monitor_check_after_id[0] = splash_controller.schedule(
                200,
                _check_monitor_transition,
            )
            return
        candidate = resolve_tk_display_metrics(
            root,
            presentation_profile=presentation_profile,
            presentation_actions_adapter=presentation_actions_adapter,
            scale_override=(
                viewer_settings.tk_scale if viewer_settings is not None else None
            ),
        )
        observed_geometry = TkWindowGeometry(
            width=max(1, root.winfo_width()),
            height=max(1, root.winfo_height()),
            x=root.winfo_x(),
            y=root.winfo_y(),
        )
        try:
            current_window_state = str(root.state())
        except Exception:
            current_window_state = "normal"
        if not display_scale_changed(display_metrics, candidate):
            settled_normal_geometry.observe(
                observed_geometry,
                window_state=current_window_state,
            )
            return
        source_geometry = settled_normal_geometry.geometry
        scaled_geometry = scale_window_geometry(
            source_geometry,
            current_scale=display_metrics.layout_scale,
            candidate_scale=candidate.layout_scale,
            minimum_size=(
                int(round(_SPLASH_RESIZE_MIN_WIDTH * candidate.layout_scale)),
                int(round(_SPLASH_RESIZE_MIN_HEIGHT * candidate.layout_scale)),
            ),
            preferred_size=(
                int(round(_SPLASH_WINDOW_WIDTH * candidate.layout_scale)),
                int(round(_SPLASH_WINDOW_MIN_HEIGHT * candidate.layout_scale)),
            ),
            work_area=candidate.work_area,
            destination_position=(observed_geometry.x, observed_geometry.y),
        )
        preferences_panel = preferences_panel_ref[0]
        recompose_request[0] = _SplashRecomposeRequest(
            _SplashResumeState(
                geometry=scaled_geometry,
                active_surface=active_surface[0],
                preferences=(
                    preferences_panel.snapshot()
                    if preferences_panel is not None
                    else None
                ),
                map_scroll_fraction=map_library_panel.scroll_fraction(),
                cave=active_cave[0],
                display_metrics=candidate,
                window_state=current_window_state,
            )
        )
        _LOG.info(
            "Tk monitor transition: monitor=%s->%s, layout_scale=%.4f->%.4f, "
            "source_window=%sx%s, observed_destination_window=%sx%s, "
            "final_window=%sx%s, state=%s.",
            display_metrics.monitor_id,
            candidate.monitor_id,
            display_metrics.layout_scale,
            candidate.layout_scale,
            source_geometry.width,
            source_geometry.height,
            observed_geometry.width,
            observed_geometry.height,
            scaled_geometry.width,
            scaled_geometry.height,
            current_window_state,
        )
        _finalize_leave_splash()

    def _schedule_monitor_transition_check(event=None) -> None:
        if presentation_profile.platform_name != "windows":
            return
        if splash_controller.closing or recompose_request[0] is not None:
            return
        if event is not None and getattr(event, "widget", root) is not root:
            return
        splash_controller.cancel_scheduled_callback(monitor_check_after_id[0])
        monitor_check_after_id[0] = splash_controller.schedule(
            180,
            _check_monitor_transition,
        )

    root.bind("<Return>", _handle_root_return)
    root.bind("<Escape>", _cancel_preferences_or_close)
    monitor_configure_binding_id[0] = root.bind(
        "<Configure>",
        _schedule_monitor_transition_check,
        add="+",
    )
    bind_primary_shortcut(
        root,
        "w",
        _cancel_preferences_or_close,
        presentation_profile=presentation_profile,
    )
    root.protocol("WM_DELETE_WINDOW", on_close)

    update_manager.set_foreground_update_surface_active(True)
    try:
        root.mainloop()
    finally:
        splash_controller.cancel_scheduled_callbacks()
        update_manager.set_foreground_update_surface_active(False)

    # Some adapters keep the single Tk app object alive for process-level
    # native menu callbacks. Others destroy the splash root normally.
    if presentation_profile.splash_layout.destroy_root_on_close:
        try:
            root.destroy()
        except Exception:
            pass  # already destroyed, or a background thread beat us to it

    return recompose_request[0] or splash_controller.selected_folder


def _load_last_browse_dir() -> str | None:
    try:
        with open(_last_browse_path_file(), "r", encoding="utf-8") as f:
            path = f.read().strip()
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    return None


def _save_last_browse_dir(path: str) -> None:
    try:
        if not path:
            return
        directory = path if os.path.isdir(path) else os.path.dirname(path)
        if not directory or not os.path.isdir(directory):
            return
        write_text_atomic(_last_browse_path_file(), directory)
    except Exception:
        pass


def _load_library_recent_map_paths() -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for path in load_recent_map_paths():
        if not path:
            continue
        try:
            normalized = os.path.abspath(os.path.expanduser(path))
        except (OSError, TypeError):
            continue
        if normalized in seen or not os.path.isdir(normalized):
            continue
        paths.append(normalized)
        seen.add(normalized)
    return paths
