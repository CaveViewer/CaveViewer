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

import json
import os
import sys
import tempfile
import threading
from caveviewer_version import APP_NAME, APP_VERSION
from core.logging_utils import get_logger
from gui.platform import get_splash_platform_adapter


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
_LOGO_PATH = _resolve_asset_path("loading_logo.png")
_LAST_BROWSE_PATH_FILE = os.path.join(os.path.expanduser("~"), ".caveviewer_last_browse_path")
_ADVANCED_SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".caveviewer_advanced_settings.json")

# URL for example maps link -- empty/None means link is disabled
_EXAMPLE_MAPS_URL = None
_LOG = get_logger("CaveViewer")

_BG_COLOR = "#0a0a0d"           # near-black, matches the in-app overlay backgrounds
_PANEL_COLOR = "#12121a"        # slightly lighter panel background
_TITLE_COLOR = "#f2d98c"        # amber/gold, matches the in-app title text color
_SUBTITLE_COLOR = "#cccdd6"     # light gray, matches in-app subtitle/body text
_INSTRUCTION_COLOR = "#9a9aa6"  # dimmer gray, matches in-app secondary/note text
_BUTTON_BG = "#caa23e"          # amber button, matches the in-app active-button color
_BUTTON_FG = "#1a1408"          # dark text on the amber button, matches in-app active-button text
_BORDER_COLOR = "#5c5c6e"
_UPDATE_STARTED_SENTINEL = "__caveviewer_update_started__"
_PLATFORM_ADAPTER = get_splash_platform_adapter()
_UI_FONT_FAMILY = _PLATFORM_ADAPTER.ui_font_family()
_TITLE_FONT = (_UI_FONT_FAMILY, 24, "bold")
_VERSION_FONT = (_UI_FONT_FAMILY, 12)
_BODY_FONT = (_UI_FONT_FAMILY, 12)
_SMALL_FONT = (_UI_FONT_FAMILY, 10)
_LINK_FONT = (_UI_FONT_FAMILY, 10, "underline")
_CTA_LINK_FONT = (_UI_FONT_FAMILY, 12, "bold", "underline")
_CTA_HINT_FONT = (_UI_FONT_FAMILY, 10)
_BUTTON_FONT = (_UI_FONT_FAMILY, 13)
_SPLASH_WINDOW_HEIGHT = 600 if sys.platform == "darwin" else 644
_SPLASH_CTA_BOTTOM_PAD = 16 if sys.platform == "darwin" else 44
_CREDITS_TEXT = (
    "CaveViewer created by Brian Deatherage & Zsolt Zsabo of\n"
    "BottomLine Projects Scientific Dive Team and other volunteers.\n")

_ADVANCED_SETTING_FIELDS = (
    {
        "key": "memory_target_percent",
        "env_var": "CAVEVIEWER_MEMORY_UTILIZATION_TARGET",
        "label": "Memory target (%)",
        "hint": "Percent of total RAM CaveViewer may use for loaded chunks.",
    },
    {
        "key": "io_workers",
        "env_var": "CAVEVIEWER_IO_WORKERS",
        "label": "Worker count",
        "hint": "Background chunk-loading worker threads.",
    },
    {
        "key": "io_reserved_cpus",
        "env_var": "CAVEVIEWER_IO_RESERVED_CPUS",
            "label": "CPU cores to keep free",
        "hint": "How many CPU cores CaveViewer should avoid using for streaming workers.",
    },
)


def _default_advanced_settings() -> dict[str, str]:
    return {field["key"]: "" for field in _ADVANCED_SETTING_FIELDS}


def _effective_advanced_settings(values: dict | None = None) -> dict[str, str]:
    normalized = _normalize_advanced_settings(values)
    logical_cpus = max(1, os.cpu_count() or 1)
    defaults = {
        "memory_target_percent": "12",
        "io_reserved_cpus": "3",
        "io_workers": str(max(1, logical_cpus - 3)),
    }
    return {
        key: (normalized.get(key, "") or defaults[key])
        for key in defaults
    }


def _normalize_advanced_settings(values: dict | None) -> dict[str, str]:
    normalized = _default_advanced_settings()
    if not isinstance(values, dict):
        return normalized
    for field in _ADVANCED_SETTING_FIELDS:
        raw = values.get(field["key"], "")
        normalized[field["key"]] = str(raw).strip() if raw is not None else ""
    return normalized


