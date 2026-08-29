"""Export a bounded region of a render cache as a standalone CaveViewer map.

Slices operate on the render-cache representation rather than the original
OBJ/GLB source.  That makes an exported directory portable, and also lets the
operation run for a cache-only map opened on a different computer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

import numpy as np

from caveviewer.core.chunking.io import (
    CHUNKS_DIRNAME,
    iter_chunk_file_groups,
)
from caveviewer.core.chunking.metadata import (
    load_manifest,
    manifest_chunk_size,
    manifest_max_upload_group_mb,
)
from caveviewer.core.chunking.staging import (
    MANIFEST_NAME,
    CacheAsset,
    _publish_cache_directory,
    _stage_cache_assets,
)
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.map.cache_build_lock import CacheBuildLock
from caveviewer.core.map.cache_identity import (
    GUIDED_DIVE_CACHE_IDENTITY_KEY,
    GuidedDiveCacheIdentity,
    build_derived_guided_dive_cache_identity,
    canonical_manifest_sha256,
    guided_dive_cache_identity_from_manifest,
)
from caveviewer.core.textures.decoding import resolve_texture_path


SLICE_MANIFEST_KEY = "slice"
SLICE_SCHEMA_VERSION = 1
SLICE_EXPORTER_VERSION = 2
SLICE_GEOMETRY_MODE = "whole_source_chunks"
SLICE_MARKER_SUFFIX = ".cvslice"
DEFAULT_SLICE_PADDING = 5.0
_SLICE_SEGMENT_SEPARATOR = " - Segment "
# Chunk groups remain bounded while the exporter validates source geometry and
# derives the slice's detailed minimap footprint before copying each chunk.
_SLICE_BLOCK_VERTICES = 4_095
_SLICE_NAME_MAX_LENGTH = 80
_BOUNDS_EPSILON = 1.0e-6
_FOOTPRINT_TARGET_CELLS = 200
_MIN_FOOTPRINT_CELL_SIZE = 2.0
_SLICE_MANIFEST_RESERVE_BYTES = 1024 * 1024

ProgressCallback = Callable[[str, float], None]
CancelCallback = Callable[[], bool]

_LOG = get_logger("MapSlice")


class SliceExportCancelled(RuntimeError):
    """Raised when a user requests cancellation before publication."""


@dataclass(frozen=True, slots=True)
class SliceBounds:
    """Finite axis-aligned source-coordinate bounds for one export."""

    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]

    def __post_init__(self) -> None:
        minimum = _finite_vector(self.minimum, "slice minimum")
        maximum = _finite_vector(self.maximum, "slice maximum")
        if any(maximum[index] <= minimum[index] for index in range(3)):
            raise ValueError("Slice bounds must have positive extent on every axis")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)

    @classmethod
    def from_anchors(
        cls,
        start: tuple[float, float, float] | np.ndarray,
        end: tuple[float, float, float] | np.ndarray,
        *,
        padding: float = DEFAULT_SLICE_PADDING,
    ) -> "SliceBounds":
        """Create padded bounds around the two camera anchors."""
        resolved_padding = float(padding)
        if not math.isfinite(resolved_padding) or resolved_padding < 0.0:
            raise ValueError("Slice padding must be a finite non-negative value")
        start_vector = np.asarray(_finite_vector(start, "slice start"), dtype=np.float64)
        end_vector = np.asarray(_finite_vector(end, "slice end"), dtype=np.float64)
        return cls(
            tuple((np.minimum(start_vector, end_vector) - resolved_padding).tolist()),
            tuple((np.maximum(start_vector, end_vector) + resolved_padding).tolist()),
        )

    def overlaps(self, minimum: np.ndarray, maximum: np.ndarray) -> bool:
        """Return whether this volume overlaps a finite chunk AABB."""
        return bool(
            np.all(np.asarray(maximum, dtype=np.float64) >= self.minimum)
            and np.all(np.asarray(minimum, dtype=np.float64) <= self.maximum)
        )

    def payload(self) -> dict[str, list[float]]:
        """Return JSON-safe canonical bounds metadata."""
        return {
            "bounds_min": list(self.minimum),
            "bounds_max": list(self.maximum),
        }


@dataclass(frozen=True, slots=True)
class SliceExportRequest:
    """All core-owned input required to publish one standalone slice."""

    source_cache_dir: str
    output_dir: str
    bounds: SliceBounds
    entry_position: tuple[float, float, float]
    display_name: str | None = None
    root_cave_name: str | None = None

    def __post_init__(self) -> None:
        raw_source_cache_dir = os.fspath(self.source_cache_dir).strip()
        raw_output_dir = os.fspath(self.output_dir).strip()
        if not raw_source_cache_dir or not raw_output_dir:
            raise ValueError("Slice export requires source and output directories")
        # Use physical paths for both operation locks.  A map opened through
        # a symlink must still coordinate with a rebuild using its real cache
        # location, and an output beneath a symlinked map library must remain
        # on that same filesystem for atomic publication.
        source_cache_dir = os.path.realpath(os.path.abspath(raw_source_cache_dir))
        output_dir = os.path.realpath(os.path.abspath(raw_output_dir))
        if source_cache_dir == output_dir:
            raise ValueError("Slice output cannot replace its source cache")
        if _paths_overlap(source_cache_dir, output_dir):
            raise ValueError("Slice output must not contain or replace its source cache")
        if not os.path.basename(output_dir):
            raise ValueError("Slice output directory must have a name")
        entry_position = _finite_vector(self.entry_position, "slice entry position")
        if not _point_inside_bounds(entry_position, self.bounds):
            raise ValueError("Slice entry position must be inside the slice bounds")
        object.__setattr__(self, "source_cache_dir", source_cache_dir)
        object.__setattr__(self, "output_dir", output_dir)
        object.__setattr__(self, "entry_position", entry_position)
        if self.display_name is not None:
            object.__setattr__(self, "display_name", str(self.display_name).strip())
        if self.root_cave_name is not None:
            object.__setattr__(self, "root_cave_name", str(self.root_cave_name).strip())


@dataclass(frozen=True, slots=True)
class SliceExportResult:
    """The published map root and compact export accounting."""

    output_dir: str
    triangle_count: int
    chunk_count: int
    texture_count: int


def sanitize_slice_name(value: str | None, *, fallback: str = "Cave slice") -> str:
    """Return a portable child-directory and marker basename."""
    raw = str(value or "").strip()
    if not raw:
        raw = fallback
    normalized = "".join(
        character if character.isalnum() or character in {" ", "-", "_", "."} else "-"
        for character in raw
    )
    normalized = " ".join(normalized.split()).strip(" .-")
    normalized = normalized[:_SLICE_NAME_MAX_LENGTH].rstrip(" .-")
    return normalized or "Cave slice"


def next_slice_display_name(
    map_storage_dir: str | os.PathLike[str],
    cave_name: str | None,
) -> str:
    """Return the next stable, portable segment name for one source cave.

    Existing segment names are matched case-insensitively so the same map
    storage directory produces one sequence on case-sensitive and
    case-insensitive filesystems.  The highest existing number is retained
    rather than reusing a deleted segment number.
    """
    parent = Path(os.path.abspath(os.fspath(map_storage_dir)))
    base_name = sanitize_slice_name(cave_name, fallback="Cave")
    highest_segment = 0
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                segment_number = _matching_slice_segment_number(entry.name, base_name)
                if segment_number is not None:
                    highest_segment = max(highest_segment, segment_number)
    except FileNotFoundError:
        # The viewer normally creates and validates the configured storage
        # directory before reaching this policy helper.  Keeping this fallback
        # makes callers that are about to create the directory deterministic.
        pass
    return _format_slice_segment_name(base_name, highest_segment + 1)


def _format_slice_segment_name(base_name: str, segment_number: int) -> str:
    """Append a segment suffix without allowing filename truncation to remove it."""
    if isinstance(segment_number, bool) or not isinstance(segment_number, int):
        raise ValueError("Slice segment number must be an integer")
    if segment_number < 1:
        raise ValueError("Slice segment number must be positive")
    suffix = f"{_SLICE_SEGMENT_SEPARATOR}{segment_number}"
    available_base_length = _SLICE_NAME_MAX_LENGTH - len(suffix)
    if available_base_length < 1:
        raise ValueError("Slice segment number is too large for a portable name")
    trimmed_base_name = base_name[:available_base_length].rstrip(" .-")
    return f"{trimmed_base_name or 'Cave'}{suffix}"


def _matching_slice_segment_number(candidate_name: str, base_name: str) -> int | None:
    """Return a segment number only for a name emitted by this naming policy."""
    _, separator, raw_segment_number = candidate_name.casefold().rpartition(
        _SLICE_SEGMENT_SEPARATOR.casefold()
    )
    if (
        not separator
        or not raw_segment_number.isascii()
        or not raw_segment_number.isdecimal()
    ):
        return None
    try:
        segment_number = int(raw_segment_number)
        expected_name = _format_slice_segment_name(base_name, segment_number)
    except ValueError:
        return None
    if segment_number < 1 or candidate_name.casefold() != expected_name.casefold():
        return None
    return segment_number


def unique_slice_output_dir(
    map_storage_dir: str | os.PathLike[str],
    display_name: str | None,
) -> str:
    """Choose an unused child directory below the configured map storage root."""
    parent = Path(os.path.abspath(os.fspath(map_storage_dir)))
    name = sanitize_slice_name(display_name)
    candidate = parent / name
    suffix = 2
    while os.path.lexists(candidate):
        candidate = parent / f"{name} {suffix}"
        suffix += 1
    return str(candidate)


def export_slice(
    request: SliceExportRequest,
    *,
    progress_cb: ProgressCallback | None = None,
    cancel_requested: CancelCallback | None = None,
) -> SliceExportResult:
    """Copy complete selected chunks into a standalone precompiled-map directory.

    Both the source and output targets use the cache-operation lock.  The
    source lock prevents a cooperative rebuild from replacing chunks while the
    exporter is reading them; the output lock prevents two exports from
    selecting and publishing the same generated map directory.
    """
    _emit_progress(progress_cb, "planning slice", 0.0)
    _raise_if_cancelled(cancel_requested)
    output_parent = os.path.dirname(request.output_dir)
    os.makedirs(output_parent, exist_ok=True)

    with CacheBuildLock(request.source_cache_dir), CacheBuildLock(request.output_dir):
        if os.path.lexists(request.output_dir):
            raise FileExistsError(f"Slice output already exists: {request.output_dir}")
        source_manifest = _validated_source_manifest(request.source_cache_dir)
        source_manifest_digest = canonical_manifest_sha256(source_manifest)
        candidates = _overlapping_source_chunks(source_manifest, request.bounds)
        if not candidates:
            raise ValueError("The selected slice does not overlap any map chunk")
        _ensure_export_capacity(request, source_manifest, candidates)

        staging_dir = tempfile.mkdtemp(
            prefix=f".{os.path.basename(request.output_dir)}.tmp-{os.getpid()}-",
            dir=output_parent,
        )
        try:
            result = _export_in_staging_directory(
                request,
                source_manifest,
                source_manifest_digest,
                candidates,
                staging_dir,
                progress_cb=progress_cb,
                cancel_requested=cancel_requested,
            )
            _raise_if_cancelled(cancel_requested)
            current_source_manifest = _validated_source_manifest(request.source_cache_dir)
            if canonical_manifest_sha256(current_source_manifest) != source_manifest_digest:
                raise RuntimeError("Source map cache changed while the slice was exporting")
            if os.path.lexists(request.output_dir):
                raise FileExistsError(f"Slice output already exists: {request.output_dir}")
            _emit_progress(progress_cb, "publishing slice", 0.98)
            _publish_cache_directory(staging_dir, request.output_dir)
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    _emit_progress(progress_cb, "done", 1.0)
    _LOG.info(
        "Published cave slice %s (%d chunks, %d triangles)",
        request.output_dir,
        result.chunk_count,
        result.triangle_count,
    )
    return result


def validate_slice_source(source_cache_dir: str | os.PathLike[str]) -> Mapping[str, Any]:
    """Validate a precompiled map before arming an interactive slice action."""
    return _validated_source_manifest(os.path.abspath(os.fspath(source_cache_dir)))


def _export_in_staging_directory(
    request: SliceExportRequest,
    source_manifest: Mapping[str, Any],
    source_manifest_digest: str,
    candidates: list[tuple[tuple[int, int, int], Mapping[str, Any]]],
    staging_dir: str,
    *,
    progress_cb: ProgressCallback | None,
    cancel_requested: CancelCallback | None,
) -> SliceExportResult:
    chunks_dir = os.path.join(staging_dir, CHUNKS_DIRNAME)
    os.makedirs(chunks_dir, exist_ok=True)
    output_chunks: dict[str, dict[str, object]] = {}
    used_materials: set[str] = set()
    triangle_count = 0
    footprint_cell_size = _slice_footprint_cell_size(candidates)
    footprint_cells: set[tuple[int, int]] = set()

    for index, (cell, _chunk_info) in enumerate(candidates):
        _raise_if_cancelled(cancel_requested)
        source_chunk_path = _slice_source_chunk_path(request.source_cache_dir, cell)
        chunk_minimum: np.ndarray | None = None
        chunk_maximum: np.ndarray | None = None
        chunk_materials: list[str] = []
        chunk_triangle_count = 0
        for group in iter_chunk_file_groups(
            request.source_cache_dir,
            cell,
            block_vertices=_SLICE_BLOCK_VERTICES,
        ):
            _raise_if_cancelled(cancel_requested)
            positions = np.asarray(group.positions, dtype=np.float32)
            if not len(positions):
                continue
            if len(positions) % 3:
                raise ValueError(
                    f"Slice source chunk {cell!r} is not triangle-aligned"
                )
            if not np.isfinite(positions).all():
                raise ValueError(
                    f"Slice source chunk {cell!r} contains non-finite geometry"
                )
            group_minimum = positions.min(axis=0)
            group_maximum = positions.max(axis=0)
            if chunk_minimum is None:
                chunk_minimum = group_minimum.copy()
                chunk_maximum = group_maximum.copy()
            else:
                np.minimum(chunk_minimum, group_minimum, out=chunk_minimum)
                np.maximum(chunk_maximum, group_maximum, out=chunk_maximum)
            if group.material_name not in chunk_materials:
                chunk_materials.append(group.material_name)
            used_materials.add(group.material_name)
            chunk_triangle_count += len(positions) // 3
            _add_slice_footprint_cells(
                footprint_cells,
                positions,
                cell_size=footprint_cell_size,
            )

        if chunk_minimum is None or chunk_maximum is None or not chunk_triangle_count:
            raise ValueError(f"Slice source chunk {cell!r} contains no triangles")

        # Preserve the source binary exactly. Rewriting or clipping triangles
        # changes wall surfaces, UVs, normals, and group boundaries even when
        # the resulting map remains technically loadable.
        cell_key = f"{cell[0]}_{cell[1]}_{cell[2]}"
        shutil.copy2(source_chunk_path, os.path.join(chunks_dir, f"{cell_key}.bin"))
        output_chunks[cell_key] = {
            "materials": chunk_materials,
            "bounds_min": chunk_minimum.tolist(),
            "bounds_max": chunk_maximum.tolist(),
        }
        triangle_count += chunk_triangle_count
        _emit_progress(
            progress_cb,
            "copying slice chunks",
            0.05 + 0.80 * ((index + 1) / max(len(candidates), 1)),
        )

    if not output_chunks:
        raise ValueError("The selected slice contains no renderable chunks")

    _raise_if_cancelled(cancel_requested)
    material_manifest, texture_assets = _slice_texture_assets(
        request.source_cache_dir,
        source_manifest,
        used_materials,
    )
    marker_name = _slice_marker_name(request, texture_assets)
    marker_payload = json.dumps(
        {
            "format": "caveviewer.slice",
            "schema_version": SLICE_SCHEMA_VERSION,
            "display_name": _slice_display_name(request),
            "root_cave_name": _slice_root_cave_name(request),
            "exporter_version": SLICE_EXPORTER_VERSION,
            "geometry_mode": SLICE_GEOMETRY_MODE,
        },
        allow_nan=False,
        sort_keys=True,
    ).encode("utf-8")
    _emit_progress(progress_cb, "copying slice assets", 0.88)
    _stage_cache_assets(
        staging_dir,
        [
            *texture_assets,
            CacheAsset(relative_path=marker_name, data=marker_payload),
        ],
    )

    slice_metadata = {
        "schema_version": SLICE_SCHEMA_VERSION,
        "parent_manifest_sha256": source_manifest_digest,
        "root_cave_name": _slice_root_cave_name(request),
        **request.bounds.payload(),
        "entry_position": list(request.entry_position),
        "exporter_version": SLICE_EXPORTER_VERSION,
        "geometry_mode": SLICE_GEOMETRY_MODE,
    }
    parent_identity = _parent_identity(source_manifest, source_manifest_digest)
    slice_metadata["parent_identity"] = parent_identity.payload()
    output_manifest: dict[str, Any] = {
        "version": source_manifest["version"],
        "chunk_size": manifest_chunk_size(source_manifest),
        "source_obj": marker_name,
        "mtl_materials": material_manifest,
        "chunks": output_chunks,
        "footprint_cell_size": footprint_cell_size,
        "footprint_cells": _flatten_slice_footprint_cells(footprint_cells),
        "triangle_count": triangle_count,
        SLICE_MANIFEST_KEY: slice_metadata,
    }
    max_upload_group_mb = manifest_max_upload_group_mb(source_manifest)
    if max_upload_group_mb is not None:
        output_manifest["max_upload_group_mb"] = max_upload_group_mb
    output_manifest[GUIDED_DIVE_CACHE_IDENTITY_KEY] = (
        build_derived_guided_dive_cache_identity(
            parent_identity,
            {
                "kind": "caveviewer.slice",
                "schema_version": SLICE_SCHEMA_VERSION,
                "exporter_version": SLICE_EXPORTER_VERSION,
                "geometry_mode": SLICE_GEOMETRY_MODE,
                **request.bounds.payload(),
                "entry_position": list(request.entry_position),
            },
            output_manifest,
        ).payload()
    )
    _raise_if_cancelled(cancel_requested)
    _emit_progress(progress_cb, "writing slice manifest", 0.95)
    with open(os.path.join(staging_dir, MANIFEST_NAME), "w", encoding="utf-8") as file_obj:
        json.dump(output_manifest, file_obj, allow_nan=False, sort_keys=True)

    return SliceExportResult(
        output_dir=request.output_dir,
        triangle_count=triangle_count,
        chunk_count=len(output_chunks),
        texture_count=len(texture_assets),
    )


def _validated_source_manifest(source_cache_dir: str) -> Mapping[str, Any]:
    manifest = load_manifest(source_cache_dir)
    if not isinstance(manifest, Mapping) or manifest.get("version") != 1:
        raise ValueError("Slice source is not a supported precompiled CaveViewer map")
    if manifest_chunk_size(dict(manifest)) is None:
        raise ValueError("Slice source manifest has no valid chunk size")
    chunks = manifest.get("chunks")
    if not isinstance(chunks, Mapping):
        raise ValueError("Slice source manifest has no chunk mapping")
    chunks_dir = os.path.join(source_cache_dir, CHUNKS_DIRNAME)
    if not os.path.isdir(chunks_dir):
        raise ValueError("Slice source map has no chunk directory")
    return manifest


def _overlapping_source_chunks(
    manifest: Mapping[str, Any],
    bounds: SliceBounds,
) -> list[tuple[tuple[int, int, int], Mapping[str, Any]]]:
    chunks = manifest["chunks"]
    selected: list[tuple[tuple[int, int, int], Mapping[str, Any]]] = []
    for raw_cell, raw_info in chunks.items():
        if not isinstance(raw_info, Mapping):
            raise ValueError(f"Slice source chunk {raw_cell!r} has invalid metadata")
        cell = _parse_cell(raw_cell)
        minimum = _finite_vector(raw_info.get("bounds_min"), f"chunk {raw_cell} minimum")
        maximum = _finite_vector(raw_info.get("bounds_max"), f"chunk {raw_cell} maximum")
        if any(maximum[axis] < minimum[axis] for axis in range(3)):
            raise ValueError(f"Slice source chunk {raw_cell!r} has inverted bounds")
        if bounds.overlaps(np.asarray(minimum), np.asarray(maximum)):
            selected.append((cell, raw_info))
    return sorted(selected, key=lambda item: item[0])


def _slice_source_chunk_path(
    source_cache_dir: str,
    cell: tuple[int, int, int],
) -> str:
    """Return one validated source-chunk path contained by the parent cache."""
    cache_root = os.path.realpath(source_cache_dir)
    source_path = os.path.realpath(
        os.path.join(
            cache_root,
            CHUNKS_DIRNAME,
            f"{cell[0]}_{cell[1]}_{cell[2]}.bin",
        )
    )
    try:
        contained = os.path.commonpath((cache_root, source_path)) == cache_root
    except ValueError:
        contained = False
    if not contained or not os.path.isfile(source_path):
        raise ValueError(f"Slice source chunk is unavailable: {source_path}")
    return source_path


def _slice_footprint_cell_size(
    candidates: list[tuple[tuple[int, int, int], Mapping[str, Any]]],
) -> float:
    """Choose the same bounded minimap resolution used by normal imports."""
    minimum_x = minimum_z = math.inf
    maximum_x = maximum_z = -math.inf
    for cell, info in candidates:
        minimum = _finite_vector(info.get("bounds_min"), f"chunk {cell} minimum")
        maximum = _finite_vector(info.get("bounds_max"), f"chunk {cell} maximum")
        minimum_x = min(minimum_x, minimum[0])
        minimum_z = min(minimum_z, minimum[2])
        maximum_x = max(maximum_x, maximum[0])
        maximum_z = max(maximum_z, maximum[2])
    extent = max(maximum_x - minimum_x, maximum_z - minimum_z, 1.0)
    return max(_MIN_FOOTPRINT_CELL_SIZE, extent / _FOOTPRINT_TARGET_CELLS)


def _add_slice_footprint_cells(
    footprint_cells: set[tuple[int, int]],
    positions: np.ndarray,
    *,
    cell_size: float,
) -> None:
    """Accumulate detailed top-down occupancy from one bounded vertex block."""
    coordinates = np.floor(positions[:, [0, 2]] / cell_size).astype(np.int64)
    footprint_cells.update(
        (int(cell_x), int(cell_z))
        for cell_x, cell_z in np.unique(coordinates, axis=0).tolist()
    )


def _flatten_slice_footprint_cells(
    footprint_cells: set[tuple[int, int]],
) -> list[int]:
    """Return deterministic flat X/Z pairs accepted by the normal minimap."""
    flattened: list[int] = []
    for cell_x, cell_z in sorted(footprint_cells):
        flattened.extend((cell_x, cell_z))
    return flattened


def _ensure_export_capacity(
    request: SliceExportRequest,
    source_manifest: Mapping[str, Any],
    candidates: list[tuple[tuple[int, int, int], Mapping[str, Any]]],
) -> None:
    """Reject an obviously impossible export before creating staging output."""
    candidate_bytes = 0
    candidate_materials: set[str] = set()
    for cell, _info in candidates:
        path = _slice_source_chunk_path(request.source_cache_dir, cell)
        try:
            candidate_bytes += os.path.getsize(path)
        except OSError as exc:
            raise ValueError(f"Slice source chunk is unavailable: {path}") from exc
        raw_material_names = _info.get("materials")
        if isinstance(raw_material_names, list):
            candidate_materials.update(
                str(name) for name in raw_material_names if isinstance(name, str)
            )
    raw_materials = source_manifest.get("mtl_materials")
    material_textures = raw_materials if isinstance(raw_materials, Mapping) else {}
    texture_bytes = 0
    seen_texture_paths: set[str] = set()
    for material_name in candidate_materials:
        raw_texture = material_textures.get(material_name)
        if raw_texture is None:
            continue
        if not isinstance(raw_texture, str):
            raise ValueError(
                f"Slice source material {material_name!r} has an invalid texture path"
            )
        relative_path = os.path.normpath(raw_texture)
        if relative_path in seen_texture_paths:
            continue
        seen_texture_paths.add(relative_path)
        texture_path = _safe_cache_asset_source_path(
            request.source_cache_dir,
            relative_path,
        )
        try:
            texture_bytes += os.path.getsize(texture_path)
        except OSError as exc:
            raise ValueError(
                f"Slice source texture for material {material_name!r} is missing: "
                f"{relative_path}"
            ) from exc
    available_bytes = shutil.disk_usage(_nearest_existing_directory(os.path.dirname(request.output_dir))).free
    required_bytes = candidate_bytes + texture_bytes + _SLICE_MANIFEST_RESERVE_BYTES
    if available_bytes < required_bytes:
        raise OSError(
            "Insufficient free space for a slice staging export; "
            f"need at least {required_bytes} bytes, have {available_bytes} bytes"
        )


def _slice_texture_assets(
    source_cache_dir: str,
    source_manifest: Mapping[str, Any],
    used_materials: set[str],
) -> tuple[dict[str, str | None], list[CacheAsset]]:
    raw_materials = source_manifest.get("mtl_materials")
    materials = raw_materials if isinstance(raw_materials, Mapping) else {}
    output_materials: dict[str, str | None] = {}
    assets_by_relative_path: dict[str, CacheAsset] = {}
    for material_name in sorted(used_materials):
        raw_texture = materials.get(material_name)
        if raw_texture is None:
            output_materials[material_name] = None
            continue
        if not isinstance(raw_texture, str):
            raise ValueError(
                f"Slice source material {material_name!r} has an invalid texture path"
            )
        relative_path = os.path.normpath(raw_texture)
        source_texture_path = _safe_cache_asset_source_path(source_cache_dir, relative_path)
        if not os.path.isfile(source_texture_path):
            raise ValueError(
                f"Slice source texture for material {material_name!r} is missing: "
                f"{relative_path}"
            )
        output_materials[material_name] = relative_path
        assets_by_relative_path.setdefault(
            relative_path,
            CacheAsset(relative_path=relative_path, source_path=source_texture_path),
        )
    return output_materials, list(assets_by_relative_path.values())


def _safe_cache_asset_source_path(cache_dir: str, relative_path: str) -> str:
    resolved = resolve_texture_path(cache_dir, relative_path)
    cache_root = os.path.realpath(cache_dir)
    resolved_real_path = os.path.realpath(resolved)
    try:
        inside_cache = os.path.commonpath((cache_root, resolved_real_path)) == cache_root
    except ValueError:
        inside_cache = False
    if not inside_cache:
        raise ValueError(f"Unsafe texture path: {relative_path!r}")
    return resolved


def _slice_marker_name(request: SliceExportRequest, assets: list[CacheAsset]) -> str:
    base_name = sanitize_slice_name(_slice_display_name(request)).replace("/", "-")
    existing = {os.path.normpath(asset.relative_path) for asset in assets}
    marker_name = f"{base_name}{SLICE_MARKER_SUFFIX}"
    suffix = 2
    while marker_name in existing:
        marker_name = f"{base_name}-{suffix}{SLICE_MARKER_SUFFIX}"
        suffix += 1
    return marker_name


def _slice_display_name(request: SliceExportRequest) -> str:
    return sanitize_slice_name(request.display_name or os.path.basename(request.output_dir))


def _slice_root_cave_name(request: SliceExportRequest) -> str:
    """Return the original cave label retained when a slice is sliced again."""
    return sanitize_slice_name(
        request.root_cave_name or _slice_display_name(request),
        fallback="Cave",
    )


def _parent_identity(
    source_manifest: Mapping[str, Any],
    source_manifest_digest: str,
) -> GuidedDiveCacheIdentity:
    identity = guided_dive_cache_identity_from_manifest(source_manifest)
    if identity is not None:
        return identity
    # Legacy cache-only maps did not always record an identity.  A canonical
    # manifest digest still gives their slices a stable, isolated identity.
    return GuidedDiveCacheIdentity(
        version=1,
        source_sha256=source_manifest_digest,
        cache_manifest_sha256=source_manifest_digest,
    )


def _parse_cell(raw_cell: object) -> tuple[int, int, int]:
    parts = str(raw_cell).replace(",", "_").split("_")
    if len(parts) != 3:
        raise ValueError(f"Invalid source chunk key: {raw_cell!r}")
    try:
        return tuple(int(part) for part in parts)  # type: ignore[return-value]
    except ValueError as exc:
        raise ValueError(f"Invalid source chunk key: {raw_cell!r}") from exc


def _finite_vector(value: object, label: str) -> tuple[float, float, float]:
    if value is None:
        raise ValueError(f"{label} must contain three finite coordinates")
    try:
        vector = tuple(float(component) for component in value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain three finite coordinates") from exc
    if len(vector) != 3 or not all(math.isfinite(component) for component in vector):
        raise ValueError(f"{label} must contain three finite coordinates")
    return vector  # type: ignore[return-value]


def _point_inside_bounds(point: tuple[float, float, float], bounds: SliceBounds) -> bool:
    return all(
        bounds.minimum[index] - _BOUNDS_EPSILON
        <= point[index]
        <= bounds.maximum[index] + _BOUNDS_EPSILON
        for index in range(3)
    )


def _nearest_existing_directory(path: str) -> str:
    candidate = os.path.abspath(path)
    while not os.path.isdir(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return candidate


def _paths_overlap(first: str, second: str) -> bool:
    """Return whether one filesystem path contains the other.

    Publishing an output over an ancestor of its source could atomically move
    the source cache aside.  Resolve existing symlink ancestors before this
    check so an apparently separate output path cannot evade the guard.
    """
    first_real_path = os.path.realpath(first)
    second_real_path = os.path.realpath(second)
    try:
        common = os.path.commonpath((first_real_path, second_real_path))
    except ValueError:
        return False
    return common == first_real_path or common == second_real_path


def _emit_progress(
    progress_cb: ProgressCallback | None,
    stage: str,
    fraction: float,
) -> None:
    if progress_cb is not None:
        progress_cb(stage, max(0.0, min(1.0, float(fraction))))


def _raise_if_cancelled(cancel_requested: CancelCallback | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise SliceExportCancelled("Slice export canceled")
