"""
gui/splash_screen.py

The very first thing shown when CaveViewer launches: a small landing
window with the program name/version, the skull logo, and a Browse
button to pick the folder containing the cave map's .obj/.mtl/texture
files -- replacing the old behavior of jumping straight into a bare
native folder-picker dialog with zero context about what the program
even is.

Built with Tkinter (ships with standard Python on Windows/Mac, same
reasoning as the existing native folder-picker dialog already used
elsewhere in caveviewer.py -- no extra install needed). Styled to loosely
match the in-program overlays' dark background + amber accent look,
though Tkinter's native widgets can only approximate that so closely --
this is a real OS window with title bar and native buttons, not a custom-
drawn OpenGL overlay like the rest of the program's UI.

This is intentionally a SEPARATE function from pick_folder_dialog() in
caveviewer.py (which stays a quick bare native dialog) -- the splash
screen is for the very first launch, when the person hasn't seen the
program yet and benefits from the context; the OPEN button mid-session
(see viewer_window.py) is for someone already using the program, where a
quick plain dialog is the better fit and a full splash screen would just
be unnecessary ceremony.

This window also hosts inline update UX (check, availability, download
progress, and install handoff) so update actions remain in one surface
without extra modal pop-ups.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile
import threading
from caveviewer_version import APP_NAME, APP_VERSION
from core.logging_utils import get_logger
from gui.advanced_settings import (
    ADVANCED_SETTING_COLUMNS as _ADVANCED_SETTING_COLUMNS,
    ADVANCED_SETTING_FIELDS as _ADVANCED_SETTING_FIELDS,
    apply_advanced_settings_to_env as _apply_advanced_settings_to_env,
    effective_advanced_settings as _effective_advanced_settings,
    load_advanced_settings as _load_advanced_settings,
    save_advanced_settings as _save_advanced_settings,
    validate_advanced_settings as _validate_advanced_settings,
)
from gui.dpi_utils import apply_tk_scaling, configure_process_dpi_awareness, tk_display_scale
from gui.platform import get_splash_platform_adapter
from gui.preferences import migrate_preference_file


# Resolve asset paths for both dev and PyInstaller bundle environments
def _resolve_asset_path(filename: str) -> str | None:
    """Resolve asset file path, supporting both dev and bundled (PyInstaller) environments."""
    paths_to_try = []
    
    # 1. Try standard dev path first
    gui_dir = os.path.dirname(os.path.abspath(__file__))
    dev_path = os.path.join(gui_dir, "assets", filename)
    paths_to_try.append(dev_path)
    
    # 2. In PyInstaller bundle, check _MEIPASS (the bundle root)
    if hasattr(sys, "frozen") and hasattr(sys, "_MEIPASS"):
        bundle_path = os.path.join(sys._MEIPASS, "gui", "assets", filename)  # type: ignore
        paths_to_try.append(bundle_path)
    
    # 3. Try relative to the executable location
    if hasattr(sys, "executable"):
        exe_dir = os.path.dirname(sys.executable)
        exe_relative_path = os.path.join(exe_dir, "gui", "assets", filename)
        paths_to_try.append(exe_relative_path)
        
        # Also try parent directory of exe (in case exe is in a subdir)
        exe_parent = os.path.dirname(exe_dir)
        parent_relative_path = os.path.join(exe_parent, "gui", "assets", filename)
        paths_to_try.append(parent_relative_path)
    
    # 4. Try relative to sys.argv[0] (the invoked script/binary path)
    if sys.argv and sys.argv[0]:
        argv0_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        argv0_relative_path = os.path.join(argv0_dir, "gui", "assets", filename)
        paths_to_try.append(argv0_relative_path)
    
    # Return the first path that exists
    for path in paths_to_try:
        if os.path.exists(path):
            return path
    
    return None


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
_LAST_BROWSE_PATH_FILE = migrate_preference_file("last_browse_path", ".caveviewer_last_browse_path")

# URL for example maps link -- empty/None means link is disabled
_EXAMPLE_MAPS_URL = None
_LOG = get_logger("CaveViewer")

_BG_COLOR = "#0a0a0d"           # near-black, matches the in-app overlay backgrounds
_PANEL_COLOR = "#12121a"        # slightly lighter panel background
_TITLE_COLOR = "#f2d98c"        # amber/gold, matches the in-app title text color
_SUBTITLE_COLOR = "#cccdd6"     # light gray, matches in-app subtitle/body text
_INSTRUCTION_COLOR = "#9a9aa6"  # dimmer gray, matches in-app secondary/note text
_BUTTON_BG = "#e5a11f"          # calmer amber derived from the splash logo gold
_BUTTON_HOVER_BG = "#f0b13a"    # brighter hover state in the same logo-gold family
_BUTTON_BORDER_COLOR = "#9c6f18" # subtle darker amber edge for action buttons
_BUTTON_FG = "#1a1408"          # dark text on the amber button, matches in-app active-button text
_BORDER_COLOR = "#5c5c6e"
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
_ADVANCED_DIALOG_TWO_COLUMN = True
_ADVANCED_DIALOG_WRAP = 620 if sys.platform == "win32" else 340
_ADVANCED_DIALOG_ENTRY_WIDTH = 42 if sys.platform == "win32" else 22
_ADVANCED_DIALOG_NUMERIC_ENTRY_WIDTH = 8
_ADVANCED_DIALOG_BODY_PAD_X = 18 if sys.platform == "darwin" else (32 if sys.platform == "win32" else 24)
_ADVANCED_DIALOG_SECTION_GAP = 44 if sys.platform == "win32" else 18
# Linux/Tk's requested width is noticeably tighter than the macOS rendering,
# especially with two setting columns. Give it some horizontal breathing room;
# the geometry code still clamps this to the available screen width.
_ADVANCED_DIALOG_MIN_WIDTH = (
    1320 if sys.platform == "win32" else
    1040 if sys.platform.startswith("linux") else
    0
)
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


def _has_precompiled_cache(folder: str) -> bool:
    try:
        from core import chunker as _ck
    except Exception:
        return False

    # Support both layouts used by caveviewer.py:
    # 1) <map folder>/_cache/manifest.json
    # 2) <map folder>/.caveviewer_cache/manifest.json (legacy)
    # 3) <selected folder>/manifest.json (folder is cache root)
    candidates = [
        os.path.join(folder, _ck.CACHE_DIRNAME),
        os.path.join(folder, _ck.LEGACY_CACHE_DIRNAME),
        folder,
    ]
    for candidate in candidates:
        if os.path.exists(os.path.join(candidate, _ck.MANIFEST_NAME)):
            return True
    return False


def _validate_selected_map_folder(folder: str) -> tuple[bool, str]:
    if not folder or not os.path.isdir(folder):
        return False, "The selected path is not a valid folder."

    glb_candidates = glob.glob(os.path.join(folder, "*.glb"))
    if glb_candidates:
        return True, ""

    obj_candidates = glob.glob(os.path.join(folder, "*.obj"))
    if obj_candidates:
        obj_path = obj_candidates[0]
        mtl_name = None
        try:
            with open(obj_path, "r", errors="replace") as f:
                for line in f:
                    if line.startswith("mtllib "):
                        mtl_name = line.split(maxsplit=1)[1].strip()
                        break
        except Exception:
            # If the OBJ can't be inspected, continue with fallback checks.
            pass

        if mtl_name and os.path.exists(os.path.join(folder, mtl_name)):
            return True, ""

        if glob.glob(os.path.join(folder, "*.mtl")):
            return True, ""

        if _has_precompiled_cache(folder):
            return True, ""

        return False, (
            "Found an .obj file, but no matching .mtl file in that folder.\n\n"
            "Select a folder with a .glb file, or with both .obj and .mtl files, "
            "or a folder that already contains a CaveViewer pre-compiled cache."
        )

    if _has_precompiled_cache(folder):
        return True, ""

    return False, (
        "No supported map files were found in that folder.\n\n"
        "Select a folder with a .glb file, or with both .obj and .mtl files, "
        "or a folder that already contains a CaveViewer pre-compiled cache."
    )


def show_splash_screen(program_name: str = APP_NAME, version: str = APP_VERSION) -> str | None:
    """
    Shows the launch splash screen and blocks until the person either
    picks a folder (Browse -> select a folder -> OK) or closes the
    window. Returns the selected folder path, or None if the window was
    closed without picking one.
    """
    import tkinter as tk
    from tkinter import filedialog

    selected_folder: list[str | None] = [None]
    advanced_settings = _effective_advanced_settings(_load_advanced_settings())
    _apply_advanced_settings_to_env(advanced_settings)

    configure_process_dpi_awareness()
    root = tk.Tk(className=APP_NAME)
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

    update_state = {
        "result": None,
        "downloaded_payload": None,
        "mounted_payload": None,
        "mounted_app": None,
        "busy": False,
        "check_user_initiated": False,
    }

    # Update status label — always packed so its height is always included in
    # the window size.  State changes are text/color/cursor swaps only; no
    # widgets are ever added or removed, so the window never needs to resize.
    update_label = tk.Label(
        root,
        text="",
        font=_SMALL_FONT,
        fg=_INSTRUCTION_COLOR,
        bg=_BG_COLOR,
        cursor="arrow",
    )
    update_label.pack(pady=(0, 4))

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

    def _set_update_label(text, fg=_INSTRUCTION_COLOR, clickable=False):
        update_label.config(text=text, fg=fg,
                            cursor="hand2" if clickable else "arrow")

    def _set_progress_bar_visible(visible: bool):
        update_progress_canvas.config(bg="#1c1c24" if visible else _BG_COLOR)
        if not visible:
            update_progress_canvas.coords(_update_progress_bar, 0, 0, 0, 4)

    def _set_progress(frac: float):
        clamped = max(0.0, min(1.0, float(frac)))
        update_progress_canvas.coords(_update_progress_bar, 0, 0, int(300 * clamped), 4)

    def _close_and_install():
        root.withdraw()
        root.quit()

    def _on_download_complete(payload_path: str):
        update_state["downloaded_payload"] = payload_path
        update_state["busy"] = False
        _set_progress_bar_visible(False)
        try:
            manual_install = _PLATFORM_ADAPTER.prepare_manual_install(payload_path)
            update_state["mounted_payload"] = manual_install.mounted_payload_path
            update_state["mounted_app"] = manual_install.mounted_app_path
            _set_update_label("Update ready  \u2014  click to quit & install",
                              fg=_BUTTON_BG, clickable=True)
            update_label.bind("<Button-1>", lambda _e: _close_and_install())
        except Exception:
            _set_update_label("Downloaded  \u2014  close app to install manually",
                              fg=_SUBTITLE_COLOR)

    def _download_error_looks_security_related(err: str) -> bool:
        text = (err or "").lower()
        return any(
            marker in text
            for marker in (
                "hash",
                "sha-256",
                "sha256",
                "tampered",
                "size",
                "corrupt",
                "security",
            )
        )

    def _on_download_error(err: str):
        update_state["busy"] = False
        _set_progress_bar_visible(False)
        if _download_error_looks_security_related(err):
            _LOG.warning("Update download stopped by security check: %s", err)
            _set_update_label("Download verification failed", fg="#ff9b90")
            return
        _set_update_label("\u2193  Download failed \u2014 click to retry",
                          fg="#ff9b90", clickable=True)

    def _start_download(_event=None):
        result = update_state.get("result")
        if not result or update_state.get("busy"):
            return
        update_state["busy"] = True
        _set_update_label("Downloading\u2026", fg=_INSTRUCTION_COLOR)
        _set_progress_bar_visible(True)
        _set_progress(0.0)

        def worker():
            from gui.update_checker import download_update

            download_dir = tempfile.mkdtemp(prefix="caveviewer_update_")
            payload_path = os.path.join(download_dir, "update_payload.bin")

            def on_progress(downloaded, total):
                frac = min(1.0, downloaded / total) if total else 0.0
                pct = int(frac * 100)

                def ui_update():
                    _set_update_label(f"Downloading\u2026 {pct}%",
                                      fg=_INSTRUCTION_COLOR)
                    _set_progress(frac)

                root.after(0, ui_update)

            try:
                download_update(
                    result.download_url,
                    result.download_size_bytes,
                    payload_path,
                    expected_sha256=result.download_sha256,
                    progress_cb=on_progress,
                )
                final_payload_path = _PLATFORM_ADAPTER.persist_downloaded_payload(
                    payload_path, result.download_url
                )
                _LOG.info("Update payload saved for installation: %s", final_payload_path)
                root.after(0, lambda: _on_download_complete(final_payload_path))
            except Exception as e:
                err = str(e)
                _LOG.warning(
                    "Update download workflow failed: %s: %s",
                    type(e).__name__,
                    err,
                )
                root.after(0, lambda err=err: _on_download_error(err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_check_result(result):
        update_state["busy"] = False
        if result.error:
            if update_state.get("check_user_initiated"):
                _set_update_label("Your app is up to date.")
            return

        if not result.update_available:
            if update_state.get("check_user_initiated"):
                _set_update_label("Your app is up to date.")
            return

        update_state["result"] = result
        _set_update_label("\u2193  Download Update", fg="#8ab4ff", clickable=True)
        update_label.bind("<Button-1>", _start_download)

    def _start_check_updates(*, user_initiated: bool = False):
        if update_state["busy"]:
            return
        update_state["busy"] = True
        update_state["check_user_initiated"] = bool(user_initiated)
        if user_initiated:
            _set_update_label("Checking for updates\u2026")

        def worker():
            from gui.update_checker import check_for_update

            result = check_for_update(
                current_version=version,
                install_channel=_PLATFORM_ADAPTER.install_channel(),
            )
            root.after(0, lambda: _on_check_result(result))

        threading.Thread(target=worker, daemon=True).start()

    # -- browse button + instructions ---------------------------------------------
    def _show_invalid_map_dialog(message: str) -> None:
        dialog = tk.Toplevel(root)
        dialog.title("Map Not Found")
        dialog.configure(bg=_BG_COLOR)
        dialog.resizable(False, False)
        dialog.transient(root)

        body = tk.Frame(dialog, bg=_BG_COLOR, padx=18, pady=16)
        body.pack(fill="both", expand=True)

        tk.Label(
            body,
            text="Unable to Open This Folder",
            font=_VERSION_FONT,
            fg=_TITLE_COLOR,
            bg=_BG_COLOR,
        ).pack(anchor="w", pady=(0, 8))

        tk.Label(
            body,
            text=message,
            font=_BODY_FONT,
            fg=_SUBTITLE_COLOR,
            bg=_BG_COLOR,
            justify="left",
            wraplength=420,
        ).pack(anchor="w")

        button_row = tk.Frame(body, bg=_BG_COLOR)
        button_row.pack(fill="x", pady=(14, 0))

        def _dismiss(_event=None):
            dialog.destroy()
            return "break"

        if sys.platform == "darwin":
            ok_button = tk.Label(
                button_row,
                text="OK",
                font=_SMALL_FONT,
                bg=_BUTTON_BG,
                fg=_BUTTON_FG,
                padx=16,
                pady=6,
                cursor="hand2",
                takefocus=True,
                highlightthickness=1,
                highlightbackground=_BUTTON_BORDER_COLOR,
                highlightcolor=_BUTTON_BORDER_COLOR,
            )
            ok_button.bind("<Button-1>", _dismiss)
            ok_button.bind("<Return>", _dismiss)
            ok_button.bind("<space>", _dismiss)
            ok_button.bind("<Enter>", lambda _event: ok_button.config(bg=_BUTTON_HOVER_BG))
            ok_button.bind("<Leave>", lambda _event: ok_button.config(bg=_BUTTON_BG))
        else:
            ok_button = tk.Button(
                button_row,
                text="OK",
                command=lambda: _dismiss(),
                font=_SMALL_FONT,
                bg=_BUTTON_BG,
                fg=_BUTTON_FG,
                activebackground=_BUTTON_HOVER_BG,
                activeforeground=_BUTTON_FG,
                relief="flat",
                borderwidth=1,
                highlightthickness=1,
                highlightbackground=_BUTTON_BORDER_COLOR,
                highlightcolor=_BUTTON_BORDER_COLOR,
                padx=16,
                pady=6,
                cursor="hand2",
                default="active",
            )

        ok_button.pack(side="right")

        dialog.bind("<Escape>", _dismiss)
        dialog.bind("<Return>", _dismiss)

        dialog.update_idletasks()
        try:
            root.update_idletasks()
            dialog_w = dialog.winfo_reqwidth()
            dialog_h = dialog.winfo_reqheight()
            x = root.winfo_rootx() + max(0, (root.winfo_width() - dialog_w) // 2)
            y = root.winfo_rooty() + max(0, (root.winfo_height() - dialog_h) // 2)
            dialog.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")
        except Exception:
            pass

        dialog.wait_visibility()
        dialog.grab_set()
        ok_button.focus_set()
        dialog.focus_force()
        dialog.wait_window()

    def on_browse():
        dialog_kwargs = {
            "title": "Select a cave map folder",
        }
        last_dir = _load_last_browse_dir()
        if last_dir:
            dialog_kwargs["initialdir"] = last_dir

        folder = filedialog.askdirectory(**dialog_kwargs)
        if folder:
            is_valid, error_message = _validate_selected_map_folder(folder)
            if not is_valid:
                _show_invalid_map_dialog(error_message)
                return

            selected_folder[0] = folder
            _save_last_browse_dir(folder)
            root.withdraw()
            root.quit()

    def on_close(_event=None):
        root.withdraw()
        root.quit()

    browse_button = tk.Label(
        root,
        text="Select Map...",
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
    browse_button.bind("<Button-1>", lambda _event: on_browse())
    browse_button.bind("<Return>", lambda _event: on_browse())
    browse_button.bind("<space>", lambda _event: on_browse())
    browse_button.bind("<Enter>", lambda _event: browse_button.config(bg=_BUTTON_HOVER_BG))
    browse_button.bind("<Leave>", lambda _event: browse_button.config(bg=_BUTTON_BG))
    browse_button.pack(pady=(_TITLE_TO_ACTION_GAP, _BROWSE_BUTTON_BOTTOM_GAP))

    instruction_label = tk.Label(
        root,
        text="Point this to a folder with your map's .obj+.mtl or .glb file,\n"
             "or a folder that was already imported by CaveViewer.",
        font=_INSTRUCTION_FONT,
        fg=_INSTRUCTION_COLOR, bg=_BG_COLOR,
        justify="center",
    )
    instruction_label.pack(pady=(0, _INSTRUCTION_BOTTOM_GAP))

    def _show_advanced_settings_dialog() -> None:
        nonlocal advanced_settings
        advanced_settings = _effective_advanced_settings(_load_advanced_settings())
        _apply_advanced_settings_to_env(advanced_settings)

        dialog = tk.Toplevel(root)
        dialog.withdraw()
        dialog.title("Advanced Settings")
        dialog.configure(bg=_BG_COLOR)
        dialog.resizable(False, False)
        dialog.transient(root)

        # Keep Fedora/Linux DPI behavior from making this dense, two-column
        # form feel oversized. These fonts are local to Advanced Settings;
        # the splash screen and other Tk dialogs retain their existing fonts.
        if _LINUX_SPLASH_LAYOUT:
            advanced_section_font = (_UI_FONT_FAMILY, 10)
            advanced_body_font = (_UI_FONT_FAMILY, 10)
            advanced_small_font = (_UI_FONT_FAMILY, 9)
            advanced_field_gap = 14
            advanced_entry_pad_y = 6
            advanced_section_pad_y = 15
        else:
            advanced_section_font = _VERSION_FONT
            advanced_body_font = _BODY_FONT
            advanced_small_font = _SMALL_FONT
            advanced_field_gap = 9
            advanced_entry_pad_y = 4
            advanced_section_pad_y = 12

        body = tk.Frame(dialog, bg=_BG_COLOR, padx=_ADVANCED_DIALOG_BODY_PAD_X, pady=18)
        body.pack(fill="both", expand=True)

        field_vars: dict[str, tk.StringVar] = {}
        field_entries: dict[str, tk.Entry] = {}
        effective_settings = _effective_advanced_settings(advanced_settings)

        def _is_numeric_entry_candidate(value_type: str, candidate: str) -> bool:
            if candidate == "":
                return True
            if value_type == "int":
                return candidate.isdigit()
            if value_type == "float":
                if candidate == ".":
                    return True
                if candidate.count(".") > 1:
                    return False
                return all(ch.isdigit() or ch == "." for ch in candidate)
            return True

        numeric_entry_validator = dialog.register(_is_numeric_entry_candidate)

        def _compact_directory_path(path: str, max_chars: int = 42) -> str:
            """Keep a saved directory recognizable without showing a long absolute path."""
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
                prefix = "~" if display.startswith("~" + os.sep) else (drive + os.sep if drive else os.sep)
                compact = prefix + "…" + os.sep + suffix
                if len(compact) <= max_chars:
                    return compact
            return "…" + display[-(max_chars - 1):]

        def _clear_field_error(key: str) -> None:
            entry = field_entries.get(key)
            if entry is not None:
                entry.config(highlightbackground="#30303a", highlightcolor="#5d6f8a")
            if error_label.winfo_exists():
                _set_advanced_error("")

        section_row = tk.Frame(body, bg=_BG_COLOR)
        section_row.pack(fill="both", expand=True, pady=(0, 10))

        def _button_label(parent, text, command, bg="#2a2a33", fg=_SUBTITLE_COLOR,
                          hover_bg="#33333f", padx=10, border_color="#3a4454"):
            label = tk.Label(
                parent,
                text=text,
                font=advanced_small_font,
                bg=bg,
                fg=fg,
                padx=padx,
                pady=5,
                cursor="hand2",
                takefocus=True,
                highlightthickness=1,
                highlightbackground=border_color,
                highlightcolor=border_color,
            )

            def _invoke(_event=None):
                command()
                return "break"

            label.bind("<Button-1>", _invoke)
            label.bind("<Return>", _invoke)
            label.bind("<space>", _invoke)
            label.bind("<Enter>", lambda _event: label.config(bg=hover_bg))
            label.bind("<Leave>", lambda _event: label.config(bg=bg))
            return label

        def render_section(parent, title: str, section_key: str) -> None:
            section = tk.Frame(
                parent,
                bg=_BG_COLOR,
                padx=14,
                pady=advanced_section_pad_y,
                highlightthickness=1,
                highlightbackground=_BORDER_COLOR,
                highlightcolor=_BORDER_COLOR,
            )
            section.pack(fill="both", expand=(section_key == "streaming"), pady=(0, 12))

            tk.Label(
                section,
                text=title,
                font=advanced_section_font,
                fg=_TITLE_COLOR,
                bg=_BG_COLOR,
            ).pack(anchor="w", pady=(0, 10))

            fields = [field for field in _ADVANCED_SETTING_FIELDS if field.get("section") == section_key]
            for field in fields:
                row = tk.Frame(section, bg=_BG_COLOR)
                row.pack(fill="x", pady=(0, advanced_field_gap))

                tk.Label(
                    row,
                    text=field["label"],
                    font=advanced_body_font,
                    fg=_SUBTITLE_COLOR,
                    bg=_BG_COLOR,
                    anchor="w",
                ).pack(anchor="w")

                # macOS keeps prior splash Tk roots alive for the app-menu About
                # handler, so implicit StringVars can attach to an old hidden
                # root after returning from the viewer. Bind each variable to
                # this dialog's root so entry defaults render reliably.
                var = tk.StringVar(master=dialog, value=effective_settings[field["key"]])
                field_vars[field["key"]] = var
                value_type = field.get("value_type", "")
                entry_width = (
                    _ADVANCED_DIALOG_NUMERIC_ENTRY_WIDTH
                    if value_type in {"int", "float"}
                    else _ADVANCED_DIALOG_ENTRY_WIDTH
                )
                compact_path = field["key"] == "recording_dir"
                entry_var = var
                if compact_path:
                    entry_var = tk.StringVar(master=dialog, value=_compact_directory_path(var.get()))
                    var.trace_add(
                        "write",
                        lambda *_args, source=var, display=entry_var: display.set(
                            _compact_directory_path(source.get())
                        ),
                    )
                entry_parent = row
                entry_pack_options = {
                    "anchor": "w",
                    "pady": (advanced_entry_pad_y, advanced_entry_pad_y),
                }
                if value_type in {"path", "path_create"}:
                    entry_parent = tk.Frame(row, bg=_BG_COLOR)
                    entry_parent.pack(
                        fill="x",
                        pady=(advanced_entry_pad_y, advanced_entry_pad_y),
                    )
                    entry_pack_options = {"side": "left", "fill": "x", "expand": True}

                entry = tk.Entry(
                    entry_parent,
                    textvariable=entry_var,
                    font=advanced_body_font,
                    bg="#1c1c24",
                    fg=_SUBTITLE_COLOR,
                    insertbackground=_SUBTITLE_COLOR,
                    relief="flat",
                    highlightthickness=1,
                    highlightbackground="#30303a",
                    highlightcolor="#5d6f8a",
                    width=entry_width,
                    state="readonly" if compact_path else "normal",
                    readonlybackground="#1c1c24",
                    validate="none" if compact_path else "key",
                    validatecommand=(numeric_entry_validator, value_type, "%P"),
                )
                field_entries[field["key"]] = entry
                var.trace_add("write", lambda *_args, key=field["key"]: _clear_field_error(key))
                entry.pack(**entry_pack_options)

                if value_type in {"path", "path_create"}:
                    def choose_directory(var=var, title=field["label"]):
                        initial_dir = os.path.expanduser(var.get().strip() or "~")
                        if not os.path.isdir(initial_dir):
                            initial_dir = os.path.dirname(initial_dir)
                        if not os.path.isdir(initial_dir):
                            initial_dir = os.path.expanduser("~")
                        folder = filedialog.askdirectory(
                            title=title,
                            initialdir=initial_dir,
                            parent=dialog,
                        )
                        if folder:
                            var.set(folder)

                    browse_button = _button_label(
                        entry_parent,
                        "Browse",
                        choose_directory,
                        padx=8,
                    )
                    browse_button.pack(side="left", padx=(8, 0))

                single_line_hint = field["key"] == "recording_dir"
                hint_label = tk.Label(
                    row,
                    text=field["hint"],
                    font=advanced_small_font,
                    fg=_INSTRUCTION_COLOR,
                    bg=_BG_COLOR,
                    justify="left",
                    anchor="w",
                    wraplength=0 if single_line_hint else _ADVANCED_DIALOG_WRAP,
                )
                hint_label.pack(anchor="w", fill="x")

                if sys.platform.startswith("linux") and not single_line_hint:
                    # Tk does not expand a label's fixed wraplength when its
                    # parent column grows. Use the real row width so hints fill
                    # the available line before wrapping onto the next one.
                    def resize_hint(event, label=hint_label):
                        wraplength = max(200, event.width - 4)
                        if int(label.cget("wraplength")) != wraplength:
                            label.configure(wraplength=wraplength)

                    row.bind("<Configure>", resize_hint, add="+")

        for column_index, sections in enumerate(_ADVANCED_SETTING_COLUMNS):
            column = tk.Frame(section_row, bg=_BG_COLOR)
            if _ADVANCED_DIALOG_TWO_COLUMN:
                half_gap = _ADVANCED_DIALOG_SECTION_GAP // 2
                pad_left = half_gap if column_index > 0 else 0
                pad_right = half_gap if column_index < len(_ADVANCED_SETTING_COLUMNS) - 1 else 0
                section_row.grid_columnconfigure(
                    column_index,
                    weight=1,
                    uniform="advanced_settings_column",
                )
                column.grid(
                    row=0,
                    column=column_index,
                    sticky="nsew",
                    padx=(pad_left, pad_right),
                )
            else:
                column.pack(fill="x")
            for section_key, section_title in sections:
                render_section(column, section_title, section_key)

        button_row = tk.Frame(body, bg=_BG_COLOR)
        error_parent = button_row if _LINUX_SPLASH_LAYOUT else body
        error_label = tk.Label(
            error_parent,
            text="",
            font=advanced_small_font,
            fg="#ff9b90",
            bg=_BG_COLOR,
            justify="left",
            anchor="w",
            wraplength=620 if _LINUX_SPLASH_LAYOUT else _ADVANCED_DIALOG_WRAP,
        )
        if _LINUX_SPLASH_LAYOUT:
            button_row.pack(fill="x")
            error_label.pack(side="left", fill="x", expand=True, padx=(0, 12))
        else:
            error_label.pack(anchor="w", pady=(4, 10))
            button_row.pack(fill="x")

        def _advanced_natural_height() -> int:
            """Natural client height without space assigned by pack expansion."""
            height = 36  # body's 18px top and bottom padding
            height += section_row.winfo_reqheight() + 10  # section-row bottom pad
            height += button_row.winfo_reqheight()
            return height

        def _set_advanced_error(message: str) -> None:
            error_label.config(text=message)

        def on_cancel():
            dialog.destroy()

        def on_apply():
            for entry in field_entries.values():
                entry.config(highlightbackground="#30303a", highlightcolor="#5d6f8a")
            proposed = {key: var.get() for key, var in field_vars.items()}
            ok, message, normalized, error_key = _validate_advanced_settings(proposed)
            if not ok:
                _set_advanced_error(message or "Invalid advanced settings.")
                if error_key and error_key in field_entries:
                    bad_entry = field_entries[error_key]
                    bad_entry.config(highlightbackground="#ff6b6b", highlightcolor="#ff6b6b")
                    bad_entry.focus_set()
                    bad_entry.selection_range(0, "end")
                return

            advanced_settings = _effective_advanced_settings(normalized)
            _apply_advanced_settings_to_env(advanced_settings)
            _save_advanced_settings(advanced_settings)
            dialog.destroy()

        if sys.platform == "darwin":
            def _make_action_label(parent, text, command, bg, fg, hover_bg, padx, border_color):
                label = tk.Label(
                    parent,
                    text=text,
                    font=advanced_small_font,
                    bg=bg,
                    fg=fg,
                    padx=padx,
                    pady=6,
                    cursor="hand2",
                    takefocus=True,
                    highlightthickness=1,
                    highlightbackground=border_color,
                    highlightcolor=border_color,
                )

                def _invoke(_event=None):
                    command()
                    return "break"

                label.bind("<Button-1>", _invoke)
                label.bind("<Return>", _invoke)
                label.bind("<space>", _invoke)
                label.bind("<Enter>", lambda _event: label.config(bg=hover_bg))
                label.bind("<Leave>", lambda _event: label.config(bg=bg))
                return label

            cancel_button = _make_action_label(
                button_row,
                text="Cancel",
                command=on_cancel,
                bg="#2a2a33",
                fg=_SUBTITLE_COLOR,
                hover_bg="#33333f",
                padx=12,
                border_color="#3a4454",
            )
            apply_button = _make_action_label(
                button_row,
                text="Apply",
                command=on_apply,
                bg=_BUTTON_BG,
                fg=_BUTTON_FG,
                hover_bg=_BUTTON_HOVER_BG,
                padx=16,
                border_color=_BUTTON_BORDER_COLOR,
            )
        else:
            cancel_button = tk.Button(
                button_row,
                text="Cancel",
                command=on_cancel,
                font=advanced_small_font,
                bg="#2a2a33",
                fg=_SUBTITLE_COLOR,
                activebackground="#33333f",
                activeforeground=_SUBTITLE_COLOR,
                relief="flat",
                borderwidth=1,
                highlightthickness=1,
                highlightbackground="#3a4454",
                highlightcolor="#3a4454",
                padx=12,
                pady=6,
                cursor="hand2",
            )

            apply_button = tk.Button(
                button_row,
                text="Apply",
                command=on_apply,
                font=advanced_small_font,
                bg=_BUTTON_BG,
                fg=_BUTTON_FG,
                activebackground=_BUTTON_HOVER_BG,
                activeforeground=_BUTTON_FG,
                relief="flat",
                borderwidth=1,
                highlightthickness=1,
                highlightbackground=_BUTTON_BORDER_COLOR,
                highlightcolor=_BUTTON_BORDER_COLOR,
                padx=16,
                pady=6,
                cursor="hand2",
                default="active",
            )

        apply_button.pack(side="right")
        cancel_button.pack(side="right", padx=(0, 8))

        dialog.bind("<Escape>", lambda _event: on_cancel())
        dialog.bind("<Return>", lambda _event: on_apply())
        dialog.update_idletasks()
        geometry_applied = False
        try:
            root.update_idletasks()
            dialog_w = max(dialog.winfo_reqwidth(), _ADVANCED_DIALOG_MIN_WIDTH)
            dialog_h = dialog.winfo_reqheight()
            screen_w = dialog.winfo_screenwidth()
            screen_h = dialog.winfo_screenheight()
            dialog_w = min(dialog_w, max(320, screen_w - 16))
            parent_x = root.winfo_rootx()
            parent_y = root.winfo_rooty()
            parent_w = root.winfo_width()
            # Keep the dialog anchored near the splash window's top-right
            # corner. This matters more now that the Windows dialog is wider:
            # if the window manager places it low, the action buttons can
            # end up off-screen on shorter displays.
            protrusion_x = 72
            inset_y = 8
            desired_x = parent_x + parent_w - dialog_w + protrusion_x
            desired_y = parent_y + inset_y
            clamped_x = max(8, min(desired_x, screen_w - dialog_w - 8))
            clamped_y = max(8, min(desired_y, screen_h - 328))
            dialog_h = min(dialog_h, max(320, screen_h - clamped_y - 8))
            dialog.geometry(f"{dialog_w}x{dialog_h}+{clamped_x}+{clamped_y}")
            if _LINUX_SPLASH_LAYOUT:
                # Applying the wider geometry lets responsive hint labels use
                # longer lines. Measure again after those Configure callbacks
                # run, otherwise the old narrow/two-line measurement leaves a
                # large blank strip above the action buttons.
                for _ in range(2):
                    dialog.update_idletasks()
                fitted_height = min(
                    _advanced_natural_height(),
                    max(320, screen_h - clamped_y - 8),
                )
                dialog.geometry(
                    f"{dialog_w}x{fitted_height}+{clamped_x}+{clamped_y}"
                )
            geometry_applied = True
        except Exception:
            pass
        if not geometry_applied:
            dialog.geometry("+%d+%d" % (root.winfo_rootx() + 24, root.winfo_rooty() + 24))
        dialog.deiconify()
        dialog.lift(root)
        dialog.wait_visibility()
        dialog.grab_set()
        apply_button.focus_set()
        dialog.focus_force()

    # Example maps link - opens the sample maps dialog
    def _on_example_maps_click():
        from gui.sample_maps_dialog import show_sample_maps_dialog
        import os
        install_dir = os.path.expanduser("~")
        result = show_sample_maps_dialog(root, install_dir)
        if result:
            selected_folder[0] = result
            root.withdraw()
            root.quit()

    secondary_link_row = tk.Frame(root, bg=_BG_COLOR)
    secondary_link_row.pack(pady=(_SECONDARY_LINK_ROW_TOP_GAP, _SECONDARY_LINK_ROW_BOTTOM_GAP))

    advanced_link = tk.Label(
        secondary_link_row,
        text="Advanced Settings...",
        font=_SMALL_FONT,
        fg="#5d6f8a",
        bg=_BG_COLOR,
        cursor="hand2",
    )
    advanced_link.bind("<Button-1>", lambda _event: _show_advanced_settings_dialog())
    advanced_link.pack(side="left")

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
    )
    sample_maps_link.bind("<Button-1>", lambda _event: _on_example_maps_click())
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
    # Auto-check once at startup. Silent when offline or already up-to-date.
    root.after(350, lambda: _start_check_updates(user_initiated=False))
    root.bind("<Return>", lambda _event: on_browse())
    root.bind("<Escape>", on_close)
    root.protocol("WM_DELETE_WINDOW", on_close)

    root.mainloop()
    # On macOS, keep the Tk app object alive for the process lifetime so
    # the global app-menu About callback remains bound to a valid Tk
    # application. Destroying it here leaves a stale About callback that
    # can trigger "application has been destroyed" errors after returning
    # from the OpenGL viewer window.
    if sys.platform != "darwin":
        try:
            root.destroy()
        except Exception:
            pass  # already destroyed, or a background thread beat us to it

    return selected_folder[0]


def _load_last_browse_dir() -> str | None:
    try:
        with open(_LAST_BROWSE_PATH_FILE, "r", encoding="utf-8") as f:
            path = f.read().strip()
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    return None


def _save_last_browse_dir(path: str) -> None:
    try:
        if not path or not os.path.isdir(path):
            return
        with open(_LAST_BROWSE_PATH_FILE, "w", encoding="utf-8") as f:
            f.write(path)
    except Exception:
        pass
