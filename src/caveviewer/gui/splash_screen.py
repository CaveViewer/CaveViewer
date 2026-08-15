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
continue after this Tk window closes; verified packages are revealed for
manual handling only when a splash is visible.
"""

from __future__ import annotations

import enum
import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from caveviewer.version import APP_NAME, APP_VERSION
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.preferences import (
    apply_preferences_to_env as _apply_preferences_to_env,
    load_preferences as _load_preferences,
)
from caveviewer.gui.preferences_dialog import PreferencesPanel
from caveviewer.gui.dpi_utils import (
    apply_tk_scaling,
    configure_process_dpi_awareness,
    tk_display_scale,
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
from caveviewer.gui.map_library_controller import MapLibraryController
from caveviewer.gui.map_history import load_recent_map_paths
from caveviewer.gui.map_library_panel import (
    MapLibraryPanel,
    MapLibraryPanelStyle,
)
from caveviewer.gui.map_library_workflow import MapLibraryWorkflow
from caveviewer.gui.map_selection import (
    validate_selected_map_folder as _validate_selected_map_folder,
)
from caveviewer.gui.platform.directory_selection import (
    choose_authorized_directory,
    directory_selection_preflight,
)
from caveviewer.gui.platform import (
    DesktopServiceError,
    DesktopServices,
    get_desktop_services,
    get_splash_platform_adapter,
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
from caveviewer.gui.splash_session import SplashSession
from caveviewer.gui.tk_feedback import show_feedback
from caveviewer.gui.tk_shortcuts import bind_primary_shortcut
from caveviewer.gui.tk_theme import DARK_THEME
from caveviewer.gui.tk_typography import TkTypography, create_tk_typography
from caveviewer.gui.update_manager import (
    UpdateManager,
    UpdateSnapshot,
    UpdateState,
)
from caveviewer.resources import image_path

if TYPE_CHECKING:
    from caveviewer.gui.platform.runtime import PlatformRuntime


def _resolve_asset_path(filename: str) -> str | None:
    """Resolve an image from the installed or bundled resource package."""
    path = image_path(filename)
    return str(path) if path.is_file() else None


# Resolve this once at import time -- same asset already used for the
# in-program loading-screen logo, reused here rather than shipping a
# second copy of the same image.
_LOGO_PATH = _resolve_asset_path("app_mark_transparent.png")
_PRESENTATION_PROFILE = get_presentation_profile()
_SPLASH_LAYOUT_POLICY = _PRESENTATION_PROFILE.splash_layout
_APP_ICON_PATH = _resolve_asset_path(_SPLASH_LAYOUT_POLICY.app_icon_resource_name)


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

    macOS keeps the root alive after a viewer launch so the global app menu
    stays attached to a valid Tk application.  Reuse that root on the next
    splash cycle instead of creating another Tk root in the same process.
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
_BUTTON_BORDER_COLOR = DARK_THEME.primary_button_border
_BORDER_COLOR = DARK_THEME.border
# Navigation uses a location marker rather than a button treatment.  The
# background shift stays deliberately quiet; the amber rail and stronger label
# carry the selected-state meaning.
_NAVIGATION_ACTIVE_BG = DARK_THEME.panel
_NAVIGATION_HOVER_BG = DARK_THEME.entry_background
_NAVIGATION_ACTIVE_INDICATOR = DARK_THEME.primary_button
_WINDOWS_SPLASH_LAYOUT = _SPLASH_LAYOUT_POLICY.windows_layout
_LINUX_SPLASH_LAYOUT = _SPLASH_LAYOUT_POLICY.linux_layout
_UI_FONT_FAMILY = _PRESENTATION_PROFILE.ui_font_family
_TK_TEXT_SCALE = 1.0
_CACHE_REBUILD_CLOSE_PAUSE_ATTEMPTS = 25
_UPDATE_READY_ACTION_DELAY_MS = 3_000

_TYPOGRAPHY: TkTypography = create_tk_typography(
    _UI_FONT_FAMILY,
    text_scale=_TK_TEXT_SCALE,
)
_SPLASH_WINDOW_WIDTH = _SPLASH_LAYOUT_POLICY.window_width
_SPLASH_WINDOW_MIN_HEIGHT = _SPLASH_LAYOUT_POLICY.min_height
_SPLASH_WINDOW_EXTRA_BOTTOM_SLACK = _SPLASH_LAYOUT_POLICY.extra_bottom_slack
_CREDITS_TEXT = (
    "Concept by Brian Deatherage and Zsolt Szabo of\n"
    "BottomLine Projects Scientific Dive Team.\n"
    "Engineering and design by magic mr_v.\n\n"
    "Licensed under the GNU General Public License v3.0.\n")
_CAVEVIEWER_WEBSITE_URL = "https://www.caveviewer.com"
_BOTTOMLINE_PROJECTS_WEBSITE_URL = "https://www.bottomlineprojects.com"
_ABOUT_WEBSITE_LINKS = (
    ("www.caveviewer.com", _CAVEVIEWER_WEBSITE_URL),
    ("www.bottomlineprojects.com", _BOTTOMLINE_PROJECTS_WEBSITE_URL),
)
_ABOUT_CREDITS_WRAP_LENGTH = 430
_LIBRARY_PANEL_BORDER_COLOR = "#1e2028"
_LIBRARY_METADATA_COLOR = "#5a5d68"
_LIBRARY_METADATA_STATUS_COLOR = DARK_THEME.secondary_text
_LIBRARY_METADATA_ERROR_COLOR = DARK_THEME.error_text
_LIBRARY_METADATA_STATUS_DURATION_MS = 2500
_LIBRARY_METADATA_ERROR_DURATION_MS = 7000
# Match cave-loading progress: a subdued empty track fills with the amber
# accent as work completes.
_LIBRARY_PROGRESS_TRACK_COLOR = DARK_THEME.entry_background
_LIBRARY_PROGRESS_FILL_COLOR = DARK_THEME.primary_button
_LIBRARY_ACTION_PROGRESS_RING_DIAMETER = 22
_LIBRARY_ACTION_PROGRESS_RING_STROKE_WIDTH = 2
_LIBRARY_ACTION_STOP_SIZE = 7
_LIBRARY_ACTION_BUTTON_SIZE = 32
_LIBRARY_ACTION_ICON_STROKE_WIDTH = 2
_LIBRARY_OVERFLOW_BUTTON_SIZE = 28
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
    return create_presentation_actions_adapter(get_splash_platform_adapter())


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


def _activate_presentation_profile(profile: PresentationProfile) -> None:
    """Apply a runtime profile to legacy splash rendering tokens.

    The splash remains module-oriented for Tk callbacks, but each visible
    instance activates the process-owned immutable profile before it creates
    any widgets. This keeps static presentation choices out of the broad
    platform adapter while preserving the existing callback structure.
    """
    global _PRESENTATION_PROFILE, _SPLASH_LAYOUT_POLICY, _APP_ICON_PATH
    global _WINDOWS_SPLASH_LAYOUT, _LINUX_SPLASH_LAYOUT
    global _UI_FONT_FAMILY, _TK_TEXT_SCALE
    global _SPLASH_WINDOW_WIDTH, _SPLASH_WINDOW_MIN_HEIGHT
    global _SPLASH_WINDOW_EXTRA_BOTTOM_SLACK

    _PRESENTATION_PROFILE = profile
    _SPLASH_LAYOUT_POLICY = profile.splash_layout
    _APP_ICON_PATH = _resolve_asset_path(_SPLASH_LAYOUT_POLICY.app_icon_resource_name)
    _WINDOWS_SPLASH_LAYOUT = _SPLASH_LAYOUT_POLICY.windows_layout
    _LINUX_SPLASH_LAYOUT = _SPLASH_LAYOUT_POLICY.linux_layout
    _UI_FONT_FAMILY = profile.ui_font_family
    _TK_TEXT_SCALE = 1.0
    _SPLASH_WINDOW_WIDTH = _SPLASH_LAYOUT_POLICY.window_width
    _SPLASH_WINDOW_MIN_HEIGHT = _SPLASH_LAYOUT_POLICY.min_height
    _SPLASH_WINDOW_EXTRA_BOTTOM_SLACK = _SPLASH_LAYOUT_POLICY.extra_bottom_slack
    _refresh_tk_font_tokens()


def _configure_runtime_tk_fonts(
    root,
    *,
    presentation_profile: PresentationProfile | None = None,
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

    _TK_TEXT_SCALE = profile.tk_text_scale(default_font_points)
    _refresh_tk_font_tokens()


def _map_library_panel_style() -> MapLibraryPanelStyle:
    """Return the splash-owned style tokens for the Map Library panel."""
    return MapLibraryPanelStyle(
        panel_color=_PANEL_COLOR,
        panel_border_color=_LIBRARY_PANEL_BORDER_COLOR,
        title_color=_TITLE_COLOR,
        former_map_title_color=_LIBRARY_FORMER_MAP_TITLE_COLOR,
        instruction_color=_INSTRUCTION_COLOR,
        title_font=_TYPOGRAPHY.body_strong,
        body_font=_TYPOGRAPHY.body,
        supporting_font=_TYPOGRAPHY.supporting,
        section_font=_TYPOGRAPHY.section,
        button_bg=_PANEL_COLOR,
        button_fg=_BUTTON_BG,
        button_hover_bg=DARK_THEME.secondary_button,
        button_border_color=_BUTTON_BORDER_COLOR,
        disabled_button_bg=_PANEL_COLOR,
        disabled_button_fg=DARK_THEME.placeholder_text,
        disabled_button_border=DARK_THEME.entry_border,
        empty_note_color="#5f606b",
        metadata_color=_LIBRARY_METADATA_COLOR,
        metadata_error_color=_LIBRARY_METADATA_ERROR_COLOR,
        metadata_status_color=_LIBRARY_METADATA_STATUS_COLOR,
        metadata_status_duration_ms=_LIBRARY_METADATA_STATUS_DURATION_MS,
        metadata_error_duration_ms=_LIBRARY_METADATA_ERROR_DURATION_MS,
        progress_track_color=_LIBRARY_PROGRESS_TRACK_COLOR,
        progress_fill_color=_LIBRARY_PROGRESS_FILL_COLOR,
        action_progress_ring_diameter=_LIBRARY_ACTION_PROGRESS_RING_DIAMETER,
        action_progress_ring_stroke_width=_LIBRARY_ACTION_PROGRESS_RING_STROKE_WIDTH,
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


def _help_panel_style() -> HelpPanelStyle:
    """Return the splash-owned style tokens for the quiet Keys table."""
    return HelpPanelStyle(
        background_color=_BG_COLOR,
        tab_active_color=_BUTTON_BG,
        tab_focus_color=DARK_THEME.entry_focus_border,
        section_color=DARK_THEME.secondary_text,
        keycap_background_color=DARK_THEME.entry_background,
        keycap_border_color=DARK_THEME.secondary_button_border,
        keycap_text_color=DARK_THEME.body_text,
        action_color=DARK_THEME.body_text,
        row_divider_color=_LIBRARY_PANEL_BORDER_COLOR,
        content_pad_x=_PRESENTATION_PROFILE.preferences_dialog_layout.body_pad_x,
        tab_font=_TYPOGRAPHY.body_strong,
        section_font=_TYPOGRAPHY.section,
        keycap_font=_TYPOGRAPHY.body_strong,
        action_font=_TYPOGRAPHY.body,
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

    logo_photo = None
    if _LOGO_PATH:
        try:
            from PIL import Image, ImageTk

            logo_img = Image.open(_LOGO_PATH)
            max_logo_dim = px(92)
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
            logo_photo = ImageTk.PhotoImage(
                logo_img,
                master=parent.winfo_toplevel(),
            )
        except Exception as exc:
            _LOG.warning("Could not load About presentation logo: %s", exc)

    if logo_photo is not None:
        logo_label = tk.Label(
            content,
            image=logo_photo,
            bg=_BG_COLOR,
            borderwidth=0,
        )
        logo_label.image = logo_photo
        logo_label.pack(pady=(0, px(10)))

    tk.Label(
        content,
        text=program_name,
        font=_TYPOGRAPHY.heading,
        fg=_TITLE_COLOR,
        bg=_BG_COLOR,
    ).pack()
    tk.Label(
        content,
        text=f"Version {version}",
        font=_TYPOGRAPHY.supporting,
        fg=_SUBTITLE_COLOR,
        bg=_BG_COLOR,
    ).pack(pady=(px(2), px(18)))

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
            cursor="hand2" if on_open_website is not None else "arrow",
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
            cursor="hand2",
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


def _show_discard_preferences_dialog(
    root,
    *,
    px,
    dialog_ref: list[object | None],
    on_discard: Callable[[], None],
) -> None:
    """Ask before a navigation action discards an embedded form's edits."""
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
    dialog.title("Discard unsaved preferences?")
    dialog.configure(bg=_BG_COLOR)
    dialog.resizable(False, False)
    dialog.transient(root)
    _set_tk_window_icon(dialog)

    content = tk.Frame(dialog, bg=_BG_COLOR)
    content.pack(fill="both", expand=True, padx=px(28), pady=px(24))
    tk.Label(
        content,
        text="Discard unsaved changes?",
        font=_TYPOGRAPHY.body_strong,
        fg=_TITLE_COLOR,
        bg=_BG_COLOR,
        anchor="w",
    ).pack(fill="x")
    tk.Label(
        content,
        text=(
            "Your changes to Preferences have not been applied. "
            "Discard them and return to the Map Library?"
        ),
        font=_TYPOGRAPHY.body,
        fg=_SUBTITLE_COLOR,
        bg=_BG_COLOR,
        justify="left",
        anchor="w",
        wraplength=px(360),
    ).pack(fill="x", pady=(px(8), px(20)))

    button_row = tk.Frame(content, bg=_BG_COLOR)
    button_row.pack(fill="x")

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
            cursor="hand2",
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

    discard_button = _make_button("Discard changes", _discard, primary=True)
    keep_button = _make_button("Keep editing", _close_dialog, primary=False)
    discard_button.pack(side="right")
    keep_button.pack(side="right", padx=(0, px(8)))

    dialog.bind("<Escape>", _close_dialog)
    dialog.protocol("WM_DELETE_WINDOW", _close_dialog)
    dialog.update_idletasks()
    dialog_width = max(px(430), dialog.winfo_reqwidth())
    dialog_height = max(px(220), dialog.winfo_reqheight())
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
        keep_button.focus_set()
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
    RETRY = "retry"
    REVEAL = "reveal"