def _load_advanced_settings() -> dict[str, str]:
    try:
        with open(_ADVANCED_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return _normalize_advanced_settings(json.load(f))
    except Exception:
        return _default_advanced_settings()


def _save_advanced_settings(values: dict[str, str]) -> None:
    try:
        with open(_ADVANCED_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(_normalize_advanced_settings(values), f, indent=2)
    except Exception as e:
        _LOG.warning(f"could not save advanced settings ({e})")


def _validate_advanced_settings(values: dict[str, str]) -> tuple[bool, str | None, dict[str, str]]:
    normalized = _normalize_advanced_settings(values)

    memory_text = normalized["memory_target_percent"]
    if memory_text:
        try:
            memory_value = float(memory_text)
        except ValueError:
            return False, "Streaming memory target must be a number between 1 and 80.", normalized
        if memory_value < 1.0 or memory_value > 80.0:
            return False, "Streaming memory target must be between 1 and 80 percent.", normalized
        normalized["memory_target_percent"] = f"{memory_value:g}"

    for key, label in (("io_workers", "Worker count"), ("io_reserved_cpus", "CPUs to leave free")):
        text = normalized[key]
        if not text:
            continue
        try:
            int_value = int(text)
        except ValueError:
            return False, f"{label} must be a whole number.", normalized
        if key == "io_workers" and int_value < 1:
            return False, "Streaming worker count must be at least 1.", normalized
        if key == "io_reserved_cpus" and int_value < 0:
                return False, "CPU cores to keep free cannot be negative.", normalized
        normalized[key] = str(int_value)

    return True, None, normalized


def _apply_advanced_settings_to_env(values: dict[str, str]) -> None:
    normalized = _effective_advanced_settings(values)
    for field in _ADVANCED_SETTING_FIELDS:
        value = normalized[field["key"]]
        os.environ[field["env_var"]] = value


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

    root = tk.Tk()
    # Keep hidden until final geometry is set to avoid a visible corner->center jump.
    root.withdraw()
    root.title(f"{program_name} {version}")
    root.configure(bg=_BG_COLOR)
    root.resizable(False, False)

    _PLATFORM_ADAPTER.install_about_handler(root, program_name, version)

    window_w, window_h = 500, _SPLASH_WINDOW_HEIGHT

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
            max_logo_dim = 140
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
        "link_enabled": True,
    }

    update_panel = tk.Frame(root, bg=_BG_COLOR)

    update_status_label = tk.Label(
        update_panel,
        text="Your app is up to date.",
        font=_SMALL_FONT,
        fg=_INSTRUCTION_COLOR,
        bg=_BG_COLOR,
        justify="center",
        wraplength=420,
    )

    progress_canvas = tk.Canvas(
        update_panel,
        width=300,
        height=4,
        bg="#1c1c24",
        highlightthickness=0,
    )
    progress_bar = progress_canvas.create_rectangle(0, 0, 0, 4, fill=_BUTTON_BG, width=0)

    update_action_row = tk.Frame(update_panel, bg=_BG_COLOR)

    download_link = tk.Label(
        update_action_row,
        text="Download Update",
        font=_LINK_FONT,
        bg=_BG_COLOR,
        fg="#8ab4ff",
        cursor="hand2",
    )

    close_install_button = tk.Button(
        update_action_row,
        text="Close app to install manually",
        font=_SMALL_FONT,
        bg=_BUTTON_BG,
        fg=_BUTTON_FG,
        activebackground=_BUTTON_BG,
        activeforeground=_BUTTON_FG,
        relief="flat",
        borderwidth=0,
        padx=12,
        pady=5,
        cursor="hand2",
    )

    prompt_yes_button = tk.Button(
        update_action_row,
        text="Yes",
        font=_SMALL_FONT,
        bg=_BUTTON_BG,
        fg=_BUTTON_FG,
        activebackground=_BUTTON_BG,
        activeforeground=_BUTTON_FG,
        relief="flat",
        borderwidth=0,
        padx=12,
        pady=5,
        cursor="hand2",
        default="active",
    )

    prompt_no_button = tk.Button(
        update_action_row,
        text="No",
        font=_SMALL_FONT,
        bg="#2a2a33",
        fg=_SUBTITLE_COLOR,
        activebackground="#33333f",
        activeforeground=_SUBTITLE_COLOR,
        relief="flat",
        borderwidth=0,
        padx=12,
        pady=5,
        cursor="hand2",
    )

    dismiss_update_button = tk.Button(
        update_action_row,
        text="Later",
        font=_SMALL_FONT,
        bg="#2a2a33",
        fg=_SUBTITLE_COLOR,
        activebackground="#33333f",
        activeforeground=_SUBTITLE_COLOR,
        relief="flat",
        borderwidth=0,
        padx=12,
        pady=5,
        cursor="hand2",
    )

    def _hide_update_actions():
        for w in (download_link, close_install_button, dismiss_update_button, prompt_yes_button, prompt_no_button):
            w.pack_forget()

    def _set_link_enabled(is_enabled: bool):
        update_state["link_enabled"] = bool(is_enabled)
        if is_enabled:
            download_link.config(fg="#8ab4ff", cursor="hand2")
        else:
            download_link.config(fg="#5d6f8a", cursor="arrow")

    def _set_progress(frac: float):
        clamped = max(0.0, min(1.0, float(frac)))
        progress_canvas.coords(progress_bar, 0, 0, int(300 * clamped), 4)

    def _show_update_panel(message: str, *, show_progress: bool = False, progress_frac: float = 0.0):
        update_status_label.config(text=message)
        if message:
            if not update_status_label.winfo_ismapped():
                update_status_label.pack(pady=(0, 8))
        else:
            update_status_label.pack_forget()
        if show_progress:
            if not progress_canvas.winfo_ismapped():
                progress_canvas.pack(pady=(0, 8))
            _set_progress(progress_frac)
        else:
            progress_canvas.pack_forget()
        _hide_update_actions()
        update_action_row.pack_forget()

    update_panel.pack(pady=(0, 10))
    update_status_label.pack(pady=(0, 8))

    def _set_busy(is_busy: bool):
        update_state["busy"] = bool(is_busy)
        _set_link_enabled(not is_busy)

    def _on_download_complete(payload_path: str):
        update_state["downloaded_payload"] = payload_path
        _set_busy(False)
        try:
            manual_install = _PLATFORM_ADAPTER.prepare_manual_install(payload_path)
            update_state["mounted_payload"] = manual_install.mounted_payload_path
            update_state["mounted_app"] = manual_install.mounted_app_path
            _show_update_panel("Close the app and apply the update manually?", show_progress=False)
            update_action_row.pack(pady=(0, 2))
            # macOS standard button order: negative on left, default affirmative on right.
            prompt_yes_button.pack(side="right")
            prompt_no_button.pack(side="right", padx=(0, 8))
            prompt_yes_button.focus_set()
        except Exception as e:
            _show_update_panel(f"Update downloaded, but install could not start: {e}", show_progress=False)
            update_action_row.pack(pady=(0, 2))
            close_install_button.config(text="Close app to install manually")
            close_install_button.pack(side="left", padx=(0, 8))

    def _on_download_error(err: str):
        _set_busy(False)
        _show_update_panel("Download Update.", show_progress=False)
        update_action_row.pack(pady=(0, 2))
        download_link.pack()

    def _start_download():
        result = update_state.get("result")
        if not result:
            return

        if not update_state.get("link_enabled", True):
            return

        _set_busy(True)
        _show_update_panel("", show_progress=True, progress_frac=0.0)

        def worker():
            from gui.update_checker import download_update

            download_dir = tempfile.mkdtemp(prefix="caveviewer_update_")
            payload_path = os.path.join(download_dir, "update_payload.bin")

            def on_progress(downloaded, total):
                frac = min(1.0, downloaded / total) if total else 0.0

                def ui_update():
                    _show_update_panel("", show_progress=True, progress_frac=frac)

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
                root.after(0, lambda: _on_download_complete(final_payload_path))
            except Exception as e:
                root.after(0, lambda: _on_download_error(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_check_result(result):
        _set_busy(False)
        if result.error:
            _show_update_panel("Your app is up to date.", show_progress=False)
            return

        if not result.update_available:
            _show_update_panel("Your app is up to date.", show_progress=False)
            return

        update_state["result"] = result
        _show_update_panel("", show_progress=False)
        update_action_row.pack(pady=(0, 2))
        download_link.pack()

    def _start_check_updates(*, user_initiated: bool = False):
        if update_state["busy"]:
            return
        _set_busy(True)
        if user_initiated:
            _show_update_panel("Checking for updates...", show_progress=False)
        else:
            _show_update_panel("Your app is up to date.", show_progress=False)

        def worker():
            from gui.update_checker import check_for_update

            result = check_for_update(
                current_version=version,
                install_channel=_PLATFORM_ADAPTER.install_channel(),
            )
            root.after(0, lambda: _on_check_result(result))

        threading.Thread(target=worker, daemon=True).start()

    def _close_and_install():
        root.withdraw()
        root.quit()

    def _confirm_close_for_manual_update():
        selected_folder[0] = _UPDATE_STARTED_SENTINEL
        root.withdraw()
        root.quit()

    def _defer_manual_update_close():
        _show_update_panel("Update downloaded and opened for manual installation.", show_progress=False)

    download_link.bind("<Button-1>", lambda _event: _start_download())
    close_install_button.config(command=_close_and_install)
    prompt_yes_button.config(command=_confirm_close_for_manual_update)
    prompt_no_button.config(command=_defer_manual_update_close)

    credit_label = tk.Label(
        root,
        text=_CREDITS_TEXT,
        font=_SMALL_FONT,
        fg=_INSTRUCTION_COLOR, bg=_BG_COLOR,
        justify="center",
    )
    credit_label.pack(pady=(0, 16))

    # -- separator line, subtle ---------------------------------------------------
    separator = tk.Frame(root, bg=_BORDER_COLOR, height=1)
    separator.pack(fill="x", padx=44, pady=(0, 24))

    # -- browse button + instructions ---------------------------------------------
    def on_browse():
        dialog_kwargs = {
            "title": "Select a cave map folder",
        }
        last_dir = _load_last_browse_dir()
        if last_dir:
            dialog_kwargs["initialdir"] = last_dir

        folder = filedialog.askdirectory(**dialog_kwargs)
        if folder:
            selected_folder[0] = folder
            _save_last_browse_dir(folder)
            root.withdraw()
            root.quit()

    def on_close(_event=None):
        root.withdraw()
        root.quit()

    browse_button = tk.Button(
        root, text="Select Map...", command=on_browse,
        font=_BUTTON_FONT,
        bg=_BUTTON_BG, fg=_BUTTON_FG,
        activebackground=_BUTTON_BG, activeforeground=_BUTTON_FG,
        relief="flat", borderwidth=0,
        default="active",
        padx=30, pady=9,
        cursor="hand2",
    )
    browse_button.pack(pady=(0, 16))

    instruction_label = tk.Label(
        root,
        text="Point this to a folder with your map's .obj+.mtl or .glb file,\n"
             "or a folder that was already imported by CaveViewer.",
        font=_BODY_FONT,
        fg=_INSTRUCTION_COLOR, bg=_BG_COLOR,
        justify="center",
    )
    instruction_label.pack(pady=(0, 0))

    def _show_advanced_settings_dialog() -> None:
        dialog = tk.Toplevel(root)
        dialog.title("Advanced Settings")
        dialog.configure(bg=_BG_COLOR)
        dialog.resizable(False, False)
        dialog.transient(root)

        body = tk.Frame(dialog, bg=_BG_COLOR, padx=18, pady=18)
        body.pack(fill="both", expand=True)

        tk.Label(
            body,
            text="Streaming Performance",
            font=_VERSION_FONT,
            fg=_TITLE_COLOR,
            bg=_BG_COLOR,
        ).pack(anchor="w", pady=(0, 8))

        field_vars: dict[str, tk.StringVar] = {}
        for field in _ADVANCED_SETTING_FIELDS:
            row = tk.Frame(body, bg=_BG_COLOR)
            row.pack(fill="x", pady=(0, 10))

            tk.Label(
                row,
                text=field["label"],
                font=_BODY_FONT,
                fg=_SUBTITLE_COLOR,
                bg=_BG_COLOR,
                anchor="w",
            ).pack(anchor="w")

            var = tk.StringVar(value=_effective_advanced_settings(advanced_settings)[field["key"]])
            field_vars[field["key"]] = var
            entry = tk.Entry(
                row,
                textvariable=var,
                font=_BODY_FONT,
                bg="#1c1c24",
                fg=_SUBTITLE_COLOR,
                insertbackground=_SUBTITLE_COLOR,
                relief="flat",
                width=18,
            )
            entry.pack(anchor="w", pady=(4, 4))

            tk.Label(
                row,
                text=field["hint"],
                font=_SMALL_FONT,
                fg=_INSTRUCTION_COLOR,
                bg=_BG_COLOR,
                justify="left",
                wraplength=420,
            ).pack(anchor="w")

        error_label = tk.Label(
            body,
            text="",
            font=_SMALL_FONT,
            fg="#ff9b90",
            bg=_BG_COLOR,
            justify="left",
            wraplength=420,
        )
        error_label.pack(anchor="w", pady=(4, 10))

        button_row = tk.Frame(body, bg=_BG_COLOR)
        button_row.pack(fill="x")

        def on_cancel():
            dialog.destroy()

        def on_apply():
            nonlocal advanced_settings
            proposed = {key: var.get() for key, var in field_vars.items()}
            ok, message, normalized = _validate_advanced_settings(proposed)
            if not ok:
                error_label.config(text=message or "Invalid advanced settings.")
                return

            advanced_settings = _effective_advanced_settings(normalized)
            _apply_advanced_settings_to_env(advanced_settings)
            _save_advanced_settings(advanced_settings)
            dialog.destroy()

        cancel_button = tk.Button(
            button_row,
            text="Cancel",
            command=on_cancel,
            font=_SMALL_FONT,
            bg="#2a2a33",
            fg=_SUBTITLE_COLOR,
            activebackground="#33333f",
            activeforeground=_SUBTITLE_COLOR,
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=6,
            cursor="hand2",
        )

        apply_button = tk.Button(
            button_row,
            text="Apply",
            command=on_apply,
            font=_SMALL_FONT,
            bg=_BUTTON_BG,
            fg=_BUTTON_FG,
            activebackground=_BUTTON_BG,
            activeforeground=_BUTTON_FG,
            relief="flat",
            borderwidth=0,
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
        dialog.wait_visibility()
        dialog.grab_set()
        apply_button.focus_set()
        dialog.focus_force()

    advanced_link = tk.Label(
        root,
        text="Advanced Settings...",
        font=_SMALL_FONT,
        fg="#5d6f8a",
        bg=_BG_COLOR,
        cursor="hand2",
    )
    advanced_link.bind("<Button-1>", lambda _event: _show_advanced_settings_dialog())
    advanced_link.pack(pady=(12, 4))

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
    
    sample_maps_cta = tk.Frame(
        root,
        bg=_PANEL_COLOR,
        highlightthickness=1,
        highlightbackground="#3b3320",
        highlightcolor="#3b3320",
        cursor="hand2",
        padx=14,
        pady=9,
    )

    cta_row = tk.Frame(sample_maps_cta, bg=_PANEL_COLOR, cursor="hand2")
    cta_row.pack()

    cta_link_label = tk.Label(
        cta_row,
        text="New here? Download Sample Maps",
        font=_CTA_LINK_FONT,
        fg=_BUTTON_BG,
        bg=_PANEL_COLOR,
        cursor="hand2",
    )
    cta_link_label.pack(side="left")

    cta_hint_label = tk.Label(
        sample_maps_cta,
        text="Includes ready-to-open demo caves",
        font=_CTA_HINT_FONT,
        fg=_SUBTITLE_COLOR,
        bg=_PANEL_COLOR,
        pady=1,
        cursor="hand2",
        justify="center",
    )
    cta_hint_label.pack(pady=(5, 0))

    def _bind_sample_maps_click(widget):
        widget.bind("<Button-1>", lambda _: _on_example_maps_click())

    for widget in (sample_maps_cta, cta_row, cta_link_label, cta_hint_label):
        _bind_sample_maps_click(widget)

    sample_maps_cta.pack(pady=(20, _SPLASH_CTA_BOTTOM_PAD))

    # -- footer note ----------------------------------------------------------------

    browse_button.focus_set()
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
