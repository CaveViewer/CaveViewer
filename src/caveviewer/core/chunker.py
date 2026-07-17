"""
caveviewer.core.chunker

Spatial partitioning of a parsed mesh into a 3D grid of chunks, cached to
disk in a fast-to-load binary format. This is the piece that makes large
cave maps viewable: instead of one giant draw call / VRAM blob for the
whole cave, we split the mesh into cells (default 50m cubes -- tune via
CHUNK_SIZE for your cave's scale) and load only the cells near the camera
at runtime (see caveviewer.core.streaming_world).

Cache layout on disk, under the selected managed cache directory:
    manifest.json          - chunk grid metadata, bounds, cell size,
                              chunk_id -> required texture list, etc.
    chunks/<cx>_<cy>_<cz>.bin
                              - one binary blob per occupied cell:
                                packed positions/uvs/normals/indices,
                                grouped by material so each chunk can issue
                                one draw call per texture it touches.

A face is assigned to a cell based on its centroid. A mesh face never
spans multiple chunks even if its vertices are near a boundary -- this
intentionally avoids vertex splitting complexity. Cracks at chunk seams
are visually negligible at cave scale and the chunk overlap-load radius
(loading neighbor rings, not just the current cell) means seams are never
at the camera's center of attention for long.
"""

from __future__ import annotations

import concurrent.futures
import errno
import gc
import json
import os
import shutil
import struct
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from caveviewer.core import hardware_memory
from caveviewer.core.logging_utils import get_logger
from caveviewer.core.worker_config import (
    MAX_WORKER_RAM_UTILIZATION,
    can_start_additional_worker,
    describe_worker_target,
    resolve_worker_allocation,
)
from caveviewer.core.obj_parser import (
    RawMesh,
    MaterialRange,
    ObjFaceBatch,
    ObjVertexData,
    iter_obj_face_batches,
    parse_obj_vertices,
)
from caveviewer.core.cache_paths import (
    map_cache_build_dir,
    map_cache_candidates,
)

MANIFEST_NAME = "manifest.json"
CHUNKS_DIRNAME = "chunks"
IMPORT_RESUME_MANIFEST_NAME = "import_resume.json"
IMPORT_DISK_SPACE_MULTIPLIER = 2
IMPORT_MEMORY_HEADROOM_FRACTION = 0.90
IMPORT_MEMORY_PHYSICAL_OVERCOMMIT_FRACTION = 1.25
IMPORT_MEMORY_FIXED_OVERHEAD_BYTES = 256 * 1024 ** 2

CHUNK_SIZE_ENV_VAR = "CAVEVIEWER_CHUNK_SIZE_METERS"
OBJ_IMPORT_BATCH_FACES_ENV_VAR = "CAVEVIEWER_OBJ_IMPORT_BATCH_FACES"
OBJ_BUCKET_WORKERS_ENV_VAR = "CAVEVIEWER_OBJ_BUCKET_WORKERS"
_DEFAULT_CHUNK_SIZE_FALLBACK = 50.0  # meters; default for new cache builds
_DEFAULT_OBJ_IMPORT_BATCH_FACES = 200_000
_DEFAULT_OBJ_BUCKET_WORKERS = 2
_MAX_OBJ_BUCKET_WORKERS = 32
_INCREMENTAL_OBJ_RESUME_VERSION = 1
_OBJ_BUCKET_RECORD_SLICE_FACES = 25_000
_OBJ_BUCKET_FINALIZE_BLOCK_RECORDS = 100_000
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

_MAGIC = b"CVCH"  # CaveViewer CHunk
_VERSION = 1


class InsufficientDiskSpaceError(OSError):
    """Raised before cache creation when the source disk lacks headroom."""

    def __init__(
        self,
        source_path: str,
        required_bytes: int,
        available_bytes: int,
        *,
        source_size: int | None = None,
        staged_asset_bytes: int = 0,
    ):
        self.source_path = source_path
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        source_size = (
            required_bytes // IMPORT_DISK_SPACE_MULTIPLIER
            if source_size is None
            else source_size
        )
        message = (
            f"Not enough disk space to import {os.path.basename(source_path)!r}: "
            f"{available_bytes:,} bytes are available, but at least "
            f"{required_bytes:,} bytes are required (twice the "
            f"{source_size:,}-byte map size plus {staged_asset_bytes:,} bytes "
            "of cache assets). Free disk space and try again."
        )
        super().__init__(errno.ENOSPC, message)


class InsufficientImportMemoryError(MemoryError):
    """Raised before large import allocations when RAM headroom is insufficient."""

    def __init__(
        self,
        required_bytes: int,
        available_bytes: int,
        allowed_bytes: int,
        *,
        source_path: str | None = None,
        physical_limit_bytes: int | None = None,
    ):
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        self.allowed_bytes = allowed_bytes
        self.physical_limit_bytes = physical_limit_bytes
        self.source_path = source_path
        source_label = (
            os.path.basename(source_path)
            if source_path
            else "selected map"
        )
        message = (
            f"Not enough available system RAM to import {source_label!r}: "
            f"the estimated peak import footprint is "
            f"{required_bytes / (1024 ** 3):.1f} GB, while "
            f"{available_bytes / (1024 ** 3):.1f} GB is currently available "
            f"({allowed_bytes / (1024 ** 3):.1f} GB after the "
            f"{IMPORT_MEMORY_HEADROOM_FRACTION:.0%} safety limit)"
            + (
                f"; this also exceeds the "
                f"{physical_limit_bytes / (1024 ** 3):.1f} GB physical-memory "
                "overcommit allowance. "
                if physical_limit_bytes is not None
                else ". "
            )
            + "Close other memory-heavy applications and try again."
        )
        super().__init__(message)


class ImportPaused(RuntimeError):
    """Raised internally when a resumable import checkpoint has been saved."""

    def __init__(self, resume_dir: str | None = None):
        self.resume_dir = resume_dir
        message = "Import paused; resume checkpoint saved."
        if resume_dir:
            message = f"{message} Resume directory: {resume_dir}"
        super().__init__(message)


@dataclass(frozen=True)
class CacheAsset:
    """One texture or other immutable asset published with a map cache."""

    relative_path: str
    source_path: str | None = None
    data: bytes | None = None

    def __post_init__(self) -> None:
        if (self.source_path is None) == (self.data is None):
            raise ValueError(
                "CacheAsset requires exactly one source_path or data value"
            )


def configured_chunk_size() -> float:
    """Return the chunk size currently used by default for cache builds."""
    return DEFAULT_CHUNK_SIZE


