"""Tests for user-facing centerline Guided Dive planning."""

from __future__ import annotations

import numpy as np
import pytest

from caveviewer.core.chunking import io as chunk_io
from caveviewer.core.navigation.autodive import (
    AutoDivePlanningBudgetExceeded,
    DEFAULT_AUTO_DIVE_SPEED_M_PER_SECOND,
    DEFAULT_AUTO_DIVE_VERTICAL_POSITION_FRACTION,
    _AutoDivePlanningBudget,
    _AutoDiveCollisionValidator,
    AutoDiveSettings,
    _AutoDiveRouteSamples,
    _auto_dive_points_for_waypoint_cells,
    _centerline_cells_form_closed_loop,
    _cone_chain_anchor_indices,
    _mesh_clear_recovery_footprint_path,
    _mesh_recovery_edge_is_clear,
    _open_arc_from_closed_loop,
    _repelled_auto_dive_points,
    _mesh_recovery_scan_alignment,
    _mesh_recovery_turn_angle,
    _mesh_recovery_view_alignment,
    _route_segment_stays_in_footprint,
    build_auto_dive_initial_camera_pose,
    build_centerline_auto_dive_plan,
)
from caveviewer.core.navigation.centerline import (
    DEFAULT_CENTERLINE_ROUTE_SPEED_M_PER_SECOND,
)
from caveviewer.core.navigation.mesh_collision import CachedChunkMeshCollisionGuard
from caveviewer.core.navigation.route import NavigationConfigurationError
from caveviewer.core.navigation.cache_metadata import (
    NAVIGATION_METADATA_METHOD,
    NAVIGATION_METADATA_VERSION,
    build_navigation_metadata,
    cached_centerline_path,
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
    assert plan.render_distance_cells == 4


def test_auto_dive_runtime_planning_budget_reports_expired_phase():
    events = []

    with pytest.raises(AutoDivePlanningBudgetExceeded):
        build_centerline_auto_dive_plan(
            _l_bend_manifest(),
            current_position=(0.5, 1.0, 0.5),
            settings=AutoDiveSettings(
                planning_budget_s=1e-9,
                keyframe_spacing_m=1000.0,
            ),
            diagnostics=lambda event, payload: events.append((event, payload)),
        )

    budget_events = [
        payload
        for event, payload in events
        if event == "planning_budget_exceeded"
    ]
    assert budget_events
    assert budget_events[-1]["phase"] == "initialization"


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


def test_auto_dive_voxel_route_uses_cell_centers_not_cached_centerline_points():
    manifest = _manifest_with_cached_route(
        component_cells=((0, 0), (1, 0), (1, 1)),
        route_cells=((0, 0), (1, 0), (1, 1)),
        route_points=(
            (0.5, 1.0, 0.5),
            (0.5, 1.0, 0.5),
            (1.5, 1.0, 1.5),
        ),
    )
    centerline_path = cached_centerline_path(manifest)

    assert centerline_path is not None
    points = _auto_dive_points_for_waypoint_cells(
        centerline_path,
        waypoint_cells=((1, 0), (1, 1)),
        route_xz=((1.5, 0.5), (1.5, 1.5)),
        manifest=manifest,
        settings=AutoDiveSettings(smoothing_radius_cells=0),
        prefer_route_cell_centers=True,
        fallback_y=1.0,
    )

    assert tuple((point[0], point[2]) for point in points) == (
        (1.5, 0.5),
        (1.5, 1.5),
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

    assert len(plan.route_points) >= len(raw_route_points)
    assert any(
        _point_distance_xz(point, raw_point) > 1e-6
        for point, raw_point in zip(plan.route_points, raw_route_points, strict=True)
    )
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
    assert candidate_events[-1]["travel_filter"]["enabled"] is False
    assert {
        "name",
        "route_clear",
        "entry_clear",
        "forward_progress_m",
        "curvature_rad",
        "total_change_per_m",
    } <= set(candidate_events[-1]["candidates"][0])
    assert any(
        str(candidate["name"]).startswith("cone-")
        for candidate in candidate_events[-1]["candidates"]
    )


def test_auto_dive_candidate_diagnostics_report_first_clearance_failure():
    manifest = _manifest_with_cached_route(
        component_cells=((0, 0), (1, 1), (2, 1)),
        route_cells=((0, 0), (1, 1), (2, 1)),
        route_points=(
            (0.5, 1.0, 0.5),
            (1.5, 1.0, 1.5),
            (2.5, 1.0, 1.5),
        ),
    )
    events = []

    build_centerline_auto_dive_plan(
        manifest,
        current_position=(0.5, 1.0, 0.5),
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
    failures = [
        candidate["first_clearance_failure"]
        for candidate in candidate_events[-1]["candidates"]
        if candidate["first_clearance_failure"] is not None
    ]

    assert failures
    assert any(
        failure["reason"] == "invalid_footprint_transition"
        for failure in failures
    )


def test_auto_dive_prefers_dense_route_over_untrusted_clear_shortcut():
    manifest, raw_route_points = _manifest_with_cached_wall_hugging_route()
    events = []

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=raw_route_points[0],
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=8,
        ),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    candidate_payload = [
        payload
        for event, payload in events
        if event == "candidate_scores"
    ][-1]
    clear_shortcuts = [
        candidate
        for candidate in candidate_payload["candidates"]
        if candidate["route_clear"] and not candidate["geometry_trusted"]
    ]

    assert clear_shortcuts
    assert candidate_payload["selection_reason"] == (
        "trusted_dense_low_clearance_fallback"
    )
    selected_candidate = next(
        candidate
        for candidate in candidate_payload["candidates"]
        if candidate["name"] == candidate_payload["selected"]
    )
    assert not str(candidate_payload["selected"]).startswith(("theta", "cone"))
    assert candidate_payload["selected_geometry_trusted"] is True
    assert len(plan.route_points) >= len(raw_route_points)
    assert (
        selected_candidate["max_segment_cells"]
        <= candidate_payload["trusted_max_segment_cells"]
    )


def test_auto_dive_mesh_guard_rejects_cached_wall_shortcut(tmp_path):
    manifest, raw_route_points = _manifest_with_cached_mesh_wall_route()
    cache_dir = tmp_path / "cache"
    _write_test_chunk_mesh(
        cache_dir,
        cell=(0, 0, 0),
        triangles=(
            (
                (5.5, 0.0, 0.9),
                (5.5, 2.0, 0.9),
                (5.5, 2.0, 5.5),
            ),
            (
                (5.5, 0.0, 0.9),
                (5.5, 2.0, 5.5),
                (5.5, 0.0, 5.5),
            ),
        ),
    )
    events = []

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=raw_route_points[0],
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=8,
        ),
        cache_dir=str(cache_dir),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    candidate_payload = [
        payload
        for event, payload in events
        if event == "candidate_scores"
    ][-1]
    mesh_failures = [
        candidate["first_clearance_failure"]
        for candidate in candidate_payload["candidates"]
        if candidate["first_clearance_failure"] is not None
        and candidate["first_clearance_failure"]["reason"] == "mesh_intersection"
    ]

    assert candidate_payload["mesh_collision_enabled"] is True
    assert mesh_failures
    assert all("chunk_cell" in failure for failure in mesh_failures)
    assert not str(candidate_payload["selected"]).startswith(("theta", "cone"))
    assert plan.route_points_xz == tuple((point[0], point[2]) for point in raw_route_points)


def test_auto_dive_skips_mesh_guard_for_dense_chunk_cache(monkeypatch, tmp_path):
    manifest, raw_route_points = _manifest_with_cached_mesh_wall_route()
    manifest["triangle_count"] = 1_000_000
    events = []

    def fail_load_triangles(_self, _cell):
        raise AssertionError("high-poly startup must not load chunk triangles")

    monkeypatch.setattr(
        CachedChunkMeshCollisionGuard,
        "_load_triangles_for_chunk",
        fail_load_triangles,
    )

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=raw_route_points[0],
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=4,
        ),
        cache_dir=str(tmp_path / "cache"),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    candidate_payload = [
        payload
        for event, payload in events
        if event == "candidate_scores"
    ][-1]

    assert candidate_payload["mesh_collision_enabled"] is False
    assert plan.route_length_m > 0.0


