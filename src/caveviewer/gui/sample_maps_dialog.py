"""
caveviewer.gui.sample_maps_dialog

The window opened by the splash screen's "Sample Maps..." button: lists
the known sample cave scans (see caveviewer.gui.sample_maps), shows their size,
and lets the person download (or, if already downloaded, directly open)
whichever one they want -- a one-click way to try CaveViewer without
having their own scan yet.

Kept separate from caveviewer.gui.sample_maps (pure fetch/download/extract
logic, no UI) the same way update workflow code is kept separate from
caveviewer.gui.update_checker -- this module is purely the Tkinter presentation
and the glue that drives the other one.
"""

from __future__ import annotations

import os
import sys
import threading
import time

from caveviewer.gui.map_selection import (
    validate_selected_map_folder as _validate_selected_map_folder,
)
from caveviewer.gui.notifications import show_error, show_info, show_warning
from caveviewer.gui.platform import (
    DesktopServices,
    DirectorySelection,
    get_desktop_services,
    get_splash_platform_adapter,
)
from caveviewer.gui.preferences import migrate_state_file, write_text_atomic
from caveviewer.gui.tk_theme import DARK_THEME
from caveviewer.version import APP_NAME


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
_SAMPLE_DOWNLOAD_NOTIFICATION_PREFIX = "caveviewer.sample-map-download"


def _last_sample_maps_dir_file() -> str:
    """Resolve state lazily so tests and portable runs remain isolated."""
    return migrate_state_file(
        "last_sample_maps_dir", ".caveviewer_last_sample_maps_dir"
    )


def _activate_download_cancel_button(action_button, set_action_button):
    """Turn a row's existing action button into its cancellation control."""
    cancel_event = threading.Event()
    set_action_button(action_button, "Cancel", cancel_event.set)
    return cancel_event


def _ask_directory_in_front(
    desktop_services: DesktopServices, owner, *, title: str, initial_dir: str
):
    """Open an owned native directory chooser above the application's windows."""
    previous_topmost = False
    topmost_supported = False
    try:
        previous_topmost = owner.attributes("-topmost")
        topmost_supported = True
    except Exception:
        pass

    try:
        # Supplying parent establishes native window ownership where Tk can
        # provide it.  On Linux portal/Wayland paths, however, the chooser may
        # not become a real child of this Toplevel. Keeping this dialog topmost
        # during the chooser call can then put Sample Maps above the chooser.
        # Pulse topmost only to raise the owner, then clear it before the
        # blocking native dialog request.
        if topmost_supported:
            owner.attributes("-topmost", True)
        owner.lift()
        owner.focus_force()
        owner.update_idletasks()
        if topmost_supported:
            owner.attributes("-topmost", False)
            owner.update_idletasks()
        return desktop_services.choose_directory(
            title=title,
            initial_dir=initial_dir,
            parent=owner,
        )
    finally:
        try:
            if topmost_supported:
                owner.attributes("-topmost", previous_topmost)
            if owner.winfo_exists():
                owner.lift()
                owner.focus_force()
        except Exception:
            pass


def _download_and_extract_to_selected_directory(
    save_dir: DirectorySelection, sample, *, progress_cb=None, cancel_cb=None
) -> str:
    """Download a sample map into the local path selected by DesktopServices."""
    from caveviewer.gui.sample_maps import download_and_extract_sample_map

    return download_and_extract_sample_map(
        save_dir.path,
        sample,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
    )


def _sample_download_notification_id(sample) -> str:
    """Return a stable per-sample desktop notification ID."""
    raw_key = (
        getattr(sample, "asset_name", "")
        or getattr(sample, "display_name", "")
        or "sample"
    )
    safe_key = "".join(
        character.lower() if character.isalnum() else "-"
        for character in str(raw_key)
    ).strip("-")
    return f"{_SAMPLE_DOWNLOAD_NOTIFICATION_PREFIX}.{safe_key or 'sample'}"


def _safe_desktop_notify(
    desktop_services: DesktopServices,
    notification_id: str,
    title: str,
    body: str,
    *,
    priority: str = "normal",
) -> None:
    """Send a best-effort desktop notification without affecting workflow."""
    try:
        desktop_services.notify(
            notification_id, title, body, priority=priority
        )
    except Exception:
        # Notification failures must never break a download. Linux portals
        # already fall back internally, but tests and unusual desktop sessions
        # may provide smaller DesktopServices implementations.
        pass


