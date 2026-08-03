"""Exercise splash Map Library workflow state without constructing Tk widgets."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from caveviewer.gui.map_library_controller import MapLibraryController
from caveviewer.gui.map_library import recent_map_key
from caveviewer.gui.map_library_workflow import (
    MapLibraryWorkflow,
    _remaining_cache_error,
)
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
    ) -> bool:
        if key not in self.standard_rows:
            return False
        self.standard_actions[key] = (text, command, enabled, show_stop_progress)
        return True

    def set_standard_row_metadata(
        self,
        key: str,
        text: str,
        *,
        error: bool = False,
    ) -> None:
        self.metadata[key] = (text, error)

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


class _FakeInhibitor:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


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
    has_recorded_dive=None,
    open_recorded_dive=None,
):
    root = _FakeRoot()
    panel = _FakePanel()
    controller = MapLibraryController(maps)
    feedback = []
    closed_inhibitors = []
    opened = []
    inhibitor = _FakeInhibitor()

    workflow = MapLibraryWorkflow(
        root=root,
        controller=controller,
        panel=panel,
        standard_library_maps=maps,
        map_library_root_dir="/library",
        desktop_services=object(),
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
        has_recorded_dive=has_recorded_dive or (lambda _path: False),
        open_recorded_dive=open_recorded_dive,
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


def test_downloaded_standard_library_menu_omits_cache_action_without_cache():
    """Downloaded standard maps without generated cache expose one cleanup action."""
    library_map = _library_map()
    state = _workflow(
        [library_map],
        is_downloaded=lambda _root, _map: True,
        existing_path=lambda _root, _map: "/library/Test Cave",
        has_cache=lambda _path: False,
    )

    state.workflow.add_standard_row(library_map)
    menu_factory = state.panel.standard_menu_factories["Test Cave"]

    actions = menu_factory(SimpleNamespace(row_shell=object()))

    assert [label for label, _command in actions] == ["Remove downloaded maps"]


def test_downloaded_standard_library_menu_offers_local_recorded_dive():
    library_map = _library_map()
    opened_recorded_dive = []
    state = _workflow(
        [library_map],
        is_downloaded=lambda _root, _map: True,
        existing_path=lambda _root, _map: "/library/Test Cave",
        has_recorded_dive=lambda path: path == "/library/Test Cave",
        open_recorded_dive=opened_recorded_dive.append,
    )

    state.workflow.add_standard_row(library_map)
    menu_factory = state.panel.standard_menu_factories["Test Cave"]
    actions = menu_factory(SimpleNamespace(row_shell=object()))

    assert [label for label, _command in actions] == [
        "Play recorded dive…",
        "Remove downloaded maps",
    ]

    actions[0][1]()

    assert opened_recorded_dive == ["/library/Test Cave"]


def test_recent_map_menu_offers_local_recorded_dive():
    opened_recorded_dive = []
    state = _workflow(
        [],
        has_recorded_dive=lambda path: path == "/maps/Recent Cave",
        open_recorded_dive=opened_recorded_dive.append,
    )

    state.workflow.add_recent_row("/maps/Recent Cave")
    _entry, _open_map, menu_factory = state.panel.recent_row
    actions = menu_factory(SimpleNamespace(row_shell=object()))

    assert [label for label, _command in actions] == [
        "Play recorded dive…",
        "Remove from this list",
    ]

    actions[0][1]()

    assert opened_recorded_dive == ["/maps/Recent Cave"]


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
        "Remove downloaded maps",
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
