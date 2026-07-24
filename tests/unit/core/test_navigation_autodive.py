"""Tests for user-facing centerline Auto Dive planning."""

from __future__ import annotations

import numpy as np
import pytest

from caveviewer.core.navigation.autodive import (
    DEFAULT_AUTO_DIVE_SPEED_M_PER_SECOND,
    DEFAULT_AUTO_DIVE_VERTICAL_POSITION_FRACTION,
    AutoDiveSettings,
    _centerline_cells_form_closed_loop,
    _open_arc_from_closed_loop,
    _route_segment_stays_in_footprint,
    build_auto_dive_initial_camera_pose,
    build_centerline_auto_dive_plan,
)
from caveviewer.core.navigation.centerline import (
    DEFAULT_CENTERLINE_ROUTE_SPEED_M_PER_SECOND,
)
from caveviewer.core.navigation.cache_metadata import (
    NAVIGATION_METADATA_METHOD,
    NAVIGATION_METADATA_VERSION,
    build_navigation_metadata,
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


def test_auto_dive_prefers_cached_navigation_centerline_metadata():
    plan = build_centerline_auto_dive_plan(
        _manifest_with_cached_navigation_route(),
        current_position=(20.5, 1.0, 0.5),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            keyframe_spacing_m=1000.0,
        ),
    )

    assert plan.centerline_path.source == "cached_navigation_metadata"
    assert plan.route_cells == ((20, 0), (21, 0), (22, 0))
    assert plan.route_points_xz[0] == (20.5, 0.5)
    assert plan.route_points_xz[-1] == (22.5, 0.5)
    assert plan.route_points[1][1] == pytest.approx(1.5)


def test_auto_dive_initial_camera_pose_uses_cached_route_endpoint():
    pose = build_auto_dive_initial_camera_pose(
        _manifest_with_cached_navigation_route(),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            keyframe_spacing_m=1000.0,
            smoothing_radius_cells=0,
        ),
    )

    assert pose.position == (20.5, 1.5, 0.5)
    assert pose.yaw_deg == pytest.approx(0.0)
    assert pose.pitch_deg == pytest.approx(0.0)


def test_auto_dive_initial_camera_pose_uses_navigation_start_route_direction():
    component_cells = [
        (x, z)
        for x in range(7)
        for z in range(5)
    ]
    route_cells = tuple((3, z) for z in range(5))
    route_points = tuple(
        (3.5, 1.0, float(z) + 0.5)
        for _x, z in route_cells
    )
    manifest = _manifest_with_cached_route(
        component_cells=component_cells,
        route_cells=route_cells,
        route_points=route_points,
    )
    manifest["navigation"]["navigation_start"] = {
        "position": [3.5, 1.0, 0.5],
        "source": "test",
    }
    manifest["navigation"]["routes"][0]["starts_at_navigation_start"] = True

    pose = build_auto_dive_initial_camera_pose(
        manifest,
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=0,
        ),
    )

    assert pose.position == (3.5, 1.0, 0.5)
    assert pose.yaw_deg == pytest.approx(90.0)


def test_auto_dive_initial_camera_pose_prefers_clear_endpoint():
    component_cells = [
        (x, z)
        for x in range(10)
        for z in range(5)
    ]
    route_cells = (
        (0, 0),
        (1, 1),
        (2, 2),
        (3, 2),
        (4, 2),
        (5, 2),
        (6, 2),
        (7, 2),
        (8, 2),
        (9, 2),
    )
    route_points = tuple(
        (float(x) + 0.5, 1.0, float(z) + 0.5)
        for x, z in route_cells
    )
    manifest = _manifest_with_cached_route(
        component_cells=component_cells,
        route_cells=route_cells,
        route_points=route_points,
    )

    pose = build_auto_dive_initial_camera_pose(
        manifest,
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=0,
        ),
    )

    assert pose.position != (0.5, 1.0, 0.5)
    assert pose.position[0] <= 2.5 or pose.position[0] >= 7.5
    assert pose.position[2] >= 1.5


