"""Tests for optional navigation metadata stored in cache manifests."""

from __future__ import annotations

import numpy as np
import pytest

from caveviewer.core.navigation.cache_metadata import (
    NAVIGATION_METADATA_METHOD,
    NAVIGATION_ROUTE_Y_SMOOTHING_RADIUS_CELLS,
    NAVIGATION_METADATA_VERSION,
    build_navigation_metadata,
    cached_centerline_path,
)


def test_navigation_metadata_stores_component_centerline_routes():
    manifest = _split_manifest()

    metadata = build_navigation_metadata(manifest)

    assert metadata is not None
    assert metadata["version"] == NAVIGATION_METADATA_VERSION
    assert metadata["method"] == NAVIGATION_METADATA_METHOD
    assert metadata["route_count"] == 2
    assert metadata["recommended_route_id"] == "centerline-0"
    route_lengths = [route["length_m"] for route in metadata["routes"]]
    assert route_lengths == sorted(route_lengths, reverse=True)
    assert metadata["routes"][0]["component_size"] == 48
    assert metadata["routes"][0]["closed_loop"] is False
    assert metadata["routes"][0]["cells"]
    assert metadata["routes"][0]["component_cells"]

    cached_path = cached_centerline_path({**manifest, "navigation": metadata})

    assert cached_path is not None
    assert cached_path.source == "cached_navigation_metadata"
    assert cached_path.component_size == 48
    assert cached_path.length_m == pytest.approx(metadata["routes"][0]["length_m"])
    assert cached_path.cells == _flat_pairs(metadata["routes"][0]["cells"])


def test_navigation_metadata_uses_surface_cells_and_stores_3d_gap_points():
    positions = np.array(
        [
            [float(x) + 0.5, float(y), float(z) + 0.5]
            for x in range(7)
            for z in (0, 4)
            for y in (0, 4)
        ],
        dtype=np.float32,
    )
    manifest = {
        "chunk_size": 1.0,
        "footprint_cell_size": 1.0,
        "footprint_cells": [
            value
            for x in range(7)
            for z in (0, 4)
            for value in (x, z)
        ],
    }

    metadata = build_navigation_metadata(manifest, surface_positions=positions)

    assert metadata is not None
    assert metadata["surface_driven"] is True
    assert metadata["navigation_footprint_source"] == "surface_span_fill_v1"
    assert metadata["surface_footprint_cell_count"] == 14
    cells = _flat_pairs(metadata["routes"][0]["cells"])
    assert all(0 < cell[1] < 4 for cell in cells)
    assert any(cell[1] == 2 for cell in cells)
    points = _flat_points(metadata["routes"][0]["points"])
    y_ranges = _flat_y_ranges(metadata["routes"][0]["y_ranges"])
    assert len(points) == len(cells)
    assert len(y_ranges) == len(cells)
    assert all(point[1] == pytest.approx(2.0) for point in points)
    assert all(point[2] == pytest.approx(2.5) for point in points)
    assert all(0.5 < point[2] < 4.5 for point in points)
    assert len(metadata["routes"][0]["clearance_margins"]) == len(cells)
    assert min(metadata["routes"][0]["clearance_margins"]) > 0.0

    cached_path = cached_centerline_path({**manifest, "navigation": metadata})

    assert cached_path is not None
    assert cached_path.cached_points is not None
    assert cached_path.cached_y_ranges is not None
    assert cached_path.cached_clearance_margins is not None
    assert any(cell[1] == 2 for cell in cached_path.cells)


def test_navigation_metadata_centers_surface_route_points_across_x_passage_axis():
    positions = np.array(
        [
            [float(x) + 0.5, float(y), float(z) + 0.5]
            for z in range(7)
            for x in (0, 4)
            for y in (0, 4)
        ],
        dtype=np.float32,
    )
    manifest = {
        "chunk_size": 1.0,
        "footprint_cell_size": 1.0,
        "footprint_cells": [
            value
            for z in range(7)
            for x in (0, 4)
            for value in (x, z)
        ],
    }

    metadata = build_navigation_metadata(manifest, surface_positions=positions)

    assert metadata is not None
    points = _flat_points(metadata["routes"][0]["points"])
    assert points
    assert all(point[0] == pytest.approx(2.5) for point in points)
    assert all(0.5 < point[0] < 4.5 for point in points)


def test_navigation_metadata_samples_y_from_centered_xz_column():
    positions = np.array(
        [
            [float(x) + 0.5, float(y), float(z) + 0.5]
            for x in range(7)
            for z, y_values in (
                (0, (0, 4)),
                (2, (10, 20)),
                (4, (0, 4)),
            )
            for y in y_values
        ],
        dtype=np.float32,
    )
    manifest = {
        "chunk_size": 1.0,
        "footprint_cell_size": 1.0,
        "footprint_cells": [
            value
            for x in range(7)
            for z in (0, 2, 4)
            for value in (x, z)
        ],
    }

    metadata = build_navigation_metadata(manifest, surface_positions=positions)

    assert metadata is not None
    points = _flat_points(metadata["routes"][0]["points"])
    assert points
    assert all(point[2] == pytest.approx(2.5) for point in points)
    assert all(point[1] == pytest.approx(15.0) for point in points)


