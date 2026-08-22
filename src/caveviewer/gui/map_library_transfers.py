"""Worker and scheduler lifecycle owners for Map Library transfers."""

from __future__ import annotations

import queue
from collections.abc import Callable
from typing import Any

from caveviewer.gui.map_library_controller import MapLibraryController
from caveviewer.gui.map_library_sources import (
    GITHUB_RELEASE_MAP_SOURCE_ID,
    MapCatalogRefresh,
)
from caveviewer.gui.standard_library_download import (
    StandardLibraryDownloadFailed,
    StandardLibraryDownloadProgress,
    StandardLibraryDownloadSucceeded,
)


class MapLibraryCatalogWorkflow:
    """Own catalog worker startup, result polling, and close cancellation."""

    def __init__(
        self,
        *,
        controller: MapLibraryController,
        scheduler: Any,
        splash_exists: Callable[[], bool],
        fetch_catalog: Callable[[], tuple[MapCatalogRefresh, ...]],
        start_worker: Callable[[Callable[[], None]], None],
        queue_factory: Callable[[], Any],
        on_complete: Callable[[Any], None],
    ) -> None:
        self._controller = controller
        self._scheduler = scheduler
        self._splash_exists = splash_exists
        self._fetch_catalog = fetch_catalog
        self._start_worker = start_worker
        self._queue_factory = queue_factory
        self._on_complete = on_complete

    def start(self, pending_map=None) -> None:
        if pending_map is not None:
            self._controller.set_pending_catalog_map(pending_map)
        if self._controller.catalog_fetch.loading:
            return
        result_queue = self._queue_factory()
        if not self._controller.begin_catalog_fetch(result_queue):
            return

        def fetch_worker() -> None:
            try:
                result = self._fetch_catalog()
                if not isinstance(result, tuple) or not all(
                    isinstance(refresh, MapCatalogRefresh) for refresh in result
                ):
                    raise TypeError(
                        "Map catalog service must return a tuple of "
                        "MapCatalogRefresh values"
                    )
            except Exception as exc:
                result = (
                    MapCatalogRefresh(
                        source_id=GITHUB_RELEASE_MAP_SOURCE_ID,
                        maps=(),
                        authoritative=False,
                        error=f"Couldn't load the map library: {exc}",
                    ),
                )
            result_queue.put(result)

        self._start_worker(fetch_worker)
        self._schedule_poll()

    def _schedule_poll(self) -> None:
        if not self._splash_exists():
            return
        after_id = self._scheduler.after(120, self.poll)
        self._controller.set_catalog_after_id(after_id)

    def poll(self) -> None:
        self._controller.set_catalog_after_id(None)
        if not self._splash_exists():
            return
        result_queue = self._controller.catalog_fetch.queue
        if result_queue is None:
            return
        try:
            result = result_queue.get_nowait()
        except queue.Empty:
            self._schedule_poll()
            return
        self._on_complete(self._controller.complete_catalog_fetch(result))

    def close(self) -> None:
        cleanup = self._controller.close_catalog_fetch()
        if cleanup.after_id is None:
            return
        try:
            self._scheduler.after_cancel(cleanup.after_id)
        except Exception:
            pass


class MapLibraryDownloadWorkflow:
    """Own one download worker, queue, cancellation, inhibitor, and polling."""

    def __init__(
        self,
        *,
        controller: MapLibraryController,
        scheduler: Any,
        splash_exists: Callable[[], bool],
        start_worker: Callable[..., Any],
        queue_factory: Callable[[], Any],
        cancel_event_factory: Callable[[], Any],
        selection_factory: Callable[[str], Any],
        inhibit: Callable[..., Any],
        close_inhibitor: Callable[[Any], None],
        desktop_services: Any,
        on_progress: Callable[[Any, StandardLibraryDownloadProgress], None],
        on_success: Callable[[Any, str], None],
        on_failure: Callable[[Any, Exception], None],
    ) -> None:
        self._controller = controller
        self._scheduler = scheduler
        self._splash_exists = splash_exists
        self._start_worker = start_worker
        self._queue_factory = queue_factory
        self._cancel_event_factory = cancel_event_factory
        self._selection_factory = selection_factory
        self._inhibit = inhibit
        self._close_inhibitor = close_inhibitor
        self._desktop_services = desktop_services
        self._on_progress = on_progress
        self._on_success = on_success
        self._on_failure = on_failure

    def start(
        self,
        library_map,
        root_dir: str,
        *,
        parent: Any,
        cancel_event: Any | None = None,
    ) -> Any:
        cancel_event = cancel_event or self._cancel_event_factory()
        message_queue = self._queue_factory()
        inhibitor = self._inhibit(
            self._desktop_services,
            f"Downloading {library_map.display_name}",
            parent=parent,
        )
        self._controller.begin_download(
            library_map, cancel_event=cancel_event, inhibitor=inhibitor
        )
        try:
            worker = self._start_worker(
                self._selection_factory(root_dir),
                library_map,
                cancel_event,
                message_queue,
            )
        except Exception:
            self._clear()
            raise
        self._controller.attach_download_thread(worker)
        self._schedule(library_map, message_queue, cancel_event)
        return cancel_event

    def _schedule(self, library_map, message_queue, cancel_event) -> None:
        if not self._controller.should_handle_download_poll(cancel_event):
            return
        if not self._splash_exists():
            cancel_event.set()
            self._clear()
            return
        after_id = self._scheduler.after(
            80, lambda: self.poll(library_map, message_queue, cancel_event)
        )
        self._controller.set_download_after_id(after_id)

    def poll(self, library_map, message_queue, cancel_event) -> None:
        if not self._controller.should_handle_download_poll(cancel_event):
            return
        self._controller.set_download_after_id(None)
        if not self._splash_exists():
            cancel_event.set()
            self._clear()
            return
        latest_progress = None
        terminal = None
        while True:
            try:
                message = message_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(message, StandardLibraryDownloadProgress):
                latest_progress = message
            else:
                terminal = message
                break
        if latest_progress is not None:
            self._on_progress(library_map, latest_progress)
        if isinstance(terminal, StandardLibraryDownloadSucceeded):
            self._on_success(library_map, terminal.result_path)
            return
        if isinstance(terminal, StandardLibraryDownloadFailed):
            self._on_failure(library_map, terminal.error)
            return
        self._schedule(library_map, message_queue, cancel_event)

    def _clear(self) -> None:
        inhibitor = self._controller.clear_active_download()
        self._close_inhibitor(inhibitor)

    def close(self) -> None:
        cleanup = self._controller.close_active_download()
        if cleanup.cancel_event is not None:
            cleanup.cancel_event.set()
        if cleanup.after_id is not None:
            try:
                self._scheduler.after_cancel(cleanup.after_id)
            except Exception:
                pass
        self._close_inhibitor(cleanup.inhibitor)
