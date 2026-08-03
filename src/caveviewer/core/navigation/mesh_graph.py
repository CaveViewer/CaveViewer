"""Conservative mesh-derived free-space roadmap construction.

The existing navigation atlas starts with sampled surface voxels and then
derives a graph from that field.  This module takes the opposite direction:
it queries the cached triangle mesh directly, finds sparse points in vertical
interior intervals, and connects only points whose straight segment has
already passed an exact mesh collision check.

This is intentionally an *offline* cache builder.  It does not retain mesh
triangles or construct a whole-map dense occupancy volume at runtime.  A
route remains subject to the normal graph/voxel/mesh safety validator before
Guided Dive can execute it.

The vertical-parity test is deliberately conservative.  A column with an odd
or otherwise ambiguous number of mesh crossings is omitted instead of being
treated as cave air.  That makes an open/non-watertight scan lose coverage,
not silently invent an outside route.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import heapq
import math

import numpy as np

from caveviewer.core.navigation.centerline import FootprintCell, Point
from caveviewer.core.navigation.cubic_graph import (
    CubicVoxelKey,
    SparseCubicVoxelGraph,
    build_route_ordered_cubic_path,
)
from caveviewer.core.navigation.fixed_voxels import (
    segment_voxel_probe_fractions,
)
from caveviewer.core.navigation.voxel_graph_3d import (
    NAVIGATION_MESH_3D_GRAPH_METHOD,
    NavigationVoxel3DEdge,
    NavigationVoxel3DGraph,
    NavigationVoxel3DNode,
    VoxelGraphKey,
)


MESH_NAVIGATION_GRAPH_METHOD = NAVIGATION_MESH_3D_GRAPH_METHOD
"""Persisted graph method for a sparse mesh-derived free-space roadmap."""

MESH_NAVIGATION_GRAPH_VERSION = 2

TriangleProvider = Callable[[Point, Point], Iterable[np.ndarray]]
MeshEdgeSafetyCheck = Callable[[Point, Point], bool]
MeshPointProbe = Callable[[Point], tuple[bool, float] | None]


@dataclass(frozen=True)
class MeshNavigationGraphConfig:
    """Bounded cache-time configuration for the mesh roadmap.

    ``horizontal_sample_spacing_m`` controls sparse vertical probes, not a
    dense filled voxel field.  The builder emits one or more points only for
    mesh-proven interior intervals encountered by those probes.
    """

    horizontal_sample_spacing_m: float = 4.0
    vertical_sample_spacing_m: float = 2.0
    minimum_clearance_m: float = 0.25
    max_nodes: int = 96_000
    max_edges_per_node: int = 16
    # This is a candidate-query cap, not a graph-degree target.  It keeps the
    # exact mesh segment checks linear in the sparse roadmap size instead of
    # considering every nearby pair in a wide passage.
    max_edge_candidates_per_node: int = 32
    # A scan mesh can block the nearest candidate with a tiny sliver while a
    # second nearby point in the same direction is a valid continuation.
    # Keep a bounded fallback rather than inventing a disconnected passage.
    max_edge_candidates_per_direction: int = 2
    max_edge_distance_m: float = 16.0
    max_vertical_edge_distance_m: float = 8.0
    max_interval_points_per_column: int = 12
    ray_merge_epsilon_m: float = 1e-4

    def validated(self) -> "MeshNavigationGraphConfig":
        horizontal = _positive_finite(
            self.horizontal_sample_spacing_m,
            "mesh graph horizontal sample spacing",
        )
        vertical = _positive_finite(
            self.vertical_sample_spacing_m,
            "mesh graph vertical sample spacing",
        )
        clearance = float(self.minimum_clearance_m)
        if not math.isfinite(clearance) or clearance < 0.0:
            raise ValueError("mesh graph minimum clearance must be non-negative")
        edge_distance = _positive_finite(
            self.max_edge_distance_m,
            "mesh graph maximum edge distance",
        )
        vertical_edge_distance = _positive_finite(
            self.max_vertical_edge_distance_m,
            "mesh graph maximum vertical edge distance",
        )
        merge_epsilon = _positive_finite(
            self.ray_merge_epsilon_m,
            "mesh graph ray merge epsilon",
        )
        return MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=horizontal,
            vertical_sample_spacing_m=vertical,
            minimum_clearance_m=clearance,
            max_nodes=max(2, int(self.max_nodes)),
            max_edges_per_node=max(2, int(self.max_edges_per_node)),
            max_edge_candidates_per_node=max(
                2,
                int(self.max_edge_candidates_per_node),
            ),
            max_edge_candidates_per_direction=max(
                1,
                min(8, int(self.max_edge_candidates_per_direction)),
            ),
            max_edge_distance_m=edge_distance,
            max_vertical_edge_distance_m=vertical_edge_distance,
            max_interval_points_per_column=max(
                1,
                int(self.max_interval_points_per_column),
            ),
            ray_merge_epsilon_m=merge_epsilon,
        )


@dataclass(frozen=True)
class MeshNavigationGraphBuildResult:
    """One bounded mesh-roadmap build result and its diagnostic evidence."""

    graph: NavigationVoxel3DGraph | None
    details: Mapping[str, object]


@dataclass(frozen=True)
class _MeshCandidate:
    key: VoxelGraphKey
    point: Point
    footprint_cell: FootprintCell
    clearance_m: float


@dataclass(frozen=True)
class MeshNavigationGraphAnchor:
    """A known-free candidate point for a mesh-connected roadmap.

    The point is supplied by preserved voxel evidence, while this module
    independently checks mesh clearance and builds its connectivity solely
    from exact mesh-safe segments. This is the fail-closed alternative for
    open scan meshes whose triangle winding cannot prove a global inside.
    """

    point: Point
    footprint_cell: FootprintCell
    clearance_m: float


def build_exact_cubic_spine_navigation_path_graph(
    cubic_graph: SparseCubicVoxelGraph,
    route_guide_points: Sequence[Point],
    *,
    start_keys: Sequence[CubicVoxelKey],
    terminal_keys: Sequence[CubicVoxelKey],
    waypoint_key_groups: Sequence[Sequence[CubicVoxelKey]] = (),
    footprint_cell_size_m: float,
    point_probe: MeshPointProbe,
    edge_is_clear: MeshEdgeSafetyCheck,
    horizontal_gate_radius_m: float,
    require_waypoint_key_groups: bool = False,
    config: MeshNavigationGraphConfig | None = None,
) -> MeshNavigationGraphBuildResult:
    """Extract, exact-check, and smooth one route-ordered cubic spine.

    The selected packed component has already proven six-connected fixed-cell
    reachability. This builder decomposes that proof through ordered,
    surface-gap-derived waypoint candidates instead of flooding the whole cave
    volume. Cubic adjacency remains proposal evidence only: every persisted
    segment, including a smoothing shortcut, passes crossed-boundary voxel
    probes and the exact cached-mesh guard before publication.
    """
    resolved = (config or MeshNavigationGraphConfig()).validated()
    cell_size = _positive_finite(
        footprint_cell_size_m,
        "exact cubic spine footprint cell size",
    )
    entries = tuple(
        dict.fromkeys(key for key in start_keys if cubic_graph.contains_key(key))
    )
    terminals = tuple(
        dict.fromkeys(
            key for key in terminal_keys if cubic_graph.contains_key(key)
        )
    )
    base_details: dict[str, object] = {
        "method": MESH_NAVIGATION_GRAPH_METHOD,
        "version": MESH_NAVIGATION_GRAPH_VERSION,
        "sampling_envelope": "selected_route_cubic_spine",
        "inside_evidence": "cached_voxel_free_space_point_probe",
        "edge_evidence": "exact_cached_mesh_and_voxel_sampled_edges",
        "search_method": "route_ordered_six_connected_cubic_spine",
        "search_heuristic": "surface_gap_waypoints_then_exact_terminal",
        "route_guide_point_count": len(route_guide_points),
        "entry_seed_candidate_count": len(entries),
        "terminal_hint_count": len(terminals),
        "lattice_spacing_m": [
            float(value) for value in cubic_graph.cell_size_m
        ],
        "minimum_clearance_m": float(resolved.minimum_clearance_m),
        "horizontal_gate_radius_m": float(horizontal_gate_radius_m),
        "surface_gap_waypoints_required": bool(
            require_waypoint_key_groups
        ),
        "component_complete": False,
    }
    if not entries or not terminals or len(route_guide_points) < 2:
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                **base_details,
                "reason": "exact_cubic_spine_inputs_missing",
                "known_terminal_reached": False,
                "node_limit_reached": False,
            },
        )

    blocked_edges: set[tuple[CubicVoxelKey, CubicVoxelKey]] = set()
    checked_edges: dict[tuple[CubicVoxelKey, CubicVoxelKey], bool] = {}
    point_cache: dict[CubicVoxelKey, tuple[bool, float] | None] = {}
    search_expansion_limit = int(resolved.max_nodes)
    remaining_search_expansions = search_expansion_limit
    exact_edge_check_limit = max(2, int(resolved.max_nodes) * 2)
    exact_edge_check_count = 0
    voxel_probe_count = 0
    voxel_rejection_count = 0
    mesh_rejection_count = 0
    repair_pass_limit = 8
    repair_pass_count = 0
    route_attempt_count = 0
    route_attempts: list[dict[str, object]] = []
    last_route_details: Mapping[str, object] = {}
    exact_check_limit_reached = False

    def ordered_edge(
        first: CubicVoxelKey,
        second: CubicVoxelKey,
    ) -> tuple[CubicVoxelKey, CubicVoxelKey]:
        return (first, second) if first < second else (second, first)

    def probe_key(
        key: CubicVoxelKey,
    ) -> tuple[bool, float] | None:
        if key in point_cache:
            return point_cache[key]
        point = cubic_graph.voxel_center(key)
        try:
            probe = point_probe(point)
        except Exception:
            probe = None
        if (
            probe is None
            or not bool(probe[0])
            or not math.isfinite(float(probe[1]))
            or float(probe[1]) + 1e-9
            < float(resolved.minimum_clearance_m)
        ):
            point_cache[key] = None
        else:
            point_cache[key] = (True, max(0.0, float(probe[1])))
        return point_cache[key]

    def exact_edge_safe(
        first_key: CubicVoxelKey,
        second_key: CubicVoxelKey,
    ) -> bool:
        nonlocal exact_edge_check_count
        nonlocal voxel_probe_count
        nonlocal voxel_rejection_count
        nonlocal mesh_rejection_count
        nonlocal exact_check_limit_reached
        edge = ordered_edge(first_key, second_key)
        cached = checked_edges.get(edge)
        if cached is not None:
            return bool(cached)
        if exact_edge_check_count >= exact_edge_check_limit:
            exact_check_limit_reached = True
            return False
        exact_edge_check_count += 1
        first = cubic_graph.voxel_center(first_key)
        second = cubic_graph.voxel_center(second_key)
        clear = probe_key(first_key) is not None and probe_key(second_key) is not None
        if clear:
            fractions = segment_voxel_probe_fractions(
                first,
                second,
                lattice_spacing_m=cubic_graph.cell_size_m,
                maximum_sample_spacing_m=max(
                    0.125,
                    min(cubic_graph.cell_size_m) * 0.5,
                ),
            )
            for fraction in fractions:
                sample = tuple(
                    first[axis]
                    + (second[axis] - first[axis]) * fraction
                    for axis in range(3)
                )
                voxel_probe_count += 1
                try:
                    probe = point_probe(sample)  # type: ignore[arg-type]
                except Exception:
                    probe = None
                if (
                    probe is None
                    or not bool(probe[0])
                    or not math.isfinite(float(probe[1]))
                    or float(probe[1]) + 1e-9
                    < float(resolved.minimum_clearance_m)
                ):
                    voxel_rejection_count += 1
                    clear = False
                    break
        if clear:
            try:
                clear = bool(edge_is_clear(first, second))
            except Exception:
                clear = False
            if not clear:
                mesh_rejection_count += 1
        checked_edges[edge] = bool(clear)
        return bool(clear)

    def simplify_exact_leg(
        path_keys: Sequence[CubicVoxelKey],
    ) -> tuple[CubicVoxelKey, ...]:
        """String-pull locally without adding an unchecked executable edge."""
        if len(path_keys) <= 2:
            return tuple(path_keys)
        cumulative = [0.0]
        for first_key, second_key in zip(
            path_keys,
            path_keys[1:],
            strict=False,
        ):
            cumulative.append(
                cumulative[-1]
                + math.dist(
                    cubic_graph.voxel_center(first_key),
                    cubic_graph.voxel_center(second_key),
                )
            )
        simplified = [path_keys[0]]
        source_index = 0
        candidate_limit = max(1, int(resolved.max_edge_candidates_per_node))
        while source_index < len(path_keys) - 1:
            source_point = cubic_graph.voxel_center(path_keys[source_index])
            furthest_index = source_index + 1
            cursor = source_index + 1
            candidates: list[int] = []
            while cursor < len(path_keys):
                route_distance_m = cumulative[cursor] - cumulative[source_index]
                target_point = cubic_graph.voxel_center(path_keys[cursor])
                if route_distance_m > float(resolved.max_edge_distance_m) + 1e-9:
                    break
                if (
                    abs(target_point[1] - source_point[1])
                    <= float(resolved.max_vertical_edge_distance_m) + 1e-9
                ):
                    candidates.append(cursor)
                cursor += 1
            for candidate_index in reversed(candidates[-candidate_limit:]):
                if exact_edge_safe(
                    path_keys[source_index],
                    path_keys[candidate_index],
                ):
                    furthest_index = candidate_index
                    break
            simplified.append(path_keys[furthest_index])
            source_index = furthest_index
        return tuple(simplified)

    def ordered_gate_witness_indices(
        path_keys: Sequence[CubicVoxelKey],
    ) -> tuple[int, ...] | None:
        """Return one monotonically ordered path witness for every gate."""
        previous_index = 0
        witnesses: list[int] = []
        for group in waypoint_key_groups:
            witness_index = next(
                (
                    index
                    for index in range(previous_index + 1, len(path_keys))
                    if path_keys[index] in group
                ),
                None,
            )
            if witness_index is None:
                return None
            witnesses.append(witness_index)
            previous_index = witness_index
        return tuple(witnesses)

    def simplify_exact_path(
        path_keys: Sequence[CubicVoxelKey],
        *,
        gate_witness_indices: Sequence[int],
    ) -> tuple[CubicVoxelKey, ...]:
        """String-pull each leg while retaining every ordered gate witness."""
        if len(path_keys) <= 2:
            return tuple(path_keys)
        protected_indices = (
            0,
            *(int(index) for index in gate_witness_indices),
            len(path_keys) - 1,
        )
        if any(
            second <= first
            for first, second in zip(
                protected_indices,
                protected_indices[1:],
                strict=False,
            )
        ):
            return ()
        simplified: list[CubicVoxelKey] = []
        for first_index, second_index in zip(
            protected_indices,
            protected_indices[1:],
            strict=False,
        ):
            leg = simplify_exact_leg(
                path_keys[first_index : second_index + 1]
            )
            if len(leg) < 2:
                return ()
            if simplified:
                simplified.extend(leg[1:])
            else:
                simplified.extend(leg)
        return tuple(simplified)

    selected_path_keys: tuple[CubicVoxelKey, ...] | None = None
    selected_route_details: Mapping[str, object] = {}
    selected_entry_key: CubicVoxelKey | None = None
    for entry_key in entries:
        while (
            remaining_search_expansions > 0
            and repair_pass_count <= repair_pass_limit
        ):
            route_attempt_count += 1
            route_build = build_route_ordered_cubic_path(
                cubic_graph,
                route_guide_points,
                start_key=entry_key,
                terminal_keys=terminals,
                horizontal_gate_radius_m=horizontal_gate_radius_m,
                max_expansions=remaining_search_expansions,
                waypoint_key_groups=waypoint_key_groups,
                require_waypoint_key_groups=require_waypoint_key_groups,
                blocked_edges=blocked_edges,
                edge_is_clear=exact_edge_safe,
                max_blocked_edges=min(4_096, exact_edge_check_limit),
            )
            last_route_details = dict(route_build.details)
            used_expansions = max(
                0,
                int(route_build.details.get("expanded_voxel_count", 0)),
            )
            remaining_search_expansions = max(
                0,
                remaining_search_expansions - used_expansions,
            )
            route_attempts.append(
                {
                    "entry_key": [int(value) for value in entry_key],
                    "built": route_build.path is not None,
                    "reason": str(route_build.details.get("reason", "")),
                    "expanded_voxel_count": int(used_expansions),
                    "remaining_expansion_budget": int(
                        remaining_search_expansions
                    ),
                }
            )
            if route_build.path is None:
                break
            path_keys = route_build.path.keys
            rejected = {
                ordered_edge(first_key, second_key)
                for first_key, second_key in zip(
                    path_keys,
                    path_keys[1:],
                    strict=False,
                )
                if not exact_edge_safe(first_key, second_key)
            }
            if exact_check_limit_reached:
                break
            new_rejections = rejected - blocked_edges
            if rejected:
                blocked_edges.update(rejected)
                repair_pass_count += 1
                if not new_rejections:
                    break
                continue
            gate_witness_indices = ordered_gate_witness_indices(path_keys)
            if gate_witness_indices is None:
                last_route_details = {
                    **dict(route_build.details),
                    "reason": "exact_cubic_spine_gate_witness_missing",
                    "known_terminal_reached": False,
                }
                break
            simplified = simplify_exact_path(
                path_keys,
                gate_witness_indices=gate_witness_indices,
            )
            if exact_check_limit_reached:
                break
            if (
                len(simplified) >= 2
                and simplified[-1] in terminals
                and len(simplified) == len(set(simplified))
            ):
                selected_path_keys = simplified
                selected_route_details = {
                    **dict(route_build.details),
                    "persisted_intermediate_gate_witness_count": len(
                        gate_witness_indices
                    ),
                }
                selected_entry_key = entry_key
            break
        if selected_path_keys is not None or exact_check_limit_reached:
            break

    retained_cubic_details = dict(
        selected_route_details or last_route_details
    )
    local_exact_reroute_count = max(
        0,
        int(retained_cubic_details.get("exact_reroute_count", 0)),
    )
    local_blocked_edge_count = max(
        0,
        int(retained_cubic_details.get("blocked_edge_count", 0)),
    )
    common_details = {
        **base_details,
        "search_expansion_limit": int(search_expansion_limit),
        "expanded_node_count": int(
            search_expansion_limit - remaining_search_expansions
        ),
        "remaining_search_expansions": int(remaining_search_expansions),
        "exact_edge_check_limit": int(exact_edge_check_limit),
        "mesh_edge_check_count": int(exact_edge_check_count),
        "mesh_rejected_edge_count": int(mesh_rejection_count),
        "edge_voxel_probe_count": int(voxel_probe_count),
        "edge_voxel_rejection_count": int(voxel_rejection_count),
        "blocked_cubic_edge_count": max(
            len(blocked_edges),
            local_blocked_edge_count,
        ),
        "repair_pass_count": int(
            repair_pass_count + local_exact_reroute_count
        ),
        "local_exact_reroute_count": int(local_exact_reroute_count),
        "repair_pass_limit": int(repair_pass_limit),
        "route_attempt_count": int(route_attempt_count),
        "route_attempts": list(route_attempts),
        "cubic_spine": retained_cubic_details,
    }
    if selected_path_keys is None or selected_entry_key is None:
        route_limit_reached = bool(
            last_route_details.get("node_limit_reached", False)
            or remaining_search_expansions <= 0
        )
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                **common_details,
                "reason": (
                    "exact_cubic_spine_edge_check_limit_reached"
                    if exact_check_limit_reached
                    else (
                        "exact_cubic_spine_search_limit_reached"
                        if route_limit_reached
                        else "exact_cubic_spine_terminal_unreachable"
                    )
                ),
                "known_terminal_reached": False,
                "node_limit_reached": bool(route_limit_reached),
            },
        )

    candidates: dict[VoxelGraphKey, _MeshCandidate] = {}
    for key in selected_path_keys:
        probe = probe_key(key)
        if probe is None:
            return MeshNavigationGraphBuildResult(
                graph=None,
                details={
                    **common_details,
                    "reason": "exact_cubic_spine_persisted_node_rejected",
                    "known_terminal_reached": False,
                    "node_limit_reached": False,
                },
            )
        point = cubic_graph.voxel_center(key)
        candidates[key] = _MeshCandidate(
            key=key,
            point=point,
            footprint_cell=(
                int(math.floor(point[0] / cell_size)),
                int(math.floor(point[2] / cell_size)),
            ),
            clearance_m=float(probe[1]),
        )
    terminal_key = selected_path_keys[-1]
    graph = _mesh_path_graph(
        candidates,
        selected_path_keys,
        grid_size_m=cubic_graph.cell_size_m,
        terminal_key=terminal_key,
    )
    graph_distance_m = sum(
        math.dist(candidates[first].point, candidates[second].point)
        for first, second in zip(
            selected_path_keys,
            selected_path_keys[1:],
            strict=False,
        )
    )
    terminal_index = terminals.index(terminal_key)
    return MeshNavigationGraphBuildResult(
        graph=graph,
        details={
            **common_details,
            "reason": "exact_cubic_spine_terminal_path_built",
            "known_terminal_reached": True,
            "node_limit_reached": False,
            "seed_graph_key": [int(value) for value in selected_entry_key],
            "seed_point": [
                float(value)
                for value in cubic_graph.voxel_center(selected_entry_key)
            ],
            "seed_hint_index": 0,
            "terminal_graph_key": [int(value) for value in terminal_key],
            "terminal_hint_index": int(terminal_index),
            "selected_terminal_hint_index": int(terminal_index),
            "terminal_hint_point": [
                float(value)
                for value in cubic_graph.voxel_center(terminal_key)
            ],
            "selected_terminal_hint_point": [
                float(value)
                for value in cubic_graph.voxel_center(terminal_key)
            ],
            "terminal_attachment_distance_m": 0.0,
            "terminal_graph_distance_m": float(graph_distance_m),
            "persisted_path_node_count": len(selected_path_keys),
            "persisted_path_edge_count": max(0, len(selected_path_keys) - 1),
            "unsimplified_path_node_count": int(
                selected_route_details.get("path", {}).get("voxel_count", 0)
                if isinstance(selected_route_details.get("path"), Mapping)
                else 0
            ),
            "graph": graph.diagnostic_payload(),
            "terminal_count": int(graph.terminal_count),
            "unknown_boundary_count": int(graph.unknown_boundary_count),
        },
    )


def build_validated_mesh_path_graph(
    path_points: Sequence[Point],
    *,
    lattice_spacing_m: Sequence[float],
    footprint_cell_size_m: float,
    point_probe: MeshPointProbe,
    edge_is_clear: MeshEdgeSafetyCheck,
    minimum_clearance_m: float,
) -> MeshNavigationGraphBuildResult:
    """Publish one already ordered path after independent exact validation."""
    resolved_diagnostics = MeshNavigationGraphConfig().validated()
    try:
        spacing = tuple(float(value) for value in lattice_spacing_m)
    except (TypeError, ValueError):
        spacing = ()
    if (
        len(spacing) != 3
        or not all(math.isfinite(value) and value > 0.0 for value in spacing)
    ):
        return _empty_result(
            "validated_mesh_path_spacing_invalid",
            resolved=resolved_diagnostics,
        )
    cell_size = _positive_finite(
        footprint_cell_size_m,
        "validated mesh path footprint cell size",
    )
    clearance_floor = max(0.0, float(minimum_clearance_m))
    resolved_diagnostics = MeshNavigationGraphConfig(
        horizontal_sample_spacing_m=spacing[0],
        vertical_sample_spacing_m=spacing[1],
        minimum_clearance_m=clearance_floor,
    ).validated()
    candidates: dict[VoxelGraphKey, _MeshCandidate] = {}
    ordered_keys: list[VoxelGraphKey] = []
    point_probe_count = 0
    edge_probe_count = 0
    for raw_point in path_points:
        try:
            point: Point = tuple(  # type: ignore[assignment]
                float(value) for value in raw_point
            )
        except (TypeError, ValueError):
            return _empty_result(
                "validated_mesh_path_point_invalid",
                resolved=resolved_diagnostics,
            )
        if len(point) != 3 or not all(math.isfinite(value) for value in point):
            return _empty_result(
                "validated_mesh_path_point_invalid",
                resolved=resolved_diagnostics,
            )
        key: VoxelGraphKey = tuple(  # type: ignore[assignment]
            int(math.floor(point[axis] / spacing[axis]))
            for axis in range(3)
        )
        if ordered_keys and key == ordered_keys[-1]:
            continue
        if key in candidates:
            return _empty_result(
                "validated_mesh_path_revisit_detected",
                resolved=resolved_diagnostics,
            )
        point_probe_count += 1
        try:
            probe = point_probe(point)
        except Exception:
            probe = None
        if (
            probe is None
            or not bool(probe[0])
            or not math.isfinite(float(probe[1]))
            or float(probe[1]) + 1e-9 < clearance_floor
        ):
            return _empty_result(
                "validated_mesh_path_point_rejected",
                resolved=resolved_diagnostics,
            )
        candidates[key] = _MeshCandidate(
            key=key,
            point=point,
            footprint_cell=(
                int(math.floor(point[0] / cell_size)),
                int(math.floor(point[2] / cell_size)),
            ),
            clearance_m=max(0.0, float(probe[1])),
        )
        ordered_keys.append(key)
    if len(ordered_keys) < 2:
        return _empty_result(
            "validated_mesh_path_has_no_travel",
            resolved=resolved_diagnostics,
        )
    for first_key, second_key in zip(
        ordered_keys,
        ordered_keys[1:],
        strict=False,
    ):
        first = candidates[first_key].point
        second = candidates[second_key].point
        for fraction in segment_voxel_probe_fractions(
            first,
            second,
            lattice_spacing_m=spacing,
            maximum_sample_spacing_m=max(0.125, min(spacing) * 0.5),
        ):
            sample = tuple(
                first[axis] + (second[axis] - first[axis]) * fraction
                for axis in range(3)
            )
            edge_probe_count += 1
            try:
                probe = point_probe(sample)  # type: ignore[arg-type]
            except Exception:
                probe = None
            if (
                probe is None
                or not bool(probe[0])
                or not math.isfinite(float(probe[1]))
                or float(probe[1]) + 1e-9 < clearance_floor
            ):
                return _empty_result(
                    "validated_mesh_path_edge_voxel_rejected",
                    resolved=resolved_diagnostics,
                )
        try:
            clear = bool(edge_is_clear(first, second))
        except Exception:
            clear = False
        if not clear:
            return _empty_result(
                "validated_mesh_path_edge_mesh_rejected",
                resolved=resolved_diagnostics,
            )
    terminal_key = ordered_keys[-1]
    graph = _mesh_path_graph(
        candidates,
        ordered_keys,
        grid_size_m=spacing,
        terminal_key=terminal_key,
    )
    distance_m = sum(
        math.dist(candidates[first].point, candidates[second].point)
        for first, second in zip(
            ordered_keys,
            ordered_keys[1:],
            strict=False,
        )
    )
    return MeshNavigationGraphBuildResult(
        graph=graph,
        details={
            "method": MESH_NAVIGATION_GRAPH_METHOD,
            "version": MESH_NAVIGATION_GRAPH_VERSION,
            "reason": "validated_ordered_mesh_path_built",
            "known_terminal_reached": True,
            "node_limit_reached": False,
            "seed_graph_key": [int(value) for value in ordered_keys[0]],
            "terminal_graph_key": [int(value) for value in terminal_key],
            "terminal_graph_distance_m": float(distance_m),
            "persisted_path_node_count": len(ordered_keys),
            "persisted_path_edge_count": len(ordered_keys) - 1,
            "point_probe_count": int(point_probe_count),
            "edge_voxel_probe_count": int(edge_probe_count),
            "graph": graph.diagnostic_payload(),
            "terminal_count": int(graph.terminal_count),
            "unknown_boundary_count": int(graph.unknown_boundary_count),
        },
    )


def build_mesh_navigation_graph(
    manifest: Mapping[str, object],
    route: Mapping[str, object],
    *,
    triangle_provider: TriangleProvider,
    edge_is_clear: MeshEdgeSafetyCheck,
    sampling_cells: Sequence[FootprintCell] | None = None,
    terminal_hint_points: Sequence[Point] = (),
    config: MeshNavigationGraphConfig | None = None,
) -> MeshNavigationGraphBuildResult:
    """Build a sparse, direct-mesh free-space graph for one route component.

    Mesh topology is the source of this graph's nodes and edges:

    * a node is the center of a paired vertical mesh-intersection interval;
    * a node is retained only when it has the configured direct mesh
      clearance; and
    * an edge is retained only after ``edge_is_clear`` accepts the segment.

    The supplied route/component metadata is used strictly as a bounded
    sampling envelope.  It neither supplies route points nor contributes
    connectivity to the resulting graph.
    """
    resolved = (config or MeshNavigationGraphConfig()).validated()
    bounds = _manifest_bounds(manifest)
    cells = (
        tuple(sorted({(int(cell[0]), int(cell[1])) for cell in sampling_cells}))
        if sampling_cells is not None
        else _route_component_cells(route)
    )
    cell_size = _route_cell_size(route, manifest)
    if bounds is None:
        return _empty_result("mesh_bounds_missing", resolved=resolved)
    if not cells or cell_size is None:
        return _empty_result("mesh_sampling_envelope_missing", resolved=resolved)

    bounds_min, bounds_max = bounds
    candidates: dict[VoxelGraphKey, _MeshCandidate] = {}
    probe_count = 0
    paired_column_count = 0
    unpaired_column_count = 0
    empty_column_count = 0
    clearance_rejection_count = 0
    interval_point_count = 0
    duplicate_key_count = 0
    node_limit_reached = False
    local_triangle_query_count = 0
    local_triangle_count = 0
    local_triangle_padding_m = max(
        float(resolved.minimum_clearance_m),
        float(resolved.ray_merge_epsilon_m),
    )

    for cell in cells:
        # A cell-level triangle query is intentionally shared by every
        # vertical probe and clearance test in that cell.  Querying the
        # chunk cache separately for each 4 m probe turns a one-route cache
        # build into thousands of repeated chunk decodes on large caves.
        cell_bounds_min = (
            float(cell[0]) * cell_size - local_triangle_padding_m,
            float(bounds_min[1]),
            float(cell[1]) * cell_size - local_triangle_padding_m,
        )
        cell_bounds_max = (
            float(cell[0] + 1) * cell_size + local_triangle_padding_m,
            float(bounds_max[1]),
            float(cell[1] + 1) * cell_size + local_triangle_padding_m,
        )
        local_triangles = _triangles_for_bounds(
            triangle_provider,
            cell_bounds_min,
            cell_bounds_max,
        )
        local_triangle_min = (
            np.empty((0, 3), dtype=np.float64)
            if local_triangles.size == 0
            else local_triangles.min(axis=1)
        )
        local_triangle_max = (
            np.empty((0, 3), dtype=np.float64)
            if local_triangles.size == 0
            else local_triangles.max(axis=1)
        )
        local_triangle_query_count += 1
        local_triangle_count += int(len(local_triangles))
        for x, z in _cell_probe_positions(
            cell,
            cell_size=cell_size,
            spacing_m=resolved.horizontal_sample_spacing_m,
        ):
            probe_count += 1
            intersections = _vertical_mesh_intersections(
                x,
                z,
                bounds_min=bounds_min,
                bounds_max=bounds_max,
                triangles=local_triangles,
                triangle_min=local_triangle_min,
                triangle_max=local_triangle_max,
                merge_epsilon_m=resolved.ray_merge_epsilon_m,
            )
            if not intersections:
                empty_column_count += 1
                continue
            if len(intersections) % 2:
                unpaired_column_count += 1
                continue
            paired_column_count += 1
            for lower_y, upper_y in zip(intersections[::2], intersections[1::2]):
                for y in _interval_sample_positions(
                    lower_y,
                    upper_y,
                    clearance_m=resolved.minimum_clearance_m,
                    spacing_m=resolved.vertical_sample_spacing_m,
                    max_points=resolved.max_interval_points_per_column,
                ):
                    interval_point_count += 1
                    point = (float(x), float(y), float(z))
                    if not _point_has_clearance(
                        point,
                        clearance_m=resolved.minimum_clearance_m,
                        triangles=local_triangles,
                        triangle_min=local_triangle_min,
                        triangle_max=local_triangle_max,
                    ):
                        clearance_rejection_count += 1
                        continue
                    key = _candidate_key(
                        point,
                        horizontal_spacing_m=resolved.horizontal_sample_spacing_m,
                        vertical_spacing_m=resolved.vertical_sample_spacing_m,
                    )
                    candidate = _MeshCandidate(
                        key=key,
                        point=point,
                        footprint_cell=cell,
                        clearance_m=resolved.minimum_clearance_m,
                    )
                    existing = candidates.get(key)
                    if existing is not None:
                        duplicate_key_count += 1
                        # Points sharing a graph key arise only from adjacent
                        # sampling envelopes.  Keep deterministic geometry.
                        if candidate.point >= existing.point:
                            continue
                    candidates[key] = candidate
                    if len(candidates) >= resolved.max_nodes:
                        node_limit_reached = True
                        break
                if node_limit_reached:
                    break
            if node_limit_reached:
                break
        if node_limit_reached:
            break

    base_details: dict[str, object] = {
        "method": MESH_NAVIGATION_GRAPH_METHOD,
        "version": MESH_NAVIGATION_GRAPH_VERSION,
        "sampling_envelope": "provided_route_corridor_cells_only",
        "inside_evidence": "paired_vertical_mesh_intervals",
        "edge_evidence": "exact_cached_mesh_segment_guard",
        "horizontal_sample_spacing_m": float(
            resolved.horizontal_sample_spacing_m
        ),
        "vertical_sample_spacing_m": float(resolved.vertical_sample_spacing_m),
        "minimum_clearance_m": float(resolved.minimum_clearance_m),
        "max_edge_candidates_per_node": int(
            resolved.max_edge_candidates_per_node
        ),
        "max_edge_candidates_per_direction": int(
            resolved.max_edge_candidates_per_direction
        ),
        "probe_count": int(probe_count),
        "paired_column_count": int(paired_column_count),
        "unpaired_column_count": int(unpaired_column_count),
        "empty_column_count": int(empty_column_count),
        "interval_point_count": int(interval_point_count),
        "clearance_rejection_count": int(clearance_rejection_count),
        "duplicate_key_count": int(duplicate_key_count),
        "candidate_node_count": int(len(candidates)),
        "node_limit_reached": bool(node_limit_reached),
        "component_cell_count": int(len(cells)),
        "local_triangle_query_count": int(local_triangle_query_count),
        "local_triangle_count": int(local_triangle_count),
    }
    return _build_mesh_graph_result(
        candidates,
        edge_is_clear=edge_is_clear,
        terminal_hint_points=terminal_hint_points,
        resolved=resolved,
        base_details=base_details,
        node_limit_reached=node_limit_reached,
        missing_candidate_reason="mesh_free_space_candidates_missing",
        built_reason="mesh_free_space_graph_built",
    )


def build_mesh_anchored_navigation_graph(
    anchors: Sequence[MeshNavigationGraphAnchor],
    *,
    footprint_cell_size_m: float,
    triangle_provider: TriangleProvider,
    edge_is_clear: MeshEdgeSafetyCheck,
    terminal_hint_points: Sequence[Point] = (),
    config: MeshNavigationGraphConfig | None = None,
) -> MeshNavigationGraphBuildResult:
    """Build a mesh-connected roadmap rooted in known free-space anchors.

    Scan meshes are commonly open, self-overlapping, or locally unoriented,
    so a parity test alone cannot prove their global interior.  This builder
    keeps the existing voxel atlas as *inside evidence* for sparse anchors,
    then makes all new topology decisions from direct mesh clearance and
    exact mesh-safe edges.  The voxel graph contributes neither an edge nor a
    route to the resulting roadmap.
    """
    resolved = (config or MeshNavigationGraphConfig()).validated()
    cell_size = _positive_finite(
        footprint_cell_size_m,
        "mesh anchor footprint cell size",
    )
    valid_anchors = tuple(
        anchor
        for anchor in anchors
        if _anchor_is_finite(anchor)
    )
    if not valid_anchors:
        return _empty_result("mesh_anchor_candidates_missing", resolved=resolved)

    grouped: dict[FootprintCell, list[MeshNavigationGraphAnchor]] = {}
    for anchor in valid_anchors:
        grouped.setdefault(anchor.footprint_cell, []).append(anchor)

    candidates: dict[VoxelGraphKey, _MeshCandidate] = {}
    mesh_clearance_rejection_count = 0
    duplicate_key_count = 0
    local_triangle_query_count = 0
    local_triangle_count = 0
    node_limit_reached = False
    padding = max(
        float(resolved.minimum_clearance_m),
        float(resolved.ray_merge_epsilon_m),
    )
    for cell in sorted(grouped):
        cell_anchors = grouped[cell]
        lower_y = min(float(anchor.point[1]) for anchor in cell_anchors) - padding
        upper_y = max(float(anchor.point[1]) for anchor in cell_anchors) + padding
        triangles = _triangles_for_bounds(
            triangle_provider,
            (
                float(cell[0]) * cell_size - padding,
                lower_y,
                float(cell[1]) * cell_size - padding,
            ),
            (
                float(cell[0] + 1) * cell_size + padding,
                upper_y,
                float(cell[1] + 1) * cell_size + padding,
            ),
        )
        triangle_min = (
            np.empty((0, 3), dtype=np.float64)
            if triangles.size == 0
            else triangles.min(axis=1)
        )
        triangle_max = (
            np.empty((0, 3), dtype=np.float64)
            if triangles.size == 0
            else triangles.max(axis=1)
        )
        local_triangle_query_count += 1
        local_triangle_count += int(len(triangles))
        for anchor in sorted(
            cell_anchors,
            key=lambda value: (
                tuple(float(item) for item in value.point),
                -float(value.clearance_m),
            ),
        ):
            if not _point_has_clearance(
                anchor.point,
                clearance_m=resolved.minimum_clearance_m,
                triangles=triangles,
                triangle_min=triangle_min,
                triangle_max=triangle_max,
            ):
                mesh_clearance_rejection_count += 1
                continue
            key = _candidate_key(
                anchor.point,
                horizontal_spacing_m=resolved.horizontal_sample_spacing_m,
                vertical_spacing_m=resolved.vertical_sample_spacing_m,
            )
            candidate = _MeshCandidate(
                key=key,
                point=tuple(float(value) for value in anchor.point),
                footprint_cell=(
                    int(anchor.footprint_cell[0]),
                    int(anchor.footprint_cell[1]),
                ),
                clearance_m=max(
                    0.0,
                    min(
                        float(anchor.clearance_m),
                        float(resolved.minimum_clearance_m),
                    ),
                ),
            )
            existing = candidates.get(key)
            if existing is not None:
                duplicate_key_count += 1
                if _anchored_candidate_sort_key(candidate) >= _anchored_candidate_sort_key(
                    existing
                ):
                    continue
            candidates[key] = candidate
            if len(candidates) >= resolved.max_nodes:
                node_limit_reached = True
                break
        if node_limit_reached:
            break

    base_details: dict[str, object] = {
        "method": MESH_NAVIGATION_GRAPH_METHOD,
        "version": MESH_NAVIGATION_GRAPH_VERSION,
        "sampling_envelope": "selected_voxel_spine_corridor",
        "inside_evidence": "voxel_free_space_anchor_with_direct_mesh_clearance",
        "edge_evidence": "exact_cached_mesh_segment_guard",
        "horizontal_sample_spacing_m": float(
            resolved.horizontal_sample_spacing_m
        ),
        "vertical_sample_spacing_m": float(resolved.vertical_sample_spacing_m),
        "minimum_clearance_m": float(resolved.minimum_clearance_m),
        "max_edge_candidates_per_node": int(
            resolved.max_edge_candidates_per_node
        ),
        "max_edge_candidates_per_direction": int(
            resolved.max_edge_candidates_per_direction
        ),
        "anchor_count": len(valid_anchors),
        "anchor_footprint_cell_count": len(grouped),
        "mesh_clearance_rejection_count": int(mesh_clearance_rejection_count),
        "duplicate_key_count": int(duplicate_key_count),
        "candidate_node_count": int(len(candidates)),
        "node_limit_reached": bool(node_limit_reached),
        "local_triangle_query_count": int(local_triangle_query_count),
        "local_triangle_count": int(local_triangle_count),
    }
    return _build_mesh_graph_result(
        candidates,
        edge_is_clear=edge_is_clear,
        terminal_hint_points=terminal_hint_points,
        resolved=resolved,
        base_details=base_details,
        node_limit_reached=node_limit_reached,
        missing_candidate_reason="mesh_anchored_candidates_missing",
        built_reason="mesh_anchored_roadmap_built",
    )


def build_seeded_mesh_navigation_path_graph(
    seed_points: Sequence[Point],
    *,
    footprint_cell_size_m: float,
    component_cells: Iterable[FootprintCell],
    point_probe: MeshPointProbe,
    edge_is_clear: MeshEdgeSafetyCheck,
    terminal_hint_points: Sequence[Point] = (),
    config: MeshNavigationGraphConfig | None = None,
) -> MeshNavigationGraphBuildResult:
    """Build one exact-connected 2 m path from a known entrance component.

    The retained voxel atlas answers only whether a lattice point has cached
    free-space evidence. Starting at a supplied entrance seed, topology is
    discovered by a bounded 26-neighbor flood whose every accepted edge
    passes the exact cached-mesh segment guard. This avoids both whole-cave
    candidate construction and false coarse-voxel shortcuts through walls.

    Once the reachable component is complete, the builder selects the
    furthest route hint that has an exact-safe attachment and persists only
    the shortest physical path to it. The larger flood is cache-build proof;
    runtime loads a compact, branch-free graph.
    """
    resolved = (config or MeshNavigationGraphConfig()).validated()
    spacing = float(resolved.horizontal_sample_spacing_m)
    cell_size = _positive_finite(
        footprint_cell_size_m,
        "seeded mesh graph footprint cell size",
    )
    allowed_cells = {
        (int(cell[0]), int(cell[1]))
        for cell in component_cells
    }
    finite_seeds = tuple(
        tuple(float(value) for value in point)
        for point in seed_points
        if len(point) == 3 and all(math.isfinite(float(value)) for value in point)
    )
    if not finite_seeds or not allowed_cells:
        return _empty_result(
            "seeded_mesh_graph_seed_or_component_missing",
            resolved=resolved,
        )

    probe_cache: dict[VoxelGraphKey, _MeshCandidate | None] = {}
    probe_count = 0
    free_probe_count = 0
    clearance_rejection_count = 0

    def candidate_for_key(key: VoxelGraphKey) -> _MeshCandidate | None:
        nonlocal probe_count, free_probe_count, clearance_rejection_count
        if key in probe_cache:
            return probe_cache[key]
        point: Point = tuple(
            (float(key[axis]) + 0.5) * spacing
            for axis in range(3)
        )  # type: ignore[assignment]
        footprint_cell = (
            int(math.floor(point[0] / cell_size)),
            int(math.floor(point[2] / cell_size)),
        )
        if footprint_cell not in allowed_cells:
            probe_cache[key] = None
            return None
        probe_count += 1
        try:
            probe = point_probe(point)
        except Exception:
            probe = None
        if probe is None or not bool(probe[0]):
            probe_cache[key] = None
            return None
        clearance_m = max(0.0, float(probe[1]))
        if (
            not math.isfinite(clearance_m)
            or clearance_m + 1e-9 < float(resolved.minimum_clearance_m)
        ):
            clearance_rejection_count += 1
            probe_cache[key] = None
            return None
        free_probe_count += 1
        candidate = _MeshCandidate(
            key=key,
            point=point,
            footprint_cell=footprint_cell,
            clearance_m=clearance_m,
        )
        probe_cache[key] = candidate
        return candidate

    seed_key: VoxelGraphKey | None = None
    seed_point: Point | None = None
    seed_snap_distance_m = math.inf
    maximum_seed_distance_m = float(resolved.max_edge_distance_m)
    seed_step_limit = max(
        1,
        int(math.ceil(maximum_seed_distance_m / spacing)),
    )
    for raw_seed in finite_seeds:
        base_key = tuple(
            int(math.floor(raw_seed[axis] / spacing))
            for axis in range(3)
        )
        nearby = sorted(
            (
                math.dist(
                    raw_seed,
                    tuple(
                        (float(base_key[axis] + delta[axis]) + 0.5) * spacing
                        for axis in range(3)
                    ),
                ),
                (
                    base_key[0] + delta[0],
                    base_key[1] + delta[1],
                    base_key[2] + delta[2],
                ),
            )
            for delta in (
                (dx, dy, dz)
                for dx in range(-seed_step_limit, seed_step_limit + 1)
                for dy in range(-seed_step_limit, seed_step_limit + 1)
                for dz in range(-seed_step_limit, seed_step_limit + 1)
            )
        )
        for distance_m, key in nearby:
            if distance_m > maximum_seed_distance_m + 1e-9:
                break
            if distance_m >= seed_snap_distance_m - 1e-12:
                break
            candidate = candidate_for_key(key)
            if candidate is None:
                continue
            if distance_m > 1e-9 and not edge_is_clear(
                raw_seed,
                candidate.point,
            ):
                continue
            seed_key = key
            seed_point = raw_seed
            seed_snap_distance_m = float(distance_m)
            break
    if seed_key is None or seed_point is None:
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                "method": MESH_NAVIGATION_GRAPH_METHOD,
                "version": MESH_NAVIGATION_GRAPH_VERSION,
                "reason": "seeded_mesh_graph_entry_missing",
                "probe_count": int(probe_count),
                "free_probe_count": int(free_probe_count),
                "minimum_clearance_m": float(resolved.minimum_clearance_m),
            },
        )

    offsets = tuple(
        (dx, dy, dz)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
        if (dx, dy, dz) != (0, 0, 0)
    )
    reached = {seed_key}
    queue = deque((seed_key,))
    candidates: dict[VoxelGraphKey, _MeshCandidate] = {
        seed_key: candidate_for_key(seed_key)  # type: ignore[dict-item]
    }
    adjacency: dict[VoxelGraphKey, set[VoxelGraphKey]] = {
        seed_key: set()
    }
    checked_edges: dict[
        tuple[VoxelGraphKey, VoxelGraphKey],
        bool,
    ] = {}
    mesh_rejected_edge_count = 0
    node_limit_reached = False
    while queue and not node_limit_reached:
        key = queue.popleft()
        candidate = candidates[key]
        for offset in offsets:
            target = (
                key[0] + offset[0],
                key[1] + offset[1],
                key[2] + offset[2],
            )
            other = candidate_for_key(target)
            if other is None:
                continue
            ordered = (
                (key, target) if key < target else (target, key)
            )
            clear = checked_edges.get(ordered)
            if clear is None:
                clear = bool(edge_is_clear(candidate.point, other.point))
                checked_edges[ordered] = clear
                if not clear:
                    mesh_rejected_edge_count += 1
            if not clear:
                continue
            if target not in reached:
                if len(reached) >= int(resolved.max_nodes):
                    node_limit_reached = True
                    break
                reached.add(target)
                candidates[target] = other
                adjacency[target] = set()
                queue.append(target)
            adjacency[key].add(target)
            adjacency[target].add(key)

    base_details: dict[str, object] = {
        "method": MESH_NAVIGATION_GRAPH_METHOD,
        "version": MESH_NAVIGATION_GRAPH_VERSION,
        "sampling_envelope": "seeded_exact_mesh_component",
        "inside_evidence": "cached_voxel_free_space_point_probe",
        "edge_evidence": "exact_cached_mesh_26_neighbor_guard",
        "lattice_spacing_m": spacing,
        "minimum_clearance_m": float(resolved.minimum_clearance_m),
        "seed_point": [float(value) for value in seed_point],
        "seed_graph_key": [int(value) for value in seed_key],
        "seed_snap_distance_m": float(seed_snap_distance_m),
        "probe_count": int(probe_count),
        "free_probe_count": int(free_probe_count),
        "clearance_rejection_count": int(clearance_rejection_count),
        "reachable_node_count": len(reached),
        "mesh_edge_check_count": len(checked_edges),
        "mesh_rejected_edge_count": int(mesh_rejected_edge_count),
        "mesh_safe_undirected_edge_count": int(
            sum(len(targets) for targets in adjacency.values()) // 2
        ),
        "node_limit_reached": bool(node_limit_reached),
        "component_complete": bool(not node_limit_reached and not queue),
    }
    if node_limit_reached:
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                **base_details,
                "reason": "seeded_mesh_graph_node_limit_reached",
            },
        )

    distances, previous = _mesh_graph_shortest_distances(
        candidates,
        adjacency,
        start_key=seed_key,
    )
    terminal_attachments: list[
        tuple[float, int, float, VoxelGraphKey, Point]
    ] = []
    for hint_index, raw_hint in enumerate(terminal_hint_points):
        if len(raw_hint) != 3:
            continue
        try:
            hint: Point = tuple(float(value) for value in raw_hint)  # type: ignore[assignment]
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in hint):
            continue
        nearby_keys = heapq.nsmallest(
            16,
            candidates,
            key=lambda key: (
                math.dist(hint, candidates[key].point),
                key,
            ),
        )
        for key in nearby_keys:
            attachment_distance_m = math.dist(
                hint,
                candidates[key].point,
            )
            if (
                attachment_distance_m
                > float(resolved.max_edge_distance_m) + 1e-9
            ):
                break
            if (
                attachment_distance_m > 1e-9
                and not edge_is_clear(hint, candidates[key].point)
            ):
                continue
            graph_distance_m = float(distances.get(key, 0.0))
            if graph_distance_m <= spacing + 1e-9:
                continue
            terminal_attachments.append(
                (
                    graph_distance_m,
                    int(hint_index),
                    float(attachment_distance_m),
                    key,
                    hint,
                )
            )
            break
    if not terminal_attachments:
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                **base_details,
                "reason": "seeded_mesh_graph_terminal_hint_unreachable",
                "terminal_hint_count": len(terminal_hint_points),
                "terminal_attachment_count": 0,
            },
        )
    (
        terminal_graph_distance_m,
        terminal_hint_index,
        terminal_attachment_distance_m,
        terminal_key,
        terminal_hint,
    ) = max(
        terminal_attachments,
        key=lambda item: (
            item[0],
            item[1],
            -item[2],
            item[3],
        ),
    )
    path_keys = _mesh_graph_path_from_previous(
        previous,
        start_key=seed_key,
        terminal_key=terminal_key,
    )
    if len(path_keys) < 2:
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                **base_details,
                "reason": "seeded_mesh_graph_terminal_path_missing",
            },
        )
    graph = _mesh_path_graph(
        candidates,
        path_keys,
        spacing_m=spacing,
        terminal_key=terminal_key,
    )
    return MeshNavigationGraphBuildResult(
        graph=graph,
        details={
            **base_details,
            "reason": "seeded_mesh_component_path_built",
            "terminal_hint_count": len(terminal_hint_points),
            "terminal_attachment_count": len(terminal_attachments),
            "terminal_hint_index": int(terminal_hint_index),
            "terminal_hint_point": [
                float(value) for value in terminal_hint
            ],
            "terminal_graph_key": [int(value) for value in terminal_key],
            "terminal_graph_distance_m": float(terminal_graph_distance_m),
            "terminal_attachment_distance_m": float(
                terminal_attachment_distance_m
            ),
            "persisted_path_node_count": len(path_keys),
            "persisted_path_edge_count": max(0, len(path_keys) - 1),
            "graph": graph.diagnostic_payload(),
            "terminal_count": int(graph.terminal_count),
            "unknown_boundary_count": int(graph.unknown_boundary_count),
        },
    )

def build_goal_directed_seeded_mesh_navigation_path_graph(
    seed_points: Sequence[Point],
    *,
    footprint_cell_size_m: float,
    component_cells: Iterable[FootprintCell],
    point_probe: MeshPointProbe,
    edge_is_clear: MeshEdgeSafetyCheck,
    terminal_point: Point,
    terminal_candidate_points: Sequence[Point] = (),
    route_guide_points: Sequence[Point] = (),
    require_exact_terminal_point: bool = False,
    progress_callback: Callable[[Mapping[str, object]], None] | None = None,
    config: MeshNavigationGraphConfig | None = None,
) -> MeshNavigationGraphBuildResult:
    """Build one bounded exact path to a known terminal.

    This is the primary adaptive builder for one selected cave route. It first
    runs on the configured lattice and can be retried at a genuinely finer
    spacing when coarse evidence loses a narrow passage. It searches only the caller's
    route corridor and uses weighted A* so wide rooms do not require a complete
    component flood. Voxel probes establish cached inside evidence; every
    accepted edge and the final terminal attachment still pass the exact
    cached-mesh guard.
    """
    resolved = (config or MeshNavigationGraphConfig()).validated()
    horizontal_spacing = float(resolved.horizontal_sample_spacing_m)
    vertical_spacing = float(resolved.vertical_sample_spacing_m)
    grid_size = (
        horizontal_spacing,
        vertical_spacing,
        horizontal_spacing,
    )
    minimum_spacing = min(grid_size)
    cell_diagonal_m = math.sqrt(
        sum(float(value) * float(value) for value in grid_size)
    )

    def lattice_key(point: Sequence[float]) -> VoxelGraphKey:
        return tuple(
            int(math.floor(float(point[axis]) / grid_size[axis]))
            for axis in range(3)
        )  # type: ignore[return-value]

    def lattice_center(key: Sequence[int]) -> Point:
        return tuple(
            (float(key[axis]) + 0.5) * grid_size[axis]
            for axis in range(3)
        )  # type: ignore[return-value]
    cell_size = _positive_finite(
        footprint_cell_size_m,
        "goal-directed mesh graph footprint cell size",
    )
    allowed_cells = {
        (int(cell[0]), int(cell[1])) for cell in component_cells
    }
    finite_seeds = tuple(
        tuple(float(value) for value in point)
        for point in seed_points
        if len(point) == 3
        and all(math.isfinite(float(value)) for value in point)
    )
    finite_targets: list[Point] = []
    for raw_target in (terminal_point, *terminal_candidate_points):
        try:
            candidate_target: Point = tuple(  # type: ignore[assignment]
                float(value) for value in raw_target
            )
        except (TypeError, ValueError):
            continue
        if (
            len(candidate_target) == 3
            and all(math.isfinite(value) for value in candidate_target)
            and candidate_target not in finite_targets
        ):
            finite_targets.append(candidate_target)
    target = (
        finite_targets[0]
        if finite_targets
        else (math.nan, math.nan, math.nan)
    )
    if (
        not finite_seeds
        or not allowed_cells
        or len(target) != 3
        or not all(math.isfinite(value) for value in target)
    ):
        return _empty_result(
            "goal_directed_mesh_graph_seed_component_or_terminal_missing",
            resolved=resolved,
        )

    probe_cache: dict[VoxelGraphKey, _MeshCandidate | None] = {}
    probe_count = 0
    free_probe_count = 0
    clearance_rejection_count = 0

    def candidate_for_key(key: VoxelGraphKey) -> _MeshCandidate | None:
        nonlocal probe_count, free_probe_count, clearance_rejection_count
        if key in probe_cache:
            return probe_cache[key]
        point = lattice_center(key)
        footprint_cell = (
            int(math.floor(point[0] / cell_size)),
            int(math.floor(point[2] / cell_size)),
        )
        if footprint_cell not in allowed_cells:
            probe_cache[key] = None
            return None
        probe_count += 1
        try:
            probe = point_probe(point)
        except Exception:
            probe = None
        if probe is None or not bool(probe[0]):
            probe_cache[key] = None
            return None
        clearance_m = max(0.0, float(probe[1]))
        if (
            not math.isfinite(clearance_m)
            or clearance_m + 1e-9 < float(resolved.minimum_clearance_m)
        ):
            clearance_rejection_count += 1
            probe_cache[key] = None
            return None
        free_probe_count += 1
        candidate = _MeshCandidate(
            key=key,
            point=point,
            footprint_cell=footprint_cell,
            clearance_m=clearance_m,
        )
        probe_cache[key] = candidate
        return candidate

    entry_candidates: dict[VoxelGraphKey, tuple[int, Point, float]] = {}
    # Every supplied seed is already an occupied-wins free-space sample.
    # Attach it only to the local execution lattice; the general graph-edge
    # budget must not become permission to relocate an entrance by 16 m.
    maximum_seed_distance_m = min(
        float(resolved.max_edge_distance_m),
        float(cell_diagonal_m),
    )
    seed_step_limits = _orthogonal_step_limits(
        maximum_seed_distance_m,
        grid_size,
    )
    # Retain one exact-safe lattice entry per early route hint. A single
    # weighted search can then reject an isolated scan layer and continue from
    # another bounded entry without map-specific configuration.
    for raw_seed_index, raw_seed in enumerate(finite_seeds):
        base_key = lattice_key(raw_seed)
        nearby = sorted(
            (
                math.dist(
                    raw_seed,
                    lattice_center(
                        tuple(
                            base_key[axis] + delta[axis]
                            for axis in range(3)
                        )
                    ),
                ),
                (
                    base_key[0] + delta[0],
                    base_key[1] + delta[1],
                    base_key[2] + delta[2],
                ),
            )
            for delta in (
                (dx, dy, dz)
                for dx in range(-seed_step_limits[0], seed_step_limits[0] + 1)
                for dy in range(-seed_step_limits[1], seed_step_limits[1] + 1)
                for dz in range(-seed_step_limits[2], seed_step_limits[2] + 1)
            )
            if math.dist(
                raw_seed,
                lattice_center(
                    tuple(
                        base_key[axis] + delta[axis]
                        for axis in range(3)
                    )
                ),
            ) <= maximum_seed_distance_m + 1e-9
        )
        for distance_m, key in nearby:
            if distance_m > maximum_seed_distance_m + 1e-9:
                break
            candidate = candidate_for_key(key)
            if candidate is None:
                continue
            if distance_m > 1e-9 and not edge_is_clear(
                raw_seed,
                candidate.point,
            ):
                continue
            entry = (int(raw_seed_index), raw_seed, float(distance_m))
            existing = entry_candidates.get(key)
            if existing is None or (
                entry[0],
                entry[2],
                entry[1],
            ) < (
                existing[0],
                existing[2],
                existing[1],
            ):
                entry_candidates[key] = entry
            break
    if not entry_candidates:
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                "method": MESH_NAVIGATION_GRAPH_METHOD,
                "version": MESH_NAVIGATION_GRAPH_VERSION,
                "reason": "goal_directed_mesh_graph_entry_missing",
                "probe_count": int(probe_count),
                "free_probe_count": int(free_probe_count),
                "minimum_clearance_m": float(resolved.minimum_clearance_m),
                "lattice_spacing_m": [float(value) for value in grid_size],
                "entry_seed_hint_count": len(finite_seeds),
                "entry_seed_candidate_count": 0,
            },
        )

    finite_guides = tuple(
        tuple(float(value) for value in point)
        for point in route_guide_points
        if len(point) == 3
        and all(math.isfinite(float(value)) for value in point)
    )
    if not finite_guides:
        first_entry = min(
            entry_candidates.values(),
            key=lambda entry: (entry[0], entry[2], entry[1]),
        )
        finite_guides = (first_entry[1], target)
    elif finite_guides[-1] != target:
        finite_guides = (*finite_guides, target)

    def horizontal_distance(first: Point, second: Point) -> float:
        return math.hypot(
            float(second[0]) - float(first[0]),
            float(second[2]) - float(first[2]),
        )

    guide_remaining_m = [0.0] * len(finite_guides)
    for index in range(len(finite_guides) - 2, -1, -1):
        guide_remaining_m[index] = (
            guide_remaining_m[index + 1]
            + horizontal_distance(
                finite_guides[index],
                finite_guides[index + 1],
            )
        )
    guide_indices_by_cell: dict[FootprintCell, list[int]] = {}
    for index, point in enumerate(finite_guides):
        cell = (
            int(math.floor(point[0] / cell_size)),
            int(math.floor(point[2] / cell_size)),
        )
        for delta_x in (-1, 0, 1):
            for delta_z in (-1, 0, 1):
                guide_indices_by_cell.setdefault(
                    (cell[0] + delta_x, cell[1] + delta_z),
                    [],
                ).append(index)

    def route_heuristic(candidate: _MeshCandidate) -> tuple[float, int]:
        local_indices = guide_indices_by_cell.get(candidate.footprint_cell)
        indices = local_indices if local_indices else range(len(finite_guides))
        guide_index = min(
            indices,
            key=lambda index: (
                horizontal_distance(candidate.point, finite_guides[index]),
                -int(index),
            ),
        )
        lateral_distance_m = horizontal_distance(
            candidate.point,
            finite_guides[guide_index],
        )
        return (
            lateral_distance_m + guide_remaining_m[guide_index],
            int(guide_index),
        )

    offsets = tuple(
        (dx, dy, dz)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
        if (dx, dy, dz) != (0, 0, 0)
    )
    candidates: dict[VoxelGraphKey, _MeshCandidate] = {}
    for entry_key in entry_candidates:
        entry_candidate = candidate_for_key(entry_key)
        if entry_candidate is not None:
            candidates[entry_key] = entry_candidate
    checked_edges: dict[
        tuple[VoxelGraphKey, VoxelGraphKey],
        bool,
    ] = {}
    heuristic_weight = 1.5
    distances: dict[VoxelGraphKey, float] = {}
    previous: dict[VoxelGraphKey, VoxelGraphKey] = {}
    source_entries: dict[VoxelGraphKey, VoxelGraphKey] = {}
    queue: list[tuple[float, int, float, VoxelGraphKey]] = []
    maximum_guide_index_seen = 0
    for entry_key, candidate in candidates.items():
        seed_hint_index, _seed_point, _seed_snap_distance_m = (
            entry_candidates[entry_key]
        )
        start_heuristic, start_guide_index = route_heuristic(candidate)
        route_prefix_m = max(
            0.0,
            guide_remaining_m[0] - guide_remaining_m[start_guide_index],
        )
        # Equalize each entry against the route prefix it skips. The tiny
        # deterministic rank favors an earlier valid entry when two complete
        # paths have otherwise equivalent weighted-A* cost.
        initial_cost_m = (
            heuristic_weight * route_prefix_m
            + float(seed_hint_index) * 1e-6
        )
        distances[entry_key] = initial_cost_m
        source_entries[entry_key] = entry_key
        maximum_guide_index_seen = max(
            maximum_guide_index_seen,
            int(start_guide_index),
        )
        heapq.heappush(
            queue,
            (
                initial_cost_m + heuristic_weight * start_heuristic,
                -int(start_guide_index),
                initial_cost_m,
                entry_key,
            ),
        )
    closed: set[VoxelGraphKey] = set()
    expanded_count = 0
    mesh_rejected_edge_count = 0
    guided_portal_candidate_count = 0
    edge_voxel_probe_count = 0
    edge_voxel_rejection_count = 0
    guided_portal_voxel_probe_count = 0
    guided_portal_voxel_rejection_count = 0
    guided_portal_accepted_count = 0
    non_improving_edge_skip_count = 0
    guided_portal_edge_candidate_limit = 4
    terminal_key: VoxelGraphKey | None = None
    selected_seed_key: VoxelGraphKey | None = None
    selected_terminal_hint_index: int | None = None
    selected_terminal_hint_point: Point | None = None
    terminal_attachment_distance_m = math.inf
    node_limit_reached = False
    # The target may lie between lattice points.  A short exact connector is
    # sufficient, but a 16 m attachment would incorrectly turn a route prefix
    # into a terminal. Keep the attachment local to the fine lattice.
    terminal_attachment_limit_m = (
        1e-9
        if require_exact_terminal_point
        else min(
            float(resolved.max_edge_distance_m),
            cell_diagonal_m,
        )
    )
    target_key_radii = tuple(
        max(
            1,
            int(math.ceil(terminal_attachment_limit_m / axis_size)) + 1,
        )
        for axis_size in grid_size
    )
    targets_by_search_key: dict[
        VoxelGraphKey,
        tuple[tuple[int, Point], ...],
    ] = {}
    mutable_targets_by_key: dict[
        VoxelGraphKey,
        list[tuple[int, Point]],
    ] = {}
    for target_index, candidate_target in enumerate(finite_targets):
        base_key = lattice_key(candidate_target)
        for delta_x in range(-target_key_radii[0], target_key_radii[0] + 1):
            for delta_y in range(-target_key_radii[1], target_key_radii[1] + 1):
                for delta_z in range(
                    -target_key_radii[2],
                    target_key_radii[2] + 1,
                ):
                    search_key = (
                        base_key[0] + delta_x,
                        base_key[1] + delta_y,
                        base_key[2] + delta_z,
                    )
                    mutable_targets_by_key.setdefault(
                        search_key,
                        [],
                    ).append((target_index, candidate_target))
    targets_by_search_key = {
        key: tuple(values)
        for key, values in mutable_targets_by_key.items()
    }
    while queue:
        _priority, _negative_guide_index, distance_m, key = heapq.heappop(
            queue
        )
        if key in closed:
            continue
        if distance_m > distances.get(key, math.inf) + 1e-12:
            continue
        closed.add(key)
        expanded_count += 1
        candidate = candidates[key]
        if progress_callback is not None and (
            expanded_count == 1 or expanded_count % 64 == 0
        ):
            try:
                progress_callback(
                    {
                        "expanded_node_count": int(expanded_count),
                        "discovered_node_count": len(candidates),
                        "queued_node_count": len(queue),
                        "mesh_edge_check_count": len(checked_edges),
                        "maximum_route_guide_index_seen": int(
                            maximum_guide_index_seen
                        ),
                        "maximum_route_guide_fraction_seen": float(
                            maximum_guide_index_seen
                            / max(1, len(finite_guides) - 1)
                        ),
                    }
                )
            except Exception:
                # Diagnostics must never change cache topology or authority.
                pass
        # A snapped endpoint may coincide with one of the bounded ingress
        # seeds on a very short route. Accepting it here would create a
        # zero-edge "path" and prematurely stop the search before another
        # real terminal can be reached. A certified dive must contain travel.
        terminal_targets = (
            ()
            if key == source_entries[key]
            else targets_by_search_key.get(key, ())
        )
        for target_index, candidate_target in terminal_targets:
            attachment_distance_m = math.dist(
                candidate.point,
                candidate_target,
            )
            if attachment_distance_m > terminal_attachment_limit_m + 1e-9:
                continue
            if attachment_distance_m <= 1e-9 or edge_is_clear(
                candidate.point,
                candidate_target,
            ):
                terminal_key = key
                selected_seed_key = source_entries[key]
                selected_terminal_hint_index = int(target_index)
                selected_terminal_hint_point = candidate_target
                terminal_attachment_distance_m = float(attachment_distance_m)
                break
        if terminal_key is not None:
            break

        neighbor_candidates: dict[
            VoxelGraphKey,
            tuple[float, int, float, VoxelGraphKey, _MeshCandidate, bool],
        ] = {}

        def retain_neighbor(
            neighbor_key: VoxelGraphKey,
            neighbor: _MeshCandidate,
            *,
            guided_portal: bool,
        ) -> None:
            heuristic_m, guide_index = route_heuristic(neighbor)
            item = (
                heuristic_m,
                -guide_index,
                math.dist(candidate.point, neighbor.point),
                neighbor_key,
                neighbor,
                bool(guided_portal),
            )
            existing = neighbor_candidates.get(neighbor_key)
            if existing is None or item[:4] < existing[:4]:
                neighbor_candidates[neighbor_key] = item

        for offset in offsets:
            neighbor_key = (
                key[0] + offset[0],
                key[1] + offset[1],
                key[2] + offset[2],
            )
            if neighbor_key in closed:
                continue
            neighbor = candidate_for_key(neighbor_key)
            if neighbor is None:
                continue
            retain_neighbor(
                neighbor_key,
                neighbor,
                guided_portal=False,
            )

        _candidate_heuristic_m, candidate_guide_index = route_heuristic(
            candidate
        )
        maximum_portal_route_distance_m = float(
            resolved.max_edge_distance_m
        )
        portal_key_radius = 1
        for guide_index in range(
            candidate_guide_index + 1,
            len(finite_guides),
        ):
            route_distance_m = (
                guide_remaining_m[candidate_guide_index]
                - guide_remaining_m[guide_index]
            )
            if route_distance_m > maximum_portal_route_distance_m + 1e-9:
                break
            guide = finite_guides[guide_index]
            base_key = lattice_key(guide)
            for delta_x in range(-portal_key_radius, portal_key_radius + 1):
                for delta_y in range(-portal_key_radius, portal_key_radius + 1):
                    for delta_z in range(
                        -portal_key_radius,
                        portal_key_radius + 1,
                    ):
                        neighbor_key = (
                            base_key[0] + delta_x,
                            base_key[1] + delta_y,
                            base_key[2] + delta_z,
                        )
                        if neighbor_key == key or neighbor_key in closed:
                            continue
                        neighbor = candidate_for_key(neighbor_key)
                        if neighbor is None:
                            continue
                        portal_distance_m = math.dist(
                            candidate.point,
                            neighbor.point,
                        )
                        if (
                            portal_distance_m
                            > maximum_portal_route_distance_m + 1e-9
                        ):
                            continue
                        guided_portal_candidate_count += 1
                        retain_neighbor(
                            neighbor_key,
                            neighbor,
                            guided_portal=True,
                        )

        ordered_neighbor_candidates = sorted(neighbor_candidates.values())
        local_edge_candidate_limit = int(
            resolved.max_edge_candidates_per_node
        )
        selected_neighbor_candidates = (
            [
                item
                for item in ordered_neighbor_candidates
                if not item[5]
            ][:local_edge_candidate_limit]
            + [
                item
                for item in ordered_neighbor_candidates
                if item[5]
            ][:guided_portal_edge_candidate_limit]
        )
        for (
            heuristic_m,
            negative_guide_index,
            edge_distance_m,
            neighbor_key,
            neighbor,
            guided_portal,
        ) in selected_neighbor_candidates:
            maximum_guide_index_seen = max(
                maximum_guide_index_seen,
                -int(negative_guide_index),
            )
            if neighbor_key not in candidates:
                if len(candidates) >= int(resolved.max_nodes):
                    node_limit_reached = True
                    break
                candidates[neighbor_key] = neighbor
            candidate_distance_m = distance_m + edge_distance_m
            existing_distance_m = distances.get(neighbor_key)
            if (
                existing_distance_m is not None
                and candidate_distance_m >= existing_distance_m - 1e-12
            ):
                # Every existing distance entered the queue only through an
                # already voxel- and mesh-safe edge. A non-improving edge can
                # never enter the persisted shortest path, so do not spend an
                # exact collision query merely to discard it afterward.
                non_improving_edge_skip_count += 1
                continue
            ordered = (
                (key, neighbor_key)
                if key < neighbor_key
                else (neighbor_key, key)
            )
            clear = checked_edges.get(ordered)
            if clear is None:
                clear = True
                # Endpoint probes do not prove the interior of even a local
                # diagonal edge. Runtime samples every executable segment at
                # half-lattice spacing, so cache construction must apply that
                # same voxel test before persisting an edge. Otherwise a path
                # can pass cache generation and fail immediately when the
                # serialized atlas checks an occupied midpoint.
                sample_fractions = segment_voxel_probe_fractions(
                    candidate.point,
                    neighbor.point,
                    lattice_spacing_m=grid_size,
                    maximum_sample_spacing_m=max(0.125, minimum_spacing * 0.5),
                )
                for fraction in sample_fractions:
                    sample = tuple(
                        candidate.point[axis]
                        + (
                            neighbor.point[axis]
                            - candidate.point[axis]
                        )
                        * fraction
                        for axis in range(3)
                    )
                    edge_voxel_probe_count += 1
                    if guided_portal:
                        guided_portal_voxel_probe_count += 1
                    try:
                        probe = point_probe(sample)  # type: ignore[arg-type]
                    except Exception:
                        probe = None
                    if (
                        probe is None
                        or not bool(probe[0])
                        or not math.isfinite(float(probe[1]))
                        or float(probe[1]) + 1e-9
                        < float(resolved.minimum_clearance_m)
                    ):
                        edge_voxel_rejection_count += 1
                        if guided_portal:
                            guided_portal_voxel_rejection_count += 1
                        clear = False
                        break
                if clear:
                    clear = bool(
                        edge_is_clear(candidate.point, neighbor.point)
                    )
                checked_edges[ordered] = clear
                if not clear:
                    mesh_rejected_edge_count += 1
                elif guided_portal and edge_distance_m > cell_diagonal_m:
                    guided_portal_accepted_count += 1
            if not clear:
                continue
            distances[neighbor_key] = candidate_distance_m
            previous[neighbor_key] = key
            source_entries[neighbor_key] = source_entries[key]
            heapq.heappush(
                queue,
                (
                    candidate_distance_m + heuristic_weight * heuristic_m,
                    negative_guide_index,
                    candidate_distance_m,
                    neighbor_key,
                ),
            )
        if node_limit_reached:
            break

    diagnostic_seed_key = selected_seed_key or min(
        entry_candidates,
        key=lambda entry_key: (
            entry_candidates[entry_key][0],
            entry_candidates[entry_key][2],
            entry_key,
        ),
    )
    seed_hint_index, seed_point, seed_snap_distance_m = entry_candidates[
        diagnostic_seed_key
    ]
    base_details: dict[str, object] = {
        "method": MESH_NAVIGATION_GRAPH_METHOD,
        "version": MESH_NAVIGATION_GRAPH_VERSION,
        "sampling_envelope": "selected_route_corridor",
        "inside_evidence": "cached_voxel_free_space_point_probe",
        "edge_evidence": (
            "exact_cached_mesh_and_voxel_sampled_edges"
        ),
        "search_method": "weighted_a_star",
        "search_heuristic": "ordered_horizontal_route_remaining_distance",
        "route_guide_point_count": len(finite_guides),
        "maximum_route_guide_index_seen": int(maximum_guide_index_seen),
        "maximum_route_guide_fraction_seen": float(
            maximum_guide_index_seen / max(1, len(finite_guides) - 1)
        ),
        "edge_candidate_limit_per_node": int(
            resolved.max_edge_candidates_per_node
        ),
        "guided_portal_edge_candidate_limit_per_node": int(
            guided_portal_edge_candidate_limit
        ),
        "lattice_spacing_m": [float(value) for value in grid_size],
        "minimum_clearance_m": float(resolved.minimum_clearance_m),
        "seed_point": [float(value) for value in seed_point],
        "seed_hint_index": int(seed_hint_index),
        "entry_seed_hint_count": len(finite_seeds),
        "entry_seed_candidate_count": len(entry_candidates),
        "seed_graph_key": [int(value) for value in diagnostic_seed_key],
        "seed_snap_distance_m": float(seed_snap_distance_m),
        "terminal_hint_count": len(finite_targets),
        "terminal_hint_index": 0,
        "terminal_hint_point": [float(value) for value in target],
        "terminal_attachment_limit_m": float(terminal_attachment_limit_m),
        "exact_terminal_point_required": bool(require_exact_terminal_point),
        "probe_count": int(probe_count),
        "free_probe_count": int(free_probe_count),
        "clearance_rejection_count": int(clearance_rejection_count),
        "discovered_node_count": len(candidates),
        "expanded_node_count": int(expanded_count),
        "mesh_edge_check_count": len(checked_edges),
        "mesh_rejected_edge_count": int(mesh_rejected_edge_count),
        "edge_voxel_probe_count": int(edge_voxel_probe_count),
        "edge_voxel_rejection_count": int(edge_voxel_rejection_count),
        "guided_portal_candidate_count": int(
            guided_portal_candidate_count
        ),
        "guided_portal_voxel_probe_count": int(
            guided_portal_voxel_probe_count
        ),
        "guided_portal_voxel_rejection_count": int(
            guided_portal_voxel_rejection_count
        ),
        "guided_portal_accepted_count": int(
            guided_portal_accepted_count
        ),
        "non_improving_edge_skip_count": int(
            non_improving_edge_skip_count
        ),
        "mesh_safe_undirected_edge_count": int(
            sum(1 for clear in checked_edges.values() if clear)
        ),
        "node_limit_reached": bool(node_limit_reached),
        "component_complete": False,
    }
    if terminal_key is None or selected_seed_key is None:
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                **base_details,
                "reason": (
                    "goal_directed_mesh_graph_node_limit_reached"
                    if node_limit_reached
                    else "goal_directed_mesh_graph_terminal_unreachable"
                ),
            },
        )
    path_keys = _mesh_graph_path_from_previous(
        previous,
        start_key=selected_seed_key,
        terminal_key=terminal_key,
    )
    if len(path_keys) < 2:
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                **base_details,
                "reason": "goal_directed_mesh_graph_terminal_path_missing",
            },
        )
    graph = _mesh_path_graph(
        candidates,
        path_keys,
        grid_size_m=grid_size,
        terminal_key=terminal_key,
    )
    terminal_graph_distance_m = sum(
        math.dist(candidates[first].point, candidates[second].point)
        for first, second in zip(path_keys, path_keys[1:], strict=False)
    )
    return MeshNavigationGraphBuildResult(
        graph=graph,
        details={
            **base_details,
            "reason": "goal_directed_mesh_terminal_path_built",
            "terminal_graph_key": [int(value) for value in terminal_key],
            "terminal_graph_distance_m": float(terminal_graph_distance_m),
            "terminal_attachment_distance_m": float(
                terminal_attachment_distance_m
            ),
            "selected_terminal_hint_index": int(
                selected_terminal_hint_index
                if selected_terminal_hint_index is not None
                else 0
            ),
            "selected_terminal_hint_point": [
                float(value)
                for value in (
                    selected_terminal_hint_point
                    if selected_terminal_hint_point is not None
                    else target
                )
            ],
            "persisted_path_node_count": len(path_keys),
            "persisted_path_edge_count": max(0, len(path_keys) - 1),
            "graph": graph.diagnostic_payload(),
            "terminal_count": int(graph.terminal_count),
            "unknown_boundary_count": int(graph.unknown_boundary_count),
        },
    )


def _mesh_graph_shortest_distances(
    candidates: Mapping[VoxelGraphKey, _MeshCandidate],
    adjacency: Mapping[VoxelGraphKey, set[VoxelGraphKey]],
    *,
    start_key: VoxelGraphKey,
) -> tuple[
    dict[VoxelGraphKey, float],
    dict[VoxelGraphKey, VoxelGraphKey],
]:
    distances = {start_key: 0.0}
    previous: dict[VoxelGraphKey, VoxelGraphKey] = {}
    queue: list[tuple[float, VoxelGraphKey]] = [(0.0, start_key)]
    while queue:
        distance_m, key = heapq.heappop(queue)
        if distance_m > distances.get(key, math.inf) + 1e-12:
            continue
        for target in sorted(adjacency.get(key, ())):
            candidate_distance_m = distance_m + math.dist(
                candidates[key].point,
                candidates[target].point,
            )
            existing = distances.get(target)
            if (
                existing is not None
                and candidate_distance_m >= existing - 1e-12
            ):
                continue
            distances[target] = candidate_distance_m
            previous[target] = key
            heapq.heappush(queue, (candidate_distance_m, target))
    return distances, previous


def _mesh_graph_path_from_previous(
    previous: Mapping[VoxelGraphKey, VoxelGraphKey],
    *,
    start_key: VoxelGraphKey,
    terminal_key: VoxelGraphKey,
) -> tuple[VoxelGraphKey, ...]:
    path = [terminal_key]
    while path[-1] != start_key:
        predecessor = previous.get(path[-1])
        if predecessor is None:
            return ()
        path.append(predecessor)
    path.reverse()
    return tuple(path)


def _mesh_path_graph(
    candidates: Mapping[VoxelGraphKey, _MeshCandidate],
    path_keys: Sequence[VoxelGraphKey],
    *,
    spacing_m: float | None = None,
    grid_size_m: Sequence[float] | None = None,
    terminal_key: VoxelGraphKey,
) -> NavigationVoxel3DGraph:
    if grid_size_m is None:
        resolved_spacing = _positive_finite(
            spacing_m,
            "mesh path graph lattice spacing",
        )
        grid_size = (
            resolved_spacing,
            resolved_spacing,
            resolved_spacing,
        )
    else:
        grid_size = _orthogonal_grid_size(grid_size_m)
    minimum_spacing = min(grid_size)
    cell_diagonal_m = math.sqrt(sum(value * value for value in grid_size))
    path = tuple(path_keys)
    progress: dict[VoxelGraphKey, float] = {path[0]: 0.0}
    path_edge_distances: list[float] = []
    path_vertical_distances: list[float] = []
    for first, second in zip(path, path[1:], strict=False):
        edge_distance_m = math.dist(
            candidates[first].point,
            candidates[second].point,
        )
        path_edge_distances.append(edge_distance_m)
        path_vertical_distances.append(
            abs(candidates[first].point[1] - candidates[second].point[1])
        )
        progress[second] = progress[first] + edge_distance_m
    nodes: dict[VoxelGraphKey, NavigationVoxel3DNode] = {}
    edges: dict[VoxelGraphKey, tuple[NavigationVoxel3DEdge, ...]] = {}
    for index, key in enumerate(path):
        candidate = candidates[key]
        neighbors = tuple(
            value
            for value in (
                path[index - 1] if index > 0 else None,
                path[index + 1] if index + 1 < len(path) else None,
            )
            if value is not None
        )
        terminal = key == terminal_key
        nodes[key] = NavigationVoxel3DNode(
            key=key,
            center=candidate.point,
            footprint_cell=candidate.footprint_cell,
            component_id=0,
            progress_m=float(progress[key]),
            connectivity_score=float(len(neighbors)),
            local_degree=len(neighbors),
            dead_end=terminal,
            terminal=terminal,
            unknown_boundary=False,
            available_volume_m3=float(candidate.clearance_m**3),
            min_clearance_m=float(candidate.clearance_m),
            mean_clearance_m=float(candidate.clearance_m),
            preferred_neighbors=neighbors,
        )
        outgoing: list[NavigationVoxel3DEdge] = []
        for target in neighbors:
            delta = tuple(
                candidates[target].point[axis] - candidate.point[axis]
                for axis in range(3)
            )
            distance_m = math.sqrt(sum(value * value for value in delta))
            outgoing.append(
                NavigationVoxel3DEdge(
                    source=key,
                    target=target,
                    distance_m=float(distance_m),
                    direction=tuple(
                        value / max(distance_m, 1e-12)
                        for value in delta
                    ),
                    min_clearance_m=min(
                        float(candidate.clearance_m),
                        float(candidates[target].clearance_m),
                    ),
                    line_of_sight=True,
                )
            )
        edges[key] = tuple(outgoing)
    return NavigationVoxel3DGraph(
        nodes=nodes,
        edges=edges,
        component_count=1,
        grid_size_m=(
            float(grid_size[0]),
            float(grid_size[1]),
            float(grid_size[2]),
        ),
        max_edge_distance_cells=max(
            2,
            int(
                math.ceil(
                    max(path_edge_distances, default=minimum_spacing)
                    / minimum_spacing
                )
            ),
        ),
        max_edges_per_node=2,
        max_edge_distance_m=float(
            max(
                path_edge_distances,
                default=cell_diagonal_m,
            )
        ),
        max_vertical_edge_distance_m=float(
            max(path_vertical_distances, default=grid_size[1])
        ),
        method=MESH_NAVIGATION_GRAPH_METHOD,
    )


def _build_mesh_graph_result(
    candidates: Mapping[VoxelGraphKey, _MeshCandidate],
    *,
    edge_is_clear: MeshEdgeSafetyCheck,
    terminal_hint_points: Sequence[Point],
    resolved: MeshNavigationGraphConfig,
    base_details: Mapping[str, object],
    node_limit_reached: bool,
    missing_candidate_reason: str,
    built_reason: str,
) -> MeshNavigationGraphBuildResult:
    """Connect candidate points and serialize one sparse graph result."""
    if len(candidates) < 2:
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={**base_details, "reason": str(missing_candidate_reason)},
        )

    adjacency, edge_details = _mesh_safe_adjacency(
        candidates,
        edge_is_clear=edge_is_clear,
        max_edges_per_node=resolved.max_edges_per_node,
        max_edge_candidates_per_node=resolved.max_edge_candidates_per_node,
        max_edge_candidates_per_direction=(
            resolved.max_edge_candidates_per_direction
        ),
        max_edge_distance_m=resolved.max_edge_distance_m,
        max_vertical_edge_distance_m=resolved.max_vertical_edge_distance_m,
    )
    component_ids = _component_ids(adjacency)
    component_count = max(component_ids.values(), default=-1) + 1
    hinted_terminals = _hinted_terminal_keys(
        candidates,
        terminal_hint_points,
        maximum_distance_m=max(
            float(resolved.max_edge_distance_m) * 4.0,
            float(resolved.horizontal_sample_spacing_m) * 4.0,
        ),
    )
    nodes: dict[VoxelGraphKey, NavigationVoxel3DNode] = {}
    edges: dict[VoxelGraphKey, tuple[NavigationVoxel3DEdge, ...]] = {}
    for key in sorted(candidates):
        candidate = candidates[key]
        targets = tuple(sorted(adjacency.get(key, ())))
        degree = len(targets)
        unknown_boundary = bool(
            node_limit_reached and (degree <= 1 or key in hinted_terminals)
        )
        terminal = bool(
            degree > 0
            and not unknown_boundary
            and (
                key in hinted_terminals
                if terminal_hint_points
                else degree <= 1
            )
        )
        nodes[key] = NavigationVoxel3DNode(
            key=key,
            center=candidate.point,
            footprint_cell=candidate.footprint_cell,
            component_id=int(component_ids[key]),
            progress_m=0.0,
            connectivity_score=float(degree),
            local_degree=int(degree),
            dead_end=terminal,
            terminal=terminal,
            unknown_boundary=unknown_boundary,
            available_volume_m3=float(candidate.clearance_m**3),
            min_clearance_m=float(candidate.clearance_m),
            mean_clearance_m=float(candidate.clearance_m),
            preferred_neighbors=targets,
        )
        outgoing: list[NavigationVoxel3DEdge] = []
        for target in targets:
            other = candidates[target]
            delta = tuple(
                float(other.point[axis] - candidate.point[axis])
                for axis in range(3)
            )
            distance_m = math.sqrt(sum(value * value for value in delta))
            if distance_m <= 1e-9:
                continue
            outgoing.append(
                NavigationVoxel3DEdge(
                    source=key,
                    target=target,
                    distance_m=float(distance_m),
                    direction=tuple(value / distance_m for value in delta),
                    min_clearance_m=min(
                        float(candidate.clearance_m),
                        float(other.clearance_m),
                    ),
                    line_of_sight=True,
                )
            )
        edges[key] = tuple(outgoing)

    graph = NavigationVoxel3DGraph(
        nodes=nodes,
        edges=edges,
        component_count=int(component_count),
        grid_size_m=(
            float(resolved.horizontal_sample_spacing_m),
            min(4.0, float(resolved.vertical_sample_spacing_m)),
            float(resolved.horizontal_sample_spacing_m),
        ),
        max_edge_distance_cells=max(
            1,
            int(
                math.ceil(
                    float(resolved.max_edge_distance_m)
                    / float(resolved.horizontal_sample_spacing_m)
                )
            ),
        ),
        max_edges_per_node=int(resolved.max_edges_per_node),
        max_edge_distance_m=float(resolved.max_edge_distance_m),
        max_vertical_edge_distance_m=float(resolved.max_vertical_edge_distance_m),
        method=MESH_NAVIGATION_GRAPH_METHOD,
    )
    return MeshNavigationGraphBuildResult(
        graph=graph,
        details={
            **base_details,
            **edge_details,
            "reason": str(built_reason),
            "graph": graph.diagnostic_payload(),
            "terminal_hint_count": len(terminal_hint_points),
            "terminal_hint_node_count": len(hinted_terminals),
            "terminal_count": int(graph.terminal_count),
            "unknown_boundary_count": int(graph.unknown_boundary_count),
        },
    )


def _anchor_is_finite(anchor: MeshNavigationGraphAnchor) -> bool:
    return (
        len(anchor.point) == 3
        and all(math.isfinite(float(value)) for value in anchor.point)
        and math.isfinite(float(anchor.clearance_m))
        and float(anchor.clearance_m) >= 0.0
    )


def _anchored_candidate_sort_key(
    candidate: _MeshCandidate,
) -> tuple[float, Point]:
    return (-float(candidate.clearance_m), candidate.point)


def _empty_result(
    reason: str,
    *,
    resolved: MeshNavigationGraphConfig,
) -> MeshNavigationGraphBuildResult:
    return MeshNavigationGraphBuildResult(
        graph=None,
        details={
            "method": MESH_NAVIGATION_GRAPH_METHOD,
            "version": MESH_NAVIGATION_GRAPH_VERSION,
            "reason": str(reason),
            "horizontal_sample_spacing_m": float(
                resolved.horizontal_sample_spacing_m
            ),
            "vertical_sample_spacing_m": float(resolved.vertical_sample_spacing_m),
            "minimum_clearance_m": float(resolved.minimum_clearance_m),
        },
    )


def _manifest_bounds(
    manifest: Mapping[str, object],
) -> tuple[Point, Point] | None:
    chunks = manifest.get("chunks")
    if not isinstance(chunks, Mapping):
        return None
    lowers: list[Point] = []
    uppers: list[Point] = []
    for value in chunks.values():
        if not isinstance(value, Mapping):
            continue
        lower = _point(value.get("bounds_min"))
        upper = _point(value.get("bounds_max"))
        if lower is None or upper is None:
            continue
        lowers.append(
            tuple(min(lower[axis], upper[axis]) for axis in range(3))
        )
        uppers.append(
            tuple(max(lower[axis], upper[axis]) for axis in range(3))
        )
    if not lowers or not uppers:
        return None
    return (
        tuple(min(value[axis] for value in lowers) for axis in range(3)),
        tuple(max(value[axis] for value in uppers) for axis in range(3)),
    )  # type: ignore[return-value]


def _route_component_cells(route: Mapping[str, object]) -> tuple[FootprintCell, ...]:
    for name in ("component_cells", "voxel_sampling_cells", "cells"):
        raw = route.get(name)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            continue
        values = list(raw)
        if len(values) % 2:
            continue
        cells: list[FootprintCell] = []
        try:
            for index in range(0, len(values), 2):
                cells.append((int(values[index]), int(values[index + 1])))
        except (TypeError, ValueError):
            continue
        if cells:
            return tuple(sorted(set(cells)))
    return ()


def _route_cell_size(
    route: Mapping[str, object],
    manifest: Mapping[str, object],
) -> float | None:
    for source in (route, manifest):
        raw = source.get("footprint_cell_size")
        try:
            size = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(size) and size > 0.0:
            return size
    return None


def _cell_probe_positions(
    cell: FootprintCell,
    *,
    cell_size: float,
    spacing_m: float,
) -> tuple[tuple[float, float], ...]:
    count = max(1, int(math.ceil(float(cell_size) / float(spacing_m))))
    return tuple(
        (
            (float(cell[0]) + (float(x_index) + 0.5) / float(count))
            * float(cell_size),
            (float(cell[1]) + (float(z_index) + 0.5) / float(count))
            * float(cell_size),
        )
        for x_index in range(count)
        for z_index in range(count)
    )


def _vertical_mesh_intersections(
    x: float,
    z: float,
    *,
    bounds_min: Point,
    bounds_max: Point,
    triangles: np.ndarray,
    triangle_min: np.ndarray,
    triangle_max: np.ndarray,
    merge_epsilon_m: float,
) -> tuple[float, ...]:
    """Return de-duplicated vertical ray/triangle intersections in world Y."""
    epsilon = max(1e-7, float(merge_epsilon_m) * 0.1)
    lower_y = float(bounds_min[1]) - epsilon
    upper_y = float(bounds_max[1]) + epsilon
    if triangles.size == 0:
        return ()
    column_mask = (
        (triangle_min[:, 0] <= float(x) + epsilon)
        & (triangle_max[:, 0] >= float(x) - epsilon)
        & (triangle_min[:, 2] <= float(z) + epsilon)
        & (triangle_max[:, 2] >= float(z) - epsilon)
    )
    if not bool(np.any(column_mask)):
        return ()
    origin = np.asarray((float(x), lower_y, float(z)), dtype=np.float64)
    direction = np.asarray((0.0, 1.0, 0.0), dtype=np.float64)
    parameters = _ray_triangle_parameters(
        origin,
        direction,
        triangles[column_mask],
        maximum_t=upper_y - lower_y,
    )
    if parameters.size == 0:
        return ()
    return tuple(
        lower_y + parameter
        for parameter in _dedupe_sorted_values(
            parameters,
            epsilon_m=merge_epsilon_m,
        )
    )


def _interval_sample_positions(
    lower_y: float,
    upper_y: float,
    *,
    clearance_m: float,
    spacing_m: float,
    max_points: int,
) -> tuple[float, ...]:
    lower = float(lower_y) + float(clearance_m)
    upper = float(upper_y) - float(clearance_m)
    if upper <= lower + 1e-7:
        return ()
    height = upper - lower
    count = min(
        max(1, int(math.ceil(height / max(1e-6, float(spacing_m))))),
        max(1, int(max_points)),
    )
    return tuple(
        lower + (float(index) + 0.5) * height / float(count)
        for index in range(count)
    )


def _point_has_clearance(
    point: Point,
    *,
    clearance_m: float,
    triangles: np.ndarray,
    triangle_min: np.ndarray,
    triangle_max: np.ndarray,
) -> bool:
    """Return whether no mesh triangle lies within the requested clearance."""
    radius = max(0.0, float(clearance_m))
    if radius <= 1e-9:
        return True
    if triangles.size == 0:
        return True
    point_array = np.asarray(point, dtype=np.float64)
    below = np.maximum(triangle_min - point_array, 0.0)
    above = np.maximum(point_array - triangle_max, 0.0)
    aabb_distance_squared = np.einsum("ij,ij->i", below + above, below + above)
    candidate_indices = np.flatnonzero(
        aabb_distance_squared <= radius * radius + 1e-12
    )
    for index in candidate_indices:
        if _point_triangle_distance_squared(point_array, triangles[index]) < (
            radius * radius - 1e-12
        ):
            return False
    return True


def _mesh_safe_adjacency(
    candidates: Mapping[VoxelGraphKey, _MeshCandidate],
    *,
    edge_is_clear: MeshEdgeSafetyCheck,
    max_edges_per_node: int,
    max_edge_candidates_per_node: int,
    max_edge_candidates_per_direction: int,
    max_edge_distance_m: float,
    max_vertical_edge_distance_m: float,
) -> tuple[dict[VoxelGraphKey, set[VoxelGraphKey]], dict[str, int]]:
    """Build a bounded, symmetric exact-mesh edge set.

    Spatial buckets avoid an all-pairs search. Each source retains a small,
    bounded sequence of nearby candidates in every direction bin before exact
    collision checks. The second candidate matters: the nearest one can be
    blocked by a local mesh sliver even when the passage remains connected.
    """
    bucket_size = max(1e-6, float(max_edge_distance_m))
    buckets: dict[tuple[int, int, int], list[VoxelGraphKey]] = {}
    for key, candidate in candidates.items():
        bucket = tuple(
            int(math.floor(candidate.point[axis] / bucket_size))
            for axis in range(3)
        )
        buckets.setdefault(bucket, []).append(key)
    pair_distances: dict[tuple[VoxelGraphKey, VoxelGraphKey], float] = {}
    for key in sorted(candidates):
        candidate = candidates[key]
        source_bucket = tuple(
            int(math.floor(candidate.point[axis] / bucket_size))
            for axis in range(3)
        )
        candidates_by_direction: dict[
            tuple[int, int, int],
            list[tuple[float, VoxelGraphKey]],
        ] = {}
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for target in buckets.get(
                        (
                            source_bucket[0] + dx,
                            source_bucket[1] + dy,
                            source_bucket[2] + dz,
                        ),
                        (),
                    ):
                        if target == key:
                            continue
                        other = candidates[target]
                        delta = tuple(
                            float(other.point[axis] - candidate.point[axis])
                            for axis in range(3)
                        )
                        distance_m = math.sqrt(sum(value * value for value in delta))
                        if (
                            distance_m <= 1e-9
                            or distance_m > float(max_edge_distance_m) + 1e-9
                            or abs(delta[1])
                            > float(max_vertical_edge_distance_m) + 1e-9
                        ):
                            continue
                        direction = _edge_direction_bin(delta)
                        candidates_by_direction.setdefault(direction, []).append(
                            (distance_m, target)
                        )
        for distance_m, target in _round_robin_direction_candidates(
            candidates_by_direction,
            max_per_direction=max_edge_candidates_per_direction,
            maximum=max_edge_candidates_per_node,
        ):
            ordered = (key, target) if key < target else (target, key)
            existing = pair_distances.get(ordered)
            if existing is None or distance_m < existing:
                pair_distances[ordered] = distance_m
    pairs = sorted(
        (distance_m, first, second)
        for (first, second), distance_m in pair_distances.items()
    )
    adjacency = {key: set() for key in candidates}
    checked = 0
    rejected = 0
    accepted = 0
    degree_limit = max(1, int(max_edges_per_node))
    for _distance_m, first, second in pairs:
        if len(adjacency[first]) >= degree_limit or len(adjacency[second]) >= degree_limit:
            continue
        checked += 1
        if not edge_is_clear(candidates[first].point, candidates[second].point):
            rejected += 1
            continue
        adjacency[first].add(second)
        adjacency[second].add(first)
        accepted += 1
    return adjacency, {
        "candidate_edge_pair_count": int(len(pairs)),
        "mesh_edge_check_count": int(checked),
        "mesh_rejected_edge_count": int(rejected),
        "mesh_safe_undirected_edge_count": int(accepted),
    }


def _round_robin_direction_candidates(
    candidates_by_direction: Mapping[
        tuple[int, int, int],
        Sequence[tuple[float, VoxelGraphKey]],
    ],
    *,
    max_per_direction: int,
    maximum: int,
) -> tuple[tuple[float, VoxelGraphKey], ...]:
    """Keep bounded alternatives without starving one local direction."""
    per_direction = max(1, int(max_per_direction))
    limit = max(1, int(maximum))
    ranked = {
        direction: tuple(sorted(values, key=lambda item: (item[0], item[1])))[:per_direction]
        for direction, values in candidates_by_direction.items()
        if values
    }
    result: list[tuple[float, VoxelGraphKey]] = []
    for rank in range(per_direction):
        for direction in sorted(ranked):
            values = ranked[direction]
            if rank >= len(values):
                continue
            result.append(values[rank])
            if len(result) >= limit:
                return tuple(result)
    return tuple(result)


def _edge_direction_bin(delta: Sequence[float]) -> tuple[int, int, int]:
    """Return a deterministic sign bin for one local candidate direction."""
    return tuple(
        1 if float(value) > 1e-9 else -1 if float(value) < -1e-9 else 0
        for value in delta
    )  # type: ignore[return-value]


def _component_ids(
    adjacency: Mapping[VoxelGraphKey, set[VoxelGraphKey]],
) -> dict[VoxelGraphKey, int]:
    result: dict[VoxelGraphKey, int] = {}
    component_id = 0
    for start in sorted(adjacency):
        if start in result:
            continue
        result[start] = component_id
        queue: deque[VoxelGraphKey] = deque((start,))
        while queue:
            current = queue.popleft()
            for target in sorted(adjacency.get(current, ())):
                if target in result:
                    continue
                result[target] = component_id
                queue.append(target)
        component_id += 1
    return result


def _hinted_terminal_keys(
    candidates: Mapping[VoxelGraphKey, _MeshCandidate],
    hint_points: Sequence[Point],
    *,
    maximum_distance_m: float,
) -> frozenset[VoxelGraphKey]:
    """Snap cache-time target hints to direct-mesh candidate nodes.

    Hints constrain only which verified mesh node is called a terminal.  They
    never add a node, edge, or connectivity assertion to the roadmap.
    """
    if not candidates or not hint_points:
        return frozenset()
    maximum_squared = max(0.0, float(maximum_distance_m)) ** 2
    selected: set[VoxelGraphKey] = set()
    for hint in hint_points:
        nearest_key, distance_squared = min(
            (
                (
                    key,
                    sum(
                        (float(candidate.point[axis]) - float(hint[axis])) ** 2
                        for axis in range(3)
                    ),
                )
                for key, candidate in candidates.items()
            ),
            key=lambda item: (item[1], item[0]),
        )
        if distance_squared <= maximum_squared + 1e-9:
            selected.add(nearest_key)
    return frozenset(selected)


def _orthogonal_grid_size(value: Sequence[float]) -> tuple[float, float, float]:
    """Validate one X/Y/Z lattice size tuple."""
    if len(value) != 3:
        raise ValueError("mesh graph grid size must contain three axes")
    return tuple(
        _positive_finite(item, "mesh graph grid axis size")
        for item in value
    )  # type: ignore[return-value]


def _orthogonal_lattice_key(
    point: Sequence[float],
    grid_size_m: Sequence[float],
) -> VoxelGraphKey:
    """Map one world point to an orthogonal navigation lattice."""
    sizes = _orthogonal_grid_size(grid_size_m)
    return tuple(
        int(math.floor(float(point[axis]) / sizes[axis]))
        for axis in range(3)
    )  # type: ignore[return-value]


def _orthogonal_lattice_center(
    key: Sequence[int],
    grid_size_m: Sequence[float],
) -> Point:
    """Return the physical center of one orthogonal lattice cell."""
    sizes = _orthogonal_grid_size(grid_size_m)
    return tuple(
        (float(key[axis]) + 0.5) * sizes[axis]
        for axis in range(3)
    )  # type: ignore[return-value]


def _orthogonal_step_limits(
    maximum_distance_m: float,
    grid_size_m: Sequence[float],
) -> tuple[int, int, int]:
    """Return per-axis key radii covering a physical search distance."""
    maximum = _positive_finite(
        maximum_distance_m,
        "mesh graph lattice search distance",
    )
    sizes = _orthogonal_grid_size(grid_size_m)
    return tuple(
        max(1, int(math.ceil(maximum / sizes[axis])))
        for axis in range(3)
    )  # type: ignore[return-value]


def _candidate_key(
    point: Point,
    *,
    horizontal_spacing_m: float,
    vertical_spacing_m: float,
) -> VoxelGraphKey:
    return (
        int(math.floor(float(point[0]) / horizontal_spacing_m)),
        int(math.floor(float(point[1]) / vertical_spacing_m)),
        int(math.floor(float(point[2]) / horizontal_spacing_m)),
    )


def _triangles_for_bounds(
    triangle_provider: TriangleProvider,
    bounds_min: Point,
    bounds_max: Point,
) -> np.ndarray:
    groups: list[np.ndarray] = []
    try:
        meshes = triangle_provider(bounds_min, bounds_max)
    except Exception:
        return np.empty((0, 3, 3), dtype=np.float64)
    for mesh in meshes:
        triangles = np.asarray(mesh, dtype=np.float64)
        if triangles.ndim == 2 and triangles.shape[1] == 3:
            if len(triangles) % 3:
                continue
            triangles = triangles.reshape(-1, 3, 3)
        if (
            triangles.ndim != 3
            or triangles.shape[1:] != (3, 3)
            or not np.all(np.isfinite(triangles))
        ):
            continue
        groups.append(triangles)
    if not groups:
        return np.empty((0, 3, 3), dtype=np.float64)
    return np.concatenate(groups, axis=0)


def _ray_triangle_parameters(
    origin: np.ndarray,
    direction: np.ndarray,
    triangles: np.ndarray,
    *,
    maximum_t: float,
) -> np.ndarray:
    """Return positive Moller-Trumbore ray parameters for triangle hits."""
    first = triangles[:, 0]
    edge_one = triangles[:, 1] - first
    edge_two = triangles[:, 2] - first
    pvec = np.cross(direction, edge_two)
    determinant = np.einsum("ij,ij->i", edge_one, pvec)
    valid = np.abs(determinant) > 1e-10
    if not np.any(valid):
        return np.empty(0, dtype=np.float64)
    inverse = np.zeros_like(determinant)
    inverse[valid] = 1.0 / determinant[valid]
    tvec = origin - first
    u = np.einsum("ij,ij->i", tvec, pvec) * inverse
    qvec = np.cross(tvec, edge_one)
    v = np.einsum("j,ij->i", direction, qvec) * inverse
    t = np.einsum("ij,ij->i", edge_two, qvec) * inverse
    mask = (
        valid
        & (u >= -1e-9)
        & (v >= -1e-9)
        & (u + v <= 1.0 + 1e-9)
        & (t > 1e-8)
        & (t < float(maximum_t) - 1e-8)
    )
    return np.sort(t[mask])


def _dedupe_sorted_values(
    values: np.ndarray,
    *,
    epsilon_m: float,
) -> tuple[float, ...]:
    if values.size == 0:
        return ()
    result = [float(values[0])]
    for raw in values[1:]:
        value = float(raw)
        if abs(value - result[-1]) <= float(epsilon_m):
            result[-1] = (result[-1] + value) * 0.5
        else:
            result.append(value)
    return tuple(result)


def _point_triangle_distance_squared(point: np.ndarray, triangle: np.ndarray) -> float:
    """Return squared point-to-triangle distance (Ericson region tests)."""
    first, second, third = triangle
    first_to_second = second - first
    first_to_third = third - first
    first_to_point = point - first
    dot_one = float(np.dot(first_to_second, first_to_point))
    dot_two = float(np.dot(first_to_third, first_to_point))
    if dot_one <= 0.0 and dot_two <= 0.0:
        return float(np.dot(first_to_point, first_to_point))

    second_to_point = point - second
    dot_three = float(np.dot(first_to_second, second_to_point))
    dot_four = float(np.dot(first_to_third, second_to_point))
    if dot_three >= 0.0 and dot_four <= dot_three:
        return float(np.dot(second_to_point, second_to_point))

    first_second_area = dot_one * dot_four - dot_three * dot_two
    if first_second_area <= 0.0 and dot_one >= 0.0 and dot_three <= 0.0:
        fraction = dot_one / max(1e-18, dot_one - dot_three)
        closest = first + fraction * first_to_second
        delta = point - closest
        return float(np.dot(delta, delta))

    third_to_point = point - third
    dot_five = float(np.dot(first_to_second, third_to_point))
    dot_six = float(np.dot(first_to_third, third_to_point))
    if dot_six >= 0.0 and dot_five <= dot_six:
        return float(np.dot(third_to_point, third_to_point))

    first_third_area = dot_five * dot_two - dot_one * dot_six
    if first_third_area <= 0.0 and dot_two >= 0.0 and dot_six <= 0.0:
        fraction = dot_two / max(1e-18, dot_two - dot_six)
        closest = first + fraction * first_to_third
        delta = point - closest
        return float(np.dot(delta, delta))

    second_third_area = dot_three * dot_six - dot_five * dot_four
    if second_third_area <= 0.0 and (dot_four - dot_three) >= 0.0 and (
        dot_five - dot_six
    ) >= 0.0:
        numerator = dot_four - dot_three
        denominator = numerator + dot_five - dot_six
        fraction = numerator / max(1e-18, denominator)
        closest = second + fraction * (third - second)
        delta = point - closest
        return float(np.dot(delta, delta))

    denominator = 1.0 / max(
        1e-18,
        first_second_area + first_third_area + second_third_area,
    )
    second_weight = first_third_area * denominator
    third_weight = first_second_area * denominator
    closest = first + first_to_second * second_weight + first_to_third * third_weight
    delta = point - closest
    return float(np.dot(delta, delta))


def _point(value: object) -> Point | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if len(value) != 3:
        return None
    try:
        point = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in point):
        return None
    return point  # type: ignore[return-value]


def _positive_finite(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be positive and finite") from exc
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name} must be positive and finite")
    return number
