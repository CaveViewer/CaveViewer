"""Tests for exact cached-mesh collision and local enclosure evidence."""

from __future__ import annotations

import numpy as np
import pytest

from caveviewer.core.navigation.mesh_collision import (
    MeshCollisionHit,
    _segment_triangles_intersection,
    opposing_mesh_axis_support,
)


def _axis_plane(
    axis: int,
    value: float,
    *,
    extent: float = 8.0,
) -> np.ndarray:
    other_axes = [index for index in range(3) if index != axis]
    points = []
    for first, second in (
        ((-extent, -extent), (extent, -extent)),
        ((extent, extent), (-extent, extent)),
    ):
        point = np.zeros(3, dtype=np.float64)
        point[axis] = value
        point[other_axes[0]] = first[0]
        point[other_axes[1]] = first[1]
        points.append(point)
        point = np.zeros(3, dtype=np.float64)
        point[axis] = value
        point[other_axes[0]] = second[0]
        point[other_axes[1]] = second[1]
        points.append(point)
    return np.asarray(
        (
            (points[0], points[1], points[2]),
            (points[0], points[2], points[3]),
        ),
        dtype=np.float64,
    )


def _collision_probe(triangles: np.ndarray):
    triangles = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
    triangle_min = triangles.min(axis=1)
    triangle_max = triangles.max(axis=1)

    def probe(first, second):
        start = np.asarray(first, dtype=np.float64)
        end = np.asarray(second, dtype=np.float64)
        result = _segment_triangles_intersection(
            start,
            end,
            triangles,
            segment_min=np.minimum(start, end),
            segment_max=np.maximum(start, end),
            triangle_min=triangle_min,
            triangle_max=triangle_max,
        )
        if result is None:
            return None
        _t, point = result
        return MeshCollisionHit(
            point=tuple(float(value) for value in point),
            chunk_cell=(0, 0, 0),
        )

    return probe


def test_opposing_support_keeps_y_open_x_bracketed_tunnel():
    triangles = np.concatenate(
        (
            _axis_plane(0, -2.0),
            _axis_plane(0, 3.0),
        ),
        axis=0,
    )

    support = opposing_mesh_axis_support(
        (0.0, 0.0, 0.0),
        segment_collision=_collision_probe(triangles),
        max_distance_m=10.0,
    )

    assert tuple(item.axis for item in support) == ("x",)
    assert support[0].negative_distance_m == pytest.approx(2.0)
    assert support[0].positive_distance_m == pytest.approx(3.0)


def test_opposing_support_rejects_unbounded_exterior_point():
    triangles = np.concatenate(
        (
            _axis_plane(0, -2.0),
            _axis_plane(1, -3.0),
            _axis_plane(2, -4.0),
        ),
        axis=0,
    )

    support = opposing_mesh_axis_support(
        (0.0, 0.0, 0.0),
        segment_collision=_collision_probe(triangles),
        max_distance_m=10.0,
    )

    assert support == ()


def test_one_spanning_triangle_cannot_invent_an_internal_gap():
    spanning_triangle = np.asarray(
        (
            (
                (-8.0, 1.0, -8.0),
                (8.0, 1.0, -8.0),
                (0.0, 1.0, 8.0),
            ),
        ),
        dtype=np.float64,
    )

    support = opposing_mesh_axis_support(
        (0.0, 0.0, 0.0),
        segment_collision=_collision_probe(spanning_triangle),
        max_distance_m=10.0,
    )

    assert support == ()


def test_opposing_support_preserves_half_meter_vertical_passage():
    triangles = np.concatenate(
        (
            _axis_plane(1, -0.25),
            _axis_plane(1, 0.25),
        ),
        axis=0,
    )

    support = opposing_mesh_axis_support(
        (0.0, 0.0, 0.0),
        segment_collision=_collision_probe(triangles),
        max_distance_m=2.0,
        minimum_clearance_m=0.20,
    )

    assert tuple(item.axis for item in support) == ("y",)
    assert support[0].negative_distance_m == pytest.approx(0.25)
    assert support[0].positive_distance_m == pytest.approx(0.25)


def test_opposing_support_enforces_requested_clearance():
    triangles = np.concatenate(
        (
            _axis_plane(0, -0.01),
            _axis_plane(0, 1.0),
        ),
        axis=0,
    )

    support = opposing_mesh_axis_support(
        (0.0, 0.0, 0.0),
        segment_collision=_collision_probe(triangles),
        max_distance_m=2.0,
        minimum_clearance_m=0.05,
    )

    assert support == ()
