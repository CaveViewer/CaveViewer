"""Workflow orchestration for the splash Map Library."""

from __future__ import annotations

import hashlib
import os
import queue
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from caveviewer.gui.cache_rebuild_controller import (
    CacheRebuildFailed,
    CacheRebuildJobController,
    CacheRebuildPaused,
    CacheRebuildProgress,
    CacheRebuildStarted,
    CacheRebuildSucceeded,
)
from caveviewer.gui.cave_metadata import (
    CaveMetadata,
    CaveMetadataCatalog,
    CaveMetadataMatch,
)
from caveviewer.gui.map_cache_management import (
    has_managed_map_cache,
    remove_managed_map_cache,
)
from caveviewer.gui.map_cache_rebuild import (
    CacheRebuildPreflight,
    probe_map_library_cache_rebuild,
)
from caveviewer.gui.map_history import remove_recent_map_path
from caveviewer.gui.map_library import recent_map_entry, recent_map_key
from caveviewer.gui.map_library_controller import (
    MapLibraryController,
    StandardLibraryMapAvailability,
    StandardLibraryMapRow,
)
from caveviewer.gui.map_library_sources import (
    MapCatalogRefresh,
    default_map_library_catalog_service,
)
from caveviewer.gui.map_library_transfers import (
    MapLibraryCatalogWorkflow,
    MapLibraryDownloadWorkflow,
)
from caveviewer.gui.map_library_cache_rebuild_workflow import (
    MapLibraryCacheRebuildWorkflow,
)
from caveviewer.gui.map_library_panel import (
    MapLibraryMenuAction,
    MapLibraryPanel,
    MapLibraryRowWidgets,
)
from caveviewer.gui.guided_dive_playback import (
    GuidedDivePlaybackPreflight,
    guided_dive_menu_decision,
    guided_dive_playback_preflight,
    guided_dive_trace_directory,
)
from caveviewer.gui.features import FeatureDecision
from caveviewer.gui.platform import (
    DesktopServiceError,
    DesktopServices,
    DirectorySelection,
)
from caveviewer.gui.platform.file_selection import (
    choose_authorized_file,
    file_selection_preflight,
)
from caveviewer.gui.platform.desktop_notifications import send_desktop_notification
from caveviewer.gui.standard_library_download import (
    StandardLibraryDownloadProgress,
    close_desktop_inhibitor,
    safe_desktop_inhibit,
    start_standard_library_download_worker,
)
from caveviewer.gui.standard_library_maps import (
    DownloadCancelled,
    bootstrap_managed_standard_library_map_installs,
    existing_standard_library_map_path,
    is_app_supplied_standard_library_map_path,
    is_standard_library_map_downloaded,
    managed_standard_library_map_installs,
    remove_downloaded_standard_library_map,
    set_managed_standard_library_map_former,
)


FeedbackCallback = Callable[..., None]

if TYPE_CHECKING:
    from caveviewer.gui.platform.runtime import PlatformRuntime


def _remaining_cache_error(
    cache_result: Any, removed_paths: Sequence[str]
) -> str | None:
    """Return cache-removal errors not made irrelevant by removing the map folder."""
    error = getattr(cache_result, "error", None)
    if not error:
        return None
    cache_dir = getattr(cache_result, "cache_dir", None)
    if not cache_dir:
        return str(error)

    cache_dir_abs = os.path.abspath(os.fspath(cache_dir))
    for removed_path in removed_paths:
        removed_abs = os.path.abspath(os.fspath(removed_path))
        try:
            if os.path.commonpath((removed_abs, cache_dir_abs)) == removed_abs:
                return None
        except ValueError:
            continue
    return str(error)


OpenMapCallback = Callable[[str], None]
OpenGuidedDiveCallback = Callable[[str], None]
ShowCaveMetadataCallback = Callable[[CaveMetadata], None]

_CACHE_REBUILD_NOTIFICATION_PREFIX = "caveviewer.cache-rebuild"


def _cache_rebuild_notification_id(map_path: str) -> str:
    """Return an opaque, stable desktop-notification ID for one map."""
    normalized_path = os.path.abspath(os.fspath(map_path))
    digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()[:16]
    return f"{_CACHE_REBUILD_NOTIFICATION_PREFIX}.{digest}"


def _start_catalog_thread(target: Callable[[], None]) -> None:
    """Start the background worker that fetches standard-library metadata."""
    threading.Thread(
        target=target,
        name="CaveViewer-map-library-catalog",
        daemon=True,
    ).start()


@dataclass(slots=True)
class _ActiveCacheRebuild:
    """Presentation context for the one splash-owned rebuild job."""

    path: str
    title: str
    row_widgets: MapLibraryRowWidgets | None
    library_map: Any | None = None
    base_metadata: str = ""
    operation: str = "rebuild"


@dataclass(frozen=True, slots=True)
class MapLibraryComposition:
    """Tk-thread objects and session callbacks owned by splash composition."""

    root: Any
    controller: MapLibraryController
    panel: MapLibraryPanel
    standard_library_maps: Sequence[Any]
    map_library_root_dir: str
    desktop_services: DesktopServices
    splash_exists: Callable[[], bool]
    show_feedback: FeedbackCallback
    logger: Any
    platform_runtime: PlatformRuntime | None = None
    map_library_root_dir_provider: Callable[[], str] | None = None


@dataclass(frozen=True, slots=True)
class MapLibraryStorageDependencies:
    """Filesystem and install-registry operations used by the workflow."""

    has_cache: Callable[[str], bool] = has_managed_map_cache
    remove_cache: Callable[[str], Any] = remove_managed_map_cache
    remove_recent_path: Callable[[str], None] = remove_recent_map_path
    is_downloaded: Callable[[str, Any], bool] = is_standard_library_map_downloaded
    existing_path: Callable[
        [str, Any], str | None
    ] = existing_standard_library_map_path
    remove_downloaded: Callable[
        [str, Any], Any
    ] = remove_downloaded_standard_library_map
    is_app_supplied_path: Callable[
        [str, str], bool
    ] = is_app_supplied_standard_library_map_path
    bootstrap_managed_installs: Callable[
        [str, list[Any]], list[Any]
    ] = bootstrap_managed_standard_library_map_installs
    managed_installs: Callable[[], list[Any]] = managed_standard_library_map_installs
    set_managed_install_former: Callable[..., None] = set_managed_standard_library_map_former


@dataclass(frozen=True, slots=True)
class MapLibraryCatalogDependencies:
    """Catalog worker creation and result transport."""

    fetch_catalog: Callable[[], tuple[MapCatalogRefresh, ...]] | None = None
    start_worker: Callable[[Callable[[], None]], None] = _start_catalog_thread
    queue_factory: Callable[[], Any] = lambda: queue.Queue(maxsize=1)


@dataclass(frozen=True, slots=True)
class MapLibraryDownloadDependencies:
    """One-download worker, cancellation, and desktop-inhibition operations."""

    start_worker: Callable[
        [DirectorySelection, Any, threading.Event, Any], threading.Thread
    ] = start_standard_library_download_worker
    cancelled_type: type[BaseException] = DownloadCancelled
    queue_factory: Callable[[], Any] = queue.Queue
    cancel_event_factory: Callable[[], threading.Event] = threading.Event
    directory_selection_factory: Callable[
        [str], DirectorySelection
    ] = DirectorySelection.from_path
    inhibit_desktop: Callable[..., Any] = safe_desktop_inhibit
    close_inhibitor: Callable[[Any], None] = close_desktop_inhibitor