def test_auto_dive_enables_recovery_when_dense_triangles_are_chunked(tmp_path):
    manifest = _split_manifest()
    manifest["triangle_count"] = 1_000_000

    guard = CachedChunkMeshCollisionGuard.from_manifest(
        manifest,
        cache_dir=str(tmp_path / "cache"),
    )

    assert guard is not None
    assert guard.mesh_recovery_enabled is True


def test_auto_dive_mesh_guard_trims_fully_blocked_route_to_safe_prefix(tmp_path):
    manifest, raw_route_points = _manifest_with_cached_mesh_blocked_route()
    cache_dir = tmp_path / "cache"
    _write_test_chunk_mesh(
        cache_dir,
        cell=(0, 0, 0),
        triangles=(
            (
                (5.5, 0.0, 0.0),
                (5.5, 2.0, 0.0),
                (5.5, 2.0, 6.0),
            ),
            (
                (5.5, 0.0, 0.0),
                (5.5, 2.0, 6.0),
                (5.5, 0.0, 6.0),
            ),
        ),
    )
    events = []

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=raw_route_points[0],
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=4,
        ),
        cache_dir=str(cache_dir),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    candidate_payload = [
        payload
        for event, payload in events
        if event == "candidate_scores"
    ][-1]

    assert candidate_payload["selected"] == "raw"
    assert candidate_payload["selection_reason"] == (
        "mesh_compromised_prefix_fallback"
    )
    assert candidate_payload["mesh_collision_enabled"] is True
    assert candidate_payload["selected_route_truncated"] is True
    assert candidate_payload["selected_safe_prefix_length_m"] > 0.0
    assert all(
        not candidate["mesh_clear"]
        for candidate in candidate_payload["candidates"]
    )
    assert plan.route_length_m > 0.0
    assert max(point[0] for point in plan.route_points) < 5.5
    assert max(point[0] for point in plan.route_points) <= 3.5


