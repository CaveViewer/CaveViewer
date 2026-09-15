"""Pure viewport ray picking over resident CaveViewer chunk geometry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math

import numpy as np


_EPSILON = 1e-8
_TRIANGLE_BATCH_SIZE = 16_384


@dataclass(frozen=True)
class ViewportRay:
    """One normalized world-space ray cast from a viewport pixel."""

    origin: np.ndarray
    direction: np.ndarray


@dataclass(frozen=True)
class SurfaceHit:
    """Nearest resident cave surface reached by a viewport ray."""

    point: tuple[float, float, float]
    distance: float


def viewport_ray(
    *,
    screen_x: float,
    screen_y: float,
    viewport_size: tuple[int, int],
    camera_position,
    view: np.ndarray,
    projection: np.ndarray,
) -> ViewportRay | None:
    """Build a world-space ray for one top-left-origin viewport coordinate."""
    width, height = viewport_size
    if width <= 0 or height <= 0:
        return None

    try:
        inverse_view_projection = np.linalg.inv(
            np.asarray(projection, dtype=np.float64)
            @ np.asarray(view, dtype=np.float64)
        )
    except np.linalg.LinAlgError:
        return None

    ndc_x = (2.0 * float(screen_x) / float(width)) - 1.0
    ndc_y = 1.0 - (2.0 * float(screen_y) / float(height))
    near_clip = np.array((ndc_x, ndc_y, -1.0, 1.0), dtype=np.float64)
    far_clip = np.array((ndc_x, ndc_y, 1.0, 1.0), dtype=np.float64)
    near_world = inverse_view_projection @ near_clip
    far_world = inverse_view_projection @ far_clip
    if abs(float(near_world[3])) <= _EPSILON or abs(float(far_world[3])) <= _EPSILON:
        return None
    near_world = near_world[:3] / near_world[3]
    far_world = far_world[:3] / far_world[3]
    origin = np.asarray(camera_position, dtype=np.float64).reshape(3)
    direction = far_world - near_world
    length = float(np.linalg.norm(direction))
    if not math.isfinite(length) or length <= _EPSILON:
        return None
    return ViewportRay(origin=origin, direction=direction / length)


def nearest_surface_hit(
    ray: ViewportRay | None,
    *,
    chunk_aabbs: Mapping[object, tuple[np.ndarray, np.ndarray]],
    chunk_triangle_groups: Mapping[object, Sequence[Sequence[object]]],
) -> SurfaceHit | None:
    """Return the closest triangle hit among the currently resident chunks."""
    if ray is None:
        return None

    candidates: list[tuple[float, object]] = []
    for cell, bounds in chunk_aabbs.items():
        entry_distance = _ray_aabb_entry_distance(ray, *bounds)
        if entry_distance is not None:
            candidates.append((entry_distance, cell))
    candidates.sort(key=lambda candidate: candidate[0])

    nearest_distance = math.inf
    for entry_distance, cell in candidates:
        if entry_distance > nearest_distance:
            break
        for group in chunk_triangle_groups.get(cell, ()):
            if len(group) < 2:
                continue
            distance = _nearest_triangle_distance(ray, group[1], nearest_distance)
            if distance is not None:
                nearest_distance = distance

    if not math.isfinite(nearest_distance):
        return None
    point = ray.origin + ray.direction * nearest_distance
    return SurfaceHit(
        point=(float(point[0]), float(point[1]), float(point[2])),
        distance=float(nearest_distance),
    )


def _ray_aabb_entry_distance(
    ray: ViewportRay,
    bounds_min,
    bounds_max,
) -> float | None:
    """Return the non-negative entry distance for a ray/AABB intersection."""
    lower = np.minimum(
        np.asarray(bounds_min, dtype=np.float64).reshape(3),
        np.asarray(bounds_max, dtype=np.float64).reshape(3),
    )
    upper = np.maximum(
        np.asarray(bounds_min, dtype=np.float64).reshape(3),
        np.asarray(bounds_max, dtype=np.float64).reshape(3),
    )
    entry = -math.inf
    exit_distance = math.inf
    for axis in range(3):
        origin = float(ray.origin[axis])
        direction = float(ray.direction[axis])
        if abs(direction) <= _EPSILON:
            if origin < lower[axis] or origin > upper[axis]:
                return None
            continue
        near = (lower[axis] - origin) / direction
        far = (upper[axis] - origin) / direction
        entry = max(entry, min(near, far))
        exit_distance = min(exit_distance, max(near, far))
        if entry > exit_distance:
            return None
    if exit_distance <= _EPSILON:
        return None
    return max(entry, 0.0)


def _nearest_triangle_distance(
    ray: ViewportRay,
    positions,
    upper_distance: float,
) -> float | None:
    """Return the closest ray/triangle hit while bounding temporary memory."""
    vertices = np.asarray(positions)
    if vertices.ndim != 2 or vertices.shape[1] != 3:
        return None
    triangle_count = len(vertices) // 3
    if triangle_count == 0:
        return None

    nearest = upper_distance
    for first_triangle in range(0, triangle_count, _TRIANGLE_BATCH_SIZE):
        triangles = vertices[
            first_triangle * 3:min(
                triangle_count, first_triangle + _TRIANGLE_BATCH_SIZE
            ) * 3
        ].reshape(-1, 3, 3)
        distances = _triangle_distances(ray, triangles)
        if distances.size == 0:
            continue
        candidate = float(np.min(distances))
        if candidate < nearest:
            nearest = candidate
    return None if nearest == upper_distance else nearest


def _triangle_distances(ray: ViewportRay, triangles: np.ndarray) -> np.ndarray:
    """Return positive Moller-Trumbore hit distances for one triangle batch."""
    vertex0 = triangles[:, 0]
    edge1 = triangles[:, 1] - vertex0
    edge2 = triangles[:, 2] - vertex0
    pvec = np.cross(ray.direction, edge2)
    determinant = np.einsum("ij,ij->i", edge1, pvec)
    valid = np.abs(determinant) > _EPSILON
    if not np.any(valid):
        return np.empty(0, dtype=np.float64)

    inverse_determinant = np.zeros_like(determinant)
    inverse_determinant[valid] = 1.0 / determinant[valid]
    tvec = ray.origin - vertex0
    u = np.einsum("ij,ij->i", tvec, pvec) * inverse_determinant
    qvec = np.cross(tvec, edge1)
    v = np.einsum("j,ij->i", ray.direction, qvec) * inverse_determinant
    distance = np.einsum("ij,ij->i", edge2, qvec) * inverse_determinant
    valid &= u >= 0.0
    valid &= u <= 1.0
    valid &= v >= 0.0
    valid &= (u + v) <= 1.0
    valid &= distance > _EPSILON
    return distance[valid]
