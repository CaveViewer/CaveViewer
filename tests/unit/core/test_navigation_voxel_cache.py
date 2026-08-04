"""Tests for bounded cache-time navigation voxel models."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
import math

import numpy as np
import pytest

import caveviewer.core.navigation.voxel_cache as voxel_cache_module
from caveviewer.core.navigation.cache_metadata import (
    NAVIGATION_METADATA_METHOD,
    NAVIGATION_METADATA_VERSION,
    cached_centerline_path,
)
from caveviewer.core.navigation.cubic_graph import SparseCubicVoxelGraph
from caveviewer.core.navigation.autodive import (
    AutoDiveSettings,
    NavigationVoxelGraphAuthorityError,
    build_centerline_auto_dive_plan,
)
from caveviewer.core.navigation.mesh_graph import MeshNavigationGraphConfig
from caveviewer.core.navigation.route import NavigationConfigurationError
from caveviewer.core.navigation.voxel_cache import (
    NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
    NAVIGATION_VOXEL_CACHE_NAME,
    NavigationVoxelCellMetric,
    NavigationVoxelAtlas,
    NavigationVoxelBranchScore,
    NavigationVoxelCacheConfig,
    NavigationVoxelScoringPolicy,
    _cache_graph_base_grid_size,
    _build_adaptive_seeded_mesh_navigation_path,
    _build_route_ordered_fine_mesh_navigation_path,
    _consecutive_route_cells,
    _component_vertical_gap_intervals,
    _component_vertical_gap_seed_points,
    _cubic_corridor_radius_candidates,
    _fine_seed_tile_coverage_details,
    _fine_prepared_graph_seed_points,
    _mesh_entry_route_sampling_cells,
    _mesh_supported_candidate_points,
    _mesh_spine_roadmap_anchors,
    _horizontal_cubic_voxel_candidates,
    _horizontal_route_tube_point_filter,
    _point_segment_distance_squared,
    _publish_certified_complete_route,
    _route_tube_point_filter,
    _route_transition_sampling_y_ranges,
    _route_cell_horizontal_guide_points,
    _select_terminal_cubic_component,
    _source_connected_vertical_gap_layer,
    _surface_gap_cubic_waypoint_key_groups,
    _surface_gap_interval_route_key_groups,
    _surface_gap_interval_terminal_keys,
    _true_3d_unknown_boundary_keys,
    build_navigation_voxel_cache,
    deserialize_local_voxel_volume,
    load_cached_navigation_voxel_volume,
    navigation_route_contract_rebuild_reason,
    serialize_navigation_voxel_volume,
    supported_navigation_voxel_cache_identity,
)
from caveviewer.core.navigation.voxel_graph_3d import (
    NAVIGATION_MESH_3D_GRAPH_METHOD,
    NAVIGATION_VOXEL_3D_GRAPH_METHOD,
    NavigationVoxel3DMetric,
    build_navigation_voxel_3d_graph,
)
from caveviewer.core.navigation.voxel_volume import (
    LocalVoxelVolume,
    VoxelVolumeConfig,
    build_surface_voxel_volume,
)


def test_cubic_corridor_backoff_preserves_voxel_resolution():
    assert _cubic_corridor_radius_candidates(
        16.0,
        voxel_size_m=1.0,
    ) == (16.0, 8.0, 4.0)
    assert _cubic_corridor_radius_candidates(
        3.0,
        voxel_size_m=1.0,
    ) == (3.0,)
    evidence_bounded = _cubic_corridor_radius_candidates(
        16.0,
        voxel_size_m=1.0,
        minimum_radius_m=7.5,
    )
    assert evidence_bounded == (16.0, 8.0, 7.5)
    assert min(evidence_bounded) == 7.5


def test_mesh_supported_candidates_filter_unbounded_points_stably():
    calls = []

    def supported(point, max_distance_m, minimum_clearance_m):
        calls.append((point, max_distance_m, minimum_clearance_m))
        return point[0] >= 0.0

    points = _mesh_supported_candidate_points(
        ((-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        mesh_point_has_opposing_support=supported,
        max_distance_m=128.0,
        minimum_clearance_m=0.25,
    )

    assert points == ((1.0, 0.0, 0.0),)
    assert calls == [
        ((-1.0, 0.0, 0.0), 128.0, 0.25),
        ((1.0, 0.0, 0.0), 128.0, 0.25),
    ]


def test_mesh_supported_candidates_fail_closed_on_probe_error():
    points = _mesh_supported_candidate_points(
        ((1.0, 2.0, 3.0),),
        mesh_point_has_opposing_support=lambda *_args: (_ for _ in ()).throw(
            OSError("mesh unavailable")
        ),
        max_distance_m=128.0,
        minimum_clearance_m=0.25,
    )

    assert points == ()


def test_mesh_supported_candidates_preserve_legacy_test_seam_when_unavailable():
    points = _mesh_supported_candidate_points(
        ((1.0, 2.0, 3.0),),
        mesh_point_has_opposing_support=None,
        max_distance_m=128.0,
        minimum_clearance_m=0.25,
    )

    assert points == ((1.0, 2.0, 3.0),)


def test_ordered_fine_mesh_retry_reaches_every_surface_gap_gate():
    surface_gap_points = tuple(
        (float(index) + 0.1, 0.19, 0.1)
        for index in range(4)
    )
    exact_lattice_points = tuple(
        (float(index) + 0.25, 0.125, 0.25)
        for index in range(4)
    )
    result = _build_route_ordered_fine_mesh_navigation_path(
        surface_gap_points,
        waypoint_point_groups=(
            (surface_gap_points[1],),
            (surface_gap_points[2],),
        ),
        entry_candidate_points=(exact_lattice_points[0],),
        terminal_candidate_points=(surface_gap_points[-1],),
        footprint_cell_size_m=1.0,
        component_cells={(index, 0) for index in range(4)},
        point_probe=lambda _point: (True, 1.0),
        edge_is_clear=lambda _first, _second: True,
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=0.5,
            vertical_sample_spacing_m=0.25,
            minimum_clearance_m=0.05,
            max_nodes=512,
            max_edge_distance_m=1.0,
            max_vertical_edge_distance_m=0.5,
        ),
        route_tube_radius_m=1.0,
        max_total_nodes=512,
    )

    assert result.graph is not None
    assert result.details["reason"] == (
        "route_ordered_fine_mesh_terminal_path_built"
    )
    assert result.details["reached_intermediate_gate_count"] == 2
    centers = tuple(node.center for node in result.graph.nodes.values())
    assert centers == exact_lattice_points
    assert len(centers) == len(set(centers))
    used_budget = sum(
        int(attempt["used_node_budget"])
        for attempt in result.details["leg_attempts"]
    )
    assert used_budget + int(result.details["remaining_search_nodes"]) == 512


def test_ordered_fine_mesh_retry_fails_closed_on_missing_gate_evidence():
    points = (
        (0.25, 0.125, 0.25),
        (1.25, 0.125, 0.25),
        (2.25, 0.125, 0.25),
    )
    result = _build_route_ordered_fine_mesh_navigation_path(
        points,
        waypoint_point_groups=((),),
        entry_candidate_points=(points[0],),
        terminal_candidate_points=(points[-1],),
        footprint_cell_size_m=1.0,
        component_cells={(index, 0) for index in range(3)},
        point_probe=lambda _point: (True, 1.0),
        edge_is_clear=lambda _first, _second: True,
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=0.5,
            vertical_sample_spacing_m=0.25,
            max_nodes=128,
        ),
        route_tube_radius_m=1.0,
        max_total_nodes=128,
    )

    assert result.graph is None
    assert result.details["reason"] == "route_ordered_fine_mesh_inputs_missing"


def test_ordered_fine_mesh_retry_rejects_wrong_gate_count():
    points = tuple((float(index) + 0.25, 0.125, 0.25) for index in range(4))
    result = _build_route_ordered_fine_mesh_navigation_path(
        points,
        waypoint_point_groups=((points[1],),),
        entry_candidate_points=(points[0],),
        terminal_candidate_points=(points[-1],),
        footprint_cell_size_m=1.0,
        component_cells={(index, 0) for index in range(4)},
        point_probe=lambda _point: (True, 1.0),
        edge_is_clear=lambda _first, _second: True,
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=0.5,
            vertical_sample_spacing_m=0.25,
            max_nodes=128,
        ),
        route_tube_radius_m=1.0,
        max_total_nodes=128,
    )

    assert result.graph is None
    assert result.details["reason"] == "route_ordered_fine_mesh_inputs_missing"
    assert result.details["expected_intermediate_gate_count"] == 2


def test_cell_derived_guides_keep_sparse_dogleg_inside_route_tube():
    cells = _consecutive_route_cells(
        ((0, 0), (0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    )
    guides = _route_cell_horizontal_guide_points(cells, cell_size=1.0)
    contains = _horizontal_route_tube_point_filter(
        guides,
        radius_m=0.6,
        voxel_size_m=0.5,
    )

    assert cells == ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    assert all(contains(point) for point in guides)
    assert contains((1.5, 99.0, 0.5))


def test_cache_analysis_never_retries_a_ranked_route_suffix(monkeypatch):
    points = tuple(
        (float(index) + 0.5, 0.5, 0.5)
        for index in range(5)
    )
    route = {
        "id": "centerline-0",
        "footprint_cell_size": 1.0,
        "component_cells": [
            value for index in range(5) for value in (index, 0)
        ],
        "cells": [value for index in range(5) for value in (index, 0)],
        "points": [value for point in points for value in point],
        "length_m": 4.0,
    }
    calls: list[tuple[tuple[tuple[float, float, float], ...], list[int]]] = []

    def fake_build(_manifest, attempted_route, attempted_points, **_kwargs):
        calls.append(
            (
                tuple(attempted_points),
                list(attempted_route["cells"]),
            )
        )
        if len(calls) == 1:
            return None, {}, {
                "surface_sample_count": 1,
                "cubic_graph": {"free_voxel_count": 32},
                "cubic_component": {
                    "ingress_hint_index": 2,
                    "terminal_hint_index": 4,
                    "contiguous_route_length_m": 2.0,
                    "selected_component_voxel_count": 8,
                    "route_component_fallback": {
                        "ingress_hint_index": 1,
                        "terminal_hint_index": 3,
                        "contiguous_route_length_m": 2.0,
                        "component_voxel_count": 16,
                    },
                },
                "prepared_mesh_graph": {
                    "reason": "adaptive_mesh_known_terminal_unreachable"
                },
            }
        return None, {}, {
            "surface_sample_count": 1,
            "cubic_graph": {"free_voxel_count": 16},
            "cubic_component": {
                "ingress_hint_index": 0,
                "terminal_hint_index": 2,
                "contiguous_route_length_m": 2.0,
                "selected_component_voxel_count": 16,
            },
            "prepared_mesh_graph": {
                "reason": "adaptive_mesh_known_terminal_unreachable"
            },
        }

    monkeypatch.setattr(
        voxel_cache_module,
        "_build_route_voxel_atlas",
        fake_build,
    )

    summary = voxel_cache_module._analyze_route(
        {"footprint_cell_size": 1.0},
        route,
        points,
        route_id="centerline-0",
        triangle_provider=lambda _lower, _upper: (),
        mesh_edge_is_clear=lambda _first, _second: True,
        config=NavigationVoxelCacheConfig(),
    )

    assert len(calls) == 1
    assert calls[0][0] == points
    assert summary["built"] is False
    assert summary["mesh_terminal_route_attempt_count"] == 1
    assert [
        (
            attempt["source_hint_start_index"],
            attempt["source_hint_end_index"],
        )
        for attempt in summary["mesh_terminal_route_attempts"]
    ] == [(0, 4)]


def test_obj_ingress_never_retries_or_publishes_a_route_suffix(monkeypatch):
    points = tuple((float(index) + 0.5, 0.5, 0.5) for index in range(5))
    route = {
        "id": "centerline-0",
        "footprint_cell_size": 1.0,
        "component_cells": [
            value for index in range(5) for value in (index, 0)
        ],
        "cells": [value for index in range(5) for value in (index, 0)],
        "points": [value for point in points for value in point],
        "length_m": 4.0,
    }
    calls = 0

    def fake_build(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return None, {}, {
            "surface_sample_count": 1,
            "cubic_graph": {"free_voxel_count": 32},
            "cubic_component": {
                "ingress_hint_index": 2,
                "terminal_hint_index": 4,
                "route_component_fallback": {
                    "ingress_hint_index": 1,
                    "terminal_hint_index": 3,
                },
            },
            "prepared_mesh_graph": {
                "reason": "adaptive_mesh_known_terminal_unreachable"
            },
        }

    monkeypatch.setattr(
        voxel_cache_module,
        "_build_route_voxel_atlas",
        fake_build,
    )

    summary = voxel_cache_module._analyze_route(
        {"footprint_cell_size": 1.0},
        route,
        points,
        route_id="centerline-0",
        triangle_provider=lambda _lower, _upper: (),
        mesh_edge_is_clear=lambda _first, _second: True,
        config=NavigationVoxelCacheConfig(),
        source_ingress_anchor=(0.0, 0.0, 0.0),
    )

    assert calls == 1
    assert summary["built"] is False
    assert summary["mesh_terminal_route_attempt_count"] == 1


def test_route_tube_filter_rejects_stacked_space_outside_3d_route():
    contains = _route_tube_point_filter(
        ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)),
        radius_m=1.0,
        voxel_size_m=1.0,
    )

    assert contains((5.0, 1.5, 0.0)) is True
    assert contains((5.0, 8.0, 0.0)) is False
    assert contains((5.0, 0.0, 8.0)) is False


def test_horizontal_route_tube_keeps_stacked_vertical_candidates():
    contains = _horizontal_route_tube_point_filter(
        ((0.0, -50.0, 0.0), (10.0, 75.0, 0.0)),
        radius_m=1.0,
        voxel_size_m=1.0,
    )

    assert contains((5.0, -500.0, 0.0)) is True
    assert contains((5.0, 500.0, 0.0)) is True
    assert contains((5.0, 0.0, 8.0)) is False


def test_route_tube_filter_matches_exact_segment_distance_across_buckets():
    route = (
        (-12.25, -3.5, 7.0),
        (20.75, 4.25, -11.0),
        (20.75, 4.25, -11.0),
        (-2.0, 9.0, 17.0),
    )
    radius_m = 1.25
    voxel_size_m = 0.5
    contains = _route_tube_point_filter(
        route,
        radius_m=radius_m,
        voxel_size_m=voxel_size_m,
    )
    effective_radius = radius_m + math.sqrt(3.0) * voxel_size_m * 0.5
    segments = tuple(zip(route[:-1], route[1:], strict=True))

    for x in range(-16, 25, 4):
        for y in range(-8, 13, 4):
            for z in range(-16, 21, 4):
                point = (float(x), float(y), float(z))
                expected = any(
                    _point_segment_distance_squared(point, first, second)
                    <= effective_radius * effective_radius + 1e-9
                    for first, second in segments
                )
                assert contains(point) is expected


def test_cache_identity_rejects_previous_two_metre_atlas():
    assert supported_navigation_voxel_cache_identity(
        6,
        "whole_cave_voxel_atlas_v6",
    )


def test_v12_cache_configuration_rejects_horizontal_voxels_coarser_than_one_metre():
    with pytest.raises(ValueError, match="1 m or finer"):
        NavigationVoxelCacheConfig(voxel_size_m=1.01).validated()


def test_v12_cache_configuration_rejects_coarse_authoritative_mesh_y():
    with pytest.raises(ValueError, match="mesh graph Y spacing"):
        NavigationVoxelCacheConfig(
            mesh_graph_vertical_sample_spacing_m=0.5,
        ).validated()


def test_current_atlas_round_trips_the_mesh_graph_separately_from_voxel_graph():
    metrics = {
        (0, 0, 0): NavigationVoxel3DMetric(
            center=(0.5, 0.5, 0.5),
            footprint_cell=(0, 0),
            available_volume_m3=1.0,
            free_voxel_count=1,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=0.0,
        ),
        (1, 0, 0): NavigationVoxel3DMetric(
            center=(1.5, 0.5, 0.5),
            footprint_cell=(1, 0),
            available_volume_m3=1.0,
            free_voxel_count=1,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=1.0,
        ),
    }
    voxel_graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )
    mesh_metrics = {
        key: replace(metric, center=(metric.center[0], 0.125, metric.center[2]))
        for key, metric in metrics.items()
    }
    mesh_graph = replace(
        build_navigation_voxel_3d_graph(
            mesh_metrics,
            grid_size_m=(1.0, 0.25, 1.0),
        ),
        method=NAVIGATION_MESH_3D_GRAPH_METHOD,
    )
    tile = LocalVoxelVolume(
        voxel_size_m=1.0,
        vertical_voxel_size_m=0.25,
        origin=(0.0, 0.0, 0.0),
        shape=(2, 1, 1),
        surface_cells=frozenset(),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=2,
    )
    payload = serialize_navigation_voxel_volume(
        NavigationVoxelAtlas(
            tiles=(tile,),
            coverage_scope="certified_terminal_route",
            prepared_3d_graph=voxel_graph,
            prepared_mesh_graph=mesh_graph,
            mesh_graph_entry_anchor_radius_m=24.0,
            fixed_isotropic_voxel_size_m=1.0,
            fixed_vertical_voxel_size_m=0.25,
            surface_overlap_occupied_wins=True,
        )
    )

    restored = deserialize_local_voxel_volume(payload)

    assert isinstance(restored, NavigationVoxelAtlas)
    assert restored.prepared_3d_graph is not None
    assert restored.prepared_mesh_graph is not None
    assert restored.prepared_mesh_graph.method == NAVIGATION_MESH_3D_GRAPH_METHOD
    assert restored.authoritative_graph is restored.prepared_mesh_graph
    assert restored.mesh_graph_entry_anchor_radius_m == 24.0

    coarse_mesh_payload = copy.deepcopy(payload)
    coarse_mesh_payload["prepared_mesh_graph"]["grid_size_m"] = [
        1.0,
        1.0,
        1.0,
    ]
    with pytest.raises(ValueError, match="terminal path is invalid"):
        deserialize_local_voxel_volume(coarse_mesh_payload)

    coarse_scalar_payload = copy.deepcopy(payload)
    coarse_scalar_payload["fixed_vertical_voxel_size_m"] = 0.5
    with pytest.raises(ValueError, match="too coarse"):
        deserialize_local_voxel_volume(coarse_scalar_payload)

    mismatched_scalar_payload = copy.deepcopy(payload)
    mismatched_scalar_payload["fixed_vertical_voxel_size_m"] = 0.125
    with pytest.raises(ValueError, match="too coarse"):
        deserialize_local_voxel_volume(mismatched_scalar_payload)

    legacy_payload = dict(payload)
    legacy_payload.update(
        {
            "version": 10,
            "method": "navigation_voxel_atlas_v10",
            "coverage_scope": "entire_cave_component",
        }
    )
    for field_name in (
        "fixed_voxel_method",
        "fixed_isotropic_voxel_size_m",
        "sampling_complete",
        "surface_overlap_policy",
        "cubic_graph_method",
    ):
        legacy_payload.pop(field_name, None)
    legacy = deserialize_local_voxel_volume(legacy_payload)
    assert isinstance(legacy, NavigationVoxelAtlas)
    assert legacy.prepared_mesh_graph is not None
    assert legacy.fixed_isotropic_voxel_size_m == 0.0
    assert legacy.surface_overlap_occupied_wins is False
    assert not supported_navigation_voxel_cache_identity(
        5,
        "whole_cave_voxel_atlas_v5",
    )


def test_mesh_entry_sampling_uses_world_cells_across_negative_boundaries():
    component_cells = {
        (x, z)
        for x in range(-12, -6)
        for z in range(8, 17)
    }

    cells = _mesh_entry_route_sampling_cells(
        (
            (-84.4, 10.0, 142.4),
            (-89.7, 8.0, 131.9),
            (-95.0, 9.0, 110.8),
        ),
        footprint_cell_size_m=10.554,
        component_cells=component_cells,
    )

    assert (-8, 13) in cells
    assert (-9, 12) in cells
    assert (-10, 10) in cells
    assert all(cell in component_cells for cell in cells)


def test_adaptive_mesh_path_uses_fine_corridor_when_two_metre_seed_is_missing():
    route_points = tuple(
        (float(index) + 0.5, 0.5, 0.5)
        for index in range(9)
    )

    def one_metre_probe(point):
        key = tuple(int(math.floor(value)) for value in point)
        if key not in {(x, 0, 0) for x in range(9)}:
            return None
        return True, 1.0

    result = _build_adaptive_seeded_mesh_navigation_path(
        route_points,
        footprint_cell_size_m=32.0,
        component_cells={(0, 0)},
        point_probe=one_metre_probe,
        edge_is_clear=lambda _first, _second: True,
        coarse_config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=2.0,
            vertical_sample_spacing_m=2.0,
            max_nodes=64,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
        fine_spacing_m=1.0,
    )

    assert result.graph is not None
    assert result.details["adaptive_retry_used"] is True
    assert result.details["known_terminal_reached"] is True
    assert result.details["adaptive_fine_spacing_m"] == 1.0
    assert result.details["adaptive_fine_route_tube_radius_m"] == 4.0
    assert result.details["edge_candidate_limit_per_node"] == 12
    assert result.details["coarse_reason"] == (
        "goal_directed_mesh_graph_entry_missing"
    )
    assert result.graph.terminal_count == 1


def test_adaptive_mesh_path_retries_half_metre_horizontal_quarter_metre_vertical_lattice():
    route_points = tuple(
        (0.25 + float(index) * 0.5, 0.125, 0.25)
        for index in range(12)
    )
    def half_metre_probe(point):
        if not (
            0.0 <= point[0] < 6.0
            and 0.0 <= point[1] < 1.0
            and 0.0 <= point[2] < 1.0
        ):
            return None
        return True, 1.0

    def quarter_aligned_edge(first, second):
        return first[1:] == (0.125, 0.25) and second[1:] == (0.125, 0.25)

    result = _build_adaptive_seeded_mesh_navigation_path(
        route_points,
        footprint_cell_size_m=32.0,
        component_cells={(0, 0)},
        point_probe=half_metre_probe,
        edge_is_clear=quarter_aligned_edge,
        coarse_config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=1.0,
            vertical_sample_spacing_m=1.0,
            max_nodes=64,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
        fine_spacing_m=0.5,
    )

    assert result.graph is not None
    assert result.details["adaptive_retry_used"] is True
    assert result.details["adaptive_coarse_spacing_m"] == 1.0
    assert result.details["adaptive_fine_spacing_m"] == 0.5
    assert result.details["adaptive_fine_vertical_spacing_m"] == 0.25
    assert result.details["adaptive_fine_route_tube_radius_m"] == 4.0
    assert result.details["known_terminal_reached"] is True
    assert result.graph.terminal_count == 1


def test_adaptive_mesh_path_widens_fine_tube_after_bounded_exhaustion():
    free_keys = {
        *((value, 0, 0) for value in range(5)),
        *((4, 0, value) for value in range(1, 11)),
        *((value, 0, 10) for value in range(5, 17)),
        *((16, 0, value) for value in range(10)),
        *((value, 0, 0) for value in range(17, 21)),
    }

    def half_metre_detour_probe(point):
        coordinates = tuple(
            int(math.floor(float(value) / 0.5))
            for value in point
        )
        if coordinates not in free_keys:
            return None
        return True, 1.0

    result = _build_adaptive_seeded_mesh_navigation_path(
        ((0.25, 0.25, 0.25), (10.25, 0.25, 0.25)),
        footprint_cell_size_m=32.0,
        component_cells={(0, 0)},
        point_probe=half_metre_detour_probe,
        edge_is_clear=lambda first, second: math.dist(first, second) <= 0.9,
        coarse_config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=1.0,
            vertical_sample_spacing_m=1.0,
            max_nodes=4096,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
        fine_spacing_m=0.5,
    )

    assert result.graph is not None
    assert result.details["adaptive_retry_used"] is True
    assert result.details["adaptive_fine_route_tube_radius_m"] == 8.0
    assert [
        attempt["route_tube_radius_m"]
        for attempt in result.details["adaptive_fine_route_tube_attempts"]
    ] == [4.0, 8.0]
    assert result.details["adaptive_fine_route_tube_attempts"][0][
        "built"
    ] is False
    assert result.details["adaptive_fine_route_tube_attempts"][1][
        "built"
    ] is True


def test_adaptive_mesh_path_does_not_publish_intermediate_hint_as_terminal():
    route_points = (
        (1.0, 1.0, 1.0),
        (5.0, 1.0, 1.0),
        (11.0, 1.0, 1.0),
    )

    def coarse_only_probe(point):
        if point[1:] != (1.0, 1.0) or point[0] not in {1.0, 3.0, 5.0}:
            return None
        return True, 1.0

    result = _build_adaptive_seeded_mesh_navigation_path(
        route_points,
        footprint_cell_size_m=32.0,
        component_cells={(0, 0)},
        point_probe=coarse_only_probe,
        edge_is_clear=lambda _first, _second: True,
        coarse_config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=2.0,
            vertical_sample_spacing_m=2.0,
            max_nodes=64,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
        fine_spacing_m=1.0,
    )

    assert result.graph is None
    assert result.details["reason"] == (
        "adaptive_mesh_known_terminal_unreachable"
    )
    assert result.details["coarse_maximum_route_guide_index_seen"] == 1
    assert result.details["known_terminal_reached"] is False


def test_adaptive_mesh_path_forwards_reachable_terminal_snap_candidates():
    primary_terminal = (5.5, 0.5, 0.5)
    reachable_candidate = (4.5, 0.5, 0.5)
    free_keys = {(value, 0, 0) for value in range(5)}

    def point_probe(point):
        key = tuple(int(math.floor(value)) for value in point)
        if key not in free_keys:
            return None
        return True, 1.0

    result = _build_adaptive_seeded_mesh_navigation_path(
        ((0.5, 0.5, 0.5), primary_terminal),
        footprint_cell_size_m=32.0,
        component_cells={(0, 0)},
        point_probe=point_probe,
        edge_is_clear=(
            lambda _first, second: tuple(second) != primary_terminal
        ),
        coarse_config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=1.0,
            vertical_sample_spacing_m=1.0,
            max_nodes=64,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
        fine_spacing_m=0.5,
        terminal_candidate_points=(reachable_candidate,),
    )

    assert result.graph is not None
    assert result.details["adaptive_retry_used"] is False
    assert result.details["terminal_hint_count"] == 2
    assert result.details["selected_terminal_hint_point"] == list(
        reachable_candidate
    )


def test_terminal_component_prefers_longer_run_over_short_endpoint_suffix():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((value, 0, 0) for value in range(9))
        + ((20, 0, 0), (21, 0, 0), (22, 0, 0))
    )
    route_points = tuple(
        (float(value) + 0.5, 0.5, 0.5)
        for value in (*range(9), 20, 21, 22)
    )

    component, details = _select_terminal_cubic_component(
        graph,
        route_points,
        terminal_snap_distance_m=1.0,
        ingress_snap_distance_m=1.0,
        max_component_voxels=32,
    )
    strict_component, strict_details = _select_terminal_cubic_component(
        graph,
        route_points,
        terminal_snap_distance_m=1.0,
        ingress_snap_distance_m=1.0,
        max_component_voxels=32,
        require_original_ingress=True,
    )

    assert component is not None
    assert component.keys() == tuple((value, 0, 0) for value in range(9))
    assert details["ingress_hint_index"] == 0
    assert details["terminal_hint_index"] == 8
    assert details["ingress_selection"] == (
        "ranked_contiguous_route_component_v2"
    )
    assert strict_component is None
    assert strict_details["original_ingress_required"] is True


def test_terminal_component_selects_shortest_meaningful_unauthored_route():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((value, 0, 0) for value in range(81))
        + tuple((value, 0, 0) for value in range(100, 141))
    )
    route_points = tuple(
        (float(value) + 0.5, 0.5, 0.5)
        for value in (*range(81), *range(100, 141))
    )

    component, details = _select_terminal_cubic_component(
        graph,
        route_points,
        terminal_snap_distance_m=1.0,
        ingress_snap_distance_m=1.0,
        max_component_voxels=256,
    )

    assert component is not None
    assert component.keys() == tuple(
        (value, 0, 0) for value in range(100, 141)
    )
    assert details["ingress_hint_index"] == 81
    assert details["terminal_hint_index"] == 121
    assert details["contiguous_route_length_m"] == pytest.approx(40.0)
    assert details["minimum_meaningful_route_length_m"] == 32.0
    assert [
        (
            candidate["ingress_hint_index"],
            candidate["terminal_hint_index"],
        )
        for candidate in details["route_component_fallbacks"]
    ] == [(81, 121), (0, 80)]
    # A later component is diagnostic evidence only; production never retries
    # it as an executable route suffix.


def test_terminal_component_accepts_a_bounded_3d_endpoint_snap():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((value, 0, 0) for value in range(6))
    )

    component, details = _select_terminal_cubic_component(
        graph,
        ((0.5, 0.5, 0.5), (8.5, 0.5, 0.5)),
        terminal_snap_distance_m=4.0,
        ingress_snap_distance_m=1.0,
        max_component_voxels=32,
    )

    assert component is not None
    assert details["terminal_graph_key"] == [5, 0, 0]
    assert details["terminal_snap_distance_m"] == pytest.approx(3.0)
    assert details["known_terminal_reached"] is True
    assert details["terminal_graph_key_candidate_count"] == 2
    assert details["terminal_graph_key_candidates"][0] == [5, 0, 0]


def test_terminal_component_uses_strict_obj_source_ingress_in_full_xyz():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((value, 0, 0) for value in range(31))
    )

    component, details = _select_terminal_cubic_component(
        graph,
        ((20.5, 0.5, 0.5), (30.5, 0.5, 0.5)),
        terminal_snap_distance_m=2.0,
        ingress_snap_distance_m=2.0,
        max_component_voxels=64,
        require_original_ingress=True,
        source_ingress_point=(0.1, 0.5, 0.5),
        source_ingress_snap_distance_m=240.0,
    )

    assert component is not None
    assert details["ingress_graph_key"] == [0, 0, 0]
    assert details["ingress_selection"] == (
        "strict_navigation_source_ingress_v2"
    )
    assert details["source_ingress_attachment_point"] == [0.5, 0.5, 0.5]
    assert details["source_ingress_attachment_distance_m"] == pytest.approx(0.4)
    assert details["source_ingress_snap_limit_m"] == 24.0


def test_terminal_component_limits_obj_attachment_to_original_gap_envelope():
    keys = tuple((0, y, 0) for y in range(11)) + tuple(
        (x, 10, 0) for x in range(1, 5)
    )
    graph = SparseCubicVoxelGraph.from_keys(keys)

    component, details = _select_terminal_cubic_component(
        graph,
        ((0.5, 99.0, 0.5), (4.5, -99.0, 0.5)),
        terminal_snap_distance_m=2.0,
        ingress_snap_distance_m=24.0,
        max_component_voxels=64,
        require_original_ingress=True,
        source_ingress_point=(0.5, 9.5, 0.5),
        source_ingress_snap_distance_m=24.0,
        source_ingress_gap_y_ranges={(0, 0): (0.0, 0.75)},
        source_ingress_footprint_cell_size_m=1.0,
    )

    assert component is not None
    assert details["ingress_graph_key"] == [0, 0, 0]
    assert details["source_ingress_attachment_point"] == [0.5, 0.5, 0.5]
    assert details["source_ingress_gap_envelope_required"] is True


def test_terminal_component_prefers_the_closest_authored_ingress_component():
    near_component = tuple((value, 0, 0) for value in range(5))
    larger_far_component = tuple((value, 2, 0) for value in range(9))
    graph = SparseCubicVoxelGraph.from_keys(
        (*near_component, *larger_far_component)
    )

    component, details = _select_terminal_cubic_component(
        graph,
        ((0.5, 0.5, 0.5), (4.5, 0.5, 0.5)),
        terminal_snap_distance_m=3.0,
        ingress_snap_distance_m=3.0,
        max_component_voxels=64,
        require_original_ingress=True,
        source_ingress_point=(0.1, 0.5, 0.5),
        source_ingress_snap_distance_m=24.0,
    )

    assert component is not None
    assert component.keys() == near_component
    assert details["source_ingress_attachment_point"] == [0.5, 0.5, 0.5]
    assert details["source_ingress_attachment_distance_m"] == pytest.approx(0.4)


def test_terminal_component_never_relocates_a_missing_obj_source_ingress():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((value, 0, 0) for value in range(20, 31))
    )

    component, details = _select_terminal_cubic_component(
        graph,
        ((20.5, 0.5, 0.5), (30.5, 0.5, 0.5)),
        terminal_snap_distance_m=2.0,
        ingress_snap_distance_m=2.0,
        max_component_voxels=64,
        require_original_ingress=True,
        source_ingress_point=(0.1, 0.5, 0.5),
        source_ingress_snap_distance_m=4.0,
    )

    assert component is None
    assert details["reason"] == "cubic_component_ingress_voxel_missing"
    assert details["source_ingress_required"] is True
    assert details["source_ingress_snap_limit_m"] == 4.0


def test_terminal_component_publishes_only_the_local_endpoint_shell():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple(
            (x, y, z)
            for x in range(5)
            for y in range(5)
            for z in range(5)
        )
    )

    component, details = _select_terminal_cubic_component(
        graph,
        ((0.5, 0.5, 0.5), (4.5, 4.5, 4.5)),
        terminal_snap_distance_m=8.0,
        ingress_snap_distance_m=1.0,
        max_component_voxels=256,
    )

    assert component is not None
    assert details["terminal_graph_key"][0::2] == [4, 4]
    assert details["terminal_graph_key"][1] == 0
    assert details["terminal_graph_key_candidate_count"] == 8
    assert details["terminal_graph_key_candidates"] == [
        [4, 0, 4],
        [3, 0, 4],
        [4, 0, 3],
        [4, 1, 4],
        [3, 0, 3],
        [3, 1, 4],
        [4, 1, 3],
        [3, 1, 3],
    ]


def test_terminal_component_prefers_route_continuity_over_nearest_tiny_pocket():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((value, 0, 0) for value in range(6))
        + ((8, 0, 0), (9, 0, 0))
    )
    route_points = tuple(
        (float(value) + 0.5, 0.5, 0.5)
        for value in range(9)
    )

    component, details = _select_terminal_cubic_component(
        graph,
        route_points,
        terminal_snap_distance_m=4.0,
        ingress_snap_distance_m=1.0,
        max_component_voxels=32,
    )

    assert component is not None
    assert component.keys() == tuple((value, 0, 0) for value in range(6))
    assert details["terminal_graph_key"] == [5, 0, 0]
    assert details["ingress_selection"] == "original_route_ingress_v1"
    assert details["terminal_component_candidate_count"] == 2


def test_terminal_component_falls_back_to_longest_connected_route_run():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((value, 0, 0) for value in range(21))
        + ((100, 0, 0), (101, 0, 0))
    )
    route_points = tuple(
        (float(value) + 0.5, 0.5, 0.5)
        for value in (*range(21), 100, 101)
    )

    component, details = _select_terminal_cubic_component(
        graph,
        route_points,
        terminal_snap_distance_m=2.0,
        ingress_snap_distance_m=1.0,
        max_component_voxels=64,
    )

    assert component is not None
    assert component.keys() == tuple((value, 0, 0) for value in range(21))
    assert details["ingress_hint_index"] == 0
    assert details["terminal_hint_index"] == 20
    assert details["terminal_graph_key"] == [20, 0, 0]
    assert details["contiguous_route_length_m"] == pytest.approx(20.0)
    assert details["ingress_selection"] == (
        "ranked_contiguous_route_component_v2"
    )


def test_terminal_component_uses_connected_run_when_raw_endpoint_has_no_voxel():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((value, 0, 0) for value in range(21))
    )
    route_points = tuple(
        (float(value) + 0.5, 0.5, 0.5)
        for value in range(21)
    ) + ((1000.5, 0.5, 0.5),)

    component, details = _select_terminal_cubic_component(
        graph,
        route_points,
        terminal_snap_distance_m=2.0,
        ingress_snap_distance_m=1.0,
        max_component_voxels=64,
    )

    assert component is not None
    assert details["ingress_hint_index"] == 0
    assert details["terminal_hint_index"] == 20
    assert details["terminal_component_candidate_count"] == 0
    assert details["ingress_selection"] == (
        "ranked_contiguous_route_component_v2"
    )


def test_terminal_component_rejects_incomplete_exact_source_route():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((value, 0, 0) for value in range(21))
    )
    route_points = tuple(
        (float(value) + 0.5, 0.5, 0.5)
        for value in range(21)
    ) + ((1000.5, 0.5, 0.5),)

    component, details = _select_terminal_cubic_component(
        graph,
        route_points,
        terminal_snap_distance_m=2.0,
        ingress_snap_distance_m=2.0,
        max_component_voxels=64,
        require_original_ingress=True,
        source_ingress_point=(0.1, 0.5, 0.5),
        source_ingress_snap_distance_m=4.0,
    )

    assert component is None
    assert details["known_terminal_reached"] is False
    assert details["reason"] == "cubic_component_terminal_voxel_missing"


def test_nonzero_certified_route_offset_is_never_published_as_a_suffix():
    route = {
        "footprint_cell_size": 1.0,
        "selection_method": "physical_endpoint_diameter_v1",
        "cells": [0, 0, 1, 0, 10, 0, 11, 0],
        "points": [
            0.5,
            0.5,
            0.5,
            1.5,
            0.5,
            0.5,
            10.5,
            0.5,
            0.5,
            11.5,
            0.5,
            0.5,
        ],
        "y_ranges": [0.0, 1.0] * 4,
        "clearance_margins": [1.0, 1.0, 2.0, 2.0],
    }

    original = copy.deepcopy(route)
    _publish_certified_complete_route(
        route,
        ((10.5, 0.5, 0.5), (11.5, 0.5, 0.5)),
        source_point_offset=2,
    )

    assert route == original


def test_complete_certified_route_becomes_the_published_route():
    route = {
        "footprint_cell_size": 1.0,
        "selection_method": "physical_endpoint_diameter_v1",
        "cells": [0, 0, 1, 0],
        "points": [0.5, 0.5, 0.5, 1.5, 0.5, 0.5],
        "y_ranges": [0.0, 1.0] * 2,
        "clearance_margins": [1.0, 1.0],
    }

    _publish_certified_complete_route(
        route,
        ((0.5, 0.5, 0.5), (1.5, 0.5, 0.5)),
        source_point_offset=0,
    )

    assert route["points"] == [0.5, 0.5, 0.5, 1.5, 0.5, 0.5]
    assert route["cells"] == [0, 0, 1, 0]
    assert route["length_m"] == 1.0
    assert route["selection_method"] == "physical_endpoint_diameter_v1"
    assert route["certified_ingress_hint_index"] == 0
    assert route["source_route_point_count"] == 2
    assert route["point_source"] == "prepared_mesh_graph_path_v1"
    assert route["certified_start_position"] == [0.5, 0.5, 0.5]
    assert "y_ranges" not in route
    assert "clearance_margins" not in route


def test_horizontal_terminal_candidates_ignore_endpoint_y_and_keep_layers():
    graph = SparseCubicVoxelGraph.from_keys(
        ((0, -40, 0), (0, 0, 0), (0, 40, 0), (8, 0, 0)),
        voxel_size_m=1.0,
        vertical_voxel_size_m=0.25,
    )

    candidates = _horizontal_cubic_voxel_candidates(
        graph,
        (0.5, 10_000.0, 0.5),
        max_distance_m=0.1,
        limit=3,
    )

    assert {key for key, _distance in candidates} == {
        (0, -40, 0),
        (0, 0, 0),
        (0, 40, 0),
    }
    assert all(distance == pytest.approx(0.0) for _key, distance in candidates)


def test_component_vertical_gap_seed_payload_maps_cells_to_world_points():
    parsed = _component_vertical_gap_seed_points(
        [0, 0, -1.25, 0, 0, 2.75, 1, 0, 4.0],
        component_cells={(0, 0), (1, 0)},
        cell_size=2.0,
    )

    assert parsed == {
        (0, 0): ((1.0, -1.25, 1.0), (1.0, 2.75, 1.0)),
        (1, 0): ((3.0, 4.0, 1.0),),
    }


def test_surface_gap_waypoints_keep_only_selected_component_layers():
    graph = SparseCubicVoxelGraph.from_keys(
        ((0, 0, 0), (1, 0, 0), (2, 0, 0)),
        voxel_size_m=1.0,
        vertical_voxel_size_m=0.25,
    )

    groups = _surface_gap_cubic_waypoint_key_groups(
        graph,
        route_cells=((0, 0), (1, 0), (2, 0)),
        vertical_gap_seeds={
            (1, 0): (
                (1.5, 0.125, 0.5),
                (1.5, 10.125, 0.5),
            )
        },
    )

    assert groups == (((1, 0, 0),),)


def test_surface_gap_interval_gate_uses_free_key_far_from_midpoint_only_in_cell():
    graph = SparseCubicVoxelGraph.from_keys(
        (
            (0, 0, 0),
            (1, 20, 0),  # Near the middle interval, but in the prior cell.
            (3, 0, 0),  # Valid free key far below the wide-gap midpoint.
            (3, 100, 0),  # Correct cell, but outside every bounded interval.
            (4, 0, 0),
        ),
        voxel_size_m=1.0,
        vertical_voxel_size_m=0.25,
    )
    details: dict[str, object] = {}

    groups = _surface_gap_interval_route_key_groups(
        graph,
        route_cells=((0, 0), (1, 0), (2, 0)),
        vertical_gap_intervals={
            (0, 0): ((0.0, 1.0),),
            (1, 0): ((0.0, 10.0),),
            (2, 0): ((0.0, 1.0),),
        },
        footprint_cell_size_m=2.0,
        diagnostics=details,
    )

    assert groups is not None
    assert groups[1] == ((3, 0, 0),)
    assert (1, 20, 0) not in groups[1]
    assert (3, 100, 0) not in groups[1]
    assert details["surface_gap_gate_reason"] == "complete"


def test_surface_gap_terminal_keys_exclude_an_unbounded_lower_layer():
    graph = SparseCubicVoxelGraph.from_keys(
        ((0, -177, 0), (0, 19, 0)),
        voxel_size_m=1.0,
        vertical_voxel_size_m=0.25,
    )

    keys = _surface_gap_interval_terminal_keys(
        graph,
        terminal_cell=(0, 0),
        vertical_gap_intervals={(0, 0): ((0.582, 9.325),)},
        footprint_cell_size_m=10.0,
    )

    assert keys == ((0, 19, 0),)


def test_surface_gap_terminal_keys_enforce_requested_endpoint_cap():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((x, 0, 0) for x in range(200)),
        voxel_size_m=1.0,
        vertical_voxel_size_m=0.25,
    )


def test_interval_component_keeps_source_and_terminal_distance_caps():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((x, 0, 0) for x in range(200)),
        voxel_size_m=1.0,
        vertical_voxel_size_m=0.25,
    )

    component, details = _select_terminal_cubic_component(
        graph,
        ((0.5, 0.125, 0.5), (100.5, 0.125, 0.5)),
        terminal_snap_distance_m=8.0,
        ingress_snap_distance_m=8.0,
        max_component_voxels=256,
        require_original_ingress=True,
        source_ingress_point=(0.5, 0.125, 0.5),
        source_ingress_snap_distance_m=8.0,
        required_route_cells=((0, 0), (1, 0)),
        required_vertical_gap_intervals={
            (0, 0): ((0.0, 1.0),),
            (1, 0): ((0.0, 1.0),),
        },
        required_footprint_cell_size_m=100.0,
    )

    assert component is not None
    assert details["ingress_graph_key"] == [0, 0, 0]
    assert details["terminal_graph_key"] == [100, 0, 0]
    assert details["source_ingress_attachment_distance_m"] == pytest.approx(0.0)
    assert details["terminal_snap_distance_m"] == pytest.approx(0.0)

    keys = _surface_gap_interval_terminal_keys(
        graph,
        terminal_cell=(1, 0),
        vertical_gap_intervals={(1, 0): ((0.0, 1.0),)},
        footprint_cell_size_m=100.0,
        terminal_point=(100.5, 0.125, 0.5),
        max_horizontal_distance_m=8.0,
    )

    assert keys
    assert keys[0] == (100, 0, 0)
    assert (149, 0, 0) not in keys
    assert all(
        math.hypot(
            graph.voxel_center(key)[0] - 100.5,
            graph.voxel_center(key)[2] - 0.5,
        )
        <= 8.0 + 1e-9
        for key in keys
    )


def test_terminal_component_discovery_is_not_starved_by_nearer_pocket():
    source = tuple((x, 0, 50) for x in range(251))
    nearer_pocket = tuple(
        (x, 0, z)
        for x in range(245, 256)
        for z in range(11)
    )
    graph = SparseCubicVoxelGraph.from_keys(
        (*source, *nearer_pocket),
        voxel_size_m=1.0,
        vertical_voxel_size_m=0.25,
    )

    component, details = _select_terminal_cubic_component(
        graph,
        (
            (0.5, 0.125, 50.5),
            (150.5, 0.125, 50.5),
            (250.5, 0.125, 0.5),
        ),
        terminal_snap_distance_m=80.0,
        ingress_snap_distance_m=24.0,
        max_component_voxels=1_000,
        require_original_ingress=True,
        source_ingress_point=(0.5, 0.125, 50.5),
        source_ingress_snap_distance_m=24.0,
        source_ingress_gap_y_ranges={
            (0, 0): (0.0, 1.0),
            (1, 0): (0.0, 1.0),
            (2, 0): (0.0, 1.0),
        },
        source_ingress_footprint_cell_size_m=100.0,
        required_route_cells=((0, 0), (1, 0), (2, 0)),
        required_vertical_gap_intervals={
            (0, 0): ((0.0, 1.0),),
            (1, 0): ((0.0, 1.0),),
            (2, 0): ((0.0, 1.0),),
        },
        required_footprint_cell_size_m=100.0,
    )

    assert component is not None
    assert set(component.keys()) == set(source)
    assert component.free_voxel_count == 251
    assert details["terminal_interval_candidate_count"] == 172
    assert details["terminal_component_candidate_count"] == 2
    assert details["reason"] == "cubic_terminal_component_selected"
    assert details["ingress_reached"] is True
    assert details["ingress_graph_key"] == [0, 0, 50]
    assert details["terminal_graph_key"] == [250, 0, 50]
    assert details["terminal_graph_key_candidate_count"] == 51
    assert details["surface_gap_gate_reason"] == "complete"
    assert details["missing_surface_gap_gate_indices"] == []
    assert details["source_ingress_attachment_distance_m"] == pytest.approx(
        0.0
    )


def test_surface_gap_transition_bridge_does_not_merge_stacked_layers():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((0, y, 0) for y in range(51))
        + ((1, 50, 0),)
        + tuple((2, y, 0) for y in range(51))
    )
    details: dict[str, object] = {}

    groups = _surface_gap_interval_route_key_groups(
        graph,
        route_cells=((0, 0), (1, 0), (2, 0)),
        vertical_gap_intervals={
            (0, 0): ((0.0, 1.0), (100.0, 101.0)),
            (1, 0): ((0.0, 1.0), (100.0, 101.0)),
            (2, 0): ((0.0, 1.0), (100.0, 101.0)),
        },
        footprint_cell_size_m=1.0,
        max_vertical_transition_m=24.0,
        diagnostics=details,
    )

    assert groups is None
    assert details["surface_gap_gate_reason"] == "component_candidates_missing"
    assert details["missing_surface_gap_gate_indices"] == [1]


def test_surface_gap_transition_bridge_uses_one_compatible_interval_pair():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((0, y, 0) for y in range(6))
        + ((1, 5, 0),)
        + tuple((2, y, 0) for y in range(5, 21))
    )
    details: dict[str, object] = {}

    groups = _surface_gap_interval_route_key_groups(
        graph,
        route_cells=((0, 0), (1, 0), (2, 0)),
        vertical_gap_intervals={
            (0, 0): ((0.0, 1.0),),
            (1, 0): ((10.0, 11.0),),
            (2, 0): ((20.0, 21.0),),
        },
        footprint_cell_size_m=1.0,
        max_vertical_transition_m=12.0,
        diagnostics=details,
    )

    assert groups is not None
    assert groups[1] == ((1, 5, 0),)
    assert details["surface_gap_gate_reason"] == (
        "complete_with_pairwise_transition_bridge"
    )
    assert details["surface_gap_transition_fallback_indices"] == [1]


def test_terminal_component_requires_every_interval_backed_route_cell():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((x, 0, 0) for x in range(3))
        + tuple((x, 2, 0) for x in range(3))
    )
    route_cells = ((0, 0), (1, 0), (2, 0))

    component, details = _select_terminal_cubic_component(
        graph,
        tuple((float(x) + 0.5, 100.0, 0.5) for x in range(3)),
        terminal_snap_distance_m=8.0,
        ingress_snap_distance_m=8.0,
        max_component_voxels=32,
        require_original_ingress=True,
        source_ingress_point=(0.5, 2.5, 0.5),
        source_ingress_snap_distance_m=24.0,
        required_route_cells=route_cells,
        required_vertical_gap_intervals={
            (0, 0): ((0.0, 3.0),),
            (1, 0): ((2.0, 3.0),),
            (2, 0): ((0.0, 3.0),),
        },
        required_footprint_cell_size_m=1.0,
    )

    assert component is not None
    assert component.keys() == tuple((x, 2, 0) for x in range(3))
    assert details["terminal_graph_key"] == [2, 2, 0]
    assert details["ingress_graph_key"] == [0, 2, 0]
    assert details["surface_gap_gate_reason"] == "complete"
    assert details["terminal_graph_key_candidates"] == [[2, 2, 0]]


def test_surface_gap_interval_gate_fails_closed_when_final_interval_is_missing():
    details: dict[str, object] = {}
    groups = _surface_gap_interval_route_key_groups(
        SparseCubicVoxelGraph.from_keys(
            ((0, 0, 0), (1, 0, 0), (2, 0, 0))
        ),
        route_cells=((0, 0), (1, 0), (2, 0)),
        vertical_gap_intervals={
            (0, 0): ((0.0, 1.0),),
            (1, 0): ((0.0, 1.0),),
        },
        footprint_cell_size_m=1.0,
        diagnostics=details,
    )

    assert groups is None
    assert details["surface_gap_gate_reason"] == "bounded_intervals_missing"
    assert details["missing_surface_gap_gate_indices"] == [2]
    assert details["missing_surface_gap_gate_cells"] == [[2, 0]]


def test_component_vertical_gap_interval_payload_is_all_or_nothing():
    parsed = _component_vertical_gap_intervals(
        [0, 0, -2.0, -1.0, 0, 0, 3.0, 5.0, 1, 0, 7.0, 8.0],
        component_cells={(0, 0), (1, 0)},
    )

    assert parsed == {
        (0, 0): ((-2.0, -1.0), (3.0, 5.0)),
        (1, 0): ((7.0, 8.0),),
    }
    assert _component_vertical_gap_intervals(
        [0, 0, -2.0, -1.0, 99, 99, 0.0, 1.0],
        component_cells={(0, 0)},
    ) == {}


def test_source_connected_gap_layer_follows_obj_entrance_not_stacked_route_y():
    intervals = {
        (0, 0): ((0.0, 1.0), (10.0, 11.0)),
        (1, 0): ((0.5, 1.5), (10.0, 11.0)),
        (2, 0): ((1.0, 2.0), (10.0, 11.0)),
        (1, 1): ((0.5, 1.5), (10.0, 11.0)),
    }

    seeds, y_ranges = _source_connected_vertical_gap_layer(
        intervals,
        route_cells=((0, 0), (1, 0), (2, 0)),
        eligible_cells=set(intervals),
        source_ingress_anchor=(0.5, 0.5, 0.5),
        cell_size=1.0,
        max_attachment_distance_m=24.0,
        max_vertical_transition_m=2.0,
    )

    assert set(seeds) == set(intervals)
    assert [
        seeds[cell][0][1] for cell in ((0, 0), (1, 0), (2, 0))
    ] == pytest.approx([0.5, 1.0, 1.5])
    assert y_ranges[(1, 1)] == pytest.approx((0.5, 1.5))
    assert all(point[1] < 3.0 for points in seeds.values() for point in points)


def test_source_connected_gap_layer_fails_closed_at_layer_disappearance():
    intervals = {
        (0, 0): ((0.0, 1.0), (10.0, 11.0)),
        (1, 0): ((10.0, 11.0),),
        (2, 0): ((10.0, 11.0),),
    }

    seeds, y_ranges = _source_connected_vertical_gap_layer(
        intervals,
        route_cells=((0, 0), (1, 0), (2, 0)),
        eligible_cells=set(intervals),
        source_ingress_anchor=(0.5, 0.5, 0.5),
        cell_size=1.0,
        max_attachment_distance_m=2.0,
        max_vertical_transition_m=1.0,
    )

    assert seeds == {}
    assert y_ranges == {}


def test_source_connected_gap_layer_never_skips_missing_route_cell():
    intervals = {
        (0, 0): ((0.0, 1.0),),
        (2, 0): ((0.0, 1.0),),
    }

    assert _source_connected_vertical_gap_layer(
        intervals,
        route_cells=((0, 0), (1, 0), (2, 0)),
        eligible_cells={(0, 0), (1, 0), (2, 0)},
        source_ingress_anchor=(0.5, 0.5, 0.5),
        cell_size=1.0,
        max_attachment_distance_m=24.0,
        max_vertical_transition_m=2.0,
    ) == ({}, {})


def test_route_transition_sampling_ranges_bridge_steep_cardinal_step():
    selected = {
        (0, 0): (0.0, 1.0),
        (1, 0): (10.0, 11.0),
        (1, 1): (30.0, 31.0),
    }

    expanded = _route_transition_sampling_y_ranges(
        selected,
        route_cells=((0, 0), (1, 0)),
    )

    assert expanded[(0, 0)] == (0.0, 1.0)
    assert expanded[(1, 0)] == (0.0, 11.0)
    assert expanded[(1, 1)] == (30.0, 31.0)


def test_route_transition_sampling_ranges_bridge_both_sides_of_middle_cell():
    expanded = _route_transition_sampling_y_ranges(
        {
            (0, 0): (0.0, 1.0),
            (1, 0): (10.0, 11.0),
            (2, 0): (20.0, 21.0),
        },
        route_cells=((0, 0), (1, 0), (2, 0)),
    )

    assert expanded[(0, 0)] == (0.0, 1.0)
    assert expanded[(1, 0)] == (0.0, 21.0)
    assert expanded[(2, 0)] == (10.0, 21.0)


def test_route_transition_sampling_ranges_add_cardinal_diagonal_support():
    selected = {
        (0, 0): (0.0, 1.0),
        (1, 1): (10.0, 11.0),
        (0, 1): (20.0, 21.0),
    }

    expanded = _route_transition_sampling_y_ranges(
        selected,
        route_cells=((0, 0), (1, 1)),
    )

    assert expanded[(0, 0)] == (0.0, 1.0)
    assert expanded[(1, 1)] == (0.0, 11.0)
    assert expanded[(0, 1)] == (0.0, 21.0)
    assert _route_transition_sampling_y_ranges(
        {(0, 0): (0.0, 1.0), (1, 1): (10.0, 11.0)},
        route_cells=((0, 0), (1, 1)),
    ) == {}


def test_mesh_anchor_quantization_keeps_highest_clearance_candidate():
    metrics = {
        (0, 0, 0): NavigationVoxel3DMetric(
            center=(0.2, 0.5, 0.2),
            footprint_cell=(0, 0),
            available_volume_m3=1.0,
            free_voxel_count=1,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=0.0,
        ),
        (1, 0, 0): NavigationVoxel3DMetric(
            center=(2.2, 0.5, 0.2),
            footprint_cell=(0, 0),
            available_volume_m3=1.0,
            free_voxel_count=1,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=2.0,
        ),
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(2.0, 1.0, 2.0),
    )

    class _AlwaysFreeAtlas:
        @staticmethod
        def probe_point(point, *, include_clearance):
            assert include_clearance is True
            return True, 2.0 if point == route_start else 1.0

    route_start = (0.8, 0.5, 0.8)
    anchors, _details = _mesh_spine_roadmap_anchors(
        graph,
        ((0, 0, 0), (1, 0, 0)),
        atlas=_AlwaysFreeAtlas(),
        allowed_cells={(0, 0)},
        route_seed_points=(route_start, (2.8, 0.5, 0.8)),
        footprint_cell_size_m=8.0,
        horizontal_spacing_m=2.0,
        vertical_spacing_m=1.0,
        entry_anchor_radius_m=2.0,
    )
    selected = next(
        anchor
        for anchor in anchors
        if anchor.point[0] // 2.0 == 0
        and anchor.point[1] // 1.0 == 0
        and anchor.point[2] // 2.0 == 0
    )

    assert selected.point == route_start


def test_truncated_sampling_marks_only_unrepresented_footprint_frontier():
    metrics = {
        (0, 0, 0): NavigationVoxel3DMetric(
            center=(0.5, 0.5, 0.5),
            footprint_cell=(0, 0),
            available_volume_m3=1.0,
            free_voxel_count=1,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=0.0,
        ),
        (1, 0, 0): NavigationVoxel3DMetric(
            center=(1.5, 0.5, 0.5),
            footprint_cell=(1, 0),
            available_volume_m3=1.0,
            free_voxel_count=1,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=1.0,
        ),
    }

    unknown = _true_3d_unknown_boundary_keys(
        metrics,
        component_cell_set={(0, 0), (1, 0), (2, 0)},
        sampling_truncated=True,
    )

    assert unknown == {(1, 0, 0)}


def test_coarsened_graph_does_not_mark_sampled_footprint_as_unknown():
    metrics = {
        (0, 0, 0): NavigationVoxel3DMetric(
            center=(0.5, 0.5, 0.5),
            footprint_cell=(0, 0),
            available_volume_m3=1.0,
            free_voxel_count=1,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=0.0,
        ),
    }

    unknown = _true_3d_unknown_boundary_keys(
        metrics,
        component_cell_set={(0, 0)},
        covered_footprint_cells={(0, 0)},
        sampling_truncated=True,
    )

    assert unknown == set()


def test_cache_graph_buckets_large_filled_sample_sets_before_materialization():
    assert _cache_graph_base_grid_size(
        1_000,
        base_voxel_size=1.0,
        max_nodes=262_144,
    ) == (1.0, 1.0, 1.0)
    assert _cache_graph_base_grid_size(
        7_500_000,
        base_voxel_size=1.0,
        max_nodes=262_144,
    ) == (16.0, 1.0, 16.0)


def test_fine_seed_uses_prepared_easiest_terminal_spine_not_centerline():
    metrics = {
        (index, 0, 0): NavigationVoxel3DMetric(
            center=(float(index) + 0.5, 0.5, 0.5),
            footprint_cell=(index, 0),
            available_volume_m3=1.0,
            free_voxel_count=1,
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

    seeds, details = _fine_prepared_graph_seed_points(
        graph,
        route_points=(
            (0.5, 100.0, 0.5),
            (1.5, 100.0, 0.5),
            (2.5, 100.0, 0.5),
        ),
        selected_regions=(),
        max_tiles=4,
        fine_tile_radius_m=1.0,
    )

    assert details["fine_route_seed_method"] == (
        "prepared_easiest_terminal_graph_spine_v1"
    )
    assert details["fine_graph_spine_coverage_complete"] is True
    assert seeds[0] == (0.5, 0.5, 0.5)
    assert seeds[-1] == (2.5, 0.5, 0.5)


def test_fine_seed_coverage_reports_missing_persisted_tile():
    tile = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(0.0, 0.0, 0.0),
        shape=(2, 2, 2),
        surface_cells=frozenset(),
        triangle_count=0,
        surface_sample_count=0,
        sampling_truncated=False,
        max_clearance_search_cells=2,
    )

    details = _fine_seed_tile_coverage_details(
        ((0.5, 0.5, 0.5), (3.5, 0.5, 0.5)),
        (tile,),
    )

    assert details["fine_built_tile_seed_coverage_complete"] is False
    assert details["fine_built_tile_uncovered_seed_count"] == 1
    assert details["fine_built_tile_uncovered_seed_examples"] == [
        [3.5, 0.5, 0.5]
    ]


def test_cache_time_builds_model_and_volume_metrics(tmp_path):
    navigation = {
        "routes": [_route("centerline-0", scale=1.0)],
        "recommended_route_id": "centerline-0",
    }
    manifest = {"footprint_cell_size": 1.0}

    result = build_navigation_voxel_cache(
        manifest,
        navigation,
        triangle_provider=_floor_provider,
        mesh_edge_is_clear=_always_clear_edge,
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            max_cells=4096,
            max_surface_samples=4096,
        ),
    )

    assert result.built_route_count == 1
    summary = navigation["routes"][0]["voxel_corridor"]
    assert summary["built"] is True
    assert summary["outcome"] == "built"
    assert summary["available_volume_m3"] > 0.0
    assert summary["volume_per_route_m"] > 0.0
    assert navigation["routes"][0]["component_cells"]
    assert "surface_cells" not in summary
    model = result.payload["routes"]["centerline-0"]["model"]
    restored = deserialize_local_voxel_volume(model)
    assert restored.voxel_count <= 4096
    assert restored.surface_cells

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / NAVIGATION_VOXEL_CACHE_NAME).write_text(
        json.dumps(result.payload),
        encoding="utf-8",
    )
    navigation.update(
        {
            "version": NAVIGATION_METADATA_VERSION,
            "method": NAVIGATION_METADATA_METHOD,
        }
    )
    manifest["navigation"] = navigation
    cached_path = cached_centerline_path(manifest, cache_dir=str(cache_dir))
    assert cached_path is not None
    assert cached_path.cached_voxel_volume is not None
    assert cached_path.cached_voxel_metrics is not None
    assert cached_path.cached_voxel_metrics["available_volume_m3"] > 0.0


def test_v11_does_not_publish_without_an_exact_mesh_terminal_path():
    navigation = {"routes": [_route("centerline-0", scale=1.0)]}

    result = build_navigation_voxel_cache(
        {"footprint_cell_size": 1.0},
        navigation,
        triangle_provider=_floor_provider,
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            max_cells=4096,
            max_surface_samples=4096,
        ),
    )

    summary = navigation["routes"][0]["voxel_corridor"]
    assert result.built_route_count == 0
    assert result.chunked_payload is None
    assert summary["built"] is False
    assert summary["outcome"] == "known_terminal_unreachable"
    assert summary["prepared_mesh_graph"]["reason"] == (
        "mesh_edge_guard_unavailable"
    )


def test_obj_source_anchor_reserved_schema_fails_closed():
    valid = {
        "position": [0.0, 2.0, 0.5],
        "kind": "obj_surface_vertex",
        "source": "fixture.obj",
        "source_vertex_index": 0,
        "source_order": "obj_declaration_order",
        "executable": False,
        "attachment_required": True,
        "attachment_coordinate_space": "xyz",
    }

    assert voxel_cache_module._imported_navigation_start_anchor({}) is None
    assert voxel_cache_module._imported_navigation_start_anchor(
        {"navigation_start_anchor": valid}
    ) == (0.0, 2.0, 0.5)
    malformed = (
        None,
        {**valid, "kind": "unknown"},
        {**valid, "source_vertex_index": "0"},
        {**valid, "position": (0.0, 2.0, 0.5)},
        {**valid, "position": [False, 2.0, 0.5]},
        {key: value for key, value in valid.items() if key != "source"},
        {**valid, "unexpected": True},
    )
    for anchor in malformed:
        with pytest.raises(ValueError, match="OBJ navigation start anchor"):
            voxel_cache_module._imported_navigation_start_anchor(
                {"navigation_start_anchor": anchor}
            )


def test_v11_obj_wall_anchor_is_never_an_executable_route_point():
    wall_anchor = (0.0, 2.0, 0.5)
    checked_edges: list[tuple[tuple[float, ...], tuple[float, ...]]] = []

    def wall_provider(bounds_min, bounds_max):
        if not all(
            bounds_min[axis] - 1e-9
            <= wall_anchor[axis]
            <= bounds_max[axis] + 1e-9
            for axis in range(3)
        ):
            return ()
        return (
            np.asarray(
                [[wall_anchor, (0.0, 0.0, 0.0), (0.0, 4.0, 0.0)]],
                dtype=np.float64,
            ),
        )

    def guarded_edge(first, second):
        edge = (tuple(first), tuple(second))
        checked_edges.append(edge)
        return wall_anchor not in edge

    route = _route("centerline-0", scale=1.0)
    route["starts_at_navigation_start_anchor"] = True
    flat_component_cells = route["component_cells"]
    route["component_vertical_gap_intervals"] = [
        value
        for index in range(0, len(flat_component_cells), 2)
        for value in (
            flat_component_cells[index],
            flat_component_cells[index + 1],
            0.0,
            4.0,
        )
    ]
    navigation = {
        "navigation_start_anchor": {
            "position": list(wall_anchor),
            "kind": "obj_surface_vertex",
            "source": "fixture.obj",
            "source_vertex_index": 0,
            "source_order": "obj_declaration_order",
            "executable": False,
            "attachment_required": True,
            "attachment_coordinate_space": "xyz",
        },
        "routes": [route],
    }

    result = build_navigation_voxel_cache(
        {"footprint_cell_size": 1.0},
        navigation,
        triangle_provider=wall_provider,
        mesh_edge_is_clear=guarded_edge,
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            max_cells=4096,
            max_surface_samples=4096,
            mesh_graph_entry_anchor_radius_m=240.0,
        ),
    )

    assert result.built_route_count == 1
    route = navigation["routes"][0]
    summary = route["voxel_corridor"]
    cubic = summary["cubic_component"]
    prepared = summary["prepared_mesh_graph"]
    assert cubic["ingress_selection"] == (
        "strict_navigation_source_ingress_v2"
    )
    assert cubic["source_ingress_snap_limit_m"] == 24.0
    assert cubic["source_ingress_attachment_distance_m"] <= 24.0
    assert prepared["source_ingress_snap_limit_m"] == 24.0
    assert prepared["source_ingress_attachment_distance_m"] <= 24.0
    assert math.dist(
        tuple(navigation["navigation_start_anchor"]["position"]),
        tuple(route["certified_start_position"]),
    ) <= 24.0
    executable_points = tuple(
        tuple(route["points"][index : index + 3])
        for index in range(0, len(route["points"]), 3)
    )
    assert wall_anchor not in executable_points
    assert tuple(route["certified_start_position"]) != wall_anchor
    assert checked_edges
    assert all(wall_anchor not in edge for edge in checked_edges)


def test_obj_transition_envelope_certifies_steep_route_without_route_y():
    component_cells = ((0, 0), (1, 0), (2, 0))
    route = {
        "id": "centerline-0",
        "closed_loop": False,
        "starts_at_navigation_start_anchor": True,
        "footprint_cell_size": 1.0,
        "component_cells": [
            value for cell in component_cells for value in cell
        ],
        "cells": [value for cell in component_cells for value in cell],
        "length_m": 2.0,
        # Deliberately contradictory imported heights: topology must come
        # only from the surface-gap intervals below.
        "points": [
            0.5,
            100.0,
            0.5,
            1.5,
            -100.0,
            0.5,
            2.5,
            100.0,
            0.5,
        ],
        "component_vertical_gap_intervals": [
            0,
            0,
            0.0,
            1.0,
                1,
                0,
                2.0,
                3.0,
                2,
                0,
                4.0,
                5.0,
        ],
    }
    navigation = {
        "navigation_start_anchor": {
            "position": [0.5, 0.5, 0.5],
            "kind": "obj_surface_vertex",
            "source": "fixture.obj",
            "source_vertex_index": 0,
            "source_order": "obj_declaration_order",
            "executable": False,
            "attachment_required": True,
            "attachment_coordinate_space": "xyz",
        },
        "routes": [route],
    }

    result = build_navigation_voxel_cache(
        {"footprint_cell_size": 1.0},
        navigation,
        triangle_provider=lambda _lower, _upper: (),
        mesh_edge_is_clear=_always_clear_edge,
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            vertical_voxel_size_m=0.25,
            tile_size_m=2.0,
            max_tiles=32,
            max_cells=4096,
            max_surface_samples=4096,
        ),
    )

    assert result.built_route_count == 1
    summary = route["voxel_corridor"]
    assert summary["built"] is True
    assert summary["cubic_graph"]["source_free_space_method"] == (
        "all_filtered_non_surface_cells_v1"
    )
    assert summary["certified_ingress_hint_index"] == 0
    assert summary["certified_terminal_hint_index"] == 2
    assert summary["cubic_component"][
        "source_ingress_gap_envelope_required"
    ] is True


def test_cache_time_publishes_graph_index_and_lazy_voxel_chunks(tmp_path):
    component_cells = ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    route = {
        "id": "centerline-0",
        "footprint_cell_size": 1.0,
        "component_cells": [value for cell in component_cells for value in cell],
        "cells": [value for cell in component_cells for value in cell],
        "component_vertical_gap_seeds": [
            value
            for cell in component_cells
            for value in (cell[0], cell[1], 2.0)
        ],
        "component_vertical_gap_intervals": [
            value
            for cell in component_cells
            for value in (cell[0], cell[1], 0.0, 4.0)
        ],
        "component_y_ranges": [
            value
            for _cell in component_cells
            for value in (0.0, 4.0)
        ],
        "length_m": 4.0,
        "starts_at_navigation_start": True,
        "points": [
            value
            for cell in component_cells
            for value in (float(cell[0]) + 0.5, 2.0, float(cell[1]) + 0.5)
        ],
    }
    navigation = {
        "navigation_start": {"position": [0.5, 2.0, 0.5]},
        "routes": [route],
        "recommended_route_id": "centerline-0",
        "version": NAVIGATION_METADATA_VERSION,
        "method": NAVIGATION_METADATA_METHOD,
    }
    manifest = {
        "footprint_cell_size": 1.0,
        "navigation": navigation,
    }
    result = build_navigation_voxel_cache(
        manifest,
        navigation,
        triangle_provider=_floor_provider,
        mesh_edge_is_clear=_always_clear_edge,
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            tile_size_m=2.0,
            max_tiles=8,
            max_cells=4096,
            max_surface_samples=4096,
        ),
    )

    assert result.chunked_payload is not None
    route_model = result.chunked_payload["routes"]["centerline-0"]["model"]
    assert "tiles" not in route_model
    assert "fine_tiles" not in route_model
    assert route_model["chunk_store"]["chunk_count"] == len(
        result.chunk_payloads
    )
    assert all(
        chunk["voxel_size_m"] == 1.0
        and chunk["sampling_truncated"] is False
        for chunk in result.chunk_payloads.values()
    )
    assert all(
        chunk["voxel_size_m"] == 1.0
        for chunk in route_model["chunk_store"]["chunks"]
    )
    descriptor = navigation["voxel_cache"]
    assert descriptor["storage_method"] == "navigation_voxel_chunks_v1"

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    for relative_path, chunk_payload in result.chunk_payloads.items():
        chunk_path = cache_dir / relative_path
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_path.write_text(json.dumps(chunk_payload), encoding="utf-8")
    (cache_dir / NAVIGATION_VOXEL_CACHE_NAME).write_text(
        json.dumps(result.chunked_payload),
        encoding="utf-8",
    )

    restored = load_cached_navigation_voxel_volume(
        str(cache_dir),
        manifest,
        "centerline-0",
    )
    assert isinstance(restored, NavigationVoxelAtlas)
    assert restored.tiles == ()
    assert restored.fine_tiles == ()
    assert restored.has_prepared_3d_graph is True
    assert restored.chunk_store is not None
    assert restored.chunk_store.stats()["backend"] == "disk_lru"
    assert restored.chunk_store.stats()["resident_chunk_count"] == 0
    assert restored.fine_tile_for_point((0.5, 2.0, 0.5)) is not None
    assert restored.chunk_store.stats()["resident_chunk_count"] >= 1
    assert restored.probe_point((0.5, 2.0, 0.5)) is not None

    certificate_cache_dir = tmp_path / "certificate-cache"
    certificate_dir = certificate_cache_dir / "navigation_certificate"
    certificate_dir.mkdir(parents=True)
    for relative_path, chunk_payload in result.chunk_payloads.items():
        chunk_path = certificate_dir / relative_path
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_path.write_text(json.dumps(chunk_payload), encoding="utf-8")
    (certificate_dir / NAVIGATION_VOXEL_CACHE_NAME).write_text(
        json.dumps(result.chunked_payload),
        encoding="utf-8",
    )
    certificate_navigation = copy.deepcopy(navigation)
    certificate_navigation["voxel_cache"].update(
        {
            "path": "navigation_certificate/navigation_voxels.json",
            "chunk_directory": "navigation_certificate/navigation_voxel_chunks",
        }
    )
    certificate_manifest = dict(manifest)
    certificate_manifest["navigation"] = certificate_navigation

    certificate_restored = load_cached_navigation_voxel_volume(
        str(certificate_cache_dir),
        certificate_manifest,
        "centerline-0",
    )
    assert isinstance(certificate_restored, NavigationVoxelAtlas)
    assert certificate_restored.chunk_store is not None
    assert certificate_restored.fine_tile_for_point((0.5, 2.0, 0.5)) is not None

    bad_cache_dir = tmp_path / "bad-cache"
    bad_cache_dir.mkdir()
    first_relative_path = next(iter(result.chunk_payloads))
    for relative_path, chunk_payload in result.chunk_payloads.items():
        bad_payload = dict(chunk_payload)
        if relative_path == first_relative_path:
            bad_payload["sampling_truncated"] = True
        chunk_path = bad_cache_dir / relative_path
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_path.write_text(json.dumps(bad_payload), encoding="utf-8")
    (bad_cache_dir / NAVIGATION_VOXEL_CACHE_NAME).write_text(
        json.dumps(result.chunked_payload),
        encoding="utf-8",
    )
    bad_restored = load_cached_navigation_voxel_volume(
        str(bad_cache_dir),
        manifest,
        "centerline-0",
    )
    assert isinstance(bad_restored, NavigationVoxelAtlas)
    assert bad_restored.chunk_store is not None
    first_descriptor = next(
        descriptor
        for descriptor in bad_restored.chunk_store.descriptors()
        if descriptor.relative_path == first_relative_path
    )
    assert bad_restored.chunk_store.get_chunk(first_descriptor.chunk_id) is None
    assert bad_restored.chunk_store.stats()["load_errors"] == 1


def test_cache_time_route_selection_prefers_longest_certified_route():
    navigation = {
        "routes": [
            _route("narrow", scale=1.0),
            _route("wide", scale=4.0),
        ],
        "recommended_route_id": "narrow",
    }

    result = build_navigation_voxel_cache(
        {"footprint_cell_size": 1.0},
        navigation,
        triangle_provider=_floor_provider,
        mesh_edge_is_clear=_always_clear_edge,
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            max_cells=20_000,
            max_surface_samples=4096,
        ),
    )

    assert result.built_route_count == 2
    assert result.recommended_route_id == "wide"
    assert navigation["recommended_route_id"] == "wide"
    assert navigation["route_selection_method"] == (
        "longest_safe_non_circular_certified_route_v1"
    )
    assert (
        navigation["routes"][1]["voxel_corridor"]["available_volume_m3"]
        > navigation["routes"][0]["voxel_corridor"]["available_volume_m3"]
    )


def test_obj_anchor_selects_longest_exact_safe_non_circular_route():
    navigation = {
        "navigation_start_anchor": _strict_obj_start_anchor(),
        "routes": [
            {
                "id": "short-clear",
                "closed_loop": False,
                "starts_at_navigation_start_anchor": True,
            },
            {
                "id": "long-narrow",
                "closed_loop": False,
                "starts_at_navigation_start_anchor": True,
            },
            {
                "id": "long-clear",
                "closed_loop": False,
                "starts_at_navigation_start_anchor": True,
            },
            {
                "id": "large-loop",
                "closed_loop": True,
                "starts_at_navigation_start_anchor": True,
            },
        ],
    }
    for route in navigation["routes"]:
        route["certified_start_position"] = [0.5, 2.0, 0.5]
    summaries = {
        "short-clear": _exact_source_anchor_summary(
            length_m=20.0,
            min_clearance_m=4.0,
            obj_anchor=True,
        ),
        "long-narrow": _exact_source_anchor_summary(
            length_m=80.0,
            min_clearance_m=0.5,
            obj_anchor=True,
        ),
        "long-clear": _exact_source_anchor_summary(
            length_m=80.0,
            min_clearance_m=1.5,
            obj_anchor=True,
        ),
        "large-loop": _exact_source_anchor_summary(
            length_m=200.0,
            min_clearance_m=8.0,
            obj_anchor=True,
        ),
    }

    selected = voxel_cache_module._select_recommended_route_id(
        navigation,
        summaries,
    )

    assert selected == "long-clear"


def test_navigation_start_selects_longest_exact_safe_non_circular_route():
    navigation = {
        "navigation_start": {"position": [0.5, 2.0, 0.5]},
        "routes": [
            {
                "id": "short-clear",
                "closed_loop": False,
                "starts_at_navigation_start": True,
            },
            {
                "id": "long-narrow",
                "closed_loop": False,
                "starts_at_navigation_start": True,
            },
            {
                "id": "long-clear",
                "closed_loop": False,
                "starts_at_navigation_start": True,
            },
            {
                "id": "large-loop",
                "closed_loop": True,
                "starts_at_navigation_start": True,
            },
            {
                "id": "remote",
                "closed_loop": False,
                "starts_at_navigation_start": False,
            },
        ],
    }
    for route in navigation["routes"]:
        route["certified_start_position"] = [0.5, 2.0, 0.5]
    summaries = {
        "short-clear": _exact_source_anchor_summary(
            length_m=20.0,
            min_clearance_m=4.0,
        ),
        "long-narrow": _exact_source_anchor_summary(
            length_m=80.0,
            min_clearance_m=0.5,
        ),
        "long-clear": _exact_source_anchor_summary(
            length_m=80.0,
            min_clearance_m=1.5,
        ),
        "large-loop": _exact_source_anchor_summary(
            length_m=200.0,
            min_clearance_m=8.0,
        ),
        "remote": _exact_source_anchor_summary(
            length_m=300.0,
            min_clearance_m=8.0,
        ),
    }

    selected = voxel_cache_module._select_recommended_route_id(
        navigation,
        summaries,
    )

    assert selected == "long-clear"


def test_navigation_start_rejects_longer_incomplete_certified_prefix():
    navigation = {
        "navigation_start": {"position": [0.5, 2.0, 0.5]},
        "routes": [
            {
                "id": "complete",
                "closed_loop": False,
                "starts_at_navigation_start": True,
            },
            {
                "id": "incomplete-prefix",
                "closed_loop": False,
                "starts_at_navigation_start": True,
            },
        ],
    }
    for route in navigation["routes"]:
        route["certified_start_position"] = [0.5, 2.0, 0.5]
    complete = _exact_source_anchor_summary(
        length_m=40.0,
        min_clearance_m=1.0,
    )
    incomplete = _exact_source_anchor_summary(
        length_m=400.0,
        min_clearance_m=4.0,
    )
    incomplete["source_route_point_count"] = 20
    incomplete["certified_terminal_hint_index"] = 8

    selected = voxel_cache_module._select_recommended_route_id(
        navigation,
        {
            "complete": complete,
            "incomplete-prefix": incomplete,
        },
    )

    assert selected == "complete"


def test_navigation_start_does_not_replace_capacity_limited_long_route_with_short_route():
    navigation = {
        "navigation_start": {"position": [0.5, 2.0, 0.5]},
        "routes": [
            {
                "id": "short",
                "length_m": 40.0,
                "closed_loop": False,
                "starts_at_navigation_start": True,
                "certified_start_position": [0.5, 2.0, 0.5],
            },
            {
                "id": "long-unresolved",
                "length_m": 400.0,
                "closed_loop": False,
                "starts_at_navigation_start": True,
            },
        ],
    }
    unresolved = {
        "built": False,
        "certified_route_length_m": 420.0,
        "prepared_mesh_graph": {
            "reason": "exact_cubic_spine_search_limit_reached",
            "node_limit_reached": True,
        },
    }

    selected = voxel_cache_module._select_recommended_route_id(
        navigation,
        {
            "short": _exact_source_anchor_summary(
                length_m=40.0,
                min_clearance_m=2.0,
            ),
            "long-unresolved": unresolved,
        },
    )

    assert selected is None


def test_navigation_start_allows_short_route_after_conclusive_long_route_rejection():
    navigation = {
        "navigation_start": {"position": [0.5, 2.0, 0.5]},
        "routes": [
            {
                "id": "short",
                "length_m": 40.0,
                "closed_loop": False,
                "starts_at_navigation_start": True,
                "certified_start_position": [0.5, 2.0, 0.5],
            },
            {
                "id": "long-blocked",
                "length_m": 400.0,
                "closed_loop": False,
                "starts_at_navigation_start": True,
            },
        ],
    }

    selected = voxel_cache_module._select_recommended_route_id(
        navigation,
        {
            "short": _exact_source_anchor_summary(
                length_m=40.0,
                min_clearance_m=2.0,
            ),
            "long-blocked": {
                "built": False,
                "certified_route_length_m": 420.0,
                "prepared_mesh_graph": {
                    "reason": "exact_cubic_spine_terminal_unreachable",
                    "node_limit_reached": False,
                },
            },
        },
    )

    assert selected == "short"


def test_runtime_route_contract_accepts_obj_anchor_with_spatial_chunks():
    anchor_navigation = {
        "navigation_start_anchor": _strict_obj_start_anchor(),
        "route_selection_method": (
            "longest_safe_non_circular_certified_route_v1"
        ),
        "recommended_route_id": "route",
        "routes": [
            {
                "id": "route",
                "closed_loop": False,
                "starts_at_navigation_start_anchor": True,
                "certified_start_position": [0.5, 2.0, 0.5],
                "voxel_corridor": _exact_source_anchor_summary(
                    length_m=40.0,
                    min_clearance_m=2.0,
                    obj_anchor=True,
                ),
            }
        ],
    }
    assert navigation_route_contract_rebuild_reason(
        anchor_navigation,
        manifest_chunks={
            "0_0_0": {
                "bounds_min": [0.0, 0.0, 0.0],
                "bounds_max": [1.0, 1.0, 1.0],
            }
        },
    ) is None


def test_runtime_route_contract_rejects_capacity_fallback():

    navigation = {
        "navigation_start": {"position": [0.5, 2.0, 0.5]},
        "route_selection_method": (
            "longest_safe_non_circular_certified_route_v1"
        ),
        "recommended_route_id": "short",
        "routes": [
            {
                "id": "short",
                "length_m": 40.0,
                "closed_loop": False,
                "starts_at_navigation_start": True,
                "voxel_corridor": {"built": True, "route_length_m": 40.0},
            },
            {
                "id": "long",
                "length_m": 400.0,
                "closed_loop": False,
                "starts_at_navigation_start": True,
                "voxel_corridor": {
                    "built": False,
                    "prepared_mesh_graph": {
                        "reason": "adaptive_mesh_known_terminal_unreachable",
                        "node_limit_reached": True,
                    },
                },
            },
        ],
    }

    assert navigation_route_contract_rebuild_reason(navigation) == (
        "longer_route_search_capacity_limited"
    )


def test_navigation_start_publishes_longest_safe_selection_method(
    monkeypatch,
):
    navigation = {
        "navigation_start": {"position": [0.5, 2.0, 0.5]},
        "recommended_route_id": "short",
        "routes": [
            {
                **_route("short", scale=1.0),
                "closed_loop": False,
                "starts_at_navigation_start": True,
                "selection_method": "navigation_start_to_farthest_endpoint_v1",
            },
            {
                **_route("long", scale=2.0),
                "closed_loop": False,
                "starts_at_navigation_start": True,
                "selection_method": "navigation_start_to_farthest_endpoint_v1",
            },
        ],
    }
    for route in navigation["routes"]:
        route["certified_start_position"] = [0.5, 2.0, 0.5]
    summaries = {
        "short": _exact_source_anchor_summary(
            length_m=10.0,
            min_clearance_m=2.0,
        ),
        "long": _exact_source_anchor_summary(
            length_m=40.0,
            min_clearance_m=0.5,
        ),
    }

    def fake_analyze_route(*_args, route_id, **_kwargs):
        return {
            **summaries[route_id],
            "certified_ingress_hint_index": 0,
            "_model": {"fixture": route_id},
        }

    monkeypatch.setattr(
        voxel_cache_module,
        "_analyze_route",
        fake_analyze_route,
    )

    result = build_navigation_voxel_cache(
        {"footprint_cell_size": 1.0},
        navigation,
        triangle_provider=_floor_provider,
        mesh_edge_is_clear=_always_clear_edge,
    )

    expected_method = "longest_safe_non_circular_certified_route_v1"
    assert result.recommended_route_id == "long"
    assert navigation["recommended_route_id"] == "long"
    assert navigation["route_selection_method"] == expected_method
    assert navigation["routes"][1]["selection_method"] == expected_method
    assert navigation["routes"][0]["selection_method"] == (
        "navigation_start_to_farthest_endpoint_v1"
    )


def test_obj_anchor_publishes_longest_safe_selection_method(
    monkeypatch,
):
    navigation = {
        "navigation_start_anchor": _strict_obj_start_anchor(),
        "recommended_route_id": "short",
        "routes": [
            {
                **_route("short", scale=1.0),
                "closed_loop": False,
                "starts_at_navigation_start_anchor": True,
                "selection_method": "physical_endpoint_diameter_v1",
            },
            {
                **_route("long", scale=2.0),
                "closed_loop": False,
                "starts_at_navigation_start_anchor": True,
                "selection_method": "physical_endpoint_diameter_v1",
            },
        ],
    }
    for route in navigation["routes"]:
        route["certified_start_position"] = [0.5, 2.0, 0.5]
    summaries = {
        "short": _exact_source_anchor_summary(
            length_m=10.0,
            min_clearance_m=2.0,
            obj_anchor=True,
        ),
        "long": _exact_source_anchor_summary(
            length_m=40.0,
            min_clearance_m=0.5,
            obj_anchor=True,
        ),
    }

    def fake_analyze_route(*_args, route_id, **_kwargs):
        return {
            **summaries[route_id],
            "certified_ingress_hint_index": 0,
            "_model": {"fixture": route_id},
        }

    monkeypatch.setattr(
        voxel_cache_module,
        "_analyze_route",
        fake_analyze_route,
    )

    result = build_navigation_voxel_cache(
        {"footprint_cell_size": 1.0},
        navigation,
        triangle_provider=_floor_provider,
        mesh_edge_is_clear=_always_clear_edge,
    )

    expected_method = "longest_safe_non_circular_certified_route_v1"
    assert result.recommended_route_id == "long"
    assert navigation["recommended_route_id"] == "long"
    assert navigation["route_selection_method"] == expected_method
    assert navigation["routes"][1]["selection_method"] == expected_method
    assert navigation["routes"][0]["selection_method"] == (
        "physical_endpoint_diameter_v1"
    )


def test_cache_time_voxel_atlas_certifies_one_terminal_corridor():
    component_cells = ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    route = {
        "id": "centerline-0",
        "footprint_cell_size": 1.0,
        "component_cells": [value for cell in component_cells for value in cell],
        "cells": [value for cell in component_cells for value in cell],
        "component_vertical_gap_seeds": [
            value
            for cell in component_cells
            for value in (cell[0], cell[1], 2.0)
        ],
        "component_vertical_gap_intervals": [
            value
            for cell in component_cells
            for value in (cell[0], cell[1], 0.0, 4.0)
        ],
        "component_y_ranges": [
            value
            for _cell in component_cells
            for value in (0.0, 4.0)
        ],
        "length_m": 4.0,
        "starts_at_navigation_start": True,
        "points": [
            value
            for cell in component_cells
            for value in (float(cell[0]) + 0.5, 2.0, float(cell[1]) + 0.5)
        ],
    }
    navigation = {"routes": [route]}

    result = build_navigation_voxel_cache(
        {"footprint_cell_size": 1.0},
        navigation,
        triangle_provider=_floor_provider,
        mesh_edge_is_clear=_always_clear_edge,
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            tile_size_m=2.0,
            max_tiles=8,
            max_cells=512,
            max_surface_samples=4096,
        ),
    )

    assert result.built_route_count == 1
    summary = route["voxel_corridor"]
    assert summary["coverage_scope"] == "certified_terminal_route"
    assert summary["coverage_includes_preceding_curvature"] is False
    assert summary["cubic_graph"]["source_free_space_method"] == (
        "all_filtered_non_surface_cells_v1"
    )
    assert summary["coverage_cell_count"] == len(component_cells)
    assert summary["tile_count"] >= 2
    model = result.payload["routes"]["centerline-0"]["model"]
    assert model["method"] == NAVIGATION_VOXEL_ATLAS_MODEL_METHOD
    assert model["fixed_isotropic_voxel_size_m"] == 1.0
    assert model["sampling_complete"] is True
    assert model["surface_overlap_policy"] == "occupied_wins"
    assert model["branch_lookahead_method"] == "voxel_branch_lookahead_v1"
    assert model["prepared_graph"] is None
    assert model["prepared_3d_graph"]["method"] == NAVIGATION_VOXEL_3D_GRAPH_METHOD
    restored = deserialize_local_voxel_volume(model)
    assert isinstance(restored, NavigationVoxelAtlas)
    assert len(restored.tiles) == summary["tile_count"]
    assert restored.bounds_min[0] <= 0.0
    assert restored.bounds_max[2] >= 3.0
    assert summary["navigation_graph_method"] == NAVIGATION_VOXEL_3D_GRAPH_METHOD
    assert summary["branch_lookahead_method"] == "voxel_branch_lookahead_v1"
    assert summary["navigation_cell_count"] >= len(component_cells)
    assert restored.navigation_cell_count >= len(component_cells)
    assert restored.has_prepared_graph is False
    assert restored.has_prepared_3d_graph is True
    assert restored.prepared_3d_graph is not None
    assert restored.prepared_3d_graph.max_edge_distance_cells == 1
    assert summary["prepared_3d_graph_role"] == (
        "compatibility_immediate_neighbors_only"
    )
    assert summary["cubic_component_probe_method"] == (
        "packed_free_key_minimum_clearance_v1"
    )
    assert summary["fine_tile_count"] == 0
    assert restored.fine_tiles == ()
    assert restored.fine_voxel_size_m == 1.0
    assert restored.fine_tile_for_point(route["points"][:3]) is not None


def test_prepared_3d_route_reroots_past_stale_camera_anchor():
    metrics = {
        (index, 0, 0): NavigationVoxel3DMetric(
            center=(float(index) + 0.5, 0.5, 0.5),
            footprint_cell=(index, 0),
            available_volume_m3=4.0,
            free_voxel_count=4,
            min_clearance_m=2.0,
            mean_clearance_m=2.0,
            progress_m=float(index),
        )
        for index in range(4)
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    current = (1.9, 0.5, 0.5)

    plan = atlas.plan_footprint_route(
        (),
        current_position=current,
        footprint_cell_size=1.0,
        preferred_direction=(1.0, 0.0, 0.0),
        lookahead_distance_m=2.0,
    )

    assert plan is not None
    assert plan.world_points[0] == pytest.approx(current)
    assert plan.world_points[1][0] > current[0]
    assert all(point[0] >= current[0] for point in plan.world_points[1:])


def test_filled_voxel_graph_rejects_terminal_branch_through_a_turn():
    cells = ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    metrics = {
        cell: NavigationVoxelCellMetric(
            available_volume_m3=100.0 - index,
            free_cell_count=10,
            min_clearance_m=2.0,
            mean_clearance_m=3.0,
            progress_m=float(index),
        )
        for index, cell in enumerate(cells)
    }
    atlas = NavigationVoxelAtlas(tiles=(), cell_metrics=metrics)
    events = []

    plan = atlas.plan_footprint_route(
        cells,
        current_position=(0.5, 2.0, 0.5),
        footprint_cell_size=1.0,
        preferred_direction=(1.0, 0.0, 0.0),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    assert plan is None
    rejection = [
        payload for event, payload in events if event == "voxel_route_rejected"
    ][-1]
    assert rejection["reason"] == "no_non_dead_end_branch"


def test_branch_lookahead_prefers_continuation_over_larger_dead_end_room():
    current = (0, 0)
    dead_end_room = (
        (1, 0),
        (2, 0),
        (3, 0),
        (1, -1),
        (2, -1),
        (3, -1),
    )
    continuing_passage = tuple((0, index) for index in range(1, 7))
    cells = (current, *dead_end_room, *continuing_passage)
    metrics = {
        current: NavigationVoxelCellMetric(1.0, 1, 1.0, 1.0, 0.0),
        **{
            cell: NavigationVoxelCellMetric(
                available_volume_m3=10_000.0,
                free_cell_count=100,
                min_clearance_m=8.0,
                mean_clearance_m=8.0,
                progress_m=float(index + 1),
            )
            for index, cell in enumerate(dead_end_room)
        },
        **{
            cell: NavigationVoxelCellMetric(
                available_volume_m3=1.0,
                free_cell_count=1,
                min_clearance_m=1.0,
                mean_clearance_m=1.0,
                progress_m=float(index + 1),
            )
            for index, cell in enumerate(continuing_passage)
        },
    }
    atlas = NavigationVoxelAtlas(tiles=(), cell_metrics=metrics)

    plan = atlas.plan_footprint_route(
        cells,
        current_position=(0.5, 2.0, 0.5),
        footprint_cell_size=1.0,
        preferred_direction=(1.0, 0.0, 0.0),
        lookahead_distance_m=4.0,
        lookahead_cells=8,
    )

    assert plan is not None
    assert plan.branch_score is not None
    assert plan.branch_score.branch_start_cell == (0, 1)
    assert plan.branch_score.target_cell == (0, 4)
    assert plan.branch_score.dead_end is False
    assert plan.goal_volume_m3 == 1.0
    assert plan.replan_at_lookahead is True
    assert plan.expanded_count <= 2_048
    dead_end_score = next(
        score
        for score in plan.branch_candidates
        if score.branch_start_cell == (1, 0)
    )
    assert dead_end_score.dead_end is True
    assert dead_end_score.target_cell in dead_end_room


def test_branch_lookahead_rejects_when_all_branches_are_dead_ends():
    current = (0, 0)
    passage = ((1, 0), (2, 0), (3, 0))
    cells = (current, *passage)
    metrics = {
        current: NavigationVoxelCellMetric(1.0, 1, 1.0, 1.0, 0.0),
        **{
            cell: NavigationVoxelCellMetric(
                available_volume_m3=10.0,
                free_cell_count=1,
                min_clearance_m=1.0,
                mean_clearance_m=1.0,
                progress_m=float(index + 1),
            )
            for index, cell in enumerate(passage)
        },
    }
    atlas = NavigationVoxelAtlas(tiles=(), cell_metrics=metrics)
    events = []

    plan = atlas.plan_footprint_route(
        cells,
        current_position=(0.5, 0.0, 0.5),
        footprint_cell_size=1.0,
        preferred_direction=(1.0, 0.0, 0.0),
        lookahead_distance_m=4.0,
        lookahead_cells=8,
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    assert plan is None
    rejection = [
        payload for event, payload in events if event == "voxel_route_rejected"
    ][-1]
    assert rejection["reason"] == "no_non_dead_end_branch"
    assert rejection["candidate_count"] == 1
    assert rejection["branch_candidates"][0]["dead_end"] is True


def test_branch_lookahead_rejects_backward_branch_when_forward_branch_continues():
    current = (0, 0)
    forward = tuple((index, 0) for index in range(1, 8))
    backward = tuple((-index, 0) for index in range(1, 8))
    cells = (current, *forward, *backward)
    metrics = {
        current: NavigationVoxelCellMetric(1.0, 1, 1.0, 1.0, 0.0),
        **{
            cell: NavigationVoxelCellMetric(
                available_volume_m3=10.0,
                free_cell_count=1,
                min_clearance_m=1.0,
                mean_clearance_m=1.0,
                progress_m=float(index + 1),
            )
            for index, cell in enumerate((*forward, *backward))
        },
    }
    atlas = NavigationVoxelAtlas(tiles=(), cell_metrics=metrics)
    events = []

    plan = atlas.plan_footprint_route(
        cells,
        current_position=(0.5, 0.0, 0.5),
        footprint_cell_size=1.0,
        preferred_direction=(1.0, 0.0, 0.0),
        lookahead_distance_m=4.0,
        lookahead_cells=8,
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    assert plan is not None
    assert plan.branch_score is not None
    assert plan.branch_score.branch_start_cell == (1, 0)
    assert plan.branch_score.first_step_alignment >= 0.0
    assert not any(event == "voxel_route_rejected" for event, _ in events)


def test_voxel_scoring_policy_prioritizes_connectivity_before_comfort_terms():
    policy = NavigationVoxelScoringPolicy(
        loop_policy="allow_forward",
        volume_weight=10.0,
    )
    highly_connected = NavigationVoxelBranchScore(
        branch_start_cell=(1, 0),
        target_cell=(2, 0),
        reached_distance_m=2.0,
        continuation_distance_m=1.0,
        onward_exit_count=1,
        frontier_count=1,
        first_step_alignment=0.1,
        path_cost_m=2.0,
        expanded_count=1,
        dead_end=False,
        target_is_terminal=False,
        connectivity_score=4.0,
        smooth_forward_score=0.1,
        volume_score=0.0,
    )
    straight_but_sparse = NavigationVoxelBranchScore(
        branch_start_cell=(1, 1),
        target_cell=(2, 1),
        reached_distance_m=10.0,
        continuation_distance_m=10.0,
        onward_exit_count=1,
        frontier_count=1,
        first_step_alignment=1.0,
        path_cost_m=10.0,
        expanded_count=1,
        dead_end=False,
        target_is_terminal=False,
        connectivity_score=1.0,
        smooth_forward_score=1.0,
        volume_score=100.0,
    )

    assert policy.branch_sort_key(highly_connected) > policy.branch_sort_key(
        straight_but_sparse
    )
    assert policy.diagnostic_payload()["loop_policy"] == "allow_forward"


def test_voxel_scoring_policy_rejects_unknown_loop_policy():
    with pytest.raises(ValueError, match="loop policy"):
        NavigationVoxelScoringPolicy(loop_policy="backtrack")


def test_cache_time_route_selection_respects_navigation_start():
    navigation = {
        "navigation_start": {"position": [0.5, 2.0, 0.5]},
        "routes": [
            {
                **_route("entrance", scale=1.0),
                "starts_at_navigation_start": True,
                "closed_loop": False,
            },
            _route("wide", scale=4.0),
        ],
        "recommended_route_id": "entrance",
    }

    result = build_navigation_voxel_cache(
        {"footprint_cell_size": 1.0},
        navigation,
        triangle_provider=_floor_provider,
        mesh_edge_is_clear=_always_clear_edge,
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            max_cells=20_000,
            max_surface_samples=4096,
        ),
    )

    assert result.recommended_route_id == "entrance"
    assert navigation["recommended_route_id"] == "entrance"


def _strict_obj_start_anchor() -> dict[str, object]:
    return {
        "position": [0.5, 2.0, 0.5],
        "kind": "obj_surface_vertex",
        "source": "fixture.obj",
        "source_vertex_index": 0,
        "source_order": "obj_declaration_order",
        "executable": False,
        "attachment_required": True,
        "attachment_coordinate_space": "xyz",
    }


def _exact_source_anchor_summary(
    *,
    length_m: float,
    min_clearance_m: float,
    obj_anchor: bool = False,
) -> dict[str, object]:
    return {
        "built": True,
        "route_length_m": float(length_m),
        "source_route_point_count": 2,
        "certified_ingress_hint_index": 0,
        "certified_terminal_hint_index": 1,
        "selected_source_hint_start_index": 0,
        "selected_source_hint_end_index": 1,
        "complete_ingress_route": True,
        "min_clearance_m": float(min_clearance_m),
        "mean_clearance_m": float(min_clearance_m + 0.5),
        "volume_per_route_m": 12.0,
        "available_volume_m3": float(length_m * 12.0),
        "_certified_route_points": (
            (0.5, 2.0, 0.5),
            (1.5, 2.0, 0.5),
        ),
        "prepared_mesh_graph": {
            "source_ingress_required": True,
            "source_ingress_connector_required": not obj_anchor,
            "source_ingress_attachment_mode": (
                "non_executable_obj_surface_anchor_snap"
                if obj_anchor
                else "executable_authored_start_connector"
            ),
            "source_ingress_connector_mesh_clear": (
                None if obj_anchor else True
            ),
            "source_ingress_point": [0.5, 2.0, 0.5],
            "source_ingress_coordinate_space": "xyz",
            "source_ingress_attachment_point": [0.5, 2.0, 0.5],
            "source_ingress_attachment_distance_m": 0.0,
            "source_ingress_snap_limit_m": 24.0,
            "terminal_graph_distance_m": float(length_m),
        },
    }


def test_corridor_volume_metrics_flood_fills_from_route_samples():
    volume = build_surface_voxel_volume(
        [_floor_provider((-2.0, -1.0, -2.0), (4.0, 4.0, 4.0))[0]],
        bounds_min=(-2.0, -1.0, -2.0),
        bounds_max=(4.0, 4.0, 4.0),
    )

    metrics = volume.corridor_volume_metrics(((0.5, 1.0, 0.5),))

    assert metrics["seed_count"] == 1
    assert metrics["free_cell_count"] > 0
    assert metrics["available_volume_m3"] > 0.0
    assert metrics["mean_clearance_m"] > 0.0


def test_voxel_probe_point_distinguishes_free_occupied_and_uncovered_space():
    volume = build_surface_voxel_volume(
        [_floor_provider((-2.0, -1.0, -2.0), (4.0, 4.0, 4.0))[0]],
        bounds_min=(-2.0, -1.0, -2.0),
        bounds_max=(4.0, 4.0, 4.0),
        config=VoxelVolumeConfig(voxel_size_m=1.0),
    )

    free = volume.probe_point((0.5, 1.0, 0.5))
    occupied = volume.probe_point((0.5, -0.5, 0.5))
    uncovered = volume.probe_point((10.0, 1.0, 0.5))

    assert free is not None and free[0] is True and free[1] > 0.0
    assert occupied == (False, 0.0)
    assert uncovered is None

    atlas = NavigationVoxelAtlas(tiles=(volume,))
    assert atlas.probe_point((0.5, 1.0, 0.5)) == free

    second_volume = build_surface_voxel_volume(
        [_floor_provider((8.0, -1.0, -2.0), (14.0, 4.0, 4.0))[0]],
        bounds_min=(8.0, -1.0, -2.0),
        bounds_max=(14.0, 4.0, 4.0),
        config=VoxelVolumeConfig(voxel_size_m=1.0),
    )
    second_free = second_volume.probe_point((8.5, 1.0, 0.5))
    assert second_free is not None and second_free[0] is True
    indexed_atlas = NavigationVoxelAtlas(tiles=(volume, second_volume))
    assert indexed_atlas.probe_point((8.5, 1.0, 0.5)) == second_free
    assert indexed_atlas.probe_point((20.0, 1.0, 0.5)) is None


def test_fine_surface_cell_can_defer_to_coarse_evidence_for_global_waypoints():
    """A 1 m surface raster must not veto an exact mesh-safe coarse point."""
    coarse = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(0.0, 0.0, 0.0),
        shape=(1, 1, 1),
        surface_cells=frozenset(),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=2,
    )
    fine = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(0.0, 0.0, 0.0),
        shape=(1, 1, 1),
        surface_cells=frozenset({(0, 0, 0)}),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=2,
    )
    atlas = NavigationVoxelAtlas(tiles=(coarse,), fine_tiles=(fine,))

    assert atlas.probe_fine_point((0.5, 0.5, 0.5)) == (False, 0.0)
    probe = atlas.probe_point((0.5, 0.5, 0.5))
    assert probe is not None and probe[0] is True
    assert atlas.fine_tiles_covering_points(((0.5, 0.5, 0.5),)) == (fine,)
    assert atlas.fine_tiles_covering_points(
        ((0.5, 0.5, 0.5), (1.5, 0.5, 0.5))
    ) == ()


def test_v11_overlap_treats_any_surface_observation_as_occupied():
    free = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(0.0, 0.0, 0.0),
        shape=(1, 1, 1),
        surface_cells=frozenset(),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=2,
    )
    occupied = replace(
        free,
        surface_cells=frozenset({(0, 0, 0)}),
    )
    atlas = NavigationVoxelAtlas(
        tiles=(free, occupied),
        coverage_scope="certified_terminal_route",
        fixed_isotropic_voxel_size_m=1.0,
        surface_overlap_occupied_wins=True,
    )

    assert atlas.probe_point((0.5, 0.5, 0.5)) == (False, 0.0)


def test_navigation_atlas_reuses_exact_point_probe_results(monkeypatch):
    volume = build_surface_voxel_volume(
        [_floor_provider((-2.0, -1.0, -2.0), (4.0, 4.0, 4.0))[0]],
        bounds_min=(-2.0, -1.0, -2.0),
        bounds_max=(4.0, 4.0, 4.0),
        config=VoxelVolumeConfig(voxel_size_m=1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(volume,))
    calls = 0
    original = NavigationVoxelAtlas._probe_point_uncached

    def counted_probe(self, point, *, include_clearance):
        nonlocal calls
        calls += 1
        return original(
            self,
            point,
            include_clearance=include_clearance,
        )

    monkeypatch.setattr(
        NavigationVoxelAtlas,
        "_probe_point_uncached",
        counted_probe,
    )

    expected = atlas.probe_point((0.5, 1.0, 0.5))
    assert atlas.probe_point((0.5, 1.0, 0.5)) == expected
    assert atlas.probe_point((0.5, 1.0, 0.5)) == expected
    assert calls == 1


def test_runtime_plan_uses_cached_model_without_voxel_rebuild(tmp_path):
    navigation = {
        "routes": [_route("centerline-0", scale=1.0)],
        "recommended_route_id": "centerline-0",
    }
    result = build_navigation_voxel_cache(
        {"footprint_cell_size": 1.0},
        navigation,
        triangle_provider=_floor_provider,
        mesh_edge_is_clear=_always_clear_edge,
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            max_cells=4096,
            max_surface_samples=4096,
        ),
    )
    navigation.update(
        {
            "version": NAVIGATION_METADATA_VERSION,
            "method": NAVIGATION_METADATA_METHOD,
        }
    )
    manifest = {
        "footprint_cell_size": 1.0,
        "navigation": navigation,
    }
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / NAVIGATION_VOXEL_CACHE_NAME).write_text(
        json.dumps(result.payload),
        encoding="utf-8",
    )
    events: list[tuple[str, dict[str, object]]] = []
    route = navigation["routes"][0]
    certified_start = tuple(route["certified_start_position"])

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=certified_start,
        settings=AutoDiveSettings(speed_m_per_second=1.0, smoothing_radius_cells=0),
        cache_dir=str(cache_dir),
        diagnostics=lambda event, payload: events.append((event, dict(payload))),
    )

    assert plan.route_length_m > 0.0
    voxel_events = [payload for event, payload in events if event == "voxel_volume"]
    assert voxel_events
    assert voxel_events[0]["outcome"] == "cache_hit"
    assert not any(payload.get("outcome") == "built" for payload in voxel_events)


def test_graph_authority_rejects_missing_cache_without_centerline_fallback(tmp_path):
    events: list[tuple[str, dict[str, object]]] = []

    with pytest.raises(
        NavigationVoxelGraphAuthorityError,
        match="requires cached navigation metadata",
    ):
        build_centerline_auto_dive_plan(
            {"footprint_cell_size": 1.0},
            current_position=(0.5, 2.0, 0.5),
            settings=AutoDiveSettings(speed_m_per_second=1.0),
            cache_dir=str(tmp_path),
            require_voxel_graph=True,
            diagnostics=lambda event, payload: events.append(
                (event, dict(payload))
            ),
        )

    authority_events = [
        payload for event, payload in events if event == "navigation_authority"
    ]
    assert authority_events
    assert authority_events[-1]["available"] is False
    assert authority_events[-1]["reason"] == "navigation_metadata_missing"


def test_runtime_cached_model_fails_closed_without_a_compatibility_branch(
    tmp_path,
    monkeypatch,
):
    component_cells = ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    route = {
        "id": "centerline-0",
        "closed_loop": False,
        "footprint_cell_size": 1.0,
        "component_cells": [value for cell in component_cells for value in cell],
        "cells": [value for cell in component_cells for value in cell],
        "component_vertical_gap_seeds": [
            value
            for cell in component_cells
            for value in (cell[0], cell[1], 2.0)
        ],
        "component_vertical_gap_intervals": [
            value
            for cell in component_cells
            for value in (cell[0], cell[1], 0.0, 4.0)
        ],
        "component_y_ranges": [
            value
            for _cell in component_cells
            for value in (0.0, 4.0)
        ],
        "length_m": 4.0,
        "starts_at_navigation_start": True,
        "points": [
            value
            for cell in component_cells
            for value in (float(cell[0]) + 0.5, 2.0, float(cell[1]) + 0.5)
        ],
    }
    navigation = {
        "navigation_start": {"position": [0.5, 2.0, 0.5]},
        "routes": [route],
        "recommended_route_id": "centerline-0",
        "version": NAVIGATION_METADATA_VERSION,
        "method": NAVIGATION_METADATA_METHOD,
    }
    manifest = {
        "footprint_cell_size": 1.0,
        "navigation": navigation,
    }
    result = build_navigation_voxel_cache(
        manifest,
        navigation,
        triangle_provider=_floor_provider,
        mesh_edge_is_clear=_always_clear_edge,
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            tile_size_m=2.0,
            max_tiles=8,
            max_cells=512,
            max_surface_samples=4096,
        ),
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / NAVIGATION_VOXEL_CACHE_NAME).write_text(
        json.dumps(result.payload),
        encoding="utf-8",
    )
    events: list[tuple[str, dict[str, object]]] = []
    certified_start = tuple(route["certified_start_position"])
    monkeypatch.setattr(
        NavigationVoxelAtlas,
        "plan_footprint_route",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(NavigationVoxelGraphAuthorityError) as error:
        build_centerline_auto_dive_plan(
            manifest,
            current_position=certified_start,
            settings=AutoDiveSettings(
                speed_m_per_second=1.0,
                lookahead_distance_m=32.0,
            ),
            cache_dir=str(cache_dir),
            require_voxel_graph=True,
            diagnostics=lambda event, payload: events.append(
                (event, dict(payload))
            ),
        )

    selection_events = [
        payload for event, payload in events if event == "voxel_route_selection"
    ]
    authority_events = [
        payload for event, payload in events if event == "navigation_authority"
    ]
    assert error.value.reason == "no_valid_forward_route"
    assert authority_events[0]["available"] is True
    assert authority_events[0]["reason"] == "ready"
    assert authority_events[0]["graph_node_count"] >= 2
    assert selection_events[0]["selected"] is False
    assert selection_events[0]["fallback_reason"] == (
        "no_viable_true_3d_voxel_branch"
    )

def test_forced_scan_fails_closed_when_current_graph_route_is_missing(
    tmp_path,
    monkeypatch,
):
    component_cells = ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    route = {
        "id": "centerline-0",
        "closed_loop": False,
        "footprint_cell_size": 1.0,
        "component_cells": [value for cell in component_cells for value in cell],
        "cells": [value for cell in component_cells for value in cell],
        "component_vertical_gap_seeds": [
            value
            for cell in component_cells
            for value in (cell[0], cell[1], 2.0)
        ],
        "component_vertical_gap_intervals": [
            value
            for cell in component_cells
            for value in (cell[0], cell[1], 0.0, 4.0)
        ],
        "component_y_ranges": [
            value
            for _cell in component_cells
            for value in (0.0, 4.0)
        ],
        "length_m": 4.0,
        "starts_at_navigation_start": True,
        "points": [
            value
            for cell in component_cells
            for value in (float(cell[0]) + 0.5, 2.0, float(cell[1]) + 0.5)
        ],
    }
    navigation = {
        "navigation_start": {"position": [0.5, 2.0, 0.5]},
        "routes": [route],
        "recommended_route_id": "centerline-0",
        "version": NAVIGATION_METADATA_VERSION,
        "method": NAVIGATION_METADATA_METHOD,
    }
    manifest = {
        "footprint_cell_size": 1.0,
        "navigation": navigation,
    }
    result = build_navigation_voxel_cache(
        manifest,
        navigation,
        triangle_provider=_floor_provider,
        mesh_edge_is_clear=_always_clear_edge,
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            tile_size_m=2.0,
            max_tiles=8,
            max_cells=512,
            max_surface_samples=4096,
        ),
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / NAVIGATION_VOXEL_CACHE_NAME).write_text(
        json.dumps(result.payload),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        NavigationVoxelAtlas,
        "plan_footprint_route",
        lambda *_args, **_kwargs: None,
    )
    events: list[tuple[str, dict[str, object]]] = []

    with pytest.raises(
        NavigationConfigurationError,
        match="route has no travel distance",
    ):
        build_centerline_auto_dive_plan(
            manifest,
            current_position=(0.5, 2.0, 0.5),
            current_yaw=0.0,
            current_pitch=0.0,
            force_hemisphere_scan=True,
            settings=AutoDiveSettings(
                speed_m_per_second=1.0,
                lookahead_distance_m=32.0,
            ),
            cache_dir=str(cache_dir),
            require_voxel_graph=True,
            diagnostics=lambda event, payload: events.append(
                (event, dict(payload))
            ),
        )
    selection = [
        payload for event, payload in events if event == "voxel_route_selection"
    ]
    assert any(
        payload.get("fallback_reason") == "continuous_scan_only_recovery"
        for payload in selection
    )
    assert any(
        payload.get("forced_full_scan") is True
        for event, payload in events
        if event == "hemisphere_scan"
    )


def test_true_3d_route_can_move_to_a_shallower_region_when_heading_is_forward():
    metrics = {
        key: NavigationVoxel3DMetric(
            center=(key[0] + 0.5, key[1] + 0.5, key[2] + 0.5),
            footprint_cell=(key[0], key[2]),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=progress,
        )
        for key, progress in {
            (0, 0, 0): 0.0,
            (1, 0, 0): 10.0,
            (2, 0, 0): 5.0,
        }.items()
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(
        tiles=(),
        prepared_3d_graph=graph,
    )

    plan = atlas.plan_footprint_route(
        ((0, 0), (1, 0), (2, 0)),
        current_position=(1.5, 0.5, 0.5),
        footprint_cell_size=1.0,
        preferred_direction=(1.0, 0.0, 0.0),
        lookahead_distance_m=8.0,
    )

    assert plan is not None
    assert plan.goal_progress_m == 5.0
    assert plan.goal_progress_m < plan.start_progress_m
    assert plan.terminal_reached is True
    assert plan.world_points[-1][0] > plan.world_points[0][0]


def test_true_3d_branch_score_prefers_heading_before_longer_perpendicular_branch():
    metrics = {}
    for x in range(9):
        key = (x, 0, 0)
        metrics[key] = NavigationVoxel3DMetric(
            center=(x + 0.5, 0.5, 0.5),
            footprint_cell=(x, 0),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(x),
        )
    for y in range(1, 9):
        key = (0, y, 0)
        metrics[key] = NavigationVoxel3DMetric(
            center=(0.5, y + 0.5, 0.5),
            footprint_cell=(0, 0),
            available_volume_m3=20.0,
            free_voxel_count=20,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(y),
        )
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)

    plan = atlas.plan_footprint_route(
        tuple((key[0], key[2]) for key in metrics),
        current_position=(0.5, 0.5, 0.5),
        footprint_cell_size=1.0,
        preferred_direction=(1.0, 0.0, 0.0),
        lookahead_distance_m=4.0,
        lookahead_cells=8,
        scoring_policy=NavigationVoxelScoringPolicy(
            loop_policy="allow_forward",
        ),
    )

    assert plan is not None
    assert plan.branch_score is not None
    assert plan.scoring_policy is not None
    assert plan.scoring_policy["loop_policy"] == "allow_forward"
    assert plan.branch_score.smooth_forward_score >= 0.0
    assert plan.branch_score.volume_score >= 0.0
    assert plan.branch_score.first_step_alignment == 1.0
    assert plan.branch_score.branch_start_key[0] > 0
    assert any(
        score.first_step_alignment == 0.0
        and score.continuation_distance_m >= plan.branch_score.continuation_distance_m
        for score in plan.branch_candidates
    )


def test_true_3d_route_uses_native_graph_component_not_coarse_cells():
    metrics = {
        key: NavigationVoxel3DMetric(
            center=(key[0] + 0.5, 0.5, 0.5),
            # Deliberately use a footprint frame unrelated to the caller's
            # coarse centerline component. The true-3D graph owns topology.
            footprint_cell=(key[0] + 100, 200),
            available_volume_m3=3.0,
            free_voxel_count=3,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(key[0]),
        )
        for key in ((10, 0, 0), (11, 0, 0), (12, 0, 0))
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    events: list[tuple[str, dict[str, object]]] = []

    plan = atlas.plan_footprint_route(
        ((0, 0), (1, 0)),
        current_position=(10.5, 0.5, 0.5),
        footprint_cell_size=10.0,
        preferred_direction=(1.0, 0.0, 0.0),
        lookahead_distance_m=4.0,
        diagnostics=lambda event, payload: events.append(
            (event, dict(payload))
        ),
    )

    assert plan is not None
    assert plan.three_d_graph is True
    assert plan.world_points[0] == (10.5, 0.5, 0.5)
    edge_filter = next(
        payload for event, payload in events if event == "voxel_route_edge_filter"
    )
    assert edge_filter["graph_native_component_filter"] is True
    assert edge_filter["rejected_component_edges"] == 0
    assert edge_filter["accepted_forward_edges"] >= 1


def test_true_3d_route_starts_at_camera_when_nearest_anchor_is_behind():
    metrics = {
        key: NavigationVoxel3DMetric(
            center=(key[0] + 0.5, 0.5, 0.5),
            footprint_cell=(key[0], 0),
            available_volume_m3=3.0,
            free_voxel_count=3,
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
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)

    plan = atlas.plan_footprint_route(
        ((0, 0), (1, 0), (2, 0)),
        current_position=(1.9, 0.5, 0.5),
        footprint_cell_size=1.0,
        preferred_direction=(1.0, 0.0, 0.0),
        lookahead_distance_m=4.0,
    )

    assert plan is not None
    assert plan.world_points[0] == pytest.approx((1.9, 0.5, 0.5))
    assert plan.world_points[1] == pytest.approx((2.5, 0.5, 0.5))
    assert all(
        point != pytest.approx((1.5, 0.5, 0.5))
        for point in plan.world_points
    )


def test_true_3d_entrance_guard_uses_horizontal_spacing_and_logs_filter_counts():
    metrics = {
        key: NavigationVoxel3DMetric(
            center=(key[0] + 0.5, key[1] + 0.5, key[2] + 0.5),
            footprint_cell=(key[0], key[2]),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(index),
        )
        for index, key in enumerate(
            ((0, 0, 0), (1, 0, 0), (2, 0, 0))
        )
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 32.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    events = []

    plan = atlas.plan_footprint_route(
        ((0, 0), (1, 0), (2, 0)),
        current_position=(1.5, 0.5, 0.5),
        footprint_cell_size=1.0,
        preferred_direction=(1.0, 0.0, 0.0),
        lookahead_distance_m=8.0,
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    assert plan is not None
    assert plan.entrance_progress_floor_m == 1.0
    assert plan.entrance_guard_tolerance_m == 1.0
    assert plan.entrance_guard_source == "horizontal_voxel_spacing"
    edge_filter = next(
        payload for event, payload in events if event == "voxel_route_edge_filter"
    )
    assert edge_filter["outgoing_edge_count"] == 2
    assert edge_filter["rejected_entrance_floor_edges"] == 0
    assert edge_filter["rejected_backward_edges"] == 1
    assert edge_filter["allowed_initial_entrance_edges"] == 1
    assert edge_filter["accepted_forward_edges"] == 1


def test_true_3d_entrance_departure_rejects_only_backward_route():
    metrics = {
        key: NavigationVoxel3DMetric(
            center=(key[0] + 0.5, key[1] + 0.5, key[2] + 0.5),
            footprint_cell=(key[0], key[2]),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(index),
        )
        for index, key in enumerate(((0, 0, 0), (1, 0, 0)))
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 32.0, 1.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)
    events = []

    plan = atlas.plan_footprint_route(
        ((0, 0), (1, 0)),
        current_position=(1.5, 0.5, 0.5),
        footprint_cell_size=1.0,
        preferred_direction=(1.0, 0.0, 0.0),
        diagnostics=lambda event, payload: events.append((event, payload)),
    )

    assert plan is None
    edge_filter = next(
        payload for event, payload in events if event == "voxel_route_edge_filter"
    )
    assert edge_filter["rejected_entrance_floor_edges"] == 0
    assert edge_filter["rejected_backward_edges"] == 1
    assert edge_filter["allowed_initial_entrance_edges"] == 1
    assert edge_filter["accepted_forward_edges"] == 0
    rejection = next(
        payload
        for event, payload in events
        if event == "voxel_route_rejected"
    )
    assert rejection["reason"] == "no_forward_continuation"
    assert rejection["rejected_entrance_floor_edges"] == 0
    assert rejection["entrance_guard_tolerance_m"] == 1.0


def test_true_3d_route_allows_bounded_initial_entrance_departure():
    metrics = {
        key: NavigationVoxel3DMetric(
            center=(key[0] * 2.0 + 1.0, 0.5, 0.5),
            footprint_cell=(key[0], key[2]),
            available_volume_m3=2.0,
            free_voxel_count=2,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=float(index),
        )
        for index, key in enumerate(
            ((0, 0, 0), (1, 0, 0), (2, 0, 0))
        )
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(2.0, 32.0, 2.0),
    )
    atlas = NavigationVoxelAtlas(tiles=(), prepared_3d_graph=graph)

    plan = atlas.plan_footprint_route(
        ((0, 0), (1, 0), (2, 0)),
        current_position=(1.0, 0.5, 0.5),
        footprint_cell_size=1.0,
        preferred_direction=(1.0, 0.0, 0.0),
        lookahead_distance_m=8.0,
    )

    assert plan is not None
    assert plan.world_points[-1][0] > plan.world_points[0][0]


def _route(route_id: str, *, scale: float) -> dict[str, object]:
    points = (
        (0.5, 2.0, 0.5),
        (2.0 * scale, 2.0, 0.5),
        (2.0 * scale, 2.0, 2.0 * scale),
    )
    extent = int(2 * scale)
    cells = tuple((x, 0) for x in range(extent + 1)) + tuple(
        (extent, z) for z in range(1, extent + 1)
    )
    return {
        "id": route_id,
        "closed_loop": False,
        "footprint_cell_size": 1.0,
        "component_cells": [value for cell in cells for value in cell],
        "cells": [value for cell in cells for value in cell],
        "component_vertical_gap_seeds": [
            value
            for cell in cells
            for value in (cell[0], cell[1], 2.0)
        ],
        "component_vertical_gap_intervals": [
            value
            for cell in cells
            for value in (cell[0], cell[1], 0.0, 4.0)
        ],
        "length_m": float(4.0 * scale),
        "points": [value for point in points for value in point],
    }


def _floor_provider(bounds_min, bounds_max):
    lower = np.asarray(bounds_min, dtype=np.float64)
    upper = np.asarray(bounds_max, dtype=np.float64)
    y = lower[1]
    return (
        np.asarray(
            [[
                [lower[0], y, lower[2]],
                [upper[0], y, lower[2]],
                [lower[0], y, upper[2]],
            ]],
            dtype=np.float64,
        ),
    )


def _always_clear_edge(_first, _second):
    return True
