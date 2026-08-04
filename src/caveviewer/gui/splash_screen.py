"""Tk startup surface for map selection, preferences, and updates.

The very first thing shown when CaveViewer launches: a small landing
window with the program name/version, the skull logo, and an Open Map Folder
button -- replacing the old behavior of jumping straight into a bare native
folder-picker dialog with zero context about what the program even is.

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
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from caveviewer.version import APP_NAME, APP_VERSION
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.preferences import (
    apply_preferences_to_env as _apply_preferences_to_env,
    load_preferences as _load_preferences,
)
from caveviewer.gui.preferences_dialog import (
    show_preferences_dialog as _show_preferences_dialog,
)
from caveviewer.gui.dpi_utils import (
    apply_tk_scaling,
    configure_process_dpi_awareness,
    tk_display_scale,
)
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
from caveviewer.gui.platform import get_splash_platform_adapter
from caveviewer.gui.platform import (
    DesktopServiceError,
    DesktopServices,
    get_desktop_services,
    tk_root_options,
)
from caveviewer.gui.preference_paths import migrate_state_file, write_text_atomic
from caveviewer.gui.splash_session import SplashSession
from caveviewer.gui.tk_feedback import show_feedback
from caveviewer.gui.tk_shortcuts import bind_primary_shortcut
from caveviewer.gui.tk_theme import DARK_THEME
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
_PLATFORM_ADAPTER = get_splash_platform_adapter()
_SPLASH_LAYOUT_POLICY = _PLATFORM_ADAPTER.splash_layout_policy()
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


def _create_splash_root(tk):
    """
    Return the process Tk root for the splash screen.

    macOS keeps the root alive after a viewer launch so the global app menu
    stays attached to a valid Tk application.  Reuse that root on the next
    splash cycle instead of creating another Tk root in the same process.
    """
    if _SPLASH_LAYOUT_POLICY.reuse_existing_root:
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
_SUBTITLE_COLOR = DARK_THEME.body_text
_INSTRUCTION_COLOR = DARK_THEME.secondary_text
_BUTTON_BG = DARK_THEME.primary_button
_BUTTON_HOVER_BG = DARK_THEME.primary_button_hover
_BUTTON_BORDER_COLOR = DARK_THEME.primary_button_border
_BUTTON_FG = DARK_THEME.primary_button_text
_BORDER_COLOR = DARK_THEME.border
_WINDOWS_SPLASH_LAYOUT = _SPLASH_LAYOUT_POLICY.windows_layout
_LINUX_SPLASH_LAYOUT = _SPLASH_LAYOUT_POLICY.linux_layout
_ROOMY_SPLASH_LAYOUT = _WINDOWS_SPLASH_LAYOUT or _LINUX_SPLASH_LAYOUT
_UI_FONT_FAMILY = _PLATFORM_ADAPTER.ui_font_family()
_TK_TEXT_SCALE = 1.0


def _scaled_tk_font_size(points: float) -> int:
    """Return a runtime-scaled Tk point size for fixed splash font tokens."""
    return max(1, int(round(float(points) * _TK_TEXT_SCALE)))


def _tk_font(points: float, *styles: str) -> tuple:
    """Return a Tk font tuple using the resolved family and runtime text scale."""
    return (_UI_FONT_FAMILY, _scaled_tk_font_size(points), *styles)


_TITLE_FONT = _tk_font(24, "bold")
_VERSION_FONT = _tk_font(12)
_BODY_FONT = _tk_font(12)
_SMALL_FONT = _tk_font(10)
_LIBRARY_SECTION_FONT = _tk_font(10, "bold")
_LIBRARY_METADATA_FONT = _tk_font(9)
_INSTRUCTION_FONT = _tk_font(11) if _ROOMY_SPLASH_LAYOUT else _BODY_FONT
_FOOTER_FONT = _tk_font(9) if _ROOMY_SPLASH_LAYOUT else _SMALL_FONT
_LINK_FONT = _tk_font(10, "underline")
_BUTTON_FONT = _tk_font(13)
_SPLASH_WINDOW_WIDTH = _SPLASH_LAYOUT_POLICY.window_width
_SPLASH_WINDOW_MIN_HEIGHT = _SPLASH_LAYOUT_POLICY.min_height
_SPLASH_WINDOW_EXTRA_BOTTOM_SLACK = _SPLASH_LAYOUT_POLICY.extra_bottom_slack
_SECONDARY_LINK_ROW_BOTTOM_GAP = (
    _SPLASH_LAYOUT_POLICY.secondary_link_row_bottom_gap
)
_FOOTER_CREDITS_BOTTOM_PAD = _SPLASH_LAYOUT_POLICY.footer_credits_bottom_pad
_TITLE_TO_ACTION_GAP = _SPLASH_LAYOUT_POLICY.title_to_action_gap
_BROWSE_BUTTON_BOTTOM_GAP = _SPLASH_LAYOUT_POLICY.browse_button_bottom_gap
_INSTRUCTION_BOTTOM_GAP = _SPLASH_LAYOUT_POLICY.instruction_bottom_gap
_SECONDARY_LINK_ROW_TOP_GAP = _SPLASH_LAYOUT_POLICY.secondary_link_row_top_gap
_CREDITS_TEXT = (
    "Concept by Brian Deatherage and Zsolt Szabo of\n"
    "BottomLine Projects Scientific Dive Team.\n"
    "Engineering and design by magic mr_v.\n\n"
    "Licensed under the GNU General Public License v3.0.\n")
_LIBRARY_SCROLLBAR_WIDTH = 14
_LIBRARY_SCROLL_THUMB_WIDTH = 5
_LIBRARY_SCROLL_THUMB_MIN_HEIGHT = 36
_LIBRARY_SCROLL_THUMB_COLOR = DARK_THEME.secondary_button_border
_LIBRARY_SCROLL_THUMB_ACTIVE_COLOR = DARK_THEME.entry_focus_border
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


def _configure_runtime_tk_fonts(root) -> None:
    """Resolve the UI font against fonts Tk can actually render."""
    global _UI_FONT_FAMILY, _TK_TEXT_SCALE, _TITLE_FONT, _VERSION_FONT, _BODY_FONT
    global _SMALL_FONT, _LIBRARY_SECTION_FONT, _LIBRARY_METADATA_FONT, _INSTRUCTION_FONT
    global _FOOTER_FONT, _LINK_FONT, _BUTTON_FONT

    default_font_points = 12.0
    try:
        import tkinter.font as tkfont

        available = {family.lower(): family for family in tkfont.families(root)}
        preferred = [_PLATFORM_ADAPTER.ui_font_family()]
        if _LINUX_SPLASH_LAYOUT:
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
            linux_layout=_LINUX_SPLASH_LAYOUT,
        )

        if resolved_family:
            _UI_FONT_FAMILY = resolved_family
            if _LINUX_SPLASH_LAYOUT:
                _LOG.info(f"Using Tk UI font family: {_UI_FONT_FAMILY}")
    except Exception as exc:
        _LOG.warning(f"could not resolve Tk UI font family ({exc}); using {_UI_FONT_FAMILY}.")

    _TK_TEXT_SCALE = _PLATFORM_ADAPTER.tk_text_scale(default_font_points)
    _TITLE_FONT = _tk_font(24, "bold")
    _VERSION_FONT = _tk_font(12)
    _BODY_FONT = _tk_font(12)
    _SMALL_FONT = _tk_font(10)
    _LIBRARY_SECTION_FONT = _tk_font(10, "bold")
    _LIBRARY_METADATA_FONT = _tk_font(9)
    _INSTRUCTION_FONT = _tk_font(11) if _ROOMY_SPLASH_LAYOUT else _BODY_FONT
    _FOOTER_FONT = _tk_font(9) if _ROOMY_SPLASH_LAYOUT else _SMALL_FONT
    _LINK_FONT = _tk_font(10, "underline")
    _BUTTON_FONT = _tk_font(13)


def _map_library_panel_style() -> MapLibraryPanelStyle:
    """Return the splash-owned style tokens for the Map Library panel."""
    return MapLibraryPanelStyle(
        panel_color=_PANEL_COLOR,
        panel_border_color=_LIBRARY_PANEL_BORDER_COLOR,
        title_color=_TITLE_COLOR,
        instruction_color=_INSTRUCTION_COLOR,
        section_font=_LIBRARY_SECTION_FONT,
        small_font=_SMALL_FONT,
        metadata_font=_LIBRARY_METADATA_FONT,
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
        scrollbar_width=_LIBRARY_SCROLLBAR_WIDTH,
        scroll_thumb_min_height=_LIBRARY_SCROLL_THUMB_MIN_HEIGHT,
        scroll_thumb_width=_LIBRARY_SCROLL_THUMB_WIDTH,
        scroll_thumb_color=_LIBRARY_SCROLL_THUMB_COLOR,
        scroll_thumb_active_color=_LIBRARY_SCROLL_THUMB_ACTIVE_COLOR,
    )


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
    progress_visible: bool = False
    progress_fraction: float = 0.0
    error: bool = False


def _display_version(version: str | None) -> str:
    text = (version or "").strip()
    return text[1:] if text.lower().startswith("v") else text


def _update_presentation(
    snapshot: UpdateSnapshot,
    reveal_action_label: str,
) -> _UpdatePresentation:
    """Map manager states to the exact compact labels rendered by the splash."""
    if (
        snapshot.automatic_update is not None
        and not snapshot.automatic_update.allows_execution
    ):
        return _UpdatePresentation(status_text=snapshot.automatic_update.explanation)
    if snapshot.state == UpdateState.AVAILABLE:
        version = _display_version(snapshot.available_version)
        return _UpdatePresentation(
            status_text=f"Update {version} available",
            action_text="Download",
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
            action_text=reveal_action_label,
            action=_UpdateAction.REVEAL,
        )
    if snapshot.state == UpdateState.FAILED:
        return _UpdatePresentation(
            status_text="Download failed",
            action_text="Retry",
            action=_UpdateAction.RETRY,
            error=True,
        )
    return _UpdatePresentation()


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
    _apply_preferences_to_env(_load_preferences())

    configure_process_dpi_awareness()
    root = _create_splash_root(tk)
    apply_tk_scaling(root)
    _configure_runtime_tk_fonts(root)
    splash_scale = tk_display_scale(root)
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

    _PLATFORM_ADAPTER.install_about_handler(root, program_name, version)

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
    content_frame.pack(fill="both", expand=True, padx=px(22))

    left_frame = tk.Frame(content_frame, bg=_BG_COLOR)
    left_frame.pack(side="left", fill="both", expand=True)

    divider = tk.Frame(content_frame, bg=_BORDER_COLOR, width=1)
    divider.pack(side="left", fill="y", padx=(px(18), px(12)), pady=px(26))

    right_frame = tk.Frame(content_frame, bg=_BG_COLOR)
    right_frame.pack(side="left", fill="both", expand=True)

    # -- logo image, centered near the top --------------------------------------
    logo_photo = None
    if _LOGO_PATH:
        try:
            from PIL import Image, ImageTk
            logo_img = Image.open(_LOGO_PATH)
            # scale down to a sensible splash-screen size if the source is
            # larger than needed, preserving aspect ratio -- keeps this
            # robust to the source asset's exact dimensions changing later
            max_logo_dim = px(140)
            scale = min(max_logo_dim / logo_img.width, max_logo_dim / logo_img.height, 1.0)
            if scale < 1.0:
                new_size = (int(logo_img.width * scale), int(logo_img.height * scale))
                logo_img = logo_img.resize(new_size, Image.LANCZOS)
            logo_photo = ImageTk.PhotoImage(logo_img, master=root)
        except Exception as e:
            _LOG.warning(f"could not load splash screen logo ({e}); continuing without it.")
    else:
        _LOG.warning("splash screen logo asset not found; continuing without it.")

    if logo_photo is not None:
        logo_label = tk.Label(left_frame, image=logo_photo, bg=_BG_COLOR, borderwidth=0)
        logo_label.image = logo_photo  # keep a reference so it isn't garbage-collected
        logo_label.pack(pady=(22, 6))

    # -- title + version, centered top -------------------------------------------
    title_label = tk.Label(
        left_frame, text=program_name, font=_TITLE_FONT,
        fg=_TITLE_COLOR, bg=_BG_COLOR,
    )
    title_label.pack(pady=(0, 0))

    version_label = tk.Label(
        left_frame, text=f"Version {version}", font=_VERSION_FONT,
        fg=_SUBTITLE_COLOR, bg=_BG_COLOR,
    )
    version_label.pack(pady=(0, 8))

    last_update_presentation: list[_UpdatePresentation | None] = [None]
    map_library_workflow_ref: list[MapLibraryWorkflow | None] = [None]

    # Status and action labels stay packed even when empty. State changes never
    # resize the splash, and keyboard focus is enabled only for active actions.
    update_label = tk.Label(
        left_frame,
        text="",
        font=_SMALL_FONT,
        fg=_INSTRUCTION_COLOR,
        bg=_BG_COLOR,
        cursor="arrow",
        takefocus=False,
        highlightthickness=1,
        highlightbackground=_BG_COLOR,
        highlightcolor=_BUTTON_BG,
    )
    update_label.pack(pady=(0, 2))

    update_action_label = tk.Label(
        left_frame,
        text="",
        font=_SMALL_FONT,
        fg=_BUTTON_BG,
        bg=_BG_COLOR,
        cursor="arrow",
        takefocus=False,
        highlightthickness=1,
        highlightbackground=_BG_COLOR,
        highlightcolor=_BUTTON_BG,
    )
    update_action_label.pack(pady=(0, 4))

    # Progress bar — always packed, always 4 px tall.  Initially its background
    # matches the window so it is invisible; becomes visible during a download.
    update_progress_canvas = tk.Canvas(
        left_frame,
        width=300,
        height=4,
        bg=_BG_COLOR,
        highlightthickness=0,
    )
    _update_progress_bar = update_progress_canvas.create_rectangle(
        0, 0, 0, 4, fill=_BUTTON_BG, width=0
    )
    update_progress_canvas.pack(pady=(0, 4))

    def _set_progress_bar_visible(visible: bool):
        update_progress_canvas.config(
            bg=DARK_THEME.entry_background if visible else _BG_COLOR
        )
        if not visible:
            update_progress_canvas.coords(_update_progress_bar, 0, 0, 0, 4)

    def _set_progress(frac: float):
        clamped = max(0.0, min(1.0, float(frac)))
        update_progress_canvas.coords(_update_progress_bar, 0, 0, int(300 * clamped), 4)

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

    def _apply_update_presentation(presentation: _UpdatePresentation) -> None:
        update_label.config(
            text=presentation.status_text,
            fg="#ff9b90" if presentation.error else _INSTRUCTION_COLOR,
        )
        update_action_label.config(text=presentation.action_text)
        _bind_label_action(update_label, presentation.status_action)
        _bind_label_action(update_action_label, presentation.action)
        _set_progress_bar_visible(presentation.progress_visible)
        _set_progress(presentation.progress_fraction)

    def _refresh_update_presentation() -> None:
        if session.closing:
            return
        snapshot = update_manager.snapshot()
        presentation = _update_presentation(
            snapshot,
            update_manager.reveal_action_label,
        )
        if presentation != last_update_presentation[0]:
            _apply_update_presentation(presentation)
            last_update_presentation[0] = presentation
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

    def _leave_splash() -> None:
        workflow = map_library_workflow_ref[0]
        if workflow is not None:
            workflow.close()
        session.mark_closing()
        session.cancel_after_callbacks(root)
        root.withdraw()
        root.quit()

    # -- browse button + instructions ---------------------------------------------
    def _show_invalid_map_feedback(message: str) -> None:
        show_feedback(
            root,
            f"Unable to open this folder: {message}",
            kind="error",
            duration_ms=9000,
            font=_BODY_FONT,
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
        _leave_splash()

    def _invoke_and_break(callback):
        callback()
        return "break"

    def _bind_activation(widget, callback) -> None:
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            widget.bind(
                sequence,
                lambda _event, cb=callback: _invoke_and_break(cb),
            )

    browse_button = tk.Label(
        left_frame,
        text="Open map…",
        font=_BUTTON_FONT,
        bg=_BUTTON_BG,
        fg=_BUTTON_FG,
        padx=34,
        pady=11,
        cursor="hand2",
        takefocus=True,
        highlightthickness=1,
        highlightbackground=_BUTTON_BORDER_COLOR,
        highlightcolor=_BUTTON_BORDER_COLOR,
    )
    _bind_activation(browse_button, on_open_map_folder)
    browse_button.bind("<Enter>", lambda _event: browse_button.config(bg=_BUTTON_HOVER_BG))
    browse_button.bind("<Leave>", lambda _event: browse_button.config(bg=_BUTTON_BG))
    browse_button.pack(pady=(_TITLE_TO_ACTION_GAP, _BROWSE_BUTTON_BOTTOM_GAP))

    instruction_label = tk.Label(
        left_frame,
        text="Choose a cave map folder.\n"
             "Maps use .obj files with matching .mtl and textures.",
        font=_INSTRUCTION_FONT,
        fg=_INSTRUCTION_COLOR, bg=_BG_COLOR,
        justify="center",
    )
    instruction_label.pack(pady=(0, _INSTRUCTION_BOTTOM_GAP))

    def _on_preferences_click():
        def _on_preferences_applied(_preferences) -> None:
            workflow = map_library_workflow_ref[0]
            if workflow is None:
                return
            from caveviewer.gui.standard_library_maps import (
                default_map_library_install_dir,
            )

            workflow.set_map_library_root_dir(default_map_library_install_dir())

        _show_preferences_dialog(
            root,
            ui_font_family=_UI_FONT_FAMILY,
            desktop_services=desktop_services,
            platform_runtime=platform_runtime,
            on_applied=_on_preferences_applied,
        )

    def _widget_exists(widget) -> bool:
        if widget is None:
            return False
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def _splash_exists() -> bool:
        return not session.closing and _widget_exists(root)

    def _open_library_map_from_splash(path: str) -> None:
        is_valid, error_message = _validate_selected_map_folder(path)
        if not is_valid:
            _show_invalid_map_feedback(error_message)
            return

        session.select_folder(path)
        _save_last_browse_dir(path)
        _leave_splash()

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
            font=_BODY_FONT,
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
        open_map=_open_library_map_from_splash,
        show_feedback=_show_map_library_feedback,
        logger=_LOG,
        map_library_root_dir_provider=default_map_library_install_dir,
        open_guided_dive=_open_guided_dive_from_splash,
    )
    map_library_workflow_ref[0] = map_library_workflow

    def _create_map_library_panel(parent) -> None:
        # The workflow owns catalog/download state transitions; splash only
        # supplies the parent widget and session-level callbacks.
        map_library_workflow.populate_panel(parent, recent_map_paths)

    secondary_link_row = tk.Frame(left_frame, bg=_BG_COLOR)
    secondary_link_row.pack(pady=(_SECONDARY_LINK_ROW_TOP_GAP, _SECONDARY_LINK_ROW_BOTTOM_GAP))

    preferences_link = tk.Label(
        secondary_link_row,
        text="Preferences",
        font=_SMALL_FONT,
        fg="#5d6f8a",
        bg=_BG_COLOR,
        cursor="hand2",
        takefocus=True,
        highlightthickness=1,
        highlightbackground=_BG_COLOR,
        highlightcolor=_BUTTON_BG,
    )
    _bind_activation(preferences_link, _on_preferences_click)
    preferences_link.pack(side="left")

    credit_label = tk.Label(
        left_frame,
        text=_CREDITS_TEXT,
        font=_FOOTER_FONT,
        fg="#5f606b",
        bg=_BG_COLOR,
        justify="center",
    )
    credit_label.pack(pady=(0, _FOOTER_CREDITS_BOTTOM_PAD))

    _create_map_library_panel(right_frame)

    # -- footer note ----------------------------------------------------------------

    browse_button.focus_set()
    root.update_idletasks()
    final_height = max(
        px(_SPLASH_WINDOW_MIN_HEIGHT),
        root.winfo_reqheight() + px(_SPLASH_WINDOW_EXTRA_BOTTOM_SLACK),
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
    root.bind("<Return>", lambda _event: on_open_map_folder())
    root.bind("<Escape>", on_close)
    bind_primary_shortcut(root, "w", on_close)
    root.protocol("WM_DELETE_WINDOW", on_close)

    update_manager.set_foreground_update_surface_active(True)
    try:
        root.mainloop()
    finally:
        session.cancel_after_callbacks(root)
        update_manager.set_foreground_update_surface_active(False)

    # Some adapters keep the single Tk app object alive for process-level
    # native menu callbacks. Others destroy the splash root normally.
    if _SPLASH_LAYOUT_POLICY.destroy_root_on_close:
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
