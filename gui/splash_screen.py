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
from gui.dpi_utils import apply_tk_scaling, configure_process_dpi_awareness, tk_display_scale
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
_INSTRUCTION_FONT = (_UI_FONT_FAMILY, 11) if sys.platform == "win32" else _BODY_FONT
_FOOTER_FONT = (_UI_FONT_FAMILY, 9) if sys.platform == "win32" else _SMALL_FONT
_LINK_FONT = (_UI_FONT_FAMILY, 10, "underline")
_BUTTON_FONT = (_UI_FONT_FAMILY, 13)
_SPLASH_WINDOW_MIN_HEIGHT = 540 if sys.platform == "darwin" else (560 if sys.platform == "win32" else 620)
_SECONDARY_LINK_ROW_BOTTOM_GAP = 18 if sys.platform == "darwin" else 36
_FOOTER_CREDITS_BOTTOM_PAD = 18 if sys.platform == "darwin" else 36
_TITLE_TO_ACTION_GAP = 58 if sys.platform == "win32" else 28
_BROWSE_BUTTON_BOTTOM_GAP = 32 if sys.platform == "win32" else 16
_INSTRUCTION_BOTTOM_GAP = 20 if sys.platform == "win32" else 0
_SECONDARY_LINK_ROW_TOP_GAP = 30 if sys.platform == "win32" else 16
_ADVANCED_DIALOG_TWO_COLUMN = True
_ADVANCED_DIALOG_WRAP = 620 if sys.platform == "win32" else 340
_ADVANCED_DIALOG_ENTRY_WIDTH = 42 if sys.platform == "win32" else 22
_ADVANCED_DIALOG_BODY_PAD_X = 18 if sys.platform == "darwin" else (32 if sys.platform == "win32" else 24)
_ADVANCED_DIALOG_SECTION_GAP = 44 if sys.platform == "win32" else 18
_ADVANCED_DIALOG_MIN_WIDTH = 1320 if sys.platform == "win32" else 0
_CREDITS_TEXT = (
    "CaveViewer created by Brian Deatherage & Zsolt Zsabo of\n"
    "BottomLine Projects Scientific Dive Team and other volunteers.\n")

_ADVANCED_SETTING_FIELDS = (
    {
        "section": "streaming",
        "key": "memory_target_percent",
        "env_var": "CAVEVIEWER_MEMORY_UTILIZATION_TARGET",
        "label": "System RAM target (%)",
        "hint": "Percent of total system RAM CaveViewer may use for loaded chunks.",
    },
    {
        "section": "streaming",
        "key": "gpu_memory_target_percent",
        "env_var": "CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET",
        "label": "GPU memory target (%)",
        "hint": "Percent of detected or configured GPU memory to use for loaded chunks.",
    },
    {
        "section": "streaming",
        "key": "gpu_memory_gb",
        "env_var": "CAVEVIEWER_GPU_MEMORY_GB",
        "label": "GPU memory override (GB)",
        "hint": "Optional. Set this if GPU memory cannot be detected automatically.",
    },
    {
        "section": "streaming",
        "key": "io_workers",
        "env_var": "CAVEVIEWER_IO_WORKERS",
        "label": "Worker count",
        "hint": "Background chunk-loading worker threads.",
    },
    {
        "section": "streaming",
        "key": "io_reserved_cpus",
        "env_var": "CAVEVIEWER_IO_RESERVED_CPUS",
        "label": "CPU cores to keep free",
        "hint": "How many CPU cores CaveViewer should avoid using for streaming workers.",
    },
    {
        "section": "streaming",
        "key": "upload_chunks_per_frame",
        "env_var": "CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME",
        "label": "Chunk uploads per frame",
        "hint": "Ready chunks to upload to the GPU each frame. Use 1 for smoother streaming.",
    },
    {
        "section": "streaming",
        "key": "upload_time_budget_ms",
        "env_var": "CAVEVIEWER_UPLOAD_TIME_BUDGET_MS",
        "label": "Upload budget (ms)",
        "hint": "Soft per-frame budget for chunk uploads. A single large chunk can exceed it.",
    },
    {
        "section": "parsing",
        "key": "chunk_size_meters",
        "env_var": "CAVEVIEWER_CHUNK_SIZE_METERS",
        "label": "Import chunk size (m)",
        "hint": "Chunk size for new/rebuilt caches. Existing caches use their manifest chunk size.",
    },
    {
        "section": "parsing",
        "key": "obj_scan_throttle_ms",
        "env_var": "CAVEVIEWER_OBJ_SCAN_THROTTLE_MS",
        "label": "OBJ scan throttle (ms)",
        "hint": "Small yield during OBJ scanning. Use 1-5ms on Windows if imports make the app unresponsive.",
    },
    {
        "section": "parsing",
        "key": "chunk_build_workers",
        "env_var": "CAVEVIEWER_CHUNK_BUILD_WORKERS",
        "label": "Import worker count",
        "hint": "Worker threads used while writing chunk files during initial map import.",
    },
    {
        "section": "parsing",
        "key": "chunk_build_reserved_cpus",
        "env_var": "CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS",
        "label": "Import CPUs to keep free",
        "hint": "CPU cores to reserve when import worker count is not explicitly set.",
    },
)

