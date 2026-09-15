"""Tests for resident-geometry viewport picking."""

import numpy as np
import pytest

from caveviewer.gui import viewer_picking


def _ray():
    return viewer_picking.ViewportRay(
        origin=np.array((0.0, 0.0, 0.0)),
        direction=np.array((0.0, 0.0, 1.0)),
    )


def _triangle(z: float) -> np.ndarray:
    return np.array(
        ((-1.0, -1.0, z), (1.0, -1.0, z), (0.0, 1.0, z)),
        dtype=np.float32,
    )


def test_viewport_ray_unprojects_the_center_pixel_forward():
    ray = viewer_picking.viewport_ray(
        screen_x=50.0,
        screen_y=50.0,
        viewport_size=(100, 100),
        camera_position=(0.0, 0.0, 0.0),
        view=np.identity(4),
        projection=np.identity(4),
    )

    assert ray is not None
    assert ray.origin == pytest.approx((0.0, 0.0, 0.0))
    assert ray.direction == pytest.approx((0.0, 0.0, 1.0))


def test_viewport_ray_rejects_empty_viewports():
    assert viewer_picking.viewport_ray(
        screen_x=0.0,
        screen_y=0.0,
        viewport_size=(0, 100),
        camera_position=(0.0, 0.0, 0.0),
        view=np.identity(4),
        projection=np.identity(4),
    ) is None


def test_nearest_surface_hit_selects_the_closest_triangle_after_bounds_filtering():
    hit = viewer_picking.nearest_surface_hit(
        _ray(),
        chunk_aabbs={
            "far": (np.array((-1, -1, 7)), np.array((1, 1, 7))),
            "near": (np.array((-1, -1, 3)), np.array((1, 1, 3))),
            "off_ray": (np.array((5, -1, 1)), np.array((7, 1, 2))),
        },
        chunk_triangle_groups={
            "far": (("rock", _triangle(7.0)),),
            "near": (("rock", _triangle(3.0)),),
            "off_ray": (("rock", _triangle(1.0)),),
        },
    )

    assert hit is not None
    assert hit.distance == pytest.approx(3.0)
    assert hit.point == pytest.approx((0.0, 0.0, 3.0))


def test_nearest_surface_hit_ignores_degenerate_triangles_and_reports_a_miss():
    hit = viewer_picking.nearest_surface_hit(
        _ray(),
        chunk_aabbs={"cave": (np.array((-1, -1, 2)), np.array((1, 1, 4)))},
        chunk_triangle_groups={
            "cave": ((
                "rock",
                np.array(
                    (
                        (0.0, 0.0, 2.0),
                        (0.0, 0.0, 2.0),
                        (0.0, 0.0, 2.0),
                    ),
                    dtype=np.float32,
                ),
            ),),
        },
    )

    assert hit is None
