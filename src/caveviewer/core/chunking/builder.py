"""
caveviewer.core.chunking.builder

Spatial partitioning of a parsed mesh into a 3D grid of chunks, cached to
disk in a fast-to-load binary format. This is the piece that makes large
cave maps viewable: instead of one giant draw call / VRAM blob for the
whole cave, we split the mesh into cells (default 50m cubes -- tune via
CHUNK_SIZE for your cave's scale) and load only the cells near the camera
at runtime (see caveviewer.core.streaming.world).

Cache layout on disk, under the selected generated cache directory:
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
import gc
import json
import os
import shutil
import tempfile
from typing import Callable

import numpy as np

from caveviewer.core.chunking.buckets import (
    _OBJ_BUCKET_FINALIZE_BLOCK_RECORDS,
    _OBJ_BUCKET_RECORD_SLICE_FACES,
    _bucket_path_for_key,
    _bucket_record_count,
    _finalize_incremental_buckets,
    _iter_bucket_record_blocks_for_range,
    _write_chunk_file_from_buckets,
    _write_obj_bucket_record_slice,
    _write_obj_face_batch_bucket_parts,
)
from caveviewer.core.chunking.capacity import (
    IMPORT_DISK_SPACE_MULTIPLIER,
    IMPORT_MEMORY_FIXED_OVERHEAD_BYTES,
    IMPORT_MEMORY_HEADROOM_FRACTION,
    IMPORT_MEMORY_PHYSICAL_OVERCOMMIT_FRACTION,
    OBJ_BUCKET_WORKERS_ENV_VAR,
    OBJ_IMPORT_BATCH_FACES_ENV_VAR,
    InsufficientDiskSpaceError,
    InsufficientImportMemoryError,
    _configured_obj_bucket_workers,
    _configured_obj_import_batch_faces,
    _DEFAULT_OBJ_BUCKET_WORKERS,
    _DEFAULT_OBJ_IMPORT_BATCH_FACES,
    _MAX_OBJ_BUCKET_WORKERS,
    cache_assets_size,
    ensure_sufficient_disk_space,
    ensure_sufficient_source_file_read_memory,
    ensure_sufficient_import_memory,
    ensure_sufficient_incremental_import_memory,
    estimate_import_memory_bytes,
    estimate_source_file_read_memory_bytes,
    estimate_incremental_import_memory_bytes,
)
from caveviewer.core.chunking.io import (
    CHUNKS_DIRNAME,
    ChunkData,
    ChunkMaterialGroup,
    _CHUNK_FILE_HEADER_BYTES,
    _CHUNK_GROUP_HEADER_BYTES,
    _MAX_CHUNK_FILE_BYTES,
    _MAX_CHUNK_FILE_GROUPS,
    _MAX_CHUNK_MATERIAL_NAME_BYTES,
    _MAGIC,
    _VERSION,
    _read_exact_chunk_bytes,
    _write_chunk_file,
    load_chunk_file as _io_load_chunk_file,
)
from caveviewer.core.chunking.metadata import (
    CHUNK_SIZE_ENV_VAR,
    DEFAULT_CHUNK_SIZE,
    _DEFAULT_CHUNK_SIZE_FALLBACK,
    _footprint_from_positions,
    _has_current_chunk_cache,
    _resolve_default_chunk_size,
    cache_chunk_size,
    cache_dir_is_valid,
    cache_is_valid,
    configured_chunk_size,
    find_landing_position,
    get_cache_dir,
    load_manifest,
    manifest_chunk_size,
    manifest_max_upload_group_mb,
    world_to_cell,
)
from caveviewer.core.chunking.staging import (
    IMPORT_RESUME_MANIFEST_NAME,
    MANIFEST_NAME,
    CacheAsset,
    ImportPaused,
    _atomic_write_json,
    _cache_asset_size,
    _deserialize_bucket_parts,
    _find_incremental_obj_resume,
    _import_resume_checkpoint_path,
    _import_resume_prefix,
    _incremental_obj_resume_checkpoint_matches,
    _materials_resume_identity,
    _nearest_existing_directory,
    _preserve_resumable_import,
    _publish_cache_directory,
    _read_incremental_obj_resume_checkpoint,
    _remove_resume_checkpoint,
    _serialize_bucket_parts,
    _source_resume_identity,
    _stage_cache_assets,
    _write_incremental_obj_resume_checkpoint,
)
from caveviewer.core.chunking.upload import (
    MAX_UPLOAD_GROUP_MB_ENV_VAR,
    ChunkUploadGroup,
    _DEFAULT_MAX_UPLOAD_GROUP_MB,
    _MAX_MAX_UPLOAD_GROUP_MB,
    _MIN_MAX_UPLOAD_GROUP_MB,
    _UPLOAD_VERTEX_BYTES,
    _compute_flat_normals,
    _max_upload_group_vertices,
    _upload_group_face_ranges,
    _upload_group_vertex_ranges,
    compute_flat_normals,
    configured_max_upload_group_bytes,
    configured_max_upload_group_mb,
    prepare_chunk_upload_groups,
    prepack_chunk_vertex_bytes,
    vertex_bytes_for_shading,
)
from caveviewer.core.hardware import system_memory
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.navigation.cache_metadata import build_navigation_metadata
from caveviewer.core.navigation.mesh_collision import CachedChunkMeshCollisionGuard
from caveviewer.core.navigation.voxel_cache import (
    NAVIGATION_VOXEL_CACHE_NAME,
    build_navigation_voxel_cache,
)
from caveviewer.core.workers.allocation import (
    MAX_WORKER_RAM_UTILIZATION,
    can_start_additional_worker,
    describe_worker_target,
    resolve_worker_allocation,
)
from caveviewer.core.mesh.obj import (
    RawMesh,
    MaterialRange,
    ObjFaceBatch,
    ObjVertexData,
    iter_obj_face_batches,
    parse_obj_vertices,
)
from caveviewer.core.map.cache_paths import (
    map_cache_build_dir,
)

_LOG = get_logger("chunker")


def _resolve_max_upload_group_mb(value: float | None) -> float:
    if value is None:
        return configured_max_upload_group_mb()
    return configured_max_upload_group_mb({
        MAX_UPLOAD_GROUP_MB_ENV_VAR: str(value),
    })


def _max_upload_group_bytes_from_mb(value: float) -> int:
    return max(1, int(float(value) * 1024 ** 2))


def build_cache(
    obj_path: str,
    mesh: RawMesh,
    materials: dict,
    chunk_size: float = DEFAULT_CHUNK_SIZE,
    progress_cb=None,
    *,
    cache_dir: str | None = None,
    assets: tuple[CacheAsset, ...] | list[CacheAsset] = (),
    max_upload_group_mb: float | None = None,
) -> str:
    """
    Partition `mesh` into spatial chunks and atomically publish the cache.

    ``cache_dir`` defaults to the generated location selected by
    ``cache_paths``. Assets are staged inside the same private directory, so the
    manifest can never become visible before all referenced textures.

    progress_cb(stage: str, fraction: float)
    """
    cache_dir = os.path.abspath(cache_dir or map_cache_build_dir(obj_path))
    cache_parent = os.path.dirname(cache_dir)
    assets = tuple(assets)
    resolved_max_upload_group_mb = _resolve_max_upload_group_mb(max_upload_group_mb)
    resolved_max_upload_group_bytes = _max_upload_group_bytes_from_mb(
        resolved_max_upload_group_mb
    )
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
            max_upload_group_mb=resolved_max_upload_group_mb,
            max_upload_group_bytes=resolved_max_upload_group_bytes,
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
    max_upload_group_mb: float | None = None,
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
    resolved_max_upload_group_mb = _resolve_max_upload_group_mb(max_upload_group_mb)
    resolved_max_upload_group_bytes = _max_upload_group_bytes_from_mb(
        resolved_max_upload_group_mb
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
            max_upload_group_mb=resolved_max_upload_group_mb,
            max_upload_group_bytes=resolved_max_upload_group_bytes,
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


def _build_incremental_obj_cache_in_directory(
    obj_path: str,
    materials: dict,
    cache_dir: str,
    *,
    chunk_size: float = DEFAULT_CHUNK_SIZE,
    progress_cb=None,
    face_batch_size: int | None = None,
    bucket_workers: int | None = None,
    max_upload_group_mb: float | None = None,
    max_upload_group_bytes: int | None = None,
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
    max_upload_group_mb = _resolve_max_upload_group_mb(max_upload_group_mb)
    max_upload_group_bytes = (
        _max_upload_group_bytes_from_mb(max_upload_group_mb)
        if max_upload_group_bytes is None
        else max(1, int(max_upload_group_bytes))
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
        max_group_bytes=max_upload_group_bytes,
    )
    shutil.rmtree(bucket_root, ignore_errors=True)

    emit_progress("writing manifest", 0.98)

    footprint_cell_size, footprint_flat = _footprint_from_positions(
        vertex_data.positions
    )
    manifest = {
        "version": _VERSION,
        "chunk_size": chunk_size,
        "max_upload_group_mb": max_upload_group_mb,
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
    _attach_navigation_metadata(
        manifest,
        surface_positions=vertex_data.positions,
        navigation_start=_navigation_start_sidecar_for_obj(obj_path),
        cache_dir=cache_dir,
    )
    with open(os.path.join(cache_dir, MANIFEST_NAME), "w") as f:
        json.dump(manifest, f)

    return cache_dir


def _build_cache_in_directory(obj_path: str, mesh: RawMesh, materials: dict,
                              cache_dir: str,
                              chunk_size: float = DEFAULT_CHUNK_SIZE,
                              progress_cb=None,
                              *,
                              max_upload_group_mb: float | None = None,
                              max_upload_group_bytes: int | None = None) -> str:
    """Build all cache artifacts inside an unpublished staging directory."""
    chunks_dir = os.path.join(cache_dir, CHUNKS_DIRNAME)
    os.makedirs(chunks_dir, exist_ok=True)
    max_upload_group_mb = _resolve_max_upload_group_mb(max_upload_group_mb)
    max_upload_group_bytes = (
        _max_upload_group_bytes_from_mb(max_upload_group_mb)
        if max_upload_group_bytes is None
        else max(1, int(max_upload_group_bytes))
    )

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
            chunks_dir,
            cell_str,
            mesh,
            groups,
            max_group_bytes=max_upload_group_bytes,
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
                    snapshot = system_memory.detect_ram_snapshot()
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
        "max_upload_group_mb": max_upload_group_mb,
        "source_obj": os.path.basename(obj_path),
        "mtl_materials": {
            name: mat.diffuse_texture for name, mat in materials.items()
        },
        "chunks": manifest_chunks,
        "footprint_cell_size": footprint_cell_size,
        "footprint_cells": footprint_flat,
        "triangle_count": int(n_faces),
    }
    _attach_navigation_metadata(
        manifest,
        surface_positions=mesh.positions,
        navigation_start=_navigation_start_sidecar_for_obj(obj_path),
        cache_dir=cache_dir,
    )
    with open(os.path.join(cache_dir, MANIFEST_NAME), "w") as f:
        json.dump(manifest, f)

    return cache_dir


def _attach_navigation_metadata(
    manifest: dict,
    *,
    surface_positions: np.ndarray | None,
    navigation_start: dict | None = None,
    cache_dir: str | None = None,
) -> None:
    """Attach optional navigation metadata without affecting cache validity."""
    try:
        navigation_metadata = build_navigation_metadata(
            manifest,
            surface_positions=surface_positions,
            navigation_start=navigation_start,
        )
    except Exception as exc:
        _LOG.warning(
            "Could not build optional navigation metadata; "
            "cache remains usable without it: %s",
            exc,
        )
        return
    if navigation_metadata is not None:
        manifest["navigation"] = navigation_metadata
        if cache_dir:
            try:
                mesh_guard = CachedChunkMeshCollisionGuard.from_manifest(
                    manifest,
                    cache_dir=cache_dir,
                )
                if mesh_guard is None:
                    _LOG.info(
                        "Skipping cache-time navigation voxel analysis: "
                        "cached mesh provider unavailable."
                    )
                    return
                voxel_result = build_navigation_voxel_cache(
                    manifest,
                    navigation_metadata,
                    triangle_provider=mesh_guard.triangle_meshes_for_bounds,
                )
                if voxel_result.built_route_count:
                    published_payload = (
                        voxel_result.chunked_payload
                        if voxel_result.chunked_payload is not None
                        else voxel_result.payload
                    )
                    for relative_path, chunk_payload in (
                        voxel_result.chunk_payloads.items()
                    ):
                        _atomic_write_json(
                            os.path.join(cache_dir, relative_path),
                            dict(chunk_payload),
                        )
                    _atomic_write_json(
                        os.path.join(cache_dir, NAVIGATION_VOXEL_CACHE_NAME),
                        published_payload,
                    )
                    _LOG.info(
                        "Built whole-cave navigation voxel atlases for %d route(s) "
                        "using %s with %d persisted chunk(s); recommended route=%s.",
                        voxel_result.built_route_count,
                        published_payload.get(
                            "storage_method",
                            "embedded_memory",
                        ),
                        len(voxel_result.chunk_payloads),
                        voxel_result.recommended_route_id,
                    )
            except Exception as exc:
                navigation_metadata.pop("voxel_cache", None)
                _LOG.warning(
                    "Could not build optional cache-time navigation voxel data; "
                    "cache remains usable without it: %s",
                    exc,
                )


def _navigation_start_sidecar_for_obj(obj_path: str) -> dict | None:
    """Return optional navigation start metadata from a source-model sidecar."""
    base_path, _extension = os.path.splitext(os.path.abspath(obj_path))
    source_dir = os.path.dirname(os.path.abspath(obj_path))
    candidate_paths = (
        f"{base_path}.navigation.json",
        os.path.join(source_dir, "navigation.json"),
    )
    seen: set[str] = set()
    for path in candidate_paths:
        if path in seen:
            continue
        seen.add(path)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            _LOG.warning("Could not read navigation sidecar %s: %s", path, exc)
            return None
        if not isinstance(payload, dict):
            _LOG.warning("Ignoring navigation sidecar %s: expected a JSON object.", path)
            return None
        result = dict(payload)
        result.setdefault("source", os.path.basename(path))
        return result
    return None


def load_chunk_file(
    cache_dir: str,
    cell: tuple[int, int, int],
    *,
    max_group_bytes: int | None = None,
) -> ChunkData:
    return _io_load_chunk_file(
        cache_dir,
        cell,
        max_group_bytes=max_group_bytes,
        max_file_bytes=_MAX_CHUNK_FILE_BYTES,
        max_file_groups=_MAX_CHUNK_FILE_GROUPS,
        max_material_name_bytes=_MAX_CHUNK_MATERIAL_NAME_BYTES,
    )
