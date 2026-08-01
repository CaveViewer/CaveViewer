#!/usr/bin/env python3
"""Evaluate an implicit isotropic cubic graph against an existing map cache."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time
import tracemalloc
from collections.abc import Mapping, Sequence

from caveviewer.core.chunking.metadata import load_manifest
from caveviewer.core.navigation.centerline import footprint_world_center
from caveviewer.core.navigation.cubic_graph import (
    CubicVoxelPath,
    build_cubic_graph_from_local_volumes,
)
from caveviewer.core.navigation.mesh_collision import CachedChunkMeshCollisionGuard
from caveviewer.core.navigation.voxel_cache import (
    NavigationVoxelAtlas,
    _fallback_y_range,
    _flat_cells,
    _route_points,
    _route_y_ranges,
    load_cached_navigation_voxel_volume,
)
from caveviewer.core.navigation.voxel_volume import (
    VoxelVolumeConfig,
    build_surface_voxel_volume,
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cache_dir = os.path.abspath(os.fspath(args.cache_dir))
    manifest = load_manifest(cache_dir)
    if not isinstance(manifest, dict):
        return _print_failure(
            "cache_manifest_missing",
            cache_dir=cache_dir,
            json_output=bool(args.json),
        )
    route_id, route = _selected_route(manifest, args.route_id)
    if route_id is None or route is None:
        return _print_failure(
            "navigation_route_missing",
            cache_dir=cache_dir,
            json_output=bool(args.json),
        )
    route_points = _route_points(route)
    if len(route_points) < 2:
        return _print_failure(
            "navigation_route_points_missing",
            cache_dir=cache_dir,
            route_id=route_id,
            json_output=bool(args.json),
        )
    start_point = _point_or_default(args.start, route_points[0])
    target_point = _point_or_default(args.target, route_points[-1])
    cell_size = _route_cell_size(route, manifest)
    ordered_component_cells = _flat_cells(route.get("component_cells"))
    if not ordered_component_cells:
        ordered_component_cells = _flat_cells(route.get("cells"))
    component_cells = set(ordered_component_cells)
    y_ranges = dict(
        zip(
            ordered_component_cells,
            _route_y_ranges(
                route.get("component_y_ranges"),
                ordered_component_cells,
            ),
            strict=False,
        )
    )
    fallback_y_range = _fallback_y_range(manifest, route_points)

    atlas = load_cached_navigation_voxel_volume(
        cache_dir,
        manifest,
        route_id,
    )
    if not isinstance(atlas, NavigationVoxelAtlas):
        return _print_failure(
            "navigation_atlas_missing",
            cache_dir=cache_dir,
            route_id=route_id,
            json_output=bool(args.json),
        )
    guard = CachedChunkMeshCollisionGuard.from_manifest(
        manifest,
        cache_dir=cache_dir,
    )
    if guard is None:
        if atlas.chunk_store is not None:
            atlas.chunk_store.close()
        return _print_failure(
            "mesh_collision_guard_missing",
            cache_dir=cache_dir,
            route_id=route_id,
            json_output=bool(args.json),
        )

    point_filter = _component_point_filter(
        component_cells,
        cell_size=cell_size,
        y_ranges=y_ranges,
        fallback_y_range=fallback_y_range,
    )
    graph_point_filter = point_filter
    tracemalloc.start()
    started = time.perf_counter()
    try:
        if args.mode == "atlas":
            volumes, source_details = _atlas_volumes(
                atlas,
                route_points=route_points,
                component_cells=component_cells,
                cell_size=cell_size,
                y_ranges=y_ranges,
                fallback_y_range=fallback_y_range,
                voxel_size_m=float(args.voxel_size),
            )
        else:
            if args.bounds is None:
                raise ValueError("region mode requires --bounds")
            volume, region_details, region_lower, region_upper = _region_volume(
                guard,
                bounds=args.bounds,
                start_point=start_point,
                target_point=target_point,
                voxel_size_m=float(args.voxel_size),
                max_voxels=int(args.max_voxels),
                max_surface_samples=int(args.max_surface_samples),
            )
            volumes = ((volume, (start_point, target_point)),)
            source_details = region_details
            graph_point_filter = _bounded_point_filter(
                point_filter,
                bounds_min=region_lower,
                bounds_max=region_upper,
            )
        build_result = build_cubic_graph_from_local_volumes(
            volumes,
            voxel_size_m=float(args.voxel_size),
            minimum_clearance_m=float(args.minimum_clearance),
            point_filter=graph_point_filter,
            allow_truncated_surface_evidence=args.mode == "atlas",
        )
        graph = build_result.graph
        start_key, start_snap_distance_m = graph.nearest_key(
            start_point,
            max_distance_m=float(args.snap_radius),
        )
        target_key, target_snap_distance_m = graph.nearest_key(
            target_point,
            max_distance_m=float(args.snap_radius),
        )
        path, exact_details = _find_exact_safe_path(
            graph,
            start_key=start_key,
            target_key=target_key,
            start_point=start_point,
            target_point=target_point,
            guard=guard,
            allow_diagonal=not bool(args.cardinal_only),
            max_expansions=int(args.max_expansions),
            max_mesh_replans=int(args.max_mesh_replans),
        )
        component_started = time.perf_counter()
        component_sizes = graph.component_sizes()
        component_duration_s = time.perf_counter() - component_started
        duration_s = time.perf_counter() - started
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        payload = {
            "cache_dir": cache_dir,
            "route_id": route_id,
            "mode": args.mode,
            "passed": bool(path is not None and exact_details["passed"]),
            "voxel_size_m": float(args.voxel_size),
            "minimum_clearance_m": float(args.minimum_clearance),
            "cardinal_only": bool(args.cardinal_only),
            "duration_s": float(duration_s),
            "python_peak_bytes": int(peak_bytes),
            "start_point": _point_payload(start_point),
            "target_point": _point_payload(target_point),
            "start_key": _key_payload(start_key),
            "target_key": _key_payload(target_key),
            "start_snap_distance_m": _finite_or_none(start_snap_distance_m),
            "target_snap_distance_m": _finite_or_none(target_snap_distance_m),
            "component_count": len(component_sizes),
            "largest_component_voxel_count": (
                component_sizes[0] if component_sizes else 0
            ),
            "component_sizes": list(component_sizes[:20]),
            "component_analysis_duration_s": float(component_duration_s),
            "source": source_details,
            "graph": build_result.details,
            "path": None if path is None else path.diagnostic_payload(),
            "exact_mesh_validation": exact_details,
        }
    except Exception as exc:
        duration_s = time.perf_counter() - started
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        payload = {
            "cache_dir": cache_dir,
            "route_id": route_id,
            "mode": args.mode,
            "passed": False,
            "reason": "cubic_graph_experiment_error",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "duration_s": float(duration_s),
            "python_peak_bytes": int(peak_bytes),
        }
    finally:
        tracemalloc.stop()
        if atlas.chunk_store is not None:
            atlas.chunk_store.close()
    _print_payload(payload, json_output=bool(args.json))
    return 0 if bool(payload.get("passed")) else 1


def _atlas_volumes(
    atlas: NavigationVoxelAtlas,
    *,
    route_points,
    component_cells,
    cell_size,
    y_ranges,
    fallback_y_range,
    voxel_size_m,
):
    if atlas.chunk_store is None:
        descriptors_and_tiles = tuple(
            (None, tile) for tile in atlas.tiles
        )
    else:
        descriptors = atlas.chunk_store.descriptors(fine_only=False)
        incompatible = tuple(
            descriptor
            for descriptor in descriptors
            if not math.isclose(
                float(descriptor.voxel_size_m),
                float(voxel_size_m),
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        )
        if incompatible:
            raise ValueError(
                "coarse atlas contains tiles that differ from the requested "
                "resolution; use region mode or rebuild"
            )
        descriptors_and_tiles = tuple(
            (descriptor, atlas.chunk_store.get_chunk(descriptor.chunk_id))
            for descriptor in descriptors
        )
    seed_points = []
    for cell in sorted(component_cells):
        x, z = footprint_world_center(cell, cell_size)
        low_y, high_y = y_ranges.get(cell, fallback_y_range)
        seed_points.append((x, (low_y + high_y) * 0.5, z))
    seed_points.extend(route_points)
    volumes = []
    missing_chunk_count = 0
    for _descriptor, tile in descriptors_and_tiles:
        if tile is None:
            missing_chunk_count += 1
            continue
        tile_seeds = tuple(
            point for point in seed_points if tile.contains_point(point)
        )
        if not tile_seeds:
            continue
        volumes.append((tile, tile_seeds))
    if not volumes:
        raise ValueError("no compatible atlas tiles contain route/component seeds")
    return tuple(volumes), {
        "method": "existing_v10_isotropic_atlas_tiles",
        "selected_volume_count": len(volumes),
        "missing_chunk_count": int(missing_chunk_count),
        "seed_point_count": len(seed_points),
    }


def _region_volume(
    guard,
    *,
    bounds,
    start_point,
    target_point,
    voxel_size_m,
    max_voxels,
    max_surface_samples,
):
    raw_lower = tuple(float(value) for value in bounds[:3])
    raw_upper = tuple(float(value) for value in bounds[3:])
    size = float(voxel_size_m)
    lower = tuple(
        math.floor(min(raw_lower[axis], raw_upper[axis]) / size) * size
        for axis in range(3)
    )
    upper = tuple(
        math.ceil(max(raw_lower[axis], raw_upper[axis]) / size) * size
        for axis in range(3)
    )
    graph_capacity = math.prod(
        max(1, int(round((upper[axis] - lower[axis]) / size)))
        for axis in range(3)
    )
    raster_capacity = math.prod(
        max(
            1,
            int(math.ceil((upper[axis] - lower[axis]) / size)) + 1,
        )
        for axis in range(3)
    )
    if raster_capacity > max(1, int(max_voxels)):
        raise ValueError(
            f"region raster requires {raster_capacity} voxels, "
            "above --max-voxels"
        )
    volume = build_surface_voxel_volume(
        guard.triangle_meshes_for_bounds(lower, upper),
        bounds_min=lower,
        bounds_max=upper,
        config=VoxelVolumeConfig(
            voxel_size_m=size,
            surface_inflation_cells=1,
            max_voxels=max(1, int(max_voxels)),
            max_surface_samples=max(1, int(max_surface_samples)),
        ),
    )
    if not math.isclose(volume.voxel_size_m, size, rel_tol=0.0, abs_tol=1e-6):
        raise ValueError("region voxelizer coarsened the requested resolution")
    if volume.sampling_truncated:
        raise ValueError("region voxelizer truncated surface evidence")
    return volume, {
        "method": "cached_mesh_region_revoxelization",
        "bounds_min": _point_payload(lower),
        "bounds_max": _point_payload(upper),
        "graph_voxel_capacity": int(graph_capacity),
        "raster_voxel_capacity": int(raster_capacity),
        "volume": volume.diagnostic_payload(),
        "seed_points": [_point_payload(start_point), _point_payload(target_point)],
    }, lower, upper


def _component_point_filter(
    component_cells,
    *,
    cell_size,
    y_ranges,
    fallback_y_range,
):
    def accepts(point):
        cell = (
            math.floor(float(point[0]) / cell_size),
            math.floor(float(point[2]) / cell_size),
        )
        if component_cells and cell not in component_cells:
            return False
        low_y, high_y = y_ranges.get(cell, fallback_y_range)
        return low_y - 1e-9 <= float(point[1]) <= high_y + 1e-9

    return accepts


def _bounded_point_filter(
    base_filter,
    *,
    bounds_min,
    bounds_max,
):
    """Exclude the voxelizer's extra upper-bound raster shell."""
    def accepts(point):
        return base_filter(point) and all(
            float(bounds_min[axis]) - 1e-9
            <= float(point[axis])
            < float(bounds_max[axis]) - 1e-9
            for axis in range(3)
        )

    return accepts