def test_auto_dive_mesh_recovery_logs_search_alternatives(tmp_path):
    manifest, raw_route_points = _manifest_with_cached_mesh_blocked_route()
    cache_dir = tmp_path / "cache"
    _write_test_chunk_mesh(
        cache_dir,
        cell=(0, 0, 0),
        triangles=(
            (
                (5.5, 0.0, 0.0),
                (5.5, 2.0, 0.0),
                (5.5, 2.0, 6.0),
            ),
            (
                (5.5, 0.0, 0.0),
                (5.5, 2.0, 6.0),
                (5.5, 0.0, 6.0),
            ),
        ),
    )
    events = []

    build_centerline_auto_dive_plan(
        manifest,
        current_position=raw_route_points[0],
        current_yaw=0.0,
        current_pitch=0.0,
        current_travel_yaw=0.0,
        current_travel_pitch=0.0,
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=4,
        ),
        cache_dir=str(cache_dir),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    recovery_events = [
        payload
        for event, payload in events
        if event == "mesh_recovery_search"
    ]

    assert recovery_events
    payload = recovery_events[-1]
    assert payload["visited_cells"] > 0
    assert payload["edge_tests"] > 0
    assert 0 < len(payload["top_candidates"]) <= payload["candidate_count"]
    assert payload["selected"] is not None
    assert payload["selected_candidate"]["cell"] == payload["selected"]
    assert {
        "selection_score_m",
        "path_quality_m",
        "path_distance_m",
        "forward_alignment",
        "turn_penalty_rad",
        "selection_turn_penalty_m",
        "path_avoidance_count",
        "path_cells",
        "scan_angle_penalty_deg",
    } <= set(payload["top_candidates"][0])
    assert payload["selection_turn_penalty_cells"] == pytest.approx(1.0)
    assert payload["path_avoidance_radius_cells"] == 1
    assert payload["budget_exhausted"] is False
    assert payload["max_visited_cells"] > payload["visited_cells"]
    assert payload["max_edge_tests"] > payload["edge_tests"]


def test_auto_dive_voxel_recovery_does_not_reject_filled_space_for_lateral_score(
    tmp_path,
):
    component_cells = [
        (x, z)
        for x in range(5)
        for z in range(5)
    ]
    route_cells = ((0, 2), (1, 2))
    route_points = tuple(
        (float(x) + 0.5, 1.0, float(z) + 0.5)
        for x, z in route_cells
    )
    manifest = _manifest_with_cached_route(
        component_cells=component_cells,
        route_cells=route_cells,
        route_points=route_points,
    )
    manifest["navigation"]["routes"][0]["clearance_margins"] = [
        1.0 for _cell in route_cells
    ]
    centerline_path = cached_centerline_path(manifest)
    guard = CachedChunkMeshCollisionGuard.from_manifest(
        manifest,
        cache_dir=str(tmp_path),
    )

    assert centerline_path is not None
    assert guard is not None
    current = route_points[0]
    strict_cache = {(0, 2): current}
    assert not _mesh_recovery_edge_is_clear(
        centerline_path,
        (0, 2),
        (1, 2),
        current_point=current,
        start_cell=(0, 2),
        collision_validator=_AutoDiveCollisionValidator(
            centerline_path,
            mesh_guard=guard,
        ),
        point_cache=strict_cache,
        edge_cache={},
    )

    voxel_cache = {(0, 2): current}
    assert _mesh_recovery_edge_is_clear(
        centerline_path,
        (0, 2),
        (1, 2),
        current_point=current,
        start_cell=(0, 2),
        collision_validator=_AutoDiveCollisionValidator(
            centerline_path,
            mesh_guard=guard,
        ),
        point_cache=voxel_cache,
        edge_cache={},
        use_footprint_centers=True,
    )


