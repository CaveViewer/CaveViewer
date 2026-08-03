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
    AUTO_DIVE_ROUTE_GOAL_FARTHEST_TERMINAL,
    AutoDivePlan,
    AutoDiveSettings,
    DEFAULT_AUTO_DIVE_ROUTE_GOAL,
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
from caveviewer.core.navigation.fixed_voxels import (
    FIXED_ORTHOGONAL_VOXEL_METHOD,
)
from caveviewer.core.navigation.cubic_graph import CUBIC_VOXEL_GRAPH_METHOD
from caveviewer.core.navigation.voxel_cache import (
    MAX_CACHE_FIXED_VOXEL_SIZE_M,
    MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M,
    MAX_OBJ_SOURCE_INGRESS_SNAP_DISTANCE_M,
    NAVIGATION_ROUTE_SELECTION_LONGEST_SAFE_NON_CIRCULAR,
    NAVIGATION_VOXEL_CACHE_METHOD,
    NAVIGATION_VOXEL_CACHE_MAX_BYTES,
    NAVIGATION_VOXEL_CACHE_VERSION,
    NavigationVoxelAtlas,
    first_manifest_chunk_center_for_route_contract,
    load_cached_navigation_voxel_volume,
    navigation_route_contract_rebuild_reason,
)
from caveviewer.core.navigation.voxel_store import (
    NAVIGATION_VOXEL_CHUNK_STORAGE_METHOD,
)
from caveviewer.core.navigation.voxel_graph_3d import (
    NavigationVoxel3DGraph,
    VoxelGraphKey,
    shortest_navigation_voxel_3d_graph_path,
)


