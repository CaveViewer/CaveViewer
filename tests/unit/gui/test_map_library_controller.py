"""Exercise splash map-library controller presentation and lifecycle state."""

from __future__ import annotations

from types import SimpleNamespace

from caveviewer.gui.map_library_controller import MapLibraryController


def _library_map(
    display_name: str = "Boh Yai Mine I",
    asset_name: str = "Boh.Yai.Mine.I.Low.Res.zip",
    size_bytes: int | None = None,
):
    return SimpleNamespace(
        display_name=display_name,
        asset_name=asset_name,
        size_bytes=size_bytes,
    )


def test_standard_library_row_uses_compact_action_and_size_text():
    library_map = _library_map()
    controller = MapLibraryController([library_map])

    row = controller.row(library_map, downloaded=False)

    assert row.key == "Boh Yai Mine I"
    assert row.title == "Boh Yai Mine I"
    assert row.detail == "57 MB"
    assert row.action_text == "Get"
    assert not row.downloaded


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