def test_auto_dive_mesh_recovery_logs_budget_abort_details(tmp_path):
    component_cells = ((0, 0), (1, 0), (2, 0))
    route_points = tuple(
        (float(x) + 0.5, 1.0, 0.5)
        for x, _z in component_cells
    )
    manifest = _manifest_with_cached_route(
        component_cells=component_cells,
        route_cells=component_cells,
        route_points=route_points,
    )
    centerline_path = cached_centerline_path(manifest)
    guard = CachedChunkMeshCollisionGuard.from_manifest(
        manifest,
        cache_dir=str(tmp_path),
    )
    events = []

    assert centerline_path is not None
    assert guard is not None
    with pytest.raises(AutoDivePlanningBudgetExceeded):
        _mesh_clear_recovery_footprint_path(
            centerline_path,
            start_cell=(0, 0),
            target_indices={(1, 0): 1},
            current_point=route_points[0],
            current_yaw=0.0,
            current_pitch=0.0,
            current_travel_yaw=0.0,
            current_travel_pitch=0.0,
            avoid_positions=None,
            collision_validator=_AutoDiveCollisionValidator(
                centerline_path,
                mesh_guard=guard,
            ),
            diagnostics=lambda event, payload: events.append((event, payload)),
            planning_budget=_AutoDivePlanningBudget(
                started_at=0.0,
                budget_s=1e-9,
            ),
        )

    payload = [
        payload
        for event, payload in events
        if event == "mesh_recovery_search"
    ][-1]
    assert payload["aborted"] is True
    assert payload["abort_phase"] == "mesh_recovery_search"
    assert payload["budget_exhausted"] is True
    assert payload["visited_cells"] == 0


def test_auto_dive_mesh_recovery_builds_local_voxel_volume_for_a_bend(tmp_path):
    manifest, raw_route_points = _manifest_with_cached_l_bend_route()
    manifest["chunk_size"] = 16.0
    manifest["chunks"] = {
        "0_0_0": {
            "bounds_min": [0.0, 0.0, 0.0],
            "bounds_max": [8.0, 2.0, 8.0],
        }
    }
    cache_dir = tmp_path / "cache"
    _write_test_chunk_mesh(
        cache_dir,
        cell=(0, 0, 0),
        triangles=(
            (
                (6.0, 0.0, -1.0),
                (6.0, 2.0, -1.0),
                (6.0, 2.0, 8.0),
            ),
            (
                (6.0, 0.0, -1.0),
                (6.0, 2.0, 8.0),
                (6.0, 0.0, 8.0),
            ),
        ),
    )
    events = []

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=raw_route_points[0],
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=4,
        ),
        cache_dir=str(cache_dir),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    voxel_events = [
        payload
        for event, payload in events
        if event == "voxel_volume"
    ]
    assert voxel_events
    assert voxel_events[-1]["built"] is True
    assert voxel_events[-1]["outcome"] == "built"
    assert voxel_events[-1]["selected_region_count"] > 0
    assert voxel_events[-1]["curvature_region_count"] > 0
    assert voxel_events[-1]["volume"]["voxel_count"] <= (
        AutoDiveSettings().voxel_max_cells
    )
    assert plan.selection_reason == "mesh_recovery_route_clear"


def test_auto_dive_user_reposition_keeps_mesh_recovery_in_manual_direction(tmp_path):
    manifest, raw_route_points = _manifest_with_cached_mesh_blocked_route()
    cache_dir = tmp_path / "cache"
    _write_test_chunk_mesh(
        cache_dir,
        cell=(0, 0, 0),
        triangles=(
            (
                (5.5, 0.0, 0.0),
                (5.5, 2.0, 0.0),
                (5.5, 2.0, 6.0),
            ),
            (
                (5.5, 0.0, 0.0),
                (5.5, 2.0, 6.0),
                (5.5, 0.0, 6.0),
            ),
        ),
    )
    events = []

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=raw_route_points[0],
        current_yaw=0.0,
        current_pitch=0.0,
        current_travel_yaw=np.pi,
        current_travel_pitch=0.0,
        user_reposition=True,
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=4,
        ),
        cache_dir=str(cache_dir),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    recovery_events = [
        payload
        for event, payload in events
        if event == "mesh_recovery_search"
    ]
    candidate_payload = [
        payload
        for event, payload in events
        if event == "candidate_scores"
    ][-1]

    assert recovery_events
    assert recovery_events[-1]["allow_reverse_travel"] is False
    assert candidate_payload["user_reposition"] is True
    assert candidate_payload["selection_reason"] == "mesh_recovery_route_clear"
    assert plan.route_truncated_by_mesh is False
    assert candidate_payload["travel_filter"]["enabled"] is True


def test_auto_dive_mesh_recovery_view_alignment_uses_pitch():
    current = (0.0, 0.0, 0.0)
    uphill = (10.0, 10.0, 0.0)
    flat = (10.0, 0.0, 0.0)

    uphill_with_positive_pitch = _mesh_recovery_view_alignment(
        current,
        uphill,
        current_yaw=0.0,
        current_pitch=np.pi / 4.0,
    )
    uphill_with_flat_pitch = _mesh_recovery_view_alignment(
        current,
        uphill,
        current_yaw=0.0,
        current_pitch=0.0,
    )
    uphill_with_negative_pitch = _mesh_recovery_view_alignment(
        current,
        uphill,
        current_yaw=0.0,
        current_pitch=-np.pi / 4.0,
    )
    flat_with_positive_pitch = _mesh_recovery_view_alignment(
        current,
        flat,
        current_yaw=0.0,
        current_pitch=np.pi / 4.0,
    )

    assert uphill_with_positive_pitch == pytest.approx(1.0)
    assert uphill_with_positive_pitch > uphill_with_flat_pitch
    assert uphill_with_negative_pitch == pytest.approx(0.0)
    assert uphill_with_positive_pitch > flat_with_positive_pitch