Point = tuple[float, float, float]
PROFILE_FULL_CAVE = "full-cave"
PROFILE_FRONTIER = "frontier"
CERTIFICATE_PROFILES = (PROFILE_FULL_CAVE, PROFILE_FRONTIER)
PHASE_ARTIFACTS = "artifacts"
PHASE_GRAPH = "graph"
PHASE_ROUTE = "route"
PHASE_ALL = "all"
CERTIFICATE_PHASES = (PHASE_ARTIFACTS, PHASE_GRAPH, PHASE_ROUTE, PHASE_ALL)
DEFAULT_CHECKPOINT_SPACING_M = 4.0
# Full fixed-route execution is sampled at up to one metre, so the default
# must cover a kilometre-scale certified passage without silently weakening
# the proof. Longer routes still fail closed unless the caller opts in.
DEFAULT_MAX_CHECKPOINTS = 4_096


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
    phase: str
    profile: str
    cache_dir: str
    route_id: str | None
    start_position: Point | None
    checks: tuple[NavigationCertificateCheck, ...]

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "certificate": "PASS" if self.passed else "FAIL",
            "passed": bool(self.passed),
            "phase": str(self.phase),
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
    start_position: Sequence[float] | None = None,
    start_yaw: float | None = None,
    start_pitch: float | None = None,
    source_path: str | os.PathLike[str] | None = None,
    route_id: str | None = None,
    profile: str = PROFILE_FULL_CAVE,
    phase: str = PHASE_ALL,
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
    if phase not in CERTIFICATE_PHASES:
        raise ValueError(f"unsupported navigation certificate phase: {phase!r}")
    if not math.isfinite(float(checkpoint_spacing_m)) or checkpoint_spacing_m <= 0.0:
        raise ValueError("checkpoint spacing must be finite and positive")
    if int(max_checkpoints) < 2:
        raise ValueError("max_checkpoints must be at least 2")

    cache_path = os.path.abspath(os.fspath(cache_dir))
    point = (
        None
        if start_position is None
        else _finite_point(start_position)
    )
    requires_start = phase in {PHASE_ROUTE, PHASE_ALL}
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

    if requires_start or start_position is not None:
        add_check(
            "input",
            point is not None,
            reason=(
                "start_position_required_for_phase"
                if start_position is None and requires_start
                else ""
                if point is not None
                else "start_position_must_be_finite_xyz"
            ),
            details={
                "start_position": _point_payload(point),
                "start_yaw": _finite_number(start_yaw),
                "start_pitch": _finite_number(start_pitch),
            },
        )
    if requires_start and point is None:
        return _result(phase, profile, cache_path, route_id, point, checks)

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
        return _result(phase, profile, cache_path, route_id, point, checks)
    add_check(
        "navigation_route",
        True,
        details={"route_id": selected_route_id},
    )

    artifact_details = _verify_navigation_artifact_index(
        cache_path,
        manifest,
        selected_route_id,
    )
    add_check(
        "navigation_artifact_index",
        bool(artifact_details["passed"]),
        reason=str(artifact_details.get("reason", "")),
        details=artifact_details,
    )
    route_contract = _verify_navigation_route_contract(
        manifest,
        selected_route_id,
    )
    add_check(
        "navigation_route_contract",
        bool(route_contract["passed"]),
        reason=str(route_contract.get("reason", "")),
        details=route_contract,
    )
    if phase == PHASE_ARTIFACTS or not bool(route_contract["passed"]):
        return _result(
            phase,
            profile,
            cache_path,
            selected_route_id,
            point,
            checks,
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
    atlas_policy_valid = bool(
        atlas is not None
        and 0.0 < atlas.fixed_isotropic_voxel_size_m
        <= MAX_CACHE_FIXED_VOXEL_SIZE_M + 1e-9
        and 0.0 < atlas.fixed_vertical_voxel_size_m
        <= MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9
        and atlas.surface_overlap_occupied_wins
        and atlas.coverage_scope == "certified_terminal_route"
    )
    artifact_reason = ""
    if navigation_load_error:
        artifact_reason = navigation_load_error
    elif atlas is None:
        artifact_reason = "prepared_mesh_navigation_atlas_missing"
    elif atlas.prepared_3d_graph is None or atlas.prepared_mesh_graph is None:
        artifact_reason = "prepared_navigation_graph_missing"
    elif not atlas_policy_valid:
        artifact_reason = "fixed_navigation_policy_invalid"
    add_check(
        "navigation_artifact",
        (
            atlas is not None
            and atlas.prepared_3d_graph is not None
            and atlas.prepared_mesh_graph is not None
            and atlas_policy_valid
        ),
        reason=artifact_reason,
        details=(
            {}
            if atlas is None
            else {
                "coverage_scope": atlas.coverage_scope,
                "tile_count": int(atlas.tile_count),
                "fine_tile_count": int(atlas.fine_tile_count),
                "navigation_cell_count": int(atlas.navigation_cell_count),
                "navigation_3d_cell_count": int(atlas.navigation_3d_cell_count),
                "mesh_navigation_cell_count": int(atlas.mesh_navigation_cell_count),
                "fixed_isotropic_voxel_size_m": float(
                    atlas.fixed_isotropic_voxel_size_m
                ),
                "fixed_vertical_voxel_size_m": float(
                    atlas.fixed_vertical_voxel_size_m
                ),
                "fixed_voxel_cell_size_m": [
                    float(value) for value in atlas.fixed_voxel_cell_size_m
                ],
                "surface_overlap_policy": (
                    "occupied_wins"
                    if atlas.surface_overlap_occupied_wins
                    else "legacy_free_preferred"
                ),
                "chunk_backend": (
                    None
                    if atlas.chunk_store is None
                    else atlas.chunk_store.stats().get("backend")
                ),
            }
        ),
    )
    if (
        atlas is None
        or atlas.prepared_3d_graph is None
        or atlas.prepared_mesh_graph is None
        or not atlas_policy_valid
    ):
        return _result(
            phase,
            profile,
            cache_path,
            selected_route_id,
            point,
            checks,
        )

    chunk_details = _verify_navigation_chunks(atlas)
    add_check(
        "navigation_chunk_decoding",
        bool(chunk_details["passed"]),
        reason=str(chunk_details.get("reason", "")),
        details=chunk_details,
    )

    graph = atlas.prepared_mesh_graph
    graph_details = _verify_graph_geometry(
        graph,
        max_vertical_grid_size_m=MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M,
    )
    add_check(
        "graph_geometry",
        bool(graph_details["passed"]),
        reason=str(graph_details.get("reason", "")),
        details=graph_details,
    )

    route_binding_details = _verify_prepared_route_binding(
        manifest,
        selected_route_id,
        graph,
    )
    add_check(
        "graph_route_binding",
        bool(route_binding_details["passed"]),
        reason=str(route_binding_details.get("reason", "")),
        details=route_binding_details,
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

    if phase == PHASE_GRAPH:
        if atlas.chunk_store is not None:
            atlas.chunk_store.close()
        return _result(
            phase,
            profile,
            cache_path,
            selected_route_id,
            point,
            checks,
        )

    if point is None:
        if atlas.chunk_store is not None:
            atlas.chunk_store.close()
        return _result(
            phase,
            profile,
            cache_path,
            selected_route_id,
            point,
            checks,
        )
    if settings is None:
        settings = AutoDiveSettings(
            route_goal=(
                AUTO_DIVE_ROUTE_GOAL_FARTHEST_TERMINAL
                if profile == PROFILE_FRONTIER
                else DEFAULT_AUTO_DIVE_ROUTE_GOAL
            )
        )
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
                    and preflight.plan.fixed_route
                    and not preflight.plan.replan_at_end
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
        route_safety_passed, route_safety_details = _validate_published_route(
            preflight.plan,
            atlas=atlas,
            graph=graph,
            mesh_guard=mesh_guard,
            settings=settings,
            start_graph_key=preflight.start_graph_key,
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
        if preflight.plan.fixed_route:
            simulation_passed, simulation_details = _simulate_fixed_route_execution(
                plan=preflight.plan,
                atlas=atlas,
                graph=graph,
                mesh_guard=mesh_guard,
                settings=settings,
                checkpoint_spacing_m=float(checkpoint_spacing_m),
                max_checkpoints=int(max_checkpoints),
            )
        else:
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
    return _result(phase, profile, cache_path, selected_route_id, point, checks)


def _verify_navigation_artifact_index(
    cache_dir: str,
    manifest: Mapping[str, Any],
    route_id: str,
) -> dict[str, object]:
    """Verify navigation paths and counts without deserializing the graph.

    This is the fast phase used immediately after cache construction.  The
    sidecar and chunk files must exist and remain inside the selected cache,
    but the large JSON graph is intentionally left unopened for the graph
    phase.  That keeps an artifact failure cheap to report and makes the
    expensive graph load an explicit post-build operation.
    """
    navigation = manifest.get("navigation")
    voxel_cache = (
        navigation.get("voxel_cache")
        if isinstance(navigation, Mapping)
        else None
    )
    failures: list[str] = []
    if not isinstance(voxel_cache, Mapping):
        return {
            "passed": False,
            "reason": "navigation_voxel_cache_metadata_missing",
            "route_id": route_id,
        }
    if (
        voxel_cache.get("version") != NAVIGATION_VOXEL_CACHE_VERSION
        or voxel_cache.get("method") != NAVIGATION_VOXEL_CACHE_METHOD
    ):
        failures.append("navigation_cache_rebuild_required")
    if voxel_cache.get("storage_method") != NAVIGATION_VOXEL_CHUNK_STORAGE_METHOD:
        failures.append("navigation_chunk_storage_method_invalid")
    if voxel_cache.get("fixed_voxel_method") != FIXED_ORTHOGONAL_VOXEL_METHOD:
        failures.append("navigation_fixed_voxel_method_invalid")
    if voxel_cache.get("cubic_graph_method") != CUBIC_VOXEL_GRAPH_METHOD:
        failures.append("navigation_cubic_graph_method_invalid")
    try:
        fixed_size_m = float(voxel_cache.get("fixed_isotropic_voxel_size_m"))
    except (TypeError, ValueError):
        fixed_size_m = math.inf
    if (
        not math.isfinite(fixed_size_m)
        or fixed_size_m <= 0.0
        or fixed_size_m > MAX_CACHE_FIXED_VOXEL_SIZE_M + 1e-9
    ):
        failures.append("navigation_fixed_voxel_size_invalid")
    try:
        fixed_vertical_size_m = float(
            voxel_cache.get("fixed_vertical_voxel_size_m")
        )
    except (TypeError, ValueError):
        fixed_vertical_size_m = math.inf
    if (
        not math.isfinite(fixed_vertical_size_m)
        or fixed_vertical_size_m <= 0.0
        or fixed_vertical_size_m
        > MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9
    ):
        failures.append("navigation_fixed_vertical_voxel_size_invalid")
    raw_cell_size = voxel_cache.get("fixed_voxel_cell_size_m")
    try:
        if (
            not isinstance(raw_cell_size, Sequence)
            or isinstance(raw_cell_size, (str, bytes))
            or len(raw_cell_size) != 3
        ):
            raise ValueError
        fixed_cell_size_m = tuple(float(value) for value in raw_cell_size)
    except (TypeError, ValueError):
        fixed_cell_size_m = (math.inf, math.inf, math.inf)
    if (
        not all(math.isfinite(value) and value > 0.0 for value in fixed_cell_size_m)
        or not math.isclose(
            fixed_cell_size_m[0],
            fixed_size_m,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        or not math.isclose(
            fixed_cell_size_m[2],
            fixed_size_m,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        or fixed_cell_size_m[1]
        > MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9
        or not math.isclose(
            fixed_cell_size_m[1],
            fixed_vertical_size_m,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ):
        failures.append("navigation_fixed_voxel_cell_size_invalid")
    if voxel_cache.get("surface_overlap_policy") != "occupied_wins":
        failures.append("navigation_overlap_policy_invalid")
    if voxel_cache.get("sampling_complete_required") is not True:
        failures.append("navigation_sampling_policy_invalid")

    sidecar_raw = voxel_cache.get("path")
    sidecar_path = _safe_cache_child_path(cache_dir, sidecar_raw)
    sidecar_bytes: int | None = None
    if sidecar_path is None or not os.path.isfile(sidecar_path):
        failures.append("navigation_sidecar_missing")
    else:
        try:
            sidecar_bytes = int(os.path.getsize(sidecar_path))
        except OSError:
            failures.append("navigation_sidecar_stat_failed")
        else:
            if sidecar_bytes <= 0:
                failures.append("navigation_sidecar_empty")
            elif sidecar_bytes > NAVIGATION_VOXEL_CACHE_MAX_BYTES:
                failures.append("navigation_sidecar_too_large")

    chunk_directory_raw = voxel_cache.get("chunk_directory")
    chunk_directory = _safe_cache_child_path(cache_dir, chunk_directory_raw)
    if chunk_directory is None or not os.path.isdir(chunk_directory):
        failures.append("navigation_chunk_directory_missing")

    expected_chunk_count: int | None
    try:
        expected_chunk_count = int(voxel_cache.get("chunk_count"))
    except (TypeError, ValueError):
        expected_chunk_count = None
        failures.append("navigation_chunk_count_invalid")
    actual_chunk_count = (
        _count_regular_files(chunk_directory)
        if chunk_directory is not None and os.path.isdir(chunk_directory)
        else 0
    )
    if expected_chunk_count is not None and (
        expected_chunk_count <= 0 or actual_chunk_count != expected_chunk_count
    ):
        failures.append("navigation_chunk_count_mismatch")

    return {
        "passed": not failures,
        "reason": failures[0] if failures else "",
        "route_id": route_id,
        "navigation_version": voxel_cache.get("version"),
        "navigation_method": voxel_cache.get("method"),
        "sidecar_path": sidecar_raw,
        "sidecar_bytes": sidecar_bytes,
        "sidecar_max_bytes": NAVIGATION_VOXEL_CACHE_MAX_BYTES,
        "chunk_directory": chunk_directory_raw,
        "expected_chunk_count": expected_chunk_count,
        "actual_chunk_count": actual_chunk_count,
        "storage_method": voxel_cache.get("storage_method"),
        "fixed_voxel_method": voxel_cache.get("fixed_voxel_method"),
        "cubic_graph_method": voxel_cache.get("cubic_graph_method"),
        "fixed_isotropic_voxel_size_m": fixed_size_m,
        "fixed_vertical_voxel_size_m": fixed_vertical_size_m,
        "fixed_voxel_cell_size_m": [
            float(value) for value in fixed_cell_size_m
        ],
        "surface_overlap_policy": voxel_cache.get("surface_overlap_policy"),
        "sampling_complete_required": voxel_cache.get(
            "sampling_complete_required"
        ),
    }


def _verify_navigation_route_contract(
    manifest: Mapping[str, Any],
    route_id: str,
) -> dict[str, object]:
    """Reject a safe-looking route that starts late or ends early.

    Geometry certification alone cannot tell whether cache construction chose
    the real cave entrance: a collision-free midpoint-to-end path is still the
    wrong Guided Dive. This fast manifest gate independently binds an OBJ
    import to declaration-order vertex zero, or honors an explicit entrance
    sidecar override, then requires the exact ingress attachment and the
    complete source-hint span.
    """
    navigation = manifest.get("navigation")
    if not isinstance(navigation, Mapping):
        return {
            "passed": False,
            "reason": "navigation_metadata_missing",
            "route_id": route_id,
        }
    routes = navigation.get("routes")
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        return {
            "passed": False,
            "reason": "navigation_routes_missing",
            "route_id": route_id,
        }
    route = next(
        (
            item
            for item in routes
            if isinstance(item, Mapping) and item.get("id") == route_id
        ),
        None,
    )
    if not isinstance(route, Mapping):
        return {
            "passed": False,
            "reason": "navigation_route_missing",
            "route_id": route_id,
        }

    rebuild_reason = navigation_route_contract_rebuild_reason(
        navigation,
        manifest_chunks=manifest.get("chunks"),
    )
    if rebuild_reason is not None:
        return {
            "passed": False,
            "reason": (
                "navigation_route_selection_unresolved"
                if rebuild_reason
                in {
                    "longer_route_search_capacity_limited",
                    "longer_route_ordering_evidence_missing",
                }
                else "navigation_route_contract_stale"
            ),
            "route_id": route_id,
            "rebuild_reason": rebuild_reason,
        }

    corridor = route.get("voxel_corridor")
    graph_details = (
        corridor.get("prepared_mesh_graph")
        if isinstance(corridor, Mapping)
        else None
    )
    if (
        not isinstance(corridor, Mapping)
        or not isinstance(graph_details, Mapping)
    ):
        return {
            "passed": False,
            "reason": "navigation_route_proof_missing",
            "route_id": route_id,
        }

    try:
        source_count = int(corridor["source_route_point_count"])
        ingress_index = int(corridor["certified_ingress_hint_index"])
        terminal_index = int(corridor["certified_terminal_hint_index"])
        selected_start = int(corridor["selected_source_hint_start_index"])
        selected_end = int(corridor["selected_source_hint_end_index"])
    except (KeyError, TypeError, ValueError):
        source_count = 0
        ingress_index = -1
        terminal_index = -1
        selected_start = -1
        selected_end = -1
    full_source_span = bool(
        source_count >= 2
        and ingress_index == 0
        and terminal_index == source_count - 1
        and selected_start == 0
        and selected_end == source_count - 1
        and corridor.get("complete_ingress_route") is True
        and graph_details.get("known_terminal_reached") is True
    )
    if (
        route.get("closed_loop") is not False
        or corridor.get("built") is not True
        or not full_source_span
    ):
        return {
            "passed": False,
            "reason": "navigation_source_route_incomplete",
            "route_id": route_id,
            "source_route_point_count": source_count,
            "certified_ingress_hint_index": ingress_index,
            "certified_terminal_hint_index": terminal_index,
            "selected_source_hint_start_index": selected_start,
            "selected_source_hint_end_index": selected_end,
            "complete_ingress_route": corridor.get("complete_ingress_route"),
        }

    certified_start = _finite_point(route.get("certified_start_position"))
    raw_start = navigation.get("navigation_start")
    declared_start = (
        _finite_point(raw_start.get("position"))
        if isinstance(raw_start, Mapping)
        else _finite_point(raw_start)
    )
    inferred_start = first_manifest_chunk_center_for_route_contract(
        manifest.get("chunks")
    )
    start_source = raw_start.get("source") if isinstance(raw_start, Mapping) else None
    raw_obj_anchor = navigation.get("navigation_start_anchor")
    obj_anchor = (
        _finite_point(raw_obj_anchor.get("position"))
        if isinstance(raw_obj_anchor, Mapping)
        else None
    )

    if declared_start is None:
        if obj_anchor is None:
            return {
                "passed": False,
                "reason": "navigation_start_missing",
                "route_id": route_id,
            }
        attachment_valid, attachment_details = _ingress_attachment_contract(
            graph_details,
            source_point=obj_anchor,
            certified_start=certified_start,
            connector_required=False,
            attachment_mode="non_executable_obj_surface_anchor_snap",
            require_mesh_clear=False,
        )
        return {
            "passed": bool(
                attachment_valid
                and route.get("starts_at_navigation_start_anchor") is True
            ),
            "reason": (
                ""
                if attachment_valid
                and route.get("starts_at_navigation_start_anchor") is True
                else "navigation_start_attachment_invalid"
            ),
            "route_id": route_id,
            "start_policy": "obj_declaration_order_vertex_zero_anchor",
            "full_source_span": full_source_span,
            **attachment_details,
        }

    if (
        start_source == "first_manifest_chunk_center_v1"
        and (
            inferred_start is None
            or not _points_match(declared_start, inferred_start)
        )
    ):
        return {
            "passed": False,
            "reason": "navigation_inferred_start_mismatch",
            "route_id": route_id,
            "declared_start": _point_payload(declared_start),
            "expected_inferred_start": _point_payload(inferred_start),
        }
    if obj_anchor is not None or route.get("starts_at_navigation_start") is not True:
        return {
            "passed": False,
            "reason": "navigation_start_policy_invalid",
            "route_id": route_id,
        }

    attachment_valid, attachment_details = _ingress_attachment_contract(
        graph_details,
        source_point=declared_start,
        certified_start=certified_start,
        connector_required=True,
        attachment_mode="executable_authored_start_connector",
        require_mesh_clear=True,
    )
    return {
        "passed": attachment_valid,
        "reason": (
            "" if attachment_valid else "navigation_start_attachment_invalid"
        ),
        "route_id": route_id,
        "start_policy": (
            "inferred_first_spatial_manifest_chunk"
            if start_source == "first_manifest_chunk_center_v1"
            else "authored_navigation_start"
        ),
        "declared_start": _point_payload(declared_start),
        "expected_inferred_start": _point_payload(inferred_start),
        "full_source_span": full_source_span,
        "source_route_point_count": source_count,
        **attachment_details,
    }


def _ingress_attachment_contract(
    graph_details: Mapping[str, object],
    *,
    source_point: Point,
    certified_start: Point | None,
    connector_required: bool,
    attachment_mode: str,
    require_mesh_clear: bool,
) -> tuple[bool, dict[str, object]]:
    recorded_source = _finite_point(graph_details.get("source_ingress_point"))
    attachment = _finite_point(
        graph_details.get("source_ingress_attachment_point")
    )
    try:
        recorded_distance_m = float(
            graph_details["source_ingress_attachment_distance_m"]
        )
        snap_limit_m = float(graph_details["source_ingress_snap_limit_m"])
    except (KeyError, TypeError, ValueError):
        recorded_distance_m = math.inf
        snap_limit_m = math.inf
    actual_distance_m = (
        math.inf
        if attachment is None
        else math.dist(source_point, attachment)
    )
    passed = bool(
        certified_start is not None
        and recorded_source is not None
        and attachment is not None
        and graph_details.get("source_ingress_required") is True
        and graph_details.get("source_ingress_connector_required")
        is connector_required
        and graph_details.get("source_ingress_attachment_mode")
        == attachment_mode
        and graph_details.get("source_ingress_coordinate_space") == "xyz"
        and (
            not require_mesh_clear
            or graph_details.get("source_ingress_connector_mesh_clear") is True
        )
        and _points_match(recorded_source, source_point)
        and _points_match(attachment, certified_start)
        and math.isfinite(recorded_distance_m)
        and recorded_distance_m >= 0.0
        and math.isfinite(snap_limit_m)
        and 0.0 < snap_limit_m
        <= MAX_OBJ_SOURCE_INGRESS_SNAP_DISTANCE_M + 1e-9
        and actual_distance_m <= snap_limit_m + 1e-9
        and abs(actual_distance_m - recorded_distance_m) <= 1e-6
    )
    return passed, {
        "source_ingress_point": _point_payload(recorded_source),
        "source_ingress_attachment_point": _point_payload(attachment),
        "source_ingress_attachment_distance_m": recorded_distance_m,
        "source_ingress_snap_limit_m": snap_limit_m,
        "source_ingress_connector_required": connector_required,
    }


def _points_match(first: Point, second: Point) -> bool:
    return all(
        abs(first[axis] - second[axis]) <= 1e-6
        for axis in range(3)
    )


def _safe_cache_child_path(cache_dir: str, raw_path: object) -> str | None:
    if not isinstance(raw_path, str) or not raw_path or os.path.isabs(raw_path):
        return None
    root = os.path.realpath(cache_dir)
    candidate = os.path.realpath(os.path.join(root, raw_path))
    try:
        if os.path.commonpath((root, candidate)) != root:
            return None
    except ValueError:
        return None
    return candidate


def _count_regular_files(directory: str | None) -> int:
    if directory is None:
        return 0
    count = 0
    try:
        for _root, _directories, files in os.walk(directory):
            count += sum(
                1
                for name in files
                if not name.startswith(".")
            )
    except OSError:
        return 0
    return count


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
        "passed": (
            not failures
            and decoded_count == len(descriptors)
            and load_errors == 0
        ),
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


def _verify_graph_geometry(
    graph: NavigationVoxel3DGraph,
    *,
    max_vertical_grid_size_m: float | None = None,
) -> dict[str, object]:
    grid = tuple(float(value) for value in graph.grid_size_m)
    vertical_resolution_valid = bool(
        max_vertical_grid_size_m is None
        or (
            math.isfinite(float(max_vertical_grid_size_m))
            and float(max_vertical_grid_size_m) > 0.0
            and math.isfinite(grid[1])
            and 0.0 < grid[1] <= float(max_vertical_grid_size_m) + 1e-9
        )
    )
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
        vertical_resolution_valid
        and
        edge_integrity_safe
        and motion_geometry_safe
        and invalid_nodes == 0
        and coordinate_failure_count == 0
        and edge_failure_count == 0
    )
    return {
        "passed": passed,
        "reason": (
            "graph_vertical_resolution_too_coarse"
            if not vertical_resolution_valid
            else "graph_node_center_grid_mismatch"
            if coordinate_failure_count
            else "graph_edge_geometry_invalid"
            if edge_failure_count
            else "graph_topology_or_motion_geometry_unsafe"
            if not (edge_integrity_safe and motion_geometry_safe)
            else ""
        ),
        "method": str(graph.method),
        "node_count": len(graph.nodes),
        "edge_count": int(graph.edge_count),
        "routable_node_count": int(graph.routable_node_count),
        "component_count": int(graph.component_count),
        "grid_size_m": [float(value) for value in grid],
        "max_vertical_grid_size_m": (
            None
            if max_vertical_grid_size_m is None
            else float(max_vertical_grid_size_m)
        ),
        "vertical_resolution_valid": vertical_resolution_valid,
        "edge_integrity_safe": edge_integrity_safe,
        "motion_geometry_safe": motion_geometry_safe,
        "invalid_node_count": invalid_nodes,
        "coordinate_mismatch_count": coordinate_failure_count,
        "coordinate_mismatch_examples": coordinate_failures,
        "edge_geometry_failure_count": edge_failure_count,
        "edge_geometry_failure_examples": edge_failures,
    }


def _strict_certificate_int(value: object) -> int | None:
    return value if type(value) is int else None


def _strict_certificate_number(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _strict_certificate_point(value: object) -> Point | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 3
    ):
        return None
    coordinates = tuple(_strict_certificate_number(item) for item in value)
    if any(item is None for item in coordinates):
        return None
    return coordinates  # type: ignore[return-value]


def _strict_certificate_flat_points(value: object) -> tuple[Point, ...] | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) < 6
        or len(value) % 3
    ):
        return None
    points: list[Point] = []
    for index in range(0, len(value), 3):
        point = _strict_certificate_point(value[index : index + 3])
        if point is None:
            return None
        points.append(point)
    return tuple(points)


def _strict_certificate_flat_cells(
    value: object,
) -> tuple[tuple[int, int], ...] | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
        or len(value) % 2
    ):
        return None
    if any(type(item) is not int for item in value):
        return None
    return tuple(
        (value[index], value[index + 1])
        for index in range(0, len(value), 2)
    )


def _strict_certificate_key(value: object) -> VoxelGraphKey | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 3
        or any(type(item) is not int for item in value)
    ):
        return None
    return tuple(value)  # type: ignore[return-value]


def _strict_certificate_component_intervals(
    value: object,
) -> dict[tuple[int, int], tuple[tuple[float, float], ...]] | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
        or len(value) % 4
    ):
        return None
    parsed: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for index in range(0, len(value), 4):
        raw_x, raw_z, raw_low, raw_high = value[index : index + 4]
        if type(raw_x) is not int or type(raw_z) is not int:
            return None
        low_y = _strict_certificate_number(raw_low)
        high_y = _strict_certificate_number(raw_high)
        if low_y is None or high_y is None or high_y <= low_y + 1e-9:
            return None
        parsed.setdefault((raw_x, raw_z), []).append((low_y, high_y))
    return {
        cell: tuple(intervals) for cell, intervals in parsed.items()
    }