def _safe_desktop_withdraw(
    desktop_services: DesktopServices, notification_id: str
) -> None:
    """Withdraw a best-effort desktop notification without affecting workflow."""
    try:
        desktop_services.withdraw_notification(notification_id)
    except Exception:
        pass


def _safe_desktop_inhibit(
    desktop_services: DesktopServices, reason: str, *, parent
):
    """Keep the desktop awake during long work when the host supports it."""
    try:
        return desktop_services.inhibit_idle_suspend(reason, parent=parent)
    except Exception:
        return None


def _close_desktop_inhibitor(inhibitor) -> None:
    """Release a desktop inhibitor returned by DesktopServices."""
    if inhibitor is None:
        return
    try:
        inhibitor.close()
    except Exception:
        pass


def _download_sample_with_desktop_activity(
    desktop_services: DesktopServices,
    parent,
    save_dir: DirectorySelection,
    sample,
    *,
    progress_cb=None,
    cancel_cb=None,
) -> str:
    """Download a sample map while using native desktop activity affordances."""
    from caveviewer.gui.sample_maps import DownloadCancelled

    display_name = getattr(sample, "display_name", "sample map")
    notification_id = _sample_download_notification_id(sample)
    _safe_desktop_notify(
        desktop_services,
        notification_id,
        f"{APP_NAME} is downloading a sample map",
        f"Downloading {display_name}...",
    )
    inhibitor = _safe_desktop_inhibit(
        desktop_services,
        f"Downloading {display_name}",
        parent=parent,
    )

    try:
        result_path = _download_and_extract_to_selected_directory(
            save_dir,
            sample,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
        )
    except Exception as exc:
        _close_desktop_inhibitor(inhibitor)
        if isinstance(exc, DownloadCancelled):
            _safe_desktop_withdraw(desktop_services, notification_id)
        else:
            _safe_desktop_notify(
                desktop_services,
                notification_id,
                f"{APP_NAME} sample map download failed",
                f"Couldn't download {display_name}.",
                priority="high",
            )
        raise

    _close_desktop_inhibitor(inhibitor)
    _safe_desktop_notify(
        desktop_services,
        notification_id,
        f"{APP_NAME} sample map is ready",
        f"{display_name} finished downloading.",
    )
    return result_path


