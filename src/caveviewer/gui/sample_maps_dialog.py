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
from caveviewer.gui.platform import (
    DesktopServices,
    DirectorySelection,
    get_desktop_services,
    get_splash_platform_adapter,
)
from caveviewer.gui.preferences import migrate_state_file, write_text_atomic
from caveviewer.gui.tk_feedback import FeedbackKind, show_feedback
from caveviewer.gui.tk_theme import DARK_THEME


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
    notify_desktop: bool = True,
) -> str:
    """Download a sample map while using native desktop activity affordances."""
    from caveviewer.gui.sample_maps import DownloadCancelled

    display_name = getattr(sample, "display_name", "sample map")
    notification_id = _sample_download_notification_id(sample)
    if notify_desktop:
        _safe_desktop_notify(
            desktop_services,
            notification_id,
            "Sample Map Download Started",
            f"Downloading {display_name}",
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
            if notify_desktop:
                _safe_desktop_withdraw(desktop_services, notification_id)
        elif notify_desktop:
            _safe_desktop_notify(
                desktop_services,
                notification_id,
                "Sample Map Download Failed",
                f"Couldn’t download {display_name}",
                priority="high",
            )
        raise

    _close_desktop_inhibitor(inhibitor)
    if notify_desktop:
        _safe_desktop_notify(
            desktop_services,
            notification_id,
            "Sample Map Ready",
            f"{display_name} finished downloading",
        )
    return result_path


def _format_sample_size(size_bytes) -> str:
    """Return a compact user-facing download size."""
    if size_bytes is None:
        return ""
    mb = size_bytes / (1024 * 1024)
    return f"{mb:.0f} MB"


def _sample_detail_text(sample, *, downloaded: bool) -> str:
    """Return the secondary row text for a sample map."""
    if downloaded:
        return "Ready to open"
    if getattr(sample, "download_url", None) is None:
        return "Download unavailable"
    return _format_sample_size(getattr(sample, "size_bytes", None))


def _sample_action_text(sample, *, downloaded: bool) -> str:
    """Return the primary action label for a sample map row."""
    if downloaded:
        return "Open Map"
    if getattr(sample, "download_url", None) is None:
        return "Unavailable"
    return "Download…"


def _sample_action_enabled(sample, *, downloaded: bool) -> bool:
    """Return whether the primary row action should be clickable."""
    return downloaded or getattr(sample, "download_url", None) is not None


def _sample_catalog_notice_text(error: str | None) -> str:
    """Return the non-blocking notice shown when fresh metadata is unavailable."""
    if not error:
        return ""
    return (
        "Couldn’t check for fresh download info. Maps you already downloaded "
        "can still be opened; new downloads need the internet."
    )


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
        existing_sample_map_path,
    )

    _UI_FONT_FAMILY = (
        ui_font_family or get_splash_platform_adapter().ui_font_family()
    )
    desktop_services = desktop_services or get_desktop_services()

    selected_folder = [None]
    dialog_closed = [False]

    dialog = tk.Toplevel(parent)
    dialog.withdraw()
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
    dialog.bind("<Escape>", lambda _event: _close_dialog())
    dialog.bind("<Control-w>", lambda _event: _close_dialog())

    def _present_dialog(initial_focus=None) -> None:
        try:
            dialog.update_idletasks()
            dialog.deiconify()
            dialog.lift(parent)
            dialog.wait_visibility()
            dialog.grab_set()
            dialog.focus_force()
            if initial_focus is not None and initial_focus.winfo_exists():
                initial_focus.focus_set()
        except tk.TclError:
            pass

    # Size everything in scaled pixels so the dialog is physically comparable
    # to the DPI-scaled splash window rather than looking small on high-DPI
    # displays.
    from caveviewer.gui.dpi_utils import tk_display_scale
    scale = tk_display_scale(parent)

    def _px(value):
        return int(round(value * scale))

    min_window_w = _px(540)
    loading_window_h = _px(360)
    row_height = _px(78)
    window_w = _px(560)
    preload_h = max(
        loading_window_h,
        _px(178) + row_height * max(1, min(4, len(KNOWN_SAMPLE_MAPS))),
    )
    dialog.minsize(min_window_w, 1)

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

    content = tk.Frame(dialog, bg=_BG_COLOR)
    content.pack(fill="both", expand=True, padx=_px(24), pady=(_px(22), _px(20)))

    header = tk.Label(
        content,
        text="Sample Maps",
        font=(_UI_FONT_FAMILY, 15, "bold"),
        fg=_TITLE_COLOR,
        bg=_BG_COLOR,
        anchor="w",
        justify="left",
    )
    header.pack(anchor="w")

    sub = tk.Label(
        content,
        text="Download a sample map folder, or open one you already have.",
        font=(_UI_FONT_FAMILY, 10),
        fg=_INSTRUCTION_COLOR,
        bg=_BG_COLOR,
        anchor="w",
        justify="left",
        wraplength=window_w - _px(64),
    )
    sub.pack(fill="x", pady=(_px(4), _px(14)))

    notice_frame = tk.Frame(
        content,
        bg=_PANEL_COLOR,
        highlightthickness=1,
        highlightbackground=_BORDER_COLOR,
        highlightcolor=_BORDER_COLOR,
    )
    notice_frame.grid_columnconfigure(1, weight=1)
    notice_accent = tk.Frame(notice_frame, bg=_BUTTON_BG, width=_px(4))
    notice_accent.grid(row=0, column=0, sticky="ns")
    notice_label = tk.Label(
        notice_frame,
        text="",
        font=(_UI_FONT_FAMILY, 9),
        fg=_SUBTITLE_COLOR,
        bg=_PANEL_COLOR,
        anchor="w",
        justify="left",
        wraplength=window_w - _px(92),
    )
    notice_label.grid(row=0, column=1, sticky="ew", padx=(_px(10), _px(12)), pady=_px(9))

    def _set_notice(message: str, *, kind: FeedbackKind = "info") -> None:
        colors = {
            "info": (_PANEL_COLOR, _BUTTON_BG),
            "warning": ("#211b10", _BUTTON_BG),
            "error": ("#261416", DARK_THEME.invalid_border),
        }.get(kind, (_PANEL_COLOR, _BUTTON_BG))
        notice_frame.config(bg=colors[0], highlightbackground=colors[1], highlightcolor=colors[1])
        notice_accent.config(bg=colors[1])
        notice_label.config(text=message, bg=colors[0])
        if not notice_frame.winfo_manager():
            notice_frame.pack(fill="x", pady=(0, _px(14)))

    # Pack the loading indicator with expand so it sits centered in the
    # already full-size window instead of clinging to the top.
    status_label = tk.Label(
        content, text="Loading available maps…", font=(_UI_FONT_FAMILY, 10),
        fg=_SUBTITLE_COLOR, bg=_BG_COLOR,
    )
    status_label.pack(expand=True)
    _present_dialog()

    list_frame = tk.Frame(content, bg=_BG_COLOR)

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
        dialog.geometry(f"{window_w}x{loading_window_h}+{anchor_x}+{anchor_y}")
        _set_notice(
            f"Couldn’t load the sample map list. {error or 'Try again later.'}",
            kind="error",
        )
        tk.Label(
            content,
            text="No sample maps are available right now.",
            font=(_UI_FONT_FAMILY, 10),
            fg=_INSTRUCTION_COLOR,
            bg=_BG_COLOR,
            wraplength=window_w - _px(64),
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(_px(8), _px(16)))
        close_btn = tk.Button(
            content, text="Close", command=dialog.destroy,
            font=(_UI_FONT_FAMILY, 9), bg=_BG_COLOR, fg=_SUBTITLE_COLOR,
            relief="flat", borderwidth=1, highlightbackground=_BORDER_COLOR,
            cursor="hand2",
        )
        close_btn.pack(anchor="e", pady=(0, _px(2)))
        close_btn.focus_set()
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
    if error:
        _set_notice(
            _sample_catalog_notice_text(error),
            kind="warning",
        )

    list_frame.pack(fill="x")

    # The dialog intentionally supports the curated 2-3 sample maps offered
    # here, not a large catalog.  A future larger collection belongs behind a
    # separate map-library link instead of putting a scrollbar into this small
    # chooser.
    rows_frame = tk.Frame(
        list_frame,
        bg=_PANEL_COLOR,
        highlightthickness=1,
        highlightbackground=_BORDER_COLOR,
        highlightcolor=_BORDER_COLOR,
    )
    rows_frame.pack(fill="x")

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

    def _show_inline_feedback(message: str, *, kind: FeedbackKind = "info") -> None:
        show_feedback(
            dialog,
            message,
            kind=kind,
            duration_ms=9000 if kind == "error" else 7000,
            font=(_UI_FONT_FAMILY, 10),
            max_wraplength=420,
        )

    # Resolve the last-used save directory once, up front. Doing this here
    # (rather than inside the "Download…" click handler) keeps the click
    # path free of any filesystem stat -- a stale saved path on a slow or
    # disconnected volume could otherwise block and delay the folder
    # chooser from appearing after the button is pressed.
    remembered_save_dir = _load_last_sample_maps_dir()
    sample_maps_root_dir = remembered_save_dir or install_dir
    initial_save_dir = [sample_maps_root_dir]

    def _download_flow(sample):
        nonlocal sample_maps_root_dir
        if sample.download_url is None:
            _show_inline_feedback(
                f"{sample.display_name} isn't available for download right now "
                f"(its file wasn't found on the server, or the server couldn't be "
                f"reached). Try again later, or pick a different sample map.",
                kind="info",
            )
            return

        # Keep the OS-native chooser owned by and above this dialog. Without
        # an owner, some window managers place it behind the Sample Maps
        # window, making Download appear unresponsive.
        save_dir = _ask_directory_in_front(
            desktop_services,
            dialog,
            title=f"Choose Download Folder for {sample.display_name}",
            initial_dir=initial_save_dir[0],
        )
        if not save_dir:
            return  # User cancelled the directory selection

        _save_last_sample_maps_dir(save_dir.path)
        sample_maps_root_dir = save_dir.path
        initial_save_dir[0] = save_dir.path
        if not _dialog_exists():
            return

        # Reuse the existing action area for cancellation, and show
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
            progress_bar_canvas.pack(fill="x", padx=_px(14), pady=(_px(6), 0))
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
                        progress_bar_canvas.coords(
                            progress_bar,
                            0,
                            0,
                            int(canvas_width * frac),
                            _px(4),
                        )
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
                notify_desktop=False,
            )
        except Exception as e:
            if not _dialog_exists():
                return
            try:
                if _widget_exists(progress_bar_canvas):
                    progress_bar_canvas.pack_forget()
                    progress_bar_canvas.coords(progress_bar, 0, 0, 0, _px(4))
                action_btn = action_buttons.get(sample.display_name)
                if _widget_exists(action_btn):
                    _set_action_button(
                        action_btn,
                        _sample_action_text(sample, downloaded=False),
                        lambda s=sample: _download_flow(s),
                        enabled=_sample_action_enabled(sample, downloaded=False),
                    )
            except tk.TclError:
                return
            if isinstance(e, DownloadCancelled):
                return
            _show_inline_feedback(
                f"Couldn't download {sample.display_name}: {e}",
                kind="error",
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
        detail_label = detail_labels.get(sample.display_name)
        if detail_label is not None:
            detail_label.config(text=_sample_detail_text(sample, downloaded=True))

        # Update the same action-area button to Open Map.
        action_btn = action_buttons[sample.display_name]
        if not _widget_exists(action_btn):
            return
        _set_action_button(
            action_btn, "Open Map",
            lambda s=sample, rp=result_path: on_open_map(s, rp),
        )

    def on_open_map(sample, result_path):
        is_valid, error_message = _validate_selected_map_folder(result_path)
        if is_valid:
            selected_folder[0] = result_path
            _close_dialog()
            return

        _show_inline_feedback(
            f"{sample.display_name} can't be opened: {error_message} "
            "Its files may have been moved or deleted. Download it again.",
            kind="warning",
        )
        downloaded_paths.pop(sample.display_name, None)
        detail_label = detail_labels.get(sample.display_name)
        if detail_label is not None:
            detail_label.config(text=_sample_detail_text(sample, downloaded=False))
        action_btn = action_buttons.get(sample.display_name)
        if action_btn is not None:
            _set_action_button(
                action_btn,
                _sample_action_text(sample, downloaded=False),
                lambda s=sample: _download_flow(s),
                enabled=_sample_action_enabled(sample, downloaded=False),
            )

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
                width=12, anchor="center",
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
            width=12,
            padx=8, pady=6,
            state="normal" if enabled else "disabled",
            cursor="hand2" if enabled else "arrow",
        )

    def _set_action_button(btn, text, command, *, enabled: bool = True):
        if sys.platform == "darwin":
            btn._cv_command = command
            btn._cv_enabled = enabled
            btn.config(
                text=text,
                bg=_BUTTON_BG if enabled else _BORDER_COLOR,
                fg=_BUTTON_FG if enabled else _INSTRUCTION_COLOR,
                cursor="hand2" if enabled else "arrow",
                takefocus=enabled,
                highlightbackground=_BUTTON_BORDER_COLOR if enabled else _BORDER_COLOR,
                highlightcolor=_BUTTON_BORDER_COLOR if enabled else _BORDER_COLOR,
            )
        else:
            btn.config(
                text=text,
                command=command if enabled else None,
                bg=_BUTTON_BG if enabled else _BORDER_COLOR,
                fg=_BUTTON_FG if enabled else _INSTRUCTION_COLOR,
                highlightbackground=_BUTTON_BORDER_COLOR if enabled else _BORDER_COLOR,
                highlightcolor=_BUTTON_BORDER_COLOR if enabled else _BORDER_COLOR,
                state="normal" if enabled else "disabled",
                cursor="hand2" if enabled else "arrow",
            )

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

            _show_inline_feedback(
                f"{sample.display_name} can't be opened: {error_message} "
                "Its files may have been moved or deleted. Download it again.",
                kind="warning",
            )
            detail_label = detail_labels.get(sample.display_name)
            if detail_label is not None:
                detail_label.config(text=_sample_detail_text(sample, downloaded=False))
            action_btn = action_buttons.get(sample.display_name)
            if action_btn is not None:
                _set_action_button(
                    action_btn,
                    _sample_action_text(sample, downloaded=False),
                    lambda s=sample: _download_flow(s),
                    enabled=_sample_action_enabled(sample, downloaded=False),
                )
            return

        _download_flow(sample)

    for index, sample in enumerate(catalog):
        row = tk.Frame(rows_frame, bg=_PANEL_COLOR)
        row.pack(fill="x")
        row.grid_columnconfigure(0, weight=1)

        # Thin progress strip reserved at the bottom of the row so it never
        # leaves an empty band above the map name; it only fills in during an
        # active download.
        progress_bar_container = tk.Frame(row, bg=_PANEL_COLOR, height=_px(8))
        progress_bar_container.grid(row=1, column=0, columnspan=2, sticky="ew")
        progress_bar_container.pack_propagate(False)  # Maintain fixed height

        progress_bar_canvas = tk.Canvas(
            progress_bar_container,
            height=_px(4),
            bg=DARK_THEME.entry_background,
            highlightthickness=0,
        )
        # Don't pack initially - will be packed when download starts
        progress_bar = progress_bar_canvas.create_rectangle(
            0, 0, 0, _px(4), fill=_BUTTON_BG, width=0
        )
        progress_bars[sample.display_name] = (progress_bar_container, progress_bar_canvas, progress_bar)

        text_frame = tk.Frame(row, bg=_PANEL_COLOR)
        text_frame.grid(row=0, column=0, sticky="ew", padx=(_px(16), _px(12)), pady=_px(13))
        text_frame.grid_columnconfigure(0, weight=1)

        tk.Label(
            text_frame, text=sample.display_name, font=(_UI_FONT_FAMILY, 11, "bold"),
            fg=_SUBTITLE_COLOR, bg=_PANEL_COLOR, anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        already_have = is_sample_map_already_downloaded(sample_maps_root_dir, sample)
        detail_text = _sample_detail_text(sample, downloaded=already_have)

        detail_label = tk.Label(
            text_frame, text=detail_text, font=(_UI_FONT_FAMILY, 9),
            fg=_INSTRUCTION_COLOR, bg=_PANEL_COLOR, anchor="w",
        )
        detail_label.grid(row=1, column=0, sticky="ew", pady=(_px(3), 0))
        detail_labels[sample.display_name] = detail_label

        btn_text = _sample_action_text(sample, downloaded=already_have)
        btn_enabled = _sample_action_enabled(sample, downloaded=already_have)

        action_btn = _make_action_button(
            row, btn_text,
            lambda s=sample: on_pick(s), enabled=btn_enabled,
        )
        action_btn.grid(row=0, column=1, sticky="e", padx=(_px(8), _px(16)), pady=_px(13))
        action_buttons[sample.display_name] = action_btn

        if index < len(catalog) - 1:
            separator = tk.Frame(rows_frame, bg=DARK_THEME.entry_border, height=1)
            separator.pack(fill="x")

    # Re-fit the window to the actual rendered content, keeping the same
    # anchor position computed up front so it never repositions. This avoids
    # the large blank area that appeared when the dialog was sized like a
    # scrollable catalog.
    dialog.update_idletasks()
    max_width = min(_screen_w - anchor_x - 8, _px(760))
    max_height = min(_screen_h - anchor_y - 8, _px(760))
    fitted_width = max(window_w, min(dialog.winfo_reqwidth(), max_width))
    fitted_height = min(dialog.winfo_reqheight(), max_height)
    dialog.geometry(f"{fitted_width}x{fitted_height}+{anchor_x}+{anchor_y}")
    for action_btn in action_buttons.values():
        try:
            enabled = getattr(action_btn, "_cv_enabled", None)
            if enabled is None:
                enabled = str(action_btn.cget("state")) != "disabled"
            if enabled:
                action_btn.focus_set()
                break
        except tk.TclError:
            pass

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