@dataclass(frozen=True)
class _UpdatePresentation:
    status_text: str = ""
    action_text: str = ""
    action: _UpdateAction | None = None
    status_action: _UpdateAction | None = None
    action_replaces_status_after_delay: bool = False
    progress_visible: bool = False
    progress_fraction: float = 0.0
    error: bool = False


def _update_presentation(snapshot: UpdateSnapshot) -> _UpdatePresentation:
    """Map manager states to the exact compact labels rendered by the splash."""
    if (
        snapshot.automatic_update is not None
        and not snapshot.automatic_update.allows_execution
    ):
        return _UpdatePresentation(status_text=snapshot.automatic_update.explanation)
    if snapshot.state == UpdateState.AVAILABLE:
        return _UpdatePresentation(
            action_text="Download update",
            action=_UpdateAction.DOWNLOAD,
        )
    if snapshot.state == UpdateState.DOWNLOADING:
        return _UpdatePresentation(
            status_text=f"Downloading… {snapshot.progress_percent}%",
            progress_visible=True,
            progress_fraction=snapshot.progress_percent / 100.0,
        )
    if snapshot.state == UpdateState.VERIFYING:
        return _UpdatePresentation(
            status_text="Verifying…",
            progress_visible=True,
            progress_fraction=1.0,
        )
    if snapshot.state == UpdateState.READY:
        if (
            snapshot.update_package_reveal is not None
            and not snapshot.update_package_reveal.allows_execution
        ):
            return _UpdatePresentation(
                status_text=snapshot.update_package_reveal.explanation
            )
        return _UpdatePresentation(
            status_text="Update ready",
            action_text="Show update",
            action=_UpdateAction.REVEAL,
            action_replaces_status_after_delay=True,
        )
    if snapshot.state == UpdateState.FAILED:
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
) -> str | None:
    """
    Shows the launch splash screen and blocks until the person either
    picks a folder (Browse -> select a folder -> OK) or closes the
    window. Returns the selected folder path, or None if the window was closed
    without picking one. Update work belongs to app.py and may outlive this
    particular splash instance.
    """
    import tkinter as tk

    session = SplashSession()
    if desktop_services is None:
        desktop_services = (
            platform_runtime.desktop_services
            if platform_runtime is not None
            else get_desktop_services()
        )
    presentation_profile = _presentation_profile_for_runtime(platform_runtime)
    presentation_actions_adapter = _presentation_actions_adapter_for_runtime(
        platform_runtime
    )
    _activate_presentation_profile(presentation_profile)
    _apply_preferences_to_env(_load_preferences())

    configure_process_dpi_awareness(
        presentation_actions_adapter=presentation_actions_adapter
    )
    root = _create_splash_root(
        tk,
        presentation_profile=presentation_profile,
    )
    apply_tk_scaling(root, presentation_profile=presentation_profile)
    _configure_runtime_tk_fonts(
        root,
        presentation_profile=presentation_profile,
    )
    splash_scale = tk_display_scale(root, presentation_profile=presentation_profile)
    if _LINUX_SPLASH_LAYOUT:
        try:
            _LOG.info(
                "Tk display scale: "
                f"{splash_scale:.2f}; tk scaling: {float(root.tk.call('tk', 'scaling')):.2f}"
            )
        except Exception:
            pass

    def px(value: float) -> int:
        return int(round(value * splash_scale))

    # Keep hidden until final geometry is set to avoid a visible corner->center jump.
    root.withdraw()
    root.title(program_name)
    root.configure(bg=_BG_COLOR)
    root.resizable(False, False)
    _set_tk_window_icon(root)

    presentation_actions_adapter.install_about_handler(root, program_name, version)

    window_w, window_h = px(_SPLASH_WINDOW_WIDTH), px(_SPLASH_WINDOW_MIN_HEIGHT)

    # Center the window on screen rather than letting the OS place it
    # arbitrarily -- a first-launch splash screen appearing somewhere
    # random/off-center is a small but noticeable rough edge.
    root.update_idletasks()
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    pos_x = (screen_w - window_w) // 2
    pos_y = (screen_h - window_h) // 3  # slightly above true vertical center, reads better
    root.geometry(f"{window_w}x{window_h}+{pos_x}+{pos_y}")

    content_frame = tk.Frame(root, bg=_BG_COLOR)
    content_frame.pack(fill="both", expand=True, padx=px(22), pady=px(16))

    # The splash is organized as a stable navigation rail beside an active
    # content surface. Keeping the rail a fixed width prevents map-library
    # and Preferences content from jumping as users navigate.
    left_frame = tk.Frame(content_frame, bg=_BG_COLOR, width=px(220))
    left_frame.pack(side="left", fill="y")
    left_frame.pack_propagate(False)

    divider = tk.Frame(content_frame, bg=_BORDER_COLOR, width=1)
    divider.pack(side="left", fill="y", padx=(px(14), px(18)), pady=px(10))

    right_frame = tk.Frame(content_frame, bg=_BG_COLOR)
    right_frame.pack(side="left", fill="both", expand=True)
    map_library_surface = tk.Frame(right_frame, bg=_BG_COLOR)
    preferences_surface = tk.Frame(right_frame, bg=_BG_COLOR)
    help_surface = tk.Frame(right_frame, bg=_BG_COLOR)
    about_surface = tk.Frame(right_frame, bg=_BG_COLOR)
    cave_metadata_surface = tk.Frame(right_frame, bg=_BG_COLOR)

    navigation_frame = tk.Frame(left_frame, bg=_BG_COLOR)
    navigation_frame.pack(fill="x", pady=(px(22), 0))

    app_status_frame = tk.Frame(left_frame, bg=_BG_COLOR)
    app_status_frame.pack(
        side="bottom",
        fill="x",
        padx=px(14),
        pady=(0, px(14)),
    )
    update_progress_width = px(192)

    last_update_presentation: list[_UpdatePresentation | None] = [None]
    map_library_workflow_ref: list[MapLibraryWorkflow | None] = [None]
    map_library_panel_ref: list[MapLibraryPanel | None] = [None]
    preferences_panel_ref: list[PreferencesPanel | None] = [None]
    help_panel_ref: list[HelpPanel | None] = [None]
    about_surface_initialized = [False]
    discard_preferences_dialog_ref: list[object | None] = [None]
    active_surface = ["map_library"]

    # The status frame remains anchored to the lower-left rail. Its version is
    # always visible; the update subsection is introduced only when it has a
    # meaningful state, keeping the quiet state genuinely compact.
    version_label = tk.Label(
        app_status_frame,
        text=f"Version {version}",
        font=_TYPOGRAPHY.supporting,
        fg=_SUBTITLE_COLOR,
        bg=_BG_COLOR,
        anchor="w",
    )
    version_label.pack(anchor="w")

    update_cluster = tk.Frame(app_status_frame, bg=_BG_COLOR)

    update_label = tk.Label(
        update_cluster,
        text="",
        font=_TYPOGRAPHY.supporting,
        fg=_INSTRUCTION_COLOR,
        bg=_BG_COLOR,
        cursor="arrow",
        takefocus=False,
        highlightthickness=1,
        highlightbackground=_BG_COLOR,
        highlightcolor=_BUTTON_BG,
        wraplength=px(192),
        justify="left",
        anchor="w",
    )

    update_action_label = tk.Label(
        update_cluster,
        text="",
        # Footer actions are links to a follow-on update task, not the primary
        # action of the active panel. Keep their hierarchy with the status
        # text; amber color and interaction behavior provide the affordance.
        font=_TYPOGRAPHY.supporting,
        fg=_BUTTON_BG,
        bg=_BG_COLOR,
        cursor="arrow",
        takefocus=False,
        highlightthickness=1,
        highlightbackground=_BG_COLOR,
        highlightcolor=_BUTTON_BG,
        wraplength=px(192),
        justify="left",
        anchor="w",
    )

    update_progress_canvas = tk.Canvas(
        update_cluster,
        width=update_progress_width,
        height=4,
        bg=_BG_COLOR,
        highlightthickness=0,
    )
    _update_progress_bar = update_progress_canvas.create_rectangle(
        0, 0, 0, 4, fill=_BUTTON_BG, width=0
    )

    def _set_update_cluster_visible(visible: bool) -> None:
        if visible:
            if not update_cluster.winfo_manager():
                update_cluster.pack(anchor="w", fill="x", pady=(px(10), 0))
            return
        update_cluster.pack_forget()

    def _layout_update_cluster(presentation: _UpdatePresentation) -> None:
        """Pack only the update controls relevant to the current state."""
        update_label.pack_forget()
        update_action_label.pack_forget()
        update_progress_canvas.pack_forget()

        if presentation.status_text:
            update_label.pack(anchor="w", fill="x")
        if (
            presentation.action_text
            and not presentation.action_replaces_status_after_delay
        ):
            update_action_label.pack(
                anchor="w",
                pady=(px(2) if presentation.status_text else 0, 0),
            )
        if presentation.progress_visible:
            update_progress_canvas.config(bg=DARK_THEME.entry_background)
            update_progress_canvas.pack(anchor="w", pady=(px(5), 0))
        else:
            update_progress_canvas.config(bg=_BG_COLOR)
            update_progress_canvas.coords(_update_progress_bar, 0, 0, 0, 4)

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

    def _set_progress(frac: float):
        clamped = max(0.0, min(1.0, float(frac)))
        update_progress_canvas.coords(
            _update_progress_bar,
            0,
            0,
            int(update_progress_width * clamped),
            4,
        )

    def _invoke_update_action(action: _UpdateAction) -> None:
        if action in {_UpdateAction.DOWNLOAD, _UpdateAction.RETRY}:
            update_manager.start_download()
        elif action == _UpdateAction.REVEAL:
            update_manager.reveal_download()

    def _bind_label_action(label, action: _UpdateAction | None) -> None:
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            label.unbind(sequence)
        enabled = action is not None
        label.config(cursor="hand2" if enabled else "arrow", takefocus=enabled)
        if not enabled:
            return

        def invoke(_event=None):
            _invoke_update_action(action)
            return "break"

        label.bind("<Button-1>", invoke)
        label.bind("<Return>", invoke)
        label.bind("<space>", invoke)

    def _show_delayed_update_action(presentation: _UpdatePresentation) -> None:
        """Replace the completion status with its single follow-up action."""
        if last_update_presentation[0] != presentation:
            return
        label_text, label_color, label_action = _update_status_label(
            presentation,
            show_delayed_action=True,
        )
        update_label.config(text=label_text, fg=label_color)
        _bind_label_action(update_label, label_action)

    def _apply_update_presentation(presentation: _UpdatePresentation) -> None:
        label_text, label_color, label_action = _update_status_label(presentation)
        update_label.config(
            text=label_text,
            fg=label_color,
        )
        update_action_label.config(text=presentation.action_text)
        _bind_label_action(update_label, label_action)
        _bind_label_action(update_action_label, presentation.action)
        _layout_update_cluster(presentation)
        _set_progress(presentation.progress_fraction)
        if presentation.action_replaces_status_after_delay:
            session.schedule_after(
                root,
                _UPDATE_READY_ACTION_DELAY_MS,
                lambda: _show_delayed_update_action(presentation),
            )

    def _refresh_update_presentation() -> None:
        if session.closing:
            return
        snapshot = update_manager.snapshot()
        presentation = _update_presentation(snapshot)
        if presentation != last_update_presentation[0]:
            last_update_presentation[0] = presentation
            _apply_update_presentation(presentation)
        if (
            snapshot.state == UpdateState.READY
            and (
                snapshot.update_package_reveal is None
                or snapshot.update_package_reveal.allows_execution
            )
        ):
            # Only a visible splash performs the one automatic file-manager
            # reveal; downloads completing inside the viewer stay unobtrusive.
            update_manager.reveal_download(automatic=True)
        session.schedule_after(root, 100, _refresh_update_presentation)

    close_waiting_for_rebuild_pause = [False]

    def _finalize_leave_splash() -> None:
        workflow = map_library_workflow_ref[0]
        if workflow is not None:
            workflow.close()
        session.mark_closing()
        session.cancel_after_callbacks(root)
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
            duration_ms=4000,
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
                root.after(100, wait_for_rebuild_pause)
            except Exception:
                _finalize_leave_splash()

        root.after(100, wait_for_rebuild_pause)

    # -- map selection and navigation actions --------------------------------------
    def _show_invalid_map_feedback(message: str) -> None:
        show_feedback(
            root,
            f"Unable to open this folder: {message}",
            kind="error",
            duration_ms=9000,
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

            session.select_folder(selection.path)
            _save_last_browse_dir(selection.path)
            _leave_splash()

    def _open_guided_dive_from_splash(trace_path: str) -> None:
        """Leave splash only after Map Library has preflighted this trace."""
        session.select_folder(trace_path)
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

    def _on_preferences_applied(_preferences) -> None:
        workflow = map_library_workflow_ref[0]
        if workflow is None:
            return
        from caveviewer.gui.standard_library_maps import (
            default_map_library_install_dir,
        )

        workflow.set_map_library_root_dir(default_map_library_install_dir())

    def _show_map_library_surface() -> None:
        """Reveal the existing Map Library without rebuilding its catalog."""
        if active_surface[0] != "map_library":
            preferences_surface.pack_forget()
            help_surface.pack_forget()
            about_surface.pack_forget()
            cave_metadata_surface.pack_forget()
            map_library_surface.pack(fill="both", expand=True)
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
        if (
            active_surface[0] != "preferences"
            or panel is None
            or not panel.has_unsaved_changes
        ):
            next_action()
            return

        def _discard_and_continue() -> None:
            panel.discard_changes()
            next_action()

        _show_discard_preferences_dialog(
            root,
            px=px,
            dialog_ref=discard_preferences_dialog_ref,
            on_discard=_discard_and_continue,
        )

    def _ensure_preferences_panel() -> PreferencesPanel:
        panel = preferences_panel_ref[0]
        if panel is not None:
            return panel

        panel = PreferencesPanel(
            preferences_surface,
            ui_font_family=_UI_FONT_FAMILY,
            desktop_services=desktop_services,
            platform_runtime=platform_runtime,
            typography=_TYPOGRAPHY,
            on_applied=_on_preferences_applied,
            on_cancel=_show_map_library_surface,
        )
        preferences_panel_ref[0] = panel
        return panel

    def _show_preferences_surface() -> None:
        panel = _ensure_preferences_panel()
        if active_surface[0] != "preferences":
            map_library_surface.pack_forget()
            help_surface.pack_forget()
            about_surface.pack_forget()
            cave_metadata_surface.pack_forget()
            preferences_surface.pack(fill="both", expand=True)
            active_surface[0] = "preferences"
        _set_active_navigation("Preferences")
        panel.focus_content()

    def _on_preferences_click():
        _show_preferences_surface()

    def _ensure_help_panel() -> HelpPanel:
        panel = help_panel_ref[0]
        if panel is not None:
            return panel
        panel = HelpPanel(
            help_surface,
            px=px,
            style=_help_panel_style(),
            sections=keyboard_control_sections(presentation_profile),
        )
        panel.create()
        help_panel_ref[0] = panel
        return panel

    def _show_help_surface() -> None:
        panel = _ensure_help_panel()
        if active_surface[0] != "help":
            map_library_surface.pack_forget()
            preferences_surface.pack_forget()
            about_surface.pack_forget()
            cave_metadata_surface.pack_forget()
            help_surface.pack(fill="both", expand=True)
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
                duration_ms=7000,
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
        if active_surface[0] != "about":
            map_library_surface.pack_forget()
            preferences_surface.pack_forget()
            help_surface.pack_forget()
            cave_metadata_surface.pack_forget()
            about_surface.pack(fill="both", expand=True)
            active_surface[0] = "about"
        _set_active_navigation("About")

    def _on_about_click() -> None:
        _request_leave_preferences(_show_about_surface)

    def _focus_map_library() -> None:
        _request_leave_preferences(_show_map_library_surface)

    def _create_navigation_icon(parent, icon_name: str):
        """Create a small, scalable outline icon for one navigation row."""
        size = px(28)
        icon = tk.Canvas(
            parent,
            width=size,
            height=size,
            bg=_BG_COLOR,
            borderwidth=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=False,
        )

        def redraw(background: str, foreground: str) -> None:
            icon.configure(bg=background)
            icon.delete("navigation-icon")
            stroke = max(1, px(1.6))
            center = size / 2

            def line(*points) -> None:
                icon.create_line(
                    *points,
                    fill=foreground,
                    width=stroke,
                    capstyle="round",
                    joinstyle="round",
                    tags="navigation-icon",
                )

            if icon_name == "map":
                line(
                    px(3),
                    px(6),
                    px(10),
                    px(3),
                    px(18),
                    px(6),
                    px(25),
                    px(3),
                    px(25),
                    px(22),
                    px(18),
                    px(25),
                    px(10),
                    px(22),
                    px(3),
                    px(25),
                    px(3),
                    px(6),
                )
                line(px(10), px(3), px(10), px(22))
                line(px(18), px(6), px(18), px(25))
            elif icon_name == "preferences":
                points = []
                for index in range(16):
                    angle = math.radians(index * 22.5 - 90)
                    radius = px(11 if index % 2 == 0 else 8)
                    points.extend(
                        (
                            center + math.cos(angle) * radius,
                            center + math.sin(angle) * radius,
                        )
                    )
                icon.create_polygon(
                    *points,
                    outline=foreground,
                    fill="",
                    width=stroke,
                    joinstyle="round",
                    tags="navigation-icon",
                )
                icon.create_oval(
                    center - px(3),
                    center - px(3),
                    center + px(3),
                    center + px(3),
                    outline=foreground,
                    width=stroke,
                    tags="navigation-icon",
                )
            elif icon_name == "help":
                icon.create_oval(
                    px(3),
                    px(3),
                    px(25),
                    px(25),
                    outline=foreground,
                    width=stroke,
                    tags="navigation-icon",
                )
                icon.create_text(
                    center,
                    center,
                    text="?",
                    font=_TYPOGRAPHY.body_strong,
                    fill=foreground,
                    tags="navigation-icon",
                )
            else:
                icon.create_oval(
                    px(3),
                    px(3),
                    px(25),
                    px(25),
                    outline=foreground,
                    width=stroke,
                    tags="navigation-icon",
                )
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
        indicator = tk.Frame(
            item_row,
            bg=_NAVIGATION_ACTIVE_INDICATOR if selected else _BG_COLOR,
            width=px(3),
        )
        indicator.pack(side="left", fill="y")
        icon = _create_navigation_icon(item_row, icon_name)
        icon.pack(side="left", padx=(px(10), px(7)))
        item = tk.Label(
            item_row,
            text=text,
            font=_TYPOGRAPHY.body_strong if selected else _TYPOGRAPHY.body,
            fg=_TITLE_COLOR if selected else _SUBTITLE_COLOR,
            bg=_NAVIGATION_ACTIVE_BG if selected else _BG_COLOR,
            anchor="w",
            padx=0,
            pady=px(9),
            cursor="hand2",
            takefocus=True,
            highlightthickness=1,
            highlightbackground=_BG_COLOR,
            highlightcolor=_BUTTON_BORDER_COLOR,
        )
        item.pack(side="left", fill="both", expand=True, padx=(0, px(11)))
        state = {
            "selected": selected,
            "hovered": False,
            "focused": False,
        }

        def refresh_visual() -> None:
            active = state["selected"] or state["hovered"] or state["focused"]
            background = _NAVIGATION_ACTIVE_BG if state["selected"] else (
                _NAVIGATION_HOVER_BG if active else _BG_COLOR
            )
            item_row.config(bg=background)
            indicator.config(
                bg=(
                    _NAVIGATION_ACTIVE_INDICATOR
                    if state["selected"]
                    else background
                )
            )
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
        item_row.pack(fill="x", pady=(0, px(4)))
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
        return not session.closing and _widget_exists(root)

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

        session.select_folder(path)
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
                duration_ms=7000,
                font=_TYPOGRAPHY.body,
                max_wraplength=420,
            )

    def _show_cave_metadata(cave: CaveMetadata) -> None:
        """Replace the right surface with one cave's descriptive information."""
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
            map_library_surface.pack_forget()
            preferences_surface.pack_forget()
            help_surface.pack_forget()
            about_surface.pack_forget()
            cave_metadata_surface.pack(fill="both", expand=True)
            active_surface[0] = "cave_metadata"
        _set_active_navigation("Map Library")
        panel.focus_content()

    try:
        cave_metadata_catalog = load_bundled_cave_metadata_catalog()
    except Exception as exc:
        _LOG.warning("Could not load bundled cave metadata: %s", exc)
        cave_metadata_catalog = None

    from caveviewer.gui.standard_library_maps import (
        default_map_library_install_dir,
        load_initial_standard_library_catalog,
    )

    map_library_root_dir = default_map_library_install_dir()
    recent_map_paths = _load_library_recent_map_paths()
    standard_library_maps = load_initial_standard_library_catalog()
    map_library_controller = MapLibraryController(standard_library_maps)
    map_library_panel = MapLibraryPanel(
        root,
        px=px,
        bind_activation=_bind_activation,
        widget_exists=lambda widget: _widget_exists(widget),
        logger=_LOG,
        style=_map_library_panel_style(),
        open_map_folder=on_open_map_folder,
    )
    map_library_panel_ref[0] = map_library_panel
    cache_rebuild_controller = CacheRebuildJobController()

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
        root=root,
        controller=map_library_controller,
        panel=map_library_panel,
        standard_library_maps=standard_library_maps,
        map_library_root_dir=map_library_root_dir,
        desktop_services=desktop_services,
        platform_runtime=platform_runtime,
        splash_exists=_splash_exists,
        splash_is_foreground=_splash_is_foreground,
        open_map=_open_library_map_from_splash,
        show_feedback=_show_map_library_feedback,
        logger=_LOG,
        map_library_root_dir_provider=default_map_library_install_dir,
        open_guided_dive=_open_guided_dive_from_splash,
        cache_rebuild_controller=cache_rebuild_controller,
        cave_metadata_catalog=cave_metadata_catalog,
        show_cave_metadata=_show_cave_metadata,
    )
    map_library_workflow_ref[0] = map_library_workflow

    def _create_map_library_panel(parent) -> None:
        # The workflow owns catalog/download state transitions; splash only
        # supplies the parent widget and session-level callbacks.
        map_library_workflow.populate_panel(parent, recent_map_paths)

    _create_map_library_panel(map_library_surface)
    map_library_surface.pack(fill="both", expand=True)

    # Measure the complete Preferences form before showing the splash. This
    # gives every right-hand surface one stable window height instead of
    # visibly growing the window when someone selects Preferences later.
    _ensure_preferences_panel()
    map_library_surface.pack_forget()
    preferences_surface.pack(fill="both", expand=True)
    root.update_idletasks()
    preferences_surface_required_height = (
        root.winfo_reqheight() + px(_SPLASH_WINDOW_EXTRA_BOTTOM_SLACK)
    )
    preferences_surface.pack_forget()
    map_library_surface.pack(fill="both", expand=True)

    map_library_navigation_item.focus_set()
    root.update_idletasks()
    final_height = max(
        px(_SPLASH_WINDOW_MIN_HEIGHT),
        root.winfo_reqheight() + px(_SPLASH_WINDOW_EXTRA_BOTTOM_SLACK),
        preferences_surface_required_height,
    )
    max_height = max(px(360), root.winfo_screenheight() - px(80))
    final_height = min(final_height, max_height)
    pos_y = (screen_h - final_height) // 3
    root.geometry(f"{window_w}x{final_height}+{pos_x}+{pos_y}")

    root.deiconify()
    root.lift()
    root.focus_force()
    # Briefly force topmost so the splash appears above the GLFW viewer window
    # that just closed -- on macOS the focus doesn't transfer automatically.
    root.attributes("-topmost", True)
    session.schedule_after(root, 200, lambda: root.attributes("-topmost", False))
    # The app-owned manager survives this Tk window and any intervening viewer.
    # Polling immutable snapshots keeps every widget mutation on the Tk thread.
    session.schedule_after(root, 50, _refresh_update_presentation)
    session.schedule_after(root, 350, update_manager.check_for_updates)
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

    root.bind("<Return>", _handle_root_return)
    root.bind("<Escape>", _cancel_preferences_or_close)
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
        session.cancel_after_callbacks(root)
        update_manager.set_foreground_update_surface_active(False)

    # Some adapters keep the single Tk app object alive for process-level
    # native menu callbacks. Others destroy the splash root normally.
    if presentation_profile.splash_layout.destroy_root_on_close:
        try:
            root.destroy()
        except Exception:
            pass  # already destroyed, or a background thread beat us to it

    return session.selected_folder


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
