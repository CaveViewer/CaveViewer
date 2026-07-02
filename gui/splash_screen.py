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

import os
import sys
import tempfile
import threading
from caveviewer_version import APP_NAME, APP_VERSION
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

# URL for example maps link -- empty/None means link is disabled
_EXAMPLE_MAPS_URL = None

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
_BUTTON_FONT = (_UI_FONT_FAMILY, 13)
_CREDITS_TEXT = (
    "CaveViewer created by Brian Deatherage & Zsolt Zsabo of\n"
    "BottomLine Projects Scientific Dive Team and other volunteers.\n")


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

    root = tk.Tk()
    # Keep hidden until final geometry is set to avoid a visible corner->center jump.
    root.withdraw()
    root.title(f"{program_name} {version}")
    root.configure(bg=_BG_COLOR)
    root.resizable(False, False)

    _PLATFORM_ADAPTER.install_about_handler(root, program_name, version)

    window_w, window_h = 500, 600

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
            print(f"[CaveViewer] Note: could not load splash screen logo ({e}); continuing without it.")
    else:
        print("[CaveViewer] Note: splash screen logo asset not found; continuing without it.")

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
    
    example_maps_label = tk.Label(
        root,
        text="Download Sample Maps",
        font=_LINK_FONT,
        fg=_BUTTON_BG,
        bg=_BG_COLOR,
        cursor="hand2",
    )
    example_maps_label.bind("<Button-1>", lambda _: _on_example_maps_click())
    example_maps_label.pack(pady=(8, 16))

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
