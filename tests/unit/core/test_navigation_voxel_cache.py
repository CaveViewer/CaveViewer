"""Tests for bounded cache-time navigation voxel models."""

from __future__ import annotations

import json

import numpy as np

from caveviewer.core.navigation.cache_metadata import (
    NAVIGATION_METADATA_METHOD,
    NAVIGATION_METADATA_VERSION,
    cached_centerline_path,
)
from caveviewer.core.navigation.autodive import (
    AutoDiveSettings,
    build_centerline_auto_dive_plan,
)
from caveviewer.core.navigation.voxel_cache import (
    NAVIGATION_VOXEL_CACHE_NAME,
    NavigationVoxelCellMetric,
    NavigationVoxelAtlas,
    NavigationVoxelCacheConfig,
    build_navigation_voxel_cache,
    deserialize_local_voxel_volume,
)
from caveviewer.core.navigation.voxel_volume import build_surface_voxel_volume


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
    assert model["method"] == "navigation_voxel_atlas_v2"
    assert model["branch_lookahead_method"] == "voxel_branch_lookahead_v1"
    restored = deserialize_local_voxel_volume(model)
    assert isinstance(restored, NavigationVoxelAtlas)
    assert len(restored.tiles) == summary["tile_count"]
    assert restored.bounds_min[0] <= 0.0
    assert restored.bounds_max[2] >= 3.0
    assert summary["navigation_graph_method"] == (
        "voxel_filled_component_graph_v1"
    )
    assert summary["branch_lookahead_method"] == "voxel_branch_lookahead_v1"
    assert summary["navigation_cell_count"] == len(component_cells)
    assert restored.navigation_cell_count == len(component_cells)


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
        settings=AutoDiveSettings(speed_m_per_second=1.0),
        cache_dir=str(cache_dir),
        diagnostics=lambda event, payload: events.append((event, dict(payload))),
    )

    selection_events = [
        payload for event, payload in events if event == "voxel_route_selection"
    ]
    candidate_events = [
        payload for event, payload in events if event == "candidate_scores"
    ]
    assert plan.selection_reason == "trusted_route_clear"
    assert selection_events[0]["selected"] is False
    assert selection_events[0]["fallback_reason"] == (
        "no_viable_filled_voxel_branch"
    )
    assert candidate_events[0]["selection_reason"] == "trusted_route_clear"


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
