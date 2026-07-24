"""Cover reusable manifest-derived centerline navigation primitives."""

from __future__ import annotations

from caveviewer.core.navigation.centerline import (
    clearance_scores_for_footprint,
    generate_centerline_path,
    lowest_cost_footprint_path,
)


def test_centerline_path_uses_vertex_footprint_without_load_complexity():
    centerline_path = generate_centerline_path(
        {
            "footprint_cell_size": 2.0,
            "footprint_cells": [
                value
                for x in range(7)
                for z in range(5)
                for value in (x, z)
            ],
        }
    )

    assert centerline_path.source == "vertex_footprint_manifest"
    assert centerline_path.footprint_cell_size == 2.0
    assert centerline_path.footprint_cell_count == 35
    assert centerline_path.component_size == 35
    assert centerline_path.cells
    assert centerline_path.points_xz
    assert centerline_path.length_m > 0.0
    assert centerline_path.clearance_scores
    assert set(centerline_path.centers) == centerline_path.component_cells
    assert set(centerline_path.cells) <= set(centerline_path.centers)


def test_lowest_cost_footprint_path_does_not_cut_wall_corner():
    cells = frozenset({(0, 0), (1, 0), (1, 1)})

    path = lowest_cost_footprint_path(
        cells,
        (0, 0),
        (1, 1),
        clearance_scores_for_footprint(cells),
    )

    assert path == ((0, 0), (1, 0), (1, 1))