def _strict_certificate_selected_intervals(
    value: object,
) -> tuple[tuple[float, float], ...] | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
        or len(value) % 2
    ):
        return None
    intervals: list[tuple[float, float]] = []
    for index in range(0, len(value), 2):
        low_y = _strict_certificate_number(value[index])
        high_y = _strict_certificate_number(value[index + 1])
        if low_y is None or high_y is None or high_y <= low_y + 1e-9:
            return None
        intervals.append((low_y, high_y))
    return tuple(intervals)


def _verify_prepared_route_binding(
    manifest: Mapping[str, Any],
    route_id: str,
    graph: NavigationVoxel3DGraph,
) -> dict[str, object]:
    """Replay immutable source-route gates against the graph actually loaded."""
    route = _route_payload(manifest, route_id)
    corridor = route.get("voxel_corridor") if isinstance(route, Mapping) else None
    proof = (
        corridor.get("prepared_mesh_graph")
        if isinstance(corridor, Mapping)
        else None
    )
    if (
        not isinstance(route, Mapping)
        or not isinstance(corridor, Mapping)
        or not isinstance(proof, Mapping)
    ):
        return {
            "passed": False,
            "reason": "prepared_route_binding_metadata_missing",
            "route_id": route_id,
        }

    source_count = _strict_certificate_int(
        corridor.get("source_route_point_count")
    )
    source_cell_count = _strict_certificate_int(
        corridor.get("source_route_cell_count")
    )
    source_start = _strict_certificate_int(
        corridor.get("selected_source_hint_start_index")
    )
    source_end = _strict_certificate_int(
        corridor.get("selected_source_hint_end_index")
    )
    ingress_index = _strict_certificate_int(
        corridor.get("certified_ingress_hint_index")
    )
    terminal_index = _strict_certificate_int(
        corridor.get("certified_terminal_hint_index")
    )
    terminal_hint_index = _strict_certificate_int(
        proof.get("selected_terminal_hint_index")
    )
    terminal_hint_count = _strict_certificate_int(
        proof.get("terminal_hint_count")
    )
    summary_terminal_count = _strict_certificate_int(
        proof.get("terminal_count")
    )
    summary_unknown_count = _strict_certificate_int(
        proof.get("unknown_boundary_count")
    )
    source_points = _strict_certificate_flat_points(
        corridor.get("source_route_points")
    )
    source_cells = _strict_certificate_flat_cells(
        corridor.get("source_route_cells")
    )
    published_points = _strict_certificate_flat_points(route.get("points"))
    published_cells = _strict_certificate_flat_cells(route.get("cells"))
    proof_route_cells = _strict_certificate_flat_cells(
        proof.get("surface_gap_route_cells")
    )
    selected_intervals = _strict_certificate_selected_intervals(
        proof.get("surface_gap_selected_route_intervals")
    )
    component_intervals = _strict_certificate_component_intervals(
        route.get("component_vertical_gap_intervals")
    )
    source_start_point = _strict_certificate_point(
        corridor.get("source_route_start_point")
    )
    source_terminal_point = _strict_certificate_point(
        corridor.get("source_route_terminal_point")
    )
    requested_terminal = _strict_certificate_point(
        proof.get("requested_terminal_point")
    )
    selected_hint = _strict_certificate_point(
        proof.get("selected_terminal_hint_point")
    )
    certified_start = _strict_certificate_point(
        route.get("certified_start_position")
    )
    ingress_attachment = _strict_certificate_point(
        proof.get("source_ingress_attachment_point")
    )
    persisted_seed_key = _strict_certificate_key(proof.get("seed_graph_key"))
    persisted_terminal_key = _strict_certificate_key(
        proof.get("terminal_graph_key")
    )
    footprint_cell_size = _strict_certificate_number(
        corridor.get("source_route_footprint_cell_size_m")
    )
    fixed_vertical_size = _strict_certificate_number(
        corridor.get("fixed_vertical_voxel_size_m")
    )
    terminal_snap_limit = _strict_certificate_number(
        proof.get("terminal_snap_limit_m")
    )
    proof_gate_count = _strict_certificate_int(
        proof.get("surface_gap_route_cell_count")
    )
    persisted_path_node_count = _strict_certificate_int(
        proof.get("persisted_path_node_count")
    )
    persisted_path_edge_count = _strict_certificate_int(
        proof.get("persisted_path_edge_count")
    )
    proof_graph_distance = _strict_certificate_number(
        proof.get("terminal_graph_distance_m")
    )

    schema_valid = bool(
        source_count is not None
        and source_cell_count is not None
        and source_start is not None
        and source_end is not None
        and ingress_index is not None
        and terminal_index is not None
        and terminal_hint_index is not None
        and terminal_hint_count is not None
        and summary_terminal_count is not None
        and summary_unknown_count is not None
        and source_points is not None
        and source_cells is not None
        and published_points is not None
        and published_cells is not None
        and proof_route_cells is not None
        and selected_intervals is not None
        and component_intervals is not None
        and source_start_point is not None
        and source_terminal_point is not None
        and requested_terminal is not None
        and selected_hint is not None
        and certified_start is not None
        and ingress_attachment is not None
        and persisted_seed_key is not None
        and persisted_terminal_key is not None
        and footprint_cell_size is not None
        and footprint_cell_size > 0.0
        and fixed_vertical_size is not None
        and fixed_vertical_size > 0.0
        and terminal_snap_limit is not None
        and terminal_snap_limit > 0.0
        and proof_gate_count is not None
        and persisted_path_node_count is not None
        and persisted_path_edge_count is not None
        and proof_graph_distance is not None
        and proof_graph_distance > 0.0
    )
    if not schema_valid:
        return {
            "passed": False,
            "reason": "prepared_route_binding_schema_invalid",
            "route_id": route_id,
        }

    assert source_count is not None
    assert source_cell_count is not None
    assert source_start is not None
    assert source_end is not None
    assert ingress_index is not None
    assert terminal_index is not None
    assert terminal_hint_index is not None
    assert terminal_hint_count is not None
    assert summary_terminal_count is not None
    assert summary_unknown_count is not None
    assert source_points is not None
    assert source_cells is not None
    assert published_points is not None
    assert published_cells is not None
    assert proof_route_cells is not None
    assert selected_intervals is not None
    assert component_intervals is not None
    assert source_start_point is not None
    assert source_terminal_point is not None
    assert requested_terminal is not None
    assert selected_hint is not None
    assert certified_start is not None
    assert ingress_attachment is not None
    assert persisted_seed_key is not None
    assert persisted_terminal_key is not None
    assert footprint_cell_size is not None
    assert fixed_vertical_size is not None
    assert terminal_snap_limit is not None
    assert proof_gate_count is not None
    assert persisted_path_node_count is not None
    assert persisted_path_edge_count is not None
    assert proof_graph_distance is not None

    full_source_span = bool(
        source_count >= 2
        and source_cell_count == source_count
        and len(source_points) == source_count
        and len(source_cells) == source_count
        and source_start == 0
        and source_end == source_count - 1
        and ingress_index == 0
        and terminal_index == source_count - 1
        and corridor.get("complete_ingress_route") is True
    )
    source_evidence_valid = bool(
        full_source_span
        and source_cells == proof_route_cells
        and len(selected_intervals) == source_count
        and proof_gate_count == source_count
        and _points_match(source_start_point, source_points[0])
        and _points_match(source_terminal_point, source_points[-1])
        and _points_match(requested_terminal, source_terminal_point)
        and len(set(source_cells)) == len(source_cells)
    )
    try:
        projected_source_cells = tuple(
            (
                math.floor(point[0] / footprint_cell_size),
                math.floor(point[2] / footprint_cell_size),
            )
            for point in source_points
        )
    except (OverflowError, ValueError):
        projected_source_cells = ()
    source_evidence_valid = bool(
        source_evidence_valid
        and projected_source_cells == source_cells
        and all(
            max(
                abs(second[0] - first[0]),
                abs(second[1] - first[1]),
            )
            == 1
            for first, second in zip(
                source_cells,
                source_cells[1:],
                strict=False,
            )
        )
    )

    selected_interval_membership = bool(
        source_evidence_valid
        and all(
            any(
                abs(selected[0] - candidate[0]) <= 1e-9
                and abs(selected[1] - candidate[1]) <= 1e-9
                for candidate in component_intervals.get(cell, ())
            )
            for cell, selected in zip(
                source_cells,
                selected_intervals,
                strict=True,
            )
        )
    )

    raw_fallback_indices = proof.get("surface_gap_transition_fallback_indices")
    fallback_indices: tuple[int, ...] | None = None
    if (
        isinstance(raw_fallback_indices, Sequence)
        and not isinstance(raw_fallback_indices, (str, bytes))
        and all(type(value) is int for value in raw_fallback_indices)
    ):
        fallback_indices = tuple(raw_fallback_indices)
    surface_evidence_valid = bool(
        selected_interval_membership
        and fallback_indices is not None
        and len(set(fallback_indices)) == len(fallback_indices)
        and all(0 < index < source_count - 1 for index in fallback_indices)
        and proof.get("surface_gap_waypoints_required") is True
        and proof.get("surface_gap_gate_source")
        == "source_layer_pairwise_surface_intervals_v3"
    )

    terminal_keys = tuple(
        sorted(
            key
            for key, node in graph.nodes.items()
            if bool(node.terminal) and not bool(node.unknown_boundary)
        )
    )
    actual_terminal_key = terminal_keys[0] if len(terminal_keys) == 1 else None
    terminal_center = (
        None
        if actual_terminal_key is None
        else _strict_certificate_point(graph.nodes[actual_terminal_key].center)
    )
    actual_counts_valid = bool(
        graph.terminal_count == 1
        and graph.unknown_boundary_count == 0
        and len(terminal_keys) == 1
    )
    summary_counts_valid = bool(
        summary_terminal_count == 1 and summary_unknown_count == 0
    )
    terminal_hint_valid = bool(
        0 <= terminal_hint_index < terminal_hint_count
    )
    hint_is_bound = bool(
        terminal_center is not None
        and _points_match(terminal_center, selected_hint)
    )
    endpoint_is_bound = bool(
        terminal_center is not None
        and _points_match(terminal_center, published_points[-1])
    )

    seed_node = graph.nodes.get(persisted_seed_key)
    seed_center = (
        None
        if seed_node is None
        else _strict_certificate_point(seed_node.center)
    )
    seed_neighbor_count = len(
        {
            edge.target
            for edge in graph.edges.get(persisted_seed_key, ())
            if edge.target in graph.nodes and edge.target != persisted_seed_key
        }
    )
    start_is_bound = bool(
        seed_node is not None
        and seed_center is not None
        and not bool(seed_node.terminal)
        and not bool(seed_node.unknown_boundary)
        and seed_neighbor_count == 1
        and _points_match(seed_center, ingress_attachment)
        and _points_match(seed_center, certified_start)
        and _points_match(seed_center, published_points[0])
    )

    graph_path_keys: tuple[VoxelGraphKey, ...] | None = None
    graph_path_details: Mapping[str, object] = {}
    if (
        persisted_seed_key in graph.nodes
        and persisted_terminal_key in graph.nodes
    ):
        graph_path_keys, graph_path_details = (
            shortest_navigation_voxel_3d_graph_path(
                graph,
                start_key=persisted_seed_key,
                terminal_key=persisted_terminal_key,
            )
        )
    graph_path_points = (
        ()
        if graph_path_keys is None
        else tuple(
            _strict_certificate_point(graph.nodes[key].center)
            for key in graph_path_keys
        )
    )
    path_points_valid = bool(
        graph_path_keys is not None
        and len(graph_path_keys) >= 2
        and all(point is not None for point in graph_path_points)
    )
    resolved_graph_points: tuple[Point, ...] = (
        tuple(graph_path_points)  # type: ignore[arg-type]
        if path_points_valid
        else ()
    )
    try:
        graph_path_cells = tuple(
            (
                math.floor(point[0] / footprint_cell_size),
                math.floor(point[2] / footprint_cell_size),
            )
            for point in resolved_graph_points
        )
    except (OverflowError, ValueError):
        graph_path_cells = ()
    graph_distance_m = sum(
        math.dist(first, second)
        for first, second in zip(
            resolved_graph_points,
            resolved_graph_points[1:],
            strict=False,
        )
    )
    published_path_is_bound = bool(
        path_points_valid
        and len(published_points) == len(resolved_graph_points)
        and all(
            _points_match(published, actual)
            for published, actual in zip(
                published_points,
                resolved_graph_points,
                strict=True,
            )
        )
        and published_cells == graph_path_cells
        and persisted_path_node_count == len(resolved_graph_points)
        and persisted_path_edge_count == len(resolved_graph_points) - 1
        and abs(proof_graph_distance - graph_distance_m) <= 1e-6
    )

    try:
        graph_vertical_size = float(graph.grid_size_m[1])
    except (TypeError, ValueError, IndexError):
        graph_vertical_size = 0.0
    vertical_margin_m = (
        fixed_vertical_size * 0.5
        + graph_vertical_size * 0.5
        + 1e-9
    )
    gate_path_indices: list[int] = []
    gate_replay_valid = bool(
        surface_evidence_valid
        and published_path_is_bound
        and math.isfinite(graph_vertical_size)
        and graph_vertical_size > 0.0
    )
    previous_path_index = -1
    fallback_set = set(fallback_indices or ())
    if gate_replay_valid:
        for gate_index, (gate_cell, selected_interval) in enumerate(
            zip(source_cells, selected_intervals, strict=True)
        ):
            if gate_index == 0:
                candidate_indices = (0,)
            elif gate_index == source_count - 1:
                candidate_indices = (len(resolved_graph_points) - 1,)
            else:
                candidate_indices = range(
                    previous_path_index + 1,
                    len(resolved_graph_points) - 1,
                )
            allowed_intervals = [selected_interval]
            if gate_index in fallback_set:
                allowed_intervals = []
                for neighbor_index in (gate_index - 1, gate_index + 1):
                    neighbor_interval = selected_intervals[neighbor_index]
                    allowed_intervals.append(
                        (
                            min(selected_interval[0], neighbor_interval[0]),
                            max(selected_interval[1], neighbor_interval[1]),
                        )
                    )
            matched_path_index = next(
                (
                    path_index
                    for path_index in candidate_indices
                    if graph_path_cells[path_index] == gate_cell
                    and any(
                        low_y - vertical_margin_m
                        <= resolved_graph_points[path_index][1]
                        <= high_y + vertical_margin_m
                        for low_y, high_y in allowed_intervals
                    )
                ),
                None,
            )
            if (
                matched_path_index is None
                or matched_path_index <= previous_path_index
            ):
                gate_replay_valid = False
                break
            gate_path_indices.append(matched_path_index)
            previous_path_index = matched_path_index

    terminal_interval_bound = bool(
        terminal_center is not None
        and source_cells
        and selected_intervals
        and any(
            abs(selected_intervals[-1][0] - candidate[0]) <= 1e-9
            and abs(selected_intervals[-1][1] - candidate[1]) <= 1e-9
            for candidate in component_intervals.get(source_cells[-1], ())
        )
        and math.floor(terminal_center[0] / footprint_cell_size)
        == source_cells[-1][0]
        and math.floor(terminal_center[2] / footprint_cell_size)
        == source_cells[-1][1]
        and selected_intervals[-1][0] - vertical_margin_m
        <= terminal_center[1]
        <= selected_intervals[-1][1] + vertical_margin_m
    )
    terminal_cap_bound = bool(
        terminal_center is not None
        and math.hypot(
            terminal_center[0] - requested_terminal[0],
            terminal_center[2] - requested_terminal[2],
        )
        <= terminal_snap_limit + 1e-9
    )

    failures = (
        (not full_source_span, "prepared_route_source_span_incomplete"),
        (len(terminal_keys) != 1, "prepared_graph_terminal_not_unique"),
        (
            persisted_terminal_key != actual_terminal_key,
            "prepared_terminal_key_mismatch",
        ),
        (not actual_counts_valid, "prepared_graph_terminal_counts_invalid"),
        (not terminal_hint_valid, "prepared_terminal_hint_invalid"),
        (not hint_is_bound, "prepared_terminal_hint_mismatch"),
        (not start_is_bound, "prepared_graph_start_binding_invalid"),
        (not endpoint_is_bound, "prepared_route_endpoint_mismatch"),
        (not source_evidence_valid, "prepared_source_route_evidence_invalid"),
        (
            not terminal_interval_bound,
            "prepared_terminal_outside_surface_gap",
        ),
        (not surface_evidence_valid, "prepared_surface_gap_evidence_invalid"),
        (
            not published_path_is_bound,
            "prepared_published_graph_path_mismatch",
        ),
        (not gate_replay_valid, "prepared_route_gate_replay_failed"),
        (not terminal_cap_bound, "prepared_terminal_snap_limit_exceeded"),
        (not summary_counts_valid, "prepared_terminal_summary_invalid"),
    )
    reason = next((value for failed, value in failures if failed), "")
    return {
        "passed": not bool(reason),
        "reason": reason,
        "route_id": route_id,
        "full_source_span": full_source_span,
        "source_route_point_count": source_count,
        "selected_source_hint_start_index": source_start,
        "selected_source_hint_end_index": source_end,
        "source_evidence_valid": source_evidence_valid,
        "surface_evidence_valid": surface_evidence_valid,
        "gate_replay_valid": gate_replay_valid,
        "gate_path_indices": gate_path_indices,
        "vertical_gate_margin_m": float(vertical_margin_m),
        "actual_terminal_keys": [list(key) for key in terminal_keys],
        "persisted_terminal_graph_key": list(persisted_terminal_key),
        "persisted_seed_graph_key": list(persisted_seed_key),
        "selected_terminal_hint_index": terminal_hint_index,
        "terminal_hint_count": terminal_hint_count,
        "selected_terminal_hint_point": selected_hint,
        "route_endpoint": published_points[-1],
        "terminal_center": terminal_center,
        "terminal_footprint_cell": list(source_cells[-1]),
        "terminal_surface_gap_intervals": [
            [float(low_y), float(high_y)]
            for low_y, high_y in component_intervals.get(source_cells[-1], ())
        ],
        "terminal_interval_bound": terminal_interval_bound,
        "terminal_snap_limit_bound": terminal_cap_bound,
        "summary_terminal_count": summary_terminal_count,
        "summary_unknown_boundary_count": summary_unknown_count,
        "actual_terminal_count": int(graph.terminal_count),
        "actual_unknown_boundary_count": int(graph.unknown_boundary_count),
        "graph_path_details": dict(graph_path_details),
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
    full_component_scope = atlas.coverage_scope == "entire_cave_component"
    terminal_route_scope = atlas.coverage_scope == "certified_terminal_route"
    terminal_route_complete = bool(
        terminal_route_scope
        and graph.component_count == 1
        and graph.terminal_count >= 1
        and unknown_count == 0
    )
    scope_complete = bool(full_component_scope or terminal_route_complete)
    passed = (
        bool(graph.nodes)
        and (
            not strict
            or (
                unknown_count == 0
                and scope_complete
                and (
                    terminal_route_scope
                    or not missing_component_cells
                )
            )
        )
    )
    return {
        "passed": passed,
        "reason": (
            "unknown_graph_boundary"
            if strict and unknown_count
            else "known_terminal_route_missing"
            if strict and terminal_route_scope and graph.terminal_count <= 0
            else "terminal_route_component_invalid"
            if strict and terminal_route_scope and graph.component_count != 1
            else "component_footprints_missing_metrics"
            if strict and full_component_scope and missing_component_cells
            else "navigation_coverage_scope_incomplete"
            if strict and not scope_complete
            else ""
        ),
        "strict": strict,
        "coverage_scope": atlas.coverage_scope,
        "coverage_scope_complete": scope_complete,
        "coverage_contract": (
            "one_exact_known_terminal_route_v1"
            if terminal_route_scope
            else "entire_cave_component_v1"
            if full_component_scope
            else "unknown"
        ),
        "terminal_route_complete": terminal_route_complete,
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


def _validate_published_route(
    plan: AutoDivePlan,
    *,
    atlas: NavigationVoxelAtlas,
    graph: NavigationVoxel3DGraph,
    mesh_guard: CachedChunkMeshCollisionGuard | None,
    settings: AutoDiveSettings,
    start_graph_key: VoxelGraphKey | None,
) -> tuple[bool, dict[str, object]]:
    """Validate the exact route geometry handed to the controller.

    Legacy plans have one prepared-graph path.  A fixed full-cave plan may
    instead contain a lightweight ledger with prepared graph segments and
    2 m refined segments.  The latter still receives exact cache/mesh checks;
    its temporary local graph was already checked during preflight and is not
    retained merely for certification.
    """
    if mesh_guard is None:
        return False, {"reason": "mesh_collision_guard_unavailable"}
    try:
        validator = GraphRouteSafetyValidator(
            atlas,
            graph,
            mesh_guard=mesh_guard,
            policy=GraphRouteSafetyPolicy(
                minimum_clearance_m=float(settings.minimum_graph_clearance_m),
            ),
        )
        segments = tuple(getattr(plan, "route_segments", ()) or ())
        route_keys = tuple(getattr(plan, "navigation_graph_keys", ()) or ())
        if not segments:
            if not route_keys:
                return False, {"reason": "preflight_graph_route_keys_missing"}
            failure = validator.route_clearance_failure(
                plan.route_points,
                route_keys,
                start_graph_key=start_graph_key,
                allow_mesh_only_start_connector=bool(
                    getattr(plan, "mesh_only_start_connector", False)
                ),
            )
            details = _route_failure_details(failure)
            details.update(
                {
                    "route_point_count": len(plan.route_points),
                    "graph_key_count": len(route_keys),
                    "route_length_m": float(plan.route_length_m),
                    "route_segment_count": 0,
                }
            )
            return failure is None, details

        ledger_points: list[Point] = []
        refined_segment_count = 0
        for segment_index, segment in enumerate(segments):
            raw_points = tuple(getattr(segment, "route_points", ()) or ())
            points = tuple(_finite_point(point) for point in raw_points)
            if len(points) < 2 or any(point is None for point in points):
                return False, {
                    "reason": "route_ledger_segment_points_invalid",
                    "failed_segment_index": segment_index,
                }
            resolved_points = tuple(point for point in points if point is not None)
            if (
                ledger_points
                and _distance(ledger_points[-1], resolved_points[0]) > 1e-5
            ):
                return False, {
                    "reason": "route_ledger_segment_seam_mismatch",
                    "failed_segment_index": segment_index,
                }
            source = str(getattr(segment, "source", ""))
            if source == "prepared_global_graph":
                segment_keys = tuple(
                    getattr(segment, "graph_keys", ()) or ()
                )
                if not segment_keys:
                    return False, {
                        "reason": "route_ledger_global_graph_keys_missing",
                        "failed_segment_index": segment_index,
                    }
                failure = validator.route_clearance_failure(
                    resolved_points,
                    segment_keys,
                    start_graph_key=segment_keys[0],
                )
            elif source == "refined_fine_2m_graph":
                refined_segment_count += 1
                raw_details = getattr(segment, "details", {})
                details_map = (
                    raw_details if isinstance(raw_details, Mapping) else {}
                )
                if not bool(details_map.get("mesh_safe", False)):
                    return False, {
                        "reason": "route_ledger_refined_proof_missing",
                        "failed_segment_index": segment_index,
                    }
                failure = None
                for local_index, (first, second) in enumerate(
                    zip(resolved_points, resolved_points[1:], strict=False)
                ):
                    failure = validator.segment_clearance_failure(
                        first,
                        second,
                        segment_index=local_index,
                        kind="certificate_refined_route_segment",
                        uncovered_reason="certificate_refined_route_uncovered",
                    )
                    if failure is not None:
                        break
            else:
                return False, {
                    "reason": "route_ledger_segment_source_unknown",
                    "failed_segment_index": segment_index,
                    "source": source,
                }
            if failure is not None:
                details = _route_failure_details(failure)
                details.update({"failed_segment_index": segment_index})
                return False, details
            if not ledger_points:
                ledger_points.extend(resolved_points)
            else:
                ledger_points.extend(resolved_points[1:])

        published = tuple(_finite_point(point) for point in plan.route_points)
        if any(point is None for point in published):
            return False, {"reason": "published_route_points_invalid"}
        published_points = tuple(point for point in published if point is not None)
        if (
            len(published_points) != len(ledger_points)
            or any(
                _distance(first, second) > 1e-5
                for first, second in zip(
                    published_points,
                    ledger_points,
                    strict=False,
                )
            )
        ):
            return False, {"reason": "route_ledger_geometry_mismatch"}
        return True, {
            "passed": True,
            "reason": "",
            "route_point_count": len(plan.route_points),
            "graph_key_count": len(route_keys),
            "route_length_m": float(plan.route_length_m),
            "route_segment_count": len(segments),
            "prepared_global_segment_count": len(segments) - refined_segment_count,
            "refined_segment_count": refined_segment_count,
            "route_geometry_source": "fixed_route_ledger",
        }
    except Exception as exc:  # pragma: no cover - artifact boundary
        return False, {
            "reason": "route_safety_exception",
            "exception": _exception_text(exc),
        }


def _simulate_fixed_route_execution(
    *,
    plan: AutoDivePlan,
    atlas: NavigationVoxelAtlas,
    graph: NavigationVoxel3DGraph,
    mesh_guard: CachedChunkMeshCollisionGuard | None,
    settings: AutoDiveSettings,
    checkpoint_spacing_m: float,
    max_checkpoints: int,
) -> tuple[bool, dict[str, object]]:
    """Simulate execution of an immutable terminal route without replanning."""
    if (
        not plan.preflight_validated
        or not plan.terminal_reached
        or plan.replan_at_end
    ):
        return False, {
            "reason": "fixed_route_execution_contract_invalid",
            "preflight_validated": bool(plan.preflight_validated),
            "terminal_reached": bool(plan.terminal_reached),
            "replan_at_end": bool(plan.replan_at_end),
        }
    if mesh_guard is None:
        return False, {"reason": "mesh_collision_guard_unavailable"}
    spacing_m = min(1.0, float(checkpoint_spacing_m))
    try:
        checkpoints = _route_checkpoints(
            plan.route_points,
            spacing_m=spacing_m,
            max_checkpoints=max_checkpoints,
        )
    except ValueError as exc:
        return False, {
            "reason": str(exc),
            "checkpoint_count": 0,
            "execution_mode": "fixed_route_no_replan",
        }
    if len(checkpoints) < 2:
        return False, {
            "reason": "route_has_no_execution_horizon",
            "execution_mode": "fixed_route_no_replan",
        }

    validator = GraphRouteSafetyValidator(
        atlas,
        graph,
        mesh_guard=mesh_guard,
        policy=GraphRouteSafetyPolicy(
            minimum_clearance_m=float(settings.minimum_graph_clearance_m),
        ),
    )
    failures: list[dict[str, object]] = []
    initial_load_errors = _chunk_load_errors(atlas)
    prefetched_chunk_ids = atlas.prefetch_for_points(checkpoints)
    simulated_count = 0
    mesh_only_connector_endpoint: Point | None = None
    if bool(getattr(plan, "mesh_only_start_connector", False)):
        route_keys = tuple(
            getattr(plan, "navigation_graph_keys", ()) or ()
        )
        start_node = graph.nodes.get(route_keys[0]) if route_keys else None
        if (
            start_node is None
            or len(plan.route_points) < 2
            or _distance(plan.route_points[1], start_node.center) > 1e-6
        ):
            return False, {
                "reason": "mesh_only_start_connector_contract_invalid",
                "execution_mode": "fixed_route_no_replan",
            }
        mesh_only_connector_endpoint = tuple(
            float(value) for value in start_node.center
        )
    for index, (first, second) in enumerate(
        zip(checkpoints, checkpoints[1:], strict=False)
    ):
        if mesh_only_connector_endpoint is not None:
            hit = mesh_guard.segment_collision(first, second)
            failure = (
                None
                if hit is None
                else GraphRouteSafetyFailure(
                    kind="fixed_route_execution_connector",
                    reason="mesh_intersection",
                    segment_index=index,
                    point=tuple(float(value) for value in hit.point),
                    first=first,
                    second=second,
                )
            )
            if _distance(second, mesh_only_connector_endpoint) <= 1e-6:
                mesh_only_connector_endpoint = None
        else:
            failure = validator.segment_clearance_failure(
                first,
                second,
                segment_index=index,
                kind="fixed_route_execution_segment",
                uncovered_reason="fixed_route_execution_uncovered",
            )
        if failure is not None:
            failures.append(
                {
                    "checkpoint_index": index,
                    "position": _point_payload(first),
                    **_route_failure_details(failure),
                }
            )
            break
        simulated_count += 1
        if _chunk_load_errors(atlas) > initial_load_errors:
            failures.append(
                {
                    "checkpoint_index": index,
                    "position": _point_payload(first),
                    "reason": "navigation_chunk_load_error_during_fixed_execution",
                }
            )
            break
    if not failures and mesh_only_connector_endpoint is not None:
        failures.append(
            {
                "checkpoint_index": simulated_count,
                "position": _point_payload(checkpoints[-1]),
                "reason": "mesh_only_start_connector_endpoint_not_reached",
            }
        )
    passed = not failures and simulated_count == len(checkpoints) - 1
    return passed, {
        "reason": "" if passed else (str(failures[0]["reason"]) if failures else ""),
        "execution_mode": "fixed_route_no_replan",
        "checkpoint_spacing_m": float(spacing_m),
        "checkpoint_count": len(checkpoints),
        "simulated_checkpoint_count": simulated_count,
        "replan_request_count": 0,
        "prefetched_chunk_count": len(prefetched_chunk_ids),
        "failure_count": len(failures),
        "failure_examples": failures[:8],
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
    route_by_id: dict[str, Mapping[str, Any]] = {}
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        candidate = route.get("id")
        if not isinstance(candidate, str) or not candidate:
            continue
        route_by_id[candidate] = route
    recommended = navigation.get("recommended_route_id")
    if not isinstance(recommended, str) or not recommended:
        return None
    if requested is not None and requested != recommended:
        return None
    selected = route_by_id.get(recommended)
    if (
        selected is None
        or navigation.get("route_selection_method")
        != NAVIGATION_ROUTE_SELECTION_LONGEST_SAFE_NON_CIRCULAR
        or selected.get("selection_method")
        != NAVIGATION_ROUTE_SELECTION_LONGEST_SAFE_NON_CIRCULAR
        or selected.get("closed_loop") is not False
    ):
        return None
    return recommended


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
    phase: str,
    profile: str,
    cache_dir: str,
    route_id: str | None,
    point: Point | None,
    checks: Sequence[NavigationCertificateCheck],
) -> NavigationCertificateResult:
    resolved = tuple(checks)
    return NavigationCertificateResult(
        passed=bool(resolved) and all(check.passed for check in resolved),
        phase=phase,
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
            "Certify a CaveViewer cache in independent artifact, graph, route, "
            "or complete phases. The full-cave profile only passes when an "
            "offline fixed route and simulated execution reach a known terminal."
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
        required=False,
        metavar=("X", "Y", "Z"),
        help="Initial camera position; required for route/all phases.",
    )
    parser.add_argument("--yaw", type=float, help="Initial camera yaw in radians.")
    parser.add_argument("--pitch", type=float, help="Initial camera pitch in radians.")
    parser.add_argument("--source", help="Optional source model for stale-cache checking.")
    parser.add_argument("--route-id", help="Explicit cached navigation route ID.")
    parser.add_argument(
        "--phase",
        choices=CERTIFICATE_PHASES,
        default=PHASE_ALL,
        help=(
            "Certification phase: artifacts skips graph deserialization; "
            "graph validates the loaded graph; route adds preflight and "
            "execution simulation; all runs every phase."
        ),
    )
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
    if args.phase in {PHASE_ROUTE, PHASE_ALL} and args.start is None:
        _parser().error("--start is required for route/all phases")
    cache_dir = Path(args.cache_dir)
    manifest = load_manifest(cache_dir)
    if manifest is None:
        payload = {
            "certificate": "FAIL",
            "passed": False,
            "phase": args.phase,
            "profile": args.profile,
            "cache_dir": str(cache_dir),
            "route_id": args.route_id,
            "start_position": (
                None
                if args.start is None
                else [float(value) for value in args.start]
            ),
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
        phase=args.phase,
        checkpoint_spacing_m=args.checkpoint_spacing_m,
        max_checkpoints=args.max_checkpoints,
    )
    _print_result(result.diagnostic_payload(), json_output=bool(args.json))
    return 0 if result.passed else 1


def _print_result(payload: Mapping[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(
        "NAVIGATION CERTIFICATE "
        f"[{payload.get('phase', 'all')}]: "
        f"{payload.get('certificate', 'FAIL')}"
    )
    for check in payload.get("checks", ()):
        if not isinstance(check, Mapping):
            continue
        status = "PASS" if check.get("passed") else "FAIL"
        reason = str(check.get("reason") or "")
        suffix = f" — {reason}" if reason else ""
        print(f"{str(check.get('name', 'check'))}: {status}{suffix}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