def test_auto_dive_initial_camera_pose_uses_physical_end_not_cached_midroute():
    manifest = _manifest_with_cached_midroute_in_long_component()

    pose = build_auto_dive_initial_camera_pose(
        manifest,
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=2,
        ),
    )

    assert not 8.0 <= pose.position[0] <= 13.0
    assert pose.position[0] <= 4.5 or pose.position[0] >= 16.5


def test_auto_dive_replan_inside_current_cell_targets_next_cell_not_same_center():
    plan = build_centerline_auto_dive_plan(
        _manifest_with_cached_progress_route(),
        current_position=(20.75, 1.0, 0.5),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=0,
        ),
    )

    assert plan.route_cells == ((20, 0), (21, 0), (22, 0))
    assert plan.route_points[0] == (20.75, 1.0, 0.5)
    assert plan.route_points[1] == (21.5, 1.5, 0.5)
    assert (20.5, 1.5, 0.5) not in plan.route_points


def test_auto_dive_replan_past_midpoint_continues_cached_route_direction():
    plan = build_centerline_auto_dive_plan(
        _manifest_with_cached_topography_route(),
        current_position=(18.5, 10.0, 0.5),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=0,
        ),
    )

    assert plan.route_cells[0] == (18, 0)
    assert plan.route_cells[-1] == (23, 0)
    assert all(point[0] >= 18.5 for point in plan.route_points)
    assert (17.5, 10.0, 0.5) not in plan.route_points


def test_auto_dive_uses_surface_span_filled_metadata_for_all_axis_centering():
    manifest, positions = _surface_wall_corridor_manifest()
    metadata = build_navigation_metadata(manifest, surface_positions=positions)

    plan = build_centerline_auto_dive_plan(
        {**manifest, "navigation": metadata},
        current_position=(1.5, 2.0, 1.5),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=0,
        ),
    )

    assert metadata is not None
    assert metadata["navigation_footprint_source"] == "surface_span_fill_v1"
    assert plan.centerline_path.cached_clearance_margins is not None
    assert all(0.5 < point[2] < 4.5 for point in plan.route_points)
    assert any(point[2] == pytest.approx(2.5) for point in plan.route_points)


def test_auto_dive_dedupes_duplicate_cached_navigation_points():
    plan = build_centerline_auto_dive_plan(
        _manifest_with_duplicate_cached_navigation_points(),
        current_position=(20.5, 1.0, 0.5),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            keyframe_spacing_m=1000.0,
        ),
    )

    keyframe_times = [keyframe.time_s for keyframe in plan.route.keyframes]
    assert keyframe_times == sorted(set(keyframe_times))
    assert plan.route_points == (
        (20.5, 1.0, 0.5),
        (20.5, 1.5, 0.5),
        (21.5, 1.5, 1.5),
    )


def test_auto_dive_applies_runtime_cached_y_smoothing_radius():
    manifest = _manifest_with_cached_topography_route()
    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=(0.5, 2.0, 0.5),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=5,
        ),
    )

    low_section_points = tuple(
        point for point in plan.route_points
        if point[0] < 12.0
    )
    high_section_points = tuple(
        point for point in plan.route_points
        if point[0] >= 12.0
    )

    assert plan.route_points[0] == (0.5, 2.0, 0.5)
    assert plan.route_points[-1] == (23.5, 10.0, 0.5)
    assert any(point[1] > 2.0 for point in low_section_points[1:])
    assert any(point[1] < 10.0 for point in high_section_points[:-1])
    assert all(0.0 <= point[1] <= 4.0 for point in low_section_points)
    assert all(8.0 <= point[1] <= 12.0 for point in high_section_points)
    assert _all_route_segments_stay_in_footprint(
        plan.route_points,
        manifest=manifest,
    )


def test_auto_dive_can_disable_runtime_cached_y_smoothing():
    plan = build_centerline_auto_dive_plan(
        _manifest_with_cached_topography_route(),
        current_position=(0.5, 2.0, 0.5),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=0,
        ),
    )

    assert [point[1] for point in plan.route_points] == pytest.approx(
        [2.0] * 12 + [10.0] * 12
    )