def _validate_exact_path(
    path: CubicVoxelPath | None,
    *,
    start_point,
    target_point,
    guard,
    collision_cache,
):
    if path is None or not path.points:
        return ({
            "passed": False,
            "reason": "cubic_voxel_path_missing",
            "segment_count": 0,
        }, None)
    points = (start_point, *path.points, target_point)
    checked = 0
    for segment_index, (first, second) in enumerate(
        zip(points, points[1:], strict=False)
    ):
        if math.dist(first, second) <= 1e-9:
            continue
        checked += 1
        ordered_points = (
            (first, second) if first < second else (second, first)
        )
        if ordered_points in collision_cache:
            hit = collision_cache[ordered_points]
        else:
            hit = guard.segment_collision(first, second)
            collision_cache[ordered_points] = hit
        if hit is not None:
            blocked_edge = (
                (path.keys[segment_index - 1], path.keys[segment_index])
                if 1 <= segment_index < len(path.keys)
                else None
            )
            return ({
                "passed": False,
                "reason": "cubic_voxel_path_mesh_collision",
                "segment_count": int(checked),
                "failed_segment_index": int(segment_index),
                "hit_point": _point_payload(hit.point),
                "hit_chunk": [int(value) for value in hit.chunk_cell],
            }, blocked_edge)
    return ({
        "passed": True,
        "reason": "",
        "segment_count": int(checked),
    }, None)