_ADVANCED_SETTING_SECTIONS = (
    ("streaming", "Streaming Performance"),
    ("parsing", "Map Parsing"),
)


def _default_advanced_settings() -> dict[str, str]:
    return {field["key"]: "" for field in _ADVANCED_SETTING_FIELDS}


def _env_setting_or_default(env_var: str, default: str) -> str:
    value = os.getenv(env_var, "").strip()
    return value if value else default


def _advanced_setting_defaults() -> dict[str, str]:
    logical_cpus = max(1, os.cpu_count() or 1)
    return {
        "memory_target_percent": _env_setting_or_default(
            "CAVEVIEWER_MEMORY_UTILIZATION_TARGET", "12"
        ),
        "gpu_memory_target_percent": _env_setting_or_default(
            "CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET", "70"
        ),
        "gpu_memory_gb": os.getenv("CAVEVIEWER_GPU_MEMORY_GB", "").strip(),
        "io_reserved_cpus": _env_setting_or_default("CAVEVIEWER_IO_RESERVED_CPUS", "3"),
        "io_workers": _env_setting_or_default(
            "CAVEVIEWER_IO_WORKERS", str(max(1, logical_cpus - 3))
        ),
        "upload_chunks_per_frame": _env_setting_or_default(
            "CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME", "1"
        ),
        "upload_time_budget_ms": _env_setting_or_default(
            "CAVEVIEWER_UPLOAD_TIME_BUDGET_MS", "3.0"
        ),
        "chunk_size_meters": _env_setting_or_default("CAVEVIEWER_CHUNK_SIZE_METERS", "8"),
        "obj_scan_throttle_ms": _env_setting_or_default(
            "CAVEVIEWER_OBJ_SCAN_THROTTLE_MS", "1" if sys.platform.startswith("win") else "0"
        ),
        "chunk_build_reserved_cpus": _env_setting_or_default(
            "CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS", "2"
        ),
        "chunk_build_workers": _env_setting_or_default(
            "CAVEVIEWER_CHUNK_BUILD_WORKERS", str(max(1, logical_cpus - 2))
        ),
    }


