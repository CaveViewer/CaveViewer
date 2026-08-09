"""Source-neutral catalog contracts for the splash Map Library."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence


GITHUB_RELEASE_MAP_SOURCE_ID = "github-release"


@dataclass(frozen=True)
class MapCatalogRefresh:
    """One map source's ordered catalog result and its authority state."""

    source_id: str
    maps: tuple[Any, ...]
    authoritative: bool
    error: str | None = None
    display_name: str = "CaveViewer Maps"

    def __post_init__(self) -> None:
        """Reject a source adapter that would create ambiguous row ownership."""
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("Map catalog refresh must have a source id")
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise ValueError("Map catalog refresh must have a display name")
        try:
            maps = tuple(self.maps)
        except TypeError as exc:
            raise TypeError("Map catalog refresh maps must be iterable") from exc
        source_id = self.source_id.strip()
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "display_name", self.display_name.strip())
        object.__setattr__(self, "maps", maps)
        for library_map in maps:
            map_source_id = getattr(
                library_map,
                "source_id",
                GITHUB_RELEASE_MAP_SOURCE_ID,
            )
            if map_source_id != source_id:
                raise ValueError(
                    "Map source returned an entry for a different source id: "
                    f"{map_source_id!r} != {source_id!r}"
                )


class MapLibrarySource(Protocol):
    """Adapt one source-specific catalog into the Map Library contract."""

    source_id: str
    display_name: str

    def fetch_catalog(self) -> MapCatalogRefresh:
        """Fetch this source's catalog without touching Tk widgets."""


class MapLibraryCatalogService:
    """Fetch independent catalog snapshots from the enabled map sources."""

    def __init__(self, sources: Sequence[MapLibrarySource]) -> None:
        self._sources = tuple(sources)
        source_ids = [source.source_id for source in self._sources]
        if any(not source_id for source_id in source_ids):
            raise ValueError("Map Library sources must have a source id")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("Map Library source ids must be unique")

    def fetch_catalogs(self) -> tuple[MapCatalogRefresh, ...]:
        """Return a result for every source, isolating source failures."""
        results: list[MapCatalogRefresh] = []
        for source in self._sources:
            try:
                result = source.fetch_catalog()
            except Exception as exc:
                result = MapCatalogRefresh(
                    source_id=source.source_id,
                    maps=(),
                    authoritative=False,
                    error=f"Couldn't load {source.display_name}: {exc}",
                    display_name=source.display_name,
                )
            if result.source_id != source.source_id:
                raise ValueError(
                    "Map source returned a catalog for a different source id: "
                    f"{result.source_id!r} != {source.source_id!r}"
                )
            results.append(result)
        return tuple(results)


def enabled_map_library_sources() -> tuple[MapLibrarySource, ...]:
    """Return the production source adapters enabled for this application.

    The import is intentionally lazy: the GitHub adapter consumes the neutral
    contracts from this module, while this composition point lets the workflow
    stay unaware of GitHub release details.  Adding a future source is a local
    change here rather than a new workflow branch.
    """
    from caveviewer.gui.standard_library_maps import GitHubReleaseMapLibrarySource

    return (GitHubReleaseMapLibrarySource(),)


def default_map_library_catalog_service() -> MapLibraryCatalogService:
    """Build the catalog service used by the splash Map Library."""
    return MapLibraryCatalogService(enabled_map_library_sources())


def normalize_catalog_refreshes(value: Any) -> tuple[MapCatalogRefresh, ...]:
    """Normalize transitional catalog call shapes into typed source results.

    Existing injected callers returned ``(maps, error)``.  Keeping that shape
    accepted at this boundary makes the source-adapter migration incremental,
    while production code and new tests use ``MapCatalogRefresh`` directly.
    """
    if isinstance(value, MapCatalogRefresh):
        return (value,)

    if isinstance(value, tuple) and len(value) == 2:
        maps, error = value
        if error is None or isinstance(error, str):
            try:
                normalized_maps = tuple(maps)
            except TypeError as exc:
                raise TypeError("Legacy map catalog result must contain maps") from exc
            return (
                MapCatalogRefresh(
                    source_id=GITHUB_RELEASE_MAP_SOURCE_ID,
                    maps=normalized_maps,
                    authoritative=error is None,
                    error=error,
                ),
            )

    try:
        results = tuple(value)
    except TypeError as exc:
        raise TypeError("Map catalog service returned an unsupported result") from exc
    if not all(isinstance(result, MapCatalogRefresh) for result in results):
        raise TypeError("Map catalog service must return MapCatalogRefresh values")
    return results
