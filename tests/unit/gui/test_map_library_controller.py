"""Exercise splash map-library controller presentation and lifecycle state."""

from __future__ import annotations

from types import SimpleNamespace

from caveviewer.gui.map_library_controller import (
    MapLibraryController,
    StandardLibraryMapAvailability,
)
from caveviewer.gui.map_library_sources import GITHUB_RELEASE_MAP_SOURCE_ID


def _library_map(
    display_name: str = "Boh Yai Mine I",
    asset_name: str = "Boh.Yai.Mine.I.Low.Res.zip",
    size_bytes: int | None = 57 * 1024 * 1024,
    catalog_id: str | None = None,
):
    return SimpleNamespace(
        display_name=display_name,
        asset_name=asset_name,
        size_bytes=size_bytes,
        catalog_id=catalog_id,
    )


def test_standard_library_row_uses_compact_action_and_size_text():
    library_map = _library_map()
    controller = MapLibraryController([library_map])

    row = controller.row(library_map, downloaded=False)

    assert row.key == (GITHUB_RELEASE_MAP_SOURCE_ID, "Boh Yai Mine I")
    assert row.title == "Boh Yai Mine I"
    assert row.detail == "57 MB"
    assert row.action_text == "Get"
    assert not row.downloaded


def test_standard_library_row_uses_catalog_id_when_available():
    library_map = _library_map(catalog_id="boh-yai-mine-i")
    controller = MapLibraryController([library_map])

    row = controller.row(library_map, downloaded=False)

    assert row.key == (GITHUB_RELEASE_MAP_SOURCE_ID, "boh-yai-mine-i")


def test_downloaded_standard_library_row_remembers_open_path():
    library_map = _library_map()
    controller = MapLibraryController([library_map])

    row = controller.row(
        library_map,
        downloaded=True,
        result_path="/maps/Boh Yai Mine I",
    )

    assert row.detail == "Downloaded"
    assert row.action_text == "Open"
    assert controller.downloaded_path(
        library_map,
        is_downloaded=False,
        existing_path=None,
    ) == "/maps/Boh Yai Mine I"


def test_cave_metadata_detail_replaces_ordinary_download_state_text():
    library_map = _library_map(display_name="Devils Eye")
    controller = MapLibraryController([library_map])

    row = controller.row(
        library_map,
        downloaded=True,
        cave_metadata_detail="Florida, United States · Underwater cave",
    )

    assert row.detail == "Florida, United States · Underwater cave"


def test_former_map_warning_still_takes_priority_over_cave_metadata():
    library_map = _library_map(display_name="Devils Eye")
    controller = MapLibraryController([library_map])
    key = controller.map_key(library_map)
    controller.replace_standard_library_maps(
        [library_map],
        availability_by_key={
            key: StandardLibraryMapAvailability.FORMER_STANDARD_LOCAL,
        },
    )

    row = controller.row(
        library_map,
        downloaded=True,
        cave_metadata_detail="Florida, United States · Underwater cave",
    )

    assert row.detail == "No longer a part of the standard library"


def test_former_downloaded_map_keeps_its_open_action():
    library_map = _library_map()
    controller = MapLibraryController([library_map])
    key = controller.map_key(library_map)
    controller.replace_standard_library_maps(
        [library_map],
        availability_by_key={
            key: StandardLibraryMapAvailability.FORMER_STANDARD_LOCAL,
        },
    )

    row = controller.row(
        library_map,
        downloaded=True,
        result_path="/maps/Boh Yai Mine I",
    )

    assert row.detail == "No longer a part of the standard library"
    assert row.action_text == "Open"
    assert row.enabled


def test_catalog_refresh_updates_standard_library_size_metadata():
    library_map = _library_map(
        display_name="Custom Cave",
        asset_name="custom.zip",
    )
    catalog_entry = _library_map(
        display_name="Custom Cave",
        asset_name="custom.zip",
        size_bytes=52 * 1024 * 1024,
    )
    controller = MapLibraryController([library_map])

    completion = controller.complete_catalog_fetch([catalog_entry], error=None)
    row = controller.row(library_map, downloaded=False)

    assert completion.maps == (catalog_entry,)
    assert completion.error is None
    assert row.detail == "52 MB"


def test_source_qualified_keys_do_not_collide_for_matching_catalog_ids():
    official_map = _library_map(catalog_id="shared")
    partner_map = _library_map(catalog_id="shared")
    partner_map.source_id = "partner-library"
    controller = MapLibraryController([official_map, partner_map])

    assert controller.map_key(official_map) == (GITHUB_RELEASE_MAP_SOURCE_ID, "shared")
    assert controller.map_key(partner_map) == ("partner-library", "shared")
    assert len(controller.catalog_by_key) == 2


def test_active_download_cleanup_returns_tk_owned_handles():
    library_map = _library_map()
    controller = MapLibraryController([library_map])
    cancel_event = object()
    inhibitor = object()
    after_id = "after#1"

    controller.begin_download(
        library_map,
        cancel_event=cancel_event,
        inhibitor=inhibitor,
    )
    controller.set_download_after_id(after_id)

    assert controller.active_download.in_progress
    assert controller.should_handle_download_poll(cancel_event)

    cleanup = controller.close_active_download()

    assert cleanup.cancel_event is cancel_event
    assert cleanup.after_id == after_id
    assert cleanup.inhibitor is inhibitor
    assert not controller.active_download.in_progress
