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
import subprocess
import sys
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
from caveviewer.gui.map_selection import (
    validate_selected_map_folder as _validate_selected_map_folder,
)
from caveviewer.gui.platform import get_splash_platform_adapter
from caveviewer.gui.platform import DesktopServices, get_desktop_services, tk_root_options
from caveviewer.gui.preference_paths import migrate_state_file, write_text_atomic
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
_INSTRUCTION_FONT = (_UI_FONT_FAMILY, 11) if _ROOMY_SPLASH_LAYOUT else _BODY_FONT
_FOOTER_FONT = (_UI_FONT_FAMILY, 9) if _ROOMY_SPLASH_LAYOUT else _SMALL_FONT
_LINK_FONT = (_UI_FONT_FAMILY, 10, "underline")
_BUTTON_FONT = (_UI_FONT_FAMILY, 13)
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


def _configure_runtime_tk_fonts(root) -> None:
    """Resolve the UI font against fonts Tk can actually render."""
    global _UI_FONT_FAMILY, _TITLE_FONT, _VERSION_FONT, _BODY_FONT
    global _SMALL_FONT, _INSTRUCTION_FONT, _FOOTER_FONT, _LINK_FONT, _BUTTON_FONT

    try:
        import tkinter.font as tkfont

        available = {family.lower(): family for family in tkfont.families(root)}
        preferred = [_PLATFORM_ADAPTER.ui_font_family()]
        if _LINUX_SPLASH_LAYOUT:
            fc_family = _fontconfig_sans_family()
            if fc_family:
                preferred.insert(0, fc_family)
            preferred.extend([
                "Adwaita Sans",
                "Cantarell",
                "Ubuntu Sans",
                "Ubuntu",
                "Noto Sans",
                "DejaVu Sans",
                "Liberation Sans",
                "sans-serif",
                "Sans",
            ])

        resolved_family = None
        for family in preferred:
            if not family:
                continue
            resolved_family = available.get(family.lower())
            if resolved_family:
                break

        if not resolved_family:
            fallback_family = tkfont.nametofont("TkDefaultFont").actual("family")
            if _LINUX_SPLASH_LAYOUT and str(fallback_family).lower() == "nimbus sans l":
                resolved_family = "sans-serif"
            else:
                resolved_family = fallback_family

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
    _INSTRUCTION_FONT = (_UI_FONT_FAMILY, 11) if _ROOMY_SPLASH_LAYOUT else _BODY_FONT
    _FOOTER_FONT = (_UI_FONT_FAMILY, 9) if _ROOMY_SPLASH_LAYOUT else _SMALL_FONT
    _LINK_FONT = (_UI_FONT_FAMILY, 10, "underline")
    _BUTTON_FONT = (_UI_FONT_FAMILY, 13)


def _fontconfig_sans_family() -> str | None:
    """Return fontconfig's preferred Linux sans-serif family."""
    try:
        result = subprocess.run(
            ["fc-match", "--format=%{family[0]}", "sans-serif:style=Regular"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        family = result.stdout.strip()
        return family or None
    except Exception:
        return None


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

    window_w, window_h = px(500), px(_SPLASH_WINDOW_MIN_HEIGHT)

    # Center the window on screen rather than letting the OS place it
    # arbitrarily -- a first-launch splash screen appearing somewhere
    # random/off-center is a small but noticeable rough edge.
    root.update_idletasks()
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    pos_x = (screen_w - window_w) // 2
    pos_y = (screen_h - window_h) // 3  # slightly above true vertical center, reads better
    root.geometry(f"{window_w}x{window_h}+{pos_x}+{pos_y}")

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
        logo_label = tk.Label(root, image=logo_photo, bg=_BG_COLOR, borderwidth=0)
        logo_label.image = logo_photo  # keep a reference so it isn't garbage-collected
        logo_label.pack(pady=(22, 6))

    # -- title + version, centered top -------------------------------------------
    title_label = tk.Label(
        root, text=program_name, font=_TITLE_FONT,
        fg=_TITLE_COLOR, bg=_BG_COLOR,
    )
    title_label.pack(pady=(0, 0))

    version_label = tk.Label(
        root, text=f"Version {version}", font=_VERSION_FONT,
        fg=_SUBTITLE_COLOR, bg=_BG_COLOR,
    )
    version_label.pack(pady=(0, 8))

    splash_state = {"closing": False}
    last_update_presentation: list[_UpdatePresentation | None] = [None]

    # Status and action labels stay packed even when empty. State changes never
    # resize the splash, and keyboard focus is enabled only for active actions.
    update_label = tk.Label(
        root,
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
        root,
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
        root,
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
        root,
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
        root,
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

    # Example maps link - opens the sample maps dialog
    def _on_example_maps_click():
        from caveviewer.gui.sample_maps import default_sample_maps_install_dir
        from caveviewer.gui.sample_maps_dialog import show_sample_maps_dialog
        result = show_sample_maps_dialog(
            root,
            default_sample_maps_install_dir(),
            ui_font_family=_UI_FONT_FAMILY,
            desktop_services=desktop_services,
        )
        if result:
            selected_folder[0] = result
            _leave_splash()

    secondary_link_row = tk.Frame(root, bg=_BG_COLOR)
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

    secondary_separator = tk.Label(
        secondary_link_row,
        text="   |   ",
        font=_SMALL_FONT,
        fg="#3f4a5c",
        bg=_BG_COLOR,
    )
    secondary_separator.pack(side="left")

    sample_maps_link = tk.Label(
        secondary_link_row,
        text="Download sample maps",
        font=_SMALL_FONT,
        fg=_BUTTON_BG,
        bg=_BG_COLOR,
        cursor="hand2",
        takefocus=True,
        highlightthickness=1,
        highlightbackground=_BG_COLOR,
        highlightcolor=_BUTTON_BG,
    )
    _bind_activation(sample_maps_link, _on_example_maps_click)
    sample_maps_link.pack(side="left")

    credit_label = tk.Label(
        root,
        text=_CREDITS_TEXT,
        font=_FOOTER_FONT,
        fg="#5f606b",
        bg=_BG_COLOR,
        justify="center",
    )
    credit_label.pack(pady=(0, _FOOTER_CREDITS_BOTTOM_PAD))

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
