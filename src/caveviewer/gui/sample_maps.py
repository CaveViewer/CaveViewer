"""
caveviewer.gui.sample_maps

Lets someone try CaveViewer without having their own scan, by downloading
one of a small set of sample cave maps hosted as assets on a dedicated
GitHub release (tag: "sample-data") -- completely separate from the
app's own version releases, so publishing a new app version never
touches this, and vice versa.

Why a separate release rather than bundling the maps with the app: the
scans are tens to hundreds of MB each, and bundling them into the app's
own download would make every single app install/update carry that
weight even for people who already have their own maps. Keeping them as
on-demand downloads means the base app stays small, and sample maps are
only ever fetched by someone who actually clicks to get one.

This module only knows about fetching release metadata and downloading/
extracting a chosen asset -- it has no UI of its own. See
caveviewer.gui.sample_maps_dialog for the Tkinter window that presents the list
and drives this.
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
from typing import Optional

from caveviewer.core.logging_utils import get_logger
from caveviewer.gui.update_checker import (
    DownloadCancelled,
    download_update,
    make_ssl_context,
)
from caveviewer.storage_paths import resolve_application_paths


_LOG = get_logger("SampleMaps")


# Configuration for sample maps repository.
# Sample maps are hosted by default at:
# https://github.com/KernalPanic/CaveViewer/releases/tag/sample-data
_DEFAULT_SAMPLE_MAPS_REPO = "KernalPanic/CaveViewer"
_DEFAULT_SAMPLE_DATA_TAG = "sample-data"

def _env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default

_SAMPLE_MAPS_REPO = _env_or_default("CAVEVIEWER_SAMPLE_MAPS_REPO", _DEFAULT_SAMPLE_MAPS_REPO)
_SAMPLE_DATA_TAG = _env_or_default("CAVEVIEWER_SAMPLE_DATA_TAG", _DEFAULT_SAMPLE_DATA_TAG)
_TAGGED_RELEASE_API_URL = _env_or_default(
    "CAVEVIEWER_SAMPLE_MAPS_API_URL",
    f"https://api.github.com/repos/{_SAMPLE_MAPS_REPO}/releases/tags/{_SAMPLE_DATA_TAG}",
)
_REQUEST_TIMEOUT_SECONDS = 8

SAMPLE_MAPS_DIRNAME = "sample_maps"
_SAMPLE_MAPS_CONFIG_LOGGED = False


@dataclass
class SampleMapInfo:
    display_name: str
    asset_name: str
    download_url: Optional[str] = None
    size_bytes: Optional[int] = None


KNOWN_SAMPLE_MAPS = [
    SampleMapInfo(display_name="Devils Eye", asset_name="Devils.Eye.3D.Map.zip"),
    SampleMapInfo(display_name="Peacock Springs Cave System", asset_name="Peacock.Springs.Cave.System.3D.Map.zip"),
]


def default_sample_maps_install_dir() -> str:
    """Return the app-managed default root for first-time sample-map downloads."""
    data_dir = resolve_application_paths().data_dir
    sample_maps_dir = data_dir / SAMPLE_MAPS_DIRNAME
    try:
        sample_maps_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOG.warning(
            "Could not create default sample map directory %s: %s",
            sample_maps_dir,
            exc,
        )
    if sample_maps_dir.is_dir():
        return str(sample_maps_dir)

    # If a conflicting file or permissions problem blocks the dedicated
    # sample_maps/ folder, fall back to the XDG data root. The normal
    # download helper will still create sample_maps/<map> under this root.
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(data_dir)


def _known_maps_with_no_download_info():
    """
    Fresh SampleMapInfo entries for every known map, with download_url/
    size_bytes left as None -- used as the fallback whenever the GitHub
    fetch fails for any reason. Returning these (rather than an empty
    list) is the actual fix for sample maps becoming unreachable while
    offline: the dialog can still show every known map and still check
    each one against local disk independently, so an already-downloaded
    map's Open button keeps working even when this fetch fails entirely.
    """
    return [
        SampleMapInfo(display_name=known.display_name, asset_name=known.asset_name)
        for known in KNOWN_SAMPLE_MAPS
    ]


def fetch_sample_map_catalog():
    """
    Fetches the sample-data release's asset list from GitHub and matches
    it up against KNOWN_SAMPLE_MAPS by filename, filling in each entry's
    real download_url/size_bytes.

    Returns (list_of_sample_maps, error_message_or_None).

    IMPORTANT: even when this fails (no internet, GitHub unreachable,
    etc -- reported via the error string), it still returns one
    SampleMapInfo per entry in KNOWN_SAMPLE_MAPS, just with
    download_url/size_bytes left as None. This is deliberate: a failed
    network fetch should never hide or remove an entry that the person
    might ALREADY have downloaded previously -- the caller
    (sample_maps_dialog.py) checks local disk state independently of
    whatever this function returns, specifically so "no internet right
    now" never blocks opening a sample map that's already sitting on
    disk from an earlier successful download. Only entries that are
    NEITHER already-downloaded NOR successfully fetched end up
    genuinely unusable, and even then they're shown (as "unavailable"),
    never silently dropped.
    """
    _log_sample_maps_config_once()

    try:
        request = urllib.request.Request(
            _TAGGED_RELEASE_API_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "CaveViewer-SampleMaps"},
        )
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS,
                                     context=make_ssl_context()) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            error_msg = f"No sample map release found for tag {_SAMPLE_DATA_TAG!r}."
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
            error_msg = f"Couldn't reach GitHub ({reason}). This may be a temporary network issue -- try again in a moment."
        else:
            error_msg = "Couldn't reach GitHub right now. This may be a temporary network issue -- try again in a moment."
        return _known_maps_with_no_download_info(), error_msg
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return _known_maps_with_no_download_info(), f"Got an unexpected response from GitHub: {e}"

    assets_by_name = {a.get("name"): a for a in data.get("assets", [])}

    results = []
    for known in KNOWN_SAMPLE_MAPS:
        asset = assets_by_name.get(known.asset_name)
        if asset:
            results.append(SampleMapInfo(
                display_name=known.display_name,
                asset_name=known.asset_name,
                download_url=asset.get("browser_download_url"),
                size_bytes=asset.get("size"),
            ))
        else:
            results.append(SampleMapInfo(display_name=known.display_name, asset_name=known.asset_name))

    return results, None


def _log_sample_maps_config_once() -> None:
    global _SAMPLE_MAPS_CONFIG_LOGGED
    if _SAMPLE_MAPS_CONFIG_LOGGED:
        return

    _SAMPLE_MAPS_CONFIG_LOGGED = True
    _LOG.info(
        "Sample map source env: CAVEVIEWER_SAMPLE_MAPS_API_URL=%r, "
        "CAVEVIEWER_SAMPLE_MAPS_REPO=%r, CAVEVIEWER_SAMPLE_DATA_TAG=%r",
        os.environ.get("CAVEVIEWER_SAMPLE_MAPS_API_URL"),
        os.environ.get("CAVEVIEWER_SAMPLE_MAPS_REPO"),
        os.environ.get("CAVEVIEWER_SAMPLE_DATA_TAG"),
    )
    _LOG.info(
        "Sample map source resolved: api_url=%r, repo=%r, tag=%r",
        _TAGGED_RELEASE_API_URL,
        _SAMPLE_MAPS_REPO,
        _SAMPLE_DATA_TAG,
    )


def _install_dir_path(install_dir) -> str:
    """
    Normalize a sample-map install root to a filesystem path string.

    Linux portal choosers return DirectorySelection objects. UI code should pass
    their `.path`, but accepting path-like selection objects here keeps the
    non-UI download helper robust when portal and Tk fallback paths differ.
    """
    return os.fspath(getattr(install_dir, "path", install_dir))


def local_sample_map_path(install_dir: str, sample: SampleMapInfo) -> str:
    """
    Where a given sample map would live locally once downloaded --
    one subfolder per map, named after its display name (so it reads
    clearly in a file browser), inside the shared sample_maps folder.
    """
    return os.path.join(_sample_maps_container_dir(install_dir), sample.display_name)


def _sample_maps_container_dir(install_dir: str) -> str:
    """
    Return the folder that should directly contain individual sample maps.

    The dialog asks where to save sample maps, and CaveViewer normally creates
    a shared sample_maps/ folder inside that selected directory. If the user
    already chooses a folder named sample_maps, treat that folder itself as the
    container instead of creating sample_maps/sample_maps/... .
    """
    normalized = os.path.normpath(_install_dir_path(install_dir))
    if os.path.basename(normalized).lower() == SAMPLE_MAPS_DIRNAME.lower():
        return normalized
    return os.path.join(normalized, SAMPLE_MAPS_DIRNAME)


def is_sample_map_already_downloaded(install_dir: str, sample: SampleMapInfo) -> bool:
    """
    True if this sample map's local folder already exists and has
    something in it -- used so the dialog can offer "Open" instead of
    "Download" for a map that's already been fetched, rather than
    re-downloading tens to hundreds of MB unnecessarily every time.
    """
    path = local_sample_map_path(install_dir, sample)
    return _folder_has_contents(path) or _folder_has_contents(_legacy_nested_sample_map_path(install_dir, sample))


def existing_sample_map_path(install_dir: str, sample: SampleMapInfo) -> str:
    """Return the actual existing local path for a sample map, if one exists."""
    path = local_sample_map_path(install_dir, sample)
    if _folder_has_contents(path):
        return path
    legacy_path = _legacy_nested_sample_map_path(install_dir, sample)
    if _folder_has_contents(legacy_path):
        return legacy_path
    return path


def _folder_has_contents(path: str) -> bool:
    try:
        return os.path.isdir(path) and len(os.listdir(path)) > 0
    except OSError:
        return False


def _legacy_nested_sample_map_path(install_dir: str, sample: SampleMapInfo) -> str:
    """Path used by older builds when users selected an existing sample_maps folder."""
    normalized = os.path.normpath(_install_dir_path(install_dir))
    if os.path.basename(normalized).lower() != SAMPLE_MAPS_DIRNAME.lower():
        return ""
    return os.path.join(normalized, SAMPLE_MAPS_DIRNAME, sample.display_name)


def download_and_extract_sample_map(install_dir: str, sample: SampleMapInfo,
                                    progress_cb=None, cancel_cb=None) -> str:
    """
    Downloads the given sample map's zip to a temp location, verifies
    its size, extracts it into its own folder under sample_maps/, and
    cleans up the temp zip. Returns the local folder path the map was
    extracted to (the same thing local_sample_map_path() would compute).

    Raises on any failure (network error, size mismatch, bad zip), or raises
    DownloadCancelled when cancel_cb reports cancellation. The caller is
    expected to catch this and show a clear message; a
    failed/partial download should never leave a half-extracted map
    sitting around looking like it succeeded.

    The release zip is assumed to contain a single top-level folder (matching
    how the sample zips were packaged) -- if so, that folder's CONTENTS become
    the map folder's contents, rather than nesting one level deeper than
    expected.
    """
    install_dir = _install_dir_path(install_dir)
    if sample.download_url is None:
        raise ValueError(f"No download URL available for {sample.display_name!r} "
                          f"(asset {sample.asset_name!r} not found on the sample-data release).")

    def raise_if_cancelled() -> None:
        if cancel_cb and cancel_cb():
            raise DownloadCancelled("Sample map download cancelled")

    raise_if_cancelled()
    dest_dir = local_sample_map_path(install_dir, sample)
    os.makedirs(_sample_maps_container_dir(install_dir), exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="caveviewer_samplemap_") as tmp_dir:
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
        if len(staging_contents) == 1 and os.path.isdir(os.path.join(staging_dir, staging_contents[0])):
            source_root = os.path.join(staging_dir, staging_contents[0])
        else:
            source_root = staging_dir

        raise_if_cancelled()
        if os.path.isdir(dest_dir):
            shutil.rmtree(dest_dir)
        shutil.copytree(source_root, dest_dir)

    _LOG.info("Sample map extracted: %s", dest_dir)
    return dest_dir
