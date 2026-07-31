"""Tests for user-facing centerline Guided Dive planning."""

from __future__ import annotations

import copy
import math
from types import SimpleNamespace

import numpy as np
import pytest

import caveviewer.core.navigation.autodive as autodive
from caveviewer.core.chunking import io as chunk_io
from caveviewer.core.navigation.autodive import (
    AUTO_DIVE_PREFLIGHT_INDETERMINATE,
    AUTO_DIVE_PREFLIGHT_READY,
    NavigationVoxelGraphAuthorityError,
    AutoDivePlanningBudgetExceeded,
    AutoDivePreflightResult,
    DEFAULT_AUTO_DIVE_SPEED_M_PER_SECOND,
    DEFAULT_AUTO_DIVE_VERTICAL_POSITION_FRACTION,
    _AutoDivePlanningBudget,
    _AutoDiveCollisionValidator,
    _AutoDiveSelectedRoute,
    AutoDiveRouteSegment,
    AutoDiveSettings,
    _AutoDiveRouteSamples,
    _auto_dive_points_for_waypoint_cells,
    _build_hemisphere_probe_route_candidate,
    _build_bounded_local_frontier_graph_route,
    _centerline_cells_form_closed_loop,
    _cone_chain_anchor_indices,
    _mesh_clear_recovery_footprint_path,
    _mesh_recovery_edge_is_clear,
    _open_arc_from_closed_loop,
    _repelled_auto_dive_points,
    _mesh_recovery_scan_alignment,
    _mesh_recovery_turn_angle,
    _mesh_recovery_view_alignment,
    _preflight_mesh_safe_graph_frontier,
    _route_segment_stays_in_footprint,
    build_auto_dive_preflight_plan,
    build_auto_dive_initial_camera_pose,
    build_centerline_auto_dive_plan,
    build_voxel_graph_auto_dive_plan,
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
from caveviewer.core.navigation.voxel_volume import LocalVoxelVolume
from caveviewer.core.navigation.voxel_cache import NavigationVoxelAtlas
from caveviewer.core.navigation.voxel_graph_3d import (
    NAVIGATION_MESH_3D_GRAPH_METHOD,
    NavigationVoxel3DMetric,
    NavigationVoxel3DEdge,
    NavigationVoxel3DGraph,
    NavigationVoxel3DNode,
    build_navigation_voxel_3d_graph,
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


def test_mesh_graph_reports_when_its_known_terminal_is_disconnected():
    nodes = {
        key: NavigationVoxel3DNode(
            key=key,
            center=tuple(float(value) for value in key),
            footprint_cell=(int(key[0]), int(key[2])),
            component_id=component_id,
            progress_m=float(key[0]),
            connectivity_score=1.0,
            local_degree=1,
            dead_end=terminal,
            terminal=terminal,
            unknown_boundary=False,
            available_volume_m3=1.0,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
        )
        for key, component_id, terminal in (
            ((0, 0, 0), 0, False),
            ((1, 0, 0), 0, False),
            ((10, 0, 0), 1, True),
        )
    }
    graph = NavigationVoxel3DGraph(
        nodes=nodes,
        edges={key: () for key in nodes},
        component_count=2,
        grid_size_m=(2.0, 1.0, 2.0),
        max_edge_distance_cells=8,
        max_edges_per_node=24,
        max_edge_distance_m=16.0,
        max_vertical_edge_distance_m=8.0,
        method=NAVIGATION_MESH_3D_GRAPH_METHOD,
    )

    terminal, details = autodive._preflight_select_graph_terminal(
        graph,
        start_key=(0, 0, 0),
        component_id=0,
        selection_policy=autodive.AUTO_DIVE_ROUTE_GOAL_EASIEST_TERMINAL,
    )

    assert terminal is None
    assert details["reason"] == "mesh_graph_terminal_disconnected_from_camera"
    assert details["disconnected_known_terminal_count"] == 1
    assert details["disconnected_terminal_component_ids"] == [1]


def test_mesh_graph_start_tolerance_covers_one_compact_path_ingress():
    graph = NavigationVoxel3DGraph(
        nodes={},
        edges={},
        component_count=0,
        grid_size_m=(2.0, 2.0, 2.0),
        max_edge_distance_cells=1,
        max_edges_per_node=26,
        max_edge_distance_m=math.sqrt(12.0),
        max_vertical_edge_distance_m=2.0,
        method=NAVIGATION_MESH_3D_GRAPH_METHOD,
    )
    atlas = NavigationVoxelAtlas(tiles=())

    tolerance_m = autodive._preflight_graph_snap_tolerance_m(
        graph,
        cached_volume=atlas,
    )

    assert tolerance_m == pytest.approx(math.sqrt(12.0) + math.sqrt(3.0))
    assert tolerance_m > 4.27583027524432


def test_voxel_graph_start_tolerance_does_not_gain_mesh_path_ingress():
    graph = NavigationVoxel3DGraph(
        nodes={},
        edges={},
        component_count=0,
        grid_size_m=(2.0, 2.0, 2.0),
        max_edge_distance_cells=1,
        max_edges_per_node=26,
        max_edge_distance_m=math.sqrt(12.0),
        max_vertical_edge_distance_m=2.0,
    )
    atlas = NavigationVoxelAtlas(tiles=())

    tolerance_m = autodive._preflight_graph_snap_tolerance_m(
        graph,
        cached_volume=atlas,
    )

    assert tolerance_m == pytest.approx(4.0)


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


@pytest.mark.parametrize(
    "route_goal",
    [
        None,
        autodive.AUTO_DIVE_ROUTE_GOAL_FARTHEST_TERMINAL,
    ],
)
def test_auto_dive_preflight_validates_graph_terminal_policy(
    monkeypatch,
    tmp_path,
    route_goal,
):
    manifest = _manifest_with_cached_route(
        component_cells=((0, 0), (1, 0), (2, 0)),
        route_cells=((0, 0), (1, 0), (2, 0)),
        route_points=(
            (0.5, 1.0, 0.5),
            (1.5, 1.0, 0.5),
            (2.5, 1.0, 0.5),
        ),
    )
    short_route = copy.deepcopy(manifest["navigation"]["routes"][0])
    short_route["id"] = "short"
    short_route["length_m"] = 1.0
    short_route["cells"] = [0, 0, 1, 0]
    short_route["points"] = [0.5, 1.0, 0.5, 1.5, 1.0, 0.5]
    long_route = copy.deepcopy(manifest["navigation"]["routes"][0])
    long_route["id"] = "long"
    long_route["length_m"] = 2.0
    manifest["navigation"]["recommended_route_id"] = "short"
    manifest["navigation"]["routes"] = [short_route, long_route]
    manifest["navigation"]["navigation_start"] = {
        "position": [0.5, 1.0, 0.5],
    }

    centerline_path = cached_centerline_path(manifest, route_id="long")
    assert centerline_path is not None
    metrics = {
        (x, 0, 0): NavigationVoxel3DMetric(
            center=(x + 0.5, 1.0, 0.5),
            footprint_cell=(x, 0),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(x),
        )
        for x in range(3)
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        max_edge_distance_cells=1,
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    captured = {}

    def fake_authority(manifest_value, *, cache_dir, settings, diagnostics, route_id):
        del manifest_value, cache_dir, settings, diagnostics
        captured["route_id"] = route_id
        return atlas, {"route_id": route_id}

    class NoHitMeshGuard:
        def segment_collision(self, first, second):
            del first, second
            return None

    monkeypatch.setattr(
        autodive,
        "_authoritative_graph_navigation_context",
        fake_authority,
    )
    monkeypatch.setattr(
        autodive.CachedChunkMeshCollisionGuard,
        "from_manifest",
        classmethod(
            lambda cls, manifest_value, *, cache_dir: NoHitMeshGuard()
        ),
    )

    settings_kwargs = {
        "speed_m_per_second": 1.0,
        "max_keyframes": 16,
        "smoothing_radius_cells": 0,
    }
    if route_goal is not None:
        settings_kwargs["route_goal"] = route_goal
    result = build_auto_dive_preflight_plan(
        manifest,
        current_position=(0.5, 1.0, 0.5),
        # A backward-facing camera must not prevent the graph-wide terminal
        # validation from finding the complete passage.
        current_yaw=np.pi,
        current_pitch=0.0,
        settings=AutoDiveSettings(**settings_kwargs),
        cache_dir=str(tmp_path),
    )

    assert isinstance(result, AutoDivePreflightResult)
    assert result.status == AUTO_DIVE_PREFLIGHT_READY
    assert result.ready is True
    assert captured["route_id"] == "long"
    assert result.navigation_route_id == "long"
    assert result.start_graph_key == (0, 0, 0)
    assert result.terminal_graph_key == (2, 0, 0)
    assert result.terminal_point == pytest.approx((2.5, 1.0, 0.5))
    expected_easiest = route_goal is None
    assert result.details["route_goal"] == (
        autodive.DEFAULT_AUTO_DIVE_ROUTE_GOAL
        if expected_easiest
        else route_goal
    )
    assert result.details["terminal_rule"] == (
        "easiest_reachable_true_3d_graph_terminal"
        if expected_easiest
        else "farthest_reachable_true_3d_graph_terminal"
    )
    assert result.details["terminal_selection_source"] == "graph_terminal"
    assert result.details["terminal_graph_distance_m"] == pytest.approx(2.0)
    assert "terminal_snap_distance_m" not in result.details
    assert result.plan is not None
    assert result.plan.preflight_validated is True
    assert result.plan.selection_reason == (
        "preflight_easiest_mesh_safe_graph_terminal"
        if expected_easiest
        else "preflight_farthest_graph_terminal_true_3d"
    )
    assert result.reason == (
        "validated_easiest_mesh_safe_graph_terminal_route"
        if expected_easiest
        else "validated_farthest_graph_terminal_route"
    )
    assert result.plan.fixed_route is expected_easiest
    assert result.plan.route_points[-1] == pytest.approx((2.5, 1.0, 0.5))
    assert all(
        keyframe.roll_deg == pytest.approx(0.0)
        for keyframe in result.plan.route.keyframes
    )


def test_easiest_terminal_policy_prefers_shortest_mesh_safe_known_terminal():
    start_key = (0, 0, 0)
    short_terminal_key = (1, 0, 0)
    long_middle_key = (0, 0, 1)
    long_terminal_key = (0, 0, 2)
    keys = (
        start_key,
        short_terminal_key,
        long_middle_key,
        long_terminal_key,
    )
    nodes = {
        start_key: NavigationVoxel3DNode(
            key=start_key,
            center=(0.0, 0.0, 0.0),
            footprint_cell=(0, 0),
            component_id=0,
            progress_m=0.0,
            connectivity_score=2.0,
            local_degree=2,
            dead_end=False,
            terminal=False,
            unknown_boundary=False,
            available_volume_m3=10.0,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
        ),
        short_terminal_key: NavigationVoxel3DNode(
            key=short_terminal_key,
            center=(1.0, 0.0, 0.0),
            footprint_cell=(1, 0),
            component_id=0,
            progress_m=1.0,
            connectivity_score=1.0,
            local_degree=1,
            dead_end=True,
            terminal=True,
            unknown_boundary=False,
            available_volume_m3=5.0,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
        ),
        long_middle_key: NavigationVoxel3DNode(
            key=long_middle_key,
            center=(0.0, 0.0, 1.0),
            footprint_cell=(0, 1),
            component_id=0,
            progress_m=1.0,
            connectivity_score=2.0,
            local_degree=2,
            dead_end=False,
            terminal=False,
            unknown_boundary=False,
            available_volume_m3=10.0,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
        ),
        long_terminal_key: NavigationVoxel3DNode(
            key=long_terminal_key,
            center=(0.0, 0.0, 2.0),
            footprint_cell=(0, 2),
            component_id=0,
            progress_m=2.0,
            connectivity_score=1.0,
            local_degree=1,
            dead_end=True,
            terminal=True,
            unknown_boundary=False,
            available_volume_m3=8.0,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
        ),
    }

    def make_edge(source, target):
        delta = np.asarray(target, dtype=np.float64) - np.asarray(
            source,
            dtype=np.float64,
        )
        distance = float(np.linalg.norm(delta))
        return NavigationVoxel3DEdge(
            source=source,
            target=target,
            distance_m=distance,
            direction=tuple(float(value) for value in (delta / distance)),
            min_clearance_m=1.0,
        )

    directed_edges = (
        (start_key, short_terminal_key),
        (start_key, long_middle_key),
        (long_middle_key, long_terminal_key),
    )
    graph = NavigationVoxel3DGraph(
        nodes=nodes,
        edges={
            key: tuple(
                make_edge(source, target)
                for source, target in directed_edges
                if source == key
            )
            for key in keys
        },
        component_count=1,
        grid_size_m=(1.0, 1.0, 1.0),
        max_edge_distance_cells=4,
        max_edges_per_node=4,
        max_edge_distance_m=4.0,
        max_vertical_edge_distance_m=4.0,
    )

    selected, details = autodive._preflight_select_graph_terminal(
        graph,
        start_key=start_key,
        component_id=0,
        selection_policy=autodive.AUTO_DIVE_ROUTE_GOAL_EASIEST_TERMINAL,
    )
    assert selected == short_terminal_key
    assert details["terminal_graph_distance_m"] == pytest.approx(1.0)

    class SelectiveGraphSafety:
        def edge_clearance_failure(self, source, target):
            return object() if target == short_terminal_key else None

    path, selected, safe_details = _preflight_mesh_safe_graph_frontier(
        graph,
        start_key=start_key,
        component_id=0,
        graph_safety_validator=SelectiveGraphSafety(),
        selection_policy=autodive.AUTO_DIVE_ROUTE_GOAL_EASIEST_TERMINAL,
    )
    assert path == (start_key, long_middle_key, long_terminal_key)
    assert selected == long_terminal_key
    assert safe_details["terminal_candidate"] is True
    assert safe_details["mesh_safe_prefix_fallback"] is False
    assert safe_details["terminal_selection_source"] == "mesh_safe_graph_terminal"


def test_auto_dive_preflight_uses_mesh_safe_frontier_when_longest_edge_is_blocked(
    monkeypatch,
):
    manifest = _manifest_with_cached_route(
        component_cells=(
            (0, 0),
            (1, 0),
            (2, 0),
            (3, 0),
            (4, 0),
            (0, 1),
            (1, 1),
            (2, 1),
        ),
        route_cells=((0, 0), (1, 0), (2, 0), (3, 0), (4, 0)),
        route_points=tuple(
            (float(x), 0.0, 0.0)
            for x in range(5)
        ),
    )

    keys = (
        (0, 0, 0),
        (1, 0, 0),
        (2, 0, 0),
        (3, 0, 0),
        (4, 0, 0),
        (0, 1, 0),
        (1, 1, 0),
        (2, 1, 0),
    )
    terminal_key = (4, 0, 0)
    safe_frontier_key = (2, 1, 0)
    nodes = {
        key: NavigationVoxel3DNode(
            key=key,
            center=tuple(float(value) for value in key),
            footprint_cell=(int(key[0]), int(key[2])),
            component_id=0,
            progress_m=float(key[0]),
            connectivity_score=1.0,
            local_degree=1,
            dead_end=False,
            terminal=key == terminal_key,
            unknown_boundary=key == safe_frontier_key,
            available_volume_m3=10.0,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
        )
        for key in keys
    }

    def make_edge(source, target):
        delta = np.asarray(target, dtype=np.float64) - np.asarray(
            source,
            dtype=np.float64,
        )
        distance = float(np.linalg.norm(delta))
        return NavigationVoxel3DEdge(
            source=source,
            target=target,
            distance_m=distance,
            direction=tuple(float(value) for value in (delta / distance)),
            min_clearance_m=1.0,
        )

    directed_edges = (
        ((0, 0, 0), (1, 0, 0)),
        ((1, 0, 0), (2, 0, 0)),
        ((2, 0, 0), (3, 0, 0)),
        ((3, 0, 0), (4, 0, 0)),
        ((0, 0, 0), (0, 1, 0)),
        ((0, 1, 0), (1, 1, 0)),
        ((1, 1, 0), (2, 1, 0)),
    )
    graph = NavigationVoxel3DGraph(
        nodes=nodes,
        edges={
            key: tuple(
                make_edge(source, target)
                for source, target in directed_edges
                if source == key
            )
            for key in keys
        },
        component_count=1,
        grid_size_m=(1.0, 1.0, 1.0),
        max_edge_distance_cells=4,
        max_edges_per_node=8,
        max_edge_distance_m=4.0,
        max_vertical_edge_distance_m=4.0,
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)

    monkeypatch.setattr(
        autodive,
        "_authoritative_graph_navigation_context",
        lambda manifest_value, **kwargs: (
            atlas,
            {"route_id": "cached-main"},
        ),
    )

    class SelectiveMeshGuard:
        def segment_collision(self, first, second):
            if tuple(first) == (3.0, 0.0, 0.0) and tuple(second) == (
                4.0,
                0.0,
                0.0,
            ):
                return SimpleNamespace(point=(3.5, 0.0, 0.0))
            return None

    monkeypatch.setattr(
        autodive.CachedChunkMeshCollisionGuard,
        "from_manifest",
        classmethod(
            lambda cls, manifest_value, *, cache_dir: SelectiveMeshGuard()
        ),
    )

    result = build_auto_dive_preflight_plan(
        manifest,
        current_position=(0.0, 0.0, 0.0),
        current_yaw=0.0,
        current_pitch=0.0,
        settings=AutoDiveSettings(
            smoothing_radius_cells=0,
            route_goal=autodive.AUTO_DIVE_ROUTE_GOAL_FARTHEST_TERMINAL,
        ),
        cache_dir="/cache/devils-eye",
    )

    assert result.status == AUTO_DIVE_PREFLIGHT_READY
    assert result.reason == "validated_mesh_safe_graph_frontier_route"
    assert result.start_graph_key == (0, 0, 0)
    assert result.terminal_graph_key == safe_frontier_key
    assert result.terminal_point == pytest.approx((2.0, 1.0, 0.0))
    assert result.coverage_incomplete is True
    assert result.details["requested_terminal_graph_key"] == [4, 0, 0]
    assert result.details["mesh_safe_frontier_fallback"] is True
    assert result.plan is not None
    assert result.plan.selection_reason == "preflight_mesh_safe_graph_frontier"
    assert result.plan.replan_at_end is True
    assert result.plan.terminal_reached is False
    assert np.allclose(
        result.plan.route_points,
        (
            (0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (1.0, 1.0, 0.0),
            (2.0, 1.0, 0.0),
        ),
    )


def test_easiest_preflight_fails_closed_without_a_refined_portal(monkeypatch):
    """A blocked complete route must not become a short frontier in easiest mode."""
    manifest = _manifest_with_cached_route(
        component_cells=((0, 0), (1, 0), (2, 0)),
        route_cells=((0, 0), (1, 0), (2, 0)),
        route_points=(
            (0.5, 0.5, 0.5),
            (1.5, 0.5, 0.5),
            (2.5, 0.5, 0.5),
        ),
    )
    metrics = {
        (index, 0, 0): NavigationVoxel3DMetric(
            center=(float(index) + 0.5, 0.5, 0.5),
            footprint_cell=(index, 0),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(index),
        )
        for index in range(3)
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        max_edge_distance_cells=1,
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    monkeypatch.setattr(
        autodive,
        "_authoritative_graph_navigation_context",
        lambda *_args, **_kwargs: (atlas, {"route_id": "cached-main"}),
    )

    class BlockedTerminalMeshGuard:
        def segment_collision(self, first, second):
            if tuple(first) == (1.5, 0.5, 0.5) and tuple(second) == (
                2.5,
                0.5,
                0.5,
            ):
                return SimpleNamespace(point=(2.0, 0.5, 0.5))
            return None

    monkeypatch.setattr(
        autodive.CachedChunkMeshCollisionGuard,
        "from_manifest",
        classmethod(lambda cls, *_args, **_kwargs: BlockedTerminalMeshGuard()),
    )
    monkeypatch.setattr(
        autodive,
        "_preflight_mesh_safe_graph_frontier",
        lambda *_args, **_kwargs: pytest.fail(
            "easiest mode must not replace a failed terminal with a frontier"
        ),
    )

    result = build_auto_dive_preflight_plan(
        manifest,
        current_position=(0.5, 0.5, 0.5),
        settings=AutoDiveSettings(smoothing_radius_cells=0),
        cache_dir="/cache/devils-eye",
    )

    assert result.status != AUTO_DIVE_PREFLIGHT_READY
    assert result.reason == "route_collision:mesh_intersection"
    assert result.plan is None
    refinement = result.details["refined_route_composition"]
    assert refinement["reason"] == "spine_portal_unrecoverable"
    assert refinement["refinement"]["reason"] == (
        "spine_portal_fine_coverage_missing"
    )


def test_easiest_preflight_publishes_only_a_composed_fixed_terminal_route(
    monkeypatch,
):
    """A successful local repair retains the original known terminal."""
    manifest = _manifest_with_cached_route(
        component_cells=((0, 0), (1, 0), (2, 0)),
        route_cells=((0, 0), (1, 0), (2, 0)),
        route_points=(
            (0.5, 0.5, 0.5),
            (1.5, 0.5, 0.5),
            (2.5, 0.5, 0.5),
        ),
    )
    metrics = {
        (index, 0, 0): NavigationVoxel3DMetric(
            center=(float(index) + 0.5, 0.5, 0.5),
            footprint_cell=(index, 0),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(index),
        )
        for index in range(3)
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        max_edge_distance_cells=1,
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    monkeypatch.setattr(
        autodive,
        "_authoritative_graph_navigation_context",
        lambda *_args, **_kwargs: (atlas, {"route_id": "cached-main"}),
    )

    class BlockedTerminalMeshGuard:
        def segment_collision(self, first, second):
            if tuple(first) == (1.5, 0.5, 0.5) and tuple(second) == (
                2.5,
                0.5,
                0.5,
            ):
                return SimpleNamespace(point=(2.0, 0.5, 0.5))
            return None

    monkeypatch.setattr(
        autodive.CachedChunkMeshCollisionGuard,
        "from_manifest",
        classmethod(lambda cls, *_args, **_kwargs: BlockedTerminalMeshGuard()),
    )
    repaired_points = (
        (0.5, 0.5, 0.5),
        (1.5, 0.5, 0.5),
        (1.5, 0.5, 1.5),
        (2.5, 0.5, 0.5),
    )
    repaired_segment = AutoDiveRouteSegment(
        route_points=repaired_points,
        route_cells=((0, 0), (1, 0), (1, 1), (2, 0)),
        source="refined_fine_2m_graph",
        details={"mesh_safe": True},
    )
    monkeypatch.setattr(
        autodive,
        "_compose_preflight_spine_fixed_route",
        lambda **_kwargs: (
            (repaired_segment,),
            repaired_points,
            repaired_segment.route_cells,
            {"reason": "spine_fixed_route_ready", "portal_handoff_count": 1},
        ),
    )

    result = build_auto_dive_preflight_plan(
        manifest,
        current_position=(0.5, 0.5, 0.5),
        settings=AutoDiveSettings(smoothing_radius_cells=0),
        cache_dir="/cache/devils-eye",
    )

    assert result.status == AUTO_DIVE_PREFLIGHT_READY
    assert result.plan is not None
    assert result.plan.fixed_route is True
    assert result.plan.terminal_reached is True
    assert result.plan.replan_at_end is False
    assert result.terminal_graph_key == (2, 0, 0)
    assert result.plan.route_points == repaired_points
    assert result.plan.route_segments == (repaired_segment,)
    assert result.plan.voxel_route_selection["route_geometry_source"] == (
        "preflight_physical_true_3d_graph_spine_with_fine_portals"
    )


def test_refined_portal_uses_persisted_1m_fallback_after_2m_fails(
    monkeypatch,
):
    """A tight passage may require the native fine-tile resolution."""
    keys = tuple((index, 0, 0) for index in range(3))
    metrics = {
        key: NavigationVoxel3DMetric(
            center=(float(key[0]) + 0.5, 0.5, 0.5),
            footprint_cell=(key[0], 0),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(key[0]),
        )
        for key in keys
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        max_edge_distance_cells=1,
    )
    volume = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(-1.0, -1.0, -1.0),
        shape=(8, 8, 8),
        surface_cells=frozenset(),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=8,
    )
    atlas = NavigationVoxelAtlas(
        tiles=(),
        fine_tiles=(volume,),
        prepared_3d_graph=graph,
    )
    current = (0.5, 0.5, 0.5)
    filled_cells = {volume.voxel_index(current): 1.0}
    calls = []

    def fake_prepare(*, grid_size_m=(2.0, 1.0, 2.0), **_kwargs):
        grid_size = tuple(float(value) for value in grid_size_m)
        calls.append(grid_size)
        return SimpleNamespace(
            volume=volume,
            filled_cells=filled_cells,
            grid_size=grid_size,
        ), {"reason": "refined_tile_graph_ready"}

    def fake_bridge_candidates(*, prepared_context, candidates, **_kwargs):
        if prepared_context.grid_size == (2.0, 1.0, 2.0):
            return None, None, None, {
                "reason": "refined_portal_bridge_candidates_exhausted",
                "portal_candidate_count": len(candidates),
                "viable_portal_candidate_count": 1,
            }
        portal_key = (1, 0, 0)
        segment = AutoDiveRouteSegment(
            route_points=(current, graph.nodes[portal_key].center),
            route_cells=(graph.nodes[portal_key].footprint_cell,),
            source="refined_fine_1m_graph",
            graph_keys=(portal_key,),
            details={"mesh_safe": True},
        )
        return segment, portal_key, (portal_key, (2, 0, 0)), {
            "reason": "refined_portal_selected",
            "refinement_grid_size_m": [1.0, 1.0, 1.0],
        }

    monkeypatch.setattr(
        autodive,
        "_prepare_preflight_refined_tile",
        fake_prepare,
    )
    monkeypatch.setattr(
        autodive,
        "_preflight_refined_portal_bridge_candidates",
        fake_bridge_candidates,
    )

    segment, portal_key, portal_path, details = (
        autodive._preflight_refined_portal_for_global_route(
            current=current,
            source_key=(0, 0, 0),
            terminal_key=(2, 0, 0),
            graph=graph,
            cached_volume=atlas,
            graph_safety_validator=object(),
            mesh_guard=object(),
            settings=AutoDiveSettings(),
            visited_portal_keys={(0, 0, 0)},
        )
    )

    assert segment is not None
    assert portal_key == (1, 0, 0)
    assert portal_path == ((1, 0, 0), (2, 0, 0))
    assert calls == [(2.0, 1.0, 2.0), (1.0, 1.0, 1.0)]
    assert details["refinement_strategy"] == "persisted_fine_1m_fallback"
    assert details["primary_2m_failure"]["reason"] == (
        "refined_portal_bridge_candidates_exhausted"
    )


def test_refined_tile_context_cache_keeps_only_recent_local_graphs(monkeypatch):
    """Failed portal probes cannot retain one graph for every fine tile."""
    contexts = {}

    def fake_prepare(*, volume, **_kwargs):
        return SimpleNamespace(volume=volume), {"reason": "ready"}

    monkeypatch.setattr(
        autodive,
        "_prepare_preflight_refined_tile",
        fake_prepare,
    )
    for index in range(3):
        volume = LocalVoxelVolume(
            voxel_size_m=1.0,
            origin=(float(index) * 10.0, 0.0, 0.0),
            shape=(2, 2, 2),
            surface_cells=frozenset(),
            triangle_count=1,
            surface_sample_count=1,
            sampling_truncated=False,
            max_clearance_search_cells=2,
        )
        context, _details = autodive._preflight_cached_refined_tile_context(
            volume=volume,
            current=(float(index) * 10.0 + 0.5, 0.5, 0.5),
            mesh_guard=object(),
            settings=AutoDiveSettings(),
            grid_size_m=(2.0, 1.0, 2.0),
            max_nodes=32,
            max_edges_per_node=4,
            context_cache=contexts,
        )
        assert context is not None

    assert len(contexts) == 2
    assert [context.volume.origin[0] for context in contexts.values()] == [
        10.0,
        20.0,
    ]


def test_fixed_spine_composition_uses_a_portal_then_resumes_the_same_terminal(
    monkeypatch,
):
    keys = tuple((index, 0, 0) for index in range(4))
    metrics = {
        key: NavigationVoxel3DMetric(
            center=(float(key[0]) + 0.5, 0.5, 0.5),
            footprint_cell=(key[0], 0),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(key[0]),
        )
        for key in keys
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        max_edge_distance_cells=1,
    )

    class SelectiveSafety:
        def edge_clearance_failure(self, source, target):
            if (source, target) == ((1, 0, 0), (2, 0, 0)):
                return SimpleNamespace(diagnostic_payload=lambda: {"reason": "mesh"})
            return None

        def route_clearance_failure(self, *_args, **_kwargs):
            return None

    calls = []

    def fake_portal(*, source_key, source_index, graph_keys, **_kwargs):
        current = graph.nodes[source_key].center
        calls.append((source_key, source_index, tuple(graph_keys)))
        assert source_key == (1, 0, 0)
        assert source_index == 1
        assert tuple(graph_keys) == keys
        segment = AutoDiveRouteSegment(
            route_points=(
                current,
                (2.0, 0.5, 1.0),
                (2.5, 0.5, 0.5),
            ),
            route_cells=((1, 0), (2, 1), (2, 0)),
            source="refined_fine_2m_graph",
            details={"mesh_safe": True},
        )
        return segment, 2, {
            "reason": "spine_portal_selected"
        }

    monkeypatch.setattr(
        autodive,
        "_preflight_refined_spine_portal",
        fake_portal,
    )
    segments, points, _cells, details = (
        autodive._compose_preflight_spine_fixed_route(
            current=(0.5, 0.5, 0.5),
            graph_keys=keys,
            graph=graph,
            cached_volume=object(),
            graph_safety_validator=SelectiveSafety(),
            mesh_guard=object(),
            settings=AutoDiveSettings(max_keyframes=32),
        )
    )

    assert segments is not None
    assert [segment.source for segment in segments] == [
        "prepared_global_graph",
        "refined_fine_2m_graph",
        "prepared_global_graph",
    ]
    assert calls == [((1, 0, 0), 1, keys)]
    assert points[0] == pytest.approx((0.5, 0.5, 0.5))
    assert points[-1] == pytest.approx((3.5, 0.5, 0.5))
    assert details["portal_handoff_count"] == 1


def test_fixed_spine_composition_never_globally_replans_a_mesh_block(
    monkeypatch,
):
    source_key = (0, 0, 0)
    blocked_key = (1, 0, 0)
    terminal_key = (2, 0, 0)
    keys = (source_key, blocked_key, terminal_key, (0, 0, 1), (1, 0, 1))
    metrics = {
        key: NavigationVoxel3DMetric(
            center=(float(key[0]) + 0.5, 0.5, float(key[2]) + 0.5),
            footprint_cell=(key[0], key[2]),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(key[0]),
        )
        for key in keys
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        max_edge_distance_cells=1,
    )

    class SelectiveSafety:
        def edge_clearance_failure(self, source, target):
            if (source, target) == (source_key, blocked_key):
                return SimpleNamespace(
                    reason="mesh_intersection",
                    diagnostic_payload=lambda: {"reason": "mesh_intersection"},
                )
            return None

        def route_clearance_failure(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(
        autodive,
        "_preflight_global_graph_route",
        lambda **_kwargs: pytest.fail(
            "fixed-spine composition must not replace its route suffix"
        ),
    )

    def fake_portal(*, source_key, source_index, graph_keys, **_kwargs):
        assert source_key == (0, 0, 0)
        assert source_index == 0
        assert tuple(graph_keys) == (source_key, blocked_key, terminal_key)
        return (
            AutoDiveRouteSegment(
                route_points=(
                    (0.5, 0.5, 0.5),
                    (1.0, 0.5, 1.0),
                    (1.5, 0.5, 0.5),
                ),
                route_cells=((0, 0), (1, 1), (1, 0)),
                source="refined_fine_2m_graph",
                details={"mesh_safe": True},
            ),
            1,
            {"reason": "spine_portal_selected"},
        )

    monkeypatch.setattr(
        autodive,
        "_preflight_refined_spine_portal",
        fake_portal,
    )
    segments, points, _cells, details = (
        autodive._compose_preflight_spine_fixed_route(
            current=(0.5, 0.5, 0.5),
            graph_keys=(source_key, blocked_key, terminal_key),
            graph=graph,
            cached_volume=object(),
            graph_safety_validator=SelectiveSafety(),
            mesh_guard=object(),
            settings=AutoDiveSettings(max_keyframes=32),
        )
    )

    assert segments is not None
    assert [segment.source for segment in segments] == [
        "refined_fine_2m_graph",
        "prepared_global_graph",
    ]
    assert points[0] == pytest.approx((0.5, 0.5, 0.5))
    assert points[-1] == pytest.approx((2.5, 0.5, 0.5))
    assert details["portal_handoff_count"] == 1
    assert "global_mesh_replan_count" not in details


def test_mesh_safe_preflight_keeps_prefix_when_no_endpoint_is_reachable():
    metrics = {
        key: NavigationVoxel3DMetric(
            center=(float(key[0]), 0.0, 0.0),
            footprint_cell=(int(key[0]), 0),
            available_volume_m3=1.0,
            free_voxel_count=1,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(key[0]),
        )
        for key in ((0, 0, 0), (1, 0, 0), (2, 0, 0))
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )

    class SelectiveGraphSafety:
        def edge_clearance_failure(self, source, target):
            return (
                object()
                if target == (2, 0, 0)
                else None
            )

    path, selected, details = _preflight_mesh_safe_graph_frontier(
        graph,
        start_key=(0, 0, 0),
        component_id=0,
        graph_safety_validator=SelectiveGraphSafety(),
    )

    assert path == ((0, 0, 0), (1, 0, 0))
    assert selected == (1, 0, 0)
    assert details["mesh_safe_prefix_fallback"] is True
    assert details["terminal_reachable_candidate_count"] == 0

    easiest_path, easiest_selected, easiest_details = (
        _preflight_mesh_safe_graph_frontier(
            graph,
            start_key=(0, 0, 0),
            component_id=0,
            graph_safety_validator=SelectiveGraphSafety(),
            selection_policy=autodive.AUTO_DIVE_ROUTE_GOAL_EASIEST_TERMINAL,
        )
    )
    assert easiest_path is None
    assert easiest_selected is None
    assert easiest_details["mesh_safe_frontier_reason"] == (
        "no_mesh_safe_graph_terminal_reachable"
    )


def test_auto_dive_preflight_uses_farthest_voxel_frontier_not_centerline_endpoint(
    monkeypatch,
):
    component_cells = (
        (0, 0),
        (1, 0),
        (2, 0),
        (1, 1),
        (1, 2),
        (1, 3),
        (1, 4),
    )
    manifest = _manifest_with_cached_route(
        component_cells=component_cells,
        route_cells=((0, 0), (1, 0), (2, 0)),
        route_points=(
            (0.5, 1.0, 0.5),
            (1.5, 1.0, 0.5),
            (2.5, 1.0, 0.5),
        ),
    )
    centerline_path = cached_centerline_path(manifest)
    assert centerline_path is not None
    metrics = {
        (x, 0, z): NavigationVoxel3DMetric(
            center=(x + 0.5, 1.0, z + 0.5),
            footprint_cell=(x, z),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(x + z),
        )
        for x, z in component_cells
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        # This models a prepared cache whose sampled boundary is incomplete.
        unknown_boundary=set(metrics),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)

    monkeypatch.setattr(
        autodive,
        "_authoritative_graph_navigation_context",
        lambda manifest_value, **kwargs: (
            atlas,
            {"route_id": "cached-main"},
        ),
    )

    class NoHitMeshGuard:
        def segment_collision(self, first, second):
            del first, second
            return None

    monkeypatch.setattr(
        autodive.CachedChunkMeshCollisionGuard,
        "from_manifest",
        classmethod(
            lambda cls, manifest_value, *, cache_dir: NoHitMeshGuard()
        ),
    )

    result = build_auto_dive_preflight_plan(
        manifest,
        current_position=(0.5, 1.0, 0.5),
        settings=AutoDiveSettings(
            max_keyframes=32,
            smoothing_radius_cells=0,
            route_goal=autodive.AUTO_DIVE_ROUTE_GOAL_FARTHEST_TERMINAL,
        ),
        cache_dir="/cache/devils-eye",
    )

    assert result.status == AUTO_DIVE_PREFLIGHT_READY
    assert result.terminal_graph_key == (1, 0, 4)
    assert result.terminal_point == pytest.approx((1.5, 1.0, 4.5))
    assert result.details["terminal_selection_source"] == (
        "unknown_boundary_frontier"
    )
    assert result.coverage_incomplete is True
    assert result.plan is not None
    assert result.plan.route_points[-1] == pytest.approx(
        (1.5, 1.0, 4.5)
    )


def test_auto_dive_preflight_does_not_fallback_to_centerline_without_graph_endpoint(
    monkeypatch,
):
    component_cells = ((0, 0), (1, 0), (1, 1), (0, 1))
    manifest = _manifest_with_cached_route(
        component_cells=component_cells,
        route_cells=component_cells,
        route_points=tuple(
            (float(x) + 0.5, 1.0, float(z) + 0.5)
            for x, z in component_cells
        ),
    )
    centerline_path = cached_centerline_path(manifest)
    assert centerline_path is not None
    metrics = {
        (x, 0, z): NavigationVoxel3DMetric(
            center=(x + 0.5, 1.0, z + 0.5),
            footprint_cell=(x, z),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(x + z),
        )
        for x, z in component_cells
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    monkeypatch.setattr(
        autodive,
        "_authoritative_graph_navigation_context",
        lambda manifest_value, **kwargs: (
            atlas,
            {"route_id": "cached-main"},
        ),
    )

    result = build_auto_dive_preflight_plan(
        manifest,
        current_position=(0.5, 1.0, 0.5),
        settings=AutoDiveSettings(
            smoothing_radius_cells=0,
            route_goal=autodive.AUTO_DIVE_ROUTE_GOAL_FARTHEST_TERMINAL,
        ),
        cache_dir="/cache/devils-eye",
    )

    assert result.status == AUTO_DIVE_PREFLIGHT_INDETERMINATE
    assert result.reason == "prepared_graph_terminal_candidates_missing"
    assert result.terminal_graph_key is None
    assert result.terminal_point is None
    assert result.plan is None


def test_auto_dive_preflight_is_indeterminate_without_mesh_validation(
    monkeypatch,
):
    manifest = _manifest_with_cached_navigation_route()
    centerline_path = cached_centerline_path(manifest)
    assert centerline_path is not None
    metrics = {
        (x, 0, 0): NavigationVoxel3DMetric(
            center=(x + 20.5, 1.0, 0.5),
            footprint_cell=(x + 20, 0),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(x),
        )
        for x in range(3)
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    monkeypatch.setattr(
        autodive,
        "_authoritative_graph_navigation_context",
        lambda manifest_value, **kwargs: (
            atlas,
            {"route_id": "cached-main"},
        ),
    )
    monkeypatch.setattr(
        autodive.CachedChunkMeshCollisionGuard,
        "from_manifest",
        classmethod(lambda cls, manifest_value, *, cache_dir: None),
    )

    result = build_auto_dive_preflight_plan(
        manifest,
        current_position=(20.5, 1.5, 0.5),
        settings=AutoDiveSettings(smoothing_radius_cells=0),
        cache_dir="/cache/devils-eye",
    )

    assert result.status == AUTO_DIVE_PREFLIGHT_INDETERMINATE
    assert result.reason == "mesh_collision_guard_unavailable"
    assert result.plan is None


def test_auto_dive_preflight_does_not_cross_graph_components_for_terminal(
    monkeypatch,
):
    manifest = _manifest_with_cached_route(
        component_cells=((0, 0), (1, 0), (10, 0), (11, 0)),
        route_cells=((0, 0), (10, 0), (11, 0)),
        route_points=(
            (0.5, 1.0, 0.5),
            (10.5, 1.0, 0.5),
            (11.5, 1.0, 0.5),
        ),
    )
    centerline_path = cached_centerline_path(manifest)
    assert centerline_path is not None
    metrics = {
        (x, 0, 0): NavigationVoxel3DMetric(
            center=(x + 0.5, 1.0, 0.5),
            footprint_cell=(x, 0),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(x),
        )
        for x in (0, 1, 10, 11)
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    monkeypatch.setattr(
        autodive,
        "_authoritative_graph_navigation_context",
        lambda manifest_value, **kwargs: (
            atlas,
            {"route_id": "cached-main"},
        ),
    )

    result = build_auto_dive_preflight_plan(
        manifest,
        current_position=(0.5, 1.0, 0.5),
        settings=AutoDiveSettings(
            smoothing_radius_cells=0,
            route_goal=autodive.AUTO_DIVE_ROUTE_GOAL_FARTHEST_TERMINAL,
        ),
        cache_dir="/cache/devils-eye",
    )

    assert result.status == AUTO_DIVE_PREFLIGHT_READY
    assert result.reason == "validated_farthest_graph_terminal_route"
    assert result.start_graph_key == (0, 0, 0)
    assert result.terminal_graph_key == (1, 0, 0)
    assert result.terminal_point == pytest.approx((1.5, 1.0, 0.5))
    assert result.plan is not None
    assert all(point[0] < 2.0 for point in result.plan.route_points)


def test_graph_native_plan_does_not_load_or_consult_centerline_clearance(
    monkeypatch,
):
    manifest = _manifest_with_cached_route(
        component_cells=((0, 0), (1, 0), (2, 0)),
        route_cells=((0, 0), (1, 0), (2, 0)),
        route_points=(
            (0.5, 1.0, 0.5),
            (1.5, 1.0, 0.5),
            (2.5, 1.0, 0.5),
        ),
    )
    metrics = {
        (x, 0, 0): NavigationVoxel3DMetric(
            center=(x + 0.5, 1.0, 0.5),
            footprint_cell=(x, 0),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=7.16,
            mean_clearance_m=7.16,
            progress_m=float(x),
        )
        for x in range(3)
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)

    monkeypatch.setattr(
        autodive,
        "cached_centerline_path",
        lambda *args, **kwargs: pytest.fail(
            "graph-native planning must not load a centerline"
        ),
    )
    monkeypatch.setattr(
        autodive,
        "_authoritative_graph_navigation_context",
        lambda manifest_value, **kwargs: (
            atlas,
            {"route_id": "graph-native"},
        ),
    )

    class NoHitMeshGuard:
        def segment_collision(self, first, second):
            del first, second
            return None

    monkeypatch.setattr(
        autodive.CachedChunkMeshCollisionGuard,
        "from_manifest",
        classmethod(
            lambda cls, manifest_value, *, cache_dir: NoHitMeshGuard()
        ),
    )

    plan = build_voxel_graph_auto_dive_plan(
        manifest,
        current_position=(0.5, 1.0, 0.5),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            minimum_graph_clearance_m=5.0,
            smoothing_radius_cells=0,
        ),
        cache_dir="/cache/devils-eye",
    )

    assert plan.centerline_path is None
    assert plan.navigation_atlas is atlas
    assert plan.navigation_graph is graph
    assert plan.route_points[0] == pytest.approx((0.5, 1.0, 0.5))
    assert plan.route_points[-1][0] > plan.route_points[0][0]
    assert plan.voxel_route_selection is not None
    assert plan.voxel_route_selection["authority"] == (
        "prepared_true_3d_voxel_graph"
    )


def test_graph_native_runtime_plan_fails_closed_without_mesh_guard(monkeypatch):
    manifest = _manifest_with_cached_route(
        component_cells=((0, 0), (1, 0), (2, 0)),
        route_cells=((0, 0), (1, 0), (2, 0)),
        route_points=(
            (0.5, 1.0, 0.5),
            (1.5, 1.0, 0.5),
            (2.5, 1.0, 0.5),
        ),
    )
    metrics = {
        (x, 0, 0): NavigationVoxel3DMetric(
            center=(x + 0.5, 1.0, 0.5),
            footprint_cell=(x, 0),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(x),
        )
        for x in range(3)
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    events = []
    monkeypatch.setattr(
        autodive,
        "_authoritative_graph_navigation_context",
        lambda manifest_value, **kwargs: (
            atlas,
            {"route_id": "graph-native", "cache_version": "3"},
        ),
    )
    monkeypatch.setattr(
        autodive.CachedChunkMeshCollisionGuard,
        "from_manifest",
        classmethod(lambda cls, manifest_value, *, cache_dir: None),
    )

    with pytest.raises(NavigationVoxelGraphAuthorityError) as error:
        build_voxel_graph_auto_dive_plan(
            manifest,
            current_position=(0.5, 1.0, 0.5),
            settings=AutoDiveSettings(smoothing_radius_cells=0),
            cache_dir="/cache/devils-eye",
            diagnostics=lambda event, payload: events.append((event, payload)),
        )

    assert error.value.reason == "mesh_collision_guard_unavailable"
    assert any(
        event == "navigation_authority"
        and payload["reason"] == "mesh_collision_guard_unavailable"
        for event, payload in events
    )


def test_user_resume_expansion_precedes_prepared_graph_search(monkeypatch):
    manifest = _manifest_with_cached_route(
        component_cells=((0, 0), (1, 0), (2, 0)),
        route_cells=((0, 0), (1, 0), (2, 0)),
        route_points=(
            (0.5, 1.0, 0.5),
            (1.5, 1.0, 0.5),
            (2.5, 1.0, 0.5),
        ),
    )
    graph = build_navigation_voxel_3d_graph(
        {
            (0, 0, 0): NavigationVoxel3DMetric(
                center=(0.5, 0.5, 0.5),
                footprint_cell=(0, 0),
                available_volume_m3=2.0,
                free_voxel_count=2,
                min_clearance_m=1.0,
                mean_clearance_m=1.0,
                progress_m=0.0,
            ),
            (1, 0, 0): NavigationVoxel3DMetric(
                center=(1.5, 0.5, 0.5),
                footprint_cell=(1, 0),
                available_volume_m3=2.0,
                free_voxel_count=2,
                min_clearance_m=1.0,
                mean_clearance_m=1.0,
                progress_m=1.0,
            ),
        },
        grid_size_m=(1.0, 1.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    volume = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(-4.0, -4.0, -4.0),
        shape=(16, 16, 16),
        surface_cells=frozenset(),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=4,
    )

    monkeypatch.setattr(
        autodive,
        "_authoritative_graph_navigation_context",
        lambda manifest_value, **kwargs: (
            atlas,
            {"route_id": "graph-native"},
        ),
    )

    class NoHitMeshGuard:
        def segment_collision(self, first, second):
            del first, second
            return None

    monkeypatch.setattr(
        autodive.CachedChunkMeshCollisionGuard,
        "from_manifest",
        classmethod(
            lambda cls, manifest_value, *, cache_dir: NoHitMeshGuard()
        ),
    )
    monkeypatch.setattr(
        autodive,
        "_make_auto_dive_local_frontier_voxel_builder",
        lambda **kwargs: (lambda: volume),
    )
    monkeypatch.setattr(
        autodive.NavigationVoxelAtlas,
        "plan_footprint_route",
        lambda *args, **kwargs: pytest.fail(
            "frontier expansion must run before the prepared graph search"
        ),
    )

    plan = build_voxel_graph_auto_dive_plan(
        manifest,
        current_position=(0.0, 0.0, 0.0),
        current_yaw=0.0,
        current_pitch=0.0,
        current_travel_yaw=0.0,
        current_travel_pitch=0.0,
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            lookahead_distance_m=4.0,
            smoothing_radius_cells=0,
        ),
        cache_dir="/cache/devils-eye",
        user_reposition=True,
    )

    assert plan.selection_reason == "continuous_local_frontier_expansion"
    assert plan.route_length_m > 0.0
    assert plan.replan_at_end is True


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


def test_graph_initial_camera_uses_route_start_when_sidecar_start_is_missing():
    navigation = {
        "recommended_route_id": "route-main",
        "routes": [
            {
                "id": "route-main",
                "points": [
                    3.5,
                    4.0,
                    5.5,
                    4.5,
                    4.0,
                    5.5,
                ],
            }
        ],
    }

    assert autodive._navigation_start_point(navigation) == (
        3.5,
        4.0,
        5.5,
    )


def test_graph_initial_camera_pose_uses_route_start_fallback(monkeypatch):
    metrics = {
        (x, 0, 0): NavigationVoxel3DMetric(
            center=(float(x) + 0.5, 4.0, 0.5),
            footprint_cell=(x, 0),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(x),
        )
        for x in range(3)
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    manifest = {
        "navigation": {
            "recommended_route_id": "route-main",
            "routes": [
                {
                    "id": "route-main",
                    "points": [
                        0.5,
                        4.0,
                        0.5,
                        1.5,
                        4.0,
                        0.5,
                    ],
                }
            ],
        }
    }

    monkeypatch.setattr(
        autodive,
        "_authoritative_graph_navigation_context",
        lambda manifest_value, **kwargs: (
            atlas,
            {"route_id": "route-main"},
        ),
    )

    pose = build_auto_dive_initial_camera_pose(
        manifest,
        settings=AutoDiveSettings(smoothing_radius_cells=0),
        cache_dir="/cache/devils-eye",
        require_voxel_graph=True,
    )

    assert pose.position == (0.5, 4.0, 0.5)
    assert pose.yaw_deg == pytest.approx(0.0)


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


def test_auto_dive_hemisphere_recovery_scans_roll_and_vertical_offsets():
    component_cells = [(x, z) for x in range(8) for z in range(8)]
    route_cells = tuple((x, 3) for x in range(8))
    route_points = tuple(
        (float(x) + 0.5, 1.0, 3.5)
        for x, _z in route_cells
    )
    manifest = _manifest_with_cached_route(
        component_cells=component_cells,
        route_cells=route_cells,
        route_points=route_points,
    )
    manifest["navigation"]["routes"][0]["y_ranges"] = [
        value
        for _cell in route_cells
        for value in (0.0, 4.0)
    ]
    centerline_path = cached_centerline_path(manifest)
    events = []

    assert centerline_path is not None
    candidate = _build_hemisphere_probe_route_candidate(
        ordinal=1,
        centerline_path=centerline_path,
        current=np.asarray(route_points[0], dtype=np.float64),
        route_points=route_points,
        current_yaw=0.0,
        current_pitch=0.0,
        current_roll=np.pi / 2.0,
        current_travel_yaw=0.0,
        current_travel_pitch=0.0,
        collision_validator=_AutoDiveCollisionValidator(centerline_path),
        avoid_positions=None,
        settings=AutoDiveSettings(
            smoothing_radius_cells=0,
            lookahead_distance_m=8.0,
        ),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    assert candidate is not None
    assert candidate.name == "hemisphere-probe"
    assert candidate.roll_deg == pytest.approx(90.0)
    scan_payload = [
        payload
        for event, payload in events
        if event == "hemisphere_scan"
    ][-1]
    assert scan_payload["generated_count"] == 32 * 4 * 9
    assert scan_payload["coarse_candidate_count"] > 0
    assert scan_payload["top_candidates"]


def test_auto_dive_execution_keyframes_ignore_probe_roll(monkeypatch):
    manifest = _l_bend_manifest()
    selected_cells = tuple(
        [(x, 0) for x in range(7)] + [(6, 1), (6, 2)]
    )
    selected_points = tuple(
        (float(x) + 0.5, 1.0, float(z) + 0.5)
        for x, z in selected_cells
    )
    selected = _AutoDiveSelectedRoute(
        points=selected_points,
        selection_reason="hemisphere-probe",
        roll_deg=90.0,
    )
    monkeypatch.setattr(
        autodive,
        "_select_best_auto_dive_route_candidate",
        lambda *args, **kwargs: selected,
    )

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=(0.5, 1.0, 0.5),
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            smoothing_radius_cells=0,
        ),
    )

    assert all(
        keyframe.roll_deg == pytest.approx(0.0)
        for keyframe in plan.route.keyframes
    )


def test_auto_dive_forced_hemisphere_scan_bypasses_local_voxel_route():
    component_cells = [(x, z) for x in range(8) for z in range(8)]
    route_cells = tuple((x, 3) for x in range(8))
    route_points = tuple(
        (float(x) + 0.5, 1.0, 3.5)
        for x, _z in route_cells
    )
    manifest = _manifest_with_cached_route(
        component_cells=component_cells,
        route_cells=route_cells,
        route_points=route_points,
    )
    centerline_path = cached_centerline_path(manifest)
    events = []

    assert centerline_path is not None
    local_volume = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(-8.0, -8.0, -8.0),
        shape=(32, 24, 32),
        surface_cells=frozenset(),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=16,
    )
    candidate = _build_hemisphere_probe_route_candidate(
        ordinal=1,
        centerline_path=centerline_path,
        current=np.asarray(route_points[0], dtype=np.float64),
        route_points=route_points,
        current_yaw=0.0,
        current_pitch=0.0,
        current_roll=0.0,
        current_travel_yaw=0.0,
        current_travel_pitch=0.0,
        collision_validator=_AutoDiveCollisionValidator(
            centerline_path,
            voxel_refinement=local_volume,
        ),
        avoid_positions=None,
        settings=AutoDiveSettings(
            smoothing_radius_cells=0,
            lookahead_distance_m=8.0,
        ),
        force_hemisphere_scan=True,
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    assert candidate is not None
    assert candidate.name == "hemisphere-probe"
    scan_payload = [
        payload
        for event, payload in events
        if event == "hemisphere_scan"
    ][-1]
    assert scan_payload["forced_full_scan"] is True
    assert scan_payload["generated_count"] == 32 * 4 * 9


def test_bounded_local_frontier_is_converted_to_safe_true_3d_graph():
    volume = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(-2.0, -2.0, -2.0),
        shape=(20, 12, 12),
        surface_cells=frozenset(),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=16,
    )
    result = _build_bounded_local_frontier_graph_route(
        volume=volume,
        current=(0.5, 0.5, 0.5),
        forward=(1.0, 0.0, 0.0),
        settings=AutoDiveSettings(
            max_keyframes=32,
            voxel_local_refinement_forward_m=8.0,
            voxel_local_refinement_max_cells=2048,
            lookahead_distance_m=4.0,
        ),
        mesh_guard=None,
        avoid_positions=None,
        authority_status={
            "route_id": "longest-passage",
            "cache_version": "3",
            "cache_method": "navigation_voxel_cache_v3",
        },
        diagnostics=None,
    )

    assert result is not None
    atlas, graph, route_points, graph_keys, route_cells, selection = result
    assert atlas.prepared_3d_graph is graph
    assert graph.motion_geometry_safe is True
    assert graph.edge_integrity_safe is True
    assert len(route_points) == len(graph_keys) + 1
    assert len(route_cells) == len(graph_keys)
    assert route_points[-1][0] > route_points[0][0]
    assert selection["authority"] == "bounded_runtime_local_true_3d_graph"
    assert selection["coverage_incomplete"] is True


def test_bounded_local_frontier_drops_inflated_camera_seed_before_validation():
    current = (0.5, 0.5, 0.5)
    current_index = (2, 2, 2)
    local_volume = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(-2.0, -2.0, -2.0),
        shape=(20, 12, 12),
        # Model the runtime rasterizer conservatively classifying the camera
        # seed as an inflated surface voxel while the prepared atlas keeps
        # the actual camera point free.
        surface_cells=frozenset({current_index}),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=16,
    )
    camera_volume = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(-2.0, -2.0, -2.0),
        shape=(20, 12, 12),
        surface_cells=frozenset(),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=16,
    )
    camera_atlas = NavigationVoxelAtlas(tiles=(camera_volume,))

    result = _build_bounded_local_frontier_graph_route(
        volume=local_volume,
        current=current,
        forward=(1.0, 0.0, 0.0),
        settings=AutoDiveSettings(
            max_keyframes=32,
            voxel_local_refinement_forward_m=8.0,
            voxel_local_refinement_max_cells=2048,
            lookahead_distance_m=4.0,
        ),
        mesh_guard=None,
        camera_atlas=camera_atlas,
        avoid_positions=None,
        authority_status={
            "route_id": "longest-passage",
            "cache_version": "3",
            "cache_method": "navigation_voxel_cache_v3",
        },
        diagnostics=None,
    )

    assert result is not None
    _atlas, _graph, route_points, graph_keys, _route_cells, _selection = result
    assert route_points[0] == current
    assert graph_keys[0] != current_index
    assert route_points[1] != local_volume.voxel_center(current_index)


def test_bounded_local_frontier_reuses_search_when_mesh_trims_prefix(monkeypatch):
    volume = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(-2.0, -2.0, -2.0),
        shape=(20, 12, 12),
        surface_cells=frozenset(),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=16,
    )

    class _MeshGuard:
        def segment_collision(self, first, second):
            if max(float(first[0]), float(second[0])) >= 6.0:
                return SimpleNamespace(point=(6.0, 0.5, 0.5))
            return None

    calls = 0
    original_find_forward_route = LocalVoxelVolume.find_forward_route

    def counting_find_forward_route(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_find_forward_route(self, *args, **kwargs)

    monkeypatch.setattr(
        LocalVoxelVolume,
        "find_forward_route",
        counting_find_forward_route,
    )
    events = []
    result = _build_bounded_local_frontier_graph_route(
        volume=volume,
        current=(0.5, 0.5, 0.5),
        forward=(1.0, 0.0, 0.0),
        settings=AutoDiveSettings(
            max_keyframes=32,
            voxel_local_refinement_forward_m=8.0,
            voxel_local_refinement_max_cells=2048,
            lookahead_distance_m=4.0,
        ),
        mesh_guard=_MeshGuard(),  # type: ignore[arg-type]
        avoid_positions=None,
        authority_status={
            "route_id": "longest-passage",
            "cache_version": "3",
            "cache_method": "navigation_voxel_cache_v3",
        },
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    assert result is not None
    _atlas, _graph, route_points, _graph_keys, _route_cells, selection = result
    assert calls == 1
    assert selection["route_truncated_by_mesh"] is True
    assert route_points[-1][0] < 6.0
    safe_prefix = [
        payload
        for event, payload in events
        if event == "voxel_local_frontier_mesh_safe_prefix"
    ]
    assert safe_prefix
    assert safe_prefix[-1]["route_search_reused"] is True
    assert not any(
        event == "voxel_local_frontier_route_retry" for event, _payload in events
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


def test_auto_dive_mesh_guard_bounds_decoded_triangle_residency(monkeypatch, tmp_path):
    guard = CachedChunkMeshCollisionGuard.from_manifest(
        _split_manifest(),
        cache_dir=str(tmp_path / "cache"),
        max_cached_triangles=2,
    )

    assert guard is not None
    monkeypatch.setattr(
        guard,
        "_load_triangles_for_chunk",
        lambda _cell: np.zeros((1, 3, 3), dtype=np.float64),
    )

    meshes = guard.triangle_meshes_for_bounds(
        (0.0, 0.0, 0.0),
        (72.0, 10.0, 6.0),
    )
    assert not guard._triangle_cache
    assert len(tuple(meshes)) > 10
    assert guard._cached_triangle_count <= 2
    assert len(guard._triangle_cache) <= 2


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
