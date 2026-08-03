"""Tests for optional navigation metadata stored in cache manifests."""

from __future__ import annotations

import numpy as np
import pytest

from caveviewer.core.navigation.cache_metadata import (
    NAVIGATION_METADATA_METHOD,
    NAVIGATION_ROUTE_Y_SMOOTHING_RADIUS_CELLS,
    NAVIGATION_METADATA_VERSION,
    NAVIGATION_RECOVERY_HOTSPOT_METHOD,
    NAVIGATION_SURFACE_Y_HISTOGRAM_BINS,
    _SurfaceColumnProfile,
    _SurfaceProfileIndex,
    _surface_component_vertical_gap_seeds_for_path,
    _surface_component_y_ranges_for_path,
    _surface_route_points_for_path,
    _surface_vertical_profiles,
    build_navigation_metadata,
    cached_centerline_path,
)
from caveviewer.core.navigation.centerline import CenterlinePath


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
    hotspots = metadata["routes"][0]["recovery_hotspots"]
    assert hotspots["method"] == NAVIGATION_RECOVERY_HOTSPOT_METHOD
    assert hotspots["score_source"] == "geometry_only_v1"
    assert hotspots["light_path_scores_available"] is False
    assert hotspots["texture_feature_scores_available"] is False
    assert hotspots["cells"]
    assert len(hotspots["scores"]) == len(_flat_pairs(hotspots["cells"]))

    cached_path = cached_centerline_path({**manifest, "navigation": metadata})

    assert cached_path is not None
    assert cached_path.source == "cached_navigation_metadata"
    assert cached_path.component_size == 48
    assert cached_path.length_m == pytest.approx(metadata["routes"][0]["length_m"])
    assert cached_path.cells == _flat_pairs(metadata["routes"][0]["cells"])
    assert cached_path.cached_recovery_hotspots


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
    component_cells = _flat_pairs(metadata["routes"][0]["component_cells"])
    component_y_ranges = _flat_y_ranges(
        metadata["routes"][0]["component_y_ranges"]
    )
    assert len(points) == len(cells)
    assert len(y_ranges) == len(cells)
    assert len(component_y_ranges) == len(component_cells)
    assert all(point[1] == pytest.approx(2.0) for point in points)
    assert all(point[2] == pytest.approx(2.5) for point in points)
    assert all(0.5 < point[2] < 4.5 for point in points)
    assert len(metadata["routes"][0]["clearance_margins"]) == len(cells)
    assert min(metadata["routes"][0]["clearance_margins"]) > 0.0
    interval_payload = metadata["routes"][0][
        "component_vertical_gap_intervals"
    ]
    assert len(interval_payload) % 4 == 0
    assert len(interval_payload) >= len(component_cells) * 4

    cached_path = cached_centerline_path({**manifest, "navigation": metadata})

    assert cached_path is not None
    assert cached_path.cached_points is not None
    assert cached_path.cached_y_ranges is not None
    assert cached_path.cached_clearance_margins is not None
    assert set(component_cells) <= set(cached_path.cached_y_ranges)
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


def test_navigation_metadata_keeps_3d_points_across_widest_supported_passage():
    positions = np.array(
        [
            [float(x) + 0.5, float(y), float(z) + 0.5]
            for x in range(7)
            for z in (0, 32)
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
            for z in (0, 32)
            for value in (x, z)
        ],
    }

    metadata = build_navigation_metadata(manifest, surface_positions=positions)

    assert metadata is not None
    route = metadata["routes"][0]
    points = _flat_points(route["points"])
    route_cells = _flat_pairs(route["cells"])
    component_cells = _flat_pairs(route["component_cells"])
    component_y_ranges = _flat_y_ranges(route["component_y_ranges"])
    assert len(points) == len(route_cells)
    assert len(component_y_ranges) == len(component_cells)
    assert any(cell[1] == 16 for cell in route_cells)
    assert all(point[1] == pytest.approx(2.0) for point in points)