def test_auto_dive_applies_runtime_cached_xz_smoothing_inside_footprint():
    manifest, raw_route_points = _manifest_with_cached_wide_zigzag_route()

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=raw_route_points[0],
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=2,
        ),
    )

    assert any(
        _point_distance_xz(point, raw_point) > 1e-6
        for point, raw_point in zip(plan.route_points, raw_route_points, strict=True)
    )
    assert _all_route_segments_stay_in_footprint(
        plan.route_points,
        manifest=manifest,
    )


def test_auto_dive_multicandidate_smoothing_prefers_central_line_of_sight_path():
    manifest, raw_route_points = _manifest_with_cached_wide_zigzag_route()

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=raw_route_points[0],
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=4,
        ),
    )

    middle_points = tuple(
        point for point in plan.route_points
        if 3.0 <= point[0] <= 7.0
    )

    assert len(plan.route_points) > len(raw_route_points)
    assert middle_points
    assert all(2.25 <= point[2] <= 2.55 for point in middle_points)
    assert _all_route_segments_stay_in_footprint(
        plan.route_points,
        manifest=manifest,
    )


def test_auto_dive_candidate_scores_are_available_for_diagnostics():
    manifest, raw_route_points = _manifest_with_cached_wide_zigzag_route()
    events = []

    build_centerline_auto_dive_plan(
        manifest,
        current_position=raw_route_points[0],
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=2,
        ),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    candidate_events = [
        payload
        for event, payload in events
        if event == "candidate_scores"
    ]
    assert candidate_events
    assert candidate_events[-1]["selected"]
    assert candidate_events[-1]["candidate_count"] >= 2
    assert {
        "name",
        "route_clear",
        "entry_clear",
        "forward_progress_m",
        "curvature_rad",
    } <= set(candidate_events[-1]["candidates"][0])


def test_auto_dive_rejects_cached_xz_smoothing_that_would_cut_walls():
    manifest, raw_route_points = _manifest_with_cached_l_bend_route()

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=(0.5, 1.0, 0.5),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=4,
        ),
    )

    assert _all_route_segments_stay_in_footprint(
        plan.route_points,
        manifest=manifest,
    )
    component = frozenset(
        _flat_pairs(manifest["navigation"]["routes"][0]["component_cells"])
    )
    assert not _route_segment_stays_in_footprint(
        (5.5, 1.0, 0.5),
        (6.5, 1.0, 1.5),
        component_cells=component,
        cell_size=1.0,
    )
    raw_route_xz = {
        (point[0], point[2])
        for point in raw_route_points
    }
    assert any(
        point not in raw_route_xz
        for point in plan.route_points_xz[1:-1]
    )
    assert any(
        5.5 < point[0] < 6.5 and 0.5 < point[1] < 1.5
        for point in plan.route_points_xz
    )
    assert plan.route_points_xz[-1] == (6.5, 6.5)


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


def _manifest_with_cached_navigation_route():
    chunks = {
        f"{x}_0_0": {
            "bounds_min": [float(x), 0.0, 0.0],
            "bounds_max": [float(x + 1), 2.0, 1.0],
        }
        for x in (0, 1, 2, 20, 21, 22)
    }
    return {
        "chunk_size": 1.0,
        "footprint_cell_size": 1.0,
        "footprint_cells": [0, 0, 1, 0, 2, 0],
        "chunks": chunks,
        "navigation": {
            "version": NAVIGATION_METADATA_VERSION,
            "method": NAVIGATION_METADATA_METHOD,
            "recommended_route_id": "cached-main",
            "routes": [
                {
                    "id": "cached-main",
                    "kind": "centerline",
                    "source": "test",
                    "selection_method": "physical_endpoint_diameter_v1",
                    "closed_loop": False,
                    "length_m": 2.0,
                    "footprint_cell_size": 1.0,
                    "footprint_cell_count": 3,
                    "component_size": 3,
                    "component_cells": [20, 0, 21, 0, 22, 0],
                    "cells": [20, 0, 21, 0, 22, 0],
                    "points": [
                        20.5,
                        1.5,
                        0.5,
                        21.5,
                        1.5,
                        0.5,
                        22.5,
                        1.5,
                        0.5,
                    ],
                    "point_source": "surface_vertical_gap",
                    "endpoint_percentile": 70.0,
                    "endpoint_threshold_clearance_cells": 1,
                }
            ],
        },
    }


