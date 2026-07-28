"""Tests for bounded cache-time navigation voxel models."""

from __future__ import annotations

import json

import numpy as np
import pytest

from caveviewer.core.navigation.cache_metadata import (
    NAVIGATION_METADATA_METHOD,
    NAVIGATION_METADATA_VERSION,
    cached_centerline_path,
)
from caveviewer.core.navigation.autodive import (
    AutoDiveSettings,
    NavigationVoxelGraphAuthorityError,
    build_centerline_auto_dive_plan,
)
from caveviewer.core.navigation.voxel_cache import (
    NAVIGATION_VOXEL_CACHE_NAME,
    NavigationVoxelCellMetric,
    NavigationVoxelAtlas,
    NavigationVoxelBranchScore,
    NavigationVoxelCacheConfig,
    NavigationVoxelScoringPolicy,
    build_navigation_voxel_cache,
    deserialize_local_voxel_volume,
    load_cached_navigation_voxel_volume,
    supported_navigation_voxel_cache_identity,
)
from caveviewer.core.navigation.voxel_graph_3d import (
    NAVIGATION_VOXEL_3D_GRAPH_METHOD,
    NavigationVoxel3DMetric,
    build_navigation_voxel_3d_graph,
)
from caveviewer.core.navigation.voxel_volume import (
    VoxelVolumeConfig,
    build_surface_voxel_volume,
)


def test_cache_identity_rejects_previous_two_metre_atlas():
    assert supported_navigation_voxel_cache_identity(
        6,
        "whole_cave_voxel_atlas_v6",
    )
    assert not supported_navigation_voxel_cache_identity(
        5,
        "whole_cave_voxel_atlas_v5",
    )


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


def test_cache_time_publishes_graph_index_and_lazy_voxel_chunks(tmp_path):
    component_cells = ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    route = {
        "id": "centerline-0",
        "footprint_cell_size": 1.0,
        "component_cells": [value for cell in component_cells for value in cell],
        "cells": [value for cell in component_cells for value in cell],
        "component_y_ranges": [
            value
            for _cell in component_cells
            for value in (0.0, 4.0)
        ],
        "length_m": 4.0,
        "points": [
            value
            for cell in component_cells
            for value in (float(cell[0]) + 0.5, 2.0, float(cell[1]) + 0.5)
        ],
    }
    navigation = {
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


def test_cache_time_route_selection_prefers_larger_volume():
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
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            max_cells=20_000,
            max_surface_samples=4096,
        ),
    )

    assert result.built_route_count == 2
    assert result.recommended_route_id == "wide"
    assert navigation["recommended_route_id"] == "wide"
    assert (
        navigation["routes"][1]["voxel_corridor"]["available_volume_m3"]
        > navigation["routes"][0]["voxel_corridor"]["available_volume_m3"]
    )


def test_cache_time_voxel_atlas_covers_the_entire_component():
    component_cells = ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    route = {
        "id": "centerline-0",
        "footprint_cell_size": 1.0,
        "component_cells": [value for cell in component_cells for value in cell],
        "cells": [value for cell in component_cells for value in cell],
        "component_y_ranges": [
            value
            for _cell in component_cells
            for value in (0.0, 4.0)
        ],
        "length_m": 4.0,
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
    assert summary["coverage_scope"] == "entire_cave_component"
    assert summary["coverage_includes_preceding_curvature"] is True
    assert summary["coverage_cell_count"] == len(component_cells)
    assert summary["tile_count"] >= 2
    model = result.payload["routes"]["centerline-0"]["model"]
    assert model["method"] == "navigation_voxel_atlas_v7"
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
    assert summary["fine_tile_count"] >= 1
    assert restored.fine_tiles
    assert restored.fine_voxel_size_m == 1.0
    assert restored.fine_tile_for_point(route["points"][:3]) is not None


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
            },
            _route("wide", scale=4.0),
        ],
        "recommended_route_id": "entrance",
    }

    result = build_navigation_voxel_cache(
        {"footprint_cell_size": 1.0},
        navigation,
        triangle_provider=_floor_provider,
        config=NavigationVoxelCacheConfig(
            voxel_size_m=1.0,
            max_cells=20_000,
            max_surface_samples=4096,
        ),
    )

    assert result.recommended_route_id == "entrance"
    assert navigation["recommended_route_id"] == "entrance"


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


def test_runtime_plan_uses_cached_model_without_voxel_rebuild(tmp_path):
    navigation = {
        "routes": [_route("centerline-0", scale=1.0)],
        "recommended_route_id": "centerline-0",
    }
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

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=(0.5, 2.0, 0.5),
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


