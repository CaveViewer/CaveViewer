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


@dataclass(frozen=True)
class SampleDownloadProgress:
    """Worker-to-UI progress message for a map library download."""

    downloaded_bytes: int
    total_bytes: int | None


@dataclass(frozen=True)
class SampleDownloadSucceeded:
    """Worker-to-UI completion message for a map library download."""

    result_path: str


@dataclass(frozen=True)
class SampleDownloadFailed:
    """Worker-to-UI failure message for a map library download."""

    error: Exception


_SAMPLE_DOWNLOAD_NOTIFICATION_PREFIX = "caveviewer.map-library-download"


def download_and_extract_to_selected_directory(
    save_dir: DirectorySelection, sample, *, progress_cb=None, cancel_cb=None
) -> str:
    """Download a standard library map into the selected local path."""
    from caveviewer.gui.sample_maps import download_and_extract_sample_map

    return download_and_extract_sample_map(
        save_dir.path,
        sample,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
    )


def run_sample_download_worker(
    save_dir: DirectorySelection,
    sample,
    cancel_event: threading.Event,
    result_queue,
) -> None:
    """Download/extract a standard library map without touching Tk state."""
    from caveviewer.gui.sample_maps import DownloadCancelled

    def on_progress(downloaded_bytes, total_bytes) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled("Map library download cancelled")
        result_queue.put(
            SampleDownloadProgress(
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
        result_queue.put(SampleDownloadFailed(exc))
    else:
        result_queue.put(SampleDownloadSucceeded(result_path))


def start_sample_download_worker(
    save_dir: DirectorySelection,
    sample,
    cancel_event: threading.Event,
    result_queue,
) -> threading.Thread:
    """Start the owned worker thread for a map library download."""
    worker = threading.Thread(
        target=run_sample_download_worker,
        args=(save_dir, sample, cancel_event, result_queue),
        name="CaveViewer-map-library-download",
        # Partial zip/extraction cleanup must reach its finally blocks.
        daemon=False,
    )
    worker.start()
    return worker


def sample_download_notification_id(sample) -> str:
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


def safe_desktop_notify(
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


def safe_desktop_withdraw(
    desktop_services: DesktopServices, notification_id: str
) -> None:
    """Withdraw a best-effort desktop notification without affecting workflow."""
    try:
        desktop_services.withdraw_notification(notification_id)
    except Exception:
        pass


def safe_desktop_inhibit(
    desktop_services: DesktopServices, reason: str, *, parent
):
    """Keep the desktop awake during long work when the host supports it."""
    try:
        return desktop_services.inhibit_idle_suspend(reason, parent=parent)
    except Exception:
        return None


def close_desktop_inhibitor(inhibitor) -> None:
    """Release a desktop inhibitor returned by DesktopServices."""
    if inhibitor is None:
        return
    try:
        inhibitor.close()
    except Exception:
        pass


def download_sample_with_desktop_activity(
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
    from caveviewer.gui.sample_maps import DownloadCancelled

    display_name = getattr(sample, "display_name", "standard library map")
    notification_id = sample_download_notification_id(sample)
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