def _manifest_with_cached_progress_route():
    manifest = _manifest_with_cached_navigation_route()
    manifest["navigation"]["routes"][0]["points"] = [
        20.5,
        1.5,
        0.5,
        21.5,
        1.5,
        0.5,
        22.5,
        1.5,
        0.5,
    ]
    return manifest


def _surface_wall_corridor_manifest():
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
    return manifest, positions


def _manifest_with_duplicate_cached_navigation_points():
    chunks = {
        f"{x}_0_{z}": {
            "bounds_min": [float(x), 0.0, float(z)],
            "bounds_max": [float(x + 1), 2.0, float(z + 1)],
        }
        for x, z in ((20, 0), (21, 0), (21, 1))
    }
    return {
        "chunk_size": 1.0,
        "footprint_cell_size": 1.0,
        "footprint_cells": [20, 0, 21, 0, 21, 1],
        "chunks": chunks,
        "navigation": {
            "version": NAVIGATION_METADATA_VERSION,
            "method": NAVIGATION_METADATA_METHOD,
            "recommended_route_id": "cached-main",
            "routes": [
                {
                    "id": "cached-main",
                    "kind": "centerline",
                    "source": "test",
                    "closed_loop": False,
                    "length_m": 2.0,
                    "footprint_cell_size": 1.0,
                    "footprint_cell_count": 3,
                    "component_size": 3,
                    "component_cells": [20, 0, 21, 0, 21, 1],
                    "cells": [20, 0, 21, 0, 21, 1],
                    "points": [
                        20.5,
                        1.5,
                        0.5,
                        20.5,
                        1.5,
                        0.5,
                        21.5,
                        1.5,
                        1.5,
                    ],
                    "point_source": "surface_vertical_gap",
                    "endpoint_percentile": 70.0,
                    "endpoint_threshold_clearance_cells": 1,
                }
            ],
        },
    }


def _manifest_with_cached_topography_route():
    cells = [(x, 0) for x in range(24)]
    points = []
    y_ranges = []
    for x, z in cells:
        low_section = x < 12
        y = 2.0 if low_section else 10.0
        low_y, high_y = (0.0, 4.0) if low_section else (8.0, 12.0)
        points.extend((float(x) + 0.5, y, float(z) + 0.5))
        y_ranges.extend((low_y, high_y))

    return {
        "chunk_size": 1.0,
        "footprint_cell_size": 1.0,
        "footprint_cells": [
            value
            for cell in cells
            for value in cell
        ],
        "chunks": {
            f"{x}_0_{z}": {
                "bounds_min": [float(x), 0.0, float(z)],
                "bounds_max": [float(x + 1), 12.0, float(z + 1)],
            }
            for x, z in cells
        },
        "navigation": {
            "version": NAVIGATION_METADATA_VERSION,
            "method": NAVIGATION_METADATA_METHOD,
            "recommended_route_id": "cached-main",
            "routes": [
                {
                    "id": "cached-main",
                    "kind": "centerline",
                    "source": "test",
                    "closed_loop": False,
                    "length_m": 23.0,
                    "footprint_cell_size": 1.0,
                    "footprint_cell_count": len(cells),
                    "component_size": len(cells),
                    "component_cells": [
                        value
                        for cell in cells
                        for value in cell
                    ],
                    "cells": [
                        value
                        for cell in cells
                        for value in cell
                    ],
                    "points": points,
                    "y_ranges": y_ranges,
                    "point_source": "surface_vertical_gap_raw",
                    "endpoint_percentile": 70.0,
                    "endpoint_threshold_clearance_cells": 1,
                }
            ],
        },
    }