def test_navigation_metadata_stores_raw_y_samples_for_runtime_smoothing():
    low_points = [
        [float(x) + 0.5, float(y), 0.5]
        for x in range(12)
        for y in (0, 4)
    ]
    high_points = [
        [float(x) + 0.5, float(y), 0.5]
        for x in range(12, 24)
        for y in (8, 12)
    ]
    positions = np.array([*low_points, *high_points], dtype=np.float32)
    manifest = {
        "chunk_size": 1.0,
        "footprint_cell_size": 1.0,
        "footprint_cells": [
            value
            for x in range(24)
            for value in (x, 0)
        ],
    }

    metadata = build_navigation_metadata(manifest, surface_positions=positions)

    assert metadata is not None
    route = metadata["routes"][0]
    assert route["point_source"] == "surface_vertical_gap_raw"
    assert route["runtime_y_smoothing"] is True
    assert (
        route["recommended_smoothing_radius_cells"]
        == NAVIGATION_ROUTE_Y_SMOOTHING_RADIUS_CELLS
    )
    assert "recommended_y_smoothing_radius_cells" not in route
    assert "y_smoothing_radius_cells" not in route
    points = _flat_points(route["points"])
    y_ranges = _flat_y_ranges(route["y_ranges"])
    y_values = [point[1] for point in points]
    assert len(y_ranges) == len(points)
    assert y_values[0] < 3.0
    assert y_values[-1] > 9.0
    assert y_values[8] == pytest.approx(y_values[0])
    assert y_values[12] == pytest.approx(y_values[-1])
    assert y_ranges[0][0] < y_values[0] < y_ranges[0][1]
    assert y_ranges[-1][0] < y_values[-1] < y_ranges[-1][1]

    cached_path = cached_centerline_path({**manifest, "navigation": metadata})

    assert cached_path is not None
    assert cached_path.cached_y_ranges is not None


def test_navigation_metadata_orients_recommended_route_from_navigation_start():
    manifest = _line_manifest(length=10)

    metadata = build_navigation_metadata(
        manifest,
        navigation_start={
            "position": [9.5, 1.0, 0.5],
            "label": "entrance",
            "source": "navigation.json",
        },
    )

    assert metadata is not None
    assert metadata["navigation_start"] == {
        "position": [9.5, 1.0, 0.5],
        "label": "entrance",
        "source": "navigation.json",
    }
    route = metadata["routes"][0]
    cells = _flat_pairs(route["cells"])
    assert route["selection_method"] == "navigation_start_to_farthest_endpoint_v1"
    assert route["candidate_count"] >= 2
    assert route["starts_at_navigation_start"] is True
    assert route["navigation_start_distance_m"] < 1.0
    assert cells[0][0] >= 8
    assert cells[-1][0] <= 1

    cached_path = cached_centerline_path({**manifest, "navigation": metadata})

    assert cached_path is not None
    assert cached_path.cells[0][0] >= 8


def test_cached_centerline_path_ignores_missing_or_unsupported_metadata():
    assert cached_centerline_path({}) is None
    assert cached_centerline_path(
        {
            "navigation": {
                "version": 999,
                "method": NAVIGATION_METADATA_METHOD,
                "routes": [],
            }
        }
    ) is None


def _split_manifest():
    footprint_cells = []
    for x in range(5):
        for z in range(5):
            footprint_cells.extend((x, z))
    for x in range(20, 36):
        for z in range(3):
            footprint_cells.extend((x, z))
    return {
        "chunk_size": 2.0,
        "footprint_cell_size": 2.0,
        "footprint_cells": footprint_cells,
        "chunks": {
            f"{x}_0_{z}": {
                "bounds_min": [x * 2.0, 0.0, z * 2.0],
                "bounds_max": [(x + 1) * 2.0, 10.0, (z + 1) * 2.0],
            }
            for x in range(36)
            for z in range(5)
            if (x < 5 or (20 <= x < 36 and z < 3))
        },
    }


def _line_manifest(*, length: int):
    cells = [(x, 0) for x in range(length)]
    return {
        "chunk_size": 1.0,
        "footprint_cell_size": 1.0,
        "footprint_cells": [
            value
            for cell in cells
            for value in cell
        ],
        "chunks": {
            f"{x}_0_0": {
                "bounds_min": [float(x), 0.0, 0.0],
                "bounds_max": [float(x + 1), 2.0, 1.0],
            }
            for x in range(length)
        },
    }


def _flat_pairs(flat: list[int]) -> tuple[tuple[int, int], ...]:
    return tuple(
        (int(flat[index]), int(flat[index + 1]))
        for index in range(0, len(flat), 2)
    )


def _flat_points(flat: list[float]) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        (
            float(flat[index]),
            float(flat[index + 1]),
            float(flat[index + 2]),
        )
        for index in range(0, len(flat), 3)
    )


def _flat_y_ranges(flat: list[float]) -> tuple[tuple[float, float], ...]:
    return tuple(
        (
            float(flat[index]),
            float(flat[index + 1]),
        )
        for index in range(0, len(flat), 2)
    )
