"""Import capacity configuration, memory estimates, and preflight checks."""

from __future__ import annotations

import errno
import os
import shutil

from caveviewer.core.chunking.staging import (
    CacheAsset,
    _cache_asset_size,
    _nearest_existing_directory,
)
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.hardware import system_memory


IMPORT_DISK_SPACE_MULTIPLIER = 2
IMPORT_MEMORY_HEADROOM_FRACTION = 0.90
IMPORT_MEMORY_PHYSICAL_OVERCOMMIT_FRACTION = 1.25
IMPORT_MEMORY_FIXED_OVERHEAD_BYTES = 256 * 1024 ** 2
SOURCE_FILE_READ_MEMORY_MULTIPLIER = 1.25

OBJ_IMPORT_BATCH_FACES_ENV_VAR = "CAVEVIEWER_OBJ_IMPORT_BATCH_FACES"
OBJ_BUCKET_WORKERS_ENV_VAR = "CAVEVIEWER_OBJ_BUCKET_WORKERS"
_DEFAULT_OBJ_IMPORT_BATCH_FACES = 200_000
_DEFAULT_OBJ_BUCKET_WORKERS = 2
_MAX_OBJ_BUCKET_WORKERS = 32
_LOG = get_logger("chunker")


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


def estimate_source_file_read_memory_bytes(source_size_bytes: int) -> int:
    """Estimate RAM needed by parser libraries that load a source container."""
    source_size_bytes = max(0, int(source_size_bytes))
    return int(
        source_size_bytes * SOURCE_FILE_READ_MEMORY_MULTIPLIER
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
    snapshot = system_memory.detect_ram_snapshot()
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


def ensure_sufficient_source_file_read_memory(source_path: str) -> None:
    """Reject whole-container parser loads whose source bytes exceed RAM."""
    snapshot = system_memory.detect_ram_snapshot()
    if snapshot is None:
        _LOG.warning(
            "Could not measure available system RAM before reading %s; "
            "continuing without source-file RAM preflight.",
            os.path.basename(source_path),
        )
        return

    source_size = os.path.getsize(source_path)
    required_bytes = estimate_source_file_read_memory_bytes(source_size)
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
            "Source-file RAM preflight warning for %s: reading the %.1f GB "
            "source may need about %.1f GB peak before geometry arrays are "
            "expanded; %.1f GB is currently available (%.1f GB after the "
            "%.0f%% safety limit). Continuing because the estimate is within "
            "the %.1f GB physical-memory overcommit allowance.",
            os.path.basename(source_path),
            source_size / (1024 ** 3),
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
    snapshot = system_memory.detect_ram_snapshot()
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


def cache_assets_size(assets: tuple[CacheAsset, ...] | list[CacheAsset]) -> int:
    """Return the total bytes that cache assets will add to staging."""
    return sum(_cache_asset_size(asset) for asset in assets)