def test_runtime_plan_selects_the_cached_filled_voxel_route(tmp_path):
    component_cells = ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    route = {
        "id": "centerline-0",
        "footprint_cell_size": 1.0,
        "component_cells": [value for cell in component_cells for value in cell],
        "cells": [value for cell in component_cells for value in cell],
        "component_y_ranges": [
            value
            for _cell in component_cells
            for value in (0.0, 4.0)
        ],
        "length_m": 4.0,
        "points": [
            value
            for cell in component_cells
            for value in (float(cell[0]) + 0.5, 2.0, float(cell[1]) + 0.5)
        ],
    }
    navigation = {
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

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=(0.5, 2.0, 0.5),
        current_yaw=0.0,
        current_pitch=0.0,
        settings=AutoDiveSettings(
            speed_m_per_second=1.0,
            lookahead_distance_m=32.0,
        ),
        cache_dir=str(cache_dir),
        require_voxel_graph=True,
        diagnostics=lambda event, payload: events.append((event, dict(payload))),
    )

    selection_events = [
        payload for event, payload in events if event == "voxel_route_selection"
    ]
    candidate_events = [
        payload for event, payload in events if event == "candidate_scores"
    ]
    authority_events = [
        payload for event, payload in events if event == "navigation_authority"
    ]
    # The true-3D graph sees additional filled volume beyond the first
    # lookahead prefix, so this fixture is a continuing branch rather than a
    # proven cave terminus.
    assert plan.selection_reason == "prepared_true_3d_graph"
    assert plan.terminal_reached is False
    assert authority_events[0]["available"] is True
    assert authority_events[0]["reason"] == "ready"
    assert authority_events[0]["graph_node_count"] >= 2
    assert selection_events[0]["selected"] is True
    assert selection_events[0]["plan"]["prepared_graph"] is True
    assert selection_events[0]["plan"]["three_d_graph"] is True
    assert candidate_events[0]["selection_reason"] == "prepared_true_3d_graph"

    forced_events: list[tuple[str, dict[str, object]]] = []
    forced_plan = build_centerline_auto_dive_plan(
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
        diagnostics=lambda event, payload: forced_events.append(
            (event, dict(payload))
        ),
    )
    forced_scan_events = [
        payload for event, payload in forced_events if event == "hemisphere_scan"
    ]
    assert forced_plan.route_length_m > 0.0
    assert forced_scan_events
    assert forced_scan_events[-1]["forced_full_scan"] is True
    assert forced_scan_events[-1]["generated_count"] == 32 * 4 * 9


def test_forced_scan_can_recover_when_current_graph_cell_has_no_forward_route(
    tmp_path,
    monkeypatch,
):
    component_cells = ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    route = {
        "id": "centerline-0",
        "footprint_cell_size": 1.0,
        "component_cells": [value for cell in component_cells for value in cell],
        "cells": [value for cell in component_cells for value in cell],
        "component_y_ranges": [
            value
            for _cell in component_cells
            for value in (0.0, 4.0)
        ],
        "length_m": 4.0,
        "points": [
            value
            for cell in component_cells
            for value in (float(cell[0]) + 0.5, 2.0, float(cell[1]) + 0.5)
        ],
    }
    navigation = {
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

    plan = build_centerline_auto_dive_plan(
        manifest,
        current_position=(3.5, 2.0, 3.5),
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

    assert plan.route_length_m > 0.0
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
    assert edge_filter["rejected_entrance_floor_edges"] == 1
    assert edge_filter["accepted_forward_edges"] == 1


def test_true_3d_entrance_floor_rejection_is_explicit_when_it_blocks_route():
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
    assert edge_filter["rejected_entrance_floor_edges"] == 1
    assert edge_filter["accepted_forward_edges"] == 0
    rejection = next(
        payload
        for event, payload in events
        if event == "voxel_route_rejected"
    )
    assert rejection["reason"] == "no_forward_continuation"
    assert rejection["rejected_entrance_floor_edges"] == 1
    assert rejection["entrance_guard_tolerance_m"] == 1.0


def _route(route_id: str, *, scale: float) -> dict[str, object]:
    points = (
        (0.5, 2.0, 0.5),
        (2.0 * scale, 2.0, 0.5),
        (2.0 * scale, 2.0, 2.0 * scale),
    )
    cells = ((0, 0), (int(2 * scale), 0), (int(2 * scale), int(2 * scale)))
    return {
        "id": route_id,
        "footprint_cell_size": 1.0,
        "component_cells": [value for cell in cells for value in cell],
        "cells": [value for cell in cells for value in cell],
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
