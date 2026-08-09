"""Cover source-neutral Map Library catalog composition contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caveviewer.gui.map_library_sources import (
    GITHUB_RELEASE_MAP_SOURCE_ID,
    MapCatalogRefresh,
    MapLibraryCatalogService,
    normalize_catalog_refreshes,
)


class _Source:
    def __init__(self, source_id: str, result) -> None:
        self.source_id = source_id
        self.display_name = source_id.title()
        self._result = result

    def fetch_catalog(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_catalog_service_keeps_source_results_independent_and_ordered():
    official_map = SimpleNamespace(source_id="official", catalog_id="same-id")
    partner_map = SimpleNamespace(source_id="partner-two", catalog_id="same-id")
    service = MapLibraryCatalogService(
        (
            _Source(
                "official",
                MapCatalogRefresh(
                    source_id="official",
                    maps=(official_map,),
                    authoritative=True,
                ),
            ),
            _Source("partner", RuntimeError("offline")),
            _Source(
                "partner-two",
                MapCatalogRefresh(
                    source_id="partner-two",
                    maps=(partner_map,),
                    authoritative=True,
                ),
            ),
        )
    )

    refreshes = service.fetch_catalogs()

    assert [refresh.source_id for refresh in refreshes] == [
        "official",
        "partner",
        "partner-two",
    ]
    assert refreshes[0].authoritative
    assert not refreshes[1].authoritative
    assert "offline" in (refreshes[1].error or "")
    assert refreshes[2].maps == (partner_map,)


def test_catalog_service_rejects_duplicate_source_ids():
    source = _Source(
        "official",
        MapCatalogRefresh(source_id="official", maps=(), authoritative=True),
    )

    with pytest.raises(ValueError, match="unique"):
        MapLibraryCatalogService((source, source))


def test_legacy_catalog_tuple_normalizes_to_non_authoritative_fallback_on_error():
    map_entry = SimpleNamespace(catalog_id="cached")

    refreshes = normalize_catalog_refreshes(([map_entry], "offline"))

    assert refreshes == (
        MapCatalogRefresh(
            source_id=GITHUB_RELEASE_MAP_SOURCE_ID,
            maps=(map_entry,),
            authoritative=False,
            error="offline",
        ),
    )
