"""
caveviewer.core.chunker

Spatial partitioning of a parsed mesh into a 3D grid of chunks, cached to
disk in a fast-to-load binary format. This is the piece that makes large
cave maps viewable: instead of one giant draw call / VRAM blob for the
whole cave, we split the mesh into cells (default 8m cubes -- tune via
CHUNK_SIZE for your cave's scale) and load only the cells near the camera
at runtime (see caveviewer.core.streaming_world).

Cache layout on disk, under <obj_folder>/_cache/:
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
from dataclasses import dataclass

import numpy as np

from caveviewer.core.logging_utils import get_logger
from caveviewer.core.worker_config import resolve_worker_count
from caveviewer.core.obj_parser import RawMesh, MaterialRange

CACHE_DIRNAME = "_cache"
LEGACY_CACHE_DIRNAME = ".caveviewer_cache"
MANIFEST_NAME = "manifest.json"
CHUNKS_DIRNAME = "chunks"
IMPORT_DISK_SPACE_MULTIPLIER = 2

CHUNK_SIZE_ENV_VAR = "CAVEVIEWER_CHUNK_SIZE_METERS"
_DEFAULT_CHUNK_SIZE_FALLBACK = 8.0  # meters; preserve existing default behavior
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

    def __init__(self, source_path: str, required_bytes: int, available_bytes: int):
        self.source_path = source_path
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        source_size = required_bytes // IMPORT_DISK_SPACE_MULTIPLIER
        message = (
            f"Not enough disk space to import {os.path.basename(source_path)!r}: "
            f"{available_bytes:,} bytes are available, but at least "
            f"{required_bytes:,} bytes are required (twice the "
            f"{source_size:,}-byte map size). Free disk space and try again."
        )
        super().__init__(errno.ENOSPC, message)


def configured_chunk_size() -> float:
    """Return the chunk size currently used by default for cache builds."""
    return DEFAULT_CHUNK_SIZE


@dataclass
class ChunkUploadGroup:
    """CPU-prepared vertex payload for one material group in a chunk.

    OpenGL object creation still has to happen on the render thread, but
    normal generation, interleaving, and bytes conversion can happen in a
    streaming worker before the chunk reaches the renderer.
    """
    material_name: str
    smooth_vertex_bytes: bytes
    flat_vertex_bytes: bytes


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


def ensure_sufficient_disk_space(source_path: str) -> None:
    """Require free space equal to at least twice the source map size.

    Cache construction expands indexed source geometry into render-ready
    chunks, so starting an import without this headroom is likely to fail
    after doing substantial work. The check targets the filesystem that will
    hold the cache: the directory containing source_path.
    """
    source_path = os.path.abspath(source_path)
    source_size = os.path.getsize(source_path)
    required_bytes = source_size * IMPORT_DISK_SPACE_MULTIPLIER
    available_bytes = shutil.disk_usage(os.path.dirname(source_path)).free
    if available_bytes < required_bytes:
        raise InsufficientDiskSpaceError(
            source_path, required_bytes, available_bytes
        )


def build_cache(obj_path: str, mesh: RawMesh, materials: dict,
                 chunk_size: float = DEFAULT_CHUNK_SIZE,
                 progress_cb=None) -> str:
    """
    Partition `mesh` into spatial chunks and write the disk cache next to
    `obj_path`. Returns the cache directory path.

    progress_cb(stage: str, fraction: float)
    """
    obj_dir = os.path.dirname(os.path.abspath(obj_path))
    ensure_sufficient_disk_space(obj_path)

    cache_dir = os.path.join(obj_dir, CACHE_DIRNAME)
    staging_dir = tempfile.mkdtemp(prefix=f".{CACHE_DIRNAME}.tmp-", dir=obj_dir)
    try:
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

    worker_count = resolve_worker_count(
        os.environ.get("CAVEVIEWER_CHUNK_BUILD_WORKERS"),
        os.environ.get("CAVEVIEWER_CHUNK_BUILD_RESERVED_CPUS"),
        default_workers=1,
        default_reserved_cpus=2,
    )

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

        def _submit_next_cell() -> bool:
            try:
                cell_coord, groups = next(cell_iterator)
            except StopIteration:
                return False
            active_futures.add(executor.submit(_write_one_cell, cell_coord, groups))
            return True

        for _ in range(min(worker_count, total_cells)):
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

                for _ in completed_results:
                    _submit_next_cell()
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
    _FOOTPRINT_TARGET_CELLS = 200
    pos_x = mesh.positions[:, 0]
    pos_z = mesh.positions[:, 2]
    extent_max = max(
        float(pos_x.max() - pos_x.min()),
        float(pos_z.max() - pos_z.min()),
        1.0,
    )
    footprint_cell_size = max(2.0, extent_max / _FOOTPRINT_TARGET_CELLS)
    fine_cx = np.floor(pos_x / footprint_cell_size).astype(np.int32)
    fine_cz = np.floor(pos_z / footprint_cell_size).astype(np.int32)
    footprint_pairs = np.unique(np.column_stack([fine_cx, fine_cz]), axis=0)
    footprint_flat = footprint_pairs.flatten().tolist()
    del fine_cx, fine_cz, footprint_pairs

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

        smooth_normals = group.normals
        flat_normals = compute_flat_normals(group.positions)
        upload_groups.append(ChunkUploadGroup(
            material_name=mat_name,
            smooth_vertex_bytes=_interleaved_vertex_bytes(group.positions, group.uvs, smooth_normals),
            flat_vertex_bytes=_interleaved_vertex_bytes(group.positions, group.uvs, flat_normals),
        ))

    chunk_data.upload_groups = upload_groups
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
    obj_dir = os.path.dirname(os.path.abspath(obj_path))
    candidates = [
        os.path.join(obj_dir, CACHE_DIRNAME),
        os.path.join(obj_dir, LEGACY_CACHE_DIRNAME),
    ]
    for cache_dir in candidates:
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
    obj_dir = os.path.dirname(os.path.abspath(obj_path))
    preferred = os.path.join(obj_dir, CACHE_DIRNAME)
    preferred_manifest = os.path.join(preferred, MANIFEST_NAME)
    if os.path.exists(preferred_manifest):
        return preferred

    legacy = os.path.join(obj_dir, LEGACY_CACHE_DIRNAME)
    legacy_manifest = os.path.join(legacy, MANIFEST_NAME)
    if os.path.exists(legacy_manifest):
        return legacy

    return preferred


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
