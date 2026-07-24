"""Tests for user-facing centerline Auto Dive planning."""

from __future__ import annotations

import pytest

from caveviewer.core.navigation.autodive import (
    DEFAULT_AUTO_DIVE_SPEED_M_PER_SECOND,
    DEFAULT_AUTO_DIVE_VERTICAL_POSITION_FRACTION,
    AutoDiveSettings,
    _centerline_cells_form_closed_loop,
    _open_arc_from_closed_loop,
    build_centerline_auto_dive_plan,
)
from caveviewer.core.navigation.centerline import (
    DEFAULT_CENTERLINE_ROUTE_SPEED_M_PER_SECOND,
)


def test_auto_dive_uses_longest_centerline_component():
    current_position = (0.0, 5.0, 0.0)
    plan = build_centerline_auto_dive_plan(
        _split_manifest(),
        current_position=current_position,
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            keyframe_spacing_m=4.0,
        ),
    )

    assert plan.centerline_path.component_size == 48
    assert plan.route_length_m > 20.0
    assert plan.route_points[0] == current_position
    assert plan.route.keyframes[0].position == current_position
    assert min(point[0] for point in plan.route_points[1:]) > 35.0
    assert plan.route.duration_s == pytest.approx(plan.route_length_m)
    assert plan.render_distance_cells == 10


def test_auto_dive_route_keeps_bend_waypoint_instead_of_cutting_wall():
    plan = build_centerline_auto_dive_plan(
        _l_bend_manifest(),
        current_position=(0.5, 1.0, 0.5),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            keyframe_spacing_m=1000.0,
        ),
    )

    assert plan.route_points_xz[0] == (0.5, 0.5)
    assert (8.5, 0.5) in plan.route_points_xz
    assert (8.5, 8.5) == plan.route_points_xz[-1]


def test_auto_dive_default_speed_is_two_hundred_twenty_five_percent_of_centerline():
    plan = build_centerline_auto_dive_plan(
        _l_bend_manifest(),
        current_position=(0.5, 1.0, 0.5),
        settings=AutoDiveSettings(keyframe_spacing_m=1000.0),
    )

    assert DEFAULT_AUTO_DIVE_SPEED_M_PER_SECOND == pytest.approx(
        DEFAULT_CENTERLINE_ROUTE_SPEED_M_PER_SECOND * 2.25
    )
    assert plan.duration_s == pytest.approx(
        plan.route_length_m / DEFAULT_AUTO_DIVE_SPEED_M_PER_SECOND
    )


def test_auto_dive_targets_lower_local_vertical_passage_fraction():
    plan = build_centerline_auto_dive_plan(
        _l_bend_manifest(),
        current_position=(0.5, 1.0, 0.5),
        settings=AutoDiveSettings(keyframe_spacing_m=1000.0),
    )

    assert DEFAULT_AUTO_DIVE_VERTICAL_POSITION_FRACTION == pytest.approx(0.35)
    assert all(
        point[1]
        == pytest.approx(2.0 * DEFAULT_AUTO_DIVE_VERTICAL_POSITION_FRACTION)
        for point in plan.route_points[1:]
    )


def test_auto_dive_closed_loop_helper_keeps_open_arc():
    loop_cells = ((0, 0), (1, 0), (1, 1), (0, 1))

    assert _centerline_cells_form_closed_loop(loop_cells) is True

    arc = _open_arc_from_closed_loop(
        loop_cells,
        start_index=1,
        gap_fraction=0.25,
    )

    assert arc[0] == (1, 0)
    assert len(arc) == 3
    assert set(arc) < set(loop_cells)


def _split_manifest():
    footprint_cells = []
    chunk_cells = []
    for x in range(5):
        for z in range(5):
            footprint_cells.extend((x, z))
            chunk_cells.append((x, 0, z))
    for x in range(20, 36):
        for z in range(3):
            footprint_cells.extend((x, z))
            chunk_cells.append((x, 0, z))

    chunks = {}
    for x, y, z in chunk_cells:
        chunks[f"{x}_{y}_{z}"] = {
            "bounds_min": [x * 2.0, 0.0, z * 2.0],
            "bounds_max": [(x + 1) * 2.0, 10.0, (z + 1) * 2.0],
        }
    return {
        "chunk_size": 2.0,
        "footprint_cell_size": 2.0,
        "footprint_cells": footprint_cells,
        "chunks": chunks,
    }


def _l_bend_manifest():
    footprint = [
        *[(x, 0) for x in range(9)],
        *[(8, z) for z in range(1, 9)],
    ]
    chunks = {
        f"{x}_0_{z}": {
            "bounds_min": [float(x), 0.0, float(z)],
            "bounds_max": [float(x + 1), 2.0, float(z + 1)],
        }
        for x, z in footprint
    }
    return {
        "chunk_size": 1.0,
        "footprint_cell_size": 1.0,
        "footprint_cells": [
            value
            for cell in footprint
            for value in cell
        ],
        "chunks": chunks,
    }
