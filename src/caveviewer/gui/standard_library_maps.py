"""Standard-library map source, storage, and download helpers.

CaveViewer keeps standard-library maps as release assets instead of bundling
them with the app. The map assets can be large, and keeping them as
on-demand downloads keeps the base install small for people who already have
their own maps.

This module fetches release metadata, resolves local map-library paths, and
downloads/extracts a selected standard-library map archive. It has no Tk UI.
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
import tempfile
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.update_checker import (
    DownloadCancelled,
    download_update,
    make_ssl_context,
)
from caveviewer.storage_paths import resolve_application_paths


_LOG = get_logger("StandardLibraryMaps")


# Configuration for the standard-library map repository.
# Map library assets are hosted by default at:
# https://github.com/CaveViewer/CaveViewer/releases/tag/sample-data
_DEFAULT_MAP_LIBRARY_REPO = "CaveViewer/CaveViewer"
_DEFAULT_MAP_LIBRARY_RELEASE_TAG = "sample-data"


def _env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


_MAP_LIBRARY_REPO = _env_or_default(
    "CAVEVIEWER_SAMPLE_MAPS_REPO", _DEFAULT_MAP_LIBRARY_REPO
)
_MAP_LIBRARY_RELEASE_TAG = _env_or_default(
    "CAVEVIEWER_SAMPLE_DATA_TAG", _DEFAULT_MAP_LIBRARY_RELEASE_TAG
)
_TAGGED_RELEASE_API_URL = _env_or_default(
    "CAVEVIEWER_SAMPLE_MAPS_API_URL",
    f"https://api.github.com/repos/{_MAP_LIBRARY_REPO}/releases/tags/{_MAP_LIBRARY_RELEASE_TAG}",
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


@dataclass(frozen=True)
class StandardLibraryMapRemovalResult:
    """Result of removing app-managed downloaded files for one map-library entry."""

    removed_paths: tuple[str, ...]
    error: str | None = None


KNOWN_STANDARD_LIBRARY_MAPS = [
    StandardLibraryMapInfo(
        display_name="Boh Yai Mine I (Low Res)",
        asset_name="Boh.Yai.Mine.I.Low.Res.zip",
    ),
    StandardLibraryMapInfo(
        display_name="Boh Yai Mine II (Low Res)",
        asset_name="Boh.Yai.Mine.II.Low.Res.zip",
    ),
    StandardLibraryMapInfo(
        display_name="Devils Eye",
        asset_name="Devils.Eye.3D.Map.zip",
    ),
    StandardLibraryMapInfo(
        display_name="Peacock Springs Cave System",
        asset_name="Peacock.Springs.Cave.System.3D.Map.zip",
    ),
]


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


def _known_maps_with_no_download_info():
    """
    Fresh StandardLibraryMapInfo entries for every known map, with download_url/
    size_bytes left as None -- used as the fallback whenever the GitHub
    fetch fails for any reason. Returning these (rather than an empty
    list) keeps standard-library maps reachable while
    offline: the dialog can still show every known map and still check
    each one against local disk independently, so an already-downloaded
    map's Open button keeps working even when this fetch fails entirely.
    """
    return [
        StandardLibraryMapInfo(display_name=known.display_name, asset_name=known.asset_name)
        for known in KNOWN_STANDARD_LIBRARY_MAPS
    ]


def fetch_standard_library_catalog():
    """
    Fetches the sample-data release's asset list from GitHub and matches
    it up against KNOWN_STANDARD_LIBRARY_MAPS by filename, filling in each entry's
    real download_url/size_bytes.

    Returns (list_of_standard_library_maps, error_message_or_None).

    IMPORTANT: even when this fails (no internet, GitHub unreachable,
    etc -- reported via the error string), it still returns one
    StandardLibraryMapInfo per entry in KNOWN_STANDARD_LIBRARY_MAPS, just with
    download_url/size_bytes left as None. This is deliberate: a failed
    network fetch should never hide or remove an entry that the person
    might ALREADY have downloaded previously -- the caller
    the caller checks local disk state independently of
    whatever this function returns, specifically so "no internet right
    now" never blocks opening a map-library entry that's already sitting on
    disk from an earlier successful download. Only entries that are
    NEITHER already-downloaded NOR successfully fetched end up
    genuinely unusable, and even then they're shown (as "unavailable"),
    never silently dropped.
    """
    _log_map_library_config_once()

    try:
        request = urllib.request.Request(
            _TAGGED_RELEASE_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "CaveViewer-MapLibrary",
            },
        )
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS,
                                     context=make_ssl_context()) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            error_msg = (
                f"No map library release found for tag {_MAP_LIBRARY_RELEASE_TAG!r}."
            )
        else:
            error_msg = f"GitHub returned an error (HTTP {e.code})."
        return _known_maps_with_no_download_info(), error_msg
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
        return _known_maps_with_no_download_info(), error_msg
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return (
            _known_maps_with_no_download_info(),
            f"Got an unexpected response from GitHub: {e}",
        )

    assets_by_name = {a.get("name"): a for a in data.get("assets", [])}

    results = []
    for known in KNOWN_STANDARD_LIBRARY_MAPS:
        asset = assets_by_name.get(known.asset_name)
        if asset:
            results.append(
                StandardLibraryMapInfo(
                    display_name=known.display_name,
                    asset_name=known.asset_name,
                    download_url=asset.get("browser_download_url"),
                    size_bytes=asset.get("size"),
                )
            )
        else:
            results.append(
                StandardLibraryMapInfo(
                    display_name=known.display_name, asset_name=known.asset_name
                )
            )

    return results, None


def _log_map_library_config_once() -> None:
    global _MAP_LIBRARY_CONFIG_LOGGED
    if _MAP_LIBRARY_CONFIG_LOGGED:
        return

    _MAP_LIBRARY_CONFIG_LOGGED = True
    _LOG.info(
        "Map library source legacy env: CAVEVIEWER_SAMPLE_MAPS_API_URL=%r, "
        "CAVEVIEWER_SAMPLE_MAPS_REPO=%r, CAVEVIEWER_SAMPLE_DATA_TAG=%r",
        os.environ.get("CAVEVIEWER_SAMPLE_MAPS_API_URL"),
        os.environ.get("CAVEVIEWER_SAMPLE_MAPS_REPO"),
        os.environ.get("CAVEVIEWER_SAMPLE_DATA_TAG"),
    )
    _LOG.info(
        "Map library source resolved: api_url=%r, repo=%r, tag=%r",
        _TAGGED_RELEASE_API_URL,
        _MAP_LIBRARY_REPO,
        _MAP_LIBRARY_RELEASE_TAG,
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
    return os.path.join(_map_library_container_dir(install_dir), sample.display_name)


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
        for sample in KNOWN_STANDARD_LIBRARY_MAPS:
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
        legacy_paths.append(os.path.join(normalized, sample.display_name))
        legacy_paths.append(
            os.path.join(normalized, _LEGACY_MAP_LIBRARY_DIRNAME, sample.display_name)
        )
    elif basename == MAP_LIBRARY_DIRNAME.lower():
        legacy_paths.append(
            os.path.join(normalized, _LEGACY_MAP_LIBRARY_DIRNAME, sample.display_name)
        )
        legacy_paths.append(
            os.path.join(
                os.path.dirname(normalized),
                _LEGACY_MAP_LIBRARY_DIRNAME,
                sample.display_name,
            )
        )
    else:
        legacy_paths.append(
            os.path.join(normalized, _LEGACY_MAP_LIBRARY_DIRNAME, sample.display_name)
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
                          f"(asset {sample.asset_name!r} not found on the sample-data release).")

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
