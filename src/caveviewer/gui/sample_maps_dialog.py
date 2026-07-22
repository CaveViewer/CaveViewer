"""
caveviewer.gui.sample_maps_dialog

The map library window lists the standard library cave scans (see
caveviewer.gui.sample_maps), shows their size, and lets the person download
(or, if already downloaded, directly open) whichever one they want -- a
one-click way to try CaveViewer without having their own scan yet.

Kept separate from caveviewer.gui.sample_maps (pure fetch/download/extract
logic, no UI) the same way update workflow code is kept separate from
caveviewer.gui.update_checker -- this module is purely the Tkinter presentation
and the glue that drives the other one.
"""

from __future__ import annotations

import os
import queue
import threading
import time

from caveviewer.gui.dialog_style import (
    DIALOG_BODY_PAD_X,
    DIALOG_BODY_PAD_Y,
    DIALOG_PANEL_BORDER,
    create_dialog_action_button,
    create_dialog_notice,
    set_dialog_action_button,
    set_dialog_notice,
)
from caveviewer.gui.map_selection import (
    validate_selected_map_folder as _validate_selected_map_folder,
)
from caveviewer.gui.platform import (
    DesktopServices,
    DirectorySelection,
    get_desktop_services,
    get_splash_platform_adapter,
)
from caveviewer.gui.preference_paths import migrate_state_file, write_text_atomic
from caveviewer.gui.sample_map_download import (
    SampleDownloadFailed as _SampleDownloadFailed,
    SampleDownloadProgress as _SampleDownloadProgress,
    SampleDownloadSucceeded as _SampleDownloadSucceeded,
    close_desktop_inhibitor as _close_desktop_inhibitor,
    safe_desktop_inhibit as _safe_desktop_inhibit,
    start_sample_download_worker as _start_sample_download_worker,
)
from caveviewer.gui.tk_feedback import FeedbackKind, show_feedback
from caveviewer.gui.tk_shortcuts import bind_primary_shortcut
from caveviewer.gui.tk_theme import DARK_THEME


_BG_COLOR = DARK_THEME.background
_PANEL_COLOR = DARK_THEME.panel
_TITLE_COLOR = DARK_THEME.title
_SUBTITLE_COLOR = DARK_THEME.body_text
_INSTRUCTION_COLOR = DARK_THEME.secondary_text
_BUTTON_BG = DARK_THEME.primary_button
_BUTTON_BORDER_COLOR = DARK_THEME.primary_button_border


def _last_sample_maps_dir_file() -> str:
    """Resolve state lazily so tests and portable runs remain isolated."""
    return migrate_state_file(
        "last_sample_maps_dir", ".caveviewer_last_sample_maps_dir"
    )


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
        # during the chooser call can then put Map Library above the chooser.
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


def _format_sample_size(size_bytes) -> str:
    """Return a compact user-facing download size."""
    if size_bytes is None:
        return ""
    mb = size_bytes / (1024 * 1024)
    return f"{mb:.0f} MB"


def _sample_detail_text(sample, *, downloaded: bool) -> str:
    """Return the secondary row text for a standard library map."""
    if downloaded:
        return "Ready to open"
    if getattr(sample, "download_url", None) is None:
        return "Download unavailable"
    return _format_sample_size(getattr(sample, "size_bytes", None))


