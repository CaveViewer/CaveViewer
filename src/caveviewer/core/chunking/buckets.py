"""Incremental chunk bucket files and bucket-to-chunk finalization."""

from __future__ import annotations

import os
import struct
from typing import Callable

import numpy as np

from caveviewer.core.chunking.io import _MAGIC, _VERSION
from caveviewer.core.chunking.upload import (
    _compute_flat_normals,
    _upload_group_vertex_ranges,
    configured_max_upload_group_bytes,
)
from caveviewer.core.mesh.obj import ObjFaceBatch, ObjVertexData


_OBJ_BUCKET_RECORD_SLICE_FACES = 25_000
_OBJ_BUCKET_FINALIZE_BLOCK_RECORDS = 100_000
_BUCKET_RECORD_FLOATS = 8
_BUCKET_RECORD_BYTES = _BUCKET_RECORD_FLOATS * np.dtype(np.float32).itemsize


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
    # (faces, 3, 3) position array. Only the final cell coordinates are kept
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
    axis_bits = 100_000
    packed = cell_coords[:, 0].astype(np.int64, copy=False) - cell_min[0]
    packed *= axis_bits * axis_bits
    shifted = cell_coords[:, 1].astype(np.int64, copy=False) - cell_min[1]
    shifted *= axis_bits
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
        cz = int(cell_packed % axis_bits)
        cy = int((cell_packed // axis_bits) % axis_bits)
        cx = int(cell_packed // (axis_bits * axis_bits))
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
    records = np.empty((len(pos_idx), _BUCKET_RECORD_FLOATS), dtype=np.float32)
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
        record_faces = records.reshape(-1, 3, _BUCKET_RECORD_FLOATS)
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
    *,
    max_group_bytes: int | None = None,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    path = os.path.join(chunks_dir, f"{cell_str}.bin")
    bounds_min = None
    bounds_max = None
    used_materials = []
    max_group_bytes = (
        configured_max_upload_group_bytes()
        if max_group_bytes is None
        else max(1, int(max_group_bytes))
    )
    split_groups: list[tuple[str, list[str], int, int]] = []
    for material_name, bucket_paths in groups:
        record_count = 0
        for bucket_path in bucket_paths:
            record_count += _bucket_record_count(bucket_path)
        ranges = _upload_group_vertex_ranges(
            record_count,
            max_group_bytes=max_group_bytes,
        )
        if not ranges:
            ranges = [(0, 0)]
        for start, end in ranges:
            split_groups.append((material_name, bucket_paths, start, end))

    with open(path, "wb") as output:
        output.write(_MAGIC)
        output.write(struct.pack("<I", _VERSION))
        output.write(struct.pack("<I", len(split_groups)))

        for material_name, bucket_paths, start, end in split_groups:
            record_count = end - start
            name_bytes = material_name.encode("utf-8")
            output.write(struct.pack("<I", len(name_bytes)))
            output.write(name_bytes)
            output.write(struct.pack("<I", record_count))

            for records in _iter_bucket_record_blocks_for_range(
                bucket_paths, start, end
            ):
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

            for records in _iter_bucket_record_blocks_for_range(
                bucket_paths, start, end
            ):
                flat_uv = np.ascontiguousarray(
                    records[:, 3:5],
                    dtype=np.float32,
                )
                output.write(flat_uv.tobytes())

            for records in _iter_bucket_record_blocks_for_range(
                bucket_paths, start, end
            ):
                flat_nrm = np.ascontiguousarray(
                    records[:, 5:8],
                    dtype=np.float32,
                )
                output.write(flat_nrm.tobytes())

            if material_name not in used_materials:
                used_materials.append(material_name)

        for _material_name, bucket_paths in groups:
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
    byte_count = os.path.getsize(bucket_path)
    if byte_count % _BUCKET_RECORD_BYTES != 0:
        raise ValueError(f"Corrupt incremental bucket: {bucket_path}")
    return byte_count // _BUCKET_RECORD_BYTES


def _iter_bucket_record_blocks_for_range(
    bucket_paths: list[str],
    start: int,
    end: int,
):
    """Yield contiguous record blocks for [start, end) across bucket files."""
    start = max(0, int(start))
    end = max(start, int(end))
    read_records = max(1, _OBJ_BUCKET_FINALIZE_BLOCK_RECORDS)
    absolute_offset = 0
    for bucket_path in bucket_paths:
        bucket_count = _bucket_record_count(bucket_path)
        bucket_start = absolute_offset
        bucket_end = absolute_offset + bucket_count
        absolute_offset = bucket_end

        if bucket_end <= start:
            continue
        if bucket_start >= end:
            break

        local_start = max(0, start - bucket_start)
        local_end = min(bucket_count, end - bucket_start)
        remaining = local_end - local_start
        if remaining <= 0:
            continue

        with open(bucket_path, "rb") as input_file:
            input_file.seek(local_start * _BUCKET_RECORD_BYTES)
            while remaining > 0:
                records_to_read = min(read_records, remaining)
                records = np.fromfile(
                    input_file,
                    dtype=np.float32,
                    count=records_to_read * _BUCKET_RECORD_FLOATS,
                )
                if records.size != records_to_read * _BUCKET_RECORD_FLOATS:
                    raise ValueError(f"Corrupt incremental bucket: {bucket_path}")
                yield records.reshape(-1, _BUCKET_RECORD_FLOATS)
                remaining -= records_to_read
