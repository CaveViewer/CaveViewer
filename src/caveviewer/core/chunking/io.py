"""Chunk binary file format read/write helpers."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from collections.abc import Iterator
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


@dataclass(frozen=True, slots=True)
class ChunkFileWriteResult:
    """Metadata for one chunk file written without constructing a RawMesh."""

    cell: tuple[int, int, int]
    bounds_min: np.ndarray
    bounds_max: np.ndarray
    materials: tuple[str, ...]
    triangle_count: int


class ChunkFileWriter:
    """Write flat material groups incrementally into one chunk file.

    The regular cache builder starts with indexed OBJ data and therefore uses
    :func:`_write_chunk_file`.  Derivative cache operations, such as slicing,
    already have flat vertex data and must not first reconstruct a full mesh in
    memory.  This writer keeps every written group bounded and patches the
    group count once the file is complete.
    """

    def __init__(
        self,
        chunks_dir: str,
        cell: tuple[int, int, int],
        *,
        max_group_bytes: int | None = None,
    ) -> None:
        self.cell = tuple(int(axis) for axis in cell)
        self.path = os.path.join(
            chunks_dir,
            f"{self.cell[0]}_{self.cell[1]}_{self.cell[2]}.bin",
        )
        os.makedirs(chunks_dir, exist_ok=True)
        self._max_group_vertices = _max_upload_group_vertices(
            configured_max_upload_group_bytes()
            if max_group_bytes is None
            else max(1, int(max_group_bytes))
        )
        self._file = open(self.path, "wb+")
        self._file.write(_MAGIC)
        self._file.write(struct.pack("<I", _VERSION))
        self._file.write(struct.pack("<I", 0))
        self._group_count = 0
        self._triangle_count = 0
        self._bounds_min: np.ndarray | None = None
        self._bounds_max: np.ndarray | None = None
        self._materials: list[str] = []
        self._finished = False

    def write_group(
        self,
        material_name: str,
        positions: np.ndarray,
        uvs: np.ndarray,
        normals: np.ndarray,
    ) -> None:
        """Append one or more triangle-aligned groups for ``material_name``."""
        if self._finished:
            raise RuntimeError("Cannot write to a finished chunk file")
        name = str(material_name)
        name_bytes = name.encode("utf-8")
        if len(name_bytes) > _MAX_CHUNK_MATERIAL_NAME_BYTES:
            raise ValueError(
                f"Chunk material name is above the {_MAX_CHUNK_MATERIAL_NAME_BYTES} "
                "byte safety limit"
            )

        flat_positions = _as_chunk_vertices(positions, 3, "positions")
        flat_uvs = _as_chunk_vertices(uvs, 2, "texture coordinates")
        flat_normals = _as_chunk_vertices(normals, 3, "normals")
        vertex_count = len(flat_positions)
        if len(flat_uvs) != vertex_count or len(flat_normals) != vertex_count:
            raise ValueError("Chunk material attributes must have matching vertex counts")
        if vertex_count % 3:
            raise ValueError("Chunk material vertex counts must be triangle-aligned")
        if not vertex_count:
            return
        if not (
            np.isfinite(flat_positions).all()
            and np.isfinite(flat_uvs).all()
            and np.isfinite(flat_normals).all()
        ):
            raise ValueError("Chunk material attributes must contain only finite values")

        for start in range(0, vertex_count, self._max_group_vertices):
            end = min(vertex_count, start + self._max_group_vertices)
            # ``_max_upload_group_vertices`` is triangle-aligned, but retain
            # this guard for custom callers and future upload policy changes.
            end -= (end - start) % 3
            if end <= start:
                raise ValueError("Chunk upload-group limit cannot hold one triangle")
            self._write_one_group(
                name,
                name_bytes,
                flat_positions[start:end],
                flat_uvs[start:end],
                flat_normals[start:end],
            )

    def _write_one_group(
        self,
        material_name: str,
        name_bytes: bytes,
        positions: np.ndarray,
        uvs: np.ndarray,
        normals: np.ndarray,
    ) -> None:
        if self._group_count >= _MAX_CHUNK_FILE_GROUPS:
            raise ValueError(
                f"Chunk file {self.path} exceeds the {_MAX_CHUNK_FILE_GROUPS} group "
                "safety limit"
            )
        payload_bytes = len(name_bytes) + _CHUNK_GROUP_HEADER_BYTES + len(positions) * 32
        if self._file.tell() + payload_bytes > _MAX_CHUNK_FILE_BYTES:
            raise ValueError(
                f"Chunk file {self.path} would exceed the {_MAX_CHUNK_FILE_BYTES} byte "
                "runtime safety limit"
            )

        self._file.write(struct.pack("<I", len(name_bytes)))
        self._file.write(name_bytes)
        self._file.write(struct.pack("<I", len(positions)))
        self._file.write(positions.tobytes())
        self._file.write(uvs.tobytes())
        self._file.write(normals.tobytes())
        self._group_count += 1
        self._triangle_count += len(positions) // 3
        if material_name not in self._materials:
            self._materials.append(material_name)

        group_min = positions.min(axis=0)
        group_max = positions.max(axis=0)
        if self._bounds_min is None:
            self._bounds_min = group_min.copy()
            self._bounds_max = group_max.copy()
        else:
            np.minimum(self._bounds_min, group_min, out=self._bounds_min)
            np.maximum(self._bounds_max, group_max, out=self._bounds_max)

    def finish(self) -> ChunkFileWriteResult | None:
        """Finalize the file, returning ``None`` when no geometry was written."""
        if self._finished:
            raise RuntimeError("Chunk file has already been finished")
        self._finished = True
        try:
            if not self._group_count:
                self._file.close()
                os.unlink(self.path)
                return None
            self._file.seek(8)
            self._file.write(struct.pack("<I", self._group_count))
            self._file.close()
            assert self._bounds_min is not None
            assert self._bounds_max is not None
            return ChunkFileWriteResult(
                cell=self.cell,
                bounds_min=self._bounds_min,
                bounds_max=self._bounds_max,
                materials=tuple(self._materials),
                triangle_count=self._triangle_count,
            )
        except BaseException:
            self.abort()
            raise

    def abort(self) -> None:
        """Close and remove an unpublished or failed chunk file."""
        if not self._finished:
            self._finished = True
        try:
            if not self._file.closed:
                self._file.close()
        finally:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def _as_chunk_vertices(values: np.ndarray, width: int, label: str) -> np.ndarray:
    array = np.ascontiguousarray(np.asarray(values, dtype=np.float32))
    if array.ndim != 2 or array.shape[1] != width:
        raise ValueError(f"Chunk {label} must have shape (N, {width})")
    return array


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


def iter_chunk_file_groups(
    cache_dir: str,
    cell: tuple[int, int, int],
    *,
    block_vertices: int = 65_535,
    max_file_bytes: int | None = None,
    max_file_groups: int | None = None,
    max_material_name_bytes: int | None = None,
) -> Iterator[ChunkMaterialGroup]:
    """Yield bounded, triangle-aligned flat vertex blocks from one chunk.

    Unlike :func:`load_chunk_file`, this iterator never materializes every
    material group in a chunk at once.  It seeks to each contiguous attribute
    range in the v1 binary format, which keeps derivative-cache operations
    bounded even when a source chunk approaches the file-size safety limit.
    """
    cell = tuple(int(axis) for axis in cell)
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

    bounded_block_vertices = max(3, int(block_vertices))
    bounded_block_vertices -= bounded_block_vertices % 3
    if bounded_block_vertices < 3:
        raise ValueError("Chunk block size must hold at least one triangle")

    with open(path, "rb") as file_obj:
        def read_exact(size: int, description: str) -> bytes:
            return _read_exact_chunk_bytes(file_obj, path, size, description)

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
            name_bytes = read_exact(
                name_len,
                f"material name for group {group_index}",
            )
            try:
                material_name = name_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"Invalid UTF-8 material name in group {group_index} of {path}"
                ) from exc
            n_verts = struct.unpack(
                "<I",
                read_exact(4, f"vertex count for material {material_name!r}"),
            )[0]
            if n_verts % 3:
                raise ValueError(
                    f"Chunk material {material_name!r} in {path} declares {n_verts} "
                    "vertices; chunk payloads must be triangle-aligned"
                )

            payload_start = file_obj.tell()
            positions_bytes = n_verts * 3 * 4
            uvs_bytes = n_verts * 2 * 4
            normals_bytes = n_verts * 3 * 4
            payload_end = payload_start + positions_bytes + uvs_bytes + normals_bytes
            if payload_end > file_size:
                raise ValueError(
                    f"Truncated chunk file while reading material {material_name!r} "
                    f"in {path}"
                )

            for start in range(0, n_verts, bounded_block_vertices):
                count = min(bounded_block_vertices, n_verts - start)
                positions_offset = payload_start + start * 3 * 4
                uvs_offset = payload_start + positions_bytes + start * 2 * 4
                normals_offset = payload_start + positions_bytes + uvs_bytes + start * 3 * 4

                file_obj.seek(positions_offset)
                positions = np.frombuffer(
                    read_exact(count * 3 * 4, f"positions for material {material_name!r}"),
                    dtype=np.float32,
                    count=count * 3,
                ).reshape(count, 3)
                file_obj.seek(uvs_offset)
                uvs = np.frombuffer(
                    read_exact(
                        count * 2 * 4,
                        f"texture coordinates for material {material_name!r}",
                    ),
                    dtype=np.float32,
                    count=count * 2,
                ).reshape(count, 2)
                file_obj.seek(normals_offset)
                normals = np.frombuffer(
                    read_exact(count * 3 * 4, f"normals for material {material_name!r}"),
                    dtype=np.float32,
                    count=count * 3,
                ).reshape(count, 3)
                yield ChunkMaterialGroup(material_name, positions, uvs, normals)

            file_obj.seek(payload_end)

        if file_obj.tell() != file_size:
            raise ValueError(
                f"Chunk file {path} has {file_size - file_obj.tell()} trailing byte(s)"
            )
    if os.path.getsize(path) != file_size:
        raise ValueError(f"Chunk file changed while reading {path}")


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