def _effective_advanced_settings(values: dict | None = None) -> dict[str, str]:
    normalized = _normalize_advanced_settings(values)
    defaults = _advanced_setting_defaults()
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

    gpu_memory_text = normalized["gpu_memory_target_percent"]
    if gpu_memory_text:
        try:
            gpu_memory_value = float(gpu_memory_text)
        except ValueError:
            return False, "GPU memory target must be a number between 1 and 80.", normalized
        if gpu_memory_value < 1.0 or gpu_memory_value > 80.0:
            return False, "GPU memory target must be between 1 and 80 percent.", normalized
        normalized["gpu_memory_target_percent"] = f"{gpu_memory_value:g}"

    gpu_memory_gb_text = normalized["gpu_memory_gb"]
    if gpu_memory_gb_text:
        try:
            gpu_memory_gb = float(gpu_memory_gb_text)
        except ValueError:
            return False, "GPU memory override must be a positive number of GB.", normalized
        if gpu_memory_gb <= 0.0 or gpu_memory_gb > 1024.0:
            return False, "GPU memory override must be between 0 and 1024 GB.", normalized
        normalized["gpu_memory_gb"] = f"{gpu_memory_gb:g}"

    integer_fields = (
        ("io_workers", "Worker count", 1, None),
        ("io_reserved_cpus", "CPUs to leave free", 0, None),
        ("upload_chunks_per_frame", "Chunk uploads per frame", 1, 16),
        ("chunk_build_workers", "Import worker count", 1, None),
        ("chunk_build_reserved_cpus", "Import CPUs to keep free", 0, None),
    )
    for key, label, minimum, maximum in integer_fields:
        text = normalized[key]
        if not text:
            continue
        try:
            int_value = int(text)
        except ValueError:
            return False, f"{label} must be a whole number.", normalized
        if int_value < minimum:
            return False, f"{label} must be at least {minimum}.", normalized
        if maximum is not None and int_value > maximum:
            return False, f"{label} must be no more than {maximum}.", normalized
        normalized[key] = str(int_value)

    upload_budget_text = normalized["upload_time_budget_ms"]
    if upload_budget_text:
        try:
            upload_budget_value = float(upload_budget_text)
        except ValueError:
            return False, "Upload budget must be a number between 0.5 and 50.", normalized
        if upload_budget_value < 0.5 or upload_budget_value > 50.0:
            return False, "Upload budget must be between 0.5 and 50 ms.", normalized
        normalized["upload_time_budget_ms"] = f"{upload_budget_value:g}"

    chunk_size_text = normalized["chunk_size_meters"]
    if chunk_size_text:
        try:
            chunk_size_value = float(chunk_size_text)
        except ValueError:
            return False, "Import chunk size must be a positive number.", normalized
        if chunk_size_value <= 0.0:
            return False, "Import chunk size must be greater than 0.", normalized
        if chunk_size_value > 512.0:
            return False, "Import chunk size must be 512m or smaller.", normalized
        normalized["chunk_size_meters"] = f"{chunk_size_value:g}"

    obj_scan_throttle_text = normalized["obj_scan_throttle_ms"]
    if obj_scan_throttle_text:
        try:
            obj_scan_throttle_value = float(obj_scan_throttle_text)
        except ValueError:
            return False, "OBJ scan throttle must be a number between 0 and 50.", normalized
        if obj_scan_throttle_value < 0.0 or obj_scan_throttle_value > 50.0:
            return False, "OBJ scan throttle must be between 0 and 50 ms.", normalized
        normalized["obj_scan_throttle_ms"] = f"{obj_scan_throttle_value:g}"

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

    configure_process_dpi_awareness()
    root = tk.Tk()
    apply_tk_scaling(root)
    splash_scale = tk_display_scale(root)

    def px(value: float) -> int:
        return int(round(value * splash_scale))

    # Keep hidden until final geometry is set to avoid a visible corner->center jump.
    root.withdraw()
    root.title(program_name)
    root.configure(bg=_BG_COLOR)
    root.resizable(False, False)

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
        "link_enabled": True,
        "check_user_initiated": False,
    }

    update_panel = tk.Frame(root, bg=_BG_COLOR)

    update_status_label = tk.Label(
        update_panel,
        text="",
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

    update_panel.pack(pady=(0, 0))

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
            message = "Your app is up to date." if update_state.get("check_user_initiated") else ""
            _show_update_panel(message, show_progress=False)
            return

        if not result.update_available:
            message = "Your app is up to date." if update_state.get("check_user_initiated") else ""
            _show_update_panel(message, show_progress=False)
            return

        update_state["result"] = result
        _show_update_panel("", show_progress=False)
        update_action_row.pack(pady=(0, 2))
        download_link.pack()

    def _start_check_updates(*, user_initiated: bool = False):
        if update_state["busy"]:
            return
        _set_busy(True)
        update_state["check_user_initiated"] = bool(user_initiated)
        if user_initiated:
            _show_update_panel("Checking for updates...", show_progress=False)
        else:
            _show_update_panel("", show_progress=False)

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
    )
    browse_button.bind("<Button-1>", lambda _event: on_browse())
    browse_button.bind("<Return>", lambda _event: on_browse())
    browse_button.bind("<space>", lambda _event: on_browse())
    browse_button.bind("<Enter>", lambda _event: browse_button.config(bg="#d8b34d"))
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
        dialog.title("Advanced Settings")
        dialog.configure(bg=_BG_COLOR)
        dialog.resizable(False, False)
        dialog.transient(root)

        body = tk.Frame(dialog, bg=_BG_COLOR, padx=_ADVANCED_DIALOG_BODY_PAD_X, pady=18)
        body.pack(fill="both", expand=True)

        field_vars: dict[str, tk.StringVar] = {}
        effective_settings = _effective_advanced_settings(advanced_settings)

        section_row = tk.Frame(body, bg=_BG_COLOR)
        section_row.pack(fill="both", expand=True, pady=(0, 10))

        def render_section(parent, title: str, section_key: str) -> None:
            section = tk.Frame(
                parent,
                bg=_BG_COLOR,
                padx=14,
                pady=12,
                highlightthickness=1,
                highlightbackground=_BORDER_COLOR,
                highlightcolor=_BORDER_COLOR,
            )
            if _ADVANCED_DIALOG_TWO_COLUMN:
                section.pack(side="left", fill="both", expand=True, padx=(0, _ADVANCED_DIALOG_SECTION_GAP))
            else:
                section.pack(fill="x", pady=(0, 12))

            tk.Label(
                section,
                text=title,
                font=_VERSION_FONT,
                fg=_TITLE_COLOR,
                bg=_BG_COLOR,
            ).pack(anchor="w", pady=(0, 10))

            fields = [field for field in _ADVANCED_SETTING_FIELDS if field.get("section") == section_key]
            for field in fields:
                row = tk.Frame(section, bg=_BG_COLOR)
                row.pack(fill="x", pady=(0, 9))

                tk.Label(
                    row,
                    text=field["label"],
                    font=_BODY_FONT,
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
                entry = tk.Entry(
                    row,
                    textvariable=var,
                    font=_BODY_FONT,
                    bg="#1c1c24",
                    fg=_SUBTITLE_COLOR,
                    insertbackground=_SUBTITLE_COLOR,
                    relief="flat",
                    width=_ADVANCED_DIALOG_ENTRY_WIDTH,
                )
                entry.pack(anchor="w", pady=(4, 4))

                tk.Label(
                    row,
                    text=field["hint"],
                    font=_SMALL_FONT,
                    fg=_INSTRUCTION_COLOR,
                    bg=_BG_COLOR,
                    justify="left",
                    wraplength=_ADVANCED_DIALOG_WRAP,
                ).pack(anchor="w")

        for section_key, section_title in _ADVANCED_SETTING_SECTIONS:
            render_section(section_row, section_title, section_key)

        if _ADVANCED_DIALOG_TWO_COLUMN:
            section_children = section_row.winfo_children()
            for child in section_children[:-1]:
                child.pack_configure(padx=(0, _ADVANCED_DIALOG_SECTION_GAP))
            if section_children:
                section_children[-1].pack_configure(padx=(0, 0))

        error_label = tk.Label(
            body,
            text="",
            font=_SMALL_FONT,
            fg="#ff9b90",
            bg=_BG_COLOR,
            justify="left",
            wraplength=_ADVANCED_DIALOG_WRAP,
        )
        error_label.pack(anchor="w", pady=(4, 10))

        button_row = tk.Frame(body, bg=_BG_COLOR)
        button_row.pack(fill="x")

        def on_cancel():
            dialog.destroy()

        def on_apply():
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
        except Exception:
            pass
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
    final_height = max(px(_SPLASH_WINDOW_MIN_HEIGHT), root.winfo_reqheight())
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
