"""Cached chunk-mesh collision queries for navigation planning."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
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

# Exact route validation performs lazy, local segment checks over chunked
# geometry. Whole-map or average-chunk triangle counts are therefore resource
# hints, not reasons to remove collision authority. Keep the guard available
# for every structurally valid cache and use these thresholds only to disable
# optional speculative recovery whose short runtime deadline could otherwise
# be monopolized by dense geometry.
_DEFAULT_MAX_MESH_RECOVERY_TRIANGLES = 5_000_000
_MAX_MESH_RECOVERY_TRIANGLES_ENV = (
    "CAVEVIEWER_AUTO_DIVE_MESH_RECOVERY_MAX_TRIANGLES"
)
_DEFAULT_MAX_MESH_RECOVERY_AVG_CHUNK_TRIANGLES = 50_000
_MAX_MESH_RECOVERY_AVG_CHUNK_TRIANGLES_ENV = (
    "CAVEVIEWER_AUTO_DIVE_MESH_RECOVERY_MAX_AVG_CHUNK_TRIANGLES"
)
# Cache-time voxel construction visits many render chunks. Retaining every
# decoded chunk duplicates a large part of the source mesh while the importer
# and navigation atlas are still resident. Bound residency by triangle count;
# a single oversized chunk is retained separately from the normal LRU. Exact
# checks already have to decode that chunk once; one bounded oversized slot
# prevents a dense local passage from re-reading the same large file for every
# neighboring graph edge without turning the regular LRU into a whole-map
# geometry cache.
_DEFAULT_MAX_CACHED_COLLISION_TRIANGLES = 250_000


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


@dataclass(frozen=True)
class _ChunkTriangleMesh:
    triangles: np.ndarray
    triangle_min: np.ndarray
    triangle_max: np.ndarray


class CachedChunkMeshCollisionGuard:
    """Segment collision guard backed by cached chunk triangle payloads."""

    def __init__(
        self,
        cache_dir: str,
        chunk_bounds: tuple[_ChunkBounds, ...],
        *,
        mesh_recovery_enabled: bool = True,
        max_cached_triangles: int | None = None,
    ) -> None:
        self._cache_dir = os.path.abspath(os.fspath(cache_dir))
        self._chunk_bounds = chunk_bounds
        self.mesh_recovery_enabled = bool(mesh_recovery_enabled)
        self._max_cached_triangles = (
            _DEFAULT_MAX_CACHED_COLLISION_TRIANGLES
            if max_cached_triangles is None
            else max(0, int(max_cached_triangles))
        )
        self._triangle_cache: OrderedDict[Cell, _ChunkTriangleMesh] = (
            OrderedDict()
        )
        self._oversized_triangle_cache: tuple[
            Cell,
            _ChunkTriangleMesh,
        ] | None = None
        self._cached_triangle_count = 0
        self._cache_lock = threading.Lock()

    @classmethod
    def from_manifest(
        cls,
        manifest: Mapping[str, Any],
        *,
        cache_dir: str | os.PathLike[str] | None,
        max_cached_triangles: int | None = None,
    ) -> "CachedChunkMeshCollisionGuard | None":
        if not cache_dir:
            return None
        chunks = manifest.get("chunks")
        if not isinstance(chunks, Mapping) or not chunks:
            return None
        chunk_count = len(chunks)
        triangle_count = _manifest_triangle_count(manifest)
        average_chunk_triangles = _average_chunk_triangle_count(
            triangle_count,
            chunk_count,
        )
        mesh_recovery_enabled = not (
            _exceeds_triangle_limit(
                triangle_count,
                _mesh_recovery_triangle_limit(),
            )
            or _exceeds_triangle_limit(
                average_chunk_triangles,
                _mesh_recovery_average_chunk_triangle_limit(),
            )
        )
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
            mesh_recovery_enabled=mesh_recovery_enabled,
            max_cached_triangles=max_cached_triangles,
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
            mesh = self._triangle_mesh_for_chunk(chunk.cell)
            if mesh.triangles.size == 0:
                continue
            result = _segment_triangles_intersection(
                start,
                end,
                mesh.triangles,
                segment_min=segment_min,
                segment_max=segment_max,
                triangle_min=mesh.triangle_min,
                triangle_max=mesh.triangle_max,
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

    def triangle_meshes_for_bounds(
        self,
        bounds_min: Point,
        bounds_max: Point,
    ) -> Iterable[np.ndarray]:
        """Return cached chunk triangle arrays intersecting local bounds.

        Navigation refinements use this bounded provider to build optional
        local analyses without loading the whole map into one mesh array.
        The result is lazy so cache-time rasterization can consume one chunk
        at a time instead of retaining every candidate array in a tuple.
        """
        first = np.asarray(bounds_min, dtype=np.float64).reshape(3)
        second = np.asarray(bounds_max, dtype=np.float64).reshape(3)
        lower = np.minimum(first, second)
        upper = np.maximum(first, second)
        for chunk in self._candidate_chunks(lower, upper):
            mesh = self._triangle_mesh_for_chunk(chunk.cell)
            if mesh.triangles.size:
                yield mesh.triangles

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

    def _triangle_mesh_for_chunk(self, cell: Cell) -> _ChunkTriangleMesh:
        with self._cache_lock:
            cached = self._triangle_cache.get(cell)
            if cached is not None:
                self._triangle_cache.move_to_end(cell)
                return cached
            oversized = self._oversized_triangle_cache
            if oversized is not None and oversized[0] == cell:
                return oversized[1]
            triangles = self._load_triangles_for_chunk(cell)
            if triangles.size == 0:
                mesh = _ChunkTriangleMesh(
                    triangles=triangles,
                    triangle_min=np.empty((0, 3), dtype=np.float64),
                    triangle_max=np.empty((0, 3), dtype=np.float64),
                )
            else:
                mesh = _ChunkTriangleMesh(
                    triangles=triangles,
                    triangle_min=triangles.min(axis=1),
                    triangle_max=triangles.max(axis=1),
                )
            triangle_count = int(len(mesh.triangles))
            if self._max_cached_triangles > 0:
                if triangle_count > self._max_cached_triangles:
                    self._oversized_triangle_cache = (cell, mesh)
                else:
                    while self._triangle_cache and (
                        self._cached_triangle_count + triangle_count
                        > self._max_cached_triangles
                    ):
                        _evicted_cell, evicted = self._triangle_cache.popitem(
                            last=False
                        )
                        self._cached_triangle_count -= len(evicted.triangles)
                    self._triangle_cache[cell] = mesh
                    self._cached_triangle_count += triangle_count
            return mesh

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


def _exceeds_triangle_limit(
    triangle_count: int | None,
    limit: int,
) -> bool:
    if triangle_count is None:
        return False
    return triangle_count > limit


def _manifest_triangle_count(manifest: Mapping[str, Any]) -> int | None:
    raw_value = manifest.get("triangle_count")
    if raw_value is None:
        return None
    try:
        triangle_count = int(raw_value)
    except (TypeError, ValueError):
        return None
    return max(0, triangle_count)


def _average_chunk_triangle_count(
    triangle_count: int | None,
    chunk_count: int,
) -> int | None:
    if triangle_count is None or chunk_count <= 0:
        return None
    return int(math.ceil(float(triangle_count) / float(chunk_count)))


def _mesh_recovery_triangle_limit() -> int:
    return _triangle_limit_from_env(
        _MAX_MESH_RECOVERY_TRIANGLES_ENV,
        _DEFAULT_MAX_MESH_RECOVERY_TRIANGLES,
    )


def _mesh_recovery_average_chunk_triangle_limit() -> int:
    return _triangle_limit_from_env(
        _MAX_MESH_RECOVERY_AVG_CHUNK_TRIANGLES_ENV,
        _DEFAULT_MAX_MESH_RECOVERY_AVG_CHUNK_TRIANGLES,
    )


def _triangle_limit_from_env(env_name: str, default: int) -> int:
    raw_value = os.environ.get(env_name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        limit = int(raw_value)
    except ValueError:
        return default
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
    triangle_min: np.ndarray | None = None,
    triangle_max: np.ndarray | None = None,
) -> tuple[float, np.ndarray] | None:
    if triangle_min is None:
        triangle_min = triangles.min(axis=1)
    if triangle_max is None:
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
