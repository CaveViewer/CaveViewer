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
