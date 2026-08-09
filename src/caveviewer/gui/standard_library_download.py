"""Worker coordination for GUI-owned map library downloads.

Tk surfaces own all widget state on the Tk thread.  This module owns the
worker-thread handoff used by those surfaces: download/extract work happens in
a non-daemon worker, progress and terminal events cross a queue boundary, and
the underlying map library installer keeps its existing temporary-file cleanup
and staged publish behavior.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from caveviewer.gui.platform import DesktopServices, DirectorySelection
from caveviewer.gui.platform.desktop_notifications import (
    send_desktop_notification,
    withdraw_desktop_notification,
)
from caveviewer.gui.platform.desktop_inhibition import (
    acquire_idle_suspend_inhibitor,
    release_desktop_inhibitor,
)


@dataclass(frozen=True)
class StandardLibraryDownloadProgress:
    """Worker-to-UI progress message for a map library download."""

    downloaded_bytes: int
    total_bytes: int | None


@dataclass(frozen=True)
class StandardLibraryDownloadSucceeded:
    """Worker-to-UI completion message for a map library download."""

    result_path: str


@dataclass(frozen=True)
class StandardLibraryDownloadFailed:
    """Worker-to-UI failure message for a map library download."""

    error: Exception


_STANDARD_LIBRARY_DOWNLOAD_NOTIFICATION_PREFIX = "caveviewer.map-library-download"


def download_and_extract_to_selected_directory(
    save_dir: DirectorySelection, sample, *, progress_cb=None, cancel_cb=None
) -> str:
    """Download a standard library map into the selected local path."""
    from caveviewer.gui.standard_library_maps import download_and_extract_standard_library_map

    return download_and_extract_standard_library_map(
        save_dir.path,
        sample,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
    )


def run_standard_library_download_worker(
    save_dir: DirectorySelection,
    sample,
    cancel_event: threading.Event,
    result_queue,
) -> None:
    """Download/extract a standard library map without touching Tk state."""
    from caveviewer.gui.standard_library_maps import DownloadCancelled

    def on_progress(downloaded_bytes, total_bytes) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled("Map library download cancelled")
        result_queue.put(
            StandardLibraryDownloadProgress(
                max(0, int(downloaded_bytes)),
                None if total_bytes is None else max(0, int(total_bytes)),
            )
        )
        if cancel_event.is_set():
            raise DownloadCancelled("Map library download cancelled")

    try:
        result_path = download_and_extract_to_selected_directory(
            save_dir,
            sample,
            progress_cb=on_progress,
            cancel_cb=cancel_event.is_set,
        )
    except Exception as exc:
        result_queue.put(StandardLibraryDownloadFailed(exc))
    else:
        result_queue.put(StandardLibraryDownloadSucceeded(result_path))


def start_standard_library_download_worker(
    save_dir: DirectorySelection,
    sample,
    cancel_event: threading.Event,
    result_queue,
) -> threading.Thread:
    """Start the owned worker thread for a map library download."""
    worker = threading.Thread(
        target=run_standard_library_download_worker,
        args=(save_dir, sample, cancel_event, result_queue),
        name="CaveViewer-map-library-download",
        # Partial zip/extraction cleanup must reach its finally blocks.
        daemon=False,
    )
    worker.start()
    return worker


def standard_library_download_notification_id(sample) -> str:
    """Return a stable per-map desktop notification ID."""
    source_id = getattr(sample, "source_id", "") or "map-library"
    map_key = (
        getattr(sample, "asset_name", "")
        or getattr(sample, "display_name", "")
        or "standard-library-map"
    )
    raw_key = f"{source_id}-{map_key}"
    safe_key = "".join(
        character.lower() if character.isalnum() else "-"
        for character in str(raw_key)
    ).strip("-")
    return f"{_STANDARD_LIBRARY_DOWNLOAD_NOTIFICATION_PREFIX}.{safe_key or 'standard-library-map'}"


def safe_desktop_notify(
    desktop_services: DesktopServices,
    notification_id: str,
    title: str,
    body: str,
    *,
    priority: str = "normal",
) -> None:
    """Send a best-effort desktop notification without affecting workflow."""
    send_desktop_notification(
        desktop_services,
        notification_id,
        title,
        body,
        priority=priority,
    )


def safe_desktop_withdraw(
    desktop_services: DesktopServices, notification_id: str
) -> None:
    """Withdraw a best-effort desktop notification without affecting workflow."""
    withdraw_desktop_notification(desktop_services, notification_id)


def safe_desktop_inhibit(
    desktop_services: DesktopServices, reason: str, *, parent
):
    """Keep the desktop awake during long work when the host supports it."""
    return acquire_idle_suspend_inhibitor(
        desktop_services,
        reason,
        parent=parent,
    )


def close_desktop_inhibitor(inhibitor) -> None:
    """Release a desktop inhibitor returned by DesktopServices."""
    release_desktop_inhibitor(inhibitor)


def download_standard_library_with_desktop_activity(
    desktop_services: DesktopServices,
    parent,
    save_dir: DirectorySelection,
    sample,
    *,
    progress_cb=None,
    cancel_cb=None,
    notify_desktop: bool = True,
) -> str:
    """Download a standard library map with native desktop activity affordances."""
    from caveviewer.gui.standard_library_maps import DownloadCancelled

    display_name = getattr(sample, "display_name", "standard library map")
    notification_id = standard_library_download_notification_id(sample)
    if notify_desktop:
        safe_desktop_notify(
            desktop_services,
            notification_id,
            "Map Library Download Started",
            f"Downloading {display_name}",
        )
    inhibitor = safe_desktop_inhibit(
        desktop_services,
        f"Downloading {display_name}",
        parent=parent,
    )

    try:
        result_path = download_and_extract_to_selected_directory(
            save_dir,
            sample,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
        )
    except Exception as exc:
        close_desktop_inhibitor(inhibitor)
        if isinstance(exc, DownloadCancelled):
            if notify_desktop:
                safe_desktop_withdraw(desktop_services, notification_id)
        elif notify_desktop:
            safe_desktop_notify(
                desktop_services,
                notification_id,
                "Map Library Download Failed",
                f"Couldn’t download {display_name}",
                priority="high",
            )
        raise

    close_desktop_inhibitor(inhibitor)
    if notify_desktop:
        safe_desktop_notify(
            desktop_services,
            notification_id,
            "Map Library Download Ready",
            f"{display_name} finished downloading",
        )
    return result_path