def show_sample_maps_dialog(
    parent,
    install_dir,
    *,
    ui_font_family: str | None = None,
    desktop_services: DesktopServices | None = None,
):
    """
    Shows the sample maps list as a modal dialog over `parent` (the
    splash screen's Tk root). Blocks until the person either picks a map
    to open (downloading it first if needed) or closes the window.

    Returns the local folder path of the map to open, or None if the
    dialog was closed without selecting one -- the caller (the splash
    screen) treats a non-None return exactly like a Browse-selected
    folder, so picking a sample map and browsing to your own folder are
    just two different ways of arriving at the same "here's a folder,
    go load it" outcome.
    """
    import tkinter as tk
    from caveviewer.gui.sample_maps import (
        DownloadCancelled, KNOWN_SAMPLE_MAPS, fetch_sample_map_catalog,
        is_sample_map_already_downloaded,
        existing_sample_map_path, local_sample_map_path,
    )

    _UI_FONT_FAMILY = (
        ui_font_family or get_splash_platform_adapter().ui_font_family()
    )
    desktop_services = desktop_services or get_desktop_services()

    selected_folder = [None]
    dialog_closed = [False]

    dialog = tk.Toplevel(parent)
    dialog.title("Sample Maps")
    dialog.configure(bg=_BG_COLOR)
    dialog.resizable(False, False)
    dialog.transient(parent)

    def _close_dialog():
        dialog_closed[0] = True
        try:
            dialog.destroy()
        except tk.TclError:
            pass

    dialog.protocol("WM_DELETE_WINDOW", _close_dialog)

    # Size everything in scaled pixels so the dialog is physically comparable
    # to the DPI-scaled splash window rather than looking small on high-DPI
    # displays.
    from caveviewer.gui.dpi_utils import tk_display_scale
    scale = tk_display_scale(parent)

    def _px(value):
        return int(round(value * scale))

    # Card metrics, defined up front so the window can open at its full,
    # comfortable size BEFORE the loading spinner shows. base_height is tuned
    # so the preload height closely matches the real populated-list height,
    # keeping the loading state and the list the SAME size (no shrink/grow
    # that would read as two separate windows).
    row_height = _px(90)
    if sys.platform == "win32":
        base_height = _px(64)
    elif sys.platform.startswith("linux"):
        base_height = _px(112)
    else:
        base_height = _px(96)
    window_w = _px(500)
    preload_h = base_height + row_height * max(1, len(KNOWN_SAMPLE_MAPS))

    # Compute the on-screen position ONCE and reuse it for every later resize.
    # Re-centering on each resize is what made the window visibly jump between
    # the loading state and the populated list; anchoring the top-left corner
    # lets the window grow in place instead of leaping to a new location.
    parent.update_idletasks()
    _screen_w = parent.winfo_screenwidth()
    _screen_h = parent.winfo_screenheight()
    _p_x = parent.winfo_rootx()
    _p_y = parent.winfo_rooty()
    _p_w = parent.winfo_width()
    anchor_x = max(8, min(_p_x + _p_w - window_w + _px(72), _screen_w - window_w - 8))
    anchor_y = max(8, min(_p_y + _px(40), _screen_h - preload_h - 8))
    dialog.geometry(f"{window_w}x{preload_h}+{anchor_x}+{anchor_y}")

    header = tk.Label(
        dialog, text="Sample Maps", font=(_UI_FONT_FAMILY, 14, "bold"),
        fg=_TITLE_COLOR, bg=_BG_COLOR,
    )
    header.pack(pady=(18, 4))

    sub = tk.Label(
        dialog, text="No map of your own? Try one of these.",
        font=(_UI_FONT_FAMILY, 9), fg=_INSTRUCTION_COLOR, bg=_BG_COLOR,
    )
    sub.pack(pady=(0, 14))

    # Pack the loading indicator with expand so it sits centered in the
    # already full-size window instead of clinging to the top.
    status_label = tk.Label(
        dialog, text="Loading available maps...", font=(_UI_FONT_FAMILY, 10),
        fg=_SUBTITLE_COLOR, bg=_BG_COLOR,
    )
    status_label.pack(expand=True)

    list_frame = tk.Frame(dialog, bg=_BG_COLOR)

    # Fetch the catalog on a background thread so the loading indicator can
    # actually animate. fetch_sample_map_catalog() makes a network request
    # that can take several seconds; running it inline would freeze the UI
    # and leave the "Loading..." text static with no sign of progress.
    fetch_holder = {}

    def _fetch_worker():
        fetch_holder["result"] = fetch_sample_map_catalog()

    threading.Thread(target=_fetch_worker, daemon=True).start()

    _spinner_frames = "|/-\\"
    _spinner_i = 0
    # Keep the indicator up for a short minimum so a fast (cached) fetch does
    # not flash the loading screen for a fraction of a second before the list
    # appears -- a brief, steady spinner reads better than a flicker.
    _min_spinner_seconds = 0.6
    _spinner_start = time.perf_counter()
    while ("result" not in fetch_holder
           or (time.perf_counter() - _spinner_start) < _min_spinner_seconds):
        if not dialog.winfo_exists():
            return selected_folder[0]
        status_label.config(
            text=f"Loading available maps  {_spinner_frames[_spinner_i % len(_spinner_frames)]}"
        )
        _spinner_i += 1
        dialog.update()
        time.sleep(0.12)

    catalog, error = fetch_holder["result"]

    status_label.destroy()

    # Only show the hard "couldn't load anything" error screen if there's
    # truly nothing to show at all -- in every other case (including a
    # failed network fetch), still show the list built from
    # KNOWN_SAMPLE_MAPS, since any map already downloaded previously
    # needs to stay openable regardless of whether THIS fetch succeeded.
    # This is the actual fix for sample maps becoming unreachable while
    # offline: a network failure used to unconditionally show this error
    # screen and never even check local disk for what's already there.
    if not catalog:
        dialog.geometry(f"{window_w}x{_px(220)}+{anchor_x}+{anchor_y}")
        tk.Label(
            dialog, text=f"Couldn't load the sample map list:\n\n{error}",
            font=(_UI_FONT_FAMILY, 9), fg=_INSTRUCTION_COLOR, bg=_BG_COLOR,
            wraplength=window_w - 60, justify="center",
        ).pack(pady=(10, 16))
        close_btn = tk.Button(
            dialog, text="Close", command=dialog.destroy,
            font=(_UI_FONT_FAMILY, 9), bg=_BG_COLOR, fg=_SUBTITLE_COLOR,
            relief="flat", borderwidth=1, highlightbackground=_BORDER_COLOR,
            cursor="hand2",
        )
        close_btn.pack(pady=(0, 14))
        dialog.wait_window()
        return None

    # A network error alongside a non-empty catalog means: every known
    # map is still listed (from KNOWN_SAMPLE_MAPS), but fresh
    # download_url/size info couldn't be fetched for any of them. Maps
    # already downloaded are completely unaffected by this (their Open
    # button works from local disk alone) -- only maps NOT yet
    # downloaded are actually impacted, since there's no fresh URL to
    # download them from until the network is back. Show a small,
    # non-blocking notice instead of refusing to show the list at all.
    extra_height = 0
    if error:
        extra_height = 50
        notice = tk.Label(
            dialog,
            text="Couldn't check for fresh download info -- maps you've already\n"
                 "downloaded still work below. New downloads need the internet.",
            font=(_UI_FONT_FAMILY, 8), fg=_INSTRUCTION_COLOR, bg=_BG_COLOR,
            justify="center",
        )
        notice.pack(pady=(0, 8))

    list_frame.pack(fill="both", expand=True, padx=20, pady=(0, 20))

    # Only a small, fixed set of sample maps is offered, so the list is
    # rendered directly with no scrolling. Rows are packed straight into
    # this frame.
    rows_frame = tk.Frame(list_frame, bg=_BG_COLOR)
    rows_frame.pack(fill="both", expand=True)

    def format_size(size_bytes):
        if size_bytes is None:
            return ""
        mb = size_bytes / (1024 * 1024)
        return f"{mb:.0f} MB"

    def _not_downloaded_detail_text(sample):
        if sample.download_url is None:
            return "Currently unavailable"
        return format_size(sample.size_bytes)

    # Dictionary to track progress bars and buttons for each sample
    progress_bars = {}
    action_buttons = {}
    detail_labels = {}
    downloaded_paths = {}  # Store result_path after download

    def _widget_exists(widget) -> bool:
        if widget is None:
            return False
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def _dialog_exists() -> bool:
        return not dialog_closed[0] and _widget_exists(dialog)

    # Resolve the last-used save directory once, up front. Doing this here
    # (rather than inside the "Save to..." click handler) keeps the click
    # path free of any filesystem stat -- a stale saved path on a slow or
    # disconnected volume could otherwise block and delay the folder
    # chooser from appearing after the button is pressed.
    remembered_save_dir = _load_last_sample_maps_dir()
    sample_maps_root_dir = remembered_save_dir or install_dir
    initial_save_dir = [sample_maps_root_dir]

    def _download_flow(sample):
        nonlocal sample_maps_root_dir
        if sample.download_url is None:
            show_info(
                f"{sample.display_name} isn't available for download right now "
                f"(its file wasn't found on the server, or the server couldn't be "
                f"reached). Try again later, or pick a different sample map.",
                parent=dialog,
            )
            return

        # Keep the OS-native chooser owned by and above this dialog. Without
        # an owner, some window managers place it behind the Sample Maps
        # window, making Save To appear unresponsive.
        save_dir = _ask_directory_in_front(
            desktop_services,
            dialog,
            title=f"Save {sample.display_name} to...",
            initial_dir=initial_save_dir[0],
        )
        if not save_dir:
            return  # User cancelled the directory selection

        _save_last_sample_maps_dir(save_dir.path)
        sample_maps_root_dir = save_dir.path
        initial_save_dir[0] = save_dir.path
        if not _dialog_exists():
            return

        # Reuse the existing Save/Open action area for cancellation, and show
        # the progress strip. Keeping the same widget avoids shifting the row
        # when a download begins.
        action_btn = action_buttons[sample.display_name]
        if not _widget_exists(action_btn):
            return
        progress_bar_container, progress_bar_canvas, progress_bar = progress_bars[sample.display_name]
        if not _widget_exists(progress_bar_canvas):
            return
        cancel_event = _activate_download_cancel_button(
            action_btn, _set_action_button
        )
        try:
            progress_bar_canvas.pack(fill="x", padx=14, pady=(6, 0))
            # Force layout update to get accurate canvas width
            dialog.update_idletasks()
        except tk.TclError:
            return

        def on_progress(downloaded, total):
            if cancel_event.is_set():
                raise DownloadCancelled("Sample map download cancelled")
            if not _dialog_exists() or not _widget_exists(progress_bar_canvas):
                cancel_event.set()
                raise DownloadCancelled("Sample map download cancelled")
            if total > 0:
                try:
                    frac = min(1.0, downloaded / total)
                    # Get the current width of the canvas (it fills the parent)
                    canvas_width = progress_bar_canvas.winfo_width()
                    if canvas_width > 1:  # winfo_width() returns 1 before widget is displayed
                        progress_bar_canvas.coords(progress_bar, 0, 0, int(canvas_width * frac), 4)
                    progress_bar_canvas.update()
                    if cancel_event.is_set() or not _dialog_exists():
                        cancel_event.set()
                        raise DownloadCancelled("Sample map download cancelled")
                except tk.TclError:
                    cancel_event.set()
                    raise DownloadCancelled("Sample map download cancelled")

        try:
            result_path = _download_sample_with_desktop_activity(
                desktop_services,
                dialog,
                save_dir,
                sample,
                progress_cb=on_progress,
                cancel_cb=cancel_event.is_set,
            )
        except Exception as e:
            if not _dialog_exists():
                return
            try:
                if _widget_exists(progress_bar_canvas):
                    progress_bar_canvas.pack_forget()
                    progress_bar_canvas.coords(progress_bar, 0, 0, 0, 4)
                action_btn = action_buttons.get(sample.display_name)
                if _widget_exists(action_btn):
                    _set_action_button(
                        action_btn,
                        "Save to...",
                        lambda s=sample: _download_flow(s),
                    )
            except tk.TclError:
                return
            if isinstance(e, DownloadCancelled):
                return
            show_error(
                f"Couldn't download {sample.display_name}:\n\n{e}",
                parent=dialog,
            )
            return

        if not _dialog_exists():
            return

        # Download succeeded - hide progress bar and show "Open Map" button
        try:
            if _widget_exists(progress_bar_canvas):
                progress_bar_canvas.pack_forget()
        except tk.TclError:
            return
        downloaded_paths[sample.display_name] = result_path
        
        # Update the same action-area button to Open.
        action_btn = action_buttons[sample.display_name]
        if not _widget_exists(action_btn):
            return
        _set_action_button(
            action_btn, "Open",
            lambda s=sample, rp=result_path: on_open_map(s, rp),
        )

    def on_open_map(sample, result_path):
        is_valid, error_message = _validate_selected_map_folder(result_path)
        if is_valid:
            selected_folder[0] = result_path
            _close_dialog()
            return

        show_warning(
            f"{sample.display_name} can't be opened:\n\n{error_message}\n\n"
            "Its files may have been moved or deleted. Download it again.",
            parent=dialog,
        )
        downloaded_paths.pop(sample.display_name, None)
        detail_label = detail_labels.get(sample.display_name)
        if detail_label is not None:
            detail_label.config(text=_not_downloaded_detail_text(sample))
        action_btn = action_buttons.get(sample.display_name)
        if action_btn is not None:
            _set_action_button(action_btn, "Save to...", lambda s=sample: _download_flow(s))

    def _open_installed_sample(result_path):
        selected_folder[0] = result_path
        _close_dialog()

    def _make_action_button(parent, text, command, enabled=True):
        # On macOS native tk.Button ignores bg/fg and renders as a gray
        # Aqua button, so use a Label styled to match the amber Tk buttons
        # used elsewhere. Other platforms honor tk.Button colors fine.
        if sys.platform == "darwin":
            btn = tk.Label(
                parent, text=text, font=(_UI_FONT_FAMILY, 10, "bold"),
                bg=_BUTTON_BG if enabled else _BORDER_COLOR,
                fg=_BUTTON_FG if enabled else _INSTRUCTION_COLOR,
                width=10, anchor="center",
                padx=8, pady=6,
                cursor="hand2" if enabled else "arrow",
                takefocus=enabled,
                highlightthickness=1,
                highlightbackground=_BUTTON_BORDER_COLOR if enabled else _BORDER_COLOR,
                highlightcolor=_BUTTON_BORDER_COLOR if enabled else _BORDER_COLOR,
            )
            btn._cv_command = command
            btn._cv_enabled = enabled

            def _invoke(_event=None, _b=btn):
                if getattr(_b, "_cv_enabled", False) and _b._cv_command:
                    _b._cv_command()
                return "break"

            if enabled:
                btn.bind("<Button-1>", _invoke)
                btn.bind("<Return>", _invoke)
                btn.bind("<space>", _invoke)
                btn.bind("<Enter>", lambda _event, _b=btn: _b.config(bg=_BUTTON_HOVER_BG))
                btn.bind("<Leave>", lambda _event, _b=btn: _b.config(bg=_BUTTON_BG))
            return btn

        return tk.Button(
            parent, text=text, command=command if enabled else None,
            font=(_UI_FONT_FAMILY, 10, "bold"),
            bg=_BUTTON_BG if enabled else _BORDER_COLOR,
            fg=_BUTTON_FG if enabled else _INSTRUCTION_COLOR,
            activebackground=_BUTTON_HOVER_BG, activeforeground=_BUTTON_FG,
            relief="flat", borderwidth=1,
            highlightthickness=1,
            highlightbackground=_BUTTON_BORDER_COLOR if enabled else _BORDER_COLOR,
            highlightcolor=_BUTTON_BORDER_COLOR if enabled else _BORDER_COLOR,
            width=10,
            padx=8, pady=6,
            state="normal" if enabled else "disabled",
            cursor="hand2" if enabled else "arrow",
        )

    def _set_action_button(btn, text, command):
        if sys.platform == "darwin":
            btn._cv_command = command
            btn._cv_enabled = True
            btn.config(text=text)
        else:
            btn.config(text=text, command=command)

    def on_pick(sample):
        sample_path = existing_sample_map_path(sample_maps_root_dir, sample)

        # Only treat the map as openable if its folder still contains a usable
        # map. The folder can go stale between opening this dialog and clicking
        # (moved or partially deleted), and is_sample_map_already_downloaded()
        # only checks that the folder is non-empty -- not that it holds a real
        # .glb / .obj+.mtl / cache. Validate the same way the splash screen's
        # Open flow does so a broken folder shows a friendly message instead of
        # crashing the viewer with "No supported model file found".
        if is_sample_map_already_downloaded(sample_maps_root_dir, sample):
            is_valid, error_message = _validate_selected_map_folder(sample_path)
            if is_valid:
                _open_installed_sample(sample_path)
                return

            show_warning(
                f"{sample.display_name} can't be opened:\n\n{error_message}\n\n"
                "Its files may have been moved or deleted. Download it again.",
                parent=dialog,
            )
            detail_label = detail_labels.get(sample.display_name)
            if detail_label is not None:
                detail_label.config(text=_not_downloaded_detail_text(sample))
            action_btn = action_buttons.get(sample.display_name)
            if action_btn is not None:
                _set_action_button(action_btn, "Save to...", lambda s=sample: _download_flow(s))
            return

        _download_flow(sample)

    for sample in catalog:
        row = tk.Frame(rows_frame, bg=_PANEL_COLOR)
        row.pack(fill="x", pady=6)

        # Thin progress strip reserved at the BOTTOM of the card so it never
        # leaves an empty band above the map name; it only fills in during an
        # active download.
        progress_bar_container = tk.Frame(row, bg=_PANEL_COLOR, height=10)
        progress_bar_container.pack(side="bottom", fill="x", padx=0, pady=0)
        progress_bar_container.pack_propagate(False)  # Maintain fixed height

        progress_bar_canvas = tk.Canvas(
            progress_bar_container,
            height=4,
            bg=DARK_THEME.entry_background,
            highlightthickness=0,
        )
        # Don't pack initially - will be packed when download starts
        progress_bar = progress_bar_canvas.create_rectangle(0, 0, 0, 4, fill=_BUTTON_BG, width=0)
        progress_bars[sample.display_name] = (progress_bar_container, progress_bar_canvas, progress_bar)

        # Content frame - contains text and button side by side
        content_frame = tk.Frame(row, bg=_PANEL_COLOR)
        content_frame.pack(fill="both", expand=True)

        text_frame = tk.Frame(content_frame, bg=_PANEL_COLOR)
        text_frame.pack(side="left", fill="both", expand=True, padx=(16, 8), pady=12)

        # Inner block packed with expand keeps the name + size vertically
        # centered within the card rather than pinned to the top.
        text_inner = tk.Frame(text_frame, bg=_PANEL_COLOR)
        text_inner.pack(expand=True, anchor="w")

        tk.Label(
            text_inner, text=sample.display_name, font=(_UI_FONT_FAMILY, 11, "bold"),
            fg=_SUBTITLE_COLOR, bg=_PANEL_COLOR, anchor="w",
        ).pack(anchor="w")

        already_have = is_sample_map_already_downloaded(sample_maps_root_dir, sample)
        if already_have:
            detail_text = "Downloaded"
        else:
            detail_text = _not_downloaded_detail_text(sample)

        detail_label = tk.Label(
            text_inner, text=detail_text, font=(_UI_FONT_FAMILY, 9),
            fg=_INSTRUCTION_COLOR, bg=_PANEL_COLOR, anchor="w",
        )
        detail_label.pack(anchor="w", pady=(2, 0))
        detail_labels[sample.display_name] = detail_label

        btn_text = "Open" if already_have else "Save to..."
        btn_enabled = already_have or sample.download_url is not None

        action_btn = _make_action_button(
            content_frame, btn_text,
            lambda s=sample: on_pick(s), enabled=btn_enabled,
        )
        action_btn.pack(side="right", padx=(8, 16), pady=12)
        action_buttons[sample.display_name] = action_btn

    # Re-fit the window to the actual rendered content, keeping the SAME
    # anchor position computed up front so it never repositions (no jump).
    # Height is floored at the preload size so the populated list is never
    # shorter than the loading state -- that shrink is what read as "two
    # windows." The Windows preload base above is tighter than other
    # platforms so this still avoids the oversized blank footer there.
    dialog.update_idletasks()
    max_width = min(_screen_w - anchor_x - 8, _px(760))
    max_height = min(_screen_h - anchor_y - 8, _px(760))
    fitted_width = max(window_w, min(dialog.winfo_reqwidth(), max_width))
    fitted_height = max(preload_h, min(dialog.winfo_reqheight(), max_height))
    dialog.geometry(f"{fitted_width}x{fitted_height}+{anchor_x}+{anchor_y}")

    dialog.wait_window()

    return selected_folder[0]


