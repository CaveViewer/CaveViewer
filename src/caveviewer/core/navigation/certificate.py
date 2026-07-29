"""Offline certification for cache-backed Guided Dive navigation.

The viewer can only prove that a route is usable after it has loaded the
same artifacts used by runtime planning.  This module turns that proof into a
bounded, machine-readable gate that can run before the GUI is started.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

from caveviewer.core.chunking.io import load_chunk_file
from caveviewer.core.chunking.metadata import cache_dir_is_valid, load_manifest
from caveviewer.core.navigation.autodive import (
    AUTO_DIVE_PREFLIGHT_READY,
    AutoDivePlan,
    AutoDiveSettings,
    NavigationVoxelGraphAuthorityError,
    _direction_from_radians,
    build_auto_dive_preflight_plan,
    build_voxel_graph_auto_dive_plan,
)
from caveviewer.core.navigation.centerline import parse_cell_key
from caveviewer.core.navigation.graph_route_safety import (
    GraphRouteSafetyFailure,
    GraphRouteSafetyPolicy,
    GraphRouteSafetyValidator,
)
from caveviewer.core.navigation.mesh_collision import (
    CachedChunkMeshCollisionGuard,
)
from caveviewer.core.navigation.voxel_cache import (
    NavigationVoxelAtlas,
    load_cached_navigation_voxel_volume,
)
from caveviewer.core.navigation.voxel_graph_3d import (
    NavigationVoxel3DGraph,
    VoxelGraphKey,
)


Point = tuple[float, float, float]
PROFILE_FULL_CAVE = "full-cave"
PROFILE_FRONTIER = "frontier"
CERTIFICATE_PROFILES = (PROFILE_FULL_CAVE, PROFILE_FRONTIER)
DEFAULT_CHECKPOINT_SPACING_M = 4.0
DEFAULT_MAX_CHECKPOINTS = 512


@dataclass(frozen=True)
class NavigationCertificateCheck:
    """One independently reported certificate gate."""

    name: str
    passed: bool
    reason: str = ""
    details: Mapping[str, object] = field(default_factory=dict)

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "name": str(self.name),
            "passed": bool(self.passed),
            "reason": str(self.reason),
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class NavigationCertificateResult:
    """Complete offline navigation certification result."""

    passed: bool
    profile: str
    cache_dir: str
    route_id: str | None
    start_position: Point | None
    checks: tuple[NavigationCertificateCheck, ...]

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "certificate": "PASS" if self.passed else "FAIL",
            "passed": bool(self.passed),
            "profile": str(self.profile),
            "cache_dir": str(self.cache_dir),
            "route_id": self.route_id,
            "start_position": _point_payload(self.start_position),
            "checks": [check.diagnostic_payload() for check in self.checks],
            "failed_checks": [
                check.name for check in self.checks if not check.passed
            ],
        }


def certify_navigation_cache(
    manifest: Mapping[str, Any],
    *,
    cache_dir: str | os.PathLike[str],
    start_position: Sequence[float],
    start_yaw: float | None = None,
    start_pitch: float | None = None,
    source_path: str | os.PathLike[str] | None = None,
    route_id: str | None = None,
    profile: str = PROFILE_FULL_CAVE,
    settings: AutoDiveSettings | None = None,
    checkpoint_spacing_m: float = DEFAULT_CHECKPOINT_SPACING_M,
    max_checkpoints: int = DEFAULT_MAX_CHECKPOINTS,
) -> NavigationCertificateResult:
    """Certify that one cache can support an executable Guided Dive route.

    ``full-cave`` is the release/test profile.  It rejects temporary unknown
    boundaries and requires a known terminal.  ``frontier`` is useful while
    developing cache discovery, but a passing frontier result must not be
    interpreted as proof that the complete cave is navigable.
    """
    if profile not in CERTIFICATE_PROFILES:
        raise ValueError(
            f"unsupported navigation certificate profile: {profile!r}"
        )
    if not math.isfinite(float(checkpoint_spacing_m)) or checkpoint_spacing_m <= 0.0:
        raise ValueError("checkpoint spacing must be finite and positive")
    if int(max_checkpoints) < 2:
        raise ValueError("max_checkpoints must be at least 2")

    cache_path = os.path.abspath(os.fspath(cache_dir))
    point = _finite_point(start_position)
    checks: list[NavigationCertificateCheck] = []

    def add_check(
        name: str,
        passed: bool,
        *,
        reason: str = "",
        details: Mapping[str, object] | None = None,
    ) -> None:
        checks.append(
            NavigationCertificateCheck(
                name=name,
                passed=bool(passed),
                reason=reason,
                details=dict(details or {}),
            )
        )

    add_check(
        "input",
        point is not None,
        reason="" if point is not None else "start_position_must_be_finite_xyz",
        details={
            "start_position": _point_payload(point),
            "start_yaw": _finite_number(start_yaw),
            "start_pitch": _finite_number(start_pitch),
        },
    )
    if point is None:
        return _result(profile, cache_path, route_id, point, checks)

    source = None if source_path is None else os.fspath(source_path)
    try:
        cache_valid = cache_dir_is_valid(cache_path, source)
    except Exception as exc:  # pragma: no cover - defensive filesystem gate
        cache_valid = False
        cache_error = _exception_text(exc)
    else:
        cache_error = ""
    chunks = manifest.get("chunks")
    add_check(
        "cache_artifacts",
        cache_valid,
        reason=(cache_error or "cache_manifest_or_chunk_layout_invalid")
        if not cache_valid
        else "",
        details={
            "manifest_version": manifest.get("version"),
            "render_chunk_count": (
                len(chunks) if isinstance(chunks, Mapping) else 0
            ),
            "chunks_directory": os.path.isdir(os.path.join(cache_path, "chunks")),
            "source_path": source,
        },
    )

    render_details = _verify_render_chunks(cache_path, manifest)
    add_check(
        "render_chunk_decoding",
        bool(render_details["passed"]),
        reason=str(render_details.get("reason", "")),
        details=render_details,
    )

    selected_route_id = _select_route_id(manifest, route_id)
    if selected_route_id is None:
        add_check(
            "navigation_route",
            False,
            reason="navigation_route_missing",
        )
        return _result(profile, cache_path, route_id, point, checks)
    add_check(
        "navigation_route",
        True,
        details={"route_id": selected_route_id},
    )

    atlas: NavigationVoxelAtlas | None = None
    try:
        loaded = load_cached_navigation_voxel_volume(
            cache_path,
            manifest,
            selected_route_id,
        )
    except Exception as exc:  # pragma: no cover - defensive cache gate
        loaded = None
        navigation_load_error = _exception_text(exc)
    else:
        navigation_load_error = ""
    if isinstance(loaded, NavigationVoxelAtlas):
        atlas = loaded
    add_check(
        "navigation_artifact",
        atlas is not None and atlas.prepared_3d_graph is not None,
        reason=(
            navigation_load_error
            or "prepared_true_3d_navigation_atlas_missing"
        )
        if atlas is None or atlas.prepared_3d_graph is None
        else "",
        details=(
            {}
            if atlas is None
            else {
                "coverage_scope": atlas.coverage_scope,
                "tile_count": int(atlas.tile_count),
                "fine_tile_count": int(atlas.fine_tile_count),
                "navigation_cell_count": int(atlas.navigation_cell_count),
                "navigation_3d_cell_count": int(atlas.navigation_3d_cell_count),
                "chunk_backend": (
                    None
                    if atlas.chunk_store is None
                    else atlas.chunk_store.stats().get("backend")
                ),
            }
        ),
    )
    if atlas is None or atlas.prepared_3d_graph is None:
        return _result(profile, cache_path, selected_route_id, point, checks)

    chunk_details = _verify_navigation_chunks(atlas)
    add_check(
        "navigation_chunk_decoding",
        bool(chunk_details["passed"]),
        reason=str(chunk_details.get("reason", "")),
        details=chunk_details,
    )

    graph = atlas.prepared_3d_graph
    graph_details = _verify_graph_geometry(graph)
    add_check(
        "graph_geometry",
        bool(graph_details["passed"]),
        reason=str(graph_details.get("reason", "")),
        details=graph_details,
    )

    coverage_details = _verify_graph_coverage(
        manifest,
        selected_route_id,
        atlas,
        graph,
        strict=profile == PROFILE_FULL_CAVE,
    )
    add_check(
        "graph_coverage",
        bool(coverage_details["passed"]),
        reason=str(coverage_details.get("reason", "")),
        details=coverage_details,
    )

    try:
        mesh_guard = CachedChunkMeshCollisionGuard.from_manifest(
            manifest,
            cache_dir=cache_path,
        )
        mesh_error = ""
    except Exception as exc:  # pragma: no cover - defensive manifest gate
        mesh_guard = None
        mesh_error = _exception_text(exc)
    add_check(
        "mesh_collision_artifact",
        mesh_guard is not None,
        reason=(mesh_error or "mesh_collision_guard_unavailable")
        if mesh_guard is None
        else "",
        details={"available": mesh_guard is not None, "error": mesh_error},
    )

    settings = settings or AutoDiveSettings()
    preflight = None
    try:
        preflight = build_auto_dive_preflight_plan(
            manifest,
            current_position=point,
            current_yaw=start_yaw,
            current_pitch=start_pitch,
            settings=settings,
            cache_dir=cache_path,
        )
        preflight_details = preflight.diagnostic_payload()
        preflight_passed = (
            preflight.status == AUTO_DIVE_PREFLIGHT_READY
            and preflight.plan is not None
            and preflight.plan.preflight_validated
            and preflight.navigation_route_id == selected_route_id
            and preflight.route_point_count >= 2
            and (
                profile != PROFILE_FULL_CAVE
                or (
                    not preflight.coverage_incomplete
                    and preflight.plan.terminal_reached
                )
            )
        )
        if not preflight_passed:
            preflight_reason = _preflight_failure_reason(preflight)
    except Exception as exc:  # pragma: no cover - production boundary
        preflight_details = {"exception": _exception_text(exc)}
        preflight_passed = False
        preflight_reason = "preflight_exception"
    add_check(
        "route_preflight",
        preflight_passed,
        reason="" if preflight_passed else preflight_reason,
        details=preflight_details,
    )

    route_safety_passed = False
    route_safety_details: dict[str, object] = {}
    if preflight is not None and preflight.plan is not None:
        plan = preflight.plan
        route_keys = tuple(plan.navigation_graph_keys)
        if not route_keys:
            route_safety_details = {"reason": "preflight_graph_route_keys_missing"}
        elif mesh_guard is None:
            route_safety_details = {"reason": "mesh_collision_guard_unavailable"}
        else:
            try:
                failure = GraphRouteSafetyValidator(
                    atlas,
                    graph,
                    mesh_guard=mesh_guard,
                    policy=GraphRouteSafetyPolicy(
                        minimum_clearance_m=float(
                            settings.minimum_graph_clearance_m
                        ),
                    ),
                ).route_clearance_failure(
                    plan.route_points,
                    route_keys,
                    start_graph_key=preflight.start_graph_key,
                )
            except Exception as exc:  # pragma: no cover - artifact boundary
                failure = None
                route_safety_details = {
                    "passed": False,
                    "reason": "route_safety_exception",
                    "exception": _exception_text(exc),
                }
            else:
                route_safety_passed = failure is None
                route_safety_details = _route_failure_details(failure)
            route_safety_details.update(
                {
                    "route_point_count": len(plan.route_points),
                    "graph_key_count": len(route_keys),
                    "route_length_m": float(plan.route_length_m),
                }
            )
    else:
        route_safety_details = {"reason": "preflight_plan_missing"}
    add_check(
        "route_safety",
        route_safety_passed,
        reason=str(route_safety_details.get("reason", ""))
        if not route_safety_passed
        else "",
        details=route_safety_details,
    )

    simulation_passed = False
    simulation_details: dict[str, object]
    if preflight is not None and preflight.plan is not None:
        simulation_passed, simulation_details = _simulate_replanning(
            manifest,
            cache_path=cache_path,
            start_yaw=start_yaw,
            start_pitch=start_pitch,
            settings=settings,
            plan=preflight.plan,
            atlas=atlas,
            mesh_guard=mesh_guard,
            checkpoint_spacing_m=float(checkpoint_spacing_m),
            max_checkpoints=int(max_checkpoints),
        )
    else:
        simulation_details = {"reason": "preflight_plan_missing"}
    add_check(
        "runtime_replanning",
        simulation_passed,
        reason=str(simulation_details.get("reason", ""))
        if not simulation_passed
        else "",
        details=simulation_details,
    )

    if atlas.chunk_store is not None:
        atlas.chunk_store.close()
    return _result(profile, cache_path, selected_route_id, point, checks)


def _verify_render_chunks(
    cache_dir: str,
    manifest: Mapping[str, Any],
) -> dict[str, object]:
    chunks = manifest.get("chunks")
    if not isinstance(chunks, Mapping) or not chunks:
        return {
            "passed": False,
            "reason": "render_chunk_manifest_missing",
            "expected_count": 0,
            "decoded_count": 0,
            "invalid_count": 0,
        }
    invalid: list[dict[str, str]] = []
    decoded_count = 0
    for raw_cell in chunks:
        try:
            cell = parse_cell_key(str(raw_cell))
            load_chunk_file(cache_dir, cell)
        except Exception as exc:
            if len(invalid) < 8:
                invalid.append(
                    {"cell": str(raw_cell), "error": _exception_text(exc)}
                )
            continue
        decoded_count += 1
    return {
        "passed": not invalid and decoded_count == len(chunks),
        "reason": "render_chunk_decode_failure" if invalid else "",
        "expected_count": len(chunks),
        "decoded_count": decoded_count,
        "invalid_count": len(chunks) - decoded_count,
        "invalid_examples": invalid,
    }


def _verify_navigation_chunks(atlas: NavigationVoxelAtlas) -> dict[str, object]:
    store = atlas.chunk_store
    if store is None:
        embedded_count = len(atlas.tiles) + len(atlas.fine_tiles)
        return {
            "passed": embedded_count > 0,
            "reason": "embedded_navigation_tiles_missing"
            if embedded_count == 0
            else "",
            "backend": "embedded",
            "expected_count": embedded_count,
            "decoded_count": embedded_count,
            "load_errors": 0,
        }
    descriptors = store.descriptors()
    failures: list[dict[str, str]] = []
    decoded_count = 0
    for descriptor in descriptors:
        if store.get_chunk(descriptor.chunk_id) is None:
            if len(failures) < 8:
                failures.append(
                    {
                        "chunk_id": str(descriptor.chunk_id),
                        "path": str(descriptor.relative_path),
                    }
                )
            continue
        decoded_count += 1
    stats = store.stats()
    load_errors = int(stats.get("load_errors", 0))
    return {
        "passed": not failures and decoded_count == len(descriptors) and load_errors == 0,
        "reason": "navigation_chunk_decode_failure"
        if failures or load_errors
        else "",
        "backend": stats.get("backend"),
        "expected_count": len(descriptors),
        "decoded_count": decoded_count,
        "load_errors": load_errors,
        "failure_examples": failures,
        "max_resident_chunks": stats.get("max_resident_chunks"),
    }


def _verify_graph_geometry(graph: NavigationVoxel3DGraph) -> dict[str, object]:
    grid = tuple(float(value) for value in graph.grid_size_m)
    coordinate_failures: list[dict[str, object]] = []
    invalid_nodes = 0
    for key, node in graph.nodes.items():
        center = tuple(float(value) for value in node.center)
        node_invalid = (
            len(key) != 3
            or len(center) != 3
            or not all(math.isfinite(value) for value in center)
            or not all(math.isfinite(value) for value in grid)
            or not all(value > 0.0 for value in grid)
        )
        if node_invalid:
            invalid_nodes += 1
            continue
        mismatch_axes: list[int] = []
        for axis in range(3):
            lower = float(key[axis]) * grid[axis]
            upper = float(key[axis] + 1) * grid[axis]
            tolerance = max(1e-5, abs(grid[axis]) * 1e-6)
            if center[axis] < lower - tolerance or center[axis] >= upper + tolerance:
                mismatch_axes.append(axis)
        if mismatch_axes:
            if len(coordinate_failures) < 8:
                coordinate_failures.append(
                    {
                        "key": [int(value) for value in key],
                        "center": [float(value) for value in center],
                        "mismatch_axes": mismatch_axes,
                    }
                )

    edge_failures: list[dict[str, object]] = []
    for source, edges in graph.edges.items():
        source_node = graph.nodes.get(source)
        for edge in edges:
            target_node = graph.nodes.get(edge.target)
            reason = ""
            if source_node is None or target_node is None:
                reason = "edge_endpoint_missing"
            else:
                delta = tuple(
                    float(target_node.center[axis] - source_node.center[axis])
                    for axis in range(3)
                )
                physical_distance = math.sqrt(sum(value * value for value in delta))
                direction_norm = math.sqrt(
                    sum(float(value) ** 2 for value in edge.direction)
                )
                if not math.isfinite(float(edge.distance_m)) or edge.distance_m <= 0.0:
                    reason = "edge_distance_invalid"
                elif abs(float(edge.distance_m) - physical_distance) > max(
                    1e-4,
                    physical_distance * 1e-3,
                ):
                    reason = "edge_distance_mismatches_centers"
                elif not math.isfinite(direction_norm) or abs(direction_norm - 1.0) > 1e-3:
                    reason = "edge_direction_not_unit_length"
            if reason and len(edge_failures) < 8:
                edge_failures.append(
                    {
                        "source": [int(value) for value in source],
                        "target": [int(value) for value in edge.target],
                        "reason": reason,
                    }
                )

    edge_integrity_safe = bool(graph.edge_integrity_safe)
    motion_geometry_safe = bool(graph.motion_geometry_safe)
    coordinate_failure_count = _count_graph_coordinate_failures(graph)
    edge_failure_count = _count_graph_edge_geometry_failures(graph)
    passed = (
        edge_integrity_safe
        and motion_geometry_safe
        and invalid_nodes == 0
        and coordinate_failure_count == 0
        and edge_failure_count == 0
    )
    return {
        "passed": passed,
        "reason": (
            "graph_node_center_grid_mismatch"
            if coordinate_failure_count
            else "graph_edge_geometry_invalid"
            if edge_failure_count
            else "graph_topology_or_motion_geometry_unsafe"
            if not (edge_integrity_safe and motion_geometry_safe)
            else ""
        ),
        "node_count": len(graph.nodes),
        "edge_count": int(graph.edge_count),
        "routable_node_count": int(graph.routable_node_count),
        "component_count": int(graph.component_count),
        "grid_size_m": [float(value) for value in grid],
        "edge_integrity_safe": edge_integrity_safe,
        "motion_geometry_safe": motion_geometry_safe,
        "invalid_node_count": invalid_nodes,
        "coordinate_mismatch_count": coordinate_failure_count,
        "coordinate_mismatch_examples": coordinate_failures,
        "edge_geometry_failure_count": edge_failure_count,
        "edge_geometry_failure_examples": edge_failures,
    }


def _verify_graph_coverage(
    manifest: Mapping[str, Any],
    route_id: str,
    atlas: NavigationVoxelAtlas,
    graph: NavigationVoxel3DGraph,
    *,
    strict: bool,
) -> dict[str, object]:
    route = _route_payload(manifest, route_id)
    component_cells = _flat_pairs(
        () if route is None else route.get("component_cells", ())
    )
    metric_footprints = set(atlas.cell_metrics)
    missing_component_cells = sorted(
        set(component_cells) - metric_footprints
    )
    unknown_count = int(graph.unknown_boundary_count)
    scope_complete = atlas.coverage_scope == "entire_cave_component"
    passed = (
        bool(graph.nodes)
        and (
            not strict
            or (
                unknown_count == 0
                and scope_complete
                and not missing_component_cells
            )
        )
    )
    return {
        "passed": passed,
        "reason": (
            "unknown_graph_boundary"
            if strict and unknown_count
            else "component_footprints_missing_metrics"
            if strict and missing_component_cells
            else "navigation_coverage_scope_incomplete"
            if strict and not scope_complete
            else ""
        ),
        "strict": strict,
        "coverage_scope": atlas.coverage_scope,
        "coverage_scope_complete": scope_complete,
        "component_cell_count": len(component_cells),
        "metric_footprint_count": len(metric_footprints),
        "missing_component_cell_count": len(missing_component_cells),
        "missing_component_cell_examples": [
            [int(cell[0]), int(cell[1])] for cell in missing_component_cells[:8]
        ],
        "graph_unknown_boundary_count": unknown_count,
        "graph_terminal_count": int(graph.terminal_count),
        "graph_dead_end_count": int(graph.dead_end_count),
    }


def _simulate_replanning(
    manifest: Mapping[str, Any],
    *,
    cache_path: str,
    start_yaw: float | None,
    start_pitch: float | None,
    settings: AutoDiveSettings,
    plan: AutoDivePlan,
    atlas: NavigationVoxelAtlas,
    mesh_guard: CachedChunkMeshCollisionGuard | None,
    checkpoint_spacing_m: float,
    max_checkpoints: int,
) -> tuple[bool, dict[str, object]]:
    try:
        checkpoints = _route_checkpoints(
            plan.route_points,
            spacing_m=checkpoint_spacing_m,
            max_checkpoints=max_checkpoints,
        )
    except ValueError as exc:
        return False, {"reason": str(exc), "checkpoint_count": 0}
    if len(checkpoints) < 2:
        return False, {"reason": "route_has_no_replanning_horizon"}

    failures: list[dict[str, object]] = []
    simulated_count = 0
    initial_load_errors = _chunk_load_errors(atlas)
    for index, (current, next_point) in enumerate(
        zip(checkpoints, checkpoints[1:], strict=False)
    ):
        yaw, pitch = _direction_angles(current, next_point)
        if index == 0:
            yaw = _finite_number(start_yaw) if start_yaw is not None else yaw
            pitch = _finite_number(start_pitch) if start_pitch is not None else pitch
        try:
            runtime_plan = build_voxel_graph_auto_dive_plan(
                manifest,
                current_position=current,
                current_yaw=yaw,
                current_pitch=pitch,
                current_travel_yaw=yaw,
                current_travel_pitch=pitch,
                settings=settings,
                cache_dir=cache_path,
                route_id=plan.navigation_route_id,
            )
        except NavigationVoxelGraphAuthorityError as exc:
            failures.append(
                {
                    "checkpoint_index": index,
                    "position": _point_payload(current),
                    "reason": str(getattr(exc, "reason", "authority_error")),
                    "error": str(exc),
                }
            )
            break
        except Exception as exc:  # pragma: no cover - production boundary
            failures.append(
                {
                    "checkpoint_index": index,
                    "position": _point_payload(current),
                    "reason": "runtime_planner_exception",
                    "error": _exception_text(exc),
                }
            )
            break

        simulated_count += 1
        if runtime_plan.route_length_m <= 0.0 or len(runtime_plan.route_points) < 2:
            failures.append(
                {
                    "checkpoint_index": index,
                    "position": _point_payload(current),
                    "reason": "runtime_plan_has_no_forward_route",
                }
            )
            break
        if runtime_plan.navigation_graph is None or not runtime_plan.navigation_graph_keys:
            failures.append(
                {
                    "checkpoint_index": index,
                    "position": _point_payload(current),
                    "reason": "runtime_plan_graph_route_missing",
                }
            )
            break
        if not _has_forward_progress(current, runtime_plan.route_points, yaw, pitch):
            failures.append(
                {
                    "checkpoint_index": index,
                    "position": _point_payload(current),
                    "reason": "runtime_plan_makes_no_forward_progress",
                }
            )
            break

        runtime_atlas = runtime_plan.navigation_atlas or atlas
        runtime_guard = mesh_guard
        try:
            runtime_failure = GraphRouteSafetyValidator(
                runtime_atlas,
                runtime_plan.navigation_graph,
                mesh_guard=runtime_guard,
                policy=GraphRouteSafetyPolicy(
                    minimum_clearance_m=float(
                        settings.minimum_graph_clearance_m
                    ),
                ),
            ).route_clearance_failure(
                runtime_plan.route_points,
                runtime_plan.navigation_graph_keys,
                start_graph_key=runtime_plan.navigation_graph_keys[0],
            )
        except Exception as exc:  # pragma: no cover - artifact boundary
            runtime_failure = GraphRouteSafetyFailure(
                kind="certificate",
                reason="runtime_route_safety_exception",
            )
            runtime_failure_details = {
                "exception": _exception_text(exc),
            }
        else:
            runtime_failure_details = {}
        if runtime_failure is not None:
            failures.append(
                {
                    "checkpoint_index": index,
                    "position": _point_payload(current),
                    **_route_failure_details(runtime_failure),
                    **runtime_failure_details,
                }
            )
            break
        if _chunk_load_errors(atlas) > initial_load_errors:
            failures.append(
                {
                    "checkpoint_index": index,
                    "position": _point_payload(current),
                    "reason": "navigation_chunk_load_error_during_replan",
                }
            )
            break

    passed = not failures and simulated_count == len(checkpoints) - 1
    return passed, {
        "reason": "" if passed else (str(failures[0]["reason"]) if failures else ""),
        "checkpoint_count": len(checkpoints),
        "simulated_checkpoint_count": simulated_count,
        "failure_count": len(failures),
        "failure_examples": failures[:8],
    }


def _route_checkpoints(
    route_points: Sequence[Sequence[float]],
    *,
    spacing_m: float,
    max_checkpoints: int,
) -> tuple[Point, ...]:
    if not math.isfinite(float(spacing_m)) or float(spacing_m) <= 0.0:
        raise ValueError("checkpoint spacing must be finite and positive")
    if int(max_checkpoints) < 2:
        raise ValueError("max_checkpoints must be at least 2")
    points = tuple(_finite_point(point) for point in route_points)
    if any(point is None for point in points):
        raise ValueError("route_contains_nonfinite_point")
    resolved = tuple(point for point in points if point is not None)
    if len(resolved) < 2:
        raise ValueError("route_has_fewer_than_two_points")
    checkpoints: list[Point] = [resolved[0]]
    for first, second in zip(resolved, resolved[1:], strict=False):
        distance = _distance(first, second)
        if distance <= 1e-9:
            continue
        steps = max(1, int(math.ceil(distance / float(spacing_m))))
        for step in range(1, steps + 1):
            fraction = step / steps
            checkpoints.append(
                tuple(
                    float(first[axis] + (second[axis] - first[axis]) * fraction)
                    for axis in range(3)
                )
            )
    if len(checkpoints) > int(max_checkpoints):
        raise ValueError(
            "route_requires_more_checkpoints_than_limit"
        )
    return tuple(checkpoints)


def _verify_graph_coordinate_counts(graph: NavigationVoxel3DGraph) -> tuple[int, int]:
    coordinate_count = 0
    invalid_count = 0
    grid = tuple(float(value) for value in graph.grid_size_m)
    for key, node in graph.nodes.items():
        center = tuple(float(value) for value in node.center)
        if not all(math.isfinite(value) and value > 0.0 for value in grid):
            invalid_count += 1
            continue
        mismatch = False
        for axis in range(3):
            lower = float(key[axis]) * grid[axis]
            upper = float(key[axis] + 1) * grid[axis]
            tolerance = max(1e-5, abs(grid[axis]) * 1e-6)
            if center[axis] < lower - tolerance or center[axis] >= upper + tolerance:
                mismatch = True
                break
        if mismatch:
            coordinate_count += 1
    return coordinate_count, invalid_count


def _count_graph_coordinate_failures(graph: NavigationVoxel3DGraph) -> int:
    return _verify_graph_coordinate_counts(graph)[0]


def _count_graph_edge_geometry_failures(graph: NavigationVoxel3DGraph) -> int:
    failures = 0
    for source, edges in graph.edges.items():
        source_node = graph.nodes.get(source)
        for edge in edges:
            target_node = graph.nodes.get(edge.target)
            if source_node is None or target_node is None:
                failures += 1
                continue
            delta = tuple(
                float(target_node.center[axis] - source_node.center[axis])
                for axis in range(3)
            )
            physical_distance = math.sqrt(sum(value * value for value in delta))
            direction_norm = math.sqrt(
                sum(float(value) ** 2 for value in edge.direction)
            )
            if (
                not math.isfinite(float(edge.distance_m))
                or edge.distance_m <= 0.0
                or abs(float(edge.distance_m) - physical_distance)
                > max(1e-4, physical_distance * 1e-3)
                or not math.isfinite(direction_norm)
                or abs(direction_norm - 1.0) > 1e-3
            ):
                failures += 1
    return failures


def _select_route_id(manifest: Mapping[str, Any], requested: str | None) -> str | None:
    navigation = manifest.get("navigation")
    routes = navigation.get("routes") if isinstance(navigation, Mapping) else None
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        return None
    candidates: list[tuple[float, str]] = []
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        candidate = route.get("id")
        if not isinstance(candidate, str) or not candidate:
            continue
        if requested == candidate:
            return candidate
        try:
            length = float(route.get("length_m"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(length) and length > 0.0:
            candidates.append((length, candidate))
    if requested:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[1] if candidates else None


def _route_payload(manifest: Mapping[str, Any], route_id: str) -> Mapping[str, Any] | None:
    navigation = manifest.get("navigation")
    routes = navigation.get("routes") if isinstance(navigation, Mapping) else None
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        return None
    for route in routes:
        if isinstance(route, Mapping) and route.get("id") == route_id:
            return route
    return None


def _flat_pairs(raw: object) -> tuple[tuple[int, int], ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    values = tuple(raw)
    if len(values) % 2:
        return ()
    try:
        return tuple(
            (int(values[index]), int(values[index + 1]))
            for index in range(0, len(values), 2)
        )
    except (TypeError, ValueError):
        return ()


def _preflight_failure_reason(preflight: object) -> str:
    reason = str(getattr(preflight, "reason", "preflight_failed"))
    if bool(getattr(preflight, "coverage_incomplete", False)):
        return "preflight_coverage_incomplete"
    if not bool(getattr(preflight, "plan", None)):
        return reason
    plan = getattr(preflight, "plan", None)
    if plan is not None and not bool(getattr(plan, "terminal_reached", False)):
        return "preflight_terminal_not_reached"
    return reason


def _route_failure_details(
    failure: GraphRouteSafetyFailure | None,
) -> dict[str, object]:
    if failure is None:
        return {"passed": True, "reason": ""}
    return {
        "passed": False,
        "reason": str(failure.reason),
        "failure": failure.diagnostic_payload(),
    }


def _has_forward_progress(
    current: Point,
    route_points: Sequence[Sequence[float]],
    yaw: float | None,
    pitch: float | None,
) -> bool:
    if len(route_points) < 2:
        return False
    direction = _direction_from_radians(yaw, pitch)
    if direction is None:
        return _distance(current, tuple(float(value) for value in route_points[-1])) > 0.25
    best_projection = -math.inf
    for raw_point in route_points[1:]:
        point = tuple(float(value) for value in raw_point)
        delta = tuple(point[axis] - current[axis] for axis in range(3))
        projection = sum(delta[axis] * float(direction[axis]) for axis in range(3))
        best_projection = max(best_projection, projection)
    return best_projection > 0.25


def _direction_angles(first: Point, second: Point) -> tuple[float, float]:
    delta = tuple(second[axis] - first[axis] for axis in range(3))
    horizontal = math.hypot(delta[0], delta[2])
    return math.atan2(delta[2], delta[0]), math.atan2(delta[1], horizontal)


def _chunk_load_errors(atlas: NavigationVoxelAtlas) -> int:
    if atlas.chunk_store is None:
        return 0
    try:
        return int(atlas.chunk_store.stats().get("load_errors", 0))
    except Exception:
        return 1


def _result(
    profile: str,
    cache_dir: str,
    route_id: str | None,
    point: Point | None,
    checks: Sequence[NavigationCertificateCheck],
) -> NavigationCertificateResult:
    resolved = tuple(checks)
    return NavigationCertificateResult(
        passed=bool(resolved) and all(check.passed for check in resolved),
        profile=profile,
        cache_dir=cache_dir,
        route_id=route_id,
        start_position=point,
        checks=resolved,
    )


def _finite_point(value: Sequence[float] | object) -> Point | None:
    if isinstance(value, (str, bytes)):
        return None
    try:
        values = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        return None
    if len(values) != 3:
        return None
    try:
        point = tuple(float(item) for item in values)
    except (TypeError, ValueError):
        return None
    return point if all(math.isfinite(item) for item in point) else None  # type: ignore[return-value]


def _finite_number(value: object) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _point_payload(point: Point | None) -> list[float] | None:
    return None if point is None else [float(value) for value in point]


def _distance(first: Point, second: Point) -> float:
    return math.sqrt(
        sum((float(first[axis]) - float(second[axis])) ** 2 for axis in range(3))
    )


def _exception_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Certify a CaveViewer cache before starting the GUI. The full-cave "
            "profile only passes when an offline route and runtime replans "
            "reach a known terminal."
        )
    )
    parser.add_argument(
        "--cache-dir",
        required=True,
        help="Cache directory containing manifest.json.",
    )
    parser.add_argument(
        "--start",
        nargs=3,
        type=float,
        required=True,
        metavar=("X", "Y", "Z"),
        help="Initial camera position in world coordinates.",
    )
    parser.add_argument("--yaw", type=float, help="Initial camera yaw in radians.")
    parser.add_argument("--pitch", type=float, help="Initial camera pitch in radians.")
    parser.add_argument("--source", help="Optional source model for stale-cache checking.")
    parser.add_argument("--route-id", help="Explicit cached navigation route ID.")
    parser.add_argument(
        "--profile",
        choices=CERTIFICATE_PROFILES,
        default=PROFILE_FULL_CAVE,
        help="full-cave requires complete coverage; frontier allows temporary boundaries.",
    )
    parser.add_argument(
        "--checkpoint-spacing-m",
        type=float,
        default=DEFAULT_CHECKPOINT_SPACING_M,
        help="Distance between offline runtime-replanning checkpoints.",
    )
    parser.add_argument(
        "--max-checkpoints",
        type=int,
        default=DEFAULT_MAX_CHECKPOINTS,
        help="Hard limit for simulated checkpoints.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the navigation certificate CLI and return a process exit code."""
    args = _parser().parse_args(argv)
    cache_dir = Path(args.cache_dir)
    manifest = load_manifest(cache_dir)
    if manifest is None:
        payload = {
            "certificate": "FAIL",
            "passed": False,
            "profile": args.profile,
            "cache_dir": str(cache_dir),
            "route_id": args.route_id,
            "start_position": [float(value) for value in args.start],
            "checks": [
                {
                    "name": "manifest",
                    "passed": False,
                    "reason": "cache_manifest_missing_or_unreadable",
                    "details": {},
                }
            ],
            "failed_checks": ["manifest"],
        }
        _print_result(payload, json_output=bool(args.json))
        return 1

    result = certify_navigation_cache(
        manifest,
        cache_dir=cache_dir,
        start_position=args.start,
        start_yaw=args.yaw,
        start_pitch=args.pitch,
        source_path=args.source,
        route_id=args.route_id,
        profile=args.profile,
        checkpoint_spacing_m=args.checkpoint_spacing_m,
        max_checkpoints=args.max_checkpoints,
    )
    _print_result(result.diagnostic_payload(), json_output=bool(args.json))
    return 0 if result.passed else 1


def _print_result(payload: Mapping[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"NAVIGATION CERTIFICATE: {payload.get('certificate', 'FAIL')}")
    for check in payload.get("checks", ()):
        if not isinstance(check, Mapping):
            continue
        status = "PASS" if check.get("passed") else "FAIL"
        reason = str(check.get("reason") or "")
        suffix = f" — {reason}" if reason else ""
        print(f"{str(check.get('name', 'check'))}: {status}{suffix}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