def test_auto_dive_mesh_recovery_scan_alignment_finds_offset_pitch_yaw():
    current = (0.0, 0.0, 0.0)
    left_uphill = (10.0, 10.0, 10.0)

    in_cone, alignment, penalty = _mesh_recovery_scan_alignment(
        current,
        left_uphill,
        current_yaw=0.0,
        current_pitch=0.0,
    )

    assert in_cone is True
    assert alignment > 0.95
    assert penalty > 0.0


def test_auto_dive_mesh_recovery_scan_alignment_covers_wide_travel_cone():
    current = (0.0, 0.0, 0.0)
    yaw = np.radians(115.0)
    wide_left = (float(np.cos(yaw) * 10.0), 0.0, float(np.sin(yaw) * 10.0))

    in_cone, alignment, penalty = _mesh_recovery_scan_alignment(
        current,
        wide_left,
        current_yaw=0.0,
        current_pitch=0.0,
    )

    assert in_cone is True
    assert alignment > 0.95
    assert penalty >= 90.0


def test_auto_dive_replan_rejects_centerline_behind_travel_direction():
    route_cells = tuple((-index, 0) for index in range(8))
    route_points = tuple(
        (float(x) + 0.5, 1.0, float(z) + 0.5)
        for x, z in route_cells
    )
    events = []

    with pytest.raises(NavigationConfigurationError, match="forward travel cone"):
        build_centerline_auto_dive_plan(
            _manifest_with_cached_route(
                component_cells=route_cells,
                route_cells=route_cells,
                route_points=route_points,
            ),
            current_position=route_points[0],
            current_travel_yaw=0.0,
            current_travel_pitch=0.0,
            settings=AutoDiveSettings(
                speed_m_per_second=1.0,
                smoothing_radius_cells=0,
            ),
            diagnostics=lambda event, payload: events.append((event, payload)),
        )

    candidate_payload = [
        payload
        for event, payload in events
        if event == "candidate_scores"
    ][-1]
    assert candidate_payload["reason"] == "no_forward_travel_candidates"
    assert candidate_payload["travel_filter"]["enabled"] is True
    assert candidate_payload["travel_filter"]["after_count"] == 0
    assert candidate_payload["travel_filter"]["rejected"]


def test_auto_dive_user_reposition_enforces_manual_direction():
    route_cells = tuple((-index, 0) for index in range(8))
    route_points = tuple(
        (float(x) + 0.5, 1.0, float(z) + 0.5)
        for x, z in route_cells
    )
    events = []

    with pytest.raises(NavigationConfigurationError, match="forward travel cone"):
        build_centerline_auto_dive_plan(
            _manifest_with_cached_route(
                component_cells=route_cells,
                route_cells=route_cells,
                route_points=route_points,
            ),
            current_position=route_points[0],
            current_yaw=0.0,
            current_pitch=0.0,
            current_travel_yaw=0.0,
            current_travel_pitch=0.0,
            user_reposition=True,
            settings=AutoDiveSettings(
                speed_m_per_second=1.0,
                smoothing_radius_cells=0,
            ),
            diagnostics=lambda event, payload: events.append((event, payload)),
        )

    candidate_payload = [
        payload
        for event, payload in events
        if event == "candidate_scores"
    ][-1]

    assert candidate_payload["user_reposition"] is True
    assert candidate_payload["reason"] == "no_forward_travel_candidates"
    assert candidate_payload["travel_filter"]["enabled"] is True
    assert candidate_payload["travel_filter"]["after_count"] == 0


def test_auto_dive_initial_start_ignores_sideways_travel_cone():
    route_cells = tuple((0, z) for z in range(6))
    route_points = tuple(
        (float(x) + 0.5, 1.0, float(z) + 0.5)
        for x, z in route_cells
    )
    manifest = _manifest_with_cached_route(
        component_cells=route_cells,
        route_cells=route_cells,
        route_points=route_points,
    )

    with pytest.raises(NavigationConfigurationError, match="forward travel cone"):
        build_centerline_auto_dive_plan(
            manifest,
            current_position=route_points[0],
            current_yaw=0.0,
            current_pitch=0.0,
            current_travel_yaw=0.0,
            current_travel_pitch=0.0,
            settings=AutoDiveSettings(
                speed_m_per_second=1.0,
                smoothing_radius_cells=0,
            ),
        )

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=route_points[0],
        current_yaw=0.0,
        current_pitch=0.0,
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=0,
        ),
    )

    assert plan.route_points[0] == route_points[0]
    assert plan.route_length_m > 0.0