def _center_over_parent(window, parent, width, height):
    parent.update_idletasks()
    px = parent.winfo_rootx()
    py = parent.winfo_rooty()
    pw = parent.winfo_width()
    screen_w = parent.winfo_screenwidth()
    screen_h = parent.winfo_screenheight()

    # Match the advanced settings dialog behavior: keep the child near
    # the parent's upper-right corner, but let it protrude past the
    # parent's right edge so the overlap is visually obvious.
    protrusion_x = 72
    inset_y = 40
    desired_x = px + pw - width + protrusion_x
    desired_y = py + inset_y
    clamped_x = max(8, min(desired_x, screen_w - width - 8))
    clamped_y = max(8, min(desired_y, screen_h - height - 8))
    window.geometry(f"{width}x{height}+{clamped_x}+{clamped_y}")


def _load_last_sample_maps_dir() -> str | None:
    """Load the last directory where the user saved sample maps."""
    try:
        with open(_last_sample_maps_dir_file(), "r", encoding="utf-8") as f:
            path = f.read().strip()
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    return None


def _save_last_sample_maps_dir(path: str) -> None:
    """Save the directory where the user saved sample maps."""
    try:
        if not path or not os.path.isdir(path):
            return
        write_text_atomic(_last_sample_maps_dir_file(), path)
    except Exception:
        pass
