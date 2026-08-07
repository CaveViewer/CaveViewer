"""Chunk cache metadata, spatial indexing, and cache lookup helpers."""

from __future__ import annotations

import math
import os

import numpy as np

from caveviewer.core.chunking.io import CHUNKS_DIRNAME, _VERSION
from caveviewer.core.chunking.staging import MANIFEST_NAME
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.json_io import load_bounded_json
from caveviewer.core.map.cache_paths import map_cache_candidates

CHUNK_SIZE_ENV_VAR = "CAVEVIEWER_CHUNK_SIZE_METERS"
_DEFAULT_CHUNK_SIZE_FALLBACK = 50.0  # meters; default for new cache builds
MAX_CACHE_MANIFEST_BYTES = 128 * 1024 * 1024
_LOG = get_logger("chunker")


def _resolve_default_chunk_size() -> float:
    raw = os.environ.get(CHUNK_SIZE_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_CHUNK_SIZE_FALLBACK
    try:
        value = float(raw)
        if value <= 0.0:
            raise ValueError("must be > 0")
        return value
    except Exception:
        _LOG.warning(
            f"ignoring invalid {CHUNK_SIZE_ENV_VAR}={raw!r}; "
            f"using default {_DEFAULT_CHUNK_SIZE_FALLBACK:.1f}m"
        )
        return _DEFAULT_CHUNK_SIZE_FALLBACK


DEFAULT_CHUNK_SIZE = _resolve_default_chunk_size()


def configured_chunk_size() -> float:
    """Return the chunk size currently configured for new cache builds."""
    return _resolve_default_chunk_size()


def world_to_cell(point: np.ndarray, chunk_size: float) -> tuple[int, int, int]:
    return tuple(np.floor(point / chunk_size).astype(np.int64).tolist())


def _footprint_from_positions(positions: np.ndarray) -> tuple[float, list[int]]:
    _FOOTPRINT_TARGET_CELLS = 200
    _FOOTPRINT_BLOCK_VERTICES = 250_000
    if len(positions) == 0:
        return 2.0, []

    pos_x = positions[:, 0]
    pos_z = positions[:, 2]
    min_x = float(pos_x.min())
    max_x = float(pos_x.max())
    min_z = float(pos_z.min())
    max_z = float(pos_z.max())
    extent_max = max(
        max_x - min_x,
        max_z - min_z,
        1.0,
    )
    footprint_cell_size = max(2.0, extent_max / _FOOTPRINT_TARGET_CELLS)
    min_cx = int(np.floor(min_x / footprint_cell_size))
    min_cz = int(np.floor(min_z / footprint_cell_size))
    max_cz = int(np.floor(max_z / footprint_cell_size))
    z_span = max(1, max_cz - min_cz + 1)

    unique_keys = np.empty(0, dtype=np.int64)
    for start in range(0, len(positions), _FOOTPRINT_BLOCK_VERTICES):
        end = min(start + _FOOTPRINT_BLOCK_VERTICES, len(positions))
        block_cx = np.floor(pos_x[start:end] / footprint_cell_size).astype(np.int64)
        block_cz = np.floor(pos_z[start:end] / footprint_cell_size).astype(np.int64)
        block_keys = (block_cx - min_cx) * z_span + (block_cz - min_cz)
        block_unique = np.unique(block_keys)
        if unique_keys.size:
            unique_keys = np.unique(np.concatenate((unique_keys, block_unique)))
        else:
            unique_keys = block_unique

    footprint_flat: list[int] = []
    for key in unique_keys.tolist():
        footprint_flat.append(int(key // z_span) + min_cx)
        footprint_flat.append(int(key % z_span) + min_cz)
    return footprint_cell_size, footprint_flat


def load_manifest(cache_dir):
    # If no cache_dir was provided (launch without a preloaded map), or the
    # manifest file is missing, return None so callers can handle "no map".
    if not cache_dir:
        return None

    manifest_path = os.path.join(cache_dir, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        return None

    try:
        manifest = load_bounded_json(
            manifest_path,
            max_bytes=MAX_CACHE_MANIFEST_BYTES,
            description="cache manifest",
        )
    except (OSError, ValueError) as exc:
        _LOG.warning("could not read cache manifest %s: %s", manifest_path, exc)
        return None
    if not isinstance(manifest, dict):
        _LOG.warning("cache manifest is not a JSON object: %s", manifest_path)
        return None
    return manifest


def manifest_chunk_size(manifest: dict | None) -> float | None:
    """Return the chunk size recorded in a cache manifest, if valid."""
    if not isinstance(manifest, dict):
        return None
    try:
        chunk_size = float(manifest.get("chunk_size"))
    except (TypeError, ValueError):
        return None
    if chunk_size <= 0.0:
        return None
    return chunk_size


def manifest_max_upload_group_mb(manifest: dict | None) -> float | None:
    """Return the upload-group MB limit recorded in a cache manifest."""
    if not isinstance(manifest, dict):
        return None
    try:
        value = float(manifest.get("max_upload_group_mb"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


def cache_chunk_size(cache_dir: str) -> float | None:
    """Read the chunk size from an existing cache's manifest."""
    try:
        return manifest_chunk_size(load_manifest(cache_dir))
    except Exception:
        return None


def cache_dir_is_valid(cache_dir: str, source_path: str | None = None) -> bool:
    """Return whether one explicit cache directory is current and usable."""
    manifest_path = os.path.join(cache_dir, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        return False
    if source_path is not None and os.path.getmtime(manifest_path) < os.path.getmtime(source_path):
        return False
    try:
        manifest = load_bounded_json(
            manifest_path,
            max_bytes=MAX_CACHE_MANIFEST_BYTES,
            description="cache manifest",
        )
    except Exception:
        return False
    return _has_current_chunk_cache(cache_dir, manifest)


def cache_is_valid(obj_path: str) -> bool:
    """Return whether a source OBJ has a current, non-stale chunk cache."""
    return any(
        cache_dir_is_valid(cache_dir, obj_path)
        for cache_dir in map_cache_candidates(obj_path)
    )


def _has_current_chunk_cache(cache_dir: str, manifest: dict) -> bool:
    """Return whether a manifest points at the active render-chunk layout."""
    if not isinstance(manifest, dict) or manifest.get("version") != _VERSION:
        return False
    if not isinstance(manifest.get("chunks"), dict):
        return False
    return os.path.isdir(os.path.join(cache_dir, CHUNKS_DIRNAME))


def get_cache_dir(obj_path: str) -> str:
    candidates = map_cache_candidates(obj_path)
    for candidate in candidates:
        if os.path.exists(os.path.join(candidate, MANIFEST_NAME)):
            return candidate
    return candidates[0]


def find_landing_position(
    manifest: dict,
    target_x: float,
    target_z: float,
    preferred_y: float,
    search_radius_cells: int = 12,
) -> tuple[float, float, float]:
    """
    Given a target world (x, z) -- e.g. from a minimap click, which only
    knows X/Z -- finds a world (x, y, z) that actually lands inside the
    cave's occupied space near that column, rather than blindly keeping
    whatever Y the camera happened to be at before.

    Strategy: look at every chunk cell whose (x, z) column matches the
    target cell (collapsing Y, same idea as the minimap's footprint). Each
    matching cell's vertical center (midpoint of its bounds_min/max Y) is
    a candidate landing height; pick whichever candidate is closest to
    `preferred_y` (typically the camera's current height) so a multi-level
    cave doesn't always snap you to the lowest or first-found level --
    if you're already up high and click a spot that has both a low and a
    high passage, you land in the one nearer to where you already were.

    If no chunk exists at that exact (x, z) column (a click slightly off
    from any real passage on the crude minimap outline, since chunk cells
    are coarse), the search expands outward ring by ring up to
    `search_radius_cells` until it finds the nearest occupied column, and
    targets the center of THAT column's cells instead -- so a near-miss
    click still lands you inside the cave rather than in empty space.

    search_radius_cells defaults to 12 (not a small number like 3) because
    a thin, winding cave passage drawn on a coarse minimap is easy to
    click slightly off of -- especially on a long straight stretch, where
    the click error needed to miss the passage entirely doesn't need to
    be large. A too-small search radius meant some clicks fell through
    every ring with nothing found, landing the camera in genuinely empty
    space with zero chunks anywhere nearby (visible as "CHUNKS 0" forever
    and a loading panel that never finds anything to load).

    If even the expanded ring search finds nothing (a pathological case,
    e.g. an extremely sparse or disconnected map), this falls back to the
    single closest occupied column anywhere in the ENTIRE map, rather
    than giving up and teleporting into empty space -- guaranteeing this
    function always lands you somewhere inside the cave if the cave has
    any chunks at all.

    Returns (landing_x, landing_y, landing_z). landing_x/z may differ
    significantly from target_x/z if the fallback search had to reach far
    to find any occupied column at all.
    """
    chunk_size = manifest["chunk_size"]
    target_cx = int(np.floor(target_x / chunk_size))
    target_cz = int(np.floor(target_z / chunk_size))

    # Build a quick lookup: (cx, cz) -> list of (y_center, cell_str) for
    # every cell in that column, across all Y levels.
    columns: dict[tuple[int, int], list[tuple[float, str]]] = {}
    for cell_str, info in manifest["chunks"].items():
        cx, _cy, cz = (int(v) for v in cell_str.split("_"))
        y_center = (info["bounds_min"][1] + info["bounds_max"][1]) / 2.0
        columns.setdefault((cx, cz), []).append((y_center, cell_str))

    def best_y_in_column(cx: int, cz: int) -> float | None:
        candidates = columns.get((cx, cz))
        if not candidates:
            return None
        # closest to preferred_y, so multi-level caves keep you near your
        # current level rather than always jumping to one extreme
        return min(candidates, key=lambda c: abs(c[0] - preferred_y))[0]

    # exact column first
    y = best_y_in_column(target_cx, target_cz)
    if y is not None:
        return target_x, y, target_z

    # expand outward ring by ring looking for the nearest occupied column
    for radius in range(1, search_radius_cells + 1):
        best_dist = None
        best_col = None
        best_y_val = None
        for dx in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                if max(abs(dx), abs(dz)) != radius:
                    continue  # only the new outer ring at this radius
                col = (target_cx + dx, target_cz + dz)
                y_val = best_y_in_column(*col)
                if y_val is None:
                    continue
                dist = dx * dx + dz * dz
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_col = col
                    best_y_val = y_val
        if best_col is not None:
            landing_x = (best_col[0] + 0.5) * chunk_size
            landing_z = (best_col[1] + 0.5) * chunk_size
            return landing_x, best_y_val, landing_z

    # Ring search exhausted with nothing found -- rather than teleport
    # into empty space (the actual bug this fixes), fall back to a full
    # scan of every occupied column in the manifest and pick whichever is
    # closest to the original click. This is more expensive (O(number of
    # chunks)) but only runs in this rare fallback case, and guarantees a
    # minimap click always lands somewhere inside the cave if the cave
    # has any chunks loaded into the manifest at all.
    best_dist = None
    best_col = None
    best_y_val = None
    for (cx, cz), candidates in columns.items():
        dist = (cx - target_cx) ** 2 + (cz - target_cz) ** 2
        if best_dist is None or dist < best_dist:
            y_val = min(candidates, key=lambda c: abs(c[0] - preferred_y))[0]
            best_dist = dist
            best_col = (cx, cz)
            best_y_val = y_val

    if best_col is not None:
        landing_x = (best_col[0] + 0.5) * chunk_size
        landing_z = (best_col[1] + 0.5) * chunk_size
        return landing_x, best_y_val, landing_z

    # truly no chunks exist anywhere in the manifest (an empty/corrupt
    # cache) -- nothing sensible to land on, so fall back to the original
    # behavior of just keeping preferred_y rather than raising.
    return target_x, preferred_y, target_z
