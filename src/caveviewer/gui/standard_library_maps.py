"""Standard-library map source, storage, and download helpers.

CaveViewer keeps standard-library maps as release assets instead of bundling
them with the app. The map assets can be large, and keeping them as
on-demand downloads keeps the base install small for people who already have
their own maps.

This module fetches release metadata, resolves local map-library paths, and
downloads/extracts a selected standard-library map archive. It has no Tk UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from caveviewer.core.json_io import load_bounded_json
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.preference_paths import write_text_atomic
from caveviewer.gui.update_checker import (
    DownloadCancelled,
    download_update,
    make_ssl_context,
)
from caveviewer.resources import resource_path
from caveviewer.storage_paths import resolve_application_paths


_LOG = get_logger("StandardLibraryMaps")


# Configuration for the standard-library map repository.
# Map library assets are hosted by default at:
# https://github.com/CaveViewer/CaveViewer/releases/tag/sample-data
_DEFAULT_MAP_LIBRARY_REPO = "CaveViewer/CaveViewer"
_DEFAULT_MAP_LIBRARY_RELEASE_TAG = "sample-data"
_DEFAULT_MAP_LIBRARY_CATALOG_ASSET_NAME = "caveviewer-map-library.v1.json"
_BUNDLED_MAP_LIBRARY_CATALOG_RESOURCE = "map_library_catalog.v1.json"
_MAP_LIBRARY_CATALOG_CACHE_FILE = "map_library_catalog.v1.json"
_MAX_RELEASE_METADATA_BYTES = 2 * 1024 * 1024
_MAX_MAP_LIBRARY_CATALOG_BYTES = 128 * 1024


def _env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_alias_or_default(primary_name: str, legacy_name: str, default: str) -> str:
    """Return a renamed environment override while preserving legacy aliases."""
    primary_value = os.environ.get(primary_name, "").strip()
    if primary_value:
        return primary_value
    return _env_or_default(legacy_name, default)


_MAP_LIBRARY_REPO = _env_alias_or_default(
    "CAVEVIEWER_MAP_LIBRARY_REPO",
    "CAVEVIEWER_SAMPLE_MAPS_REPO",
    _DEFAULT_MAP_LIBRARY_REPO,
)
_MAP_LIBRARY_RELEASE_TAG = _env_alias_or_default(
    "CAVEVIEWER_MAP_LIBRARY_RELEASE_TAG",
    "CAVEVIEWER_SAMPLE_DATA_TAG",
    _DEFAULT_MAP_LIBRARY_RELEASE_TAG,
)
_TAGGED_RELEASE_API_URL = _env_alias_or_default(
    "CAVEVIEWER_MAP_LIBRARY_API_URL",
    "CAVEVIEWER_SAMPLE_MAPS_API_URL",
    f"https://api.github.com/repos/{_MAP_LIBRARY_REPO}/releases/tags/{_MAP_LIBRARY_RELEASE_TAG}",
)
_MAP_LIBRARY_CATALOG_ASSET_NAME = _env_or_default(
    "CAVEVIEWER_MAP_LIBRARY_CATALOG_ASSET_NAME",
    _DEFAULT_MAP_LIBRARY_CATALOG_ASSET_NAME,
)
_REQUEST_TIMEOUT_SECONDS = 8

MAP_LIBRARY_DIRNAME = "map_library"
_LEGACY_MAP_LIBRARY_DIRNAME = "sample_maps"

_MAP_LIBRARY_CONFIG_LOGGED = False


@dataclass
class StandardLibraryMapInfo:
    display_name: str
    asset_name: str
    download_url: Optional[str] = None
    size_bytes: Optional[int] = None
    catalog_id: Optional[str] = None
    folder_name: Optional[str] = None
    sha256: Optional[str] = None


@dataclass(frozen=True)
class StandardLibraryMapRemovalResult:
    """Result of removing app-managed downloaded files for one map-library entry."""

    removed_paths: tuple[str, ...]
    error: str | None = None


def bundled_standard_library_catalog() -> list[StandardLibraryMapInfo]:
    """Return the package-bundled fallback map catalog."""
    catalog_path = resource_path(_BUNDLED_MAP_LIBRARY_CATALOG_RESOURCE)
    payload = load_bounded_json(
        catalog_path,
        max_bytes=_MAX_MAP_LIBRARY_CATALOG_BYTES,
        description="bundled map library catalog",
    )
    return _standard_library_maps_from_catalog_payload(
        payload,
        source_description="bundled map library catalog",
    )


def load_initial_standard_library_catalog() -> list[StandardLibraryMapInfo]:
    """Return the best local catalog for initial splash rendering."""
    cached_catalog = _load_cached_standard_library_catalog()
    if cached_catalog:
        return cached_catalog
    return bundled_standard_library_catalog()


def default_map_library_install_dir() -> str:
    """Return the app-managed default root for first-time map-library downloads."""
    data_dir = resolve_application_paths().data_dir
    map_library_dir = data_dir / MAP_LIBRARY_DIRNAME
    legacy_map_library_dir = data_dir / _LEGACY_MAP_LIBRARY_DIRNAME

    if (
        legacy_map_library_dir.is_dir()
        and map_library_dir.exists()
        and not map_library_dir.is_dir()
    ):
        _LOG.warning(
            "Could not use map library path %s because it is not a directory; "
            "using legacy map-library directory %s",
            map_library_dir,
            legacy_map_library_dir,
        )
        return str(legacy_map_library_dir)

    try:
        map_library_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOG.warning(
            "Could not create default map library directory %s: %s",
            map_library_dir,
            exc,
        )
    if map_library_dir.is_dir():
        _move_legacy_map_library_contents(legacy_map_library_dir, map_library_dir)
        return str(map_library_dir)

    # If a conflicting file or permissions problem blocks the dedicated
    # map_library/ folder, fall back to the XDG data root. The normal
    # download helper will still create map_library/<map> under this root.
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(data_dir)


def _move_legacy_map_library_contents(legacy_dir: Path, map_library_dir: Path) -> None:
    """Move legacy app-managed sample_maps/ contents into map_library/."""
    if not legacy_dir.is_dir() or legacy_dir == map_library_dir:
        return

    try:
        entries = list(legacy_dir.iterdir())
    except OSError as exc:
        _LOG.warning(
            "Could not inspect legacy map-library directory %s: %s",
            legacy_dir,
            exc,
        )
        return

    for entry in entries:
        _move_legacy_map_library_entry(entry, map_library_dir / entry.name)

    try:
        legacy_dir.rmdir()
        _LOG.info("Removed empty legacy map-library directory %s", legacy_dir)
    except OSError:
        _LOG.warning(
            "Legacy map-library directory %s still contains items that were not moved",
            legacy_dir,
        )


def _move_legacy_map_library_entry(source: Path, destination: Path) -> None:
    if not destination.exists():
        try:
            source.rename(destination)
            _LOG.info("Moved legacy map-library item %s to %s", source, destination)
        except OSError as exc:
            _LOG.warning("Could not move legacy map-library item %s: %s", source, exc)
        return

    if source.is_dir() and destination.is_dir() and not source.is_symlink():
        _merge_legacy_map_library_directory(source, destination)
        return

    _LOG.warning(
        "Keeping legacy map-library item %s because %s already exists",
        source,
        destination,
    )


def _merge_legacy_map_library_directory(source: Path, destination: Path) -> None:
    try:
        entries = list(source.iterdir())
    except OSError as exc:
        _LOG.warning(
            "Could not inspect legacy map-library directory %s: %s",
            source,
            exc,
        )
        return

    for entry in entries:
        _move_legacy_map_library_entry(entry, destination / entry.name)

    try:
        source.rmdir()
    except OSError:
        _LOG.warning(
            "Keeping legacy map-library directory %s because it still contains conflicts",
            source,
        )


def _catalog_cache_path() -> Path:
    """Return the last-successful remote catalog cache file."""
    return resolve_application_paths().cache_dir / _MAP_LIBRARY_CATALOG_CACHE_FILE


def _load_cached_standard_library_catalog() -> list[StandardLibraryMapInfo]:
    """Return cached remote map metadata, or an empty list when unavailable."""
    cache_path = _catalog_cache_path()
    if not cache_path.is_file():
        return []
    try:
        payload = load_bounded_json(
            cache_path,
            max_bytes=_MAX_MAP_LIBRARY_CATALOG_BYTES,
            description="cached map library catalog",
        )
        return _standard_library_maps_from_catalog_payload(
            payload,
            source_description="cached map library catalog",
        )
    except Exception as exc:
        _LOG.warning("Ignoring unreadable map library catalog cache %s: %s", cache_path, exc)
        return []


def _save_cached_standard_library_catalog(
    maps: list[StandardLibraryMapInfo],
) -> None:
    """Persist remote map metadata without temporary download URLs."""
    cache_path = _catalog_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(
            str(cache_path),
            json.dumps(
                _catalog_payload_from_standard_library_maps(maps),
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
    except Exception as exc:
        _LOG.warning("Could not cache map library catalog %s: %s", cache_path, exc)


def _maps_without_download_info(
    maps: list[StandardLibraryMapInfo],
) -> list[StandardLibraryMapInfo]:
    """Return local catalog entries with network-only URLs removed."""
    return [
        StandardLibraryMapInfo(
            display_name=library_map.display_name,
            asset_name=library_map.asset_name,
            size_bytes=library_map.size_bytes,
            catalog_id=library_map.catalog_id,
            folder_name=library_map.folder_name,
            sha256=library_map.sha256,
        )
        for library_map in maps
    ]


def _fallback_maps_with_no_download_info() -> list[StandardLibraryMapInfo]:
    """
    Return the best local catalog for network failures.

    A failed GitHub fetch should not hide maps that were bundled with the app
    or discovered during an earlier successful remote catalog refresh. The UI
    checks local disk independently, so already-downloaded entries can still
    open while offline.
    """
    return _maps_without_download_info(load_initial_standard_library_catalog())


def _read_json_url(url: str, *, accept: str, max_bytes: int):
    """Fetch a bounded UTF-8 JSON document from a trusted configured URL."""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "CaveViewer-MapLibrary",
        },
    )
    with urllib.request.urlopen(
        request,
        timeout=_REQUEST_TIMEOUT_SECONDS,
        context=make_ssl_context(),
    ) as response:
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"map library response exceeded {max_bytes} bytes")
    return json.loads(payload.decode("utf-8"))


def _release_assets_by_name(release_payload) -> dict[str, dict]:
    """Return valid GitHub release assets keyed by filename."""
    assets = release_payload.get("assets", [])
    if not isinstance(assets, list):
        raise TypeError("release assets must be a list")
    results: dict[str, dict] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if isinstance(name, str) and name:
            results[name] = asset
    return results


def _catalog_payload_from_standard_library_maps(
    maps: list[StandardLibraryMapInfo],
) -> dict:
    """Serialize map metadata for the local cache using the public v1 shape."""
    entries = []
    for index, library_map in enumerate(maps):
        entry = {
            "id": library_map.catalog_id or _catalog_id_from_asset(library_map),
            "title": library_map.display_name,
            "asset": library_map.asset_name,
            "sort": index * 10,
        }
        if library_map.folder_name and library_map.folder_name != library_map.display_name:
            entry["folder"] = library_map.folder_name
        if library_map.size_bytes is not None:
            entry["size_bytes"] = library_map.size_bytes
        if library_map.sha256:
            entry["sha256"] = library_map.sha256
        entries.append(entry)
    return {"version": 1, "maps": entries}


def _map_catalog_from_release_zip_assets(
    assets_by_name: dict[str, dict],
) -> list[StandardLibraryMapInfo]:
    """
    Build a compatible catalog when a release has map zips but no manifest.

    Bundled metadata preserves the established titles/order for known release
    assets. Additional zip assets are inferred from their filenames so adding a
    new map zip to the current GitHub release still makes it visible before a
    full manifest asset is published.
    """
    catalog = bundled_standard_library_catalog()
    seen_assets = {library_map.asset_name for library_map in catalog}
    extra_assets = sorted(
        asset_name
        for asset_name in assets_by_name
        if _is_map_archive_asset_name(asset_name) and asset_name not in seen_assets
    )
    catalog.extend(
        StandardLibraryMapInfo(
            display_name=_display_name_from_map_asset_name(asset_name),
            asset_name=asset_name,
            catalog_id=_catalog_id_from_asset_name(asset_name),
        )
        for asset_name in extra_assets
    )
    return catalog


def _is_map_archive_asset_name(asset_name: str) -> bool:
    """Return whether a GitHub release asset should be treated as a map zip."""
    return asset_name.lower().endswith(".zip")


def _display_name_from_map_asset_name(asset_name: str) -> str:
    """Infer a readable title for manifest-less map zip assets."""
    stem = asset_name[:-4] if asset_name.lower().endswith(".zip") else asset_name
    words = stem.replace("_", " ").replace("-", " ").replace(".", " ").split()
    return " ".join(words) or stem or asset_name


def _catalog_id_from_asset(library_map: StandardLibraryMapInfo) -> str:
    """Return a stable fallback ID when older metadata has no explicit ID."""
    return _catalog_id_from_asset_name(
        library_map.asset_name or library_map.display_name
    )


def _catalog_id_from_asset_name(asset_name: str) -> str:
    """Return a stable ID derived from an asset filename or display value."""
    raw_value = asset_name[:-4] if asset_name.lower().endswith(".zip") else asset_name
    return "".join(
        char.lower() if char.isalnum() else "-"
        for char in raw_value
    ).strip("-") or "standard-library-map"


def _standard_library_maps_from_catalog_payload(
    payload,
    *,
    source_description: str,
) -> list[StandardLibraryMapInfo]:
    """Validate the v1 catalog manifest and return ordered map entries."""
    if not isinstance(payload, dict):
        raise ValueError(f"{source_description} must be a JSON object")
    if payload.get("version") != 1:
        raise ValueError(f"{source_description} must declare version 1")
    raw_maps = payload.get("maps")
    if not isinstance(raw_maps, list):
        raise ValueError(f"{source_description} maps must be a list")

    parsed: list[tuple[int, int, StandardLibraryMapInfo]] = []
    seen_ids: set[str] = set()
    seen_assets: set[str] = set()
    for index, raw_map in enumerate(raw_maps):
        if not isinstance(raw_map, dict):
            raise ValueError(f"{source_description} map #{index + 1} must be an object")
        catalog_id = _required_catalog_string(
            raw_map,
            "id",
            source_description=source_description,
            index=index,
        )
        title = _required_catalog_string(
            raw_map,
            "title",
            source_description=source_description,
            index=index,
        )
        asset = _required_catalog_string(
            raw_map,
            "asset",
            source_description=source_description,
            index=index,
        )
        folder = _optional_catalog_string(raw_map, "folder")
        sha256 = _optional_sha256(raw_map, source_description, index)
        size_bytes = _optional_nonnegative_int(
            raw_map,
            "size_bytes",
            source_description=source_description,
            index=index,
        )
        sort_order = _optional_nonnegative_int(
            raw_map,
            "sort",
            source_description=source_description,
            index=index,
        )
        if catalog_id in seen_ids:
            raise ValueError(f"{source_description} has duplicate map id {catalog_id!r}")
        if asset in seen_assets:
            raise ValueError(f"{source_description} has duplicate asset {asset!r}")
        seen_ids.add(catalog_id)
        seen_assets.add(asset)
        parsed.append(
            (
                index if sort_order is None else sort_order,
                index,
                StandardLibraryMapInfo(
                    display_name=title,
                    asset_name=asset,
                    size_bytes=size_bytes,
                    catalog_id=catalog_id,
                    folder_name=folder,
                    sha256=sha256,
                ),
            )
        )
    return [item[2] for item in sorted(parsed, key=lambda item: (item[0], item[1]))]


def _required_catalog_string(
    raw_map: dict,
    field_name: str,
    *,
    source_description: str,
    index: int,
) -> str:
    value = raw_map.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{source_description} map #{index + 1} has invalid {field_name!r}"
        )
    return value.strip()


def _optional_catalog_string(raw_map: dict, field_name: str) -> str | None:
    value = raw_map.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _optional_nonnegative_int(
    raw_map: dict,
    field_name: str,
    *,
    source_description: str,
    index: int,
) -> int | None:
    value = raw_map.get(field_name)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(
            f"{source_description} map #{index + 1} has invalid {field_name!r}"
        )
    return value


def _optional_sha256(
    raw_map: dict,
    source_description: str,
    index: int,
) -> str | None:
    value = raw_map.get("sha256")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"{source_description} map #{index + 1} has invalid 'sha256'"
        )
    cleaned = value.strip().lower()
    if len(cleaned) != 64 or any(char not in "0123456789abcdef" for char in cleaned):
        raise ValueError(
            f"{source_description} map #{index + 1} has invalid 'sha256'"
        )
    return cleaned


def _enrich_catalog_with_release_assets(
    catalog: list[StandardLibraryMapInfo],
    assets_by_name: dict[str, dict],
) -> list[StandardLibraryMapInfo]:
    """Join catalog metadata to GitHub release asset URLs and sizes."""
    results = []
    for library_map in catalog:
        asset = assets_by_name.get(library_map.asset_name)
        results.append(
            StandardLibraryMapInfo(
                display_name=library_map.display_name,
                asset_name=library_map.asset_name,
                download_url=(
                    asset.get("browser_download_url")
                    if isinstance(asset, dict)
                    else None
                ),
                size_bytes=(
                    asset.get("size")
                    if isinstance(asset, dict) and isinstance(asset.get("size"), int)
                    else library_map.size_bytes
                ),
                catalog_id=library_map.catalog_id,
                folder_name=library_map.folder_name,
                sha256=library_map.sha256,
            )
        )
    return results


def _remote_catalog_from_release_assets(
    assets_by_name: dict[str, dict],
) -> tuple[list[StandardLibraryMapInfo], str | None, bool]:
    """
    Return catalog entries, an optional warning, and whether remote metadata won.

    The release manifest controls the dynamic list when present. Until that
    asset is published, CaveViewer preserves bundled metadata for known zips
    and infers rows for additional release zip assets.
    """
    catalog_asset = assets_by_name.get(_MAP_LIBRARY_CATALOG_ASSET_NAME)
    catalog_url = (
        catalog_asset.get("browser_download_url")
        if isinstance(catalog_asset, dict)
        else None
    )
    if not catalog_url:
        return _map_catalog_from_release_zip_assets(assets_by_name), None, True

    try:
        payload = _read_json_url(
            catalog_url,
            accept="application/json",
            max_bytes=_MAX_MAP_LIBRARY_CATALOG_BYTES,
        )
        return (
            _standard_library_maps_from_catalog_payload(
                payload,
                source_description="remote map library catalog",
            ),
            None,
            True,
        )
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        urllib.error.URLError,
        ValueError,
        TypeError,
    ) as exc:
        return (
            load_initial_standard_library_catalog(),
            f"Got an unexpected map library catalog: {exc}",
            False,
        )


def fetch_standard_library_catalog() -> tuple[list[StandardLibraryMapInfo], str | None]:
    """
    Fetch the GitHub-hosted map catalog and release asset download details.

    Returns ``(maps, error_message_or_None)``. A remote catalog manifest named
    ``caveviewer-map-library.v1.json`` controls the dynamic map list when it is
    attached to the configured release. If the manifest is absent, CaveViewer
    preserves bundled metadata for known zip assets, infers rows for additional
    release zip assets, and fills download URLs from the release assets. If
    GitHub cannot be reached, CaveViewer returns cached or bundled catalog
    metadata without download URLs so previously downloaded maps remain visible
    and openable.
    """
    _log_map_library_config_once()

    try:
        data = _read_json_url(
            _TAGGED_RELEASE_API_URL,
            accept="application/vnd.github+json",
            max_bytes=_MAX_RELEASE_METADATA_BYTES,
        )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            error_msg = (
                f"No map library release found for tag {_MAP_LIBRARY_RELEASE_TAG!r}."
            )
        else:
            error_msg = f"GitHub returned an error (HTTP {e.code})."
        return _fallback_maps_with_no_download_info(), error_msg
    except urllib.error.URLError as e:
        # URLError is a catch-all for "the request itself failed to
        # complete" -- it does NOT specifically mean "no internet
        # connection," even though that's the most common cause. It can
        # also fire from DNS resolution hiccups, a VPN/proxy/firewall
        # interfering, GitHub being briefly unreachable, or a connection
        # that timed out despite eventually working on a retry. Blaming
        # "check your internet connection" unconditionally is sometimes
        # simply wrong, and unhelpful to someone who genuinely IS online
        # -- surfacing the real underlying reason (e.reason) when
        # available gives them and any future debugging far more to go
        # on than a generic, possibly-incorrect accusation.
        reason = getattr(e, "reason", None)
        if reason:
            error_msg = (
                f"Couldn't reach GitHub ({reason}). This may be a temporary "
                "network issue -- try again in a moment."
            )
        else:
            error_msg = (
                "Couldn't reach GitHub right now. This may be a temporary "
                "network issue -- try again in a moment."
            )
        return _fallback_maps_with_no_download_info(), error_msg
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, KeyError, TypeError) as e:
        return (
            _fallback_maps_with_no_download_info(),
            f"Got an unexpected response from GitHub: {e}",
        )

    try:
        assets_by_name = _release_assets_by_name(data)
    except TypeError as exc:
        return (
            _fallback_maps_with_no_download_info(),
            f"Got an unexpected response from GitHub: {exc}",
        )
    catalog, catalog_error, remote_manifest_loaded = _remote_catalog_from_release_assets(
        assets_by_name
    )
    results = _enrich_catalog_with_release_assets(catalog, assets_by_name)
    if remote_manifest_loaded:
        _save_cached_standard_library_catalog(results)
    return results, catalog_error


def _log_map_library_config_once() -> None:
    global _MAP_LIBRARY_CONFIG_LOGGED
    if _MAP_LIBRARY_CONFIG_LOGGED:
        return

    _MAP_LIBRARY_CONFIG_LOGGED = True
    _LOG.info(
        "Map library source env: CAVEVIEWER_MAP_LIBRARY_API_URL=%r, "
        "CAVEVIEWER_MAP_LIBRARY_REPO=%r, "
        "CAVEVIEWER_MAP_LIBRARY_RELEASE_TAG=%r, "
        "CAVEVIEWER_MAP_LIBRARY_CATALOG_ASSET_NAME=%r",
        os.environ.get("CAVEVIEWER_MAP_LIBRARY_API_URL"),
        os.environ.get("CAVEVIEWER_MAP_LIBRARY_REPO"),
        os.environ.get("CAVEVIEWER_MAP_LIBRARY_RELEASE_TAG"),
        os.environ.get("CAVEVIEWER_MAP_LIBRARY_CATALOG_ASSET_NAME"),
    )
    _LOG.info(
        "Map library source legacy env: CAVEVIEWER_SAMPLE_MAPS_API_URL=%r, "
        "CAVEVIEWER_SAMPLE_MAPS_REPO=%r, CAVEVIEWER_SAMPLE_DATA_TAG=%r",
        os.environ.get("CAVEVIEWER_SAMPLE_MAPS_API_URL"),
        os.environ.get("CAVEVIEWER_SAMPLE_MAPS_REPO"),
        os.environ.get("CAVEVIEWER_SAMPLE_DATA_TAG"),
    )
    _LOG.info(
        "Map library source resolved: api_url=%r, repo=%r, tag=%r, catalog=%r",
        _TAGGED_RELEASE_API_URL,
        _MAP_LIBRARY_REPO,
        _MAP_LIBRARY_RELEASE_TAG,
        _MAP_LIBRARY_CATALOG_ASSET_NAME,
    )


def _install_dir_path(install_dir) -> str:
    """
    Normalize a map-library install root to a filesystem path string.

    Linux portal choosers return DirectorySelection objects. UI code should pass
    their `.path`, but accepting path-like selection objects here keeps the
    non-UI download helper robust when portal and Tk fallback paths differ.
    """
    return os.fspath(getattr(install_dir, "path", install_dir))


def local_standard_library_map_path(install_dir: str, sample: StandardLibraryMapInfo) -> str:
    """
    Where a given standard-library map would live locally once downloaded --
    one subfolder per map, named after its display name (so it reads
    clearly in a file browser), inside the shared map_library folder.
    """
    return os.path.join(
        _map_library_container_dir(install_dir),
        sample.folder_name or sample.display_name,
    )


def _map_library_container_dir(install_dir: str) -> str:
    """
    Return the folder that should directly contain individual map-library entries.

    The dialog asks where to save map-library entries, and CaveViewer normally
    creates a shared map_library/ folder inside that selected directory. If
    the user already chooses a folder named map_library, treat that folder
    itself as the container instead of creating map_library/map_library/... .
    Legacy sample_maps/ selections are also treated as existing containers so
    older custom locations remain usable.
    """
    normalized = os.path.normpath(_install_dir_path(install_dir))
    basename = os.path.basename(normalized).lower()
    if basename in {MAP_LIBRARY_DIRNAME.lower(), _LEGACY_MAP_LIBRARY_DIRNAME.lower()}:
        return normalized
    return os.path.join(normalized, MAP_LIBRARY_DIRNAME)


def is_standard_library_map_downloaded(install_dir: str, sample: StandardLibraryMapInfo) -> bool:
    """
    True if this standard-library map's local folder already exists and has
    something in it -- used so the dialog can offer "Open" instead of
    "Download" for a map that's already been fetched, rather than
    re-downloading tens to hundreds of MB unnecessarily every time.
    """
    path = local_standard_library_map_path(install_dir, sample)
    return _folder_has_contents(path) or any(
        _folder_has_contents(legacy_path)
        for legacy_path in _legacy_standard_library_map_paths(install_dir, sample)
    )


def existing_standard_library_map_path(install_dir: str, sample: StandardLibraryMapInfo) -> str:
    """Return the actual existing local path for a standard-library map."""
    path = local_standard_library_map_path(install_dir, sample)
    if _folder_has_contents(path):
        return path
    for legacy_path in _legacy_standard_library_map_paths(install_dir, sample):
        if _folder_has_contents(legacy_path):
            return legacy_path
    return path


def remove_downloaded_standard_library_map(
    install_dir: str | os.PathLike[str],
    sample: StandardLibraryMapInfo,
) -> StandardLibraryMapRemovalResult:
    """Remove app-managed downloaded files for one bundled CaveViewer map."""
    removed_paths: list[str] = []
    errors: list[str] = []

    for candidate in _standard_library_map_removal_candidates(install_dir, sample):
        try:
            if not os.path.lexists(candidate):
                continue
            if os.path.islink(candidate) or not os.path.isdir(candidate):
                errors.append(f"{candidate} is not a removable directory")
                continue
            shutil.rmtree(candidate)
            removed_paths.append(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")

    return StandardLibraryMapRemovalResult(
        removed_paths=tuple(removed_paths),
        error="; ".join(errors) if errors else None,
    )


def is_app_supplied_standard_library_map_path(
    path: str | os.PathLike[str], install_dir: str | os.PathLike[str] | None = None
) -> bool:
    """
    Return whether `path` is one of CaveViewer's known app-library maps.

    Recent maps should reflect user-opened maps, not the curated map-library maps
    already listed in the Map Library's CaveViewer Maps section.  Compare exact
    managed-library and legacy folder locations so similarly named
    user maps elsewhere on disk still remain eligible for history.
    """
    normalized = _normalized_path_for_compare(path)
    if normalized is None:
        return False

    for candidate in _app_supplied_standard_library_map_path_candidates(install_dir):
        candidate_normalized = _normalized_path_for_compare(candidate)
        if candidate_normalized is not None and normalized == candidate_normalized:
            return True
    return False


def _standard_library_map_removal_candidates(
    install_dir: str | os.PathLike[str],
    sample: StandardLibraryMapInfo,
) -> list[str]:
    raw_candidates = [
        local_standard_library_map_path(os.fspath(install_dir), sample),
        *_legacy_standard_library_map_paths(os.fspath(install_dir), sample),
    ]
    candidates: list[str] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        normalized = _normalized_path_for_compare(candidate)
        if normalized is None or normalized in seen:
            continue
        candidates.append(candidate)
        seen.add(normalized)
    return candidates


def _folder_has_contents(path: str) -> bool:
    try:
        return os.path.isdir(path) and len(os.listdir(path)) > 0
    except OSError:
        return False


def _app_supplied_standard_library_map_path_candidates(
    install_dir: str | os.PathLike[str] | None = None,
) -> list[str]:
    if install_dir is None:
        data_dir = resolve_application_paths().data_dir
        roots = (
            data_dir,
            data_dir / MAP_LIBRARY_DIRNAME,
            data_dir / _LEGACY_MAP_LIBRARY_DIRNAME,
        )
    else:
        roots = (Path(_install_dir_path(install_dir)),)

    candidates: list[str] = []
    for root in roots:
        root_path = os.fspath(root)
        for sample in load_initial_standard_library_catalog():
            candidates.append(local_standard_library_map_path(root_path, sample))
            candidates.extend(_legacy_standard_library_map_paths(root_path, sample))
    return candidates


def _normalized_path_for_compare(path: str | os.PathLike[str]) -> str | None:
    try:
        if not path:
            return None
        return os.path.normcase(
            os.path.abspath(os.path.expanduser(os.fspath(path)))
        )
    except (OSError, TypeError, ValueError):
        return None


def _legacy_standard_library_map_paths(
    install_dir: str, sample: StandardLibraryMapInfo
) -> list[str]:
    """Paths used by older builds before the app-managed map_library folder."""
    normalized = os.path.normpath(_install_dir_path(install_dir))
    basename = os.path.basename(normalized).lower()
    legacy_paths: list[str] = []

    if basename == _LEGACY_MAP_LIBRARY_DIRNAME.lower():
        legacy_paths.append(
            os.path.join(normalized, sample.folder_name or sample.display_name)
        )
        legacy_paths.append(
            os.path.join(
                normalized,
                _LEGACY_MAP_LIBRARY_DIRNAME,
                sample.folder_name or sample.display_name,
            )
        )
    elif basename == MAP_LIBRARY_DIRNAME.lower():
        legacy_paths.append(
            os.path.join(
                normalized,
                _LEGACY_MAP_LIBRARY_DIRNAME,
                sample.folder_name or sample.display_name,
            )
        )
        legacy_paths.append(
            os.path.join(
                os.path.dirname(normalized),
                _LEGACY_MAP_LIBRARY_DIRNAME,
                sample.folder_name or sample.display_name,
            )
        )
    else:
        legacy_paths.append(
            os.path.join(
                normalized,
                _LEGACY_MAP_LIBRARY_DIRNAME,
                sample.folder_name or sample.display_name,
            )
        )

    return legacy_paths


def _standard_library_publish_staging_prefix(dest_dir: str) -> str:
    name = os.path.basename(os.path.normpath(dest_dir)) or "standard-library-map"
    safe_name = "".join(
        char if char.isalnum() or char in "._-" else "-"
        for char in name
    ).strip(".-")
    return f".{safe_name or 'standard-library-map'}.tmp-"


def _remove_replaced_standard_library_backup(backup_dir: str) -> None:
    if os.path.isdir(backup_dir) and not os.path.islink(backup_dir):
        shutil.rmtree(backup_dir)
    else:
        os.remove(backup_dir)


def _verify_standard_library_zip_hash(
    zip_path: str,
    sample: StandardLibraryMapInfo,
) -> None:
    """Verify a downloaded archive when the catalog supplies a SHA-256 hash."""
    if not sample.sha256:
        return
    digest = hashlib.sha256()
    with open(zip_path, "rb") as archive:
        for chunk in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_hash = digest.hexdigest()
    if actual_hash != sample.sha256:
        raise ValueError(
            f"Downloaded map archive for {sample.display_name!r} failed "
            "SHA-256 verification"
        )


def _publish_standard_library_map_directory(staging_dir: str, dest_dir: str) -> None:
    """Publish a completed map-library tree while preserving an old install."""
    backup_dir = f"{staging_dir}.previous"
    moved_existing_install = False

    try:
        if os.path.lexists(dest_dir):
            os.replace(dest_dir, backup_dir)
            moved_existing_install = True
        os.replace(staging_dir, dest_dir)
    except BaseException:
        if moved_existing_install:
            try:
                os.replace(backup_dir, dest_dir)
            except OSError as restore_error:
                _LOG.error(
                    "Could not restore previous map-library entry %s after publish failure: %s",
                    dest_dir,
                    restore_error,
                )
        raise

    if moved_existing_install:
        try:
            _remove_replaced_standard_library_backup(backup_dir)
        except OSError as cleanup_error:
            _LOG.warning(
                "Could not remove replaced map-library backup %s: %s",
                backup_dir,
                cleanup_error,
            )


def _copy_and_publish_standard_library_map(
    source_root: str,
    dest_dir: str,
    raise_if_cancelled: Callable[[], None],
) -> None:
    """
    Copy an extracted standard-library map to private sibling staging, then publish it.

    The sibling staging directory keeps the final rename on the destination
    filesystem. If copying or publishing fails, the old installed map-library entry is
    left in place and the unpublished staging tree is removed.
    """
    publish_dest_dir = os.path.abspath(dest_dir)
    parent_dir = os.path.dirname(publish_dest_dir)
    os.makedirs(parent_dir, exist_ok=True)
    publish_staging_dir = tempfile.mkdtemp(
        prefix=_standard_library_publish_staging_prefix(dest_dir),
        dir=parent_dir,
    )
    try:
        shutil.copytree(source_root, publish_staging_dir, dirs_exist_ok=True)
        raise_if_cancelled()
        _publish_standard_library_map_directory(publish_staging_dir, publish_dest_dir)
        publish_staging_dir = ""
    finally:
        if publish_staging_dir:
            shutil.rmtree(publish_staging_dir, ignore_errors=True)


def download_and_extract_standard_library_map(install_dir: str, sample: StandardLibraryMapInfo,
                                    progress_cb=None, cancel_cb=None) -> str:
    """
    Downloads the given standard-library map zip to a temp location, verifies
    its size, extracts it into its own folder under map_library/, and
    cleans up the temp zip. Returns the local folder path the map was
    extracted to (the same thing local_standard_library_map_path() would compute).

    Raises on any failure (network error, size mismatch, bad zip), or raises
    DownloadCancelled when cancel_cb reports cancellation. The caller is
    expected to catch this and show a clear message; a
    failed/partial download should never leave a half-extracted map
    sitting around looking like it succeeded.

    The release zip is assumed to contain a single top-level folder. If so,
    that folder's CONTENTS become
    the map folder's contents, rather than nesting one level deeper than
    expected.
    """
    install_dir = _install_dir_path(install_dir)
    if sample.download_url is None:
        raise ValueError(f"No download URL available for {sample.display_name!r} "
                          f"(asset {sample.asset_name!r} not found on the map library release).")

    def raise_if_cancelled() -> None:
        if cancel_cb and cancel_cb():
            raise DownloadCancelled("Map library download cancelled")

    raise_if_cancelled()
    dest_dir = local_standard_library_map_path(install_dir, sample)
    os.makedirs(_map_library_container_dir(install_dir), exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="caveviewer_map_library_") as tmp_dir:
        zip_path = os.path.join(tmp_dir, sample.asset_name)

        download_update(
            sample.download_url,
            sample.size_bytes,
            zip_path,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
        )
        raise_if_cancelled()
        _verify_standard_library_zip_hash(zip_path, sample)
        raise_if_cancelled()

        staging_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(staging_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                raise_if_cancelled()
                zf.extract(member, staging_dir)
        raise_if_cancelled()

        staging_contents = os.listdir(staging_dir)
        if len(staging_contents) == 1 and os.path.isdir(
            os.path.join(staging_dir, staging_contents[0])
        ):
            source_root = os.path.join(staging_dir, staging_contents[0])
        else:
            source_root = staging_dir

        raise_if_cancelled()
        _copy_and_publish_standard_library_map(source_root, dest_dir, raise_if_cancelled)

    _LOG.info("Map library entry extracted: %s", dest_dir)
    return dest_dir