@dataclass(frozen=True, slots=True)
class MapLibraryActionDependencies:
    """User-initiated map and Guided Dive actions."""

    open_map: OpenMapCallback
    guided_dive_menu: Callable[[str], FeatureDecision] = guided_dive_menu_decision
    guided_dive_preflight: Callable[
        [str, str], GuidedDivePlaybackPreflight
    ] = guided_dive_playback_preflight
    open_guided_dive: OpenGuidedDiveCallback | None = None
    cave_metadata_catalog: CaveMetadataCatalog | None = None
    show_cave_metadata: ShowCaveMetadataCallback | None = None


@dataclass(frozen=True, slots=True)
class MapLibraryCacheRebuildDependencies:
    """Cache-rebuild process owner and splash presentation policy."""

    preflight: Callable[[str], CacheRebuildPreflight] = probe_map_library_cache_rebuild
    controller: CacheRebuildJobController | None = None
    splash_is_foreground: Callable[[], bool] | None = None
    notification_sender: Callable[..., bool] = send_desktop_notification

class MapLibraryWorkflow:
    """
    Coordinate the splash Map Library's non-presentation workflow.

    The panel owns Tk widgets and this workflow owns the allowed state
    transitions around catalog fetches, downloads, cancellation, row refresh,
    and file-removal actions. All public methods are called on the Tk thread;
    worker results cross queue boundaries and are applied from scheduled polls.
    """

    def __init__(
        self,
        *,
        composition: MapLibraryComposition,
        actions: MapLibraryActionDependencies,
        storage: MapLibraryStorageDependencies | None = None,
        catalog: MapLibraryCatalogDependencies | None = None,
        download: MapLibraryDownloadDependencies | None = None,
        cache_rebuild: MapLibraryCacheRebuildDependencies | None = None,
    ) -> None:
        storage = storage or MapLibraryStorageDependencies()
        catalog = catalog or MapLibraryCatalogDependencies()
        download = download or MapLibraryDownloadDependencies()
        cache_rebuild = cache_rebuild or MapLibraryCacheRebuildDependencies()
        self.root = composition.root
        self.controller = composition.controller
        self.panel = composition.panel
        self.standard_library_maps = tuple(composition.standard_library_maps)
        self.map_library_root_dir = composition.map_library_root_dir
        self.desktop_services = composition.desktop_services
        self.platform_runtime = composition.platform_runtime
        self.splash_exists = composition.splash_exists
        self.open_map = actions.open_map
        self.show_feedback = composition.show_feedback
        self.logger = composition.logger
        self.has_cache = storage.has_cache
        self.remove_cache = storage.remove_cache
        self.remove_recent_path = storage.remove_recent_path
        self.is_downloaded = storage.is_downloaded
        self.existing_path = storage.existing_path
        self.remove_downloaded = storage.remove_downloaded
        self.is_app_supplied_path = storage.is_app_supplied_path
        self.fetch_catalog = (
            catalog.fetch_catalog
            or default_map_library_catalog_service().fetch_catalogs
        )
        self.bootstrap_managed_installs = storage.bootstrap_managed_installs
        self.managed_installs = storage.managed_installs
        self.set_managed_install_former = storage.set_managed_install_former
        self.map_library_root_dir_provider = composition.map_library_root_dir_provider
        self.start_download_worker = download.start_worker
        self.start_catalog_worker = catalog.start_worker
        self.download_cancelled_type = download.cancelled_type
        self.download_queue_factory = download.queue_factory
        self.catalog_queue_factory = catalog.queue_factory
        self.cancel_event_factory = download.cancel_event_factory
        self.directory_selection_factory = download.directory_selection_factory
        self.inhibit_desktop = download.inhibit_desktop
        self.close_inhibitor = download.close_inhibitor
        self.guided_dive_menu = actions.guided_dive_menu
        self.guided_dive_preflight = actions.guided_dive_preflight
        self.open_guided_dive = actions.open_guided_dive
        self.cache_rebuild_preflight = cache_rebuild.preflight
        self.cache_rebuild_controller = (
            cache_rebuild.controller or CacheRebuildJobController()
        )
        self.splash_is_foreground = cache_rebuild.splash_is_foreground or (
            lambda: True
        )
        self.notification_sender = cache_rebuild.notification_sender
        self.cave_metadata_catalog = actions.cave_metadata_catalog
        self.show_cave_metadata = actions.show_cave_metadata
        self.catalog_workflow = MapLibraryCatalogWorkflow(
            controller=self.controller,
            scheduler=self.root,
            splash_exists=self.splash_exists,
            fetch_catalog=self.fetch_catalog,
            start_worker=self.start_catalog_worker,
            queue_factory=self.catalog_queue_factory,
            on_complete=self._complete_catalog_fetch,
        )
        self.download_workflow = MapLibraryDownloadWorkflow(
            controller=self.controller,
            scheduler=self.root,
            splash_exists=self.splash_exists,
            start_worker=self.start_download_worker,
            queue_factory=self.download_queue_factory,
            cancel_event_factory=self.cancel_event_factory,
            selection_factory=self.directory_selection_factory,
            inhibit=self.inhibit_desktop,
            close_inhibitor=self.close_inhibitor,
            desktop_services=self.desktop_services,
            on_progress=self.apply_download_progress,
            on_success=self.finish_download_success,
            on_failure=self.finish_download_failure,
        )
        self._active_cache_rebuild: _ActiveCacheRebuild | None = None
        self.cache_rebuild_workflow = MapLibraryCacheRebuildWorkflow(
            controller=self.cache_rebuild_controller,
            scheduler=self.root,
            splash_exists=self.splash_exists,
            apply_updates=self._apply_cache_rebuild_updates,
        )
        self.recent_map_paths: list[str] = []

    def populate_panel(self, parent, recent_map_paths: Sequence[str]) -> None:
        """Create Map Library rows and start the initial catalog refresh."""
        self.recent_map_paths = list(recent_map_paths)
        self._bootstrap_managed_installs()
        self._restore_persisted_former_maps()
        self.recent_map_paths = [
            path
            for path in self.recent_map_paths
            if not self.is_app_supplied_path(path, self.map_library_root_dir)
        ]
        self.panel.create(parent)
        if self.recent_map_paths:
            for recent_path in self.recent_map_paths:
                self.add_recent_row(recent_path)
        else:
            self.panel.ensure_recent_empty_note()

        for library_map in self.standard_library_maps:
            self.add_standard_row(library_map)

        self.panel.finish_population()
        self.start_catalog_fetch()

    def _bootstrap_managed_installs(self) -> None:
        """Adopt known legacy library folders before remote reconciliation."""
        try:
            self.bootstrap_managed_installs(
                self.map_library_root_dir,
                list(self.standard_library_maps),
            )
        except Exception as exc:
            self.logger.warning("Could not bootstrap map library installs: %s", exc)

    def _registered_local_library_installs(self) -> tuple[Any, ...]:
        """Return prior app-managed entries retained for former-map handling."""
        try:
            installs = self.managed_installs()
        except Exception as exc:
            self.logger.warning("Could not load map library installs: %s", exc)
            return ()

        return tuple(installs)

    def _map_from_managed_install(self, install):
        """Restore row metadata from one private app-managed install record."""
        try:
            return install.as_map_info()
        except AttributeError:
            return install

    def _registered_local_library_maps(self) -> tuple[Any, ...]:
        """Return row metadata restored from known local app-managed installs."""
        installs = self._registered_local_library_installs()

        maps: list[Any] = []
        for install in installs:
            try:
                library_map = self._map_from_managed_install(install)
            except Exception as exc:
                self.logger.warning("Could not restore a managed map install: %s", exc)
                continue
            maps.append(library_map)
        return tuple(maps)

    def _restore_persisted_former_maps(self) -> None:
        """Keep a previously confirmed former map visible while offline.

        A stale catalog cache may no longer mention a map that was authoritatively
        removed during an earlier run.  The managed-install registry retains
        that known former state so an offline restart still distinguishes the
        local copy from maps that remain in the standard library.
        """
        maps = list(self.standard_library_maps)
        availability_by_key = {
            self.controller.map_key(library_map): self.controller.availability_for(
                library_map
            )
            for library_map in maps
        }
        changed = False
        known_keys = set(availability_by_key)
        for install in self._registered_local_library_installs():
            if not getattr(install, "former", False):
                continue
            try:
                library_map = self._map_from_managed_install(install)
            except Exception as exc:
                self.logger.warning("Could not restore a former map install: %s", exc)
                continue
            key = self.controller.map_key(library_map)
            availability_by_key[key] = StandardLibraryMapAvailability.FORMER_STANDARD_LOCAL
            if key not in known_keys:
                maps.append(library_map)
                known_keys.add(key)
            changed = True
        if not changed:
            return
        self.standard_library_maps = tuple(maps)
        self.controller.replace_standard_library_maps(
            self.standard_library_maps,
            availability_by_key=availability_by_key,
        )

    def _set_managed_install_former(self, library_map, *, former: bool) -> None:
        """Persist a source-confirmed former/current transition best-effort."""
        try:
            self.set_managed_install_former(library_map, former=former)
        except Exception as exc:
            self.logger.warning(
                "Could not update managed map availability for %s: %s",
                getattr(library_map, "display_name", "map"),
                exc,
            )

    def close(self) -> None:
        """Close transient UI and request a resumable active rebuild pause."""
        self.panel.close_active_menu()
        self.download_workflow.close()
        self.catalog_workflow.close()
        self.request_cache_rebuild_pause(for_close=True)

    def set_map_library_root_dir(self, map_library_root_dir: str) -> None:
        """Update the storage root used by standard-library rows."""
        if map_library_root_dir == self.map_library_root_dir:
            return
        if self.controller.active_download.in_progress:
            self._show_info(
                "The map library folder change will be used after the current "
                "download finishes.",
                duration_ms=7000,
                max_wraplength=360,
            )
            return
        self.map_library_root_dir = map_library_root_dir
        for library_map in self.standard_library_maps:
            self.refresh_standard_row(library_map)

    def sync_map_library_root_dir(self) -> None:
        """Refresh the storage root from Preferences before map actions."""
        if (
            self.map_library_root_dir_provider is None
            or self.controller.active_download.in_progress
        ):
            return
        self.set_map_library_root_dir(self.map_library_root_dir_provider())

    def add_recent_row(self, path: str) -> None:
        """Append one recent-map row and wire its management actions."""
        entry = recent_map_entry(path)
        metadata_match = self.cave_metadata_match(entry.title)
        if metadata_match is not None:
            entry = replace(entry, detail=metadata_match.cave.library_detail)
        title = entry.title

        def menu_actions(
            row_widgets,
            path=path,
            title=title,
            metadata_match=metadata_match,
        ):
            actions = []
            metadata_action = None
            if metadata_match is not None and self.show_cave_metadata is not None:
                metadata_action = (
                    "About cave",
                    lambda cave=metadata_match.cave: self.show_cave_metadata(cave),
                )
            if self._guided_dive_action_available(path):
                actions.append(
                    (
                        "Open dive plan…",
                        lambda path=path: self.open_guided_dive_for_map(path),
                    )
                )
            actions.append(
                (
                    "Remove from this list",
                    lambda path=path: self.remove_recent_map(path),
                )
            )
            rebuild_action = self.cache_rebuild_menu_action(
                path,
                title,
                row_widgets,
            )
            if rebuild_action is not None:
                actions.append(rebuild_action)
            if self.has_cache(path):
                actions.append(
                    self.cache_removal_menu_action(
                        path,
                        title,
                        row_widgets,
                    )
                )
            if metadata_action is not None:
                actions.append(metadata_action)
            return tuple(actions)

        self.panel.add_recent_row(
            entry,
            action=lambda path=path: self.open_recent_map(path),
            menu_actions_factory=menu_actions,
        )

    def add_standard_row(self, library_map) -> None:
        """Append one standard-library row and wire its workflow actions."""
        downloaded = self.is_downloaded(self.map_library_root_dir, library_map)
        result_path = self.existing_path(self.map_library_root_dir, library_map)
        row = self.standard_library_row(
            library_map,
            downloaded=downloaded,
            result_path=result_path if downloaded else None,
        )

        def menu_actions(row_widgets, library_map=library_map):
            actions = []
            metadata_match = self.cave_metadata_match_for_library_map(
                self.controller.resolve_catalog_entry(library_map)
            )
            metadata_action = None
            if metadata_match is not None and self.show_cave_metadata is not None:
                metadata_action = (
                    "About cave",
                    lambda cave=metadata_match.cave: self.show_cave_metadata(cave),
                )
            map_path = self.downloaded_library_map_path(library_map)
            if map_path is None:
                if metadata_action is not None:
                    actions.append(metadata_action)
                return tuple(actions)
            if self._guided_dive_action_available(map_path):
                actions.append(
                    (
                        "Open dive plan…",
                        lambda map_path=map_path: self.open_guided_dive_for_map(
                            map_path
                        ),
                    )
                )
            actions.append(
                (
                    "Remove map files",
                    lambda map_path=map_path, library_map=library_map: (
                        self.remove_standard_download(
                            library_map,
                            map_path,
                            row_widgets,
                        )
                    ),
                ),
            )
            rebuild_action = self.cache_rebuild_menu_action(
                map_path,
                library_map.display_name,
                row_widgets,
                library_map=library_map,
            )
            if rebuild_action is not None:
                actions.append(rebuild_action)
            if self.has_cache(map_path):
                actions.append(
                    self.cache_removal_menu_action(
                        map_path,
                        library_map.display_name,
                        row_widgets,
                    )
                )
            if metadata_action is not None:
                actions.append(metadata_action)
            return tuple(actions)

        self.panel.add_standard_row(
            row,
            action=lambda library_map=library_map: self.on_map_action(library_map),
            former=self.controller.is_former_standard_map(library_map),
            menu_actions_factory=menu_actions,
        )

    def cache_rebuild_menu_action(
        self,
        path: str,
        title: str,
        row_widgets: MapLibraryRowWidgets | None,
        *,
        library_map: Any | None = None,
    ):
        """Build the current rebuild menu action for one map-library row."""
        try:
            preflight = self.cache_rebuild_preflight(path)
        except Exception as exc:
            self.logger.warning("Cache rebuild preflight failed for %s: %s", title, exc)
            return MapLibraryMenuAction(
                "Rebuild cache",
                explanation="Cache rebuild availability could not be determined.",
            )

        decision = preflight.decision
        if not decision.is_visible:
            return None
        resume_available = preflight.resumable_import is not None
        label = self._cache_rebuild_action_label(preflight)
        if self.cache_rebuild_controller.active:
            return MapLibraryMenuAction(
                label,
                explanation="Another cache rebuild is already in progress.",
            )
        if not decision.allows_execution:
            return MapLibraryMenuAction(
                label,
                explanation=decision.explanation,
            )
        if resume_available:
            return (
                label,
                lambda: self.resume_cache_rebuild(
                    path,
                    title,
                    row_widgets,
                    library_map=library_map,
                ),
            )
        return (
            label,
            lambda: self.start_cache_rebuild(
                path,
                title,
                row_widgets,
                library_map=library_map,
            ),
        )

    @staticmethod
    def _cache_rebuild_action_label(preflight: CacheRebuildPreflight) -> str:
        """Return the row-menu label for the current cache action."""
        if preflight.resumable_import is not None:
            return "Resume cache rebuild"
        target = preflight.capability.value
        if target is not None and target.operation == "build":
            return "Build cache"
        return "Rebuild cache"

    def cache_removal_menu_action(
        self,
        path: str,
        title: str,
        row_widgets: MapLibraryRowWidgets | None,
    ):
        """Build the current generated-cache removal action for one row."""
        if self.cache_rebuild_controller.active:
            return MapLibraryMenuAction(
                "Remove cache",
                explanation="Cache actions are unavailable while a rebuild is running.",
            )
        return (
            "Remove cache",
            lambda: self.remove_map_cache(path, title, row_widgets),
        )

    def start_cache_rebuild(
        self,
        path: str,
        title: str,
        row_widgets: MapLibraryRowWidgets | None,
        *,
        library_map: Any | None = None,
    ) -> None:
        """Start an ordinary rebuild, preserving its existing resume behavior."""
        self._start_cache_rebuild(
            path,
            title,
            row_widgets,
            library_map=library_map,
            resume_required=False,
        )

    def resume_cache_rebuild(
        self,
        path: str,
        title: str,
        row_widgets: MapLibraryRowWidgets | None,
        *,
        library_map: Any | None = None,
    ) -> None:
        """Resume only a checkpoint that remains valid at click time."""
        self._start_cache_rebuild(
            path,
            title,
            row_widgets,
            library_map=library_map,
            resume_required=True,
        )

    def _start_cache_rebuild(
        self,
        path: str,
        title: str,
        row_widgets: MapLibraryRowWidgets | None,
        *,
        library_map: Any | None = None,
        resume_required: bool,
    ) -> None:
        """Start one forced rebuild without opening a viewer."""
        if self.cache_rebuild_controller.active:
            self._show_info(
                "Finish or pause the current cache rebuild before starting another.",
                duration_ms=7000,
                max_wraplength=360,
            )
            return
        if self.controller.active_download.in_progress:
            self._show_info(
                "Finish or stop the current map library download before "
                "rebuilding a cache.",
                duration_ms=7000,
                max_wraplength=380,
            )
            return

        # Menu eligibility can be stale by the time the action is invoked.
        preflight = self._fresh_cache_rebuild_preflight(path, title)
        if preflight is None:
            return
        target = preflight.capability.value
        if target is None:
            return
        if resume_required and preflight.resumable_import is None:
            self._show_error(
                "Saved rebuild checkpoint is no longer usable. "
                "Open the menu again and choose Rebuild cache.",
                max_wraplength=420,
            )
            return

        active = _ActiveCacheRebuild(
            path=path,
            title=title,
            row_widgets=row_widgets,
            library_map=library_map,
            base_metadata=(
                "" if library_map is not None else recent_map_entry(path).detail
            ),
            operation=(
                "build"
                if target.operation == "build" and not resume_required
                else "rebuild"
            ),
        )
        self._active_cache_rebuild = active
        if resume_required:
            started = self.cache_rebuild_controller.start(
                target,
                resume_required=True,
            )
        else:
            started = self.cache_rebuild_controller.start(target)
        if isinstance(started, CacheRebuildFailed):
            self._handle_cache_rebuild_failure(started)
            return

        assert isinstance(started, CacheRebuildStarted)
        self._show_active_cache_rebuild(active)
        self.cache_rebuild_workflow.schedule_poll()

    def _fresh_cache_rebuild_preflight(
        self,
        path: str,
        title: str,
    ) -> CacheRebuildPreflight | None:
        """Re-evaluate one map-local rebuild decision at the action boundary."""
        try:
            preflight = self.cache_rebuild_preflight(path)
        except Exception as exc:
            self.logger.warning("Cache rebuild preflight failed for %s: %s", title, exc)
            self._show_error("Cache rebuild availability could not be determined.")
            return None
        if (
            not preflight.decision.allows_execution
            or preflight.capability.value is None
        ):
            self._show_error(preflight.decision.explanation)
            return None
        return preflight

    @staticmethod
    def _cache_rebuild_operation_word(active: _ActiveCacheRebuild) -> str:
        return "Building" if active.operation == "build" else "Rebuilding"

    @staticmethod
    def _cache_rebuild_result_word(active: _ActiveCacheRebuild) -> str:
        return "built" if active.operation == "build" else "rebuilt"

    def _show_active_cache_rebuild(self, active: _ActiveCacheRebuild) -> None:
        """Replace the active row's normal action with progress and pause."""
        can_pause = self.cache_rebuild_controller.pause_supported
        self.panel.set_row_action(
            active.row_widgets,
            "Pause",
            self.request_cache_rebuild_pause,
            enabled=can_pause,
            show_pause_progress=True,
        )
        self.panel.set_row_metadata(
            active.row_widgets,
            f"{self._cache_rebuild_operation_word(active)} cache — Starting import",
        )
        self.panel.set_row_progress(active.row_widgets, 0.0)
        self.panel.refresh_row_overflow(active.row_widgets)

    def request_cache_rebuild_pause(self, *, for_close: bool = False) -> bool:
        """Cooperatively checkpoint the active OBJ rebuild without deleting work."""
        active = self._active_cache_rebuild
        if active is None:
            return False
        if not self.cache_rebuild_workflow.request_pause(for_close=for_close):
            return False
        self.panel.set_row_action(
            active.row_widgets,
            "Pause",
            lambda: None,
            enabled=False,
            show_pause_progress=True,
        )
        self.panel.set_row_metadata(active.row_widgets, "Pausing cache rebuild…")
        self.panel.refresh_row_overflow(active.row_widgets)
        return True

    def _apply_cache_rebuild_updates(self, updates: tuple[Any, ...]) -> None:
        """Render typed updates delivered by the rebuild lifecycle owner."""
        for update in updates:
            if isinstance(update, CacheRebuildProgress):
                self._apply_cache_rebuild_progress(update)
            elif isinstance(update, CacheRebuildSucceeded):
                self._handle_cache_rebuild_success(update)
            elif isinstance(update, CacheRebuildPaused):
                self._handle_cache_rebuild_paused(update)
            elif isinstance(update, CacheRebuildFailed):
                self._handle_cache_rebuild_failure(update)

    def _apply_cache_rebuild_progress(self, update: CacheRebuildProgress) -> None:
        active = self._active_cache_rebuild
        if active is None:
            return
        stage = " ".join(update.stage.strip().split()) or "Rebuilding cache"
        label = stage[:1].upper() + stage[1:]
        if update.pausing:
            label = (
                "Pausing cache build"
                if active.operation == "build"
                else "Pausing cache rebuild"
            )
        self.panel.set_row_metadata(
            active.row_widgets,
            f"{self._cache_rebuild_operation_word(active)} cache — {label}",
        )
        self.panel.set_row_progress(active.row_widgets, update.fraction)

    def _restore_cache_rebuild_row(self, active: _ActiveCacheRebuild) -> None:
        """Restore the row's normal Open action after a terminal rebuild state."""
        if active.library_map is not None:
            self.refresh_standard_row(active.library_map)
            return
        self.panel.set_row_action(
            active.row_widgets,
            "Open",
            lambda path=active.path: self.open_recent_map(path),
        )
        self.panel.set_row_metadata(active.row_widgets, active.base_metadata)

    def _finish_cache_rebuild(self) -> _ActiveCacheRebuild | None:
        active = self._active_cache_rebuild
        self._active_cache_rebuild = None
        return active

    def _notify_cache_rebuild(
        self,
        active: _ActiveCacheRebuild,
        title: str,
        body: str,
        *,
        priority: str = "normal",
    ) -> None:
        """Best-effort terminal notification when inline feedback is backgrounded."""
        try:
            if self.splash_is_foreground():
                return
        except Exception as exc:
            self.logger.warning(
                "Could not determine splash foreground state for cache rebuild: %s",
                exc,
            )
            return
        try:
            self.notification_sender(
                self.desktop_services,
                _cache_rebuild_notification_id(active.path),
                title,
                body,
                priority=priority,
                platform_runtime=self.platform_runtime,
            )
        except Exception as exc:
            self.logger.warning(
                "Could not send cache rebuild desktop notification: %s",
                exc,
            )

    def _handle_cache_rebuild_success(self, update: CacheRebuildSucceeded) -> None:
        active = self._finish_cache_rebuild()
        if active is None:
            return
        self._restore_cache_rebuild_row(active)
        self.panel.show_row_status(
            active.row_widgets,
            f"Cache {self._cache_rebuild_result_word(active)}",
        )
        self.panel.refresh_row_overflow(active.row_widgets)
        self._notify_cache_rebuild(
            active,
            "Cache Build Complete"
            if active.operation == "build"
            else "Cache Rebuild Complete",
            (
                f"{active.title} cache built."
                if active.operation == "build"
                else f"{active.title} cache rebuilt."
            ),
        )

    def _handle_cache_rebuild_paused(self, update: CacheRebuildPaused) -> None:
        active = self._finish_cache_rebuild()
        if active is None:
            return
        self._restore_cache_rebuild_row(active)
        self.panel.show_row_status(
            active.row_widgets,
            "Cache build paused"
            if active.operation == "build"
            else "Cache rebuild paused",
        )
        self.panel.refresh_row_overflow(active.row_widgets)

    def _handle_cache_rebuild_failure(self, update: CacheRebuildFailed) -> None:
        active = self._finish_cache_rebuild()
        if active is None:
            return
        self._restore_cache_rebuild_row(active)
        self.panel.show_row_status(
            active.row_widgets,
            "Cache not built" if active.operation == "build" else "Cache retained",
            error=True,
        )
        self.panel.refresh_row_overflow(active.row_widgets)
        operation = "build" if active.operation == "build" else "rebuild"
        self.logger.warning(
            "Cache %s failed for %s: %s",
            operation,
            active.title,
            update.error,
        )
        message = f"Couldn't {operation} cache for {active.title}."
        if active.operation != "build":
            message = f"{message} The existing cache was retained."
        if update.suggestion:
            message = f"{message} {update.suggestion}"
        self._show_error(message, max_wraplength=420)
        self._notify_cache_rebuild(
            active,
            "Cache Build Failed"
            if active.operation == "build"
            else "Cache Rebuild Failed",
            (
                f"Couldn't build {active.title}."
                if active.operation == "build"
                else f"Couldn't rebuild {active.title}; its existing cache was retained."
            ),
            priority="high",
        )

    def _cache_rebuild_blocks_map_actions(self) -> bool:
        """Keep splash-owned rebuild lifecycle intact until its child settles."""
        if not self.cache_rebuild_controller.active:
            return False
        self._show_info(
            "Wait for the cache rebuild to finish or pause before opening another map.",
            duration_ms=7000,
            max_wraplength=380,
        )
        return True

    def open_recent_map(self, path: str) -> None:
        """Open a recent row unless the splash currently owns a rebuild child."""
        if self._cache_rebuild_blocks_map_actions():
            return
        if self.is_app_supplied_path(path, self.map_library_root_dir):
            self._show_info(
                "This map is still managed by Map Library. Open it from the "
                "CaveViewer Maps section instead.",
                max_wraplength=400,
            )
            return
        self.open_map(path)

    def _guided_dive_action_available(self, map_path: str) -> bool:
        """Return whether this map currently exposes an executable dive action."""
        return bool(
            self.open_guided_dive is not None
            and self.guided_dive_menu(map_path).allows_execution
        )

    def open_guided_dive_for_map(self, map_path: str) -> None:
        """Choose, preflight, then launch one map-local Guided Dive.

        The menu may have been open while the trace or cache changed. Recheck
        availability before the desktop picker, then validate the actual
        selected JSONL and its current cache before the splash session leaves.
        """
        if (
            self.open_guided_dive is None
            or self._cache_rebuild_blocks_map_actions()
        ):
            return

        availability = self.guided_dive_menu(map_path)
        if not availability.allows_execution:
            if availability.is_visible:
                self._show_error(availability.explanation)
            else:
                self._show_info(availability.explanation)
            return

        try:
            picker_preflight = file_selection_preflight(
                self.desktop_services,
                platform_runtime=self.platform_runtime,
            )
            if not picker_preflight.decision.allows_execution:
                self.logger.warning(
                    "Guided Dive file selection unavailable: %s",
                    picker_preflight.decision.reason_code,
                )
                self._show_error(picker_preflight.decision.explanation)
                return
            selection = choose_authorized_file(
                picker_preflight,
                self.desktop_services,
                title="Open Dive Plan",
                initial_dir=os.fspath(guided_dive_trace_directory(map_path)),
                parent=self.root,
            )
        except DesktopServiceError as exc:
            self.logger.warning("Guided Dive file selection failed: %s", exc)
            self._show_error("Couldn't open the dive plan picker.")
            return
        except Exception as exc:
            self.logger.warning("Guided Dive file selection failed: %s", exc)
            self._show_error("Couldn't open the dive plan picker.")
            return
        if selection is None:
            return

        try:
            preflight = self.guided_dive_preflight(map_path, selection.path)
        except Exception as exc:
            self.logger.warning("Guided Dive preflight failed: %s", exc)
            self._show_error("Dive plan availability could not be determined.")
            return

        target = preflight.capability.value
        if not preflight.decision.allows_execution or target is None:
            self.logger.warning(
                "Guided Dive preflight rejected %s: %s",
                selection.path,
                preflight.decision.reason_code,
            )
            self._show_error(preflight.decision.explanation)
            return

        self.open_guided_dive(os.fspath(target.trace.path))

    def remove_map_cache(
        self,
        path: str,
        title: str,
        row_widgets: MapLibraryRowWidgets | None,
    ) -> None:
        """Remove generated cache data for a map-library row."""
        if self.cache_rebuild_controller.active:
            self._show_info(
                "Cache actions are unavailable while a rebuild is running.",
                duration_ms=7000,
                max_wraplength=360,
            )
            return
        result = self.remove_cache(path)
        if result.error:
            self.logger.warning("Unable to remove cache for %s: %s", title, result.error)
            if not self.panel.show_row_status(
                row_widgets,
                "Couldn’t remove cache",
                error=True,
            ):
                self._show_error(
                    f"Unable to remove cache for {title}: {result.error}"
                )
        elif result.removed:
            self.panel.show_row_status(row_widgets, "Cache removed")
        else:
            self.panel.show_row_status(row_widgets, "No cache found")

        self.panel.refresh_row_overflow(row_widgets)

    def remove_standard_download(
        self,
        library_map,
        map_path: str,
        row_widgets: MapLibraryRowWidgets | None,
    ) -> None:
        """Remove a downloaded standard-library map and its generated cache."""
        if self.cache_rebuild_controller.active:
            self._show_info(
                "Map files cannot be removed while a cache rebuild is running.",
                duration_ms=7000,
                max_wraplength=360,
            )
            return
        cache_result = self.remove_cache(map_path)
        removal_result = self.remove_downloaded(
            self.map_library_root_dir,
            library_map,
        )
        if (
            not removal_result.error
            and self.controller.is_former_standard_map(library_map)
            and not self.is_downloaded(self.map_library_root_dir, library_map)
        ):
            self._remove_former_standard_map(library_map)
        else:
            self.refresh_standard_row(library_map)

        cache_error = _remaining_cache_error(cache_result, removal_result.removed_paths)
        removal_error = removal_result.error
        if cache_error or removal_error:
            error = "; ".join(
                item for item in (cache_error, removal_error) if item
            )
            self.logger.warning(
                "Unable to remove downloaded maps for %s: %s",
                library_map.display_name,
                error,
            )
            if not self.panel.show_row_status(
                row_widgets,
                "Couldn’t remove files",
                error=True,
            ):
                self._show_error(
                    "Unable to remove downloaded maps for "
                    f"{library_map.display_name}: {error}"
                )
            self.panel.refresh_row_overflow(row_widgets)
            return

        if removal_result.removed_paths or getattr(cache_result, "removed", False):
            self.panel.show_row_status(row_widgets, "Removed")
            return

        self.panel.show_row_status(row_widgets, "No files found")

    def _remove_former_standard_map(self, library_map) -> None:
        """Remove a former-library row after its managed files no longer exist."""
        key = self.controller.map_key(library_map)
        self.controller.remove_standard_library_map(library_map)
        self.standard_library_maps = tuple(
            candidate
            for candidate in self.standard_library_maps
            if self.controller.map_key(candidate) != key
        )
        if self.splash_exists():
            self.panel.remove_standard_row(key)

    def remove_recent_map(self, path: str) -> None:
        """Forget one recent-map path and remove the visible row."""
        self.remove_recent_path(path)
        normalized = recent_map_key(path)
        self.recent_map_paths = [
            recent_path
            for recent_path in self.recent_map_paths
            if recent_map_key(recent_path) != normalized
        ]
        self.panel.remove_recent_row(normalized)

    def refresh_standard_row(self, library_map) -> None:
        """Refresh one standard-library row from catalog and local disk state."""
        downloaded = self.is_downloaded(self.map_library_root_dir, library_map)
        result_path = self.existing_path(self.map_library_root_dir, library_map)
        self.controller.set_downloaded_path(
            library_map,
            downloaded=downloaded,
            result_path=result_path,
        )
        row = self.standard_library_row(
            library_map,
            downloaded=downloaded,
            result_path=result_path if downloaded else None,
        )
        self.panel.set_standard_row_former(
            row.key,
            row.availability is StandardLibraryMapAvailability.FORMER_STANDARD_LOCAL,
        )
        self.set_row_metadata(library_map, row.detail)
        self.set_standard_action(library_map, row)

    def cave_metadata_match(self, title: str) -> CaveMetadataMatch | None:
        """Return one safe metadata association for a visible map title."""
        catalog = self.cave_metadata_catalog
        if catalog is None:
            return None
        try:
            return catalog.match(title)
        except Exception as exc:
            self.logger.warning("Could not match cave metadata for %s: %s", title, exc)
            return None

    def cave_metadata_match_for_library_map(
        self,
        library_map,
    ) -> CaveMetadataMatch | None:
        """Prefer a source-supplied stable cave id over name matching."""
        catalog = self.cave_metadata_catalog
        if catalog is None:
            return None
        title = getattr(library_map, "display_name", "")
        try:
            return catalog.match(
                title,
                cave_metadata_id=getattr(library_map, "cave_metadata_id", None),
            )
        except Exception as exc:
            self.logger.warning("Could not match cave metadata for %s: %s", title, exc)
            return None

    def standard_library_row(
        self,
        library_map,
        *,
        downloaded: bool,
        enabled: bool = True,
        action_text: str | None = None,
        result_path: str | None = None,
    ) -> StandardLibraryMapRow:
        """Build a standard row while restoring its stable cave subtitle."""
        resolved_map = self.controller.resolve_catalog_entry(library_map)
        metadata_match = self.cave_metadata_match_for_library_map(resolved_map)
        return self.controller.row(
            resolved_map,
            downloaded=downloaded,
            enabled=enabled,
            action_text=action_text,
            result_path=result_path,
            cave_metadata_detail=(
                metadata_match.cave.library_detail
                if metadata_match is not None
                else None
            ),
        )

    def downloaded_library_map_path(self, library_map) -> str | None:
        """Return the known downloaded path for a standard-library map."""
        return self.controller.downloaded_path(
            library_map,
            is_downloaded=self.is_downloaded(
                self.map_library_root_dir,
                library_map,
            ),
            existing_path=self.existing_path(
                self.map_library_root_dir,
                library_map,
            ),
        )

    def open_standard_map(self, library_map) -> None:
        """Open the selected standard-library map when it is available locally."""
        if self._cache_rebuild_blocks_map_actions():
            return
        self.sync_map_library_root_dir()
        map_path = (
            self.downloaded_library_map_path(library_map)
            or self.existing_path(self.map_library_root_dir, library_map)
        )
        if map_path is None:
            self._show_error(
                "The downloaded map could not be found. Try downloading it again.",
                max_wraplength=360,
            )
            self.refresh_standard_row(library_map)
            return
        self.open_map(map_path)

    def set_row_metadata(
        self,
        library_map,
        text: str,
        *,
        error: bool = False,
    ) -> None:
        """Set the stable metadata text for one standard-library row."""
        self.panel.set_standard_row_metadata(
            self.controller.map_key(library_map),
            text,
            error=error,
        )

    def show_standard_row_status(
        self,
        library_map,
        text: str,
        *,
        error: bool = False,
    ) -> None:
        """Show short-lived operational feedback over a stable row subtitle."""
        self.panel.show_standard_row_status(
            self.controller.map_key(library_map),
            text,
            error=error,
        )

    def set_standard_action(self, library_map, row) -> None:
        """Apply a row model's primary action to the standard-library panel."""
        if row.downloaded:
            if not self.panel.set_standard_row_action(
                row.key,
                row.action_text,
                lambda library_map=library_map: self.open_standard_map(
                    library_map
                ),
                enabled=row.enabled,
            ):
                return
            self.set_row_metadata(library_map, row.detail)
            self.panel.refresh_standard_row_overflow(row.key)
            return
        if not self.panel.set_standard_row_action(
            row.key,
            row.action_text,
            lambda library_map=library_map: self.on_map_action(library_map),
            enabled=row.enabled,
        ):
            return
        self.panel.refresh_standard_row_overflow(row.key)

    def set_non_active_actions_enabled(self, active_map, enabled: bool) -> None:
        """Enable or disable standard-library actions except the active row."""
        active_key = self.controller.map_key(active_map)
        for library_map in self.standard_library_maps:
            if self.controller.map_key(library_map) == active_key:
                continue
            result_path = self.downloaded_library_map_path(library_map)
            downloaded = bool(result_path) or self.is_downloaded(
                self.map_library_root_dir,
                library_map,
            )
            row = self.standard_library_row(
                library_map,
                downloaded=downloaded,
                enabled=enabled,
                result_path=result_path if downloaded else None,
            )
            self.set_standard_action(library_map, row)

    def clear_active_download(self, library_map) -> None:
        """Clear active download state and restore disabled row actions."""
        inhibitor = self.controller.clear_active_download()
        self.close_inhibitor(inhibitor)
        if self.splash_exists():
            self.set_non_active_actions_enabled(library_map, True)

    def finish_download_success(self, library_map, result_path: str) -> None:
        """Apply a successful standard-library download result."""
        if not self.splash_exists():
            self.clear_active_download(library_map)
            return
        self.reset_progress(library_map)
        row = self.standard_library_row(
            library_map,
            downloaded=True,
            result_path=result_path,
        )
        self.set_standard_action(library_map, row)
        self.clear_active_download(library_map)

    def finish_download_failure(self, library_map, error: Exception) -> None:
        """Apply a failed or canceled standard-library download result."""
        if not self.splash_exists():
            self.clear_active_download(library_map)
            return
        self.reset_progress(library_map)
        if isinstance(error, self.download_cancelled_type):
            row = self.standard_library_row(library_map, downloaded=False)
            self.set_row_metadata(library_map, row.detail)
            self.set_standard_action(library_map, row)
            self.clear_active_download(library_map)
            return
        row = self.standard_library_row(
            library_map,
            downloaded=False,
            action_text="Retry",
        )
        self.set_row_metadata(library_map, row.detail)
        self.set_standard_action(library_map, row)
        self.show_standard_row_status(library_map, "Download failed", error=True)
        self.clear_active_download(library_map)
        self._show_error(
            f"Couldn't download {library_map.display_name}. "
            "Check your connection and retry.",
            max_wraplength=360,
        )

    def start_inline_download(self, library_map) -> None:
        """Start a standard-library download from an already resolved row."""
        if self._cache_rebuild_blocks_map_actions():
            return
        self.sync_map_library_root_dir()
        if self.controller.is_former_standard_map(library_map):
            self.open_standard_map(library_map)
            return
        if self.controller.active_download.in_progress:
            self._show_info(
                "Finish or stop the current map library download before "
                "starting another.",
                duration_ms=7000,
                max_wraplength=360,
            )
            return
        if getattr(library_map, "download_url", None) is None:
            self.prepare_catalog_for_download(library_map)
            return

        row_key = self.controller.map_key(library_map)
        if not self.panel.has_standard_row(row_key):
            return

        self.show_progress(library_map)
        self.set_row_metadata(library_map, "Downloading…")
        cancel_event = self.cancel_event_factory()

        def request_cancel() -> None:
            cancel_event.set()
            self.panel.set_standard_row_action(
                row_key,
                "",
                lambda: None,
                enabled=False,
                show_stop_progress=True,
            )
            self.set_row_metadata(library_map, "Stopping…")

        self.panel.set_standard_row_action(
            row_key,
            "",
            request_cancel,
            show_stop_progress=True,
        )
        self.set_non_active_actions_enabled(library_map, False)
        try:
            owned_cancel_event = self.download_workflow.start(
                library_map,
                self.map_library_root_dir,
                parent=self.root,
                cancel_event=cancel_event,
            )
        except RuntimeError as exc:
            self.reset_progress(library_map)
            row = self.standard_library_row(
                library_map,
                downloaded=False,
                action_text="Retry",
            )
            self.set_row_metadata(library_map, row.detail)
            self.set_standard_action(library_map, row)
            self.show_standard_row_status(library_map, "Download failed", error=True)
            self.clear_active_download(library_map)
            self._show_error(
                f"Couldn't start the {library_map.display_name} download: {exc}",
                max_wraplength=360,
            )
            return
        # The cancel action is created before worker startup so the row responds
        # immediately. Both values are the same factory-created event.
        if owned_cancel_event is not cancel_event:
            cancel_event = owned_cancel_event

    def handle_download_info_unavailable(self, library_map) -> None:
        """Put a row into retry state when catalog details are unavailable."""
        row = self.standard_library_row(
            library_map,
            downloaded=False,
            action_text="Retry",
        )
        self.set_row_metadata(library_map, row.detail)
        self.set_standard_action(library_map, row)
        self.show_standard_row_status(
            library_map,
            "Download info unavailable",
            error=True,
        )
        self.set_non_active_actions_enabled(library_map, True)
        self._show_error(
            "Couldn't load download info. Check your connection and retry.",
            max_wraplength=360,
        )

    def _complete_catalog_fetch(self, completion) -> None:
        """Apply one catalog result delivered by the lifecycle owner."""
        self.reconcile_standard_catalog(completion.refreshes)

        pending_map = completion.pending_map
        if pending_map is None:
            return
        resolved_map = self.controller.resolve_catalog_entry(pending_map)
        if getattr(resolved_map, "download_url", None) is None:
            self.handle_download_info_unavailable(pending_map)
            return
        self.start_inline_download(resolved_map)

    def reconcile_standard_catalog(
        self,
        refreshes: Sequence[MapCatalogRefresh],
    ) -> None:
        """
        Reconcile visible standard-library rows after a catalog refresh.

        Each authoritative source owns its current available-map list. A local
        installation missing from that source becomes a muted former-map row;
        a fallback/error result never makes that change.
        """
        active_keys = {
            key
            for key in (
                self.controller.active_download.map_name,
            )
            if key is not None
        }
        previous_keys = tuple(
            self.controller.map_key(library_map)
            for library_map in self.standard_library_maps
        )
        current_by_key = {
            self.controller.map_key(library_map): library_map
            for library_map in self.standard_library_maps
        }
        current_availability = {
            key: self.controller.availability_for(library_map)
            for key, library_map in current_by_key.items()
        }
        registered_installs = self._registered_local_library_installs()
        registered_maps: list[tuple[Any, Any]] = []
        persisted_former_keys = set()
        for install in registered_installs:
            try:
                library_map = self._map_from_managed_install(install)
            except Exception as exc:
                self.logger.warning("Could not restore a managed map install: %s", exc)
                continue
            key = self.controller.map_key(library_map)
            registered_maps.append((install, library_map))
            if getattr(install, "former", False):
                persisted_former_keys.add(key)
        refresh_by_source = {refresh.source_id: refresh for refresh in refreshes}
        authoritative_sources = {
            refresh.source_id for refresh in refreshes if refresh.authoritative
        }
        authoritative_keys = {
            refresh.source_id: {
                self.controller.map_key(library_map) for library_map in refresh.maps
            }
            for refresh in refreshes
            if refresh.authoritative
        }
        visible_maps: list[Any] = []
        availability_by_key = {}
        visible_keys = set()

        def add_visible(
            library_map,
            availability: StandardLibraryMapAvailability,
        ) -> None:
            key = self.controller.map_key(library_map)
            if key in visible_keys:
                if availability is StandardLibraryMapAvailability.FORMER_STANDARD_LOCAL:
                    availability_by_key[key] = availability
                return
            visible_keys.add(key)
            visible_maps.append(library_map)
            availability_by_key[key] = availability

        for refresh in refreshes:
            source_id = refresh.source_id
            if refresh.authoritative:
                for library_map in refresh.maps:
                    self._set_managed_install_former(library_map, former=False)
                    add_visible(
                        library_map,
                        (
                            StandardLibraryMapAvailability.REMOTE_AVAILABLE
                            if getattr(library_map, "download_url", None)
                            else StandardLibraryMapAvailability.REMOTE_UNAVAILABLE
                        ),
                    )
                continue

            for library_map in refresh.maps:
                key = self.controller.map_key(library_map)
                add_visible(
                    library_map,
                    (
                        StandardLibraryMapAvailability.FORMER_STANDARD_LOCAL
                        if key in persisted_former_keys
                        else current_availability.get(
                            key,
                            StandardLibraryMapAvailability.REMOTE_AVAILABLE,
                        )
                    ),
                )
            for library_map in self.standard_library_maps:
                if self.controller.map_key(library_map)[0] != source_id:
                    continue
                add_visible(
                    library_map,
                    (
                        StandardLibraryMapAvailability.FORMER_STANDARD_LOCAL
                        if self.controller.map_key(library_map)
                        in persisted_former_keys
                        else current_availability[self.controller.map_key(library_map)]
                    ),
                )

        for library_map in self.standard_library_maps:
            key = self.controller.map_key(library_map)
            source_id = key[0]
            if source_id not in refresh_by_source:
                add_visible(
                    library_map,
                    (
                        StandardLibraryMapAvailability.FORMER_STANDARD_LOCAL
                        if key in persisted_former_keys
                        else current_availability[key]
                    ),
                )
                continue
            if source_id not in authoritative_sources:
                continue
            if key in authoritative_keys[source_id]:
                continue
            if key in active_keys:
                add_visible(library_map, current_availability[key])
                continue
            if self.is_downloaded(self.map_library_root_dir, library_map):
                self._set_managed_install_former(library_map, former=True)
                add_visible(
                    library_map,
                    StandardLibraryMapAvailability.FORMER_STANDARD_LOCAL,
                )
                continue
            self.panel.remove_standard_row(key)

        for install, library_map in registered_maps:
            key = self.controller.map_key(library_map)
            source_id = key[0]
            if source_id not in authoritative_sources:
                if getattr(install, "former", False):
                    add_visible(
                        library_map,
                        StandardLibraryMapAvailability.FORMER_STANDARD_LOCAL,
                    )
                continue
            if (
                key in authoritative_keys.get(source_id, set())
                or key in active_keys
                or not self.is_downloaded(self.map_library_root_dir, library_map)
            ):
                continue
            self._set_managed_install_former(library_map, former=True)
            add_visible(
                library_map,
                StandardLibraryMapAvailability.FORMER_STANDARD_LOCAL,
            )

        # Preserve the established position of retained entries.  In
        # particular, a locally installed map that has disappeared upstream
        # stays where users last saw it rather than jumping into a new section
        # or to the end of the list.  Truly new catalog entries follow the
        # existing order in their source-provided sequence.
        visible_by_key = {
            self.controller.map_key(library_map): library_map
            for library_map in visible_maps
        }
        ordered_visible_maps: list[Any] = []
        ordered_keys = set()
        for key in previous_keys:
            library_map = visible_by_key.get(key)
            if library_map is None:
                continue
            ordered_visible_maps.append(library_map)
            ordered_keys.add(key)
        for library_map in visible_maps:
            key = self.controller.map_key(library_map)
            if key in ordered_keys:
                continue
            ordered_visible_maps.append(library_map)
            ordered_keys.add(key)

        visible_maps_tuple = tuple(ordered_visible_maps)
        self.standard_library_maps = visible_maps_tuple
        self.controller.replace_standard_library_maps(
            visible_maps_tuple,
            availability_by_key=availability_by_key,
        )

        for library_map in visible_maps_tuple:
            if self.controller.map_key(library_map) in active_keys:
                continue
            if not self.panel.has_standard_row(self.controller.map_key(library_map)):
                self.add_standard_row(library_map)
                continue
            self.refresh_standard_row(library_map)

    def start_catalog_fetch(self, pending_map=None) -> None:
        """Start a background standard-library catalog refresh."""
        self.catalog_workflow.start(pending_map)

    def prepare_catalog_for_download(self, library_map) -> None:
        """Fetch catalog details before downloading an unresolved row."""
        if self._cache_rebuild_blocks_map_actions():
            return
        if self.controller.is_former_standard_map(library_map):
            self.open_standard_map(library_map)
            return
        if self.controller.active_download.in_progress:
            self._show_info(
                "Finish or stop the current map library download before "
                "starting another.",
                duration_ms=7000,
                max_wraplength=360,
            )
            return
        self.set_row_metadata(library_map, "Preparing download…")
        row = self.standard_library_row(
            library_map,
            downloaded=False,
            enabled=False,
        )
        self.set_standard_action(library_map, row)
        self.set_non_active_actions_enabled(library_map, False)
        self.start_catalog_fetch(pending_map=library_map)

    def on_map_action(self, library_map) -> None:
        """Open a local map or start the standard-library download workflow."""
        if self._cache_rebuild_blocks_map_actions():
            return
        self.sync_map_library_root_dir()
        resolved_map = self.controller.resolve_catalog_entry(library_map)
        if self.is_downloaded(self.map_library_root_dir, resolved_map):
            self.open_standard_map(library_map)
            return
        if self.controller.catalog_fetch.loading and getattr(
            resolved_map,
            "download_url",
            None,
        ) is None:
            self.prepare_catalog_for_download(library_map)
            return
        self.start_inline_download(resolved_map)

    def reset_progress(self, library_map) -> None:
        """Hide and reset one standard-library row progress strip."""
        self.panel.reset_standard_progress(self.controller.map_key(library_map))

    def show_progress(self, library_map) -> None:
        """Show an empty progress strip for one standard-library row."""
        self.panel.show_standard_progress(self.controller.map_key(library_map))

    def apply_download_progress(
        self,
        library_map,
        progress: StandardLibraryDownloadProgress,
    ) -> None:
        """Apply a worker progress message to the matching row."""
        self.panel.apply_standard_progress(
            self.controller.map_key(library_map),
            progress.downloaded_bytes,
            progress.total_bytes,
        )

    def _show_error(
        self,
        message: str,
        *,
        duration_ms: int = 9000,
        max_wraplength: int | None = None,
    ) -> None:
        self.show_feedback(
            message,
            kind="error",
            duration_ms=duration_ms,
            max_wraplength=max_wraplength,
        )

    def _show_info(
        self,
        message: str,
        *,
        duration_ms: int = 7000,
        max_wraplength: int | None = None,
    ) -> None:
        self.show_feedback(
            message,
            kind="info",
            duration_ms=duration_ms,
            max_wraplength=max_wraplength,
        )