def test_auto_dive_uses_view_pitch_to_choose_upward_forward_direction():
    component_cells = [(x, 0) for x in range(4)]
    route_cells = tuple(component_cells)
    route_points = (
        (0.5, 2.0, 0.5),
        (1.5, 1.0, 0.5),
        (2.5, 1.0, 0.5),
        (3.5, 1.0, 0.5),
    )

    plan = build_centerline_auto_dive_plan(
        _manifest_with_cached_route(
            component_cells=component_cells,
            route_cells=route_cells,
            route_points=route_points,
        ),
        current_position=route_points[1],
        current_yaw=np.pi,
        current_pitch=np.radians(45.0),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=0,
        ),
    )

    assert plan.route_points[0] == route_points[1]
    assert plan.route_points[1] == route_points[0]
    assert plan.route.keyframes[0].pitch_deg > 0.0


def test_auto_dive_uses_camera_position_offset_to_choose_upward_direction():
    component_cells = [(x, 0) for x in range(4)]
    route_cells = tuple(component_cells)
    route_points = (
        (0.5, 3.0, 0.5),
        (1.5, 1.0, 0.5),
        (2.5, 1.0, 0.5),
        (3.5, 1.0, 0.5),
    )

    plan = build_centerline_auto_dive_plan(
        _manifest_with_cached_route(
            component_cells=component_cells,
            route_cells=route_cells,
            route_points=route_points,
        ),
        current_position=(1.5, 2.0, 0.5),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=0,
        ),
    )

    assert plan.route_points[0] == (1.5, 2.0, 0.5)
    assert plan.route_points[1] == route_points[0]
    assert plan.route.keyframes[0].pitch_deg > 0.0


def test_auto_dive_resume_does_not_backtrack_to_rejoin_centerline():
    component_cells = [(x, 0) for x in range(7)]
    route_cells = tuple(component_cells)
    route_points = tuple((float(x) + 0.5, 1.0, 0.5) for x in range(7))

    plan = build_centerline_auto_dive_plan(
        _manifest_with_cached_route(
            component_cells=component_cells,
            route_cells=route_cells,
            route_points=route_points,
        ),
        current_position=(2.75, 1.0, 0.5),
        current_yaw=0.0,
        current_pitch=0.0,
        current_travel_yaw=0.0,
        current_travel_pitch=0.0,
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=0,
        ),
    )

    assert plan.route_points[0] == (2.75, 1.0, 0.5)
    assert plan.route_points[1][0] > 2.75


