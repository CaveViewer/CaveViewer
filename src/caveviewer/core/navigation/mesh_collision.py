"""Cached chunk-mesh collision queries for navigation planning."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
import threading
from typing import Any

import numpy as np

from caveviewer.core.chunking.io import load_chunk_file
from caveviewer.core.navigation.centerline import (
    Cell,
    Point,
    parse_cell_key,
)

_DEFAULT_MAX_COLLISION_GUARD_TRIANGLES = 250_000
_MAX_COLLISION_GUARD_TRIANGLES_ENV = (
    "CAVEVIEWER_AUTO_DIVE_MESH_COLLISION_MAX_TRIANGLES"
)


@dataclass(frozen=True)
class MeshCollisionHit:
    """A segment/mesh intersection found in cached chunk geometry."""

    point: Point
    chunk_cell: Cell


@dataclass(frozen=True)
class _ChunkBounds:
    cell: Cell
    bounds_min: np.ndarray
    bounds_max: np.ndarray


class CachedChunkMeshCollisionGuard:
    """Segment collision guard backed by cached chunk triangle payloads."""

    def __init__(
        self,
        cache_dir: str,
        chunk_bounds: tuple[_ChunkBounds, ...],
    ) -> None:
        self._cache_dir = os.path.abspath(os.fspath(cache_dir))
        self._chunk_bounds = chunk_bounds
        self._triangle_cache: dict[Cell, np.ndarray] = {}
        self._cache_lock = threading.Lock()

    @classmethod
    def from_manifest(
        cls,
        manifest: Mapping[str, Any],
        *,
        cache_dir: str | os.PathLike[str] | None,
    ) -> "CachedChunkMeshCollisionGuard | None":
        if not cache_dir:
            return None
        chunks = manifest.get("chunks")
        if not isinstance(chunks, Mapping) or not chunks:
            return None
        if _manifest_exceeds_collision_guard_triangle_limit(manifest):
            return None
        chunk_bounds: list[_ChunkBounds] = []
        for raw_cell, info in chunks.items():
            if not isinstance(info, Mapping):
                continue
            try:
                cell = parse_cell_key(str(raw_cell))
                bounds_min = np.asarray(info["bounds_min"], dtype=np.float64).reshape(3)
                bounds_max = np.asarray(info["bounds_max"], dtype=np.float64).reshape(3)
            except Exception:
                continue
            chunk_bounds.append(
                _ChunkBounds(
                    cell=cell,
                    bounds_min=np.minimum(bounds_min, bounds_max),
                    bounds_max=np.maximum(bounds_min, bounds_max),
                )
            )
        if not chunk_bounds:
            return None
        return cls(
            os.fspath(cache_dir),
            tuple(sorted(chunk_bounds, key=lambda chunk: chunk.cell)),
        )

    def segment_collision(
        self,
        first: Point,
        second: Point,
    ) -> MeshCollisionHit | None:
        start = np.asarray(first, dtype=np.float64)
        end = np.asarray(second, dtype=np.float64)
        direction = end - start
        if float(np.dot(direction, direction)) <= 1e-18:
            return None

        segment_min = np.minimum(start, end)
        segment_max = np.maximum(start, end)
        best_t: float | None = None
        best_hit: MeshCollisionHit | None = None
        for chunk in self._candidate_chunks(segment_min, segment_max):
            triangles = self._triangles_for_chunk(chunk.cell)
            if triangles.size == 0:
                continue
            result = _segment_triangles_intersection(
                start,
                end,
                triangles,
                segment_min=segment_min,
                segment_max=segment_max,
            )
            if result is None:
                continue
            t, point = result
            if best_t is None or t < best_t:
                best_t = t
                best_hit = MeshCollisionHit(
                    point=(float(point[0]), float(point[1]), float(point[2])),
                    chunk_cell=chunk.cell,
                )
        return best_hit

    def _candidate_chunks(
        self,
        segment_min: np.ndarray,
        segment_max: np.ndarray,
    ) -> tuple[_ChunkBounds, ...]:
        return tuple(
            chunk
            for chunk in self._chunk_bounds
            if _aabb_intersects(
                segment_min,
                segment_max,
                chunk.bounds_min,
                chunk.bounds_max,
            )
        )

    def _triangles_for_chunk(self, cell: Cell) -> np.ndarray:
        with self._cache_lock:
            cached = self._triangle_cache.get(cell)
            if cached is not None:
                return cached
            triangles = self._load_triangles_for_chunk(cell)
            self._triangle_cache[cell] = triangles
            return triangles

    def _load_triangles_for_chunk(self, cell: Cell) -> np.ndarray:
        try:
            chunk = load_chunk_file(self._cache_dir, cell)
        except Exception:
            return np.empty((0, 3, 3), dtype=np.float64)
        groups = getattr(chunk, "groups", {})
        triangle_groups: list[np.ndarray] = []
        for group in groups.values():
            positions = np.asarray(getattr(group, "positions", ()), dtype=np.float64)
            if positions.ndim != 2 or positions.shape[1] != 3:
                continue
            triangle_vertex_count = (len(positions) // 3) * 3
            if triangle_vertex_count <= 0:
                continue
            triangle_groups.append(
                positions[:triangle_vertex_count].reshape(-1, 3, 3)
            )
        if not triangle_groups:
            return np.empty((0, 3, 3), dtype=np.float64)
        return np.concatenate(triangle_groups, axis=0)


def _manifest_exceeds_collision_guard_triangle_limit(
    manifest: Mapping[str, Any],
) -> bool:
    triangle_count = _manifest_triangle_count(manifest)
    if triangle_count is None:
        return False
    return triangle_count > _collision_guard_triangle_limit()


def _manifest_triangle_count(manifest: Mapping[str, Any]) -> int | None:
    raw_value = manifest.get("triangle_count")
    if raw_value is None:
        return None
    try:
        triangle_count = int(raw_value)
    except (TypeError, ValueError):
        return None
    return max(0, triangle_count)


def _collision_guard_triangle_limit() -> int:
    raw_value = os.environ.get(_MAX_COLLISION_GUARD_TRIANGLES_ENV)
    if raw_value is None or not raw_value.strip():
        return _DEFAULT_MAX_COLLISION_GUARD_TRIANGLES
    try:
        limit = int(raw_value)
    except ValueError:
        return _DEFAULT_MAX_COLLISION_GUARD_TRIANGLES
    return max(0, limit)


def _aabb_intersects(
    first_min: np.ndarray,
    first_max: np.ndarray,
    second_min: np.ndarray,
    second_max: np.ndarray,
) -> bool:
    return bool(
        np.all(first_max >= second_min)
        and np.all(second_max >= first_min)
    )


def _segment_triangles_intersection(
    start: np.ndarray,
    end: np.ndarray,
    triangles: np.ndarray,
    *,
    segment_min: np.ndarray,
    segment_max: np.ndarray,
) -> tuple[float, np.ndarray] | None:
    triangle_min = triangles.min(axis=1)
    triangle_max = triangles.max(axis=1)
    aabb_mask = np.all(triangle_max >= segment_min, axis=1) & np.all(
        segment_max >= triangle_min,
        axis=1,
    )
    if not bool(np.any(aabb_mask)):
        return None
    candidates = triangles[aabb_mask]
    direction = end - start
    edge1 = candidates[:, 1] - candidates[:, 0]
    edge2 = candidates[:, 2] - candidates[:, 0]
    direction_rows = np.broadcast_to(direction, edge2.shape)
    pvec = np.cross(direction_rows, edge2)
    det = np.einsum("ij,ij->i", edge1, pvec)
    det_mask = np.abs(det) > 1e-9
    if not bool(np.any(det_mask)):
        return None

    det_values = det[det_mask]
    filtered = candidates[det_mask]
    filtered_edge1 = edge1[det_mask]
    filtered_edge2 = edge2[det_mask]
    filtered_pvec = pvec[det_mask]
    inv_det = 1.0 / det_values
    tvec = start - filtered[:, 0]
    u = np.einsum("ij,ij->i", tvec, filtered_pvec) * inv_det
    u_mask = (u >= 0.0) & (u <= 1.0)
    if not bool(np.any(u_mask)):
        return None

    filtered = filtered[u_mask]
    filtered_edge1 = filtered_edge1[u_mask]
    filtered_edge2 = filtered_edge2[u_mask]
    tvec = tvec[u_mask]
    inv_det = inv_det[u_mask]
    u = u[u_mask]
    qvec = np.cross(tvec, filtered_edge1)
    v = np.einsum(
        "ij,ij->i",
        np.broadcast_to(direction, qvec.shape),
        qvec,
    ) * inv_det
    v_mask = (v >= 0.0) & (u + v <= 1.0)
    if not bool(np.any(v_mask)):
        return None

    filtered_edge2 = filtered_edge2[v_mask]
    qvec = qvec[v_mask]
    inv_det = inv_det[v_mask]
    t = np.einsum("ij,ij->i", filtered_edge2, qvec) * inv_det
    t_mask = (t >= 1e-7) & (t <= 1.0 + 1e-7)
    if not bool(np.any(t_mask)):
        return None
    hit_t = float(t[t_mask].min())
    point = start + direction * hit_t
    return hit_t, point
