"""Frustum visibility helpers for resident viewer chunks.

The viewer window owns the current camera matrices and resident GPU objects.
This module owns the frustum-plane math and the small cache that avoids
retesting every loaded chunk when neither the view nor resident chunk
generation changed between render frames.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def frustum_planes(view: np.ndarray, projection: np.ndarray) -> np.ndarray:
    """
    Extract the 6 view-frustum planes in world space from row-major matrices.

    The combined clip matrix M = projection @ view maps world-space column
    vectors to clip space; summing/differencing rows of M gives the six plane
    equations. Returns a (6, 4) float64 array where each row (a, b, c, d)
    satisfies a*x + b*y + c*z + d >= 0 for inside points.
    """
    vp = (projection @ view).astype(np.float64)
    planes = np.empty((6, 4), dtype=np.float64)
    planes[0] = vp[3] + vp[0]   # left
    planes[1] = vp[3] - vp[0]   # right
    planes[2] = vp[3] + vp[1]   # bottom
    planes[3] = vp[3] - vp[1]   # top
    planes[4] = vp[3] + vp[2]   # near
    planes[5] = vp[3] - vp[2]   # far
    lengths = np.linalg.norm(planes[:, :3], axis=1, keepdims=True)
    planes /= np.maximum(lengths, 1e-9)
    return planes


def aabb_inside_frustum(
    planes: np.ndarray,
    bounds_min: np.ndarray,
    bounds_max: np.ndarray,
) -> bool:
    """
    Return whether an AABB is conservatively inside the frustum.

    For each plane, pick the AABB corner furthest along the plane normal. If
    that positive vertex is outside the plane, the entire AABB is outside the
    frustum. This can keep a few false positives, but it does not false-cull
    visible chunks.
    """
    for a, b, c, d in planes:
        px = bounds_max[0] if a >= 0 else bounds_min[0]
        py = bounds_max[1] if b >= 0 else bounds_min[1]
        pz = bounds_max[2] if c >= 0 else bounds_min[2]
        if a * px + b * py + c * pz + d < 0:
            return False
    return True


class FrustumCullingCache:
    """Cache visible resident chunks for unchanged view/projection/generation."""

    def __init__(self) -> None:
        self._view: np.ndarray | None = None
        self._projection: np.ndarray | None = None
        self._generation: int | None = None
        self._visible_chunks: list[tuple[Any, Any]] = []
        self.reused_last_result = False

    def invalidate(self) -> None:
        """Force the next visibility query to retest all resident chunks."""
        self._view = None
        self._projection = None
        self._generation = None
        self._visible_chunks = []
        self.reused_last_result = False

    def visible_chunks(
        self,
        *,
        view: np.ndarray,
        projection: np.ndarray,
        chunk_gpu_objects: dict[Any, Any],
        chunk_aabbs: dict[Any, tuple[np.ndarray, np.ndarray]],
        generation: int,
    ) -> list[tuple[Any, Any]]:
        """Return resident chunks visible in the current frustum."""
        view_array = np.asarray(view, dtype=np.float64)
        projection_array = np.asarray(projection, dtype=np.float64)
        generation = int(generation)
        if (
            self._generation == generation
            and self._view is not None
            and self._projection is not None
            and np.array_equal(self._view, view_array)
            and np.array_equal(self._projection, projection_array)
        ):
            self.reused_last_result = True
            return self._visible_chunks

        planes = frustum_planes(view_array, projection_array)
        visible_chunks = []
        for cell, vao_list in chunk_gpu_objects.items():
            aabb = chunk_aabbs.get(cell)
            if aabb is None or aabb_inside_frustum(planes, aabb[0], aabb[1]):
                visible_chunks.append((cell, vao_list))

        self._view = view_array.copy()
        self._projection = projection_array.copy()
        self._generation = generation
        self._visible_chunks = visible_chunks
        self.reused_last_result = False
        return visible_chunks