def test_auto_dive_mesh_recovery_prefers_upward_forward_over_long_backtrack():
    component_cells = (
        [(x, 0) for x in range(-8, 1)]
        + [(1, 0)]
    )
    route_cells = tuple(component_cells)
    route_points = tuple(
        (
            float(x) + 0.5,
            2.0 if x == 1 else 1.0,
            float(z) + 0.5,
        )
        for x, z in route_cells
    )
    centerline_path = cached_centerline_path(
        _manifest_with_cached_route(
            component_cells=component_cells,
            route_cells=route_cells,
            route_points=route_points,
        )
    )
    events = []

    assert centerline_path is not None
    cells = _mesh_clear_recovery_footprint_path(
        centerline_path,
        start_cell=(0, 0),
        target_indices={},
        current_point=(0.5, 1.0, 0.5),
        current_yaw=0.0,
        current_pitch=np.radians(45.0),
        current_travel_yaw=0.0,
        current_travel_pitch=np.radians(45.0),
        avoid_positions=None,
        collision_validator=_AutoDiveCollisionValidator(centerline_path),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    assert cells[-1] == (1, 0)
    payload = [
        payload
        for event, payload in events
        if event == "mesh_recovery_search"
    ][-1]
    assert payload["selected_candidate"]["forward_alignment"] > 0.0


def test_auto_dive_mesh_recovery_rejects_backtracking_from_travel_direction():
    component_cells = [(0, z) for z in range(11)]
    route_cells = tuple(component_cells)
    route_points = tuple(
        (float(x) + 0.5, 1.0, float(z) + 0.5)
        for x, z in route_cells
    )
    centerline_path = cached_centerline_path(
        _manifest_with_cached_route(
            component_cells=component_cells,
            route_cells=route_cells,
            route_points=route_points,
        )
    )

    assert centerline_path is not None
    cells = _mesh_clear_recovery_footprint_path(
        centerline_path,
        start_cell=(0, 5),
        target_indices={},
        current_point=(0.5, 1.0, 5.5),
        current_yaw=np.pi / 2.0,
        current_pitch=0.0,
        current_travel_yaw=-np.pi / 2.0,
        current_travel_pitch=0.0,
        avoid_positions=None,
        collision_validator=_AutoDiveCollisionValidator(centerline_path),
    )

    assert cells[-1] == (0, 0)
    assert all(cell[1] <= 5 for cell in cells)


def test_auto_dive_mesh_recovery_prefers_long_low_turn_corridor():
    component_cells = (
        [(x, 0) for x in range(9)]
        + [(0, 1), (0, 2)]
    )
    route_cells = tuple(component_cells)
    route_points = tuple(
        (float(x) + 0.5, 1.0, float(z) + 0.5)
        for x, z in route_cells
    )
    centerline_path = cached_centerline_path(
        _manifest_with_cached_route(
            component_cells=component_cells,
            route_cells=route_cells,
            route_points=route_points,
        )
    )

    assert centerline_path is not None
    cells = _mesh_clear_recovery_footprint_path(
        centerline_path,
        start_cell=(0, 0),
        target_indices={},
        current_point=(0.5, 1.0, 0.5),
        current_yaw=0.0,
        current_pitch=0.0,
        current_travel_yaw=0.0,
        current_travel_pitch=0.0,
        avoid_positions=None,
        collision_validator=_AutoDiveCollisionValidator(centerline_path),
    )

    assert cells[-1] == (8, 0)


def test_auto_dive_mesh_recovery_prefers_longer_passage_over_short_turn():
    component_cells = (
        [(x, 0) for x in range(5)]
        + [(1, z) for z in range(1, 8)]
    )
    route_cells = tuple(component_cells)
    route_points = tuple(
        (float(x) + 0.5, 1.0, float(z) + 0.5)
        for x, z in route_cells
    )
    centerline_path = cached_centerline_path(
        _manifest_with_cached_route(
            component_cells=component_cells,
            route_cells=route_cells,
            route_points=route_points,
        )
    )
    events = []

    assert centerline_path is not None
    cells = _mesh_clear_recovery_footprint_path(
        centerline_path,
        start_cell=(0, 0),
        target_indices={},
        current_point=(0.5, 1.0, 0.5),
        current_yaw=0.0,
        current_pitch=0.0,
        current_travel_yaw=0.0,
        current_travel_pitch=0.0,
        avoid_positions=None,
        collision_validator=_AutoDiveCollisionValidator(centerline_path),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    assert cells[-1] == (1, 7)
    payload = [
        payload
        for event, payload in events
        if event == "mesh_recovery_search"
    ][-1]
    selected = payload["selected_candidate"]
    assert selected["path_distance_m"] > 7.0
    assert selected["selection_turn_penalty_m"] < selected["path_distance_m"]
    assert selected["path_avoidance_count"] == 0


def test_auto_dive_mesh_recovery_penalizes_paths_through_prior_boundaries():
    component_cells = (
        [(x, 0) for x in range(5)]
        + [(1, z) for z in range(1, 8)]
    )
    route_cells = tuple(component_cells)
    route_points = tuple(
        (float(x) + 0.5, 1.0, float(z) + 0.5)
        for x, z in route_cells
    )
    centerline_path = cached_centerline_path(
        _manifest_with_cached_route(
            component_cells=component_cells,
            route_cells=route_cells,
            route_points=route_points,
        )
    )
    events = []

    assert centerline_path is not None
    cells = _mesh_clear_recovery_footprint_path(
        centerline_path,
        start_cell=(0, 0),
        target_indices={},
        current_point=(0.5, 1.0, 0.5),
        current_yaw=0.0,
        current_pitch=0.0,
        current_travel_yaw=0.0,
        current_travel_pitch=0.0,
        avoid_positions=((1.5, 1.0, 4.5),),
        collision_validator=_AutoDiveCollisionValidator(centerline_path),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    assert cells[-1] == (4, 0)
    payload = [
        payload
        for event, payload in events
        if event == "mesh_recovery_search"
    ][-1]
    assert payload["avoid_cells"] == [[1, 4]]
    assert payload["selected_candidate"]["path_avoidance_count"] == 0
    avoided_candidates = [
        candidate
        for candidate in payload["top_candidates"]
        if candidate["cell"] == [1, 7]
    ]
    assert avoided_candidates
    assert avoided_candidates[0]["path_avoidance_count"] > 0


def test_auto_dive_mesh_recovery_turn_angle_penalizes_direction_changes():
    assert _mesh_recovery_turn_angle(None, (1, 0)) == pytest.approx(0.0)
    assert _mesh_recovery_turn_angle((1, 0), (1, 0)) == pytest.approx(0.0)
    assert _mesh_recovery_turn_angle((1, 0), (0, 1)) == pytest.approx(
        np.pi / 2.0
    )


def test_auto_dive_repulsion_uses_component_y_ranges_when_moving_off_wall():
    component_cells = [
        (x, z)
        for x in range(8)
        for z in range(5)
    ]
    route_cells = tuple((x, 0) for x in range(8))
    route_points = tuple(
        (float(x) + 0.5, 0.25, 0.5)
        for x, _z in route_cells
    )
    manifest = _manifest_with_cached_route(
        component_cells=component_cells,
        route_cells=route_cells,
        route_points=route_points,
    )
    manifest["navigation"]["routes"][0]["component_y_ranges"] = [
        value
        for _cell in component_cells
        for value in (0.0, 4.0)
    ]
    centerline_path = cached_centerline_path(manifest)

    assert centerline_path is not None
    repelled_points = _repelled_auto_dive_points(
        centerline_path,
        waypoint_cells=route_cells,
        route_points=route_points,
        settings=AutoDiveSettings(smoothing_radius_cells=4),
        collision_validator=_AutoDiveCollisionValidator(centerline_path),
    )
    moved_points = tuple(
        point for point in repelled_points[1:-1]
        if point[2] > 0.5
    )

    assert moved_points
    assert max(point[2] for point in moved_points) >= 2.5
    assert all(point[1] == pytest.approx(2.0) for point in moved_points)


def test_auto_dive_cone_chain_uses_bounded_forward_anchor_series():
    samples = _AutoDiveRouteSamples(
        cells=tuple((index, 0) for index in range(64)),
        points=tuple((float(index), 1.0, 0.5) for index in range(64)),
    )

    anchors = _cone_chain_anchor_indices(samples, radius_cells=15)

    assert 2 <= len(anchors) <= 5
    assert anchors == tuple(sorted(anchors))
    assert anchors[0] > 0
    assert anchors[-1] > anchors[0]


def test_auto_dive_camera_look_steers_slightly_away_from_side_wall():
    component_cells = [
        (x, z)
        for x in range(12)
        for z in range(5)
    ]
    route_cells = tuple((x, 0) for x in range(12))
    route_points = tuple(
        (float(x) + 0.5, 1.0, 0.5)
        for x, _z in route_cells
    )

    plan = build_centerline_auto_dive_plan(
        _manifest_with_cached_route(
            component_cells=component_cells,
            route_cells=route_cells,
            route_points=route_points,
        ),
        current_position=route_points[0],
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=0,
            lookahead_distance_m=4.0,
        ),
    )

    assert 0.0 < plan.route.keyframes[0].yaw_deg <= 20.0
    assert plan.route.keyframes[0].pitch_deg == pytest.approx(0.0)


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
    assert set(plan.route_points_xz) <= raw_route_xz
    assert any(
        point in plan.route_points_xz
        for point in ((6.5, 0.5), (6.5, 1.5))
    )
    assert any(
        point in plan.route_points_xz
        for point in ((6.5, 5.5), (6.5, 6.5))
    )
    assert not any(
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


def _manifest_with_cached_wall_hugging_route():
    component_cells = [
        (x, z)
        for x in range(28)
        for z in range(5)
    ]
    route_cells = (
        *[(x, 2) for x in range(3, 8)],
        *[(x, 0) for x in range(8, 16)],
        *[(x, 2) for x in range(16, 25)],
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
    manifest["navigation"]["routes"][0]["clearance_margins"] = [
        1.0 for _cell in route_cells
    ]
    return manifest, route_points


def _manifest_with_cached_mesh_wall_route():
    component_cells = [
        (x, z)
        for x in range(12)
        for z in range(6)
    ]
    route_cells = (
        (2, 2),
        (2, 1),
        (2, 0),
        (3, 0),
        (4, 0),
        (5, 0),
        (6, 0),
        (7, 0),
        (8, 0),
        (8, 1),
        (8, 2),
        (8, 3),
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
    manifest["chunk_size"] = 16.0
    manifest["chunks"] = {
        "0_0_0": {
            "bounds_min": [0.0, 0.0, 0.0],
            "bounds_max": [12.0, 2.0, 6.0],
        }
    }
    manifest["navigation"]["routes"][0]["clearance_margins"] = [
        1.0 for _cell in route_cells
    ]
    return manifest, route_points


def _manifest_with_cached_mesh_blocked_route():
    component_cells = [
        (x, z)
        for x in range(12)
        for z in range(6)
    ]
    route_cells = (
        (2, 2),
        (8, 2),
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
    manifest["chunk_size"] = 16.0
    manifest["chunks"] = {
        "0_0_0": {
            "bounds_min": [0.0, 0.0, 0.0],
            "bounds_max": [12.0, 2.0, 6.0],
        }
    }
    return manifest, route_points


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


def _write_test_chunk_mesh(cache_dir, *, cell, triangles) -> None:
    chunks_dir = cache_dir / chunk_io.CHUNKS_DIRNAME
    chunks_dir.mkdir(parents=True)
    path = chunks_dir / f"{cell[0]}_{cell[1]}_{cell[2]}.bin"
    positions = np.asarray(triangles, dtype=np.float32).reshape(-1, 3)
    uvs = np.zeros((len(positions), 2), dtype=np.float32)
    normals = np.tile(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), (len(positions), 1))
    name = b"wall"
    with path.open("wb") as output:
        output.write(chunk_io._MAGIC)
        output.write(chunk_io._VERSION.to_bytes(4, "little"))
        output.write((1).to_bytes(4, "little"))
        output.write(len(name).to_bytes(4, "little"))
        output.write(name)
        output.write(len(positions).to_bytes(4, "little"))
        output.write(positions.tobytes())
        output.write(uvs.tobytes())
        output.write(normals.tobytes())
