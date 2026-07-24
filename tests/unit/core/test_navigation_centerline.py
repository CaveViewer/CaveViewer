"""Cover reusable manifest-derived centerline navigation primitives."""

from __future__ import annotations

from caveviewer.core.navigation.centerline import generate_centerline_path


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
    assert set(centerline_path.centers) == set(centerline_path.cells)