def _configured_obj_import_batch_faces(environ: dict[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = env.get(OBJ_IMPORT_BATCH_FACES_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_OBJ_IMPORT_BATCH_FACES
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError("must be > 0")
        return max(1_000, min(2_000_000, value))
    except Exception:
        _LOG.warning(
            "Ignoring invalid %s=%r; using default %d faces per batch.",
            OBJ_IMPORT_BATCH_FACES_ENV_VAR,
            raw,
            _DEFAULT_OBJ_IMPORT_BATCH_FACES,
        )
        return _DEFAULT_OBJ_IMPORT_BATCH_FACES


def _configured_obj_bucket_workers(environ: dict[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = env.get(OBJ_BUCKET_WORKERS_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_OBJ_BUCKET_WORKERS
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError("must be > 0")
        return max(1, min(_MAX_OBJ_BUCKET_WORKERS, value))
    except Exception:
        _LOG.warning(
            "Ignoring invalid %s=%r; using default %d bucket workers.",
            OBJ_BUCKET_WORKERS_ENV_VAR,
            raw,
            _DEFAULT_OBJ_BUCKET_WORKERS,
        )
        return _DEFAULT_OBJ_BUCKET_WORKERS


def estimate_import_memory_bytes(
    vertex_count: int,
    uv_count: int,
    normal_count: int,
    face_count: int,
) -> int:
    """Estimate peak RAM needed for OBJ/GLB parse arrays plus chunking temps."""
    vertex_count = max(0, int(vertex_count))
    uv_count = max(0, int(uv_count))
    normal_count = max(0, int(normal_count))
    face_count = max(0, int(face_count))

    mesh_array_bytes = (
        vertex_count * 3 * 4
        + uv_count * 2 * 4
        + normal_count * 3 * 4
        + face_count * 3 * 4 * 3
    )
    chunking_temp_bytes = face_count * (
        3 * 4  # cell_coords
        + 4    # face_material_id
        + 8    # cell_key / combined_key
        + 8    # material_key
        + 8    # order
        + 8    # sorted_keys
    )
    return int(
        (mesh_array_bytes + chunking_temp_bytes) * 1.20
        + IMPORT_MEMORY_FIXED_OVERHEAD_BYTES
    )


def estimate_incremental_import_memory_bytes(
    vertex_count: int,
    uv_count: int,
    normal_count: int,
    face_batch_size: int | None = None,
    bucket_workers: int | None = None,
) -> int:
    """Estimate peak RAM for the incremental OBJ importer."""
    vertex_count = max(0, int(vertex_count))
    uv_count = max(0, int(uv_count))
    normal_count = max(0, int(normal_count))
    face_batch_size = max(
        1,
        int(
            _DEFAULT_OBJ_IMPORT_BATCH_FACES
            if face_batch_size is None
            else face_batch_size
        ),
    )
    bucket_workers = max(
        1,
        int(
            _DEFAULT_OBJ_BUCKET_WORKERS
            if bucket_workers is None
            else bucket_workers
        ),
    )

    attribute_bytes = (
        vertex_count * 3 * 4
        + uv_count * 2 * 4
        + normal_count * 3 * 4
    )
    batch_index_bytes = face_batch_size * 3 * 4 * 3
    batch_payload_bytes = face_batch_size * 3 * 8 * 4
    batch_sort_bytes = face_batch_size * (8 + 8 + 4)
    per_worker_batch_bytes = (
        batch_index_bytes + batch_payload_bytes + batch_sort_bytes
    )
    return int(
        (attribute_bytes + per_worker_batch_bytes * bucket_workers) * 1.35
        + IMPORT_MEMORY_FIXED_OVERHEAD_BYTES
    )


def ensure_sufficient_incremental_import_memory(
    vertex_count: int,
    uv_count: int,
    normal_count: int,
    face_count: int,
    *,
    source_path: str | None = None,
    face_batch_size: int | None = None,
    bucket_workers: int | None = None,
) -> None:
    """Reject incremental imports whose estimated peak footprint exceeds RAM."""
    del face_count  # face count affects runtime/disk work, not peak batch RAM.
    snapshot = hardware_memory.detect_ram_snapshot()
    if snapshot is None:
        _LOG.warning(
            "Could not measure available system RAM before incremental import "
            "allocation; continuing without import RAM preflight."
        )
        return

    required_bytes = estimate_incremental_import_memory_bytes(
        vertex_count,
        uv_count,
        normal_count,
        face_batch_size,
        bucket_workers=bucket_workers,
    )
    available_bytes = max(0, int(snapshot.available_bytes))
    allowed_bytes = int(available_bytes * IMPORT_MEMORY_HEADROOM_FRACTION)
    physical_overcommit_limit_bytes = int(
        snapshot.total_bytes * IMPORT_MEMORY_PHYSICAL_OVERCOMMIT_FRACTION
    )
    if required_bytes > physical_overcommit_limit_bytes:
        raise InsufficientImportMemoryError(
            required_bytes,
            available_bytes,
            allowed_bytes,
            source_path=source_path,
            physical_limit_bytes=physical_overcommit_limit_bytes,
        )

    if required_bytes > allowed_bytes:
        _LOG.warning(
            "Incremental import RAM preflight warning for %s: estimated %.1f GB "
            "peak; %.1f GB currently available (%.1f GB after the %.0f%% safety "
            "limit). Continuing because the estimate is within the %.1f GB "
            "physical-memory overcommit allowance.",
            os.path.basename(source_path) if source_path else "selected map",
            required_bytes / (1024 ** 3),
            available_bytes / (1024 ** 3),
            allowed_bytes / (1024 ** 3),
            IMPORT_MEMORY_HEADROOM_FRACTION * 100.0,
            physical_overcommit_limit_bytes / (1024 ** 3),
        )


def ensure_sufficient_import_memory(
    vertex_count: int,
    uv_count: int,
    normal_count: int,
    face_count: int,
    *,
    source_path: str | None = None,
) -> None:
    """Reject imports whose estimated peak footprint exceeds available RAM."""
    snapshot = hardware_memory.detect_ram_snapshot()
    if snapshot is None:
        _LOG.warning(
            "Could not measure available system RAM before import allocation; "
            "continuing without import RAM preflight."
        )
        return

    required_bytes = estimate_import_memory_bytes(
        vertex_count,
        uv_count,
        normal_count,
        face_count,
    )
    available_bytes = max(0, int(snapshot.available_bytes))
    allowed_bytes = int(available_bytes * IMPORT_MEMORY_HEADROOM_FRACTION)
    physical_overcommit_limit_bytes = int(
        snapshot.total_bytes * IMPORT_MEMORY_PHYSICAL_OVERCOMMIT_FRACTION
    )
    if required_bytes > physical_overcommit_limit_bytes:
        raise InsufficientImportMemoryError(
            required_bytes,
            available_bytes,
            allowed_bytes,
            source_path=source_path,
            physical_limit_bytes=physical_overcommit_limit_bytes,
        )

    # Available RAM is a moving target, especially on macOS where inactive
    # pages, compression, and swap can make a previously successful import
    # look unsafe at one instant. Treat low current availability as a warning
    # unless the estimate is also beyond the physical-memory envelope above.
    if required_bytes > allowed_bytes:
        _LOG.warning(
            "Import RAM preflight warning for %s: estimated %.1f GB peak; "
            "%.1f GB currently available (%.1f GB after the %.0f%% safety "
            "limit). Continuing because the estimate is within the %.1f GB "
            "physical-memory overcommit allowance.",
            os.path.basename(source_path) if source_path else "selected map",
            required_bytes / (1024 ** 3),
            available_bytes / (1024 ** 3),
            allowed_bytes / (1024 ** 3),
            IMPORT_MEMORY_HEADROOM_FRACTION * 100.0,
            physical_overcommit_limit_bytes / (1024 ** 3),
        )
        return

    _LOG.info(
        "Import RAM preflight passed for %s: estimated %.1f GB peak; "
        "%.1f GB available.",
        os.path.basename(source_path) if source_path else "selected map",
        required_bytes / (1024 ** 3),
        available_bytes / (1024 ** 3),
    )


@dataclass
class ChunkUploadGroup:
    """CPU-side source data for one material group in a chunk.

    OpenGL object creation still has to happen on the render thread, but
    the expensive chunk-file decode happens in a streaming worker before the
    chunk reaches the renderer.  A worker may prepack one vertex-byte payload
    for the shade mode expected at upload time; the source arrays remain
    available so a late SHADE toggle can still fall back to building the other
    mode correctly.
    """
    material_name: str
    positions: np.ndarray
    uvs: np.ndarray
    smooth_normals: np.ndarray
    prepacked_vertex_bytes: bytes | None = None
    prepacked_smooth_shading: bool | None = None

    def prepack_vertex_bytes(self, *, smooth_shading: bool) -> None:
        self.prepacked_vertex_bytes = vertex_bytes_for_shading(
            self.positions,
            self.uvs,
            self.smooth_normals,
            smooth_shading=smooth_shading,
        )
        self.prepacked_smooth_shading = bool(smooth_shading)

    def has_prepacked_vertex_bytes(self, *, smooth_shading: bool) -> bool:
        return (
            self.prepacked_vertex_bytes is not None
            and self.prepacked_smooth_shading == bool(smooth_shading)
        )

    def vertex_bytes(self, *, smooth_shading: bool) -> bytes:
        if self.has_prepacked_vertex_bytes(smooth_shading=smooth_shading):
            return self.prepacked_vertex_bytes
        return vertex_bytes_for_shading(
            self.positions,
            self.uvs,
            self.smooth_normals,
            smooth_shading=smooth_shading,
        )

    @property
    def smooth_vertex_bytes(self) -> bytes:
        return self.vertex_bytes(smooth_shading=True)

    @property
    def flat_vertex_bytes(self) -> bytes:
        return self.vertex_bytes(smooth_shading=False)


@dataclass
class ChunkMaterialGroup:
    material_name: str
    positions: np.ndarray   # (N, 3) float32, flat (already expanded, not indexed)
    uvs: np.ndarray         # (N, 2) float32
    normals: np.ndarray     # (N, 3) float32


@dataclass
class ChunkData:
    """In-memory representation of one spatial cell's geometry, grouped by
    material so the renderer can do one draw call per texture per chunk."""
    cell: tuple[int, int, int]
    groups: dict[str, ChunkMaterialGroup]
    bounds_min: np.ndarray  # (3,) float32
    bounds_max: np.ndarray  # (3,) float32
    upload_groups: list[ChunkUploadGroup] | None = None


def world_to_cell(point: np.ndarray, chunk_size: float) -> tuple[int, int, int]:
    return tuple(np.floor(point / chunk_size).astype(np.int64).tolist())


def ensure_sufficient_disk_space(
    source_path: str,
    cache_dir: str | None = None,
    *,
    staged_asset_bytes: int = 0,
) -> None:
    """Require free space equal to at least twice the source map size.

    Cache construction expands indexed source geometry into render-ready
    chunks, so starting an import without this headroom is likely to fail
    after doing substantial work. The check targets the filesystem that will
    hold the cache, which may be an XDG cache filesystem rather than the
    source-map filesystem.
    """
    source_path = os.path.abspath(source_path)
    source_size = os.path.getsize(source_path)
    required_bytes = (
        source_size * IMPORT_DISK_SPACE_MULTIPLIER + staged_asset_bytes
    )
    target_dir = (
        os.path.dirname(os.path.abspath(cache_dir))
        if cache_dir
        else os.path.dirname(source_path)
    )
    available_bytes = shutil.disk_usage(_nearest_existing_directory(target_dir)).free
    if available_bytes < required_bytes:
        raise InsufficientDiskSpaceError(
            source_path,
            required_bytes,
            available_bytes,
            source_size=source_size,
            staged_asset_bytes=staged_asset_bytes,
        )


def build_cache(
    obj_path: str,
    mesh: RawMesh,
    materials: dict,
    chunk_size: float = DEFAULT_CHUNK_SIZE,
    progress_cb=None,
    *,
    cache_dir: str | None = None,
    assets: tuple[CacheAsset, ...] | list[CacheAsset] = (),
) -> str:
    """
    Partition `mesh` into spatial chunks and atomically publish the cache.

    ``cache_dir`` defaults to the managed location selected by
    ``cache_paths``. Assets are staged inside the same private directory, so the
    manifest can never become visible before all referenced textures.

    progress_cb(stage: str, fraction: float)
    """
    cache_dir = os.path.abspath(cache_dir or map_cache_build_dir(obj_path))
    cache_parent = os.path.dirname(cache_dir)
    assets = tuple(assets)
    ensure_sufficient_disk_space(
        obj_path,
        cache_dir,
        staged_asset_bytes=sum(_cache_asset_size(asset) for asset in assets),
    )
    os.makedirs(cache_parent, exist_ok=True)

    staging_dir = tempfile.mkdtemp(
        prefix=f".{os.path.basename(cache_dir)}.tmp-{os.getpid()}-",
        dir=cache_parent,
    )
    try:
        _stage_cache_assets(staging_dir, assets)
        _build_cache_in_directory(
            obj_path,
            mesh,
            materials,
            staging_dir,
            chunk_size=chunk_size,
            progress_cb=progress_cb,
        )
        _publish_cache_directory(staging_dir, cache_dir)
    except BaseException:
        # In particular, ENOSPC can be raised after several worker writes.
        # Removing the private staging tree guarantees a failed build never
        # leaves partial chunks looking like a usable cache.
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    if progress_cb:
        progress_cb("done", 1.0)

    return cache_dir


def build_cache_incremental_obj(
    obj_path: str,
    materials: dict,
    chunk_size: float = DEFAULT_CHUNK_SIZE,
    progress_cb=None,
    *,
    cache_dir: str | None = None,
    assets: tuple[CacheAsset, ...] | list[CacheAsset] = (),
    face_batch_size: int | None = None,
    bucket_workers: int | None = None,
    pause_requested: Callable[[], bool] | None = None,
) -> str:
    """
    Build a cache from an OBJ without retaining whole-model face arrays.

    The importer keeps vertex attributes in memory, streams triangulated faces
    in bounded batches, spills de-indexed vertex payloads into temporary
    per-cell/material bucket files, then finalizes those buckets into the same
    chunk binary format used by ``build_cache``.
    """
    cache_dir = os.path.abspath(cache_dir or map_cache_build_dir(obj_path))
    cache_parent = os.path.dirname(cache_dir)
    assets = tuple(assets)
    resolved_face_batch_size = (
        _configured_obj_import_batch_faces()
        if face_batch_size is None
        else max(1, int(face_batch_size))
    )
    resolved_bucket_workers = (
        _configured_obj_bucket_workers()
        if bucket_workers is None
        else max(1, min(_MAX_OBJ_BUCKET_WORKERS, int(bucket_workers)))
    )
    ensure_sufficient_disk_space(
        obj_path,
        cache_dir,
        staged_asset_bytes=sum(_cache_asset_size(asset) for asset in assets),
    )
    os.makedirs(cache_parent, exist_ok=True)

    resume = _find_incremental_obj_resume(
        cache_dir,
        obj_path=obj_path,
        materials=materials,
        chunk_size=chunk_size,
        face_batch_size=resolved_face_batch_size,
    )
    if resume is None:
        staging_dir = tempfile.mkdtemp(
            prefix=f".{os.path.basename(cache_dir)}.tmp-{os.getpid()}-",
            dir=cache_parent,
        )
        resume_checkpoint = None
        is_resuming = False
    else:
        staging_dir, resume_checkpoint = resume
        is_resuming = True
        _LOG.info(
            "Resuming previously paused OBJ import from checkpoint: %s "
            "(stage=%s, progress=%.0f%%).",
            staging_dir,
            resume_checkpoint.get("stage", "unknown"),
            float(resume_checkpoint.get("progress_fraction", 0.0)) * 100.0,
        )
        if progress_cb:
            progress_cb(
                "resuming import",
                float(resume_checkpoint.get("progress_fraction", 0.0)),
            )

    try:
        if not is_resuming:
            _stage_cache_assets(staging_dir, assets)
        _build_incremental_obj_cache_in_directory(
            obj_path,
            materials,
            staging_dir,
            chunk_size=chunk_size,
            progress_cb=progress_cb,
            face_batch_size=resolved_face_batch_size,
            bucket_workers=resolved_bucket_workers,
            pause_requested=pause_requested,
            resume_checkpoint=resume_checkpoint,
        )
        _remove_resume_checkpoint(staging_dir)
        _publish_cache_directory(staging_dir, cache_dir)
    except ImportPaused as paused:
        resume_dir = _preserve_resumable_import(staging_dir, cache_dir)
        paused.resume_dir = resume_dir
        _LOG.info("Paused OBJ import checkpoint saved in: %s", resume_dir)
        raise
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    if progress_cb:
        progress_cb("done", 1.0)

    return cache_dir


def _nearest_existing_directory(path: str) -> str:
    """Find the filesystem that will contain a not-yet-created cache path."""
    candidate = os.path.abspath(path)
    while not os.path.isdir(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return candidate


def _cache_asset_size(asset: CacheAsset) -> int:
    if asset.source_path is not None:
        return os.path.getsize(asset.source_path)
    return len(asset.data or b"")


def cache_assets_size(assets: tuple[CacheAsset, ...] | list[CacheAsset]) -> int:
    """Return the total bytes that cache assets will add to staging."""
    return sum(_cache_asset_size(asset) for asset in assets)


def _stage_cache_assets(
    staging_dir: str, assets: tuple[CacheAsset, ...] | list[CacheAsset]
) -> None:
    """Write validated relative assets inside an unpublished cache tree."""
    written_paths: set[str] = set()
    for asset in assets:
        relative_path = os.path.normpath(asset.relative_path)
        first_component = relative_path.split(os.sep, 1)[0]
        if (
            not relative_path
            or os.path.isabs(relative_path)
            or relative_path == os.pardir
            or relative_path.startswith(os.pardir + os.sep)
            or first_component in {CHUNKS_DIRNAME, MANIFEST_NAME}
        ):
            raise ValueError(f"Unsafe cache asset path: {asset.relative_path!r}")
        if relative_path in written_paths:
            raise ValueError(f"Duplicate cache asset path: {asset.relative_path!r}")
        written_paths.add(relative_path)

        destination = os.path.join(staging_dir, relative_path)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if asset.source_path is not None:
            shutil.copy2(asset.source_path, destination)
        else:
            with open(destination, "wb") as output:
                output.write(asset.data or b"")


def _publish_cache_directory(staging_dir: str, cache_dir: str) -> None:
    """Publish a completed staging tree while preserving an old cache on failure."""
    backup_dir = f"{staging_dir}.previous"
    moved_existing_cache = False

    try:
        if os.path.lexists(cache_dir):
            os.replace(cache_dir, backup_dir)
            moved_existing_cache = True
        os.replace(staging_dir, cache_dir)
    except BaseException:
        if moved_existing_cache:
            try:
                os.replace(backup_dir, cache_dir)
            except OSError as restore_error:
                _LOG.error(
                    "Could not restore previous cache %s after publish failure: %s",
                    cache_dir,
                    restore_error,
                )
        raise

    if moved_existing_cache:
        try:
            shutil.rmtree(backup_dir)
        except OSError as cleanup_error:
            _LOG.warning(
                "Could not remove replaced cache backup %s: %s",
                backup_dir,
                cleanup_error,
            )


def _import_resume_prefix(cache_dir: str) -> str:
    return f".{os.path.basename(cache_dir)}.resume-"


def _import_resume_checkpoint_path(staging_dir: str) -> str:
    return os.path.join(staging_dir, IMPORT_RESUME_MANIFEST_NAME)


def _source_resume_identity(source_path: str) -> dict:
    stat = os.stat(source_path)
    return {
        "path": os.path.abspath(source_path),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _materials_resume_identity(materials: dict) -> dict[str, str | None]:
    return {
        str(name): getattr(material, "diffuse_texture", None)
        for name, material in sorted(materials.items(), key=lambda item: str(item[0]))
    }


def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=os.path.dirname(path),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _serialize_bucket_parts(
    bucket_parts: dict[tuple[tuple[int, int, int], str], list[str]],
    root_dir: str,
) -> list[dict]:
    serialized = []
    for (cell, material_name), paths in sorted(
        bucket_parts.items(),
        key=lambda item: (item[0][0], item[0][1]),
    ):
        serialized.append(
            {
                "cell": [int(cell[0]), int(cell[1]), int(cell[2])],
                "material": str(material_name),
                "paths": [
                    os.path.relpath(path, root_dir)
                    for path in paths
                ],
            }
        )
    return serialized


def _deserialize_bucket_parts(
    payload: list[dict],
    root_dir: str,
) -> dict[tuple[tuple[int, int, int], str], list[str]]:
    bucket_parts: dict[tuple[tuple[int, int, int], str], list[str]] = {}
    for item in payload:
        cell_payload = item.get("cell", [])
        if len(cell_payload) != 3:
            raise ValueError("Resume checkpoint contains an invalid bucket cell.")
        cell = (
            int(cell_payload[0]),
            int(cell_payload[1]),
            int(cell_payload[2]),
        )
        material_name = str(item.get("material", "__no_material__"))
        paths = [
            os.path.normpath(os.path.join(root_dir, str(relative_path)))
            for relative_path in item.get("paths", [])
        ]
        bucket_parts[(cell, material_name)] = paths
    return bucket_parts


def _write_incremental_obj_resume_checkpoint(
    staging_dir: str,
    *,
    obj_path: str,
    materials: dict,
    chunk_size: float,
    face_batch_size: int,
    stage: str,
    next_batch_index: int,
    bucketed_faces: int,
    face_count: int,
    bucket_parts: dict[tuple[tuple[int, int, int], str], list[str]],
    progress_fraction: float,
    completed_manifest_chunks: dict | None = None,
    total_cell_count: int | None = None,
) -> None:
    payload = {
        "version": _INCREMENTAL_OBJ_RESUME_VERSION,
        "kind": "incremental_obj_import",
        "source": _source_resume_identity(obj_path),
        "chunk_size": float(chunk_size),
        "face_batch_size": int(face_batch_size),
        "materials": _materials_resume_identity(materials),
        "stage": str(stage),
        "next_batch_index": int(next_batch_index),
        "bucketed_faces": int(bucketed_faces),
        "face_count": int(face_count),
        "progress_fraction": max(0.0, min(1.0, float(progress_fraction))),
        "bucket_parts": _serialize_bucket_parts(bucket_parts, staging_dir),
        "completed_manifest_chunks": completed_manifest_chunks or {},
        "total_cell_count": (
            None if total_cell_count is None else int(total_cell_count)
        ),
        "updated_at": time.time(),
    }
    _atomic_write_json(_import_resume_checkpoint_path(staging_dir), payload)


def _read_incremental_obj_resume_checkpoint(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as checkpoint_file:
            checkpoint = json.load(checkpoint_file)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(checkpoint, dict):
        return None
    return checkpoint


def _incremental_obj_resume_checkpoint_matches(
    checkpoint: dict,
    *,
    obj_path: str,
    materials: dict,
    chunk_size: float,
    face_batch_size: int,
) -> bool:
    if checkpoint.get("version") != _INCREMENTAL_OBJ_RESUME_VERSION:
        return False
    if checkpoint.get("kind") != "incremental_obj_import":
        return False
    if checkpoint.get("stage") not in {"bucketing", "finalizing"}:
        return False
    if float(checkpoint.get("chunk_size", -1.0)) != float(chunk_size):
        return False
    if int(checkpoint.get("face_batch_size", -1)) != int(face_batch_size):
        return False
    if checkpoint.get("materials") != _materials_resume_identity(materials):
        return False
    try:
        return checkpoint.get("source") == _source_resume_identity(obj_path)
    except OSError:
        return False


def _find_incremental_obj_resume(
    cache_dir: str,
    *,
    obj_path: str,
    materials: dict,
    chunk_size: float,
    face_batch_size: int,
) -> tuple[str, dict] | None:
    cache_parent = os.path.dirname(cache_dir)
    prefix = _import_resume_prefix(cache_dir)
    try:
        names = os.listdir(cache_parent)
    except OSError:
        return None

    candidates: list[tuple[float, str, dict]] = []
    for name in names:
        if not name.startswith(prefix):
            continue
        resume_dir = os.path.join(cache_parent, name)
        if not os.path.isdir(resume_dir):
            continue
        checkpoint_path = _import_resume_checkpoint_path(resume_dir)
        checkpoint = _read_incremental_obj_resume_checkpoint(checkpoint_path)
        if checkpoint is None:
            continue
        if not _incremental_obj_resume_checkpoint_matches(
            checkpoint,
            obj_path=obj_path,
            materials=materials,
            chunk_size=chunk_size,
            face_batch_size=face_batch_size,
        ):
            continue
        candidates.append(
            (
                float(checkpoint.get("updated_at", os.path.getmtime(resume_dir))),
                resume_dir,
                checkpoint,
            )
        )

    if not candidates:
        return None
    _updated_at, resume_dir, checkpoint = max(candidates, key=lambda item: item[0])
    return resume_dir, checkpoint


def _preserve_resumable_import(staging_dir: str, cache_dir: str) -> str:
    cache_parent = os.path.dirname(cache_dir)
    prefix = _import_resume_prefix(cache_dir)
    if os.path.basename(staging_dir).startswith(prefix):
        return staging_dir

    for attempt in range(1000):
        suffix = f"{os.getpid()}-{time.time_ns()}"
        if attempt:
            suffix = f"{suffix}-{attempt}"
        resume_dir = os.path.join(cache_parent, f"{prefix}{suffix}")
        if not os.path.exists(resume_dir):
            os.replace(staging_dir, resume_dir)
            return resume_dir
    raise RuntimeError("Could not allocate a paused import resume directory.")


def _remove_resume_checkpoint(staging_dir: str) -> None:
    try:
        os.remove(_import_resume_checkpoint_path(staging_dir))
    except FileNotFoundError:
        pass


def _build_incremental_obj_cache_in_directory(
    obj_path: str,
    materials: dict,
    cache_dir: str,
    *,
    chunk_size: float = DEFAULT_CHUNK_SIZE,
    progress_cb=None,
    face_batch_size: int | None = None,
    bucket_workers: int | None = None,
    pause_requested: Callable[[], bool] | None = None,
    resume_checkpoint: dict | None = None,
) -> str:
    """Build cache artifacts incrementally inside an unpublished directory."""
    chunks_dir = os.path.join(cache_dir, CHUNKS_DIRNAME)
    bucket_root = os.path.join(cache_dir, ".chunk-buckets")
    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(bucket_root, exist_ok=True)

    face_batch_size = (
        _configured_obj_import_batch_faces()
        if face_batch_size is None
        else max(1, int(face_batch_size))
    )
    bucket_workers = (
        _configured_obj_bucket_workers()
        if bucket_workers is None
        else max(1, min(_MAX_OBJ_BUCKET_WORKERS, int(bucket_workers)))
    )
    checkpoint = resume_checkpoint or {}
    checkpoint_stage = checkpoint.get("stage")
    last_progress_fraction = float(checkpoint.get("progress_fraction", 0.0))

    def emit_progress(stage: str, fraction: float) -> None:
        nonlocal last_progress_fraction
        if progress_cb is None:
            return
        fraction = max(0.0, min(1.0, float(fraction)))
        if fraction < last_progress_fraction:
            fraction = last_progress_fraction
        else:
            last_progress_fraction = fraction
        progress_cb(stage, fraction)

    def should_pause() -> bool:
        return bool(pause_requested and pause_requested())

    def vertex_progress(stage: str, fraction: float) -> None:
        emit_progress(stage, 0.25 * max(0.0, min(1.0, fraction)))

    def incremental_preflight(
        vertex_count: int,
        uv_count: int,
        normal_count: int,
        face_count: int,
    ) -> None:
        ensure_sufficient_incremental_import_memory(
            vertex_count,
            uv_count,
            normal_count,
            face_count,
            source_path=obj_path,
            face_batch_size=face_batch_size,
            bucket_workers=bucket_workers,
        )

    vertex_data = parse_obj_vertices(
        obj_path,
        progress_cb=vertex_progress,
        preflight_cb=incremental_preflight,
    )

    if checkpoint_stage in {"bucketing", "finalizing"}:
        bucket_parts = _deserialize_bucket_parts(
            checkpoint.get("bucket_parts", []),
            cache_dir,
        )
        bucketed_faces = int(checkpoint.get("bucketed_faces", 0))
        next_batch_index = int(checkpoint.get("next_batch_index", 0))
        completed_manifest_chunks = dict(
            checkpoint.get("completed_manifest_chunks", {})
        )
        total_cell_count = checkpoint.get("total_cell_count")
        total_cell_count = (
            None if total_cell_count is None else int(total_cell_count)
        )
    else:
        bucket_parts: dict[tuple[tuple[int, int, int], str], list[str]] = {}
        bucketed_faces = 0
        next_batch_index = 0
        completed_manifest_chunks = {}
        total_cell_count = None

    def write_pause_checkpoint(
        stage: str,
        *,
        progress_fraction: float | None = None,
        finalizing_manifest_chunks: dict | None = None,
        finalizing_bucket_parts: dict[
            tuple[tuple[int, int, int], str], list[str]
        ] | None = None,
        finalizing_total_cell_count: int | None = None,
    ) -> None:
        _write_incremental_obj_resume_checkpoint(
            cache_dir,
            obj_path=obj_path,
            materials=materials,
            chunk_size=chunk_size,
            face_batch_size=face_batch_size,
            stage=stage,
            next_batch_index=next_batch_index,
            bucketed_faces=bucketed_faces,
            face_count=vertex_data.face_count,
            bucket_parts=(
                bucket_parts
                if finalizing_bucket_parts is None
                else finalizing_bucket_parts
            ),
            progress_fraction=(
                last_progress_fraction
                if progress_fraction is None
                else progress_fraction
            ),
            completed_manifest_chunks=(
                completed_manifest_chunks
                if finalizing_manifest_chunks is None
                else finalizing_manifest_chunks
            ),
            total_cell_count=(
                total_cell_count
                if finalizing_total_cell_count is None
                else finalizing_total_cell_count
            ),
        )
        raise ImportPaused(cache_dir)

    def face_progress(stage: str, fraction: float) -> None:
        emit_progress(stage, 0.25 + 0.40 * max(0.0, min(1.0, fraction)))

    def collect_bucket_result(
        result: tuple[int, dict[tuple[tuple[int, int, int], str], str]],
    ) -> None:
        nonlocal bucketed_faces
        face_count, batch_bucket_paths = result
        bucketed_faces += face_count
        for key, path in batch_bucket_paths.items():
            bucket_parts.setdefault(key, []).append(path)
        if vertex_data.face_count:
            emit_progress(
                "bucketing faces",
                0.25 + 0.40 * min(1.0, bucketed_faces / vertex_data.face_count),
            )

    def bucket_batch(
        batch_index: int,
        batch: ObjFaceBatch,
    ) -> tuple[int, dict[tuple[tuple[int, int, int], str], str]]:
        batch_bucket_root = os.path.join(bucket_root, f"batch-{batch_index:08d}")
        return _write_obj_face_batch_bucket_parts(
            vertex_data,
            batch,
            batch_bucket_root,
            chunk_size=chunk_size,
        )

    _LOG.debug(
        "Incremental OBJ import using %d face(s) per batch and %d bucket worker(s).",
        face_batch_size,
        bucket_workers,
    )
    if checkpoint_stage != "finalizing":
        submitted_until_batch = next_batch_index
        batches = iter_obj_face_batches(
            obj_path,
            batch_size=face_batch_size,
            progress_cb=face_progress,
        )
        if bucket_workers <= 1:
            for batch_index, batch in enumerate(batches):
                if batch_index < next_batch_index:
                    continue
                if should_pause():
                    write_pause_checkpoint("bucketing")
                collect_bucket_result(bucket_batch(batch_index, batch))
                submitted_until_batch = batch_index + 1
                next_batch_index = submitted_until_batch
        else:
            active_futures: set[concurrent.futures.Future] = set()
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=bucket_workers,
                thread_name_prefix="obj-bucket",
            ) as executor:
                for batch_index, batch in enumerate(batches):
                    if batch_index < next_batch_index:
                        continue
                    if should_pause():
                        break
                    active_futures.add(
                        executor.submit(bucket_batch, batch_index, batch)
                    )
                    submitted_until_batch = batch_index + 1
                    if len(active_futures) >= bucket_workers:
                        done, active_futures = concurrent.futures.wait(
                            active_futures,
                            return_when=concurrent.futures.FIRST_COMPLETED,
                        )
                        for future in done:
                            collect_bucket_result(future.result())
                        if should_pause():
                            break

                while active_futures:
                    done, active_futures = concurrent.futures.wait(
                        active_futures,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in done:
                        collect_bucket_result(future.result())

                next_batch_index = submitted_until_batch
                if should_pause():
                    write_pause_checkpoint("bucketing")

        next_batch_index = submitted_until_batch

    emit_progress("writing chunk files", 0.65)

    def checkpoint_finalization(
        manifest_chunks: dict,
        remaining_bucket_parts: dict[tuple[tuple[int, int, int], str], list[str]],
        final_total_cell_count: int,
    ) -> None:
        write_pause_checkpoint(
            "finalizing",
            finalizing_manifest_chunks=manifest_chunks,
            finalizing_bucket_parts=remaining_bucket_parts,
            finalizing_total_cell_count=final_total_cell_count,
        )

    manifest_chunks = _finalize_incremental_buckets(
        chunks_dir,
        bucket_parts,
        progress_cb=emit_progress,
        pause_requested=should_pause,
        checkpoint_cb=checkpoint_finalization,
        initial_manifest_chunks=completed_manifest_chunks,
        total_cell_count=total_cell_count,
    )
    shutil.rmtree(bucket_root, ignore_errors=True)

    emit_progress("writing manifest", 0.98)

    footprint_cell_size, footprint_flat = _footprint_from_positions(
        vertex_data.positions
    )
    manifest = {
        "version": _VERSION,
        "chunk_size": chunk_size,
        "source_obj": os.path.basename(obj_path),
        "mtl_materials": {
            name: mat.diffuse_texture for name, mat in materials.items()
        },
        "chunks": manifest_chunks,
        "footprint_cell_size": footprint_cell_size,
        "footprint_cells": footprint_flat,
        "triangle_count": int(bucketed_faces),
        "import_mode": "incremental_obj",
    }
    with open(os.path.join(cache_dir, MANIFEST_NAME), "w") as f:
        json.dump(manifest, f)

    return cache_dir


def _bucket_path_for_key(
    bucket_paths: dict[tuple[tuple[int, int, int], str], str],
    bucket_root: str,
    key: tuple[tuple[int, int, int], str],
) -> str:
    path = bucket_paths.get(key)
    if path is None:
        path = os.path.join(bucket_root, f"{len(bucket_paths):08x}.bin")
        bucket_paths[key] = path
    return path


def _write_obj_face_batch_bucket_parts(
    vertex_data: ObjVertexData,
    batch: ObjFaceBatch,
    bucket_root: str,
    *,
    chunk_size: float,
) -> tuple[int, dict[tuple[tuple[int, int, int], str], str]]:
    face_count = len(batch.face_pos_idx)
    if face_count <= 0:
        return 0, {}

    os.makedirs(bucket_root, exist_ok=True)
    bucket_paths: dict[tuple[tuple[int, int, int], str], str] = {}

    # Assign faces to chunk cells without materializing the full
    # (faces, 3, 3) position array.  Only the final cell coordinates are kept
    # for the batch; de-indexed vertex records are written later in bounded
    # slices per sorted bucket run.
    cell_coords = np.empty((face_count, 3), dtype=np.int32)
    face_pos_idx = batch.face_pos_idx
    inv_scaled_triangle = 1.0 / (3.0 * chunk_size)
    for axis in range(3):
        vertex_axis = vertex_data.positions[:, axis]
        centroid_axis = (
            vertex_axis[face_pos_idx[:, 0]]
            + vertex_axis[face_pos_idx[:, 1]]
            + vertex_axis[face_pos_idx[:, 2]]
        ) * inv_scaled_triangle
        cell_coords[:, axis] = np.floor(centroid_axis).astype(np.int32, copy=False)

    material_name_to_id: dict[str, int] = {}
    material_names: list[str] = []
    material_ids = np.empty(face_count, dtype=np.int32)
    for index, raw_name in enumerate(batch.material_names):
        material_name = raw_name or "__no_material__"
        material_id = material_name_to_id.get(material_name)
        if material_id is None:
            material_id = len(material_names)
            material_name_to_id[material_name] = material_id
            material_names.append(material_name)
        material_ids[index] = material_id

    cell_min = cell_coords.min(axis=0).astype(np.int64)
    AXIS_BITS = 100_000
    packed = cell_coords[:, 0].astype(np.int64, copy=False) - cell_min[0]
    packed *= AXIS_BITS * AXIS_BITS
    shifted = cell_coords[:, 1].astype(np.int64, copy=False) - cell_min[1]
    shifted *= AXIS_BITS
    packed += shifted
    packed += cell_coords[:, 2].astype(np.int64, copy=False) - cell_min[2]
    material_count = max(1, len(material_names))
    combined_key = packed * material_count + material_ids
    order = np.argsort(combined_key, kind="stable")
    sorted_keys = combined_key[order]
    del packed, shifted, combined_key

    boundaries = np.nonzero(np.diff(sorted_keys))[0] + 1
    run_starts = np.concatenate(([0], boundaries))
    run_ends = np.concatenate((boundaries, [len(sorted_keys)]))

    for start, end in zip(run_starts, run_ends, strict=False):
        key = int(sorted_keys[start])
        material_id = key % material_count
        cell_packed = key // material_count
        cz = int(cell_packed % AXIS_BITS)
        cy = int((cell_packed // AXIS_BITS) % AXIS_BITS)
        cx = int(cell_packed // (AXIS_BITS * AXIS_BITS))
        real_cell = (
            cx + int(cell_min[0]),
            cy + int(cell_min[1]),
            cz + int(cell_min[2]),
        )
        material_name = material_names[material_id]
        path = _bucket_path_for_key(
            bucket_paths,
            bucket_root,
            (real_cell, material_name),
        )
        face_indices = order[start:end]
        with open(path, "ab") as output:
            for slice_start in range(
                0,
                len(face_indices),
                _OBJ_BUCKET_RECORD_SLICE_FACES,
            ):
                slice_end = min(
                    slice_start + _OBJ_BUCKET_RECORD_SLICE_FACES,
                    len(face_indices),
                )
                _write_obj_bucket_record_slice(
                    output,
                    vertex_data,
                    batch,
                    face_indices[slice_start:slice_end],
                )

    return face_count, bucket_paths


def _write_obj_bucket_record_slice(
    output,
    vertex_data: ObjVertexData,
    batch: ObjFaceBatch,
    face_indices: np.ndarray,
) -> None:
    """Write interleaved bucket records for a bounded face-index slice."""
    if len(face_indices) == 0:
        return

    pos_idx = batch.face_pos_idx[face_indices].reshape(-1)
    records = np.empty((len(pos_idx), 8), dtype=np.float32)
    records[:, 0:3] = vertex_data.positions[pos_idx]
    records[:, 3:5] = 0.0
    records[:, 5:8] = 0.0

    uv_idx = batch.face_uv_idx[face_indices]
    uv_idx_flat = uv_idx.reshape(-1)
    valid_uv = uv_idx_flat >= 0
    if len(vertex_data.uvs) and valid_uv.any():
        records[valid_uv, 3:5] = vertex_data.uvs[uv_idx_flat[valid_uv]]

    nrm_idx = batch.face_nrm_idx[face_indices]
    nrm_idx_flat = nrm_idx.reshape(-1)
    valid_nrm = nrm_idx_flat >= 0
    if len(vertex_data.normals) and valid_nrm.any():
        records[valid_nrm, 5:8] = vertex_data.normals[nrm_idx_flat[valid_nrm]]

    missing_normal_faces = ~np.all(nrm_idx >= 0, axis=1)
    if missing_normal_faces.any():
        record_faces = records.reshape(-1, 3, 8)
        missing_indices = np.nonzero(missing_normal_faces)[0]
        flat_normals = _compute_flat_normals(
            record_faces[missing_indices, :, 0:3].reshape(-1, 3)
        ).reshape(-1, 3, 3)
        record_faces[missing_indices, :, 5:8] = flat_normals

    output.write(records.tobytes())


def _finalize_incremental_buckets(
    chunks_dir: str,
    bucket_parts: dict[tuple[tuple[int, int, int], str], list[str]],
    *,
    progress_cb=None,
    pause_requested: Callable[[], bool] | None = None,
    checkpoint_cb: Callable[
        [
            dict,
            dict[tuple[tuple[int, int, int], str], list[str]],
            int,
        ],
        None,
    ] | None = None,
    initial_manifest_chunks: dict | None = None,
    total_cell_count: int | None = None,
) -> dict:
    per_cell_groups: dict[tuple[int, int, int], list[tuple[str, list[str]]]] = {}
    for (cell, material_name), paths in bucket_parts.items():
        per_cell_groups.setdefault(cell, []).append((material_name, paths))

    manifest_chunks = dict(initial_manifest_chunks or {})
    cell_items = sorted(per_cell_groups.items())
    total_cells = int(total_cell_count or (len(manifest_chunks) + len(cell_items)))
    completed_cells = len(manifest_chunks)

    def maybe_checkpoint(next_index: int) -> None:
        if not pause_requested or not pause_requested():
            return
        if checkpoint_cb is None:
            return
        remaining_bucket_parts: dict[tuple[tuple[int, int, int], str], list[str]] = {}
        for cell_coord, groups in cell_items[next_index:]:
            for material_name, paths in groups:
                remaining_bucket_parts[(cell_coord, material_name)] = paths
        checkpoint_cb(dict(manifest_chunks), remaining_bucket_parts, total_cells)

    maybe_checkpoint(0)
    for index, (cell_coord, groups) in enumerate(cell_items, start=1):
        cell_str = f"{cell_coord[0]}_{cell_coord[1]}_{cell_coord[2]}"
        bounds_min, bounds_max, used_materials = _write_chunk_file_from_buckets(
            chunks_dir,
            cell_str,
            sorted(groups, key=lambda item: item[0]),
        )
        manifest_chunks[cell_str] = {
            "materials": used_materials,
            "bounds_min": bounds_min.tolist(),
            "bounds_max": bounds_max.tolist(),
        }
        completed_cells += 1
        if progress_cb and (
            completed_cells % 25 == 0 or completed_cells == total_cells
        ):
            progress_cb(
                "writing chunk files",
                0.65 + 0.33 * (completed_cells / max(total_cells, 1)),
            )
        maybe_checkpoint(index)
    return manifest_chunks


def _write_chunk_file_from_buckets(
    chunks_dir: str,
    cell_str: str,
    groups: list[tuple[str, list[str]]],
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    path = os.path.join(chunks_dir, f"{cell_str}.bin")
    bounds_min = None
    bounds_max = None
    used_materials = []

    with open(path, "wb") as output:
        output.write(_MAGIC)
        output.write(struct.pack("<I", _VERSION))
        output.write(struct.pack("<I", len(groups)))

        for material_name, bucket_paths in groups:
            record_count = 0
            for bucket_path in bucket_paths:
                record_count += _bucket_record_count(bucket_path)

            name_bytes = material_name.encode("utf-8")
            output.write(struct.pack("<I", len(name_bytes)))
            output.write(name_bytes)
            output.write(struct.pack("<I", record_count))

            for bucket_path in bucket_paths:
                for records in _iter_bucket_record_blocks(bucket_path):
                    flat_pos = np.ascontiguousarray(
                        records[:, 0:3],
                        dtype=np.float32,
                    )
                    if len(flat_pos):
                        group_min = flat_pos.min(axis=0)
                        group_max = flat_pos.max(axis=0)
                        if bounds_min is None:
                            bounds_min = group_min.copy()
                            bounds_max = group_max.copy()
                        else:
                            np.minimum(bounds_min, group_min, out=bounds_min)
                            np.maximum(bounds_max, group_max, out=bounds_max)
                    output.write(flat_pos.tobytes())

            for bucket_path in bucket_paths:
                for records in _iter_bucket_record_blocks(bucket_path):
                    flat_uv = np.ascontiguousarray(
                        records[:, 3:5],
                        dtype=np.float32,
                    )
                    output.write(flat_uv.tobytes())

            for bucket_path in bucket_paths:
                for records in _iter_bucket_record_blocks(bucket_path):
                    flat_nrm = np.ascontiguousarray(
                        records[:, 5:8],
                        dtype=np.float32,
                    )
                    output.write(flat_nrm.tobytes())

            used_materials.append(material_name)
            for bucket_path in bucket_paths:
                try:
                    os.remove(bucket_path)
                except FileNotFoundError:
                    pass

    if bounds_min is None:
        bounds_min = np.zeros(3, dtype=np.float32)
        bounds_max = np.zeros(3, dtype=np.float32)
    return bounds_min, bounds_max, used_materials


def _bucket_record_count(bucket_path: str) -> int:
    record_bytes = 8 * np.dtype(np.float32).itemsize
    byte_count = os.path.getsize(bucket_path)
    if byte_count % record_bytes != 0:
        raise ValueError(f"Corrupt incremental bucket: {bucket_path}")
    return byte_count // record_bytes


def _iter_bucket_record_blocks(bucket_path: str):
    floats_per_record = 8
    read_count = max(1, _OBJ_BUCKET_FINALIZE_BLOCK_RECORDS) * floats_per_record
    with open(bucket_path, "rb") as input_file:
        while True:
            records = np.fromfile(
                input_file,
                dtype=np.float32,
                count=read_count,
            )
            if records.size == 0:
                break
            if records.size % floats_per_record != 0:
                raise ValueError(f"Corrupt incremental bucket: {bucket_path}")
            yield records.reshape(-1, floats_per_record)


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


def _build_cache_in_directory(obj_path: str, mesh: RawMesh, materials: dict,
                              cache_dir: str,
                              chunk_size: float = DEFAULT_CHUNK_SIZE,
                              progress_cb=None) -> str:
    """Build all cache artifacts inside an unpublished staging directory."""
    chunks_dir = os.path.join(cache_dir, CHUNKS_DIRNAME)
    os.makedirs(chunks_dir, exist_ok=True)

    if progress_cb:
        progress_cb("computing face centroids", 0.0)

    n_faces = len(mesh.face_pos_idx)

    # Avoid materializing tri_pos (Nf, 3, 3) and centroids (Nf, 3) for
    # very large maps. For 100M+ faces those temporaries can cost many
    # gigabytes. Compute each centroid axis directly from the three vertex
    # index columns and store only the final chunk cell coordinate.
    cell_coords = np.empty((n_faces, 3), dtype=np.int32)
    face_pos_idx = mesh.face_pos_idx
    inv_scaled_triangle = 1.0 / (3.0 * chunk_size)
    for axis in range(3):
        vertex_axis = mesh.positions[:, axis]
        centroid_axis = (
            vertex_axis[face_pos_idx[:, 0]]
            + vertex_axis[face_pos_idx[:, 1]]
            + vertex_axis[face_pos_idx[:, 2]]
        ) * inv_scaled_triangle
        cell_coords[:, axis] = np.floor(centroid_axis).astype(np.int32, copy=False)
        if progress_cb:
            progress_cb("computing face centroids", 0.03 * (axis + 1))
    del face_pos_idx

    if progress_cb:
        progress_cb("grouping faces by cell", 0.1)

    # IMPORTANT: key by *unique material name*, not by MaterialRange index.
    # A single material (e.g. "tile_A") can appear in multiple separate
    # usemtl ranges throughout the OBJ (common when Agisoft interleaves
    # texture tile usage across the file). If we keyed by range index here,
    # faces using the same texture tile but from different ranges would be
    # split into separate groups instead of merging -- wasting draw calls
    # and, worse, corrupting the cell-grouping logic below since range
    # boundaries don't align with cell boundaries.
    unique_material_names = sorted(set(mr.material_name for mr in mesh.material_ranges))
    material_name_to_id = {name: i for i, name in enumerate(unique_material_names)}
    material_names = unique_material_names  # used below for id -> name lookup

    face_material_id = np.full(n_faces, -1, dtype=np.int32)
    for mr in mesh.material_ranges:
        face_material_id[mr.start_face:mr.end_face] = material_name_to_id[mr.material_name]

    cell_min = cell_coords.min(axis=0).astype(np.int64)
    AXIS_BITS = 100_000
    shifted_axis = cell_coords[:, 0].astype(np.int64, copy=False) - cell_min[0]
    cell_key = shifted_axis * (AXIS_BITS * AXIS_BITS)
    shifted_axis = cell_coords[:, 1].astype(np.int64, copy=False) - cell_min[1]
    shifted_axis *= AXIS_BITS
    cell_key += shifted_axis
    shifted_axis = cell_coords[:, 2].astype(np.int64, copy=False) - cell_min[2]
    cell_key += shifted_axis
    del cell_coords, shifted_axis
    combined_key = cell_key
    combined_key *= len(material_names) + 1
    material_key = face_material_id.astype(np.int64, copy=False)
    material_key += 1
    combined_key += material_key
    del cell_key, face_material_id, material_key
    gc.collect()

    order = np.argsort(combined_key, kind="stable")
    sorted_keys = combined_key[order]
    del combined_key
    gc.collect()

    boundaries = np.nonzero(np.diff(sorted_keys))[0] + 1
    run_starts = np.concatenate(([0], boundaries))
    run_ends = np.concatenate((boundaries, [len(sorted_keys)]))

    if progress_cb:
        progress_cb("writing chunk files", 0.3)

    manifest_chunks = {}
    total_runs = len(run_starts)

    # Build per-cell group lists first, then write each cell in parallel.
    # This stage is CPU and I/O heavy on large maps and scales well with
    # multiple cores / SSD-backed storage.
    per_cell_groups: dict[tuple[int, int, int], list[tuple[str, np.ndarray]]] = {}

    for i in range(total_runs):
        if progress_cb and i % 200 == 0:
            progress_cb("grouping chunk faces", 0.3 + 0.35 * (i / max(total_runs, 1)))

        s, e = run_starts[i], run_ends[i]
        face_idx_in_order = order[s:e]
        key = sorted_keys[s]
        mat_id = int(key % (len(material_names) + 1)) - 1
        cell_packed = key // (len(material_names) + 1)
        cz = int(cell_packed % AXIS_BITS)
        cy = int((cell_packed // AXIS_BITS) % AXIS_BITS)
        cx = int(cell_packed // (AXIS_BITS * AXIS_BITS))
        real_cell = (cx + int(cell_min[0]), cy + int(cell_min[1]), cz + int(cell_min[2]))

        mat_name = material_names[mat_id] if mat_id >= 0 else "__no_material__"
        per_cell_groups.setdefault(real_cell, []).append((mat_name, face_idx_in_order))

    worker_allocation = resolve_worker_allocation(
        os.environ.get("CAVEVIEWER_CHUNK_BUILD_WORKERS"),
        os.environ.get("CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS"),
        default_workers=1,
        default_reserved_cpus=2,
    )
    worker_count = worker_allocation.effective_workers
    _LOG.info(describe_worker_target("Cache-build", worker_allocation))

    cell_items = list(per_cell_groups.items())
    total_cells = len(cell_items)
    completed_cells = 0

    def _write_one_cell(cell_coord: tuple[int, int, int], groups: list[tuple[str, np.ndarray]]):
        cell_str = f"{cell_coord[0]}_{cell_coord[1]}_{cell_coord[2]}"
        bounds_min, bounds_max, used_materials = _write_chunk_file(
            chunks_dir, cell_str, mesh, groups
        )
        return cell_str, bounds_min, bounds_max, used_materials

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        cell_iterator = iter(cell_items)
        active_futures = set()
        submitted_cells = 0
        admitted_workers = min(1, total_cells)
        admission_blocked = False

        def _submit_next_cell() -> bool:
            nonlocal submitted_cells
            try:
                cell_coord, groups = next(cell_iterator)
            except StopIteration:
                return False
            active_futures.add(executor.submit(_write_one_cell, cell_coord, groups))
            submitted_cells += 1
            return True

        # Admit one real task first. Pool growth happens only after completed
        # work has made its memory cost observable to the system RAM probe.
        for _ in range(admitted_workers):
            _submit_next_cell()

        try:
            while active_futures:
                done_futures, active_futures = concurrent.futures.wait(
                    active_futures,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )

                # Resolve every completion in this batch before submitting
                # replacements. If any write failed, no additional cell is
                # allowed to start after that failure became observable.
                completed_results = [future.result() for future in done_futures]
                for cell_str, bounds_min, bounds_max, used_materials in completed_results:
                    manifest_chunks[cell_str] = {
                        "materials": used_materials,
                        "bounds_min": bounds_min.tolist(),
                        "bounds_max": bounds_max.tolist(),
                    }
                    completed_cells += 1
                    if progress_cb and (
                        completed_cells % 25 == 0 or completed_cells == total_cells
                    ):
                        progress_cb(
                            "writing chunk files",
                            0.65 + 0.33 * (completed_cells / max(total_cells, 1)),
                        )

                if (
                    submitted_cells < total_cells
                    and admitted_workers < min(worker_count, total_cells)
                ):
                    snapshot = hardware_memory.detect_ram_snapshot()
                    if can_start_additional_worker(snapshot):
                        admitted_workers += 1
                        pressure_note = (
                            "System RAM pressure eased; "
                            if admission_blocked
                            else ""
                        )
                        _LOG.info(
                            "%sDetected system RAM for cache-build worker "
                            "admission: %.1f GB available of %.1f GB "
                            "(%.1f%% used); increasing workers to %d of %d.",
                            pressure_note,
                            snapshot.available_bytes / (1024 ** 3),
                            snapshot.total_bytes / (1024 ** 3),
                            snapshot.utilization_fraction * 100.0,
                            admitted_workers,
                            worker_count,
                        )
                        admission_blocked = False
                    else:
                        if not admission_blocked:
                            if snapshot is None:
                                _LOG.warning(
                                    "Could not measure available system RAM; keeping "
                                    "cache construction at %d worker(s).",
                                    admitted_workers,
                                )
                            else:
                                _LOG.warning(
                                    "System RAM utilization is %.1f%%; keeping cache "
                                    "construction at %d worker(s) because the limit "
                                    "is %.0f%%.",
                                    snapshot.utilization_fraction * 100.0,
                                    admitted_workers,
                                    MAX_WORKER_RAM_UTILIZATION * 100.0,
                                )
                        admission_blocked = True

                while len(active_futures) < admitted_workers:
                    if not _submit_next_cell():
                        break
        except BaseException:
            # Do not make a full-disk failure churn through every cell that
            # was queued before the first failed write surfaced.
            for pending_future in active_futures:
                pending_future.cancel()
            raise

    if progress_cb:
        progress_cb("writing manifest", 0.98)

    # Fine-grained 2D occupancy footprint for the minimap.  This is computed
    # from raw vertex positions (not face centroids), at a resolution chosen
    # to give ~200 cells along the longest world axis -- fine enough for the
    # minimap panel regardless of the 3D chunk_size.  Stored as a flat list
    # [cx0, cz0, cx1, cz1, ...] of int32 pairs so the minimap can render a
    # detailed outline even when 3D chunks are very large (e.g. 100 m).
    footprint_cell_size, footprint_flat = _footprint_from_positions(mesh.positions)

    manifest = {
        "version": _VERSION,
        "chunk_size": chunk_size,
        "source_obj": os.path.basename(obj_path),
        "mtl_materials": {
            name: mat.diffuse_texture for name, mat in materials.items()
        },
        "chunks": manifest_chunks,
        "footprint_cell_size": footprint_cell_size,
        "footprint_cells": footprint_flat,
    }
    with open(os.path.join(cache_dir, MANIFEST_NAME), "w") as f:
        json.dump(manifest, f)

    return cache_dir


def _write_chunk_file(chunks_dir, cell_str, mesh, groups):
    """
    Write one chunk binary file containing all material groups for a cell.
    De-indexes faces into flat (position, uv, normal) vertex triples per
    group, since at render time we want simple flat VBOs, no index buffer
    juggling across materials.

    Binary format:
        MAGIC (4 bytes) | VERSION (uint32)
        n_groups (uint32)
        for each group:
            name_len (uint32) | name (utf8 bytes)
            n_verts (uint32)
            positions: n_verts * 3 float32
            uvs:       n_verts * 2 float32
            normals:   n_verts * 3 float32
    """
    path = os.path.join(chunks_dir, f"{cell_str}.bin")
    has_normals = mesh.normals.shape[0] > 0
    has_uvs = mesh.uvs.shape[0] > 0

    bounds_min = None
    bounds_max = None
    used_materials = []

    with open(path, "wb") as f:
        f.write(_MAGIC)
        f.write(struct.pack("<I", _VERSION))
        f.write(struct.pack("<I", len(groups)))

        for mat_name, face_idx in groups:
            pos_idx = mesh.face_pos_idx[face_idx].reshape(-1)
            uv_idx = mesh.face_uv_idx[face_idx].reshape(-1)
            nrm_idx = mesh.face_nrm_idx[face_idx].reshape(-1)

            # RawMesh arrays are already float32; copy=False avoids an
            # unnecessary second conversion copy while still enforcing dtype
            # if a non-standard mesh source ever slips through.
            flat_pos = mesh.positions[pos_idx].astype(np.float32, copy=False)

            if has_uvs and (uv_idx >= 0).all():
                flat_uv = mesh.uvs[uv_idx].astype(np.float32, copy=False)
            else:
                flat_uv = np.zeros((len(pos_idx), 2), dtype=np.float32)

            if has_normals and (nrm_idx >= 0).all():
                flat_nrm = mesh.normals[nrm_idx].astype(np.float32, copy=False)
            else:
                flat_nrm = _compute_flat_normals(flat_pos)

            if len(flat_pos):
                group_min = flat_pos.min(axis=0)
                group_max = flat_pos.max(axis=0)
                if bounds_min is None:
                    bounds_min = group_min.copy()
                    bounds_max = group_max.copy()
                else:
                    np.minimum(bounds_min, group_min, out=bounds_min)
                    np.maximum(bounds_max, group_max, out=bounds_max)

            name_bytes = mat_name.encode("utf-8")
            f.write(struct.pack("<I", len(name_bytes)))
            f.write(name_bytes)
            f.write(struct.pack("<I", len(flat_pos)))
            f.write(flat_pos.tobytes())
            f.write(flat_uv.tobytes())
            f.write(flat_nrm.tobytes())

            used_materials.append(mat_name)

    if bounds_min is None:
        bounds_min = np.zeros(3, dtype=np.float32)
        bounds_max = np.zeros(3, dtype=np.float32)
    return bounds_min, bounds_max, used_materials


def _compute_flat_normals(flat_pos: np.ndarray) -> np.ndarray:
    """Per-triangle face normal, duplicated across the triangle's 3 verts,
    used as a fallback when the OBJ didn't supply vertex normals."""
    tris = flat_pos.reshape(-1, 3, 3)
    e1 = tris[:, 1] - tris[:, 0]
    e2 = tris[:, 2] - tris[:, 0]
    n = np.cross(e1, e2)
    lengths = np.linalg.norm(n, axis=1, keepdims=True)
    lengths[lengths == 0] = 1.0
    n = n / lengths
    return np.repeat(n, 3, axis=0).astype(np.float32)


def compute_flat_normals(flat_pos: np.ndarray) -> np.ndarray:
    """
    Public entry point for the same per-triangle flat-normal computation
    _write_chunk_file uses internally, exposed so caveviewer.gui.viewer_window
    can recompute a flat-shaded normal set from an already-loaded chunk's
    positions at runtime (for the SHADE toggle button) without needing to
    re-import or duplicate this math. `flat_pos` must already be
    de-indexed/flat (N*3, 3) -- i.e. exactly the shape ChunkMaterialGroup.
    positions is in, which is what makes this safe to call directly on
    already-streamed chunk data.
    """
    return _compute_flat_normals(flat_pos)


def _interleaved_vertex_bytes(positions: np.ndarray, uvs: np.ndarray,
                              normals: np.ndarray) -> bytes:
    """Pack position/uv/normal columns into the renderer's VBO layout."""
    n = len(positions)
    interleaved = np.empty((n, 8), dtype=np.float32)
    interleaved[:, 0:3] = positions
    interleaved[:, 3:5] = uvs
    interleaved[:, 5:8] = normals
    return interleaved.tobytes()


def vertex_bytes_for_shading(
    positions: np.ndarray,
    uvs: np.ndarray,
    smooth_normals: np.ndarray,
    *,
    smooth_shading: bool,
) -> bytes:
    """Pack renderer vertex bytes for the requested shading mode."""
    normals = smooth_normals if smooth_shading else compute_flat_normals(positions)
    return _interleaved_vertex_bytes(positions, uvs, normals)


def prepare_chunk_upload_groups(chunk_data: ChunkData) -> ChunkData:
    """
    Precompute CPU-side renderer payloads for a loaded chunk.

    This deliberately does no OpenGL work. It is safe to call from a
    background streaming worker and leaves the render thread with only
    context-bound buffer/VAO/texture operations.
    """
    upload_groups: list[ChunkUploadGroup] = []
    for mat_name, group in chunk_data.groups.items():
        n = len(group.positions)
        if n == 0:
            continue

        upload_groups.append(ChunkUploadGroup(
            material_name=mat_name,
            positions=group.positions,
            uvs=group.uvs,
            smooth_normals=group.normals,
        ))

    chunk_data.upload_groups = upload_groups
    return chunk_data


def prepack_chunk_vertex_bytes(
    chunk_data: ChunkData,
    *,
    smooth_shading: bool,
) -> ChunkData:
    """
    Precompute renderer VBO bytes for one shade mode without doing OpenGL work.

    This is safe for a background streaming worker. The render thread still
    owns the actual GL buffer/VAO creation, but it no longer has to spend a
    frame packing large numpy arrays into interleaved bytes.
    """
    if chunk_data.upload_groups is None:
        prepare_chunk_upload_groups(chunk_data)
    for group in chunk_data.upload_groups or []:
        group.prepack_vertex_bytes(smooth_shading=smooth_shading)
    return chunk_data


def load_manifest(cache_dir):
    # If no cache_dir was provided (launch without a preloaded map), or the
    # manifest file is missing, return None so callers can handle "no map".
    if not cache_dir:
        return None

    manifest_path = os.path.join(cache_dir, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        return None

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
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


def cache_chunk_size(cache_dir: str) -> float | None:
    """Read the chunk size from an existing cache's manifest."""
    try:
        return manifest_chunk_size(load_manifest(cache_dir))
    except Exception:
        return None


def load_chunk_file(cache_dir: str, cell: tuple[int, int, int]) -> ChunkData:
    cell_str = f"{cell[0]}_{cell[1]}_{cell[2]}"
    path = os.path.join(cache_dir, CHUNKS_DIRNAME, f"{cell_str}.bin")
    with open(path, "rb") as f:
        blob = f.read()

    def require(offset: int, size: int, description: str) -> None:
        if offset < 0 or size < 0 or offset + size > len(blob):
            raise ValueError(f"Truncated chunk file while reading {description} in {path}")

    offset = 0
    require(offset, 12, "header")
    magic = blob[offset:offset + 4]
    offset += 4
    if magic != _MAGIC:
        raise ValueError(f"Bad chunk file magic in {path}")

    version = struct.unpack_from("<I", blob, offset)[0]
    offset += 4
    if version != _VERSION:
        raise ValueError(f"Unsupported chunk version {version} in {path}")

    n_groups = struct.unpack_from("<I", blob, offset)[0]
    offset += 4

    groups = {}
    bmin = None
    bmax = None
    for _ in range(n_groups):
        require(offset, 4, "material name length")
        name_len = struct.unpack_from("<I", blob, offset)[0]
        offset += 4
        require(offset, name_len, "material name")
        name = blob[offset:offset + name_len].decode("utf-8")
        offset += name_len

        require(offset, 4, "vertex count")
        n_verts = struct.unpack_from("<I", blob, offset)[0]
        offset += 4

        pos_count = n_verts * 3
        uv_count = n_verts * 2
        nrm_count = n_verts * 3

        require(offset, pos_count * 4, "positions")
        positions = np.frombuffer(blob, dtype=np.float32, count=pos_count, offset=offset).reshape(n_verts, 3)
        offset += pos_count * 4
        require(offset, uv_count * 4, "texture coordinates")
        uvs = np.frombuffer(blob, dtype=np.float32, count=uv_count, offset=offset).reshape(n_verts, 2)
        offset += uv_count * 4
        require(offset, nrm_count * 4, "normals")
        normals = np.frombuffer(blob, dtype=np.float32, count=nrm_count, offset=offset).reshape(n_verts, 3)
        offset += nrm_count * 4

        groups[name] = ChunkMaterialGroup(name, positions, uvs, normals)
        if len(positions):
            group_min = positions.min(axis=0)
            group_max = positions.max(axis=0)
            if bmin is None:
                bmin = group_min.copy()
                bmax = group_max.copy()
            else:
                np.minimum(bmin, group_min, out=bmin)
                np.maximum(bmax, group_max, out=bmax)

    if bmin is None:
        bmin = np.zeros(3, dtype=np.float32)
        bmax = np.zeros(3, dtype=np.float32)

    return ChunkData(cell=cell, groups=groups, bounds_min=bmin, bounds_max=bmax)


def cache_is_valid(obj_path: str) -> bool:
    """Cache is valid if it exists and is newer than the source OBJ (cheap
    staleness check so re-running on the same map doesn't reparse 2GB)."""
    for cache_dir in map_cache_candidates(obj_path):
        manifest_path = os.path.join(cache_dir, MANIFEST_NAME)
        if not os.path.exists(manifest_path):
            continue
        if os.path.getmtime(manifest_path) < os.path.getmtime(obj_path):
            continue
        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
        except Exception:
            continue
        if not _has_current_chunk_cache(cache_dir, manifest):
            continue
        return True
    return False


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


def find_landing_position(manifest: dict, target_x: float, target_z: float,
                            preferred_y: float, search_radius_cells: int = 12) -> tuple[float, float, float]:
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
        cx, cy, cz = (int(v) for v in cell_str.split("_"))
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
                    continue  # only the new outer ring at this radius, inner rings already checked
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
