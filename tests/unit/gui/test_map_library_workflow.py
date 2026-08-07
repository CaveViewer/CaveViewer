"""Exercise splash Map Library workflow state without constructing Tk widgets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from caveviewer.core.capabilities import (
    CapabilityResult,
    FileSelectionRoute,
    FileSelectionTarget,
)
from caveviewer.core.chunking.staging import ResumableObjImport
from caveviewer.gui.cache_rebuild_controller import (
    CacheRebuildFailed,
    CacheRebuildPaused,
    CacheRebuildProgress,
    CacheRebuildStarted,
    CacheRebuildSucceeded,
)
from caveviewer.gui.features import (
    FeatureDecision,
    FeatureId,
    FeatureState,
    decide_map_library_cache_rebuild,
)
from caveviewer.gui.map_cache_rebuild import (
    CacheRebuildPreflight,
    CacheRebuildTarget,
)
from caveviewer.gui.map_library_panel import MapLibraryMenuAction
from caveviewer.gui.map_library_controller import MapLibraryController
from caveviewer.gui.map_library import recent_map_key
from caveviewer.gui.map_library_workflow import (
    MapLibraryWorkflow,
    _cache_rebuild_notification_id,
    _remaining_cache_error,
)
from caveviewer.gui.platform.runtime import FileSelectionPreflight
from caveviewer.gui.standard_library_download import (
    StandardLibraryDownloadProgress,
    StandardLibraryDownloadSucceeded,
)


@dataclass
class _RemovalResult:
    removed_paths: tuple[str, ...] = ()
    error: str | None = None


class _FakeRoot:
    def __init__(self) -> None:
        self.after_calls = []
        self.cancelled = []

    def after(self, delay_ms, callback):
        after_id = f"after-{len(self.after_calls) + 1}"
        self.after_calls.append((after_id, delay_ms, callback))
        return after_id

    def after_cancel(self, after_id) -> None:
        self.cancelled.append(after_id)


class _FakePanel:
    def __init__(self) -> None:
        self.standard_rows = {}
        self.standard_actions = {}
        self.standard_menu_factories = {}
        self.metadata = {}
        self.progress = []
        self.closed_menus = 0
        self.created_parent = None
        self.finished = False

    def create(self, parent) -> None:
        self.created_parent = parent

    def ensure_recent_empty_note(self) -> None:
        self.empty_note = True

    def finish_population(self) -> None:
        self.finished = True

    def close_active_menu(self) -> None:
        self.closed_menus += 1

    def add_recent_row(self, entry, *, action, menu_actions_factory=None):
        self.recent_row = (entry, action, menu_actions_factory)
        return SimpleNamespace(row_shell=object())

    def add_standard_row(self, row, *, action, menu_actions_factory=None):
        self.standard_rows[row.key] = row
        self.standard_actions[row.key] = (row.action_text, action, row.enabled)
        self.standard_menu_factories[row.key] = menu_actions_factory
        return SimpleNamespace(row_shell=object())

    def has_standard_row(self, key: str) -> bool:
        return key in self.standard_rows

    def remove_standard_row(self, key: str) -> None:
        self.removed_standard_key = key
        self.standard_rows.pop(key, None)
        self.standard_actions.pop(key, None)
        self.standard_menu_factories.pop(key, None)

    def set_standard_row_action(
        self,
        key: str,
        text: str,
        command,
        *,
        enabled: bool = True,
        show_stop_progress: bool = False,
        show_pause_progress: bool = False,
    ) -> bool:
        if key not in self.standard_rows:
            return False
        self.standard_actions[key] = (text, command, enabled, show_stop_progress)
        self.standard_pause_progress = getattr(self, "standard_pause_progress", {})
        self.standard_pause_progress[key] = show_pause_progress
        return True

    def set_standard_row_metadata(
        self,
        key: str,
        text: str,
        *,
        error: bool = False,
    ) -> None:
        self.metadata[key] = (text, error)

    def set_row_action(
        self,
        row_widgets,
        text: str,
        command,
        *,
        enabled: bool = True,
        show_stop_progress: bool = False,
        show_pause_progress: bool = False,
    ) -> bool:
        self.row_action = (
            row_widgets,
            text,
            command,
            enabled,
            show_stop_progress,
            show_pause_progress,
        )
        return True

    def set_row_metadata(self, row_widgets, text: str, *, error: bool = False) -> bool:
        self.row_metadata = (row_widgets, text, error)
        self.row_metadata_history = getattr(self, "row_metadata_history", [])
        self.row_metadata_history.append(self.row_metadata)
        return True

    def set_row_progress(self, row_widgets, fraction: float) -> None:
        self.row_progress = (row_widgets, fraction)

    def refresh_standard_row_overflow(self, key: str) -> None:
        self.last_overflow_key = key

    def refresh_row_overflow(self, row_widgets) -> None:
        self.last_row_overflow = row_widgets

    def show_row_status(self, row_widgets, text: str, *, error: bool = False) -> bool:
        self.status = (row_widgets, text, error)
        return True

    def remove_recent_row(self, key: str) -> None:
        self.removed_recent_key = key

    def reset_standard_progress(self, key: str) -> None:
        self.progress.append(("reset", key))

    def show_standard_progress(self, key: str) -> None:
        self.progress.append(("show", key))

    def apply_standard_progress(
        self,
        key: str,
        downloaded_bytes: int,
        total_bytes: int | None,
    ) -> None:
        self.progress.append(
            ("apply", key, downloaded_bytes, total_bytes)
        )


class _FakeLogger:
    def __init__(self) -> None:
        self.warnings = []

    def warning(self, message, *args) -> None:
        self.warnings.append((message, args))


class _FakeDesktopServices:
    def __init__(self, *, file_selection=None) -> None:
        self.file_selection = file_selection
        self.file_calls = []

    def choose_file(self, **options):
        self.file_calls.append(options)
        return self.file_selection


class _FakeInhibitor:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeCacheRebuildController:
    def __init__(self, *, pause_supported: bool = True) -> None:
        self.active = False
        self.pause_supported = pause_supported
        self.start_calls = []
        self.resume_start_calls = []
        self.pause_calls = 0
        self.updates = []

    def start(self, target, *, resume_required: bool = False):
        self.start_calls.append(target)
        self.resume_start_calls.append(resume_required)
        self.active = True
        return CacheRebuildStarted(target)

    def request_pause(self) -> bool:
        if not self.active or not self.pause_supported:
            return False
        self.pause_calls += 1
        return True

    def request_pause_for_close(self) -> bool:
        return self.request_pause()

    def poll(self):
        updates = tuple(self.updates)
        self.updates.clear()
        if any(
            isinstance(
                update,
                (CacheRebuildSucceeded, CacheRebuildPaused, CacheRebuildFailed),
            )
            for update in updates
        ):
            self.active = False
        return updates


def _library_map(
    display_name: str = "Test Cave",
    asset_name: str = "test.zip",
    download_url: str | None = "https://example.invalid/test.zip",
    size_bytes: int | None = None,
    catalog_id: str | None = None,
):
    return SimpleNamespace(
        display_name=display_name,
        asset_name=asset_name,
        download_url=download_url,
        size_bytes=size_bytes,
        catalog_id=catalog_id,
    )


def _workflow(
    maps,
    *,
    is_downloaded=None,
    existing_path=None,
    fetch_catalog=None,
    start_download_worker=None,
    start_catalog_worker=None,
    remove_recent_path=None,
    has_cache=None,
    remove_cache=None,
    map_library_root_dir_provider=None,
    desktop_services=None,
    platform_runtime=None,
    guided_dive_menu=None,
    guided_dive_preflight=None,
    open_guided_dive=None,
    cache_rebuild_preflight=None,
    cache_rebuild_controller=None,
    splash_is_foreground=None,
    notification_sender=None,
):
    root = _FakeRoot()
    panel = _FakePanel()
    controller = MapLibraryController(maps)
    feedback = []
    closed_inhibitors = []
    opened = []
    inhibitor = _FakeInhibitor()
    desktop_services = desktop_services or _FakeDesktopServices()

    workflow = MapLibraryWorkflow(
        root=root,
        controller=controller,
        panel=panel,
        standard_library_maps=maps,
        map_library_root_dir="/library",
        desktop_services=desktop_services,
        platform_runtime=platform_runtime,
        splash_exists=lambda: True,
        open_map=opened.append,
        show_feedback=lambda message, **kwargs: feedback.append(
            (message, kwargs)
        ),
        logger=_FakeLogger(),
        has_cache=has_cache or (lambda _path: False),
        remove_cache=(
            remove_cache
            or (lambda _path: SimpleNamespace(error=None, removed=False))
        ),
        remove_recent_path=remove_recent_path or (lambda _path: None),
        is_downloaded=is_downloaded or (lambda _root, _map: False),
        existing_path=existing_path or (lambda _root, _map: None),
        remove_downloaded=lambda _root, _map: _RemovalResult(("/removed",)),
        fetch_catalog=fetch_catalog or (lambda: ([], None)),
        map_library_root_dir_provider=map_library_root_dir_provider,
        start_download_worker=(
            start_download_worker
            or (lambda _selection, _map, _event, _queue: object())
        ),
        start_catalog_worker=start_catalog_worker or (lambda target: target()),
        directory_selection_factory=lambda path: SimpleNamespace(path=path),
        inhibit_desktop=lambda *_args, **_kwargs: inhibitor,
        close_inhibitor=lambda handle: closed_inhibitors.append(handle),
        guided_dive_menu=guided_dive_menu or _hidden_guided_dive_decision,
        guided_dive_preflight=(
            guided_dive_preflight or _unexpected_guided_dive_preflight
        ),
        open_guided_dive=open_guided_dive,
        cache_rebuild_preflight=(
            cache_rebuild_preflight or _hidden_cache_rebuild_preflight
        ),
        cache_rebuild_controller=(
            cache_rebuild_controller or _FakeCacheRebuildController()
        ),
        splash_is_foreground=splash_is_foreground,
        notification_sender=(
            notification_sender or (lambda *_args, **_kwargs: True)
        ),
    )
    return SimpleNamespace(
        workflow=workflow,
        root=root,
        panel=panel,
        controller=controller,
        feedback=feedback,
        opened=opened,
        inhibitor=inhibitor,
        closed_inhibitors=closed_inhibitors,
        desktop_services=desktop_services,
        cache_rebuild_controller=workflow.cache_rebuild_controller,
    )


def _enabled_guided_dive_decision(_map_path: str | None = None) -> FeatureDecision:
    return FeatureDecision(
        feature=FeatureId.GUIDED_DIVE_PLAYBACK,
        state=FeatureState.ENABLED,
        reason_code="guided_dive_playback_available",
        explanation="Dive plan playback is available for this map.",
        route="map_local_trace",
    )


def _hidden_guided_dive_decision(_map_path: str | None = None) -> FeatureDecision:
    return FeatureDecision(
        feature=FeatureId.GUIDED_DIVE_PLAYBACK,
        state=FeatureState.HIDDEN,
        reason_code="guided_dive_trace_unavailable",
        explanation="No completed dive plans are available for this map.",
    )


def _unexpected_guided_dive_preflight(*_args):
    raise AssertionError("Guided Dive preflight should not run")


def _cache_rebuild_target() -> CacheRebuildTarget:
    return CacheRebuildTarget(
        map_path=Path("/maps/Recent Cave"),
        model_descriptor={
            "format": "obj",
            "obj_path": "/maps/Recent Cave/cave.obj",
            "mtl_path": "/maps/Recent Cave/cave.mtl",
        },
        textures_dir=Path("/maps/Recent Cave"),
        cache_dir=Path("/maps/Recent Cave/_cache"),
    )


def _enabled_cache_rebuild_preflight(
    _map_path: str | None = None,
) -> CacheRebuildPreflight:
    capability = CapabilityResult.available(
        _cache_rebuild_target(),
        reason_code="map_cache_rebuild_target_available",
    )
    return CacheRebuildPreflight(
        capability=capability,
        decision=decide_map_library_cache_rebuild(capability),
    )


def _enabled_cache_build_preflight(
    _map_path: str | None = None,
) -> CacheRebuildPreflight:
    capability = CapabilityResult.available(
        CacheRebuildTarget(
            map_path=Path("/maps/Recent Cave"),
            model_descriptor={
                "format": "obj",
                "obj_path": "/maps/Recent Cave/cave.obj",
                "mtl_path": "/maps/Recent Cave/cave.mtl",
            },
            textures_dir=Path("/maps/Recent Cave"),
            cache_dir=Path("/maps/Recent Cave/_cache"),
            operation="build",
        ),
        reason_code="map_cache_build_target_available",
    )
    return CacheRebuildPreflight(
        capability=capability,
        decision=decide_map_library_cache_rebuild(capability),
    )


def _resumable_cache_rebuild_preflight(
    _map_path: str | None = None,
) -> CacheRebuildPreflight:
    preflight = _enabled_cache_rebuild_preflight()
    return CacheRebuildPreflight(
        capability=preflight.capability,
        decision=preflight.decision,
        resumable_import=ResumableObjImport(
            resume_dir=Path("/maps/.cache.resume-checkpoint"),
            stage="bucketing",
            progress_fraction=0.5,
        ),
    )


def _hidden_cache_rebuild_preflight(
    _map_path: str | None = None,
) -> CacheRebuildPreflight:
    capability = CapabilityResult.unavailable(
        reason_code="map_cache_rebuild_no_generated_cache",
    )
    return CacheRebuildPreflight(
        capability=capability,
        decision=decide_map_library_cache_rebuild(capability),
    )


def test_populate_panel_creates_rows_and_starts_catalog_fetch():
    library_map = _library_map()
    state = _workflow([library_map])

    state.workflow.populate_panel("parent", ["/maps/user-map"])

    assert state.panel.created_parent == "parent"
    assert state.panel.finished
    assert "Test Cave" in state.panel.standard_rows
    assert state.controller.catalog_fetch.loading
    assert state.root.after_calls[-1][1] == 120


def test_map_library_root_change_refreshes_standard_rows():
    library_map = _library_map()
    checked_roots = []

    def is_downloaded(root, _map):
        checked_roots.append(root)
        return root == "/downloads"

    state = _workflow(
        [library_map],
        is_downloaded=is_downloaded,
        existing_path=lambda root, _map: f"{root}/Test Cave",
    )
    state.workflow.add_standard_row(library_map)

    state.workflow.set_map_library_root_dir("/downloads")

    assert state.workflow.map_library_root_dir == "/downloads"
    assert "/downloads" in checked_roots
    assert state.panel.standard_actions["Test Cave"][0] == "Open"
    assert state.panel.metadata["Test Cave"] == ("Downloaded", False)


def test_download_action_syncs_latest_map_library_root_before_worker_start():
    library_map = _library_map()
    selections = []

    state = _workflow(
        [library_map],
        map_library_root_dir_provider=lambda: "/custom-downloads",
        start_download_worker=lambda selection, _map, _event, _queue: (
            selections.append(selection.path) or object()
        ),
    )
    state.workflow.add_standard_row(library_map)

    state.workflow.on_map_action(library_map)

    assert state.workflow.map_library_root_dir == "/custom-downloads"
    assert selections == ["/custom-downloads"]


def test_map_library_root_change_waits_for_active_download():
    library_map = _library_map()
    state = _workflow([library_map])
    state.controller.begin_download(
        library_map,
        cancel_event=object(),
        inhibitor=None,
    )

    state.workflow.set_map_library_root_dir("/downloads")

    assert state.workflow.map_library_root_dir == "/library"
    assert state.feedback
    assert "after the current download finishes" in state.feedback[-1][0]


def test_downloaded_standard_library_menu_offers_build_cache_without_cache():
    """Downloaded standard maps without generated cache can build it in-place."""
    library_map = _library_map()
    rebuild_controller = _FakeCacheRebuildController()
    state = _workflow(
        [library_map],
        is_downloaded=lambda _root, _map: True,
        existing_path=lambda _root, _map: "/library/Test Cave",
        has_cache=lambda _path: False,
        cache_rebuild_preflight=_enabled_cache_build_preflight,
        cache_rebuild_controller=rebuild_controller,
    )
    row_widgets = SimpleNamespace(row_shell=object())

    state.workflow.add_standard_row(library_map)
    menu_factory = state.panel.standard_menu_factories["Test Cave"]

    actions = menu_factory(row_widgets)

    assert [label for label, _command in actions] == [
        "Remove map files",
        "Build cache",
    ]

    next(action for action in actions if action[0] == "Build cache")[1]()

    assert rebuild_controller.start_calls[0].operation == "build"
    assert state.panel.row_metadata[1] == "Building cache — Starting import"
    assert state.opened == []


def test_downloaded_standard_library_menu_preflights_local_guided_dive():
    library_map = _library_map()
    selected_trace = "/library/Test Cave/_guided_dives/favorite.jsonl"
    opened_guided_dive = []
    desktop_services = _FakeDesktopServices(
        file_selection=SimpleNamespace(path=selected_trace)
    )

    def preflight(map_path, trace_path):
        assert map_path == "/library/Test Cave"
        assert trace_path == selected_trace
        return SimpleNamespace(
            capability=CapabilityResult.available(
                SimpleNamespace(trace=SimpleNamespace(path=trace_path)),
                reason_code="guided_dive_playback_target_available",
            ),
            decision=_enabled_guided_dive_decision(),
        )

    state = _workflow(
        [library_map],
        is_downloaded=lambda _root, _map: True,
        existing_path=lambda _root, _map: "/library/Test Cave",
        desktop_services=desktop_services,
        guided_dive_menu=_enabled_guided_dive_decision,
        guided_dive_preflight=preflight,
        open_guided_dive=opened_guided_dive.append,
    )

    state.workflow.add_standard_row(library_map)
    menu_factory = state.panel.standard_menu_factories["Test Cave"]
    actions = menu_factory(SimpleNamespace(row_shell=object()))

    assert [label for label, _command in actions] == [
        "Open dive plan…",
        "Remove map files",
    ]

    actions[0][1]()

    assert opened_guided_dive == [selected_trace]
    assert desktop_services.file_calls == [
        {
            "title": "Open Dive Plan",
            "initial_dir": str(Path("/library/Test Cave").resolve() / "_guided_dives"),
            "parent": state.root,
        }
    ]


def test_recent_map_menu_preflights_local_guided_dive():
    selected_trace = "/maps/Recent Cave/_guided_dives/favorite.jsonl"
    opened_guided_dive = []
    desktop_services = _FakeDesktopServices(
        file_selection=SimpleNamespace(path=selected_trace)
    )

    def preflight(map_path, trace_path):
        assert map_path == "/maps/Recent Cave"
        assert trace_path == selected_trace
        return SimpleNamespace(
            capability=CapabilityResult.available(
                SimpleNamespace(trace=SimpleNamespace(path=trace_path)),
                reason_code="guided_dive_playback_target_available",
            ),
            decision=_enabled_guided_dive_decision(),
        )

    state = _workflow(
        [],
        desktop_services=desktop_services,
        guided_dive_menu=_enabled_guided_dive_decision,
        guided_dive_preflight=preflight,
        open_guided_dive=opened_guided_dive.append,
    )

    state.workflow.add_recent_row("/maps/Recent Cave")
    _entry, _open_map, menu_factory = state.panel.recent_row
    actions = menu_factory(SimpleNamespace(row_shell=object()))

    assert [label for label, _command in actions] == [
        "Open dive plan…",
        "Remove from this list",
    ]

    actions[0][1]()

    assert opened_guided_dive == [selected_trace]


def test_guided_dive_file_picker_uses_runtime_preflight_and_rechecks_adapter_route():
    selected_trace = "/maps/Recent Cave/_guided_dives/favorite.jsonl"
    opened_guided_dive = []
    preflight_calls = []
    route_checks = []
    target = FileSelectionTarget(
        primary_route=FileSelectionRoute.PORTAL,
        fallback_route=FileSelectionRoute.TK,
    )

    class RoutedDesktopServices:
        def file_selection_target(self):
            route_checks.append(True)
            return target

        def choose_file(self, **options):
            assert options == {
                "title": "Open Dive Plan",
                "initial_dir": str(
                    Path("/maps/Recent Cave").resolve() / "_guided_dives"
                ),
                "parent": state.root,
            }
            return SimpleNamespace(path=selected_trace)

    desktop_services = RoutedDesktopServices()

    class FakeRuntime:
        def __init__(self):
            self.desktop_services = desktop_services

        def file_selection_preflight(self):
            preflight_calls.append(True)
            return FileSelectionPreflight(
                capability=CapabilityResult.available(
                    target,
                    reason_code="file_selection_portal_route_available",
                ),
                decision=FeatureDecision(
                    feature=FeatureId.FILE_SELECTION,
                    state=FeatureState.ENABLED,
                    reason_code="file_selection_available",
                    explanation="File selection is available.",
                    route="portal_then_tk",
                ),
            )

    state = _workflow(
        [],
        desktop_services=desktop_services,
        platform_runtime=FakeRuntime(),
        guided_dive_menu=_enabled_guided_dive_decision,
        guided_dive_preflight=lambda _map_path, trace_path: SimpleNamespace(
            capability=CapabilityResult.available(
                SimpleNamespace(trace=SimpleNamespace(path=trace_path)),
                reason_code="guided_dive_playback_target_available",
            ),
            decision=_enabled_guided_dive_decision(),
        ),
        open_guided_dive=opened_guided_dive.append,
    )

    state.workflow.open_guided_dive_for_map("/maps/Recent Cave")

    assert preflight_calls == [True]
    assert route_checks == [True]
    assert opened_guided_dive == [selected_trace]


def test_guided_dive_file_picker_blocks_a_changed_route_before_choosing_file():
    portal_target = FileSelectionTarget(
        primary_route=FileSelectionRoute.PORTAL,
        fallback_route=FileSelectionRoute.TK,
    )
    target_calls = []
    guided_dive_preflight_calls = []

    class ChangingDesktopServices:
        def file_selection_target(self):
            target_calls.append(True)
            if len(target_calls) == 1:
                return portal_target
            return FileSelectionTarget(FileSelectionRoute.TK)

        def choose_file(self, **_options):
            raise AssertionError("a changed route must not open a file picker")

    state = _workflow(
        [],
        desktop_services=ChangingDesktopServices(),
        guided_dive_menu=_enabled_guided_dive_decision,
        guided_dive_preflight=lambda *_args: guided_dive_preflight_calls.append(
            True
        ),
        open_guided_dive=lambda _path: None,
    )

    state.workflow.open_guided_dive_for_map("/maps/Recent Cave")

    assert target_calls == [True, True]
    assert guided_dive_preflight_calls == []
    assert state.feedback[-1][0] == "Couldn't open the dive plan picker."


def test_guided_dive_file_picker_blocks_an_unavailable_file_route():
    guided_dive_preflight_calls = []

    state = _workflow(
        [],
        desktop_services=object(),
        guided_dive_menu=_enabled_guided_dive_decision,
        guided_dive_preflight=lambda *_args: guided_dive_preflight_calls.append(
            True
        ),
        open_guided_dive=lambda _path: None,
    )

    state.workflow.open_guided_dive_for_map("/maps/Recent Cave")

    assert guided_dive_preflight_calls == []
    assert state.feedback[-1][0] == "File selection is unavailable in this environment."


def test_guided_dive_preflight_rejection_keeps_the_splash_open():
    selected_trace = "/maps/Recent Cave/_guided_dives/broken.jsonl"
    opened_guided_dive = []
    rejected = FeatureDecision(
        feature=FeatureId.GUIDED_DIVE_PLAYBACK,
        state=FeatureState.DISABLED,
        reason_code="guided_dive_cache_incompatible",
        explanation="This dive plan does not match the current map cache.",
    )
    state = _workflow(
        [],
        desktop_services=_FakeDesktopServices(
            file_selection=SimpleNamespace(path=selected_trace)
        ),
        guided_dive_menu=_enabled_guided_dive_decision,
        guided_dive_preflight=lambda _map_path, _trace_path: SimpleNamespace(
            capability=CapabilityResult.unavailable(
                reason_code="guided_dive_cache_incompatible"
            ),
            decision=rejected,
        ),
        open_guided_dive=opened_guided_dive.append,
    )

    state.workflow.open_guided_dive_for_map("/maps/Recent Cave")

    assert opened_guided_dive == []
    assert state.feedback[-1][0] == rejected.explanation
    assert state.feedback[-1][1]["kind"] == "error"


def test_downloaded_standard_library_menu_includes_remove_cache_when_cache_exists():
    """Downloaded standard maps with generated cache expose both cleanup actions."""
    library_map = _library_map()
    removed_cache_paths = []
    state = _workflow(
        [library_map],
        is_downloaded=lambda _root, _map: True,
        existing_path=lambda _root, _map: "/library/Test Cave",
        has_cache=lambda path: path == "/library/Test Cave",
        remove_cache=lambda path: (
            removed_cache_paths.append(path)
            or SimpleNamespace(error=None, removed=True)
        ),
    )
    row_widgets = SimpleNamespace(row_shell=object())

    state.workflow.add_standard_row(library_map)
    menu_factory = state.panel.standard_menu_factories["Test Cave"]
    actions = menu_factory(row_widgets)

    assert [label for label, _command in actions] == [
        "Remove map files",
        "Remove cache",
    ]

    actions[1][1]()

    assert removed_cache_paths == ["/library/Test Cave"]
    assert state.panel.status == (row_widgets, "Cache removed", False)
    assert state.panel.last_row_overflow is row_widgets


def test_downloaded_folder_removal_covers_adjacent_cache_error():
    cache_result = SimpleNamespace(
        cache_dir="/library/Test Cave/_cache",
        error="cache path is not removable",
    )

    assert _remaining_cache_error(cache_result, ("/library/Test Cave",)) is None


def test_external_cache_error_survives_downloaded_folder_removal():
    cache_result = SimpleNamespace(
        cache_dir="/external/cache/Test Cave",
        error="cache path is not removable",
    )

    assert _remaining_cache_error(cache_result, ("/library/Test Cave",)) == (
        "cache path is not removable"
    )


def test_download_success_applies_progress_and_open_action():
    library_map = _library_map()
    download_calls = []

    def start_download_worker(selection, active_map, cancel_event, result_queue):
        download_calls.append(
            (selection, active_map, cancel_event, result_queue)
        )
        return object()

    state = _workflow(
        [library_map],
        start_download_worker=start_download_worker,
    )
    state.panel.add_standard_row(
        state.controller.row(library_map, downloaded=False),
        action=lambda: None,
    )

    state.workflow.start_inline_download(library_map)
    active_action = state.panel.standard_actions["Test Cave"]
    assert active_action[0] == ""
    assert active_action[3] is True
    _selection, _map, cancel_event, result_queue = download_calls[0]
    result_queue.put(StandardLibraryDownloadProgress(40, 100))
    result_queue.put(StandardLibraryDownloadSucceeded("/library/Test Cave"))

    state.workflow.poll_download_queue(library_map, result_queue, cancel_event)

    assert ("show", "Test Cave") in state.panel.progress
    assert ("apply", "Test Cave", 40, 100) in state.panel.progress
    assert state.panel.standard_actions["Test Cave"][0] == "Open"
    assert not state.controller.active_download.in_progress
    assert state.closed_inhibitors == [state.inhibitor]


def test_download_stop_action_requests_cancel_and_stays_in_stop_mode():
    library_map = _library_map()
    download_calls = []

    def start_download_worker(selection, active_map, cancel_event, result_queue):
        download_calls.append(
            (selection, active_map, cancel_event, result_queue)
        )
        return object()

    state = _workflow(
        [library_map],
        start_download_worker=start_download_worker,
    )
    state.panel.add_standard_row(
        state.controller.row(library_map, downloaded=False),
        action=lambda: None,
    )

    state.workflow.start_inline_download(library_map)
    _text, stop_action, enabled, show_stop_progress = state.panel.standard_actions[
        "Test Cave"
    ]
    _selection, _map, cancel_event, _result_queue = download_calls[0]

    assert enabled is True
    assert show_stop_progress is True

    stop_action()

    assert cancel_event.is_set()
    assert state.panel.standard_actions["Test Cave"][0] == ""
    assert state.panel.standard_actions["Test Cave"][2] is False
    assert state.panel.standard_actions["Test Cave"][3] is True
    assert state.panel.metadata["Test Cave"] == ("Stopping…", False)


def test_close_cancels_active_download_poll_and_closes_menu():
    library_map = _library_map()
    download_calls = []

    def start_download_worker(selection, active_map, cancel_event, result_queue):
        download_calls.append(
            (selection, active_map, cancel_event, result_queue)
        )
        return object()

    state = _workflow(
        [library_map],
        start_download_worker=start_download_worker,
    )
    state.panel.add_standard_row(
        state.controller.row(library_map, downloaded=False),
        action=lambda: None,
    )
    state.workflow.start_inline_download(library_map)
    _selection, _map, cancel_event, _result_queue = download_calls[0]

    state.workflow.close()

    assert cancel_event.is_set()
    assert state.root.cancelled == ["after-1"]
    assert state.panel.closed_menus == 1
    assert state.closed_inhibitors == [state.inhibitor]
    assert not state.controller.active_download.in_progress


def test_close_cancels_pending_catalog_poll():
    library_map = _library_map()
    state = _workflow([library_map])

    state.workflow.start_catalog_fetch()
    assert state.controller.catalog_fetch.loading

    state.workflow.close()

    assert state.root.cancelled == ["after-1"]
    assert not state.controller.catalog_fetch.loading
    assert state.controller.catalog_fetch.queue is None


def test_remove_recent_map_uses_injected_history_side_effect():
    library_map = _library_map()
    removed_paths = []
    state = _workflow(
        [library_map],
        remove_recent_path=removed_paths.append,
    )
    state.workflow.recent_map_paths = ["/maps/one", "/maps/two"]

    state.workflow.remove_recent_map("/maps/one")

    assert removed_paths == ["/maps/one"]
    assert state.workflow.recent_map_paths == ["/maps/two"]
    assert state.panel.removed_recent_key == recent_map_key("/maps/one")


def test_pending_download_waits_for_catalog_then_starts_resolved_download():
    pending_map = _library_map(download_url=None)
    catalog_map = _library_map(size_bytes=12_000_000)
    download_calls = []

    def start_download_worker(selection, active_map, cancel_event, result_queue):
        download_calls.append(active_map)
        return object()

    state = _workflow(
        [pending_map],
        fetch_catalog=lambda: ([catalog_map], None),
        start_download_worker=start_download_worker,
    )
    state.panel.add_standard_row(
        state.controller.row(pending_map, downloaded=False),
        action=lambda: None,
    )

    state.workflow.prepare_catalog_for_download(pending_map)
    state.workflow.poll_catalog_fetch()

    assert download_calls == [catalog_map]
    assert state.panel.metadata["Test Cave"] == ("Downloading…", False)


def test_catalog_refresh_adds_new_remote_standard_library_rows():
    initial_map = _library_map(
        display_name="Initial Cave",
        asset_name="initial.zip",
        catalog_id="initial-cave",
    )
    new_map = _library_map(
        display_name="New Remote Cave",
        asset_name="new.zip",
        size_bytes=25 * 1024 * 1024,
        catalog_id="new-remote-cave",
    )
    state = _workflow(
        [initial_map],
        fetch_catalog=lambda: ([initial_map, new_map], None),
    )

    state.workflow.populate_panel("parent", [])
    state.workflow.poll_catalog_fetch()

    assert set(state.panel.standard_rows) == {
        "initial-cave",
        "new-remote-cave",
    }
    assert state.panel.standard_rows["new-remote-cave"].title == "New Remote Cave"
    assert state.panel.standard_rows["new-remote-cave"].detail == "25 MB"


def test_catalog_refresh_removes_stale_not_downloaded_rows():
    stale_map = _library_map(
        display_name="Stale Cave",
        asset_name="stale.zip",
        catalog_id="stale-cave",
    )
    current_map = _library_map(
        display_name="Current Cave",
        asset_name="current.zip",
        catalog_id="current-cave",
    )
    state = _workflow([stale_map], fetch_catalog=lambda: ([current_map], None))

    state.workflow.populate_panel("parent", [])
    state.workflow.poll_catalog_fetch()

    assert "stale-cave" not in state.panel.standard_rows
    assert "current-cave" in state.panel.standard_rows
    assert state.panel.removed_standard_key == "stale-cave"


def test_catalog_refresh_keeps_stale_downloaded_rows():
    stale_map = _library_map(
        display_name="Downloaded Stale Cave",
        asset_name="stale.zip",
        catalog_id="downloaded-stale-cave",
    )
    state = _workflow(
        [stale_map],
        fetch_catalog=lambda: ([], None),
        is_downloaded=lambda _root, library_map: (
            library_map.catalog_id == "downloaded-stale-cave"
        ),
        existing_path=lambda _root, library_map: (
            "/library/Downloaded Stale Cave"
            if library_map.catalog_id == "downloaded-stale-cave"
            else None
        ),
    )

    state.workflow.populate_panel("parent", [])
    state.workflow.poll_catalog_fetch()

    assert "downloaded-stale-cave" in state.panel.standard_rows
    assert state.panel.standard_actions["downloaded-stale-cave"][0] == "Open"


def test_unavailable_catalog_details_show_retry_state():
    pending_map = _library_map(download_url=None)
    state = _workflow(
        [pending_map],
        fetch_catalog=lambda: ([pending_map], "offline"),
    )
    state.panel.add_standard_row(
        state.controller.row(pending_map, downloaded=False),
        action=lambda: None,
    )

    state.workflow.prepare_catalog_for_download(pending_map)
    state.workflow.poll_catalog_fetch()

    assert state.panel.metadata["Test Cave"] == (
        "Download info unavailable",
        True,
    )
    assert state.panel.standard_actions["Test Cave"][0] == "Retry"
    assert state.feedback[-1][1]["kind"] == "error"


def test_recent_menu_places_rebuild_above_remove_cache_and_never_opens_map():
    rebuild_controller = _FakeCacheRebuildController()
    state = _workflow(
        [],
        has_cache=lambda path: path == "/maps/Recent Cave",
        cache_rebuild_preflight=_enabled_cache_rebuild_preflight,
        cache_rebuild_controller=rebuild_controller,
    )
    row_widgets = SimpleNamespace(row_shell=object())

    state.workflow.add_recent_row("/maps/Recent Cave")
    _entry, _open_map, menu_factory = state.panel.recent_row
    actions = menu_factory(row_widgets)

    assert [item[0] for item in actions] == [
        "Remove from this list",
        "Rebuild cache",
        "Remove cache",
    ]

    next(action for action in actions if action[0] == "Rebuild cache")[1]()

    assert rebuild_controller.start_calls == [_cache_rebuild_target()]
    assert state.panel.row_action[1] == "Pause"
    assert state.panel.row_metadata[1] == "Rebuilding cache — Starting import"
    assert state.root.after_calls[-1][1] == 100
    assert state.opened == []


def test_recent_menu_resumes_a_validated_checkpoint_without_opening_map():
    rebuild_controller = _FakeCacheRebuildController()
    state = _workflow(
        [],
        cache_rebuild_preflight=_resumable_cache_rebuild_preflight,
        cache_rebuild_controller=rebuild_controller,
    )
    row_widgets = SimpleNamespace(row_shell=object())

    state.workflow.add_recent_row("/maps/Recent Cave")
    _entry, _open_map, menu_factory = state.panel.recent_row
    actions = menu_factory(row_widgets)

    assert [item[0] for item in actions] == [
        "Remove from this list",
        "Resume cache rebuild",
    ]

    next(action for action in actions if action[0] == "Resume cache rebuild")[1]()

    assert rebuild_controller.start_calls == [_cache_rebuild_target()]
    assert rebuild_controller.resume_start_calls == [True]
    assert state.panel.row_action[1] == "Pause"
    assert state.opened == []


def test_paused_rebuild_is_resumable_from_a_fresh_workflow_instance():
    original_controller = _FakeCacheRebuildController()
    original = _workflow(
        [],
        cache_rebuild_preflight=_enabled_cache_rebuild_preflight,
        cache_rebuild_controller=original_controller,
    )
    original.workflow.start_cache_rebuild(
        "/maps/Recent Cave",
        "Recent Cave",
        SimpleNamespace(row_shell=object()),
    )
    original_controller.updates = [
        CacheRebuildPaused(
            target=_cache_rebuild_target(),
            resume_dir="/maps/.cache.resume-checkpoint",
        )
    ]
    original.workflow.poll_cache_rebuild()

    reopened = _workflow(
        [],
        cache_rebuild_preflight=_resumable_cache_rebuild_preflight,
    )
    reopened.workflow.add_recent_row("/maps/Recent Cave")
    _entry, _open_map, menu_factory = reopened.panel.recent_row
    actions = menu_factory(SimpleNamespace(row_shell=object()))

    assert original.workflow is not reopened.workflow
    assert original.panel.status[1] == "Cache rebuild paused"
    assert [item[0] for item in actions] == [
        "Remove from this list",
        "Resume cache rebuild",
    ]


def test_downloaded_map_menu_offers_resume_for_a_validated_checkpoint():
    library_map = _library_map()
    rebuild_controller = _FakeCacheRebuildController()
    state = _workflow(
        [library_map],
        is_downloaded=lambda _root, _map: True,
        existing_path=lambda _root, _map: "/library/Test Cave",
        cache_rebuild_preflight=_resumable_cache_rebuild_preflight,
        cache_rebuild_controller=rebuild_controller,
    )
    row_widgets = SimpleNamespace(row_shell=object())

    state.workflow.add_standard_row(library_map)
    actions = state.panel.standard_menu_factories["Test Cave"](row_widgets)

    assert [item[0] for item in actions] == [
        "Remove map files",
        "Resume cache rebuild",
    ]

    next(action for action in actions if action[0] == "Resume cache rebuild")[1]()

    assert rebuild_controller.start_calls == [_cache_rebuild_target()]
    assert rebuild_controller.resume_start_calls == [True]


def test_resume_click_rejects_a_checkpoint_lost_since_menu_opened():
    preflights = iter(
        [
            _resumable_cache_rebuild_preflight(),
            _enabled_cache_rebuild_preflight(),
        ]
    )
    rebuild_controller = _FakeCacheRebuildController()
    state = _workflow(
        [],
        cache_rebuild_preflight=lambda _path: next(preflights),
        cache_rebuild_controller=rebuild_controller,
    )
    row_widgets = SimpleNamespace(row_shell=object())

    state.workflow.add_recent_row("/maps/Recent Cave")
    _entry, _open_map, menu_factory = state.panel.recent_row
    actions = menu_factory(row_widgets)

    next(action for action in actions if action[0] == "Resume cache rebuild")[1]()

    assert rebuild_controller.start_calls == []
    assert "checkpoint is no longer usable" in state.feedback[-1][0].lower()


def test_busy_rebuild_keeps_resume_label_but_disables_the_action():
    rebuild_controller = _FakeCacheRebuildController()
    rebuild_controller.active = True
    state = _workflow(
        [],
        cache_rebuild_preflight=_resumable_cache_rebuild_preflight,
        cache_rebuild_controller=rebuild_controller,
    )

    action = state.workflow.cache_rebuild_menu_action(
        "/maps/Recent Cave",
        "Recent Cave",
        SimpleNamespace(row_shell=object()),
    )

    assert isinstance(action, MapLibraryMenuAction)
    assert action.label == "Resume cache rebuild"
    assert action.action is None
    assert "already" in action.explanation.lower()


def test_rebuild_progress_success_restores_open_and_reports_completion():
    rebuild_controller = _FakeCacheRebuildController()
    state = _workflow(
        [],
        cache_rebuild_preflight=_enabled_cache_rebuild_preflight,
        cache_rebuild_controller=rebuild_controller,
    )
    row_widgets = SimpleNamespace(row_shell=object())

    state.workflow.start_cache_rebuild(
        "/maps/Recent Cave",
        "Recent Cave",
        row_widgets,
    )
    rebuild_controller.updates = [
        CacheRebuildProgress(
            target=_cache_rebuild_target(),
            stage="building chunks",
            fraction=0.625,
        ),
        CacheRebuildSucceeded(
            target=_cache_rebuild_target(),
            cache_dir="/maps/Recent Cave/_cache",
        ),
    ]

    state.workflow.poll_cache_rebuild()

    assert state.panel.row_progress == (row_widgets, 0.625)
    assert (row_widgets, "Rebuilding cache — Building chunks", False) in (
        state.panel.row_metadata_history
    )
    assert state.panel.row_action[1] == "Open"
    assert state.panel.status == (row_widgets, "Cache rebuilt", False)
    assert state.opened == []


def test_build_progress_success_restores_open_and_reports_completion():
    rebuild_controller = _FakeCacheRebuildController()
    state = _workflow(
        [],
        cache_rebuild_preflight=_enabled_cache_build_preflight,
        cache_rebuild_controller=rebuild_controller,
    )
    row_widgets = SimpleNamespace(row_shell=object())

    state.workflow.start_cache_rebuild(
        "/maps/Recent Cave",
        "Recent Cave",
        row_widgets,
    )
    rebuild_controller.updates = [
        CacheRebuildProgress(
            target=rebuild_controller.start_calls[0],
            stage="building chunks",
            fraction=0.625,
        ),
        CacheRebuildSucceeded(
            target=rebuild_controller.start_calls[0],
            cache_dir="/maps/Recent Cave/_cache",
        ),
    ]

    state.workflow.poll_cache_rebuild()

    assert state.panel.row_progress == (row_widgets, 0.625)
    assert (row_widgets, "Building cache — Building chunks", False) in (
        state.panel.row_metadata_history
    )
    assert state.panel.row_action[1] == "Open"
    assert state.panel.status == (row_widgets, "Cache built", False)
    assert state.opened == []


def test_background_cache_rebuild_reports_completion_by_desktop_notification():
    notifications = []

    def notification_sender(
        desktop_services,
        notification_id,
        title,
        body,
        *,
        priority,
        platform_runtime,
    ):
        notifications.append(
            (
                desktop_services,
                notification_id,
                title,
                body,
                priority,
                platform_runtime,
            )
        )
        return True

    rebuild_controller = _FakeCacheRebuildController()
    state = _workflow(
        [],
        cache_rebuild_preflight=_enabled_cache_rebuild_preflight,
        cache_rebuild_controller=rebuild_controller,
        splash_is_foreground=lambda: False,
        notification_sender=notification_sender,
    )
    state.workflow.start_cache_rebuild(
        "/maps/Recent Cave",
        "Recent Cave",
        SimpleNamespace(row_shell=object()),
    )
    rebuild_controller.updates = [
        CacheRebuildSucceeded(
            target=_cache_rebuild_target(),
            cache_dir="/maps/Recent Cave/_cache",
        )
    ]

    state.workflow.poll_cache_rebuild()

    assert notifications == [
        (
            state.desktop_services,
            _cache_rebuild_notification_id("/maps/Recent Cave"),
            "Cache Rebuild Complete",
            "Recent Cave cache rebuilt.",
            "normal",
            None,
        )
    ]


def test_foreground_or_paused_cache_rebuild_does_not_notify():
    notifications = []

    def notification_sender(*args, **kwargs):
        notifications.append((args, kwargs))
        return True

    foreground_controller = _FakeCacheRebuildController()
    foreground = _workflow(
        [],
        cache_rebuild_preflight=_enabled_cache_rebuild_preflight,
        cache_rebuild_controller=foreground_controller,
        splash_is_foreground=lambda: True,
        notification_sender=notification_sender,
    )
    foreground.workflow.start_cache_rebuild(
        "/maps/Recent Cave",
        "Recent Cave",
        SimpleNamespace(row_shell=object()),
    )
    foreground_controller.updates = [
        CacheRebuildSucceeded(
            target=_cache_rebuild_target(),
            cache_dir="/maps/Recent Cave/_cache",
        )
    ]
    foreground.workflow.poll_cache_rebuild()

    paused_controller = _FakeCacheRebuildController()
    paused = _workflow(
        [],
        cache_rebuild_preflight=_enabled_cache_rebuild_preflight,
        cache_rebuild_controller=paused_controller,
        splash_is_foreground=lambda: False,
        notification_sender=notification_sender,
    )
    paused.workflow.start_cache_rebuild(
        "/maps/Recent Cave",
        "Recent Cave",
        SimpleNamespace(row_shell=object()),
    )
    paused_controller.updates = [
        CacheRebuildPaused(
            target=_cache_rebuild_target(),
            resume_dir="/maps/Recent Cave/.resume",
        )
    ]
    paused.workflow.poll_cache_rebuild()

    assert notifications == []


def test_background_cache_rebuild_reports_failure_by_desktop_notification():
    notifications = []

    def notification_sender(*args, **kwargs):
        notifications.append((args, kwargs))
        return True

    rebuild_controller = _FakeCacheRebuildController()
    state = _workflow(
        [],
        cache_rebuild_preflight=_enabled_cache_rebuild_preflight,
        cache_rebuild_controller=rebuild_controller,
        splash_is_foreground=lambda: False,
        notification_sender=notification_sender,
    )
    state.workflow.start_cache_rebuild(
        "/maps/Recent Cave",
        "Recent Cave",
        SimpleNamespace(row_shell=object()),
    )
    rebuild_controller.updates = [
        CacheRebuildFailed(
            target=_cache_rebuild_target(),
            error="disk full",
        )
    ]

    state.workflow.poll_cache_rebuild()

    assert notifications == [
        (
            (
                state.desktop_services,
                _cache_rebuild_notification_id("/maps/Recent Cave"),
                "Cache Rebuild Failed",
                "Couldn't rebuild Recent Cave; its existing cache was retained.",
            ),
            {"priority": "high", "platform_runtime": None},
        )
    ]


def test_disabled_rebuild_exposes_explanation_and_failure_retains_cache():
    unavailable = CapabilityResult.unavailable(
        reason_code="map_cache_rebuild_source_unavailable",
    )

    def unavailable_preflight(_path):
        return CacheRebuildPreflight(
            capability=unavailable,
            decision=decide_map_library_cache_rebuild(unavailable),
        )

    disabled_state = _workflow(
        [],
        has_cache=lambda _path: True,
        cache_rebuild_preflight=unavailable_preflight,
    )
    disabled_state.workflow.add_recent_row("/maps/Recent Cave")
    _entry, _open_map, menu_factory = disabled_state.panel.recent_row
    actions = menu_factory(SimpleNamespace(row_shell=object()))
    disabled = next(
        action
        for action in actions
        if isinstance(action, MapLibraryMenuAction)
        and action.label == "Rebuild cache"
    )

    assert disabled.action is None
    assert "source map is unavailable" in disabled.explanation.lower()

    rebuild_controller = _FakeCacheRebuildController()
    state = _workflow(
        [],
        cache_rebuild_preflight=_enabled_cache_rebuild_preflight,
        cache_rebuild_controller=rebuild_controller,
    )
    row_widgets = SimpleNamespace(row_shell=object())
    state.workflow.start_cache_rebuild(
        "/maps/Recent Cave",
        "Recent Cave",
        row_widgets,
    )
    rebuild_controller.updates = [
        CacheRebuildFailed(
            target=_cache_rebuild_target(),
            error="disk full",
        )
    ]

    state.workflow.poll_cache_rebuild()

    assert state.panel.row_action[1] == "Open"
    assert state.panel.status == (row_widgets, "Cache retained", True)
    assert "existing cache was retained" in state.feedback[-1][0].lower()


def test_close_requests_cooperative_pause_for_active_rebuild():
    rebuild_controller = _FakeCacheRebuildController()
    state = _workflow(
        [],
        cache_rebuild_preflight=_enabled_cache_rebuild_preflight,
        cache_rebuild_controller=rebuild_controller,
    )
    state.workflow.start_cache_rebuild(
        "/maps/Recent Cave",
        "Recent Cave",
        SimpleNamespace(row_shell=object()),
    )

    state.workflow.close()

    assert rebuild_controller.pause_calls == 1
