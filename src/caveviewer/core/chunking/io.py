"""Chunk binary file format read/write helpers."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from caveviewer.core.chunking.upload import (
    _compute_flat_normals,
    _max_upload_group_vertices,
    _upload_group_face_ranges,
    configured_max_upload_group_bytes,
)

if TYPE_CHECKING:
    from caveviewer.core.mesh.obj import RawMesh


CHUNKS_DIRNAME = "chunks"

_CHUNK_FILE_HEADER_BYTES = 12
_CHUNK_GROUP_HEADER_BYTES = 8
_MAX_CHUNK_FILE_BYTES = 2 * 1024 ** 3
_MAX_CHUNK_FILE_GROUPS = 65_536
_MAX_CHUNK_MATERIAL_NAME_BYTES = 64 * 1024
_MAGIC = b"CVCH"  # CaveViewer CHunk
_VERSION = 1


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
    upload_groups: list[object] | None = None


def _write_chunk_file(
    chunks_dir: str,
    cell_str: str,
    mesh: RawMesh,
    groups: list[tuple[str, np.ndarray]],
    *,
    max_group_bytes: int | None = None,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
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
    max_group_bytes = (
        configured_max_upload_group_bytes()
        if max_group_bytes is None
        else max(1, int(max_group_bytes))
    )
    split_groups: list[tuple[str, object]] = []
    for mat_name, face_idx in groups:
        ranges = _upload_group_face_ranges(
            len(face_idx),
            max_group_bytes=max_group_bytes,
        )
        if not ranges:
            ranges = [(0, 0)]
        for start, end in ranges:
            split_groups.append((mat_name, face_idx[start:end]))

    bounds_min = None
    bounds_max = None
    used_materials = []

    with open(path, "wb") as f:
        f.write(_MAGIC)
        f.write(struct.pack("<I", _VERSION))
        f.write(struct.pack("<I", len(split_groups)))

        for mat_name, face_idx in split_groups:
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

            if mat_name not in used_materials:
                used_materials.append(mat_name)

    if bounds_min is None:
        bounds_min = np.zeros(3, dtype=np.float32)
        bounds_max = np.zeros(3, dtype=np.float32)
    return bounds_min, bounds_max, used_materials


def _read_exact_chunk_bytes(file_obj, path: str, size: int, description: str) -> bytes:
    if size < 0:
        raise ValueError(f"Invalid negative byte count while reading {description} in {path}")
    payload = file_obj.read(size)
    if len(payload) != size:
        raise ValueError(f"Truncated chunk file while reading {description} in {path}")
    return payload


def load_chunk_file(
    cache_dir: str,
    cell: tuple[int, int, int],
    *,
    max_group_bytes: int | None = None,
    max_file_bytes: int | None = None,
    max_file_groups: int | None = None,
    max_material_name_bytes: int | None = None,
) -> ChunkData:
    cell_str = f"{cell[0]}_{cell[1]}_{cell[2]}"
    path = os.path.join(cache_dir, CHUNKS_DIRNAME, f"{cell_str}.bin")
    file_size = os.path.getsize(path)
    max_file_bytes = (
        _MAX_CHUNK_FILE_BYTES
        if max_file_bytes is None
        else max(1, int(max_file_bytes))
    )
    max_file_groups = (
        _MAX_CHUNK_FILE_GROUPS
        if max_file_groups is None
        else max(0, int(max_file_groups))
    )
    max_material_name_bytes = (
        _MAX_CHUNK_MATERIAL_NAME_BYTES
        if max_material_name_bytes is None
        else max(0, int(max_material_name_bytes))
    )

    if file_size > max_file_bytes:
        raise ValueError(
            f"Chunk file {path} is {file_size} bytes, above the "
            f"{max_file_bytes} byte runtime safety limit"
        )
    if file_size < _CHUNK_FILE_HEADER_BYTES:
        raise ValueError(f"Truncated chunk file while reading header in {path}")

    resolved_max_group_bytes = (
        configured_max_upload_group_bytes()
        if max_group_bytes is None
        else max(1, int(max_group_bytes))
    )
    max_group_vertices = _max_upload_group_vertices(resolved_max_group_bytes)

    groups = {}
    bmin = None
    bmax = None
    bytes_read = 0
    with open(path, "rb") as f:
        def read_exact(size: int, description: str) -> bytes:
            nonlocal bytes_read
            payload = _read_exact_chunk_bytes(f, path, size, description)
            bytes_read += size
            if bytes_read > file_size:
                raise ValueError(f"Chunk file changed while reading {path}")
            return payload

        magic = read_exact(4, "magic")
        if magic != _MAGIC:
            raise ValueError(f"Bad chunk file magic in {path}")

        version = struct.unpack("<I", read_exact(4, "version"))[0]
        if version != _VERSION:
            raise ValueError(f"Unsupported chunk version {version} in {path}")

        n_groups = struct.unpack("<I", read_exact(4, "group count"))[0]
        if n_groups > max_file_groups:
            raise ValueError(
                f"Chunk file {path} declares {n_groups} material groups, "
                f"above the {max_file_groups} group runtime safety limit"
            )
        min_remaining = n_groups * _CHUNK_GROUP_HEADER_BYTES
        if bytes_read + min_remaining > file_size:
            raise ValueError(f"Truncated chunk file while reading group headers in {path}")

        for group_index in range(n_groups):
            name_len = struct.unpack(
                "<I",
                read_exact(4, f"material name length for group {group_index}"),
            )[0]
            if name_len > max_material_name_bytes:
                raise ValueError(
                    f"Chunk file {path} declares a {name_len} byte material "
                    f"name for group {group_index}, above the "
                    f"{max_material_name_bytes} byte runtime safety limit"
                )
            name_bytes = read_exact(name_len, f"material name for group {group_index}")
            try:
                name = name_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"Invalid UTF-8 material name in group {group_index} of {path}"
                ) from exc

            n_verts = struct.unpack(
                "<I",
                read_exact(4, f"vertex count for material {name!r}"),
            )[0]
            if n_verts % 3 != 0:
                raise ValueError(
                    f"Chunk material {name!r} in {path} declares {n_verts} "
                    "vertices; chunk payloads must be triangle-aligned"
                )
            if n_verts > max_group_vertices:
                raise ValueError(
                    f"Chunk material {name!r} in {path} declares {n_verts} "
                    f"vertices, above the {max_group_vertices} vertex runtime "
                    "safety limit for one material group"
                )

            pos_count = n_verts * 3
            uv_count = n_verts * 2
            nrm_count = n_verts * 3
            positions = np.frombuffer(
                read_exact(pos_count * 4, f"positions for material {name!r}"),
                dtype=np.float32,
                count=pos_count,
            ).reshape(n_verts, 3)
            uvs = np.frombuffer(
                read_exact(uv_count * 4, f"texture coordinates for material {name!r}"),
                dtype=np.float32,
                count=uv_count,
            ).reshape(n_verts, 2)
            normals = np.frombuffer(
                read_exact(nrm_count * 4, f"normals for material {name!r}"),
                dtype=np.float32,
                count=nrm_count,
            ).reshape(n_verts, 3)

            group_key = name
            if group_key in groups:
                duplicate_index = 2
                while f"{name}#{duplicate_index}" in groups:
                    duplicate_index += 1
                group_key = f"{name}#{duplicate_index}"
            groups[group_key] = ChunkMaterialGroup(name, positions, uvs, normals)
            if len(positions):
                group_min = positions.min(axis=0)
                group_max = positions.max(axis=0)
                if bmin is None:
                    bmin = group_min.copy()
                    bmax = group_max.copy()
                else:
                    np.minimum(bmin, group_min, out=bmin)
                    np.maximum(bmax, group_max, out=bmax)

        if bytes_read != file_size:
            raise ValueError(
                f"Chunk file {path} has {file_size - bytes_read} trailing byte(s)"
            )

    if bmin is None:
        bmin = np.zeros(3, dtype=np.float32)
        bmax = np.zeros(3, dtype=np.float32)

    return ChunkData(cell=cell, groups=groups, bounds_min=bmin, bounds_max=bmax)
