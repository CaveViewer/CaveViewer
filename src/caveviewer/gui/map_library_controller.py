"""State and row models for the splash map library."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StandardLibraryMapRow:
    """Presentation model for one standard-library map row."""

    key: str
    title: str
    detail: str
    action_text: str
    downloaded: bool
    enabled: bool = True
    result_path: str | None = None


@dataclass(frozen=True)
class LibraryDownloadCleanup:
    """State that the Tk owner must cancel or release while closing."""

    cancel_event: Any | None
    after_id: Any | None
    inhibitor: Any | None


@dataclass
class ActiveLibraryDownload:
    """Mutable state for the one standard-library download allowed at a time."""

    cancel_event: Any | None = None
    after_id: Any | None = None
    inhibitor: Any | None = None
    map_name: str | None = None
    thread: Any | None = None

    @property
    def in_progress(self) -> bool:
        return self.cancel_event is not None


@dataclass
class CatalogFetchState:
    """Mutable state for the background standard-library catalog fetch."""

    loading: bool = False
    after_id: Any | None = None
    queue: Any | None = None
    pending_map: Any | None = None
    error: str | None = None


@dataclass(frozen=True)
class CatalogFetchCompletion:
    """Result of applying a finished standard-library catalog fetch."""

    maps: tuple[Any, ...]
    error: str | None
    pending_map: Any | None


@dataclass(frozen=True)
class CatalogFetchCleanup:
    """State that the Tk owner must cancel while closing a catalog fetch."""

    after_id: Any | None


class MapLibraryController:
    """
    Own non-widget state for the splash map library.

    Tk owns widget mutation and timer scheduling. This controller owns row
    identity, catalog entries, remembered downloaded paths, and the current
    catalog/download lifecycle state so those decisions can be tested without
    constructing Tk widgets.
    """

    def __init__(self, standard_library_maps) -> None:
        self.standard_library_maps = tuple(standard_library_maps)
        self.catalog_by_key = {
            self.map_key(library_map): library_map
            for library_map in self.standard_library_maps
        }
        self.downloaded_paths: dict[str, str] = {}
        self.active_download = ActiveLibraryDownload()
        self.catalog_fetch = CatalogFetchState()

    @staticmethod
    def map_key(library_map) -> str:
        """Return the stable row key for a standard-library map."""
        return (
            getattr(library_map, "catalog_id", None)
            or getattr(library_map, "display_name", "")
        )

    def resolve_catalog_entry(self, library_map):
        """Return a catalog-refreshed map entry when available."""
        return self.catalog_by_key.get(self.map_key(library_map), library_map)

    @staticmethod
    def action_text(*, downloaded: bool) -> str:
        """Return the primary row action for the current download state."""
        return "Open" if downloaded else "Get"

    @staticmethod
    def size_text(library_map) -> str:
        """Return the compact size label for a standard-library map."""
        size_bytes = getattr(library_map, "size_bytes", None)
        if size_bytes:
            return f"{size_bytes / (1024 * 1024):.0f} MB"
        return ""

    def status_text(self, library_map, *, downloaded: bool) -> str:
        """Return the secondary row text for a standard-library map."""
        if downloaded:
            return "Downloaded"
        return self.size_text(self.resolve_catalog_entry(library_map))

    def row(
        self,
        library_map,
        *,
        downloaded: bool,
        enabled: bool = True,
        action_text: str | None = None,
        result_path: str | None = None,
    ) -> StandardLibraryMapRow:
        """Build a row presentation model for a standard-library map."""
        key = self.map_key(library_map)
        if downloaded and result_path:
            self.downloaded_paths[key] = result_path
        return StandardLibraryMapRow(
            key=key,
            title=getattr(library_map, "display_name", ""),
            detail=self.status_text(library_map, downloaded=downloaded),
            action_text=action_text or self.action_text(downloaded=downloaded),
            downloaded=downloaded,
            enabled=enabled,
            result_path=result_path,
        )

    def set_downloaded_path(
        self,
        library_map,
        *,
        downloaded: bool,
        result_path: str | None,
    ) -> None:
        """Remember or forget the downloaded path for one standard-library map."""
        key = self.map_key(library_map)
        if downloaded and result_path:
            self.downloaded_paths[key] = result_path
        else:
            self.downloaded_paths.pop(key, None)

    def downloaded_path(
        self,
        library_map,
        *,
        is_downloaded: bool,
        existing_path: str | None,
    ) -> str | None:
        """Return the remembered or existing downloaded path for a map."""
        key = self.map_key(library_map)
        result_path = self.downloaded_paths.get(key)
        if result_path:
            return result_path
        return existing_path if is_downloaded else None

    def begin_download(self, library_map, *, cancel_event, inhibitor) -> None:
        """Record the active standard-library download."""
        self.active_download = ActiveLibraryDownload(
            cancel_event=cancel_event,
            after_id=None,
            inhibitor=inhibitor,
            map_name=self.map_key(library_map),
            thread=None,
        )

    def set_download_after_id(self, after_id) -> None:
        self.active_download.after_id = after_id

    def attach_download_thread(self, thread) -> None:
        self.active_download.thread = thread

    def should_handle_download_poll(self, cancel_event) -> bool:
        return self.active_download.cancel_event is cancel_event

    def clear_active_download(self) -> Any | None:
        """Clear active download state and return any inhibitor to release."""
        inhibitor = self.active_download.inhibitor
        self.active_download = ActiveLibraryDownload()
        return inhibitor

    def close_active_download(self) -> LibraryDownloadCleanup:
        """Clear active download state and return cleanup handles."""
        cleanup = LibraryDownloadCleanup(
            cancel_event=self.active_download.cancel_event,
            after_id=self.active_download.after_id,
            inhibitor=self.active_download.inhibitor,
        )
        self.active_download = ActiveLibraryDownload()
        return cleanup

    def set_pending_catalog_map(self, library_map) -> None:
        self.catalog_fetch.pending_map = library_map

    def begin_catalog_fetch(self, fetch_queue) -> bool:
        """
        Mark a catalog fetch as active.

        Returns ``False`` when an existing fetch is already active.
        """
        if self.catalog_fetch.loading:
            return False
        self.catalog_fetch.loading = True
        self.catalog_fetch.queue = fetch_queue
        self.catalog_fetch.error = None
        return True

    def set_catalog_after_id(self, after_id) -> None:
        self.catalog_fetch.after_id = after_id

    def complete_catalog_fetch(
        self,
        catalog,
        error: str | None,
    ) -> CatalogFetchCompletion:
        """Apply a fetched catalog and return the pending map to continue."""
        self.catalog_fetch.loading = False
        self.catalog_fetch.queue = None
        self.catalog_fetch.error = error
        for library_map in catalog:
            self.catalog_by_key[self.map_key(library_map)] = library_map
        pending_map = self.catalog_fetch.pending_map
        self.catalog_fetch.pending_map = None
        return CatalogFetchCompletion(
            maps=tuple(catalog),
            error=error,
            pending_map=pending_map,
        )

    def replace_standard_library_maps(self, standard_library_maps) -> tuple[Any, ...]:
        """Replace catalog rows after a remote refresh reconciles visibility."""
        self.standard_library_maps = tuple(standard_library_maps)
        self.catalog_by_key = {
            self.map_key(library_map): library_map
            for library_map in self.standard_library_maps
        }
        return self.standard_library_maps

    def close_catalog_fetch(self) -> CatalogFetchCleanup:
        """Clear catalog fetch state and return cleanup handles."""
        cleanup = CatalogFetchCleanup(after_id=self.catalog_fetch.after_id)
        self.catalog_fetch = CatalogFetchState()
        return cleanup
