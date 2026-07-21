"""
caveviewer.gui.splash_screen

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
import queue
import sys
import threading
from dataclasses import dataclass

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
from caveviewer.gui.map_cache_management import (
    has_managed_map_cache,
    remove_managed_map_cache,
)
from caveviewer.gui.map_history import load_recent_map_paths, remove_recent_map_path
from caveviewer.gui.map_selection import (
    validate_selected_map_folder as _validate_selected_map_folder,
)
from caveviewer.gui.platform import get_splash_platform_adapter
from caveviewer.gui.platform import (
    DesktopServices,
    DirectorySelection,
    get_desktop_services,
    tk_root_options,
)
from caveviewer.gui.preference_paths import migrate_state_file, write_text_atomic
from caveviewer.gui.sample_map_download import (
    SampleDownloadFailed,
    SampleDownloadProgress,
    SampleDownloadSucceeded,
    close_desktop_inhibitor,
    safe_desktop_inhibit,
    start_sample_download_worker,
)
from caveviewer.gui.tk_feedback import show_feedback
from caveviewer.gui.tk_shortcuts import bind_primary_shortcut
from caveviewer.gui.tk_theme import DARK_THEME
from caveviewer.gui.update_manager import (
    UpdateManager,
    UpdateSnapshot,
    UpdateState,
)
from caveviewer.resources import image_path


def _resolve_asset_path(filename: str) -> str | None:
    """Resolve an image from the installed or bundled resource package."""
    path = image_path(filename)
    return str(path) if path.is_file() else None


# Resolve this once at import time -- same asset already used for the
# in-program loading-screen logo, reused here rather than shipping a
# second copy of the same image.
_LOGO_PATH = _resolve_asset_path("app_mark_transparent.png")
if sys.platform == "darwin":
    _APP_ICON_PATH = _resolve_asset_path("app_icon_macos.png")
elif sys.platform == "win32":
    _APP_ICON_PATH = _resolve_asset_path("app_icon_windows.png")
else:
    _APP_ICON_PATH = _resolve_asset_path("app_icon_macos.png")


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
    if sys.platform == "darwin":
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
_PLATFORM_ADAPTER = get_splash_platform_adapter()
_WINDOWS_SPLASH_LAYOUT = sys.platform == "win32"
_LINUX_SPLASH_LAYOUT = sys.platform.startswith("linux")
_ROOMY_SPLASH_LAYOUT = _WINDOWS_SPLASH_LAYOUT or _LINUX_SPLASH_LAYOUT
_UI_FONT_FAMILY = _PLATFORM_ADAPTER.ui_font_family()
_TITLE_FONT = (_UI_FONT_FAMILY, 24, "bold")
_VERSION_FONT = (_UI_FONT_FAMILY, 12)
_BODY_FONT = (_UI_FONT_FAMILY, 12)
_SMALL_FONT = (_UI_FONT_FAMILY, 10)
_LIBRARY_METADATA_FONT = (_UI_FONT_FAMILY, 9)
_INSTRUCTION_FONT = (_UI_FONT_FAMILY, 11) if _ROOMY_SPLASH_LAYOUT else _BODY_FONT
_FOOTER_FONT = (_UI_FONT_FAMILY, 9) if _ROOMY_SPLASH_LAYOUT else _SMALL_FONT
_LINK_FONT = (_UI_FONT_FAMILY, 10, "underline")
_BUTTON_FONT = (_UI_FONT_FAMILY, 13)
_SPLASH_WINDOW_WIDTH = 940
_SPLASH_WINDOW_MIN_HEIGHT = 480 if sys.platform == "darwin" else 560
_SPLASH_WINDOW_EXTRA_BOTTOM_SLACK = 24 if sys.platform == "darwin" else 0
_SECONDARY_LINK_ROW_BOTTOM_GAP = 18 if sys.platform == "darwin" else 36
_FOOTER_CREDITS_BOTTOM_PAD = 18 if sys.platform == "darwin" else 36
_TITLE_TO_ACTION_GAP = 72 if _LINUX_SPLASH_LAYOUT else (58 if _WINDOWS_SPLASH_LAYOUT else 28)
_BROWSE_BUTTON_BOTTOM_GAP = 42 if _LINUX_SPLASH_LAYOUT else (32 if _WINDOWS_SPLASH_LAYOUT else 16)
_INSTRUCTION_BOTTOM_GAP = 30 if _LINUX_SPLASH_LAYOUT else (20 if _WINDOWS_SPLASH_LAYOUT else 0)
_SECONDARY_LINK_ROW_TOP_GAP = 40 if _LINUX_SPLASH_LAYOUT else (30 if _WINDOWS_SPLASH_LAYOUT else 16)
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
_LIBRARY_PANEL_BORDER_COLOR = "#252832"
_LIBRARY_METADATA_COLOR = "#5a5d68"
_LIBRARY_METADATA_ERROR_COLOR = DARK_THEME.error_text
_LIBRARY_PROGRESS_TRACK_COLOR = DARK_THEME.entry_background
_LIBRARY_PROGRESS_FILL_COLOR = DARK_THEME.primary_button
_LIBRARY_PROGRESS_HEIGHT = 4
_LIBRARY_PROGRESS_BOTTOM_PAD = 5
_LIBRARY_ACTION_BUTTON_WIDTH = 8
_LIBRARY_ACTION_BUTTON_PAD_X = 10
_LIBRARY_ACTION_BUTTON_PAD_Y = 5
_LIBRARY_OVERFLOW_TEXT = "⋮"
_LIBRARY_OVERFLOW_FONT = (_UI_FONT_FAMILY, 14, "bold")
_LIBRARY_OVERFLOW_FG = "#606370"
_LIBRARY_OVERFLOW_HOVER_FG = _INSTRUCTION_COLOR
_LIBRARY_OVERFLOW_HOVER_BG = DARK_THEME.secondary_button
_LIBRARY_MENU_BG = DARK_THEME.entry_background
_LIBRARY_MENU_BORDER = DARK_THEME.secondary_button_border
_LIBRARY_MENU_HOVER_BG = DARK_THEME.secondary_button_hover
_LIBRARY_MENU_TEXT = DARK_THEME.body_text
_SAMPLE_MAP_SIZE_LABELS = {
    "Boh.Yai.Mine.I.Low.Res.zip": "57 MB",
    "Boh.Yai.Mine.II.Low.Res.zip": "62 MB",
    "Devils.Eye.3D.Map.zip": "87 MB",
    "Peacock.Springs.Cave.System.3D.Map.zip": "365 MB",
}
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
    global _UI_FONT_FAMILY, _TITLE_FONT, _VERSION_FONT, _BODY_FONT
    global _SMALL_FONT, _LIBRARY_METADATA_FONT, _INSTRUCTION_FONT
    global _FOOTER_FONT, _LINK_FONT, _BUTTON_FONT, _LIBRARY_OVERFLOW_FONT

    try:
        import tkinter.font as tkfont

        available = {family.lower(): family for family in tkfont.families(root)}
        preferred = [_PLATFORM_ADAPTER.ui_font_family()]
        if _LINUX_SPLASH_LAYOUT:
            # Keep splash startup on the Tk path free of subprocess waits.
            # Prefer families Tk already knows instead of asking fontconfig.
            preferred.extend(_LINUX_TK_SANS_FAMILIES)

        fallback_family = tkfont.nametofont("TkDefaultFont").actual("family")
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

    _TITLE_FONT = (_UI_FONT_FAMILY, 24, "bold")
    _VERSION_FONT = (_UI_FONT_FAMILY, 12)
    _BODY_FONT = (_UI_FONT_FAMILY, 12)
    _SMALL_FONT = (_UI_FONT_FAMILY, 10)
    _LIBRARY_METADATA_FONT = (_UI_FONT_FAMILY, 9)
    _INSTRUCTION_FONT = (_UI_FONT_FAMILY, 11) if _ROOMY_SPLASH_LAYOUT else _BODY_FONT
    _FOOTER_FONT = (_UI_FONT_FAMILY, 9) if _ROOMY_SPLASH_LAYOUT else _SMALL_FONT
    _LINK_FONT = (_UI_FONT_FAMILY, 10, "underline")
    _BUTTON_FONT = (_UI_FONT_FAMILY, 13)
    _LIBRARY_OVERFLOW_FONT = (_UI_FONT_FAMILY, 14, "bold")


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


@dataclass(frozen=True)
class _MapLibraryRowWidgets:
    """Tk widgets owned by one map-library row on the splash thread."""

    row_shell: object
    leading_widget: object
    action_button: object
    metadata_label: object | None
    progress_bar_canvas: object | None = None
    progress_bar: object | None = None


def _display_version(version: str | None) -> str:
    text = (version or "").strip()
    return text[1:] if text.lower().startswith("v") else text


def _update_presentation(
    snapshot: UpdateSnapshot,
    reveal_action_label: str,
) -> _UpdatePresentation:
    """Map manager states to the exact compact labels rendered by the splash."""
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


def _sample_map_splash_action_text(downloaded: bool) -> str:
    return "Open" if downloaded else "Get"


def _sample_map_splash_size_text(sample) -> str:
    size_bytes = getattr(sample, "size_bytes", None)
    if size_bytes:
        return f"{size_bytes / (1024 * 1024):.0f} MB"
    return _SAMPLE_MAP_SIZE_LABELS.get(getattr(sample, "asset_name", ""), "")


def _map_library_recent_detail_text(path: str) -> str:
    parent = os.path.dirname(os.path.abspath(path))
    return _compact_map_library_path(parent, max_chars=44)


def _map_library_recent_title(path: str) -> str:
    normalized = os.path.normpath(os.path.abspath(path))
    return os.path.basename(normalized) or normalized


def _compact_map_library_path(path: str, *, max_chars: int = 44) -> str:
    expanded = os.path.abspath(os.path.expanduser(path.strip() or "~"))
    home = os.path.abspath(os.path.expanduser("~"))
    if expanded == home:
        display = "~"
    elif expanded.startswith(home + os.sep):
        display = "~" + expanded[len(home):]
    else:
        display = expanded
    if len(display) <= max_chars:
        return display

    drive, tail = os.path.splitdrive(display)
    parts = [part for part in tail.split(os.sep) if part]
    if len(parts) >= 2:
        suffix = os.sep.join(parts[-2:])
        prefix = (
            "~"
            if display.startswith("~" + os.sep)
            else drive + os.sep
            if drive
            else os.sep
        )
        compact = prefix + "…" + os.sep + suffix
        if len(compact) <= max_chars:
            return compact
    return "…" + display[-(max_chars - 1):]


def show_splash_screen(
    program_name: str = APP_NAME,
    version: str = APP_VERSION,
    *,
    update_manager: UpdateManager,
    desktop_services: DesktopServices | None = None,
) -> str | None:
    """
    Shows the launch splash screen and blocks until the person either
    picks a folder (Browse -> select a folder -> OK) or closes the
    window. Returns the selected folder path, or None if the window was closed
    without picking one. Update work belongs to app.py and may outlive this
    particular splash instance.
    """
    import tkinter as tk

    selected_folder: list[str | None] = [None]
    desktop_services = desktop_services or get_desktop_services()
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
    divider.pack(side="left", fill="y", padx=(px(18), px(18)), pady=px(26))

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

    splash_state = {"closing": False}
    last_update_presentation: list[_UpdatePresentation | None] = [None]

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
        if splash_state["closing"]:
            return
        snapshot = update_manager.snapshot()
        presentation = _update_presentation(
            snapshot,
            update_manager.reveal_action_label,
        )
        if presentation != last_update_presentation[0]:
            _apply_update_presentation(presentation)
            last_update_presentation[0] = presentation
        if snapshot.state == UpdateState.READY:
            # Only a visible splash performs the one automatic file-manager
            # reveal; downloads completing inside the viewer stay unobtrusive.
            update_manager.reveal_download(automatic=True)
        root.after(100, _refresh_update_presentation)

    def _leave_splash() -> None:
        _close_active_library_menu()
        _cancel_active_library_download_for_close()
        splash_state["closing"] = True
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

    def on_open_map_folder():
        last_dir = _load_last_browse_dir()
        selection = desktop_services.choose_directory(
            title="Open Map Folder",
            initial_dir=last_dir,
            parent=root,
        )
        if selection:
            is_valid, error_message = _validate_selected_map_folder(selection.path)
            if not is_valid:
                _show_invalid_map_feedback(error_message)
                return

            selected_folder[0] = selection.path
            _save_last_browse_dir(selection.path)
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
        text="Open Map Folder…",
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
        text="Choose the folder that contains your cave map files:\n"
             ".glb, or .obj with its matching .mtl and textures.",
        font=_INSTRUCTION_FONT,
        fg=_INSTRUCTION_COLOR, bg=_BG_COLOR,
        justify="center",
    )
    instruction_label.pack(pady=(0, _INSTRUCTION_BOTTOM_GAP))

    def _on_preferences_click():
        _show_preferences_dialog(
            root,
            ui_font_family=_UI_FONT_FAMILY,
            desktop_services=desktop_services,
        )

    from caveviewer.gui.sample_maps import (
        DownloadCancelled,
        KNOWN_SAMPLE_MAPS,
        default_sample_maps_install_dir,
        existing_sample_map_path,
        fetch_sample_map_catalog,
        is_sample_map_already_downloaded,
        remove_downloaded_sample_map,
    )

    sample_maps_root_dir = default_sample_maps_install_dir()
    recent_map_paths = _load_library_recent_map_paths()
    sample_catalog_by_name = {
        sample.display_name: sample for sample in KNOWN_SAMPLE_MAPS
    }
    # Splash owns all row widgets and mutable download presentation state on the
    # Tk thread. Catalog/download workers only publish queue messages polled by
    # this surface, preserving the same cleanup-safe installer path as the
    # original Sample Maps dialog.
    sample_map_rows: dict[str, _MapLibraryRowWidgets] = {}
    recent_map_rows: dict[str, _MapLibraryRowWidgets] = {}
    recent_rows_container = [None]
    recent_empty_note = [None]
    active_library_menu = {"window": None}
    library_scroll_sync = {"callback": None}
    library_mousewheel_bind = {"callback": None}
    downloaded_sample_paths: dict[str, str] = {}
    active_library_download = {
        "cancel_event": None,
        "after_id": None,
        "inhibitor": None,
        "sample_name": None,
        "thread": None,
    }
    sample_catalog_fetch = {
        "loading": False,
        "after_id": None,
        "queue": None,
        "pending_sample": None,
        "error": None,
    }

    def _widget_exists(widget) -> bool:
        if widget is None:
            return False
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def _splash_exists() -> bool:
        return not splash_state["closing"] and _widget_exists(root)

    def _sample_map_key(sample) -> str:
        return getattr(sample, "display_name", "")

    def _resolve_sample_catalog_entry(sample):
        return sample_catalog_by_name.get(_sample_map_key(sample), sample)

    def _sample_map_status_text(sample, *, downloaded: bool) -> str:
        if downloaded:
            return "Downloaded"
        return _sample_map_splash_size_text(_resolve_sample_catalog_entry(sample))

    def _set_library_row_metadata(
        sample, text: str, *, error: bool = False
    ) -> None:
        widgets = sample_map_rows.get(_sample_map_key(sample))
        if widgets is None or not _widget_exists(widgets.metadata_label):
            return
        widgets.metadata_label.config(
            text=text,
            fg=(
                _LIBRARY_METADATA_ERROR_COLOR
                if error
                else _LIBRARY_METADATA_COLOR
            ),
        )

    def _set_library_action_button(
        button, text: str, command, *, enabled: bool = True
    ) -> None:
        button._cv_enabled = bool(enabled)

        def invoke_if_enabled() -> None:
            if getattr(button, "_cv_enabled", True):
                command()

        _bind_activation(button, invoke_if_enabled)
        button.config(
            text=text,
            bg=_BUTTON_BG if enabled else DARK_THEME.secondary_button,
            fg=_BUTTON_FG if enabled else DARK_THEME.placeholder_text,
            cursor="hand2" if enabled else "arrow",
            takefocus=enabled,
            highlightbackground=(
                _BUTTON_BORDER_COLOR if enabled else DARK_THEME.entry_border
            ),
            highlightcolor=(
                _BUTTON_BORDER_COLOR if enabled else DARK_THEME.entry_border
            ),
        )

    def _open_library_map_from_splash(path: str) -> None:
        is_valid, error_message = _validate_selected_map_folder(path)
        if not is_valid:
            _show_invalid_map_feedback(error_message)
            return

        selected_folder[0] = path
        _save_last_browse_dir(path)
        _leave_splash()

    def _recent_map_key(path: str) -> str:
        try:
            return os.path.normcase(os.path.abspath(os.path.expanduser(path)))
        except (OSError, TypeError, ValueError):
            return str(path)

    def _close_active_library_menu() -> None:
        menu = active_library_menu.get("window")
        active_library_menu["window"] = None
        if _widget_exists(menu):
            try:
                menu.destroy()
            except tk.TclError:
                pass

    def _sync_library_scroll_after_row_change() -> None:
        callback = library_scroll_sync.get("callback")
        if callback is not None and _splash_exists():
            root.after_idle(callback)

    def _bind_library_mousewheel_if_ready(widget) -> None:
        callback = library_mousewheel_bind.get("callback")
        if callback is not None and _widget_exists(widget):
            callback(widget)

    def _remove_map_cache_from_splash(
        path: str,
        title: str,
        row_widgets: _MapLibraryRowWidgets | None,
    ) -> None:
        result = remove_managed_map_cache(path)
        if result.error:
            show_feedback(
                root,
                f"Unable to remove cache for {title}: {result.error}",
                kind="error",
                duration_ms=9000,
                font=_BODY_FONT,
            )
        elif result.removed:
            show_feedback(
                root,
                f"Removed cache for {title}.",
                kind="info",
                duration_ms=5000,
                font=_BODY_FONT,
            )
        else:
            show_feedback(
                root,
                f"No generated cache was found for {title}.",
                kind="info",
                duration_ms=5000,
                font=_BODY_FONT,
            )

        if row_widgets is not None and _widget_exists(row_widgets.leading_widget):
            _refresh_library_overflow_button(row_widgets.leading_widget)

    def _refresh_available_sample_row(sample) -> None:
        sample_key = _sample_map_key(sample)
        downloaded = is_sample_map_already_downloaded(sample_maps_root_dir, sample)
        result_path = existing_sample_map_path(sample_maps_root_dir, sample)
        if downloaded:
            downloaded_sample_paths[sample_key] = result_path
        else:
            downloaded_sample_paths.pop(sample_key, None)
        resolved_sample = _resolve_sample_catalog_entry(sample)
        _set_library_row_metadata(
            sample,
            _sample_map_status_text(resolved_sample, downloaded=downloaded),
        )
        _set_available_sample_action(
            sample,
            downloaded=downloaded,
            result_path=result_path if downloaded else None,
        )

    def _remove_standard_library_download_from_splash(
        sample,
        sample_path: str,
        row_widgets: _MapLibraryRowWidgets | None,
    ) -> None:
        cache_result = remove_managed_map_cache(sample_path)
        if cache_result.error:
            show_feedback(
                root,
                f"Unable to remove downloaded files for {sample.display_name}: "
                f"{cache_result.error}",
                kind="error",
                duration_ms=9000,
                font=_BODY_FONT,
            )
            if row_widgets is not None and _widget_exists(row_widgets.leading_widget):
                _refresh_library_overflow_button(row_widgets.leading_widget)
            return

        removal_result = remove_downloaded_sample_map(sample_maps_root_dir, sample)
        _refresh_available_sample_row(sample)
        if removal_result.error:
            show_feedback(
                root,
                f"Unable to remove downloaded files for {sample.display_name}: "
                f"{removal_result.error}",
                kind="error",
                duration_ms=9000,
                font=_BODY_FONT,
            )
            return

        if removal_result.removed_paths or cache_result.removed:
            show_feedback(
                root,
                f"Removed downloaded files for {sample.display_name}.",
                kind="info",
                duration_ms=5000,
                font=_BODY_FONT,
            )
            return

        show_feedback(
            root,
            f"No downloaded files were found for {sample.display_name}.",
            kind="info",
            duration_ms=5000,
            font=_BODY_FONT,
        )

    def _remove_recent_map_from_splash(path: str) -> None:
        remove_recent_map_path(path)
        normalized = _recent_map_key(path)
        recent_map_paths[:] = [
            recent_path
            for recent_path in recent_map_paths
            if _recent_map_key(recent_path) != normalized
        ]
        row_widgets = recent_map_rows.pop(normalized, None)
        if row_widgets is not None and _widget_exists(row_widgets.row_shell):
            row_widgets.row_shell.destroy()

        container = recent_rows_container[0]
        if not recent_map_rows and _widget_exists(container):
            if not _widget_exists(recent_empty_note[0]):
                recent_empty_note[0] = _create_map_library_empty_note(
                    container,
                    "No maps added yet.",
                    bottom_pad=18,
                )
                _bind_library_mousewheel_if_ready(recent_empty_note[0])
        _sync_library_scroll_after_row_change()

    def _library_row_menu_actions(button) -> tuple[tuple[str, object], ...]:
        factory = getattr(button, "_cv_menu_actions_factory", None)
        if factory is None:
            return ()
        try:
            return tuple(factory() or ())
        except Exception as exc:
            _LOG.warning("could not build map-library row menu: %s", exc)
            return ()

    def _refresh_library_overflow_button(button) -> None:
        has_actions = bool(_library_row_menu_actions(button))
        button._cv_has_menu_actions = has_actions
        button.config(
            text=_LIBRARY_OVERFLOW_TEXT if has_actions else "",
            fg=_LIBRARY_OVERFLOW_FG if has_actions else _PANEL_COLOR,
            cursor="hand2" if has_actions else "arrow",
            takefocus=has_actions,
            highlightbackground=_PANEL_COLOR,
            highlightcolor=_BUTTON_BORDER_COLOR if has_actions else _PANEL_COLOR,
        )

    def _show_library_row_menu(button) -> None:
        _close_active_library_menu()
        if not _widget_exists(button):
            return

        actions = _library_row_menu_actions(button)
        if not actions:
            _refresh_library_overflow_button(button)
            return

        menu = tk.Toplevel(root)
        active_library_menu["window"] = menu
        menu.withdraw()
        menu.overrideredirect(True)
        menu.transient(root)
        menu.configure(bg=_LIBRARY_MENU_BORDER)

        frame = tk.Frame(
            menu,
            bg=_LIBRARY_MENU_BG,
            highlightthickness=1,
            highlightbackground=_LIBRARY_MENU_BORDER,
            highlightcolor=_LIBRARY_MENU_BORDER,
        )
        frame.pack()

        first_item = [None]
        for item_text, item_action in actions:

            def invoke_and_close(action=item_action) -> None:
                _close_active_library_menu()
                action()

            item = tk.Label(
                frame,
                text=item_text,
                font=_SMALL_FONT,
                bg=_LIBRARY_MENU_BG,
                fg=_LIBRARY_MENU_TEXT,
                padx=px(12),
                pady=px(7),
                cursor="hand2",
                takefocus=True,
                anchor="w",
            )
            _bind_activation(item, invoke_and_close)
            item.bind(
                "<Enter>",
                lambda _event, target=item: target.config(
                    bg=_LIBRARY_MENU_HOVER_BG
                ),
            )
            item.bind(
                "<Leave>",
                lambda _event, target=item: target.config(bg=_LIBRARY_MENU_BG),
            )
            item.pack(fill="x")
            if first_item[0] is None:
                first_item[0] = item

        try:
            x = button.winfo_rootx()
            y = button.winfo_rooty() + button.winfo_height() + px(4)
            menu.geometry(f"+{x}+{y}")
            menu.deiconify()
            menu.lift()
            if first_item[0] is not None:
                first_item[0].focus_set()
        except tk.TclError:
            _close_active_library_menu()
            return

        menu.bind("<Escape>", lambda _event: _close_active_library_menu())
        menu.bind(
            "<FocusOut>",
            lambda _event: root.after(80, _close_active_library_menu),
        )

    def _create_library_overflow_button(parent, menu_actions_factory=None):
        button = tk.Label(
            parent,
            text="",
            font=_LIBRARY_OVERFLOW_FONT,
            bg=_PANEL_COLOR,
            fg=_PANEL_COLOR,
            width=2,
            padx=0,
            pady=0,
            cursor="arrow",
            takefocus=False,
            highlightthickness=1,
            highlightbackground=_PANEL_COLOR,
            highlightcolor=_PANEL_COLOR,
        )
        button._cv_menu_actions_factory = menu_actions_factory
        button._cv_has_menu_actions = False

        def show_hover(_event=None) -> None:
            if not getattr(button, "_cv_has_menu_actions", False):
                return
            button.config(
                bg=_LIBRARY_OVERFLOW_HOVER_BG,
                fg=_LIBRARY_OVERFLOW_HOVER_FG,
                highlightbackground=_LIBRARY_MENU_BORDER,
            )

        def clear_hover(_event=None) -> None:
            if not getattr(button, "_cv_has_menu_actions", False):
                button.config(
                    bg=_PANEL_COLOR,
                    fg=_PANEL_COLOR,
                    highlightbackground=_PANEL_COLOR,
                )
                return
            button.config(
                bg=_PANEL_COLOR,
                fg=_LIBRARY_OVERFLOW_FG,
                highlightbackground=_PANEL_COLOR,
            )

        _bind_activation(button, lambda: _show_library_row_menu(button))
        button.bind("<Enter>", show_hover)
        button.bind("<Leave>", clear_hover)
        button.pack(side="left", padx=(px(6), 0), pady=px(5))
        _refresh_library_overflow_button(button)
        return button

    def _open_sample_map_from_splash(sample) -> None:
        sample_path = (
            _downloaded_sample_map_path(sample)
            or existing_sample_map_path(sample_maps_root_dir, sample)
        )
        _open_library_map_from_splash(sample_path)

    def _downloaded_sample_map_path(sample) -> str | None:
        sample_key = _sample_map_key(sample)
        result_path = downloaded_sample_paths.get(sample_key)
        if result_path:
            return result_path
        if is_sample_map_already_downloaded(sample_maps_root_dir, sample):
            return existing_sample_map_path(sample_maps_root_dir, sample)
        return None

    def _set_available_sample_action(
        sample,
        *,
        downloaded: bool,
        enabled: bool = True,
        action_text: str | None = None,
        result_path: str | None = None,
    ) -> None:
        widgets = sample_map_rows.get(_sample_map_key(sample))
        if widgets is None or not _widget_exists(widgets.action_button):
            return
        if downloaded and result_path:
            downloaded_sample_paths[_sample_map_key(sample)] = result_path
        if downloaded:
            _set_library_action_button(
                widgets.action_button,
                action_text or _sample_map_splash_action_text(downloaded=True),
                lambda s=sample: _open_sample_map_from_splash(s),
                enabled=enabled,
            )
            _set_library_row_metadata(
                sample, _sample_map_status_text(sample, downloaded=True)
            )
            if _widget_exists(widgets.leading_widget):
                _refresh_library_overflow_button(widgets.leading_widget)
            return
        _set_library_action_button(
            widgets.action_button,
            action_text or _sample_map_splash_action_text(downloaded=False),
            lambda s=sample: _on_sample_map_action(s),
            enabled=enabled,
        )
        if _widget_exists(widgets.leading_widget):
            _refresh_library_overflow_button(widgets.leading_widget)

    def _set_non_active_sample_actions_enabled(
        active_sample, enabled: bool
    ) -> None:
        active_key = _sample_map_key(active_sample)
        for row_sample in KNOWN_SAMPLE_MAPS:
            if _sample_map_key(row_sample) == active_key:
                continue
            result_path = downloaded_sample_paths.get(_sample_map_key(row_sample))
            downloaded = bool(result_path) or is_sample_map_already_downloaded(
                sample_maps_root_dir,
                row_sample,
            )
            _set_available_sample_action(
                row_sample,
                downloaded=downloaded,
                enabled=enabled,
                result_path=(
                    result_path
                    or (
                        existing_sample_map_path(
                            sample_maps_root_dir, row_sample
                        )
                        if downloaded
                        else None
                    )
                ),
            )

    def _reset_library_progress_bar(sample) -> None:
        widgets = sample_map_rows.get(_sample_map_key(sample))
        if widgets is None or not _widget_exists(widgets.progress_bar_canvas):
            return
        widgets.progress_bar_canvas.config(bg=_PANEL_COLOR)
        widgets.progress_bar_canvas.coords(
            widgets.progress_bar,
            0,
            0,
            0,
            px(_LIBRARY_PROGRESS_HEIGHT),
        )

    def _show_library_progress_bar(sample) -> None:
        widgets = sample_map_rows.get(_sample_map_key(sample))
        if widgets is None or not _widget_exists(widgets.progress_bar_canvas):
            return
        widgets.progress_bar_canvas.config(bg=_LIBRARY_PROGRESS_TRACK_COLOR)
        widgets.progress_bar_canvas.coords(
            widgets.progress_bar,
            0,
            0,
            0,
            px(_LIBRARY_PROGRESS_HEIGHT),
        )
        root.update_idletasks()

    def _apply_library_download_progress(
        sample, progress: SampleDownloadProgress
    ) -> None:
        widgets = sample_map_rows.get(_sample_map_key(sample))
        if widgets is None or not _widget_exists(widgets.progress_bar_canvas):
            return
        if progress.total_bytes is None or progress.total_bytes <= 0:
            _set_library_row_metadata(sample, "Downloading…")
            return
        fraction = min(1.0, progress.downloaded_bytes / progress.total_bytes)
        _set_library_row_metadata(
            sample, f"Downloading… {int(round(fraction * 100))}%"
        )
        canvas_width = widgets.progress_bar_canvas.winfo_width()
        if canvas_width > 1:
            widgets.progress_bar_canvas.coords(
                widgets.progress_bar,
                0,
                0,
                int(canvas_width * fraction),
                px(_LIBRARY_PROGRESS_HEIGHT),
            )

    def _clear_active_library_download(sample) -> None:
        inhibitor = active_library_download.get("inhibitor")
        active_library_download.update(
            {
                "cancel_event": None,
                "after_id": None,
                "inhibitor": None,
                "sample_name": None,
                "thread": None,
            }
        )
        close_desktop_inhibitor(inhibitor)
        if _splash_exists():
            _set_non_active_sample_actions_enabled(sample, True)

    def _cancel_active_library_download_for_close() -> None:
        cancel_event = active_library_download.get("cancel_event")
        if cancel_event is not None:
            cancel_event.set()
        after_id = active_library_download.get("after_id")
        active_library_download["after_id"] = None
        if after_id is not None:
            try:
                root.after_cancel(after_id)
            except tk.TclError:
                pass
        inhibitor = active_library_download.get("inhibitor")
        active_library_download.update(
            {
                "cancel_event": None,
                "inhibitor": None,
                "sample_name": None,
                "thread": None,
            }
        )
        close_desktop_inhibitor(inhibitor)

    def _finish_library_download_success(sample, result_path: str) -> None:
        if not _splash_exists():
            _clear_active_library_download(sample)
            return
        _reset_library_progress_bar(sample)
        _set_available_sample_action(
            sample,
            downloaded=True,
            result_path=result_path,
        )
        _clear_active_library_download(sample)

    def _finish_library_download_failure(sample, error: Exception) -> None:
        if not _splash_exists():
            _clear_active_library_download(sample)
            return
        _reset_library_progress_bar(sample)
        if isinstance(error, DownloadCancelled):
            _set_library_row_metadata(
                sample, _sample_map_status_text(sample, downloaded=False)
            )
            _set_available_sample_action(sample, downloaded=False)
            _clear_active_library_download(sample)
            return
        _set_library_row_metadata(sample, "Download failed", error=True)
        _set_available_sample_action(
            sample,
            downloaded=False,
            action_text="Retry",
        )
        _clear_active_library_download(sample)
        show_feedback(
            root,
            f"Couldn't download {sample.display_name}. Check your connection and retry.",
            kind="error",
            duration_ms=9000,
            font=_BODY_FONT,
            max_wraplength=360,
        )

    def _schedule_library_download_poll(
        sample, message_queue, cancel_event
    ) -> None:
        if active_library_download.get("cancel_event") is not cancel_event:
            return
        if not _splash_exists():
            cancel_event.set()
            _clear_active_library_download(sample)
            return
        active_library_download["after_id"] = root.after(
            80,
            lambda s=sample, q=message_queue, c=cancel_event: _poll_library_download_queue(
                s,
                q,
                c,
            ),
        )

    def _poll_library_download_queue(
        sample, message_queue, cancel_event
    ) -> None:
        if active_library_download.get("cancel_event") is not cancel_event:
            return
        active_library_download["after_id"] = None
        if not _splash_exists():
            cancel_event.set()
            _clear_active_library_download(sample)
            return

        latest_progress = None
        terminal_message = None
        while True:
            try:
                message = message_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(message, SampleDownloadProgress):
                latest_progress = message
            else:
                terminal_message = message
                break

        if latest_progress is not None:
            try:
                _apply_library_download_progress(sample, latest_progress)
            except tk.TclError:
                cancel_event.set()
                _clear_active_library_download(sample)
                return

        if isinstance(terminal_message, SampleDownloadSucceeded):
            _finish_library_download_success(
                sample, terminal_message.result_path
            )
            return
        if isinstance(terminal_message, SampleDownloadFailed):
            _finish_library_download_failure(sample, terminal_message.error)
            return

        try:
            _schedule_library_download_poll(sample, message_queue, cancel_event)
        except tk.TclError:
            cancel_event.set()
            _clear_active_library_download(sample)

    def _start_inline_sample_download(sample) -> None:
        if active_library_download.get("cancel_event") is not None:
            show_feedback(
                root,
                "Finish or cancel the current map download before starting another.",
                kind="info",
                duration_ms=7000,
                font=_BODY_FONT,
                max_wraplength=360,
            )
            return
        if getattr(sample, "download_url", None) is None:
            _prepare_sample_catalog_for_download(sample)
            return

        widgets = sample_map_rows.get(_sample_map_key(sample))
        if widgets is None or not _widget_exists(widgets.action_button):
            return

        _show_library_progress_bar(sample)
        _set_library_row_metadata(sample, "Downloading…")
        cancel_event = threading.Event()
        message_queue = queue.Queue()

        def request_cancel() -> None:
            cancel_event.set()
            if _widget_exists(widgets.action_button):
                _set_library_action_button(
                    widgets.action_button,
                    "Cancel",
                    lambda: None,
                    enabled=False,
                )
            _set_library_row_metadata(sample, "Canceling…")

        _set_library_action_button(widgets.action_button, "Cancel", request_cancel)
        _set_non_active_sample_actions_enabled(sample, False)
        active_library_download.update(
            {
                "cancel_event": cancel_event,
                "after_id": None,
                "inhibitor": safe_desktop_inhibit(
                    desktop_services,
                    f"Downloading {sample.display_name}",
                    parent=root,
                ),
                "sample_name": sample.display_name,
                "thread": None,
            }
        )

        try:
            worker = start_sample_download_worker(
                DirectorySelection.from_path(sample_maps_root_dir),
                sample,
                cancel_event,
                message_queue,
            )
        except RuntimeError as exc:
            _reset_library_progress_bar(sample)
            _set_library_row_metadata(sample, "Download failed", error=True)
            _set_available_sample_action(
                sample,
                downloaded=False,
                action_text="Retry",
            )
            _clear_active_library_download(sample)
            show_feedback(
                root,
                f"Couldn't start the {sample.display_name} download: {exc}",
                kind="error",
                duration_ms=9000,
                font=_BODY_FONT,
                max_wraplength=360,
            )
            return

        active_library_download["thread"] = worker
        _schedule_library_download_poll(sample, message_queue, cancel_event)

    def _handle_download_info_unavailable(sample) -> None:
        _set_library_row_metadata(sample, "Download info unavailable", error=True)
        _set_available_sample_action(
            sample,
            downloaded=False,
            action_text="Retry",
        )
        _set_non_active_sample_actions_enabled(sample, True)
        show_feedback(
            root,
            "Couldn't load download info. Check your connection and retry.",
            kind="error",
            duration_ms=9000,
            font=_BODY_FONT,
            max_wraplength=360,
        )

    def _schedule_sample_catalog_poll() -> None:
        if not _splash_exists():
            return
        sample_catalog_fetch["after_id"] = root.after(
            120, _poll_sample_catalog_fetch
        )

    def _poll_sample_catalog_fetch() -> None:
        sample_catalog_fetch["after_id"] = None
        if not _splash_exists():
            return
        fetch_queue = sample_catalog_fetch.get("queue")
        if fetch_queue is None:
            return
        try:
            catalog, error = fetch_queue.get_nowait()
        except queue.Empty:
            _schedule_sample_catalog_poll()
            return

        sample_catalog_fetch.update(
            {
                "loading": False,
                "queue": None,
                "error": error,
            }
        )
        for catalog_sample in catalog:
            sample_catalog_by_name[_sample_map_key(catalog_sample)] = (
                catalog_sample
            )
            if active_library_download.get("sample_name") == _sample_map_key(
                catalog_sample
            ):
                continue
            downloaded = is_sample_map_already_downloaded(
                sample_maps_root_dir,
                catalog_sample,
            )
            _set_library_row_metadata(
                catalog_sample,
                _sample_map_status_text(catalog_sample, downloaded=downloaded),
            )

        pending_sample = sample_catalog_fetch.get("pending_sample")
        sample_catalog_fetch["pending_sample"] = None
        if pending_sample is None:
            return
        resolved_sample = _resolve_sample_catalog_entry(pending_sample)
        if getattr(resolved_sample, "download_url", None) is None:
            _handle_download_info_unavailable(pending_sample)
            return
        _start_inline_sample_download(resolved_sample)

    def _start_sample_catalog_fetch(pending_sample=None) -> None:
        if pending_sample is not None:
            sample_catalog_fetch["pending_sample"] = pending_sample
        if sample_catalog_fetch["loading"]:
            return
        fetch_queue = queue.Queue(maxsize=1)
        sample_catalog_fetch.update(
            {
                "loading": True,
                "queue": fetch_queue,
                "error": None,
            }
        )

        def fetch_worker() -> None:
            try:
                result = fetch_sample_map_catalog()
            except Exception as exc:
                result = ([], f"Couldn't load the sample map list: {exc}")
            fetch_queue.put(result)

        threading.Thread(
            target=fetch_worker,
            name="CaveViewer-sample-map-catalog",
            daemon=True,
        ).start()
        _schedule_sample_catalog_poll()

    def _prepare_sample_catalog_for_download(sample) -> None:
        if active_library_download.get("cancel_event") is not None:
            show_feedback(
                root,
                "Finish or cancel the current map download before starting another.",
                kind="info",
                duration_ms=7000,
                font=_BODY_FONT,
                max_wraplength=360,
            )
            return
        _set_library_row_metadata(sample, "Preparing download…")
        _set_available_sample_action(sample, downloaded=False, enabled=False)
        _set_non_active_sample_actions_enabled(sample, False)
        _start_sample_catalog_fetch(pending_sample=sample)

    def _on_sample_map_action(sample) -> None:
        resolved_sample = _resolve_sample_catalog_entry(sample)
        if is_sample_map_already_downloaded(sample_maps_root_dir, resolved_sample):
            _open_sample_map_from_splash(sample)
            return
        if sample_catalog_fetch["loading"] and getattr(
            resolved_sample, "download_url", None
        ) is None:
            _prepare_sample_catalog_for_download(sample)
            return
        _start_inline_sample_download(resolved_sample)

    def _configure_sample_button_hover(button) -> None:
        def show_hover(_event) -> None:
            if getattr(button, "_cv_enabled", True):
                button.config(bg=_BUTTON_HOVER_BG)

        def clear_hover(_event) -> None:
            button.config(
                bg=(
                    _BUTTON_BG
                    if getattr(button, "_cv_enabled", True)
                    else DARK_THEME.secondary_button
                )
            )

        button.bind("<Enter>", show_hover)
        button.bind("<Leave>", clear_hover)

    def _create_map_library_section(parent, text: str, *, top_pad: int = 10) -> None:
        label = tk.Label(
            parent,
            text=text,
            font=_SMALL_FONT,
            fg=_INSTRUCTION_COLOR,
            bg=_PANEL_COLOR,
            anchor="w",
        )
        label.pack(anchor="w", fill="x", pady=(px(top_pad), px(6)))

    def _create_map_library_empty_note(
        parent,
        text: str,
        *,
        bottom_pad: int = 8,
    ) -> tk.Label:
        label = tk.Label(
            parent,
            text=text,
            font=_SMALL_FONT,
            fg="#5f606b",
            bg=_PANEL_COLOR,
            anchor="w",
            justify="left",
        )
        label.pack(anchor="w", fill="x", pady=(0, px(bottom_pad)))
        return label

    def _create_map_library_row(
        parent,
        *,
        title: str,
        detail: str,
        size_text: str,
        action_text: str,
        action,
        reserve_metadata: bool = False,
        reserve_progress: bool = False,
        menu_actions_factory=None,
    ) -> _MapLibraryRowWidgets:
        row_shell = tk.Frame(
            parent,
            bg=_PANEL_COLOR,
            highlightthickness=0,
        )
        row_shell.pack(fill="x", pady=(0, px(12)))

        row_content = tk.Frame(row_shell, bg=_PANEL_COLOR)
        row_content.pack(fill="x")

        leading_widget = _create_library_overflow_button(
            row_content,
            menu_actions_factory,
        )

        text_column = tk.Frame(row_content, bg=_PANEL_COLOR)
        text_column.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(px(12), px(8)),
            pady=px(5),
        )

        name_label = tk.Label(
            text_column,
            text=title,
            font=_SMALL_FONT,
            fg=_TITLE_COLOR,
            bg=_PANEL_COLOR,
            anchor="w",
            justify="left",
            wraplength=px(250),
        )
        name_label.pack(anchor="w", fill="x")

        metadata_text = detail or size_text
        metadata_label = None
        if metadata_text or reserve_metadata:
            metadata_label = tk.Label(
                text_column,
                text=metadata_text,
                font=_LIBRARY_METADATA_FONT,
                fg=_LIBRARY_METADATA_COLOR,
                bg=_PANEL_COLOR,
                anchor="w",
                justify="left",
            )
            metadata_label.pack(anchor="w", fill="x", pady=(px(2), 0))

        action_button = tk.Label(
            row_content,
            text=action_text,
            font=_SMALL_FONT,
            bg=_BUTTON_BG,
            fg=_BUTTON_FG,
            width=_LIBRARY_ACTION_BUTTON_WIDTH,
            padx=px(_LIBRARY_ACTION_BUTTON_PAD_X),
            pady=px(_LIBRARY_ACTION_BUTTON_PAD_Y),
            cursor="hand2",
            takefocus=True,
            highlightthickness=1,
            highlightbackground=_BUTTON_BORDER_COLOR,
            highlightcolor=_BUTTON_BORDER_COLOR,
        )
        _bind_activation(
            action_button,
            action,
        )
        _configure_sample_button_hover(action_button)
        action_button.pack(side="right", padx=(0, px(12)), pady=px(5))
        action_button._cv_enabled = True

        progress_bar_canvas = None
        progress_bar = None
        if reserve_progress:
            # Reserve the progress strip during initial layout. Starting a
            # download only redraws this already-packed canvas, so rows below
            # the active map do not shift when the download begins.
            progress_bar_canvas = tk.Canvas(
                row_shell,
                height=px(_LIBRARY_PROGRESS_HEIGHT),
                bg=_PANEL_COLOR,
                highlightthickness=0,
            )
            progress_bar_canvas.pack(
                fill="x",
                padx=px(12),
                pady=(0, px(_LIBRARY_PROGRESS_BOTTOM_PAD)),
            )
            progress_bar = progress_bar_canvas.create_rectangle(
                0,
                0,
                0,
                px(_LIBRARY_PROGRESS_HEIGHT),
                fill=_LIBRARY_PROGRESS_FILL_COLOR,
                width=0,
            )
        return _MapLibraryRowWidgets(
            row_shell=row_shell,
            leading_widget=leading_widget,
            action_button=action_button,
            metadata_label=metadata_label,
            progress_bar_canvas=progress_bar_canvas,
            progress_bar=progress_bar,
        )

    def _create_recent_map_row(parent, path: str) -> None:
        normalized = _recent_map_key(path)
        row_widgets = None
        title = _map_library_recent_title(path)

        def menu_actions(path=path, title=title):
            actions = [
                (
                    "Remove from this list",
                    lambda path=path: _remove_recent_map_from_splash(path),
                )
            ]
            if has_managed_map_cache(path):
                actions.append(
                    (
                        "Remove cache",
                        lambda path=path, title=title: _remove_map_cache_from_splash(
                            path,
                            title,
                            row_widgets,
                        ),
                    )
                )
            return tuple(actions)

        row_widgets = _create_map_library_row(
            parent,
            title=title,
            detail=_map_library_recent_detail_text(path),
            size_text="",
            action_text="Open",
            action=lambda path=path: _open_library_map_from_splash(path),
            menu_actions_factory=menu_actions,
        )
        recent_map_rows[normalized] = row_widgets

    def _create_available_map_row(parent, sample) -> None:
        downloaded = is_sample_map_already_downloaded(sample_maps_root_dir, sample)
        row_widgets = None

        def menu_actions(sample=sample):
            sample_path = _downloaded_sample_map_path(sample)
            if sample_path is None:
                return ()
            return (
                (
                    "Remove downloaded files",
                    lambda sample_path=sample_path, sample=sample: (
                        _remove_standard_library_download_from_splash(
                            sample,
                            sample_path,
                            row_widgets,
                        )
                    ),
                ),
            )

        row_widgets = _create_map_library_row(
            parent,
            title=sample.display_name,
            detail=_sample_map_status_text(sample, downloaded=downloaded),
            size_text="",
            action_text=_sample_map_splash_action_text(downloaded),
            action=lambda sample=sample: _on_sample_map_action(sample),
            reserve_metadata=True,
            reserve_progress=True,
            menu_actions_factory=menu_actions,
        )
        sample_map_rows[_sample_map_key(sample)] = row_widgets

    def _create_map_library_panel(parent) -> None:
        panel = tk.Frame(
            parent,
            bg=_PANEL_COLOR,
            highlightthickness=1,
            highlightbackground=_LIBRARY_PANEL_BORDER_COLOR,
            highlightcolor=_LIBRARY_PANEL_BORDER_COLOR,
        )
        panel.pack(fill="both", expand=True, pady=(px(34), px(24)))

        scrollbar_fraction = [(0.0, 1.0)]
        scrollbar_thumb = [None]
        scrollbar_drag_offset = [0.0]

        scroll_shell = tk.Frame(panel, bg=_PANEL_COLOR)
        scroll_shell.pack(fill="both", expand=True, padx=px(12), pady=(0, px(12)))

        content_canvas = tk.Canvas(
            scroll_shell,
            bg=_PANEL_COLOR,
            borderwidth=0,
            highlightthickness=0,
            yscrollcommand=lambda *_args: None,
        )
        content_scrollbar = tk.Canvas(
            scroll_shell,
            bg=_PANEL_COLOR,
            borderwidth=0,
            highlightthickness=0,
            width=_LIBRARY_SCROLLBAR_WIDTH,
            cursor="sb_v_double_arrow",
        )
        content_canvas.pack(side="left", fill="both", expand=True)

        rows_frame = tk.Frame(content_canvas, bg=_PANEL_COLOR)
        rows_window = content_canvas.create_window(
            (0, 0),
            window=rows_frame,
            anchor="nw",
        )

        def _draw_library_scrollbar_thumb() -> None:
            height = max(1, content_scrollbar.winfo_height())
            first, last = scrollbar_fraction[0]
            visible_fraction = max(0.0, min(1.0, last - first))
            if visible_fraction >= 1.0:
                if scrollbar_thumb[0] is not None:
                    content_scrollbar.delete(scrollbar_thumb[0])
                    scrollbar_thumb[0] = None
                return

            thumb_height = max(
                _LIBRARY_SCROLL_THUMB_MIN_HEIGHT,
                int(round(height * visible_fraction)),
            )
            travel = max(1, height - thumb_height)
            y0 = int(round(max(0.0, min(1.0, first)) * travel))
            y1 = min(height, y0 + thumb_height)
            x = _LIBRARY_SCROLLBAR_WIDTH // 2
            if scrollbar_thumb[0] is None:
                scrollbar_thumb[0] = content_scrollbar.create_line(
                    x,
                    y0,
                    x,
                    y1,
                    fill=_LIBRARY_SCROLL_THUMB_COLOR,
                    width=_LIBRARY_SCROLL_THUMB_WIDTH,
                    capstyle="round",
                )
            else:
                content_scrollbar.coords(scrollbar_thumb[0], x, y0, x, y1)

        def _set_library_scrollbar(first: str, last: str) -> None:
            scrollbar_fraction[0] = (float(first), float(last))
            _draw_library_scrollbar_thumb()

        content_canvas.configure(yscrollcommand=_set_library_scrollbar)

        def _sync_library_scrollbar() -> None:
            width = max(1, content_canvas.winfo_width())
            content_height = rows_frame.winfo_reqheight()
            content_canvas.configure(scrollregion=(0, 0, width, content_height))
            visible_height = content_canvas.winfo_height()
            if content_height > visible_height + 1:
                if not content_scrollbar.winfo_manager():
                    content_scrollbar.pack(side="right", fill="y")
            else:
                if content_scrollbar.winfo_manager():
                    content_scrollbar.pack_forget()
                content_canvas.yview_moveto(0)

        def _resize_library_canvas_window(event) -> None:
            content_canvas.itemconfigure(rows_window, width=event.width)
            _sync_library_scrollbar()

        def _scroll_library_content(event):
            if not content_scrollbar.winfo_manager():
                return None
            delta = getattr(event, "delta", 0)
            if delta:
                content_canvas.yview_scroll(int(-1 * (delta / 120)), "units")
            elif getattr(event, "num", None) == 4:
                content_canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                content_canvas.yview_scroll(1, "units")
            return "break"

        def _start_library_scrollbar_drag(event):
            first, last = scrollbar_fraction[0]
            height = max(1, content_scrollbar.winfo_height())
            visible_fraction = max(0.0, min(1.0, last - first))
            thumb_height = max(
                _LIBRARY_SCROLL_THUMB_MIN_HEIGHT,
                int(round(height * visible_fraction)),
            )
            travel = max(1, height - thumb_height)
            thumb_top = int(round(first * travel))
            thumb_bottom = thumb_top + thumb_height
            if thumb_top <= event.y <= thumb_bottom:
                scrollbar_drag_offset[0] = event.y - thumb_top
            else:
                scrollbar_drag_offset[0] = thumb_height / 2
                _drag_library_scrollbar(event)
            if scrollbar_thumb[0] is not None:
                content_scrollbar.itemconfigure(
                    scrollbar_thumb[0],
                    fill=_LIBRARY_SCROLL_THUMB_ACTIVE_COLOR,
                )
            return "break"

        def _drag_library_scrollbar(event):
            first, last = scrollbar_fraction[0]
            height = max(1, content_scrollbar.winfo_height())
            visible_fraction = max(0.0, min(1.0, last - first))
            thumb_height = max(
                _LIBRARY_SCROLL_THUMB_MIN_HEIGHT,
                int(round(height * visible_fraction)),
            )
            travel = max(1, height - thumb_height)
            thumb_top = max(0, min(travel, event.y - scrollbar_drag_offset[0]))
            content_canvas.yview_moveto(thumb_top / travel)
            return "break"

        def _end_library_scrollbar_drag(_event):
            if scrollbar_thumb[0] is not None:
                content_scrollbar.itemconfigure(
                    scrollbar_thumb[0],
                    fill=_LIBRARY_SCROLL_THUMB_COLOR,
                )
            return "break"

        def _bind_library_mousewheel(widget) -> None:
            widget.bind("<MouseWheel>", _scroll_library_content, add="+")
            widget.bind("<Button-4>", _scroll_library_content, add="+")
            widget.bind("<Button-5>", _scroll_library_content, add="+")
            for child in widget.winfo_children():
                _bind_library_mousewheel(child)

        library_scroll_sync["callback"] = _sync_library_scrollbar
        library_mousewheel_bind["callback"] = _bind_library_mousewheel

        content_canvas.bind("<Configure>", _resize_library_canvas_window, add="+")
        content_canvas.bind("<MouseWheel>", _scroll_library_content, add="+")
        content_canvas.bind("<Button-4>", _scroll_library_content, add="+")
        content_canvas.bind("<Button-5>", _scroll_library_content, add="+")
        content_scrollbar.bind(
            "<Configure>",
            lambda _event: _draw_library_scrollbar_thumb(),
            add="+",
        )
        content_scrollbar.bind(
            "<ButtonPress-1>",
            _start_library_scrollbar_drag,
            add="+",
        )
        content_scrollbar.bind("<B1-Motion>", _drag_library_scrollbar, add="+")
        content_scrollbar.bind(
            "<ButtonRelease-1>",
            _end_library_scrollbar_drag,
            add="+",
        )

        _create_map_library_section(rows_frame, "Your Library", top_pad=16)
        recent_container = tk.Frame(rows_frame, bg=_PANEL_COLOR)
        recent_container.pack(fill="x")
        recent_rows_container[0] = recent_container
        if recent_map_paths:
            for recent_path in recent_map_paths:
                _create_recent_map_row(recent_container, recent_path)
        else:
            recent_empty_note[0] = _create_map_library_empty_note(
                recent_container,
                "No maps added yet.",
                bottom_pad=18,
            )

        _create_map_library_section(rows_frame, "Standard Library")
        for sample in KNOWN_SAMPLE_MAPS:
            _create_available_map_row(rows_frame, sample)

        _bind_library_mousewheel(rows_frame)
        root.after_idle(_sync_library_scrollbar)
        _start_sample_catalog_fetch()

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
    root.after(200, lambda: root.attributes("-topmost", False))
    # The app-owned manager survives this Tk window and any intervening viewer.
    # Polling immutable snapshots keeps every widget mutation on the Tk thread.
    root.after(50, _refresh_update_presentation)
    root.after(350, update_manager.check_for_updates)
    root.bind("<Return>", lambda _event: on_open_map_folder())
    root.bind("<Escape>", on_close)
    bind_primary_shortcut(root, "w", on_close)
    root.protocol("WM_DELETE_WINDOW", on_close)

    update_manager.set_foreground_update_surface_active(True)
    try:
        root.mainloop()
    finally:
        update_manager.set_foreground_update_surface_active(False)

    # On macOS, keep the single Tk app object alive for the process lifetime so
    # the global app-menu About callback remains bound to a valid Tk
    # application. The next splash cycle reuses this hidden root instead of
    # creating a second Tk root. Destroying it here leaves a stale About
    # callback that can trigger "application has been destroyed" errors after
    # returning from the OpenGL viewer window.
    if sys.platform != "darwin":
        try:
            root.destroy()
        except Exception:
            pass  # already destroyed, or a background thread beat us to it

    return selected_folder[0]


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