def _sample_action_text(sample, *, downloaded: bool) -> str:
    """Return the primary action label for a standard library map row."""
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
    Shows the map library list as a modal dialog over `parent` (the
    splash screen's Tk root). Blocks until the person either picks a map
    to open (downloading it first if needed) or closes the window.

    Returns the local folder path of the map to open, or None if the
    dialog was closed without selecting one -- the caller (the splash
    screen) treats a non-None return exactly like a Browse-selected
    folder, so picking a standard library map and browsing to your own folder are
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
    catalog_load_done = [None]
    active_download = {
        "cancel_event": None,
        "after_id": None,
        "inhibitor": None,
        "sample_name": None,
        "thread": None,
    }

    dialog = tk.Toplevel(parent)
    dialog.withdraw()
    dialog.title("Map Library")
    dialog.configure(bg=_BG_COLOR)
    dialog.resizable(False, False)
    dialog.transient(parent)

    def _cancel_active_download_for_close() -> None:
        cancel_event = active_download.get("cancel_event")
        if cancel_event is not None:
            cancel_event.set()

        after_id = active_download.get("after_id")
        active_download["after_id"] = None
        if after_id is not None:
            try:
                dialog.after_cancel(after_id)
            except tk.TclError:
                pass

        inhibitor = active_download.get("inhibitor")
        active_download.update(
            {
                "cancel_event": None,
                "after_id": None,
                "inhibitor": None,
                "sample_name": None,
                "thread": None,
            }
        )
        _close_desktop_inhibitor(inhibitor)

    def _close_dialog():
        dialog_closed[0] = True
        _cancel_active_download_for_close()
        done_var = catalog_load_done[0]
        if done_var is not None:
            try:
                done_var.set(True)
            except tk.TclError:
                pass
        try:
            dialog.destroy()
        except tk.TclError:
            pass

    dialog.protocol("WM_DELETE_WINDOW", _close_dialog)
    dialog.bind("<Escape>", lambda _event: _close_dialog())
    bind_primary_shortcut(dialog, "w", lambda _event: _close_dialog())

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
    content.pack(
        fill="both",
        expand=True,
        padx=_px(DIALOG_BODY_PAD_X),
        pady=(_px(DIALOG_BODY_PAD_Y), _px(DIALOG_BODY_PAD_Y)),
    )

    header = tk.Label(
        content,
        text="Map Library",
        font=(_UI_FONT_FAMILY, 15, "bold"),
        fg=_TITLE_COLOR,
        bg=_BG_COLOR,
        anchor="w",
        justify="left",
    )
    header.pack(anchor="w")

    sub = tk.Label(
        content,
        text="Download a standard library map folder, or open one you already have.",
        font=(_UI_FONT_FAMILY, 10),
        fg=_INSTRUCTION_COLOR,
        bg=_BG_COLOR,
        anchor="w",
        justify="left",
        wraplength=window_w - _px(64),
    )
    sub.pack(fill="x", pady=(_px(4), _px(14)))

    notice_frame, notice_label = create_dialog_notice(
        content,
        font=(_UI_FONT_FAMILY, 9),
        wraplength=window_w - _px(92),
    )

    def _set_notice(message: str, *, kind: FeedbackKind = "info") -> None:
        set_dialog_notice(notice_frame, notice_label, message, kind=kind)
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
    # that can take several seconds; running it inline would freeze the UI.
    # The worker only publishes to a thread-safe queue; Tk state is read and
    # mutated by the after() poller on the Tk thread.
    fetch_queue = queue.Queue(maxsize=1)
    catalog_result = {"catalog": None, "error": None}
    catalog_ready = tk.BooleanVar(dialog, value=False)
    catalog_load_done[0] = catalog_ready

    def _fetch_worker():
        try:
            result = fetch_sample_map_catalog()
        except Exception as exc:
            result = ([], f"Couldn't load the map library: {exc}")
        fetch_queue.put(result)

    threading.Thread(
        target=_fetch_worker,
        name="CaveViewer-map-library-catalog",
        daemon=True,
    ).start()

    _spinner_frames = "|/-\\"
    _spinner_i = [0]
    _spinner_start = time.perf_counter()
    _min_spinner_seconds = 0.6
    _pending_fetch_result = [None]

    def _dialog_alive() -> bool:
        if dialog_closed[0]:
            return False
        try:
            return bool(dialog.winfo_exists())
        except tk.TclError:
            return False

    def _poll_catalog_fetch() -> None:
        if not _dialog_alive():
            catalog_ready.set(True)
            return

        if _pending_fetch_result[0] is None:
            try:
                _pending_fetch_result[0] = fetch_queue.get_nowait()
            except queue.Empty:
                pass

        elapsed = time.perf_counter() - _spinner_start
        if _pending_fetch_result[0] is None or elapsed < _min_spinner_seconds:
            try:
                status_label.config(
                    text=(
                        "Loading available maps  "
                        f"{_spinner_frames[_spinner_i[0] % len(_spinner_frames)]}"
                    )
                )
            except tk.TclError:
                catalog_ready.set(True)
                return
            _spinner_i[0] += 1
            delay_ms = 120
            if _pending_fetch_result[0] is not None:
                delay_ms = max(
                    1,
                    min(
                        delay_ms,
                        int(round((_min_spinner_seconds - elapsed) * 1000)),
                    ),
                )
            dialog.after(delay_ms, _poll_catalog_fetch)
            return

        catalog, error = _pending_fetch_result[0]
        catalog_result["catalog"] = catalog
        catalog_result["error"] = error
        catalog_ready.set(True)

    dialog.after(0, _poll_catalog_fetch)
    try:
        dialog.wait_variable(catalog_ready)
    except tk.TclError:
        return selected_folder[0]
    finally:
        catalog_load_done[0] = None

    if not _dialog_alive() or catalog_result["catalog"] is None:
        return selected_folder[0]

    catalog = catalog_result["catalog"]
    error = catalog_result["error"]

    status_label.destroy()

    # Only show the hard "couldn't load anything" error screen if there's
    # truly nothing to show at all -- in every other case (including a
    # failed network fetch), still show the list built from
    # KNOWN_SAMPLE_MAPS, since any map already downloaded previously
    # needs to stay openable regardless of whether THIS fetch succeeded.
    # This is the actual fix for map library entries becoming unreachable while
    # offline: a network failure used to unconditionally show this error
    # screen and never even check local disk for what's already there.
    if not catalog:
        dialog.geometry(f"{window_w}x{loading_window_h}+{anchor_x}+{anchor_y}")
        _set_notice(
            f"Couldn’t load the map library. {error or 'Try again later.'}",
            kind="error",
        )
        tk.Label(
            content,
            text="No map library entries are available right now.",
            font=(_UI_FONT_FAMILY, 10),
            fg=_INSTRUCTION_COLOR,
            bg=_BG_COLOR,
            wraplength=window_w - _px(64),
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(_px(8), _px(16)))
        close_btn = create_dialog_action_button(
            content,
            "Close",
            dialog.destroy,
            font=(_UI_FONT_FAMILY, 9),
            kind="secondary",
            padx=12,
            pady=6,
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

    # The dialog intentionally supports the small curated map-library set offered
    # here, not a large catalog.  A future larger collection belongs behind a
    # separate map-library link instead of putting a scrollbar into this small
    # chooser.
    rows_frame = tk.Frame(
        list_frame,
        bg=_PANEL_COLOR,
        highlightthickness=1,
        highlightbackground=DIALOG_PANEL_BORDER,
        highlightcolor=DIALOG_PANEL_BORDER,
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

    def _set_sample_action(
        sample,
        *,
        downloaded: bool,
        enabled: bool = True,
        result_path: str | None = None,
    ) -> None:
        action_btn = action_buttons.get(sample.display_name)
        if not _widget_exists(action_btn):
            return
        if downloaded:
            open_path = (
                result_path
                or downloaded_paths.get(sample.display_name)
                or existing_sample_map_path(sample_maps_root_dir, sample)
            )
            _set_action_button(
                action_btn,
                "Open Map",
                lambda s=sample, rp=open_path: on_open_map(s, rp),
                enabled=enabled,
            )
            return
        _set_action_button(
            action_btn,
            _sample_action_text(sample, downloaded=False),
            lambda s=sample: _download_flow(s),
            enabled=enabled and _sample_action_enabled(sample, downloaded=False),
        )

    def _set_non_active_sample_actions_enabled(active_sample, enabled: bool) -> None:
        active_name = active_sample.display_name
        for row_sample in catalog:
            if row_sample.display_name == active_name:
                continue
            row_result_path = downloaded_paths.get(row_sample.display_name)
            row_downloaded = bool(row_result_path) or is_sample_map_already_downloaded(
                sample_maps_root_dir, row_sample
            )
            _set_sample_action(
                row_sample,
                downloaded=row_downloaded,
                enabled=enabled,
                result_path=(
                    row_result_path
                    or existing_sample_map_path(sample_maps_root_dir, row_sample)
                ),
            )

    def _reset_progress_bar(sample) -> None:
        progress_parts = progress_bars.get(sample.display_name)
        if progress_parts is None:
            return
        _progress_bar_container, progress_bar_canvas, progress_bar = progress_parts
        if not _widget_exists(progress_bar_canvas):
            return
        progress_bar_canvas.pack_forget()
        progress_bar_canvas.coords(progress_bar, 0, 0, 0, _px(4))

    def _apply_download_progress(
        sample, progress: _SampleDownloadProgress
    ) -> None:
        progress_parts = progress_bars.get(sample.display_name)
        if progress_parts is None:
            return
        _progress_bar_container, progress_bar_canvas, progress_bar = progress_parts
        if not _widget_exists(progress_bar_canvas):
            return
        if progress.total_bytes is None or progress.total_bytes <= 0:
            return
        frac = min(1.0, progress.downloaded_bytes / progress.total_bytes)
        canvas_width = progress_bar_canvas.winfo_width()
        if canvas_width > 1:
            progress_bar_canvas.coords(
                progress_bar,
                0,
                0,
                int(canvas_width * frac),
                _px(4),
            )

    def _clear_active_download(sample) -> None:
        inhibitor = active_download.get("inhibitor")
        active_download.update(
            {
                "cancel_event": None,
                "after_id": None,
                "inhibitor": None,
                "sample_name": None,
                "thread": None,
            }
        )
        _close_desktop_inhibitor(inhibitor)
        if _dialog_exists():
            _set_non_active_sample_actions_enabled(sample, True)

    def _finish_download_success(sample, result_path: str) -> None:
        if not _dialog_exists():
            _clear_active_download(sample)
            return
        _reset_progress_bar(sample)
        downloaded_paths[sample.display_name] = result_path
        detail_label = detail_labels.get(sample.display_name)
        if detail_label is not None:
            detail_label.config(text=_sample_detail_text(sample, downloaded=True))
        _set_sample_action(
            sample,
            downloaded=True,
            result_path=result_path,
        )
        _clear_active_download(sample)

    def _finish_download_failure(sample, error: Exception) -> None:
        if not _dialog_exists():
            _clear_active_download(sample)
            return
        _reset_progress_bar(sample)
        _set_sample_action(sample, downloaded=False)
        _clear_active_download(sample)
        if isinstance(error, DownloadCancelled):
            return
        _show_inline_feedback(
            f"Couldn't download {sample.display_name}: {error}",
            kind="error",
        )

    def _schedule_download_poll(sample, message_queue, cancel_event) -> None:
        if active_download.get("cancel_event") is not cancel_event:
            return
        if not _dialog_exists():
            cancel_event.set()
            _clear_active_download(sample)
            return
        active_download["after_id"] = dialog.after(
            80,
            lambda s=sample, q=message_queue, c=cancel_event: _poll_download_queue(
                s, q, c
            ),
        )

    def _poll_download_queue(sample, message_queue, cancel_event) -> None:
        if active_download.get("cancel_event") is not cancel_event:
            return
        active_download["after_id"] = None
        if not _dialog_exists():
            cancel_event.set()
            _clear_active_download(sample)
            return

        latest_progress = None
        terminal_message = None
        while True:
            try:
                message = message_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(message, _SampleDownloadProgress):
                latest_progress = message
            else:
                terminal_message = message
                break

        if latest_progress is not None:
            try:
                _apply_download_progress(sample, latest_progress)
            except tk.TclError:
                cancel_event.set()
                _clear_active_download(sample)
                return

        if isinstance(terminal_message, _SampleDownloadSucceeded):
            _finish_download_success(sample, terminal_message.result_path)
            return
        if isinstance(terminal_message, _SampleDownloadFailed):
            _finish_download_failure(sample, terminal_message.error)
            return

        try:
            _schedule_download_poll(sample, message_queue, cancel_event)
        except tk.TclError:
            cancel_event.set()
            _clear_active_download(sample)

    def _download_flow(sample):
        nonlocal sample_maps_root_dir
        if active_download.get("cancel_event") is not None:
            _show_inline_feedback(
                "Finish or cancel the current map library download before starting another.",
                kind="info",
            )
            return
        if sample.download_url is None:
            _show_inline_feedback(
                f"{sample.display_name} isn't available for download right now "
                f"(its file wasn't found on the server, or the server couldn't be "
                f"reached). Try again later, or pick a different standard library map.",
                kind="info",
            )
            return

        # Keep the OS-native chooser owned by and above this dialog. Without
        # an owner, some window managers place it behind the Map Library
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
        progress_parts = progress_bars[sample.display_name]
        _progress_bar_container, progress_bar_canvas, _progress_bar = progress_parts
        if not _widget_exists(progress_bar_canvas):
            return
        try:
            progress_bar_canvas.pack(fill="x", padx=_px(14), pady=(_px(6), 0))
            # Force layout update to get an initial canvas width; subsequent
            # progress updates are handled by the Tk event loop, not update().
            dialog.update_idletasks()
        except tk.TclError:
            return

        cancel_event = threading.Event()
        message_queue = queue.Queue()

        def request_cancel() -> None:
            cancel_event.set()
            if _widget_exists(action_btn):
                _set_action_button(
                    action_btn,
                    "Cancelling…",
                    lambda: None,
                    enabled=False,
                )

        _set_action_button(action_btn, "Cancel", request_cancel)
        _set_non_active_sample_actions_enabled(sample, False)
        active_download.update(
            {
                "cancel_event": cancel_event,
                "after_id": None,
                "inhibitor": _safe_desktop_inhibit(
                    desktop_services,
                    f"Downloading {sample.display_name}",
                    parent=dialog,
                ),
                "sample_name": sample.display_name,
                "thread": None,
            }
        )

        try:
            worker = _start_sample_download_worker(
                save_dir,
                sample,
                cancel_event,
                message_queue,
            )
        except RuntimeError as exc:
            _reset_progress_bar(sample)
            _set_sample_action(sample, downloaded=False)
            _clear_active_download(sample)
            _show_inline_feedback(
                f"Couldn't start the {sample.display_name} download: {exc}",
                kind="error",
            )
            return

        active_download["thread"] = worker
        _schedule_download_poll(sample, message_queue, cancel_event)

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
        return create_dialog_action_button(
            parent,
            text,
            command,
            font=(_UI_FONT_FAMILY, 10, "bold"),
            kind="primary",
            enabled=enabled,
            width=12,
            padx=8, pady=6,
        )

    def _set_action_button(btn, text, command, *, enabled: bool = True):
        set_dialog_action_button(
            btn,
            text=text,
            command=command,
            enabled=enabled,
            kind="primary",
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

    # Match the preferences dialog behavior: keep the child near
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
    """Load the last directory where the user saved map library entries."""
    try:
        with open(_last_sample_maps_dir_file(), "r", encoding="utf-8") as f:
            path = f.read().strip()
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    return None


def _save_last_sample_maps_dir(path: str) -> None:
    """Save the directory where the user saved map library entries."""
    try:
        if not path or not os.path.isdir(path):
            return
        write_text_atomic(_last_sample_maps_dir_file(), path)
    except Exception:
        pass