def test_component_bounds_never_use_interpolated_route_y():
    route_cells = ((0, 0), (17, 0), (34, 0))
    profiles = _SurfaceProfileIndex(
        global_low_y=0.0,
        global_high_y=14.0,
        columns={
            (0, 0): _SurfaceColumnProfile(
                low_y=0.0,
                high_y=4.0,
                occupied_y_bins={0, 27},
            ),
            (34, 0): _SurfaceColumnProfile(
                low_y=10.0,
                high_y=14.0,
                occupied_y_bins={68, 95},
            ),
        },
    )
    path = CenterlinePath(
        source="test",
        footprint_cell_size=1.0,
        footprint_cell_count=len(route_cells),
        component_size=len(route_cells),
        component_cells=frozenset(route_cells),
        cells=route_cells,
        centers={cell: (cell[0] + 0.5, cell[1] + 0.5) for cell in route_cells},
        clearance_scores={cell: 1 for cell in route_cells},
        endpoint_percentile=90.0,
        endpoint_threshold_clearance_cells=1,
        length_m=34.0,
    )

    points, route_y_ranges, _margins, interpolated_count = (
        _surface_route_points_for_path(
            path,
            surface_profiles=profiles,
        )
    )
    component_y_ranges, component_interpolated_count = (
        _surface_component_y_ranges_for_path(
            path,
            component_cells=route_cells,
            surface_profiles=profiles,
            route_y_ranges=route_y_ranges,
        )
    )

    assert interpolated_count == 1
    assert [point[1] for point in points] == pytest.approx(
        [2.0416666667, 7.0, 11.9583333333]
    )
    assert route_y_ranges[1] == pytest.approx(
        (5.1041666667, 8.8958333333)
    )
    assert component_interpolated_count == 1
    assert component_y_ranges[1] == pytest.approx((0.0, 14.0))


def test_component_vertical_gap_seeds_preserve_stacked_surface_gaps():
    route_cells = ((0, 0),)
    profiles = _SurfaceProfileIndex(
        global_low_y=0.0,
        global_high_y=20.0,
        vertical_bin_count=80,
        columns={
            (0, 0): _SurfaceColumnProfile(
                low_y=0.0,
                high_y=11.25,
                occupied_y_bins={0, 2, 40, 45},
            )
        },
    )

    seeds = _surface_component_vertical_gap_seeds_for_path(
        component_cells=route_cells,
        surface_profiles=profiles,
    )

    assert [cell for cell, _y in seeds] == [(0, 0)] * 3
    assert [y for _cell, y in seeds] == pytest.approx(
        [0.375, 5.375, 10.75]
    )


def test_surface_profile_bins_are_sparse_and_quarter_metre_or_finer():
    positions = np.asarray(
        [
            [0.5, -50.0, 0.5],
            [0.5, 50.0, 0.5],
        ],
        dtype=np.float64,
    )

    profiles = _surface_vertical_profiles(positions, cell_size=1.0)

    assert profiles is not None
    assert profiles.vertical_bin_count >= 400
    assert (
        (profiles.global_high_y - profiles.global_low_y)
        / profiles.vertical_bin_count
        <= 0.25
    )
    assert isinstance(profiles.columns[(0, 0)].occupied_y_bins, set)


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
    assert route["candidate_count"] >= 1
    assert route["starts_at_navigation_start"] is True
    assert route["navigation_start_distance_m"] < 1.0
    assert cells[0][0] >= 8
    assert cells[-1][0] <= 1

    cached_path = cached_centerline_path({**manifest, "navigation": metadata})

    assert cached_path is not None
    assert cached_path.cells[0][0] >= 8


def test_navigation_start_emits_both_geometry_derived_diameter_directions():
    metadata = build_navigation_metadata(
        _line_manifest(length=11),
        navigation_start={"position": [5.5, 1.0, 0.5]},
    )

    assert metadata is not None
    assert metadata["route_count"] == 2
    cells = [_flat_pairs(route["cells"]) for route in metadata["routes"]]
    assert {route_cells[0] for route_cells in cells} == {(5, 0)}
    assert {route_cells[-1] for route_cells in cells} == {(0, 0), (10, 0)}
    assert all(route["closed_loop"] is False for route in metadata["routes"])
    assert all(
        route["starts_at_navigation_start"] is True
        for route in metadata["routes"]
    )


