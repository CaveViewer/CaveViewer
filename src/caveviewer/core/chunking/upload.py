"""CPU-side chunk upload preparation and vertex-byte packing."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from caveviewer.core.diagnostics.logging import get_logger

if TYPE_CHECKING:
    from caveviewer.core.chunking.io import ChunkData


MAX_UPLOAD_GROUP_MB_ENV_VAR = "CAVEVIEWER_MAX_UPLOAD_GROUP_MB"

_DEFAULT_MAX_UPLOAD_GROUP_MB = 16.0
_MIN_MAX_UPLOAD_GROUP_MB = 1.0
_MAX_MAX_UPLOAD_GROUP_MB = 512.0
_UPLOAD_VERTEX_BYTES = 8 * np.dtype(np.float32).itemsize
_LOG = get_logger("chunker")


def configured_max_upload_group_mb(
    environ: dict[str, str] | None = None,
) -> float:
    """Return the configured maximum renderer upload-group payload in MB."""
    env = os.environ if environ is None else environ
    raw = env.get(MAX_UPLOAD_GROUP_MB_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_MAX_UPLOAD_GROUP_MB
    try:
        value = float(raw)
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError("must be finite and > 0")
    except Exception:
        _LOG.warning(
            "ignoring invalid %s=%r; using default %.1f MB",
            MAX_UPLOAD_GROUP_MB_ENV_VAR,
            raw,
            _DEFAULT_MAX_UPLOAD_GROUP_MB,
        )
        return _DEFAULT_MAX_UPLOAD_GROUP_MB
    return max(_MIN_MAX_UPLOAD_GROUP_MB, min(_MAX_MAX_UPLOAD_GROUP_MB, value))


def configured_max_upload_group_bytes(
    environ: dict[str, str] | None = None,
) -> int:
    return max(1, int(configured_max_upload_group_mb(environ) * 1024 ** 2))


def _compute_flat_normals(flat_pos: np.ndarray) -> np.ndarray:
    """Return per-triangle flat normals duplicated across triangle vertices."""
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
    Public entry point for per-triangle flat-normal computation.

    ``flat_pos`` must already be de-indexed/flat with shape ``(N * 3, 3)``.
    That is the shape used by loaded chunk material groups, which makes this
    safe for recomputing flat-shaded normals from already-streamed CPU data.
    """
    return _compute_flat_normals(flat_pos)


def _max_upload_group_vertices(max_group_bytes: int | None = None) -> int:
    resolved_bytes = (
        configured_max_upload_group_bytes()
        if max_group_bytes is None
        else max(1, int(max_group_bytes))
    )
    vertices = max(3, resolved_bytes // _UPLOAD_VERTEX_BYTES)
    # Renderer payloads are flat triangle lists. Keep split groups
    # triangle-aligned so normals, wireframe, and culling assumptions stay
    # unchanged. A single triangle is the hard minimum.
    vertices -= vertices % 3
    return max(3, int(vertices))


def _upload_group_vertex_ranges(
    vertex_count: int,
    *,
    max_group_bytes: int | None = None,
) -> list[tuple[int, int]]:
    if vertex_count <= 0:
        return []

    max_vertices = _max_upload_group_vertices(max_group_bytes)
    if vertex_count <= max_vertices:
        return [(0, int(vertex_count))]

    ranges: list[tuple[int, int]] = []
    start = 0
    vertex_count = int(vertex_count)
    while start < vertex_count:
        end = min(vertex_count, start + max_vertices)
        if end < vertex_count:
            end -= (end - start) % 3
        if end <= start:
            end = min(vertex_count, start + max_vertices)
        ranges.append((start, end))
        start = end
    return ranges


def _upload_group_face_ranges(
    face_count: int,
    *,
    max_group_bytes: int | None = None,
) -> list[tuple[int, int]]:
    if face_count <= 0:
        return []
    max_faces = max(1, _max_upload_group_vertices(max_group_bytes) // 3)
    if face_count <= max_faces:
        return [(0, int(face_count))]
    return [
        (start, min(int(face_count), start + max_faces))
        for start in range(0, int(face_count), max_faces)
    ]


def _interleaved_vertex_bytes(
    positions: np.ndarray,
    uvs: np.ndarray,
    normals: np.ndarray,
) -> bytes:
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


@dataclass
class ChunkUploadGroup:
    """CPU-side source data for one material group in a chunk.

    OpenGL object creation still has to happen on the render thread, but the
    expensive chunk-file decode happens in a streaming worker before the chunk
    reaches the renderer. A worker may prepack one vertex-byte payload for the
    shade mode expected at upload time; the source arrays remain available so a
    late SHADE toggle can still fall back to building the other mode correctly.
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


def prepare_chunk_upload_groups(
    chunk_data: ChunkData,
    *,
    max_group_bytes: int | None = None,
) -> ChunkData:
    """
    Precompute CPU-side renderer payloads for a loaded chunk.

    This deliberately does no OpenGL work. It is safe to call from a background
    streaming worker and leaves the render thread with only context-bound
    buffer/VAO/texture operations.
    """
    upload_groups: list[ChunkUploadGroup] = []
    for _group_key, group in chunk_data.groups.items():
        n = len(group.positions)
        if n == 0:
            continue

        for start, end in _upload_group_vertex_ranges(
            n,
            max_group_bytes=max_group_bytes,
        ):
            upload_groups.append(ChunkUploadGroup(
                material_name=group.material_name,
                positions=group.positions[start:end],
                uvs=group.uvs[start:end],
                smooth_normals=group.normals[start:end],
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
