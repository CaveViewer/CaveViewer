"""Tests for user-facing centerline Auto Dive planning."""

from __future__ import annotations

import pytest

from caveviewer.core.navigation.autodive import (
    AutoDiveSettings,
    _centerline_cells_form_closed_loop,
    _open_arc_from_closed_loop,
    build_centerline_auto_dive_plan,
)


def test_auto_dive_uses_longest_centerline_component():
    plan = build_centerline_auto_dive_plan(
        _split_manifest(),
        current_position=(0.0, 5.0, 0.0),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            keyframe_spacing_m=4.0,
        ),
    )

    assert plan.centerline_path.component_size == 48
    assert plan.route_length_m > 20.0
    assert min(point[0] for point in plan.route_points) > 35.0
    assert plan.route.duration_s == pytest.approx(plan.route_length_m)
    assert plan.render_distance_cells == 10


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