def _find_exact_safe_path(
    graph,
    *,
    start_key,
    target_key,
    start_point,
    target_point,
    guard,
    allow_diagonal,
    max_expansions,
    max_mesh_replans,
):
    if start_key is None or target_key is None:
        return None, {
            "passed": False,
            "reason": "cubic_voxel_endpoint_missing",
            "segment_count": 0,
            "mesh_replan_count": 0,
            "blocked_edge_count": 0,
        }
    blocked_edges = set()
    collision_cache = {}
    last_details = {
        "passed": False,
        "reason": "cubic_voxel_path_missing",
        "segment_count": 0,
    }
    attempts = max(0, int(max_mesh_replans)) + 1
    for attempt in range(attempts):
        path = graph.shortest_path(
            start_key,
            target_key,
            allow_diagonal=bool(allow_diagonal),
            max_expansions=int(max_expansions),
            blocked_edges=blocked_edges,
        )
        details, blocked_edge = _validate_exact_path(
            path,
            start_point=start_point,
            target_point=target_point,
            guard=guard,
            collision_cache=collision_cache,
        )
        last_details = details
        if bool(details["passed"]):
            return path, {
                **details,
                "mesh_replan_count": int(attempt),
                "blocked_edge_count": len(blocked_edges),
                "cached_exact_segment_count": len(collision_cache),
            }
        if blocked_edge is None or blocked_edge in blocked_edges:
            return path, {
                **details,
                "mesh_replan_count": int(attempt),
                "blocked_edge_count": len(blocked_edges),
                "cached_exact_segment_count": len(collision_cache),
            }
        blocked_edges.add(blocked_edge)
    return None, {
        **last_details,
        "reason": "cubic_voxel_mesh_replan_limit_reached",
        "mesh_replan_count": max(0, attempts - 1),
        "blocked_edge_count": len(blocked_edges),
        "cached_exact_segment_count": len(collision_cache),
    }