def test_obj_start_anchor_preserves_first_cell_center_and_vertical_profile():
    positions = np.array(
        [
            [float(x) + 0.5, float(y), float(z) + 0.5]
            for z in range(9)
            for x in (0, 4)
            for y in ((10, 14) if (x, z) == (0, 8) else (0, 4))
        ],
        dtype=np.float32,
    )
    manifest = {
        "chunk_size": 1.0,
        "footprint_cell_size": 1.0,
        "footprint_cells": [
            value
            for z in range(9)
            for x in (0, 4)
            for value in (x, z)
        ],
    }
    anchor = {
        "position": [0.5, 10.0, 8.5],
        "kind": "obj_surface_vertex",
        "source": "map.obj",
        "source_vertex_index": 0,
        "source_order": "obj_declaration_order",
        "executable": False,
        "attachment_required": True,
        "attachment_coordinate_space": "xyz",
    }

    metadata = build_navigation_metadata(
        manifest,
        surface_positions=positions,
        navigation_start_anchor=anchor,
    )

    assert metadata is not None
    assert metadata["navigation_start_anchor"] == anchor
    assert "navigation_start" not in metadata
    route = metadata["routes"][0]
    cells = _flat_pairs(route["cells"])
    points = _flat_points(route["points"])
    assert route["selection_method"] == (
        "obj_source_anchor_to_farthest_endpoint_v1"
    )
    assert "starts_at_navigation_start" not in route
    assert route["starts_at_navigation_start_anchor"] is True
    assert route["navigation_start_anchor_distance_m"] >= 0.0
    assert cells[0] == (0, 8)
    assert (points[0][0], points[0][2]) == pytest.approx((0.5, 8.5))
    assert [
        (point[0], point[2]) for point in points
    ] == pytest.approx(
        [path_center for path_center in ((x + 0.5, z + 0.5) for x, z in cells)]
    )
    assert 10.0 < points[0][1] < 14.0


def test_valid_authored_start_overrides_obj_order_anchor():
    manifest = _line_manifest(length=6)
    authored_start = {"position": [5.5, 1.0, 0.5], "source": "navigation.json"}
    anchor = {
        "position": [0.5, 1.0, 0.5],
        "kind": "obj_surface_vertex",
        "source": "map.obj",
        "source_vertex_index": 0,
        "source_order": "obj_declaration_order",
        "executable": False,
        "attachment_required": True,
        "attachment_coordinate_space": "xyz",
    }

    metadata = build_navigation_metadata(
        manifest,
        navigation_start=authored_start,
        navigation_start_anchor=anchor,
    )

    assert metadata is not None
    assert metadata["navigation_start"] == authored_start
    assert "navigation_start_anchor" not in metadata
    assert metadata["routes"][0]["starts_at_navigation_start"] is True
    assert metadata["routes"][0]["selection_method"] == (
        "navigation_start_to_farthest_endpoint_v1"
    )


def test_obj_start_anchor_emits_both_geometry_derived_diameter_directions():
    metadata = build_navigation_metadata(
        _line_manifest(length=11),
        navigation_start_anchor={
            "position": [5.5, 1.0, 0.5],
            "kind": "obj_surface_vertex",
            "source": "map.obj",
            "source_vertex_index": 0,
            "source_order": "obj_declaration_order",
            "executable": False,
            "attachment_required": True,
            "attachment_coordinate_space": "xyz",
        },
    )

    assert metadata is not None
    assert metadata["route_count"] == 2
    routes = metadata["routes"]
    cells = [_flat_pairs(route["cells"]) for route in routes]
    assert {route_cells[0] for route_cells in cells} == {(5, 0)}
    assert {route_cells[-1] for route_cells in cells} == {(0, 0), (10, 0)}
    assert all(route["closed_loop"] is False for route in routes)


def test_obj_start_anchor_reserved_schema_fails_closed_during_metadata_build():
    with pytest.raises(ValueError, match="OBJ navigation start anchor"):
        build_navigation_metadata(
            _line_manifest(length=3),
            navigation_start_anchor={"kind": "obj_surface_vertex"},
        )


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