def _manifest_with_cached_wide_zigzag_route():
    component_cells = [
        (x, z)
        for x in range(10)
        for z in range(5)
    ]
    z_values = (2, 1, 2, 3, 2, 1, 2, 3, 2, 1)
    route_cells = tuple((x, z) for x, z in enumerate(z_values))
    route_points = tuple(
        (float(x) + 0.5, 1.0, float(z) + 0.5)
        for x, z in route_cells
    )
    return (
        _manifest_with_cached_route(
            component_cells=component_cells,
            route_cells=route_cells,
            route_points=route_points,
        ),
        route_points,
    )


def _manifest_with_cached_l_bend_route():
    route_cells = (
        *[(x, 0) for x in range(7)],
        *[(6, z) for z in range(1, 7)],
    )
    route_points = tuple(
        (float(x) + 0.5, 1.0, float(z) + 0.5)
        for x, z in route_cells
    )
    return (
        _manifest_with_cached_route(
            component_cells=route_cells,
            route_cells=route_cells,
            route_points=route_points,
        ),
        route_points,
    )


def _manifest_with_cached_midroute_in_long_component():
    component_cells = [
        (x, z)
        for x in range(21)
        for z in range(5)
    ]
    route_cells = tuple((x, 2) for x in range(8, 13))
    route_points = tuple(
        (float(x) + 0.5, 1.0, 2.5)
        for x, _z in route_cells
    )
    return _manifest_with_cached_route(
        component_cells=component_cells,
        route_cells=route_cells,
        route_points=route_points,
    )


def _manifest_with_cached_route(
    *,
    component_cells,
    route_cells,
    route_points,
):
    return {
        "chunk_size": 1.0,
        "footprint_cell_size": 1.0,
        "footprint_cells": [
            value
            for cell in component_cells
            for value in cell
        ],
        "chunks": {
            f"{x}_0_{z}": {
                "bounds_min": [float(x), 0.0, float(z)],
                "bounds_max": [float(x + 1), 2.0, float(z + 1)],
            }
            for x, z in component_cells
        },
        "navigation": {
            "version": NAVIGATION_METADATA_VERSION,
            "method": NAVIGATION_METADATA_METHOD,
            "recommended_route_id": "cached-main",
            "routes": [
                {
                    "id": "cached-main",
                    "kind": "centerline",
                    "source": "test",
                    "closed_loop": False,
                    "length_m": float(len(route_cells) - 1),
                    "footprint_cell_size": 1.0,
                    "footprint_cell_count": len(component_cells),
                    "component_size": len(component_cells),
                    "component_cells": [
                        value
                        for cell in component_cells
                        for value in cell
                    ],
                    "cells": [
                        value
                        for cell in route_cells
                        for value in cell
                    ],
                    "points": [
                        value
                        for point in route_points
                        for value in point
                    ],
                    "point_source": "surface_vertical_gap_raw",
                    "endpoint_percentile": 70.0,
                    "endpoint_threshold_clearance_cells": 1,
                }
            ],
        },
    }


def _point_distance_xz(first, second) -> float:
    return (
        (first[0] - second[0]) ** 2
        + (first[2] - second[2]) ** 2
    ) ** 0.5


def _all_route_segments_stay_in_footprint(route_points, *, manifest) -> bool:
    component = frozenset(
        _flat_pairs(manifest["navigation"]["routes"][0]["component_cells"])
    )
    return all(
        _route_segment_stays_in_footprint(
            first,
            second,
            component_cells=component,
            cell_size=1.0,
        )
        for first, second in zip(route_points, route_points[1:], strict=False)
    )


def _flat_pairs(flat: list[int]) -> tuple[tuple[int, int], ...]:
    return tuple(
        (int(flat[index]), int(flat[index + 1]))
        for index in range(0, len(flat), 2)
    )