def _selected_route(manifest, route_id):
    navigation = manifest.get("navigation")
    if not isinstance(navigation, Mapping):
        return None, None
    selected_id = (
        str(route_id)
        if route_id is not None
        else str(navigation.get("recommended_route_id", ""))
    )
    routes = navigation.get("routes")
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        return None, None
    for route in routes:
        if isinstance(route, Mapping) and str(route.get("id")) == selected_id:
            return selected_id, route
    return None, None


def _route_cell_size(route, manifest):
    value = route.get("footprint_cell_size", manifest.get("footprint_cell_size"))
    size = float(value)
    if not math.isfinite(size) or size <= 0.0:
        raise ValueError("route footprint cell size is invalid")
    return size


def _point_or_default(value, default):
    if value is None:
        return tuple(float(item) for item in default)
    return tuple(float(item) for item in value)


def _point_payload(point):
    return [float(value) for value in point]


def _key_payload(key):
    return None if key is None else [int(value) for value in key]


def _finite_or_none(value):
    return float(value) if math.isfinite(float(value)) else None


def _print_failure(reason, *, cache_dir, json_output, route_id=None):
    payload = {
        "cache_dir": cache_dir,
        "route_id": route_id,
        "passed": False,
        "reason": reason,
    }
    _print_payload(payload, json_output=json_output)
    return 1


def _print_payload(payload, *, json_output):
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "PASS" if payload.get("passed") else "FAIL",
            payload.get("reason", ""),
        )


def _parser():
    parser = argparse.ArgumentParser(
        description=(
            "Build a read-only implicit 1 m cubic graph from an existing V10 "
            "atlas or a bounded cached-mesh region."
        )
    )
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--route-id")
    parser.add_argument("--mode", choices=("atlas", "region"), default="atlas")
    parser.add_argument("--voxel-size", type=float, default=1.0)
    parser.add_argument("--minimum-clearance", type=float, default=0.25)
    parser.add_argument("--cardinal-only", action="store_true")
    parser.add_argument("--snap-radius", type=float, default=8.0)
    parser.add_argument("--max-expansions", type=int, default=2_000_000)
    parser.add_argument("--max-mesh-replans", type=int, default=64)
    parser.add_argument("--max-voxels", type=int, default=1_000_000)
    parser.add_argument("--max-surface-samples", type=int, default=500_000)
    parser.add_argument("--bounds", nargs=6, type=float)
    parser.add_argument("--start", nargs=3, type=float)
    parser.add_argument("--target", nargs=3, type=float)
    parser.add_argument("--json", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
