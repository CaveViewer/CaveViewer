"""User-facing centerline Guided Dive route planning."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
import heapq
import math
import os
import time
from typing import Any, NoReturn

import numpy as np

from caveviewer.core.navigation.cache_metadata import (
    NAVIGATION_METADATA_KEY,
    NAVIGATION_ROUTE_Y_SMOOTHING_RADIUS_CELLS,
    cached_centerline_path,
)
from caveviewer.core.navigation.centerline import (
    CENTERLINE_COMPONENT_SELECTION_LONGEST_PATH,
    DEFAULT_CENTERLINE_ROUTE_SPEED_M_PER_SECOND,
    DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS,
    CenterlinePath,
    FootprintCell,
    Point,
    PointXZ,
    footprint_cell_distance,
    footprint_path_length,
    footprint_neighbors,
    footprint_world_center,
    generate_centerline_path,
    lowest_cost_footprint_path,
    navigable_footprint_neighbors,
    route_points_for_xz_points,
)
from caveviewer.core.navigation.mesh_collision import CachedChunkMeshCollisionGuard
from caveviewer.core.navigation.graph_route_safety import (
    GraphRouteSafetyFailure,
    GraphRouteSafetyPolicy,
    GraphRouteSafetyValidator,
)
from caveviewer.core.navigation.curvature import CURVATURE_PROFILE_METHOD
from caveviewer.core.navigation.route import (
    CameraRoute,
    NavigationConfigurationError,
    RouteKeyframe,
    path_length,
    route_keyframes_for_points,
)
from caveviewer.core.navigation.recovery_scan import (
    HemisphereProbe,
    iter_hemisphere_probes,
)
from caveviewer.core.navigation.voxel_volume import (
    DEFAULT_VOXEL_CURVATURE_RANK_THRESHOLD,
    DEFAULT_VOXEL_MAX_CELLS,
    DEFAULT_VOXEL_MAX_DISTANCE_M,
    DEFAULT_VOXEL_MAX_REGIONS,
    DEFAULT_VOXEL_LOCAL_REFINEMENT_FORWARD_M,
    DEFAULT_VOXEL_LOCAL_REFINEMENT_MAX_CELLS,
    DEFAULT_VOXEL_LOCAL_REFINEMENT_RADIUS_M,
    DEFAULT_VOXEL_SIZE_M,
    LocalVoxelVolume,
    VoxelVolumeConfig,
    VOXEL_ANALYSIS_OUTCOME_BUILT,
    VOXEL_ANALYSIS_OUTCOME_DISABLED,
    VOXEL_ANALYSIS_OUTCOME_ERROR,
    VOXEL_ANALYSIS_OUTCOME_CACHE_HIT,
    VOXEL_ANALYSIS_OUTCOME_CACHE_MISS,
    VOXEL_ANALYSIS_OUTCOME_MESH_GUARD_UNAVAILABLE,
    VOXEL_VOLUME_METHOD,
    analyze_curvature_guided_voxel_volume,
    build_surface_voxel_volume,
)
from caveviewer.core.navigation.voxel_cache import (
    NAVIGATION_VOXEL_CACHE_METHOD,
    NAVIGATION_VOXEL_CACHE_NAME,
    NAVIGATION_VOXEL_CACHE_VERSION,
    NAVIGATION_VOXEL_GRAPH_METHOD,
    NavigationVoxelAtlas,
    NavigationVoxelRoutePlan,
    NavigationVoxelScoringPolicy,
    load_cached_navigation_voxel_volume,
)
from caveviewer.core.navigation.voxel_graph_3d import (
    NavigationVoxel3DEdge,
    NavigationVoxel3DGraph,
    NavigationVoxel3DMetric,
    VoxelGraphKey,
    build_navigation_voxel_3d_graph,
)


DEFAULT_AUTO_DIVE_RENDER_DISTANCE_CELLS = 4
DEFAULT_AUTO_DIVE_CLOSED_LOOP_GAP_FRACTION = 0.15
DEFAULT_AUTO_DIVE_MAX_KEYFRAMES = 512
DEFAULT_AUTO_DIVE_SPEED_M_PER_SECOND = (
    DEFAULT_CENTERLINE_ROUTE_SPEED_M_PER_SECOND * 2.25
)
DEFAULT_AUTO_DIVE_VERTICAL_POSITION_FRACTION = 0.35
DEFAULT_AUTO_DIVE_SMOOTHING_RADIUS_CELLS = (
    NAVIGATION_ROUTE_Y_SMOOTHING_RADIUS_CELLS
)
DEFAULT_AUTO_DIVE_LOOKAHEAD_DISTANCE_M = 8.0
DEFAULT_AUTO_DIVE_LOCAL_REFINEMENT_ENABLED = True
# Runtime replans must not monopolize a consumer machine. Initial route
# planning remains uncapped; the replanner supplies this budget explicitly.
DEFAULT_AUTO_DIVE_REPLAN_PLANNING_BUDGET_S = 8.0
AUTO_DIVE_RUNTIME_METHOD = "candidate_mesh_recovery_v1"
DEFAULT_AUTO_DIVE_TRUSTED_MAX_SEGMENT_CELLS = 6.0
# Keep the camera well back from a cached-mesh boundary. A one-metre pullback
# still leaves the camera visibly inside tight passages on larger maps.
DEFAULT_AUTO_DIVE_MESH_BOUNDARY_PULLBACK_M = 2.0
_AUTO_DIVE_MESH_RECOVERY_SCAN_YAW_OFFSETS_DEG = tuple(range(-120, 121, 15))
_AUTO_DIVE_MESH_RECOVERY_SCAN_PITCH_OFFSETS_DEG = (
    -45.0,
    -30.0,
    -15.0,
    0.0,
    15.0,
    30.0,
    45.0,
)
_AUTO_DIVE_MESH_RECOVERY_SCAN_CONE_ALIGNMENT = math.cos(math.radians(22.5))
# A 90-degree cone admitted exactly sideways recovery targets. Those targets
# can then turn toward the entrance even though their first dot product was
# not negative. Keep a modest turn allowance, but require meaningful forward
# intent; ambiguous geometry should ask for assistance rather than backtrack.
_AUTO_DIVE_FORWARD_TRAVEL_CONE_DEGREES = 75.0
_AUTO_DIVE_FORWARD_TRAVEL_CONE_ALIGNMENT = math.cos(
    math.radians(_AUTO_DIVE_FORWARD_TRAVEL_CONE_DEGREES)
)
# The mesh-recovery search may explore a ninety-degree turn so it can find a
# valid passage around an obstacle. Final route acceptance remains stricter;
# otherwise an exactly sideways recovery target can be mistaken for progress.
_AUTO_DIVE_MESH_RECOVERY_TARGET_CONE_DEGREES = 90.0
_AUTO_DIVE_MESH_RECOVERY_TARGET_ALIGNMENT = math.cos(
    math.radians(_AUTO_DIVE_MESH_RECOVERY_TARGET_CONE_DEGREES)
)
_AUTO_DIVE_MESH_RECOVERY_FORWARD_ALIGNMENT = 0.0
_AUTO_DIVE_MESH_RECOVERY_TURN_PENALTY_CELLS = 5.0
_AUTO_DIVE_MESH_RECOVERY_SELECTION_TURN_PENALTY_CELLS = 1.0
_AUTO_DIVE_MESH_RECOVERY_SELECTION_TURN_PENALTY_MAX_FRACTION = 0.35
_AUTO_DIVE_MESH_RECOVERY_PATH_AVOIDANCE_RADIUS_CELLS = 1
# Mesh recovery is deliberately bounded. The collision guard loads only
# chunks intersecting tested edges, so the search budget is a better safety
# valve than disabling recovery from the map's total triangle count.
_AUTO_DIVE_MESH_RECOVERY_MAX_VISITED_CELLS = 4096
_AUTO_DIVE_MESH_RECOVERY_MAX_EDGE_TESTS = 16384
# The runtime hemisphere scan is deliberately coarse and staged. The first
# pass is cheap voxel/footprint filtering; only the best probes reach exact
# mesh collision checks. This covers all forward directions without turning a
# consumer machine into a 3D ray-tracing worker.
_AUTO_DIVE_HEMISPHERE_DIRECTION_COUNT = 32
_AUTO_DIVE_HEMISPHERE_ROLL_COUNT = 4
_AUTO_DIVE_HEMISPHERE_MAX_EXACT_CANDIDATES = 8
_AUTO_DIVE_HEMISPHERE_MAX_DIAGNOSTIC_CANDIDATES = 16
_AUTO_DIVE_HEMISPHERE_PROBE_DISTANCE_CELLS = 8.0
_AUTO_DIVE_HEMISPHERE_MIN_COVERAGE = 0.70
_AUTO_DIVE_HEMISPHERE_MIN_TARGET_ALIGNMENT = 0.0
_AUTO_DIVE_HEMISPHERE_PROGRESS_TOLERANCE_CELLS = 1.0

AutoDiveDiagnosticSink = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True)
class AutoDiveSettings:
    """Configuration for user-facing centerline Guided Dive planning."""

    render_distance_cells: int = DEFAULT_AUTO_DIVE_RENDER_DISTANCE_CELLS
    speed_m_per_second: float = DEFAULT_AUTO_DIVE_SPEED_M_PER_SECOND
    y_search_radius_cells: int = DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS
    vertical_position_fraction: float = DEFAULT_AUTO_DIVE_VERTICAL_POSITION_FRACTION
    closed_loop_gap_fraction: float = DEFAULT_AUTO_DIVE_CLOSED_LOOP_GAP_FRACTION
    max_keyframes: int = DEFAULT_AUTO_DIVE_MAX_KEYFRAMES
    keyframe_spacing_m: float | None = None
    smoothing_radius_cells: int = DEFAULT_AUTO_DIVE_SMOOTHING_RADIUS_CELLS
    lookahead_distance_m: float = DEFAULT_AUTO_DIVE_LOOKAHEAD_DISTANCE_M
    voxel_analysis_enabled: bool = True
    voxel_size_m: float = DEFAULT_VOXEL_SIZE_M
    voxel_curvature_rank_threshold: int = (
        DEFAULT_VOXEL_CURVATURE_RANK_THRESHOLD
    )
    voxel_max_regions: int = DEFAULT_VOXEL_MAX_REGIONS
    voxel_max_distance_m: float = DEFAULT_VOXEL_MAX_DISTANCE_M
    voxel_max_cells: int = DEFAULT_VOXEL_MAX_CELLS
    voxel_local_refinement_enabled: bool = DEFAULT_AUTO_DIVE_LOCAL_REFINEMENT_ENABLED
    voxel_local_refinement_radius_m: float = (
        DEFAULT_VOXEL_LOCAL_REFINEMENT_RADIUS_M
    )
    voxel_local_refinement_forward_m: float = (
        DEFAULT_VOXEL_LOCAL_REFINEMENT_FORWARD_M
    )
    voxel_local_refinement_max_cells: int = (
        DEFAULT_VOXEL_LOCAL_REFINEMENT_MAX_CELLS
    )
    # None is intentional for the initial route. AutoDiveReplanner replaces
    # this value on each runtime replan with its bounded planning budget.
    planning_budget_s: float | None = None
    # Graph-native clearance is expressed in metres. Zero keeps occupancy and
    # exact mesh collision as hard gates while allowing a map-specific vehicle
    # envelope to opt into a larger explicit margin.
    minimum_graph_clearance_m: float = 0.0
    # The policy is request-scoped so experiments can change route priorities
    # without changing the prepared graph or the navigation-thread contract.
    voxel_scoring_policy: NavigationVoxelScoringPolicy = field(
        default_factory=NavigationVoxelScoringPolicy
    )


class AutoDivePlanningBudgetExceeded(NavigationConfigurationError):
    """Raised when a runtime route plan exceeds its cooperative time budget."""

    def __init__(self, *, budget_s: float, elapsed_s: float, phase: str) -> None:
        self.budget_s = float(budget_s)
        self.elapsed_s = float(elapsed_s)
        self.phase = str(phase)
        self.reason = "planning_budget_exceeded"
        super().__init__(
            "Guided Dive planning budget exceeded during "
            f"{self.phase} ({self.elapsed_s:.3f}s >= {self.budget_s:.3f}s)"
        )


@dataclass(frozen=True)
class _AutoDivePlanningBudget:
    """Cooperative deadline shared by one runtime planning operation."""

    started_at: float
    budget_s: float | None

    @classmethod
    def from_settings(cls, settings: AutoDiveSettings) -> "_AutoDivePlanningBudget":
        budget = settings.planning_budget_s
        return cls(
            started_at=time.perf_counter(),
            budget_s=None if budget is None else float(budget),
        )

    def check(
        self,
        phase: str,
        *,
        diagnostics: AutoDiveDiagnosticSink | None = None,
    ) -> None:
        if self.budget_s is None:
            return
        elapsed_s = max(0.0, time.perf_counter() - self.started_at)
        if elapsed_s < self.budget_s:
            return
        _record_auto_dive_diagnostic(
            diagnostics,
            "planning_budget_exceeded",
            {
                "budget_s": float(self.budget_s),
                "elapsed_s": float(elapsed_s),
                "phase": str(phase),
            },
        )
        raise AutoDivePlanningBudgetExceeded(
            budget_s=float(self.budget_s),
            elapsed_s=float(elapsed_s),
            phase=str(phase),
        )

    @property
    def deadline_monotonic_s(self) -> float | None:
        """Return the absolute deadline used by cooperative inner searches."""
        if self.budget_s is None:
            return None
        return float(self.started_at + self.budget_s)

    @property
    def remaining_s(self) -> float | None:
        """Return the remaining planning budget without raising."""
        deadline = self.deadline_monotonic_s
        if deadline is None:
            return None
        return max(0.0, deadline - time.perf_counter())


@dataclass(frozen=True)
class _AutoDiveRouteSamples:
    cells: tuple[FootprintCell, ...]
    points: tuple[Point, ...]


@dataclass(frozen=True)
class _AutoDiveCandidateSpec:
    name: str
    smoothing_radius_cells: int
    use_theta: bool
    use_cone: bool
    use_weighted_smoothing: bool
    use_bspline: bool
    use_repulsion: bool = False


@dataclass(frozen=True)
class _AutoDiveRouteCandidate:
    ordinal: int
    name: str
    cells: tuple[FootprintCell, ...]
    points: tuple[Point, ...]
    roll_deg: float = 0.0


@dataclass(frozen=True)
class _AutoDiveSelectedRoute:
    points: tuple[Point, ...]
    selection_reason: str
    route_truncated_by_mesh: bool = False
    mesh_safe_prefix_length_m: float | None = None
    replan_at_end: bool = False
    roll_deg: float = 0.0
    terminal_reached: bool = False


@dataclass(frozen=True)
class _ConeChainCandidate:
    cells: tuple[FootprintCell, ...]
    anchor_index: int
    cost: float


@dataclass(frozen=True)
class _HemisphereProbeEvaluation:
    """Cheap evidence retained before exact mesh checks."""

    probe: HemisphereProbe
    points: tuple[Point, ...]
    target_cell: FootprintCell
    target_alignment: float
    progress_gain_m: float
    target_volume_m3: float
    target_clearance_m: float
    continuation_count: int
    voxel_coverage_fraction: float
    voxel_free_fraction: float
    voxel_mean_clearance_m: float


@dataclass(frozen=True)
class _AutoDiveClearanceFailure:
    kind: str
    reason: str
    index: int | None = None
    segment_index: int | None = None
    cell: FootprintCell | None = None
    chunk_cell: tuple[int, int, int] | None = None
    point: Point | None = None
    first: Point | None = None
    second: Point | None = None


@dataclass(frozen=True)
class _AutoDiveRouteCandidateScore:
    route_clear: bool
    entry_clear: bool
    mesh_clear: bool
    geometry_trusted: bool
    min_lateral_clearance_cells: int
    mean_lateral_clearance_cells: float
    min_clearance_margin_m: float
    max_segment_length_m: float
    max_segment_cells: float
    forward_progress_m: float
    pullback_penalty_m: float
    curvature_rad: float
    vertical_jerk_m: float
    curvature_rad_per_m: float
    vertical_jerk_m_per_m: float
    total_change_per_m: float
    length_m: float
    point_count: int
    first_clearance_failure: _AutoDiveClearanceFailure | None

    @property
    def sort_key(self) -> tuple[object, ...]:
        return (
            bool(self.route_clear),
            bool(self.entry_clear),
            bool(self.mesh_clear),
            bool(self.geometry_trusted),
            int(self.min_lateral_clearance_cells),
            float(self.min_clearance_margin_m),
            float(self.mean_lateral_clearance_cells),
            -float(self.max_segment_cells),
            -float(self.total_change_per_m),
            float(self.forward_progress_m),
            -float(self.pullback_penalty_m),
            -float(self.curvature_rad_per_m),
            -float(self.vertical_jerk_m_per_m),
            -float(self.length_m),
            -int(self.point_count),
        )


@dataclass(frozen=True)
class _AutoDiveCollisionValidator:
    """Route collision seam for Guided Dive path candidates.

    This is deliberately small and runtime-only. It validates against the
    cached navigation footprint, cached vertical gap ranges when available,
    and optional cached chunk mesh collision checks.
    """

    centerline_path: CenterlinePath
    mesh_guard: CachedChunkMeshCollisionGuard | None = None
    voxel_volume: LocalVoxelVolume | NavigationVoxelAtlas | None = None
    voxel_builder: Callable[[], LocalVoxelVolume | None] | None = None
    voxel_refinement: LocalVoxelVolume | None = None
    voxel_refinement_builder: Callable[[], LocalVoxelVolume | None] | None = None
    allow_native_graph_transitions: bool = False

    @property
    def cell_size(self) -> float:
        return self.centerline_path.footprint_cell_size

    @property
    def component_cells(self) -> frozenset[FootprintCell]:
        return self.centerline_path.component_cells

    @property
    def cached_y_ranges(self) -> Mapping[FootprintCell, tuple[float, float]]:
        return getattr(self.centerline_path, "cached_y_ranges", None) or {}

    @property
    def cached_clearance_margins(self) -> Mapping[FootprintCell, float]:
        return getattr(self.centerline_path, "cached_clearance_margins", None) or {}

    @property
    def has_route_clearance_metadata(self) -> bool:
        return bool(self.cached_clearance_margins)

    @property
    def has_mesh_collision_guard(self) -> bool:
        return self.mesh_guard is not None

    @property
    def active_voxel_volume(self) -> LocalVoxelVolume | NavigationVoxelAtlas | None:
        """Return the finest available field for local collision queries."""
        return self.voxel_refinement or self.voxel_volume

    def probe_voxel_point(
        self,
        point: Point,
        *,
        include_clearance: bool = True,
    ) -> tuple[bool, float] | None:
        """Query fine refinement first, then the cached whole-cave atlas."""
        for volume in (self.voxel_refinement, self.voxel_volume):
            if volume is None:
                continue
            probe = getattr(volume, "probe_point", None)
            if not callable(probe):
                continue
            result = probe(point, include_clearance=include_clearance)
            if result is not None:
                return result
        return None

    def probe_fine_point(
        self,
        point: Point,
        *,
        include_clearance: bool = True,
    ) -> tuple[bool, float] | None:
        """Query only the fine refinement layer, if one is available."""
        if self.voxel_refinement is not None:
            return self.voxel_refinement.probe_point(
                point,
                include_clearance=include_clearance,
            )
        volume = self.voxel_volume
        probe_fine = getattr(volume, "probe_fine_point", None)
        if callable(probe_fine):
            return probe_fine(
                point,
                include_clearance=include_clearance,
            )
        return None

    def point_is_clear(self, point: Point) -> bool:
        return self.point_clearance_failure(point) is None

    def point_clearance_failure(
        self,
        point: Point,
        *,
        index: int | None = None,
        segment_index: int | None = None,
        kind: str = "point",
        enforce_lateral_clearance: bool = True,
    ) -> _AutoDiveClearanceFailure | None:
        fine_result = self.probe_fine_point(
            point,
            include_clearance=False,
        )
        if fine_result is not None and not fine_result[0]:
            return _AutoDiveClearanceFailure(
                kind=kind,
                reason="voxel_blocked",
                index=index,
                segment_index=segment_index,
                point=point,
            )
        # A covered fine voxel is the authoritative local filter. It may
        # legitimately sit outside the coarse centerline footprint, which is
        # exactly the case this refinement exists to recover.
        if fine_result is not None and fine_result[0]:
            if not self.has_route_clearance_metadata or not enforce_lateral_clearance:
                return None
            lateral_clearance = _lateral_clearance_score_at_point(
                point,
                centerline_path=self.centerline_path,
            )
            if lateral_clearance < _minimum_required_lateral_clearance_score(
                self.centerline_path
            ):
                return _AutoDiveClearanceFailure(
                    kind=kind,
                    reason="low_lateral_clearance",
                    index=index,
                    segment_index=segment_index,
                    point=point,
                )
            return None
        cell = _footprint_cell_for_xz((point[0], point[2]), self.cell_size)
        if cell not in self.component_cells:
            return _AutoDiveClearanceFailure(
                kind=kind,
                reason="outside_footprint",
                index=index,
                segment_index=segment_index,
                cell=cell,
                point=point,
            )
        y_range = self.cached_y_ranges.get(cell)
        if y_range is not None:
            tolerance = max(1e-6, self.cell_size * 0.01)
            if not y_range[0] - tolerance <= point[1] <= y_range[1] + tolerance:
                return _AutoDiveClearanceFailure(
                    kind=kind,
                    reason="outside_y_range",
                    index=index,
                    segment_index=segment_index,
                    cell=cell,
                    point=point,
                )
        if not self.has_route_clearance_metadata or not enforce_lateral_clearance:
            return None
        lateral_clearance = _lateral_clearance_score_at_point(
            point,
            centerline_path=self.centerline_path,
        )
        if lateral_clearance < _minimum_required_lateral_clearance_score(
            self.centerline_path
        ):
            return _AutoDiveClearanceFailure(
                kind=kind,
                reason="low_lateral_clearance",
                index=index,
                segment_index=segment_index,
                cell=cell,
                point=point,
            )
        return None

    def segment_is_clear(self, first: Point, second: Point) -> bool:
        return self.segment_clearance_failure(first, second) is None

    def segment_clearance_failure(
        self,
        first: Point,
        second: Point,
        *,
        segment_index: int | None = None,
        allow_low_lateral_clearance: bool = False,
    ) -> _AutoDiveClearanceFailure | None:
        return _route_segment_clearance_failure(
            first,
            second,
            collision_validator=self,
            segment_index=segment_index,
            allow_low_lateral_clearance=allow_low_lateral_clearance,
        )

    def route_is_clear(self, route_points: tuple[Point, ...]) -> bool:
        return self.route_clearance_failure(route_points) is None

    def route_clearance_failure(
        self,
        route_points: tuple[Point, ...],
        *,
        allow_low_lateral_clearance: bool = False,
    ) -> _AutoDiveClearanceFailure | None:
        if not route_points:
            return _AutoDiveClearanceFailure(
                kind="route",
                reason="empty_route",
            )
        for index, point in enumerate(route_points):
            failure = self.point_clearance_failure(
                point,
                index=index,
                enforce_lateral_clearance=not allow_low_lateral_clearance,
            )
            if failure is not None:
                return failure
        for segment_index, (first, second) in enumerate(
            zip(route_points, route_points[1:], strict=False)
        ):
            failure = self.segment_clearance_failure(
                first,
                second,
                segment_index=segment_index,
                allow_low_lateral_clearance=allow_low_lateral_clearance,
            )
            if failure is not None:
                return failure
        return None

    def mesh_route_clearance_failure(
        self,
        route_points: tuple[Point, ...],
        *,
        planning_budget: _AutoDivePlanningBudget | None = None,
        diagnostics: AutoDiveDiagnosticSink | None = None,
    ) -> _AutoDiveClearanceFailure | None:
        if self.mesh_guard is None or len(route_points) < 2:
            return None
        for segment_index, (first, second) in enumerate(
            zip(route_points, route_points[1:], strict=False)
        ):
            if planning_budget is not None:
                planning_budget.check(
                    "candidate_mesh_segment",
                    diagnostics=diagnostics,
                )
            failure = _route_segment_mesh_collision_failure(
                first,
                second,
                collision_validator=self,
                segment_index=segment_index,
            )
            if failure is not None:
                return failure
        return None


@dataclass(frozen=True)
class AutoDivePlan:
    """Finite Guided Dive route with graph-native navigation evidence."""

    route: CameraRoute
    # Optional compatibility descriptor for the legacy centerline planner.
    # Graph-native production plans do not load or depend on it.
    centerline_path: CenterlinePath | None
    route_points: tuple[Point, ...]
    route_cells: tuple[FootprintCell, ...]
    circular_arc: bool
    route_length_m: float
    duration_s: float
    render_distance_cells: int
    selection_reason: str = ""
    route_truncated_by_mesh: bool = False
    mesh_safe_prefix_length_m: float | None = None
    replan_at_end: bool = False
    voxel_route_selection: Mapping[str, Any] | None = None
    terminal_reached: bool = False
    navigation_route_id: str | None = None
    preflight_validated: bool = False
    navigation_atlas: NavigationVoxelAtlas | None = None
    navigation_graph: NavigationVoxel3DGraph | None = None
    # Keep the exact graph route alongside the published camera points.  The
    # certificate and diagnostics use this to validate the same graph path
    # that the runtime planner handed to the controller.
    navigation_graph_keys: tuple[VoxelGraphKey, ...] = ()

    @property
    def route_points_xz(self) -> tuple[PointXZ, ...]:
        """Return route points projected into minimap X/Z space."""
        return tuple((point[0], point[2]) for point in self.route_points)


def auto_dive_plan_navigation_cell_size(plan: AutoDivePlan) -> float:
    """Return graph scale for GUI/replanner distance calculations."""
    graph = getattr(plan, "navigation_graph", None)
    if graph is not None:
        values = [
            float(value)
            for value in getattr(graph, "grid_size_m", ())
            if math.isfinite(float(value)) and float(value) > 0.0
        ]
        if values:
            return max(values)
    atlas = getattr(plan, "navigation_atlas", None)
    atlas_size = getattr(atlas, "voxel_size_m", None)
    try:
        if atlas_size is not None and math.isfinite(float(atlas_size)) and float(atlas_size) > 0.0:
            return float(atlas_size)
    except (TypeError, ValueError):
        pass
    centerline = getattr(plan, "centerline_path", None)
    try:
        centerline_size = float(getattr(centerline, "footprint_cell_size", 1.0))
    except (TypeError, ValueError):
        centerline_size = 1.0
    return max(1.0, centerline_size)


class NavigationVoxelGraphAuthorityError(NavigationConfigurationError):
    """Raised when runtime Guided Dive lacks an authoritative voxel graph."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        status: Mapping[str, Any],
    ) -> None:
        self.reason = str(reason)
        self.status = dict(status)
        super().__init__(message)


def _authoritative_navigation_voxel_context(
    manifest: Mapping[str, Any],
    *,
    cache_dir: str | os.PathLike[str] | None,
    settings: AutoDiveSettings,
    diagnostics: AutoDiveDiagnosticSink | None,
    route_id: str | None = None,
) -> tuple[CenterlinePath, NavigationVoxelAtlas, dict[str, Any]]:
    """Load the graph-only runtime navigation context.

    The centerline metadata is retained here only as a geometry/component
    descriptor for collision checks. It is never used as a route source when
    this context is requested.
    """
    navigation = manifest.get(NAVIGATION_METADATA_KEY)
    descriptor = (
        navigation.get("voxel_cache")
        if isinstance(navigation, Mapping)
        else None
    )
    routes = (
        navigation.get("routes")
        if isinstance(navigation, Mapping)
        else None
    )
    selected_route_id: str | None = None
    if isinstance(route_id, str) and route_id:
        selected_route_id = route_id
    if selected_route_id is None and isinstance(navigation, Mapping):
        candidate = navigation.get("recommended_route_id")
        if isinstance(candidate, str) and candidate:
            selected_route_id = candidate
    if selected_route_id is None and isinstance(routes, Sequence) and not isinstance(
        routes,
        (str, bytes),
    ):
        for route in routes:
            if not isinstance(route, Mapping):
                continue
            candidate = route.get("id")
            if isinstance(candidate, str) and candidate:
                selected_route_id = candidate
                break

    status: dict[str, Any] = {
        "authority": "prepared_true_3d_voxel_graph",
        "required": True,
        "available": False,
        "reason": None,
        "cache_directory_present": bool(cache_dir),
        "cache_declared": isinstance(descriptor, Mapping),
        "cache_version": (
            None
            if not isinstance(descriptor, Mapping)
            else descriptor.get("version")
        ),
        "cache_method": (
            None
            if not isinstance(descriptor, Mapping)
            else descriptor.get("method")
        ),
        "cache_path": (
            None
            if not isinstance(descriptor, Mapping)
            else descriptor.get("path")
        ),
        "route_id": selected_route_id,
    }

    def reject(reason: str, message: str) -> NoReturn:
        status["reason"] = str(reason)
        status["message"] = str(message)
        _record_auto_dive_diagnostic(
            diagnostics,
            "navigation_authority",
            status,
        )
        raise NavigationVoxelGraphAuthorityError(
            message,
            reason=reason,
            status=status,
        )

    if not bool(settings.voxel_analysis_enabled):
        reject(
            "voxel_analysis_disabled",
            "Guided Dive requires voxel graph navigation, but voxel analysis "
            "is disabled",
        )
    if not cache_dir:
        reject(
            "cache_directory_missing",
            "Guided Dive requires a navigation voxel cache directory",
        )
    if not isinstance(navigation, Mapping):
        reject(
            "navigation_metadata_missing",
            "Guided Dive requires cached navigation metadata",
        )
    if not isinstance(descriptor, Mapping):
        reject(
            "voxel_cache_not_declared",
            "Guided Dive requires a declared navigation voxel graph cache",
        )
    if (
        descriptor.get("version") != NAVIGATION_VOXEL_CACHE_VERSION
        or descriptor.get("method") != NAVIGATION_VOXEL_CACHE_METHOD
    ):
        reject(
            "stale_or_unsupported_cache",
            "Guided Dive requires the current prepared voxel graph cache; "
            "rebuild the navigation cache",
        )
    if descriptor.get("path") != NAVIGATION_VOXEL_CACHE_NAME:
        reject(
            "voxel_cache_path_invalid",
            "Guided Dive navigation voxel cache path is invalid; rebuild the cache",
        )
    if selected_route_id is None:
        reject(
            "navigation_route_missing",
            "Guided Dive requires a selected cached navigation route",
        )

    centerline_path = cached_centerline_path(
        manifest,
        route_id=selected_route_id,
        cache_dir=os.fspath(cache_dir),
    )
    if centerline_path is None:
        reject(
            "navigation_route_metadata_invalid",
            "Guided Dive cached navigation route metadata is invalid",
        )
    cached_volume = getattr(centerline_path, "cached_voxel_volume", None)
    if not isinstance(cached_volume, NavigationVoxelAtlas):
        reject(
            "voxel_graph_model_missing",
            "Guided Dive could not load the prepared voxel graph; rebuild the cache",
        )
    graph = cached_volume.prepared_3d_graph
    if not cached_volume.has_prepared_3d_graph or graph is None:
        reject(
            "true_3d_graph_missing",
            "Guided Dive cache does not contain a prepared true-3D voxel graph",
        )
    status.update(
        {
            "graph_method": str(graph.method),
            "graph_node_count": len(graph.nodes),
            "graph_edge_count": int(graph.edge_count),
            "graph_routable_node_count": int(graph.routable_node_count),
            "graph_component_count": int(graph.component_count),
            "graph_edge_integrity_safe": bool(graph.edge_integrity_safe),
            "motion_geometry_safe": bool(
                cached_volume.prepared_3d_motion_geometry_safe
            ),
        }
    )
    if str(graph.method) != NAVIGATION_VOXEL_GRAPH_METHOD:
        reject(
            "true_3d_graph_method_unsupported",
            "Guided Dive cache contains an unsupported true-3D voxel graph",
        )
    if not cached_volume.prepared_3d_motion_geometry_safe:
        reject(
            "true_3d_graph_geometry_unsafe",
            "Guided Dive true-3D voxel graph geometry is too coarse for motion; "
            "rebuild the navigation cache",
        )
    if len(graph.nodes) <= 0:
        reject(
            "true_3d_graph_empty",
            "Guided Dive cache contains an empty true-3D voxel graph",
        )
    if not graph.edge_integrity_safe:
        reject(
            "true_3d_graph_edge_integrity_invalid",
            "Guided Dive cache contains an invalid true-3D voxel graph edge set; "
            "rebuild the navigation cache",
        )
    if graph.edge_count <= 0 or graph.routable_node_count <= 0:
        reject(
            "true_3d_graph_has_no_routes",
            "Guided Dive cache contains voxel density but no prepared graph routes; "
            "rebuild the navigation cache",
        )

    status.update(
        {
            "available": True,
            "reason": "ready",
            "source": "cached_navigation_voxel_graph",
        }
    )
    _record_auto_dive_diagnostic(
        diagnostics,
        "navigation_authority",
        status,
    )
    return centerline_path, cached_volume, status


def _authoritative_graph_navigation_context(
    manifest: Mapping[str, Any],
    *,
    cache_dir: str | os.PathLike[str] | None,
    settings: AutoDiveSettings,
    diagnostics: AutoDiveDiagnosticSink | None,
    route_id: str | None = None,
) -> tuple[NavigationVoxelAtlas, dict[str, Any]]:
    """Load the production graph context without constructing a centerline.

    Cache metadata still identifies which route-specific sidecar to load, but
    the returned navigation object is the atlas and prepared graph themselves.
    This is the boundary used by preflight, runtime replanning, and continuous
    scanning; the compatibility centerline planner is intentionally bypassed.
    """
    navigation = manifest.get(NAVIGATION_METADATA_KEY)
    descriptor = (
        navigation.get("voxel_cache")
        if isinstance(navigation, Mapping)
        else None
    )
    selected_route_id: str | None = (
        str(route_id) if isinstance(route_id, str) and route_id else None
    )
    if selected_route_id is None and isinstance(navigation, Mapping):
        candidate = navigation.get("recommended_route_id")
        if isinstance(candidate, str) and candidate:
            selected_route_id = candidate
    if selected_route_id is None:
        routes = navigation.get("routes") if isinstance(navigation, Mapping) else None
        if isinstance(routes, Sequence) and not isinstance(routes, (str, bytes)):
            for route in routes:
                if not isinstance(route, Mapping):
                    continue
                candidate = route.get("id")
                if isinstance(candidate, str) and candidate:
                    selected_route_id = candidate
                    break

    status: dict[str, Any] = {
        "authority": "prepared_true_3d_voxel_graph",
        "required": True,
        "available": False,
        "reason": None,
        "cache_directory_present": bool(cache_dir),
        "cache_declared": isinstance(descriptor, Mapping),
        "cache_version": (
            None if not isinstance(descriptor, Mapping) else descriptor.get("version")
        ),
        "cache_method": (
            None if not isinstance(descriptor, Mapping) else descriptor.get("method")
        ),
        "cache_path": (
            None if not isinstance(descriptor, Mapping) else descriptor.get("path")
        ),
        "route_id": selected_route_id,
        "source": "cached_navigation_voxel_graph",
    }

    def reject(reason: str, message: str) -> NoReturn:
        status["reason"] = str(reason)
        status["message"] = str(message)
        _record_auto_dive_diagnostic(diagnostics, "navigation_authority", status)
        raise NavigationVoxelGraphAuthorityError(
            message,
            reason=reason,
            status=status,
        )

    if not bool(settings.voxel_analysis_enabled):
        reject(
            "voxel_analysis_disabled",
            "Guided Dive requires voxel graph navigation, but voxel analysis is disabled",
        )
    if not cache_dir:
        reject(
            "cache_directory_missing",
            "Guided Dive requires a navigation voxel cache directory",
        )
    if not isinstance(navigation, Mapping):
        reject("navigation_metadata_missing", "Guided Dive requires cached navigation metadata")
    if not isinstance(descriptor, Mapping):
        reject("voxel_cache_not_declared", "Guided Dive requires a declared navigation voxel graph cache")
    if (
        descriptor.get("version") != NAVIGATION_VOXEL_CACHE_VERSION
        or descriptor.get("method") != NAVIGATION_VOXEL_CACHE_METHOD
    ):
        reject(
            "stale_or_unsupported_cache",
            "Guided Dive requires the current prepared voxel graph cache; rebuild the navigation cache",
        )
    if descriptor.get("path") != NAVIGATION_VOXEL_CACHE_NAME:
        reject(
            "voxel_cache_path_invalid",
            "Guided Dive navigation voxel cache path is invalid; rebuild the cache",
        )
    if selected_route_id is None:
        reject(
            "navigation_route_missing",
            "Guided Dive requires a selected cached navigation route",
        )

    cached_volume = load_cached_navigation_voxel_volume(
        cache_dir,
        manifest,
        selected_route_id,
    )
    if not isinstance(cached_volume, NavigationVoxelAtlas):
        reject(
            "voxel_graph_model_missing",
            "Guided Dive could not load the prepared voxel graph; rebuild the cache",
        )
    graph = cached_volume.prepared_3d_graph
    if not cached_volume.has_prepared_3d_graph or graph is None:
        reject(
            "true_3d_graph_missing",
            "Guided Dive cache does not contain a prepared true-3D voxel graph",
        )
    status.update(
        {
            "graph_method": str(graph.method),
            "graph_node_count": len(graph.nodes),
            "graph_edge_count": int(graph.edge_count),
            "graph_routable_node_count": int(graph.routable_node_count),
            "graph_component_count": int(graph.component_count),
            "graph_edge_integrity_safe": bool(graph.edge_integrity_safe),
            "motion_geometry_safe": bool(cached_volume.prepared_3d_motion_geometry_safe),
        }
    )
    if str(graph.method) != NAVIGATION_VOXEL_GRAPH_METHOD:
        reject(
            "true_3d_graph_method_unsupported",
            "Guided Dive cache contains an unsupported true-3D voxel graph",
        )
    if not cached_volume.prepared_3d_motion_geometry_safe:
        reject(
            "true_3d_graph_geometry_unsafe",
            "Guided Dive true-3D voxel graph geometry is too coarse for motion; rebuild the navigation cache",
        )
    if len(graph.nodes) <= 0:
        reject("true_3d_graph_empty", "Guided Dive cache contains an empty true-3D voxel graph")
    if not graph.edge_integrity_safe:
        reject(
            "true_3d_graph_edge_integrity_invalid",
            "Guided Dive cache contains an invalid true-3D voxel graph edge set; rebuild the cache",
        )
    if graph.edge_count <= 0 or graph.routable_node_count <= 0:
        reject(
            "true_3d_graph_has_no_routes",
            "Guided Dive cache contains voxel density but no prepared graph routes; rebuild the cache",
        )
    status.update({"available": True, "reason": "ready"})
    _record_auto_dive_diagnostic(diagnostics, "navigation_authority", status)
    return cached_volume, status


def build_auto_dive_initial_camera_pose(
    manifest: Mapping[str, Any],
    *,
    settings: AutoDiveSettings | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    require_voxel_graph: bool = False,
) -> RouteKeyframe:
    """Return a safe endpoint camera pose for a newly loaded map.

    Viewer startup historically used the first manifest chunk center. On maps
    imported in phases, that can place the camera in the middle of a passage
    or close to a cave face, which makes the first Guided Dive replan fight its
    way out of a bad local pose. This helper chooses one endpoint of the
    selected centerline route, then returns the first route keyframe looking
    down the clearest available initial segment.
    """
    settings = settings or AutoDiveSettings()
    _validate_auto_dive_settings(settings)
    if require_voxel_graph:
        return _build_graph_initial_camera_pose(
            manifest,
            settings=settings,
            cache_dir=cache_dir,
        )
    centerline_path = cached_centerline_path(manifest, cache_dir=cache_dir)
    if centerline_path is None and require_voxel_graph:
        raise NavigationConfigurationError(
            "Guided Dive initial camera requires a prepared voxel graph cache"
        )
    if centerline_path is None:
        centerline_path = generate_centerline_path(
            manifest,
            component_selection=CENTERLINE_COMPONENT_SELECTION_LONGEST_PATH,
        )
    position_groups = _auto_dive_initial_camera_position_groups(
        centerline_path,
        manifest=manifest,
        settings=settings,
    )
    if not position_groups:
        raise NavigationConfigurationError(
            "Guided Dive initial camera requires a centerline endpoint"
        )

    for positions in position_groups:
        plans: list[AutoDivePlan] = []
        for position in positions:
            try:
                plans.append(
                    build_centerline_auto_dive_plan(
                        manifest,
                        current_position=position,
                        settings=settings,
                        cache_dir=cache_dir,
                        require_voxel_graph=require_voxel_graph,
                    )
                )
            except NavigationConfigurationError:
                continue
        if plans:
            best_plan = max(plans, key=_auto_dive_initial_plan_score)
            return best_plan.route.keyframes[0]

    raise NavigationConfigurationError(
        "Guided Dive initial camera could not build an endpoint route"
    )


def _build_graph_initial_camera_pose(
    manifest: Mapping[str, Any],
    *,
    settings: AutoDiveSettings,
    cache_dir: str | os.PathLike[str] | None,
) -> RouteKeyframe:
    """Choose the initial pose from the graph entrance, not a centerline end."""
    atlas, _authority_status = _authoritative_graph_navigation_context(
        manifest,
        cache_dir=cache_dir,
        settings=settings,
        diagnostics=None,
    )
    graph = atlas.prepared_3d_graph
    if graph is None:
        raise NavigationConfigurationError("Guided Dive prepared graph is unavailable")
    navigation = manifest.get(NAVIGATION_METADATA_KEY)
    start_point = _navigation_start_point(navigation)
    if start_point is None:
        start_key = min(
            graph.nodes,
            key=lambda key: (
                float(graph.nodes[key].progress_m),
                key,
            ),
        )
    else:
        start_key, _distance_m = _preflight_nearest_graph_key(
            graph,
            start_point,
            routable_only=True,
        )
        if start_key is None:
            raise NavigationConfigurationError(
                "Guided Dive graph has no routable entrance node"
            )
    start_node = graph.nodes[start_key]
    target_edge = max(
        (
            edge
            for edge in graph.outgoing(start_key)
            if edge.line_of_sight and edge.target in graph.nodes
        ),
        key=lambda edge: (
            float(graph.nodes[edge.target].min_clearance_m),
            float(edge.distance_m),
            edge.target,
        ),
        default=None,
    )
    points = [tuple(float(value) for value in start_node.center)]
    if target_edge is not None:
        points.append(tuple(float(value) for value in graph.nodes[target_edge.target].center))
    route_points = tuple(points)
    keyframe_payloads = route_keyframes_for_points(
        route_points,
        duration_s=(
            path_length(route_points) / float(settings.speed_m_per_second)
            if len(route_points) > 1
            else 0.0
        ),
        lookahead_distance_m=max(0.0, float(settings.lookahead_distance_m)),
    )
    return RouteKeyframe.from_mapping(keyframe_payloads[0], index=0)


def _navigation_start_point(navigation: object) -> Point | None:
    if not isinstance(navigation, Mapping):
        return None
    value = navigation.get("navigation_start")
    if value is None:
        # Older manifests may not have the explicit navigation sidecar.  The
        # first point of the selected route is still a valid map-start hint
        # for initial camera placement; it is never used as the terminal or
        # as graph-route safety evidence.
        routes = navigation.get("routes")
        if isinstance(routes, Sequence) and not isinstance(routes, (str, bytes)):
            recommended_id = navigation.get("recommended_route_id")
            selected_route = None
            if isinstance(recommended_id, str) and recommended_id:
                selected_route = next(
                    (
                        route
                        for route in routes
                        if isinstance(route, Mapping)
                        and route.get("id") == recommended_id
                    ),
                    None,
                )
            if selected_route is None:
                selected_route = next(
                    (route for route in routes if isinstance(route, Mapping)),
                    None,
                )
            if isinstance(selected_route, Mapping):
                route_points = selected_route.get("points")
                if isinstance(route_points, Sequence) and not isinstance(
                    route_points,
                    (str, bytes),
                ):
                    if len(route_points) >= 3 and all(
                        isinstance(item, (int, float))
                        and not isinstance(item, bool)
                        for item in route_points[:3]
                    ):
                        value = route_points[:3]
                    elif route_points:
                        first_point = route_points[0]
                        if isinstance(first_point, Sequence) and not isinstance(
                            first_point,
                            (str, bytes),
                        ):
                            value = first_point
    if isinstance(value, Mapping):
        value = value.get("position", value.get("point"))
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


def build_centerline_auto_dive_plan(
    manifest: Mapping[str, Any],
    *,
    current_position: tuple[float, float, float] | np.ndarray,
    current_yaw: float | None = None,
    current_pitch: float | None = None,
    current_roll: float | None = None,
    current_travel_yaw: float | None = None,
    current_travel_pitch: float | None = None,
    avoid_positions: Sequence[Sequence[float]] | None = None,
    user_reposition: bool = False,
    force_hemisphere_scan: bool = False,
    settings: AutoDiveSettings | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    diagnostics: AutoDiveDiagnosticSink | None = None,
    require_voxel_graph: bool = False,
    route_id: str | None = None,
) -> AutoDivePlan:
    """Build a finite voxel-guided Guided Dive route near the current camera.

    Compatibility callers may still use the centerline-seeded mode. Production
    Guided Dive passes ``require_voxel_graph=True``; in that mode the prepared
    true-3D voxel graph is the only runtime route authority and missing or
    invalid graph data raises an explicit configuration error.
    """
    settings = settings or AutoDiveSettings()
    _validate_auto_dive_settings(settings)
    planning_budget = _AutoDivePlanningBudget.from_settings(settings)
    planning_budget.check("initialization", diagnostics=diagnostics)
    current = np.asarray(current_position, dtype=np.float64)
    if current.shape != (3,):
        raise NavigationConfigurationError("current_position must be a 3D point")

    authority_status: dict[str, Any] | None = None
    if require_voxel_graph:
        (
            centerline_path,
            cached_voxel_volume,
            authority_status,
        ) = _authoritative_navigation_voxel_context(
            manifest,
            cache_dir=cache_dir,
            settings=settings,
            diagnostics=diagnostics,
            route_id=route_id,
        )
    else:
        centerline_path = cached_centerline_path(
            manifest,
            route_id=route_id,
            cache_dir=cache_dir,
        )
        if centerline_path is None:
            centerline_path = generate_centerline_path(
                manifest,
                component_selection=CENTERLINE_COMPONENT_SELECTION_LONGEST_PATH,
            )
        cached_voxel_volume = (
            getattr(centerline_path, "cached_voxel_volume", None)
            if bool(settings.voxel_analysis_enabled)
            else None
        )
    planning_budget.check("centerline_loaded", diagnostics=diagnostics)
    if len(centerline_path.cells) < 2:
        raise NavigationConfigurationError("Guided Dive requires a multi-point centerline")

    # An explicit user displacement is the authoritative direction for a
    # user-resume replan. Camera view is only the fallback when the user did
    # not move far enough to establish a reliable displacement vector.
    direction_yaw = (
        current_travel_yaw
        if current_travel_yaw is not None
        else current_yaw
    )
    direction_pitch = (
        current_travel_pitch
        if current_travel_pitch is not None
        else current_pitch
    )
    if require_voxel_graph:
        route_cells = ()
        circular_arc = False
    else:
        nearest_index = _nearest_centerline_index(
            centerline_path,
            current_x=float(current[0]),
            current_z=float(current[2]),
        )
        route_cells, circular_arc = _select_auto_dive_cells(
            centerline_path,
            nearest_index=nearest_index,
            closed_loop_gap_fraction=settings.closed_loop_gap_fraction,
            current=current,
            current_yaw=direction_yaw,
            current_pitch=direction_pitch,
        )
    planning_budget.check("route_seed_selected", diagnostics=diagnostics)

    navigation_metadata = manifest.get(NAVIGATION_METADATA_KEY)
    cache_voxel_cache_declared = bool(
        isinstance(navigation_metadata, Mapping)
        and isinstance(navigation_metadata.get("voxel_cache"), Mapping)
    )
    voxel_route_active = False
    voxel_route_plan: NavigationVoxelRoutePlan | None = None
    voxel_route_world_points: tuple[Point, ...] = ()
    voxel_route_payload: dict[str, Any] = {
        "method": NAVIGATION_VOXEL_GRAPH_METHOD,
        "enabled": bool(settings.voxel_analysis_enabled),
        "cache_declared": cache_voxel_cache_declared,
        "selected": False,
        "authority_required": bool(require_voxel_graph),
        "authority": authority_status,
    }
    if isinstance(cached_voxel_volume, NavigationVoxelAtlas):
        voxel_route_payload["cache_graph_cell_count"] = int(
            cached_voxel_volume.navigation_cell_count
        )
        voxel_route_payload["prepared_3d_motion_geometry_safe"] = bool(
            cached_voxel_volume.prepared_3d_motion_geometry_safe
        )
        preferred_direction = _direction_from_radians(
            direction_yaw,
            direction_pitch,
        )
        voxel_route_plan = cached_voxel_volume.plan_footprint_route(
            centerline_path.component_cells,
            current_position=current,
            footprint_cell_size=centerline_path.footprint_cell_size,
            preferred_direction=preferred_direction,
            lookahead_distance_m=float(settings.lookahead_distance_m),
            scoring_policy=settings.voxel_scoring_policy,
            diagnostics=diagnostics,
        )
        planning_budget.check("voxel_route_selected", diagnostics=diagnostics)
        if (
            voxel_route_plan is not None
            and (
                len(voxel_route_plan.cells) >= 2
                or len(voxel_route_plan.world_points) >= 2
            )
            and (
                not voxel_route_plan.three_d_graph
                or cached_voxel_volume.prepared_3d_motion_geometry_safe
            )
        ):
            route_cells = voxel_route_plan.cells
            voxel_route_world_points = voxel_route_plan.world_points
            circular_arc = False
            voxel_route_active = True
            voxel_route_payload.update(
                {
                    "selected": True,
                    "fallback_reason": None,
                    "route_geometry_source": (
                        "voxel_3d_cell_centers"
                        if voxel_route_plan.three_d_graph
                        else "voxel_cell_centers"
                    ),
                    "plan": voxel_route_plan.diagnostic_payload(),
                }
            )
        else:
            if (
                cached_voxel_volume.has_prepared_3d_graph
                and not cached_voxel_volume.prepared_3d_motion_geometry_safe
            ):
                voxel_route_payload["fallback_reason"] = (
                    "prepared_3d_graph_geometry_too_coarse"
                )
            else:
                voxel_route_payload["fallback_reason"] = (
                    "missing_filled_true_3d_voxel_graph"
                    if cached_voxel_volume.navigation_cell_count <= 0
                    else "no_viable_true_3d_voxel_branch"
                )
            voxel_route_payload["branch_policy"] = {
                "reject_dead_end": True,
                "reject_backward_first_step": preferred_direction is not None,
                "min_forward_alignment": 0.0,
            }
    elif cached_voxel_volume is not None:
        voxel_route_payload["fallback_reason"] = "legacy_local_voxel_model"
    elif cache_voxel_cache_declared:
        voxel_route_payload["fallback_reason"] = (
            "cache_disabled" if not settings.voxel_analysis_enabled else "cache_miss"
        )
    else:
        voxel_route_payload["fallback_reason"] = "filled_voxel_graph_unavailable"
    _record_auto_dive_diagnostic(
        diagnostics,
        "voxel_route_selection",
        voxel_route_payload,
    )
    scan_only_recovery = bool(
        force_hemisphere_scan
        and require_voxel_graph
        and voxel_route_plan is None
        and isinstance(cached_voxel_volume, NavigationVoxelAtlas)
    )
    if scan_only_recovery:
        voxel_route_payload["fallback_reason"] = "continuous_scan_only_recovery"
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_route_selection",
            voxel_route_payload,
        )
    if require_voxel_graph and voxel_route_plan is None and not scan_only_recovery:
        failure_status = dict(authority_status or {})
        failure_status.update(
            {
                "available": False,
                "reason": "no_valid_forward_route",
                "message": (
                    "the prepared true-3D voxel graph has no valid forward route"
                ),
            }
        )
        _record_auto_dive_diagnostic(
            diagnostics,
            "navigation_authority",
            failure_status,
        )
        raise NavigationVoxelGraphAuthorityError(
            "Guided Dive found no valid forward route in the prepared true-3D "
            "voxel graph",
            reason="no_valid_forward_route",
            status=failure_status,
        )
    if (
        isinstance(cached_voxel_volume, NavigationVoxelAtlas)
        and cached_voxel_volume.has_prepared_3d_graph
        and cached_voxel_volume.prepared_3d_motion_geometry_safe
        and voxel_route_plan is None
        and not scan_only_recovery
    ):
        raise NavigationConfigurationError(
            "Guided Dive found no valid forward route in the true 3D voxel graph"
        )
    route_uses_3d_points = len(voxel_route_world_points) >= 2
    if scan_only_recovery:
        current_cell = _current_footprint_cell(
            centerline_path,
            current,
        )
        route_cells = (current_cell,)
        waypoint_cells = (current_cell,)
        route_xz = ((float(current[0]), float(current[2])),)
        route_points = (
            (float(current[0]), float(current[1]), float(current[2])),
        )
    elif len(route_cells) < 2 and not route_uses_3d_points:
        raise NavigationConfigurationError("Guided Dive route is too short")

    if route_uses_3d_points:
        waypoint_cells = route_cells
        route_xz = tuple(
            (float(point[0]), float(point[2]))
            for point in voxel_route_world_points
        )
        route_points = voxel_route_world_points
    elif not scan_only_recovery:
        route_cells = _route_cells_connected_to_current_camera(
            centerline_path,
            route_cells=route_cells,
            current=current,
        )
        waypoint_route_cells = _route_cells_after_current_camera_progress(
            centerline_path,
            route_cells=route_cells,
            current=current,
        )
        waypoint_cells = _waypoint_cells_for_auto_dive_route(
            centerline_path,
            route_cells=waypoint_route_cells,
            settings=settings,
        )
        route_xz = tuple(
            (
                footprint_world_center(cell, centerline_path.footprint_cell_size)
                if voxel_route_active
                else _center_for_route_cell(centerline_path, cell)
            )
            for cell in waypoint_cells
        )
        route_points = _auto_dive_points_for_waypoint_cells(
            centerline_path,
            waypoint_cells=waypoint_cells,
            route_xz=route_xz,
            manifest=manifest,
            settings=settings,
            prefer_route_cell_centers=voxel_route_active,
            fallback_y=float(current[1]),
        )
    planning_budget.check("route_points_built", diagnostics=diagnostics)
    mesh_guard = CachedChunkMeshCollisionGuard.from_manifest(
        manifest,
        cache_dir=cache_dir,
    )
    if cached_voxel_volume is not None:
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_volume",
            {
                "method": NAVIGATION_VOXEL_CACHE_METHOD,
                "built": True,
                "outcome": VOXEL_ANALYSIS_OUTCOME_CACHE_HIT,
                "source": "cache",
                "metrics": getattr(centerline_path, "cached_voxel_metrics", None),
                "volume": cached_voxel_volume.diagnostic_payload(),
            },
        )
    elif cache_voxel_cache_declared and not bool(settings.voxel_analysis_enabled):
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_volume",
            {
                "method": NAVIGATION_VOXEL_CACHE_METHOD,
                "built": False,
                "outcome": VOXEL_ANALYSIS_OUTCOME_DISABLED,
                "source": "cache",
            },
        )
    elif cache_voxel_cache_declared and bool(settings.voxel_analysis_enabled):
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_volume",
            {
                "method": NAVIGATION_VOXEL_CACHE_METHOD,
                "built": False,
                "outcome": VOXEL_ANALYSIS_OUTCOME_CACHE_MISS,
                "source": "cache",
                "metrics": getattr(centerline_path, "cached_voxel_metrics", None),
            },
        )
    local_refinement_forward = _direction_from_radians(
        direction_yaw,
        direction_pitch,
    )
    fine_frontier_tile = (
        cached_voxel_volume.fine_tile_for_point(current)
        if isinstance(cached_voxel_volume, NavigationVoxelAtlas)
        else None
    )
    if isinstance(cached_voxel_volume, NavigationVoxelAtlas):
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_fine_frontier_coverage",
            {
                "covered": fine_frontier_tile is not None,
                "fine_tile_count": int(cached_voxel_volume.fine_tile_count),
                "fine_voxel_size_m": float(
                    cached_voxel_volume.fine_voxel_size_m
                ),
                "position": [float(value) for value in current],
                "source": "cache",
            },
        )
    runtime_refinement_allowed = planning_budget.budget_s is None
    local_refinement_builder = (
        _make_auto_dive_local_frontier_voxel_builder(
            current=current,
            forward=local_refinement_forward,
            mesh_guard=mesh_guard,
            settings=settings,
            diagnostics=diagnostics,
        )
        if (
            (cached_voxel_volume is not None or cache_voxel_cache_declared)
            and fine_frontier_tile is None
            and runtime_refinement_allowed
        )
        else None
    )
    if (
        fine_frontier_tile is None
        and (cached_voxel_volume is not None or cache_voxel_cache_declared)
        and not runtime_refinement_allowed
    ):
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_local_refinement",
            {
                "built": False,
                "outcome": "deferred_to_cache_or_background",
                "reason": "runtime_planning_budget",
                "planning_budget_s": planning_budget.budget_s,
            },
        )
    collision_validator = _AutoDiveCollisionValidator(
        centerline_path,
        mesh_guard=mesh_guard,
        voxel_volume=cached_voxel_volume,
        voxel_refinement_builder=local_refinement_builder,
        voxel_builder=(
            None
            if cached_voxel_volume is not None or cache_voxel_cache_declared
            else _make_auto_dive_voxel_builder(
                route_points=route_points,
                centerline_path=centerline_path,
                mesh_guard=mesh_guard,
                settings=settings,
                diagnostics=diagnostics,
            )
        ),
        allow_native_graph_transitions=bool(
            isinstance(cached_voxel_volume, NavigationVoxelAtlas)
            and cached_voxel_volume.has_prepared_3d_graph
        ),
    )
    selected_route = _select_best_auto_dive_route_candidate(
        centerline_path,
        waypoint_cells=waypoint_cells,
        route_points=route_points,
        current=current,
        settings=settings,
        collision_validator=collision_validator,
        current_yaw=current_yaw,
        current_pitch=current_pitch,
        current_roll=current_roll,
        current_travel_yaw=current_travel_yaw,
        current_travel_pitch=current_travel_pitch,
        avoid_positions=avoid_positions,
        user_reposition=user_reposition,
        force_hemisphere_scan=force_hemisphere_scan,
        voxel_route_active=bool(voxel_route_active or scan_only_recovery),
        voxel_route_plan=voxel_route_plan,
        planning_budget=planning_budget,
        diagnostics=diagnostics,
    )
    planning_budget.check("route_candidate_selected", diagnostics=diagnostics)
    route_points = selected_route.points
    route_points = _route_points_starting_at_current_camera(route_points, current)
    route_points = _dedupe_consecutive_points(route_points)
    length_m = path_length(route_points)
    if length_m <= 1e-6:
        raise NavigationConfigurationError("Guided Dive route has no travel distance")
    duration_s = length_m / float(settings.speed_m_per_second)
    keyframe_payloads = route_keyframes_for_points(
        route_points,
        duration_s=duration_s,
        lookahead_distance_m=max(0.0, float(settings.lookahead_distance_m)),
    )
    for payload in keyframe_payloads:
        # Hemisphere probe roll is a scanner orientation used for candidate
        # scoring only. It must never rotate the executed camera route.
        payload["roll_deg"] = 0.0
    keyframe_payloads = _wall_aware_auto_dive_keyframe_payloads(
        keyframe_payloads,
        route_points=route_points,
        centerline_path=centerline_path,
        settings=settings,
        collision_validator=collision_validator,
        planning_budget=planning_budget,
        diagnostics=diagnostics,
    )
    planning_budget.check("keyframes_built", diagnostics=diagnostics)
    keyframes = [
        RouteKeyframe.from_mapping(payload, index=index)
        for index, payload in enumerate(keyframe_payloads)
    ]
    route = CameraRoute.from_keyframes(keyframes)
    return AutoDivePlan(
        route=route,
        centerline_path=centerline_path,
        route_points=route_points,
        route_cells=route_cells,
        circular_arc=circular_arc,
        route_length_m=length_m,
        duration_s=duration_s,
        render_distance_cells=max(
            1,
            int(settings.render_distance_cells),
        ),
        selection_reason=selected_route.selection_reason,
        route_truncated_by_mesh=bool(selected_route.route_truncated_by_mesh),
        mesh_safe_prefix_length_m=selected_route.mesh_safe_prefix_length_m,
        replan_at_end=bool(selected_route.replan_at_end),
        voxel_route_selection=(
            None
            if voxel_route_plan is None
            else voxel_route_plan.diagnostic_payload()
        ),
        terminal_reached=bool(selected_route.terminal_reached),
        navigation_route_id=(
            None
            if authority_status is None
            else authority_status.get("route_id")
        ),
    )


def build_voxel_graph_auto_dive_plan(
    manifest: Mapping[str, Any],
    *,
    current_position: tuple[float, float, float] | np.ndarray,
    current_yaw: float | None = None,
    current_pitch: float | None = None,
    current_roll: float | None = None,
    current_travel_yaw: float | None = None,
    current_travel_pitch: float | None = None,
    avoid_positions: Sequence[Sequence[float]] | None = None,
    user_reposition: bool = False,
    force_hemisphere_scan: bool = False,
    settings: AutoDiveSettings | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    diagnostics: AutoDiveDiagnosticSink | None = None,
    route_id: str | None = None,
    expand_frontier: bool = False,
) -> AutoDivePlan:
    """Build a production Guided Dive plan directly from the prepared graph.

    This is deliberately separate from ``build_centerline_auto_dive_plan``.
    The compatibility planner remains available to older callers, while the
    GUI, preflight, runtime replanner, and continuous scan all use this path.
    """
    del current_roll, user_reposition
    settings = settings or AutoDiveSettings()
    _validate_auto_dive_settings(settings)
    planning_budget = _AutoDivePlanningBudget.from_settings(settings)
    planning_budget.check("initialization", diagnostics=diagnostics)
    current_array = np.asarray(current_position, dtype=np.float64)
    if current_array.shape != (3,) or not np.all(np.isfinite(current_array)):
        raise NavigationConfigurationError(
            "Guided Dive graph planning requires a finite 3D camera position"
        )
    current: Point = tuple(float(value) for value in current_array)

    atlas, authority_status = _authoritative_graph_navigation_context(
        manifest,
        cache_dir=cache_dir,
        settings=settings,
        diagnostics=diagnostics,
        route_id=route_id,
    )
    planning_budget.check("graph_context_loaded", diagnostics=diagnostics)
    graph = atlas.prepared_3d_graph
    if graph is None:
        raise NavigationVoxelGraphAuthorityError(
            "Guided Dive prepared graph disappeared after authority validation",
            reason="true_3d_graph_missing",
            status=authority_status,
        )

    direction_yaw = (
        current_travel_yaw if current_travel_yaw is not None else current_yaw
    )
    direction_pitch = (
        current_travel_pitch
        if current_travel_pitch is not None
        else current_pitch
    )
    # A full hemisphere scan widens discovery; it does not revoke the
    # current-travel handoff invariant.  Keep the travel direction in the
    # graph query so a nearest-node anchor behind the camera is re-rooted
    # before the route is published.  If no heading-aligned branch exists,
    # the bounded relaxed-heading retry below still gives the scan a chance
    # to find a valid lateral or vertical continuation.
    preferred_direction = _direction_from_radians(
        direction_yaw,
        direction_pitch,
    )
    graph_scale = max(
        0.25,
        *(float(value) for value in graph.grid_size_m),
        float(atlas.voxel_size_m or 0.0),
    )
    voxel_route_plan = atlas.plan_footprint_route(
        (),
        current_position=current,
        footprint_cell_size=graph_scale,
        preferred_direction=preferred_direction,
        lookahead_distance_m=float(settings.lookahead_distance_m),
        scoring_policy=settings.voxel_scoring_policy,
        diagnostics=diagnostics,
        deadline_check=lambda: planning_budget.check(
            "voxel_route_search",
            diagnostics=diagnostics,
        ),
    )
    planning_budget.check("voxel_route_selected", diagnostics=diagnostics)
    if voxel_route_plan is None and preferred_direction is not None:
        # A continuous scan is a graph query with relaxed heading preference,
        # not a return to centerline hemisphere recovery.
        voxel_route_plan = atlas.plan_footprint_route(
            (),
            current_position=current,
            footprint_cell_size=graph_scale,
            preferred_direction=None,
            lookahead_distance_m=float(settings.lookahead_distance_m),
            scoring_policy=settings.voxel_scoring_policy,
            diagnostics=diagnostics,
            deadline_check=lambda: planning_budget.check(
                "voxel_route_search_relaxed_heading",
                diagnostics=diagnostics,
            ),
        )
        planning_budget.check("voxel_route_selected", diagnostics=diagnostics)
    mesh_guard = CachedChunkMeshCollisionGuard.from_manifest(
        manifest,
        cache_dir=cache_dir,
    )
    planning_budget.check("mesh_guard_loaded", diagnostics=diagnostics)
    if mesh_guard is None:
        status = {
            **authority_status,
            "available": False,
            "reason": "mesh_collision_guard_unavailable",
            "message": (
                "Guided Dive runtime planning requires cached mesh collision "
                "evidence"
            ),
        }
        _record_auto_dive_diagnostic(
            diagnostics,
            "navigation_authority",
            status,
        )
        raise NavigationVoxelGraphAuthorityError(
            "Guided Dive runtime planning requires a cached mesh collision guard",
            reason="mesh_collision_guard_unavailable",
            status=status,
        )
    if bool(expand_frontier) and _voxel_route_needs_local_frontier_expansion(
        voxel_route_plan
    ):
        local_forward = _direction_from_radians(direction_yaw, direction_pitch)
        local_builder = _make_auto_dive_local_frontier_voxel_builder(
            current=np.asarray(current, dtype=np.float64),
            forward=local_forward,
            mesh_guard=mesh_guard,
            settings=settings,
            diagnostics=diagnostics,
            planning_budget=planning_budget,
        )
        local_volume = None if local_builder is None else local_builder()
        planning_budget.check("local_frontier_volume_built", diagnostics=diagnostics)
        local_expansion = (
            None
            if local_volume is None
            else _build_bounded_local_frontier_graph_route(
                volume=local_volume,
                current=current,
                forward=local_forward,
                settings=settings,
                mesh_guard=mesh_guard,
                avoid_positions=avoid_positions,
                authority_status=authority_status,
                diagnostics=diagnostics,
                planning_budget=planning_budget,
            )
        )
        planning_budget.check("local_frontier_graph_built", diagnostics=diagnostics)
        if local_expansion is not None:
            (
                local_atlas,
                local_graph,
                local_route_points,
                local_graph_keys,
                local_route_cells,
                local_selection,
            ) = local_expansion
            local_route_points = _dedupe_consecutive_points(local_route_points)
            if len(local_route_points) >= 2:
                local_length_m = path_length(local_route_points)
                local_duration_s = local_length_m / float(
                    settings.speed_m_per_second
                )
                local_keyframe_payloads = route_keyframes_for_points(
                    local_route_points,
                    duration_s=local_duration_s,
                    lookahead_distance_m=max(
                        0.0,
                        float(settings.lookahead_distance_m),
                    ),
                )
                for payload in local_keyframe_payloads:
                    payload["roll_deg"] = 0.0
                local_keyframes = [
                    RouteKeyframe.from_mapping(payload, index=index)
                    for index, payload in enumerate(local_keyframe_payloads)
                ]
                _record_auto_dive_diagnostic(
                    diagnostics,
                    "voxel_local_frontier_graph_expansion",
                    {
                        **local_selection,
                        "accepted": True,
                        "route_point_count": len(local_route_points),
                        "route_length_m": float(local_length_m),
                    },
                )
                return AutoDivePlan(
                    route=CameraRoute.from_keyframes(local_keyframes),
                    centerline_path=None,
                    route_points=local_route_points,
                    route_cells=local_route_cells,
                    circular_arc=False,
                    route_length_m=float(local_length_m),
                    duration_s=float(local_duration_s),
                    render_distance_cells=max(
                        1,
                        int(settings.render_distance_cells),
                    ),
                    selection_reason="continuous_local_frontier_expansion",
                    replan_at_end=True,
                    voxel_route_selection=local_selection,
                    terminal_reached=False,
                    navigation_route_id=authority_status.get("route_id"),
                    navigation_atlas=local_atlas,
                    navigation_graph=local_graph,
                    navigation_graph_keys=local_graph_keys,
                )
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_local_frontier_graph_expansion",
            {
                "accepted": False,
                "reason": "no_mesh_safe_local_graph_route",
                "authority": "bounded_runtime_local_true_3d_graph",
            },
        )
    if (
        voxel_route_plan is None
        or len(voxel_route_plan.world_points) < 2
        or not voxel_route_plan.graph_keys
    ):
        raise NavigationVoxelGraphAuthorityError(
            "Guided Dive found no valid route in the prepared true-3D voxel graph",
            reason="no_valid_forward_route",
            status={
                **authority_status,
                "available": False,
                "reason": "no_valid_forward_route",
            },
        )

    route_points = _dedupe_consecutive_points(
        tuple(tuple(float(value) for value in point) for point in voxel_route_plan.world_points)
    )
    route_cells = tuple(voxel_route_plan.cells)
    if len(route_points) > int(settings.max_keyframes):
        bounded_points, bounded_cells = _preflight_bounded_route_geometry(
            route_points,
            route_cells,
            max_keyframes=int(settings.max_keyframes),
        )
        if bounded_points is None or bounded_cells is None:
            raise NavigationConfigurationError(
                "Guided Dive graph route exceeds the keyframe budget"
            )
        route_points = bounded_points
        route_cells = bounded_cells

    executed_start_key = _graph_key_for_route_point(
        route_points[1],
        voxel_route_plan.graph_keys,
        graph,
    )
    graph_safety_validator = GraphRouteSafetyValidator(
        atlas,
        graph,
        mesh_guard=mesh_guard,
        policy=GraphRouteSafetyPolicy(
            minimum_clearance_m=float(settings.minimum_graph_clearance_m),
        ),
    )
    planning_budget.check("graph_safety_validation", diagnostics=diagnostics)
    safety_failure = graph_safety_validator.route_clearance_failure(
        route_points,
        voxel_route_plan.graph_keys,
        start_graph_key=executed_start_key,
    )
    if safety_failure is not None:
        payload = safety_failure.diagnostic_payload()
        _record_auto_dive_diagnostic(
            diagnostics,
            "graph_route_safety_failed",
            {
                **payload,
                "route_id": authority_status.get("route_id"),
                "minimum_graph_clearance_m": float(
                    settings.minimum_graph_clearance_m
                ),
            },
        )
        raise NavigationConfigurationError(
            "Guided Dive graph route failed safety validation: "
            f"{safety_failure.reason}"
        )
    planning_budget.check("graph_safety_validation", diagnostics=diagnostics)

    prefetched_chunk_ids = atlas.prefetch_for_points(route_points)
    planning_budget.check("route_prefetch", diagnostics=diagnostics)
    route_length_m = path_length(route_points)
    if route_length_m <= 1e-6:
        raise NavigationConfigurationError("Guided Dive graph route has no travel distance")
    duration_s = route_length_m / float(settings.speed_m_per_second)
    keyframe_payloads = route_keyframes_for_points(
        route_points,
        duration_s=duration_s,
        lookahead_distance_m=max(0.0, float(settings.lookahead_distance_m)),
    )
    for payload in keyframe_payloads:
        payload["roll_deg"] = 0.0
    keyframes = [
        RouteKeyframe.from_mapping(payload, index=index)
        for index, payload in enumerate(keyframe_payloads)
    ]
    planning_budget.check("route_published", diagnostics=diagnostics)
    return AutoDivePlan(
        route=CameraRoute.from_keyframes(keyframes),
        centerline_path=None,
        route_points=route_points,
        route_cells=route_cells,
        circular_arc=False,
        route_length_m=route_length_m,
        duration_s=duration_s,
        render_distance_cells=max(1, int(settings.render_distance_cells)),
        selection_reason=(
            "continuous_graph_scan" if force_hemisphere_scan else "prepared_true_3d_graph"
        ),
        replan_at_end=bool(voxel_route_plan.replan_at_lookahead),
        voxel_route_selection={
            **voxel_route_plan.diagnostic_payload(),
            "route_geometry_source": "prepared_true_3d_graph",
            "authority": "prepared_true_3d_voxel_graph",
            "graph_snapshot": _graph_snapshot_payload(
                graph,
                authority_status,
            ),
            "minimum_graph_clearance_m": float(
                settings.minimum_graph_clearance_m
            ),
            "prefetched_chunk_count": len(prefetched_chunk_ids),
            "executed_start_graph_key": [
                int(value) for value in executed_start_key
            ],
        },
        terminal_reached=bool(voxel_route_plan.terminal_reached),
        navigation_route_id=authority_status.get("route_id"),
        navigation_atlas=atlas,
        navigation_graph=graph,
        navigation_graph_keys=tuple(voxel_route_plan.graph_keys),
    )


def _graph_snapshot_payload(
    graph: NavigationVoxel3DGraph,
    authority_status: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the stable graph identity used by frontier handoff logic."""
    return {
        "cache_version": authority_status.get("cache_version"),
        "cache_method": authority_status.get("cache_method"),
        "graph_method": str(graph.method),
        "node_count": int(len(graph.nodes)),
        "edge_count": int(graph.edge_count),
    }


def _voxel_route_needs_local_frontier_expansion(
    route_plan: NavigationVoxelRoutePlan | None,
) -> bool:
    """Return whether a prepared route stopped at an unresolved frontier."""
    if route_plan is None:
        return True
    branch = route_plan.branch_score
    return bool(
        route_plan.unknown_boundary_reached
        and branch is not None
        and int(branch.frontier_count) <= 0
        and int(branch.onward_exit_count) <= 0
        and not route_plan.terminal_reached
    )


def _build_bounded_local_frontier_graph_route(
    *,
    volume: LocalVoxelVolume,
    current: Point,
    forward: Sequence[float] | None,
    settings: AutoDiveSettings,
    mesh_guard: CachedChunkMeshCollisionGuard | None,
    avoid_positions: Sequence[Sequence[float]] | None,
    authority_status: Mapping[str, Any],
    diagnostics: AutoDiveDiagnosticSink | None,
    planning_budget: _AutoDivePlanningBudget | None = None,
) -> tuple[
    NavigationVoxelAtlas,
    NavigationVoxel3DGraph,
    tuple[Point, ...],
    tuple[VoxelGraphKey, ...],
    tuple[FootprintCell, ...],
    dict[str, Any],
] | None:
    """Turn one bounded local search result into a graph-native route.

    The local field is an expansion of the prepared cache, not a replacement
    route authority. It is converted to a small true-3D graph and passed
    through the same graph/voxel/mesh safety validator before execution.
    """
    if forward is None:
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_local_frontier_graph_expansion",
            {"accepted": False, "reason": "travel_direction_unavailable"},
        )
        return None
    direction_values = tuple(float(value) for value in forward)
    direction_norm = math.sqrt(sum(value * value for value in direction_values))
    if direction_norm <= 1e-9:
        return None
    direction = tuple(value / direction_norm for value in direction_values)
    local_route = volume.find_forward_route(
        current,
        direction,
        max_distance_m=float(settings.voxel_local_refinement_forward_m),
        max_nodes=int(settings.voxel_local_refinement_max_cells),
        min_target_distance_m=max(3.0, float(settings.lookahead_distance_m)),
        deadline_monotonic_s=(
            None
            if planning_budget is None
            else planning_budget.deadline_monotonic_s
        ),
    )
    if planning_budget is not None:
        planning_budget.check(
            "local_frontier_route_search",
            diagnostics=diagnostics,
        )
    if local_route is None or not local_route.indices:
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_local_frontier_graph_expansion",
            {"accepted": False, "reason": "local_forward_route_missing"},
        )
        return None

    size = max(1e-6, float(volume.voxel_size_m))
    centers = tuple(volume.voxel_center(index) for index in local_route.indices)
    if not centers:
        return None
    try:
        avoided = tuple(
            tuple(float(value) for value in position)
            for position in (avoid_positions or ())
        )
    except (TypeError, ValueError):
        avoided = ()
    avoid_radius = max(0.5, size * 1.5)
    if any(
        sum((center[axis] - position[axis]) ** 2 for axis in range(3))
        <= avoid_radius * avoid_radius
        for center in centers[1:]
        for position in avoided
        if len(position) == 3
    ):
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_local_frontier_graph_expansion",
            {
                "accepted": False,
                "reason": "local_route_intersects_avoided_frontier",
                "avoid_radius_m": float(avoid_radius),
            },
        )
        return None

    max_graph_points = max(1, int(settings.max_keyframes) - 1)
    selected_indices = tuple(local_route.indices[:max_graph_points])
    if selected_indices:
        first_center = volume.voxel_center(selected_indices[0])
        if sum(
            (float(first_center[axis]) - float(current[axis])) ** 2
            for axis in range(3)
        ) <= 1e-12:
            selected_indices = selected_indices[1:]
    selected_centers = tuple(
        volume.voxel_center(index) for index in selected_indices
    )
    if not selected_centers:
        return None
    graph_keys: tuple[VoxelGraphKey, ...] = tuple(
        tuple(int(value) for value in index)  # type: ignore[misc]
        for index in selected_indices
    )
    if len(set(graph_keys)) != len(graph_keys):
        return None

    # The local voxel search permits 26-connected diagonal steps. Preserve
    # the exact route keys, but add the intermediate free voxels needed by
    # the graph builder's line-of-sight invariant so a diagonal edge is not
    # accidentally discarded from the expanded graph.
    metric_indices: list[tuple[int, int, int]] = list(selected_indices)
    metric_index_set = set(metric_indices)
    for first, second in zip(selected_indices, selected_indices[1:], strict=False):
        changed_axes = [
            axis for axis in range(3) if int(first[axis]) != int(second[axis])
        ]
        for mask in range(1, 1 << len(changed_axes)):
            candidate = list(first)
            for bit, axis in enumerate(changed_axes):
                if mask & (1 << bit):
                    candidate[axis] = int(second[axis])
            candidate_index = tuple(int(value) for value in candidate)
            if (
                candidate_index in metric_index_set
                or not volume.contains_index(candidate_index)
                or candidate_index in volume.surface_cells
            ):
                continue
            metric_index_set.add(candidate_index)
            metric_indices.append(candidate_index)

    metrics: dict[VoxelGraphKey, NavigationVoxel3DMetric] = {}
    for index in metric_indices:
        key = tuple(int(value) for value in index)  # type: ignore[misc]
        center = volume.voxel_center(index)
        clearance = max(0.0, float(volume.surface_clearance_m(index)))
        progress = sum(
            (float(center[axis]) - float(current[axis])) * direction[axis]
            for axis in range(3)
        )
        metrics[key] = NavigationVoxel3DMetric(
            center=tuple(float(value) for value in center),
            footprint_cell=(
                int(math.floor(float(center[0]) / size)),
                int(math.floor(float(center[2]) / size)),
            ),
            available_volume_m3=float(size**3),
            free_voxel_count=1,
            min_clearance_m=clearance,
            mean_clearance_m=clearance,
            progress_m=float(progress),
        )
    unknown_boundary = (
        (graph_keys[-1],)
        if bool(local_route.boundary_reached)
        else ()
    )
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(size, size, size),
        max_edge_distance_cells=1,
        max_edges_per_node=26,
        max_total_edges=max(64, len(metrics) * 26),
        unknown_boundary=unknown_boundary,
    )
    if not graph.motion_geometry_safe or not graph.edge_integrity_safe:
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_local_frontier_graph_expansion",
            {
                "accepted": False,
                "reason": "local_graph_geometry_unsafe",
                "graph": graph.diagnostic_payload(),
            },
        )
        return None

    local_atlas = NavigationVoxelAtlas(
        tiles=(),
        fine_tiles=(volume,),
        prepared_3d_graph=graph,
        coverage_scope="runtime_local_frontier",
    )
    route_points = (tuple(float(value) for value in current),) + selected_centers
    safety_failure = GraphRouteSafetyValidator(
        local_atlas,
        graph,
        mesh_guard=mesh_guard,
        policy=GraphRouteSafetyPolicy(
            minimum_clearance_m=float(settings.minimum_graph_clearance_m),
        ),
    ).route_clearance_failure(
        route_points,
        graph_keys,
        start_graph_key=graph_keys[0],
    )
    if safety_failure is not None:
        _record_auto_dive_diagnostic(
            diagnostics,
            "graph_route_safety_failed",
            {
                **safety_failure.diagnostic_payload(),
                "route_id": authority_status.get("route_id"),
                "local_frontier_expansion": True,
            },
        )
        return None

    branch = {
        "branch_start_key": [int(value) for value in graph_keys[0]],
        "target_key": [int(value) for value in graph_keys[-1]],
        "continuation_distance_m": float(local_route.distance_m),
        "onward_exit_count": 0 if local_route.boundary_reached else 1,
        "frontier_count": 1 if local_route.boundary_reached else 0,
        "unknown_boundary": bool(local_route.boundary_reached),
        "target_is_terminal": False,
        "dead_end": False,
    }
    selection = {
        "method": str(graph.method),
        "selection_reason": "continuous_local_frontier_expansion",
        "authority": "bounded_runtime_local_true_3d_graph",
        "route_geometry_source": "bounded_cached_mesh_local_voxels",
        "coverage_incomplete": True,
        "unknown_boundary_reached": bool(local_route.boundary_reached),
        "terminal_reached": False,
        "replan_at_lookahead": True,
        "graph_keys": [
            [int(value) for value in key] for key in graph_keys[:8]
        ],
        "graph_snapshot": {
            **_graph_snapshot_payload(graph, authority_status),
            "expansion": "bounded_cached_mesh_frontier",
            "base_cache_version": authority_status.get("cache_version"),
        },
        "branch": branch,
        "local_route": local_route.diagnostic_payload(),
        "prepared_graph": graph.diagnostic_payload(),
        "avoid_positions_applied": len(avoided),
    }
    return (
        local_atlas,
        graph,
        route_points,
        graph_keys,
        tuple(
            graph.nodes[key].footprint_cell
            for key in graph_keys
            if key in graph.nodes
        ),
        selection,
    )


def _graph_key_for_route_point(
    point: Point,
    graph_keys: Sequence[VoxelGraphKey],
    graph: NavigationVoxel3DGraph,
) -> VoxelGraphKey:
    return min(
        graph_keys,
        key=lambda key: (
            _point_distance_squared(graph.nodes[key].center, point),
            key,
        ),
    )


AUTO_DIVE_PREFLIGHT_READY = "READY"
AUTO_DIVE_PREFLIGHT_INDETERMINATE = "INDETERMINATE"
AUTO_DIVE_PREFLIGHT_FAILED = "FAILED"


@dataclass(frozen=True)
class AutoDivePreflightResult:
    """The startup validation decision for one exact Guided Dive route."""

    status: str
    reason: str
    plan: AutoDivePlan | None = None
    navigation_route_id: str | None = None
    terminal_point: Point | None = None
    terminal_graph_key: VoxelGraphKey | None = None
    start_graph_key: VoxelGraphKey | None = None
    route_point_count: int = 0
    coverage_incomplete: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        """Return whether the exact validated plan may be activated."""
        return self.status == AUTO_DIVE_PREFLIGHT_READY and self.plan is not None

    def diagnostic_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": str(self.status),
            "reason": str(self.reason),
            "navigation_route_id": self.navigation_route_id,
            "terminal_point": (
                None
                if self.terminal_point is None
                else [float(value) for value in self.terminal_point]
            ),
            "terminal_graph_key": (
                None
                if self.terminal_graph_key is None
                else [int(value) for value in self.terminal_graph_key]
            ),
            "start_graph_key": (
                None
                if self.start_graph_key is None
                else [int(value) for value in self.start_graph_key]
            ),
            "route_point_count": int(self.route_point_count),
            "coverage_incomplete": bool(self.coverage_incomplete),
            "details": dict(self.details),
        }
        if self.plan is not None:
            payload["route_length_m"] = float(self.plan.route_length_m)
            payload["duration_s"] = float(self.plan.duration_s)
            payload["preflight_validated"] = bool(
                self.plan.preflight_validated
            )
        return payload


def build_auto_dive_preflight_plan(
    manifest: Mapping[str, Any],
    *,
    current_position: tuple[float, float, float] | np.ndarray,
    current_yaw: float | None = None,
    current_pitch: float | None = None,
    settings: AutoDiveSettings | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    diagnostics: AutoDiveDiagnosticSink | None = None,
) -> AutoDivePreflightResult:
    """Validate one complete cave route before Guided Dive activation.

    Startup preflight deliberately differs from receding-horizon replanning:
    it searches the prepared true-3D graph all the way to the farthest
    reachable graph terminal/frontier in the starting component. The returned
    plan is the same graph path that the controller receives, so activation
    cannot succeed on a route that was not the route validated here.
    """
    settings = settings or AutoDiveSettings()
    _validate_auto_dive_settings(settings)
    current_array = np.asarray(current_position, dtype=np.float64)
    if current_array.shape != (3,) or not np.all(np.isfinite(current_array)):
        raise NavigationConfigurationError(
            "Guided Dive preflight requires a finite 3D camera position"
        )
    current: Point = tuple(float(value) for value in current_array)

    route_id = _longest_navigation_route_id(manifest)
    if route_id is None:
        return _auto_dive_preflight_result(
            diagnostics,
            status=AUTO_DIVE_PREFLIGHT_INDETERMINATE,
            reason="longest_passage_metadata_missing",
            details={"route_selection": "prepared_graph_cache_identity"},
        )

    try:
        cached_volume, _authority_status = _authoritative_graph_navigation_context(
            manifest,
            cache_dir=cache_dir,
            settings=settings,
            diagnostics=diagnostics,
            route_id=route_id,
        )
    except NavigationVoxelGraphAuthorityError as exc:
        status = (
            AUTO_DIVE_PREFLIGHT_INDETERMINATE
            if exc.reason
            in {
                "cache_directory_missing",
                "navigation_metadata_missing",
                "voxel_cache_not_declared",
                "stale_or_unsupported_cache",
                "voxel_cache_path_invalid",
                "navigation_route_missing",
                "navigation_route_metadata_invalid",
                "voxel_graph_model_missing",
                "true_3d_graph_missing",
            }
            else AUTO_DIVE_PREFLIGHT_FAILED
        )
        return _auto_dive_preflight_result(
            diagnostics,
            status=status,
            reason=f"navigation_authority:{exc.reason}",
            navigation_route_id=route_id,
            details=dict(exc.status),
        )

    graph = cached_volume.prepared_3d_graph
    if graph is None:
        return _auto_dive_preflight_result(
            diagnostics,
            status=AUTO_DIVE_PREFLIGHT_INDETERMINATE,
            reason="prepared_graph_missing_after_authority_check",
            navigation_route_id=route_id,
        )

    snap_tolerance_m = _preflight_graph_snap_tolerance_m(
        graph,
        cached_volume=cached_volume,
    )
    start_key, start_distance_m = _preflight_nearest_graph_key(
        graph,
        current,
        routable_only=True,
    )
    if start_key is None:
        return _auto_dive_preflight_result(
            diagnostics,
            status=AUTO_DIVE_PREFLIGHT_FAILED,
            reason="prepared_graph_start_unroutable",
            navigation_route_id=route_id,
            details={
                "graph_node_count": len(graph.nodes),
            },
        )
    if start_distance_m > snap_tolerance_m:
        return _auto_dive_preflight_result(
            diagnostics,
            status=AUTO_DIVE_PREFLIGHT_INDETERMINATE,
            reason="camera_outside_prepared_graph_snap_tolerance",
            navigation_route_id=route_id,
            start_graph_key=start_key,
            details={
                "start_snap_distance_m": float(start_distance_m),
                "snap_tolerance_m": float(snap_tolerance_m),
            },
        )

    component_id = int(graph.nodes[start_key].component_id)
    terminal_key, terminal_details = _preflight_select_graph_terminal(
        graph,
        start_key=start_key,
        component_id=component_id,
    )
    if terminal_key is None:
        terminal_reason = str(
            terminal_details.get(
                "reason",
                "prepared_graph_terminal_unavailable",
            )
        )
        terminal_status = (
            AUTO_DIVE_PREFLIGHT_FAILED
            if terminal_reason == "graph_terminal_search_expansion_limit"
            else AUTO_DIVE_PREFLIGHT_INDETERMINATE
        )
        return _auto_dive_preflight_result(
            diagnostics,
            status=terminal_status,
            reason=terminal_reason,
            navigation_route_id=route_id,
            start_graph_key=start_key,
            details={**terminal_details, "start_component_id": component_id},
        )
    terminal_point = tuple(
        float(value) for value in graph.nodes[terminal_key].center
    )
    terminal_coverage_incomplete = bool(
        terminal_details.get("terminal_unknown_boundary", False)
    )

    graph_keys, graph_search_details = _preflight_global_graph_route(
        graph,
        start_key=start_key,
        terminal_key=terminal_key,
        preferred_direction=_direction_from_radians(
            current_yaw,
            current_pitch,
        ),
    )
    if graph_keys is None:
        return _auto_dive_preflight_result(
            diagnostics,
            status=AUTO_DIVE_PREFLIGHT_FAILED,
            reason=str(graph_search_details.get("reason", "no_graph_route")),
            navigation_route_id=route_id,
            terminal_point=terminal_point,
            start_graph_key=start_key,
            terminal_graph_key=terminal_key,
            details={
                **terminal_details,
                **graph_search_details,
                "start_component_id": component_id,
            },
            coverage_incomplete=terminal_coverage_incomplete,
        )

    coverage_incomplete = terminal_coverage_incomplete or any(
        bool(graph.nodes[key].unknown_boundary)
        for key in graph_keys
        if key in graph.nodes
    )
    full_route_points, full_route_cells = _preflight_route_geometry(
        current,
        graph_keys,
        graph,
    )
    if len(full_route_points) < 2:
        return _auto_dive_preflight_result(
            diagnostics,
            status=AUTO_DIVE_PREFLIGHT_FAILED,
            reason="terminal_already_at_camera",
            navigation_route_id=route_id,
            terminal_point=terminal_point,
            start_graph_key=start_key,
            terminal_graph_key=terminal_key,
            details={**terminal_details, **graph_search_details},
            coverage_incomplete=coverage_incomplete,
        )

    mesh_guard = CachedChunkMeshCollisionGuard.from_manifest(
        manifest,
        cache_dir=cache_dir,
    )
    if mesh_guard is None:
        return _auto_dive_preflight_result(
            diagnostics,
            status=AUTO_DIVE_PREFLIGHT_INDETERMINATE,
            reason="mesh_collision_guard_unavailable",
            navigation_route_id=route_id,
            terminal_point=terminal_point,
            start_graph_key=start_key,
            terminal_graph_key=terminal_key,
            details={
                **terminal_details,
                **graph_search_details,
                "start_component_id": component_id,
            },
            coverage_incomplete=coverage_incomplete,
        )

    graph_safety_validator = GraphRouteSafetyValidator(
        cached_volume,
        graph,
        mesh_guard=mesh_guard,
        policy=GraphRouteSafetyPolicy(
            minimum_clearance_m=float(settings.minimum_graph_clearance_m),
        ),
    )
    full_failure = graph_safety_validator.route_clearance_failure(
        full_route_points,
        graph_keys,
    )
    mesh_safe_frontier = False
    mesh_safe_frontier_details: dict[str, Any] | None = None
    if full_failure is not None:
        original_failure_payload = _preflight_clearance_failure_payload(
            full_failure
        )
        if full_failure.reason == "mesh_intersection":
            (
                safe_graph_keys,
                safe_terminal_key,
                safe_details,
            ) = _preflight_mesh_safe_graph_frontier(
                graph,
                start_key=start_key,
                component_id=component_id,
                graph_safety_validator=graph_safety_validator,
            )
            if safe_graph_keys is not None and safe_terminal_key is not None:
                requested_terminal_key = terminal_key
                terminal_key = safe_terminal_key
                terminal_point = tuple(
                    float(value) for value in graph.nodes[terminal_key].center
                )
                graph_keys = safe_graph_keys
                terminal_details = {
                    **terminal_details,
                    **safe_details,
                    "mesh_safe_frontier_fallback": True,
                    "requested_terminal_graph_key": [
                        int(value) for value in requested_terminal_key
                    ],
                }
                graph_search_details = {
                    **graph_search_details,
                    **safe_details,
                    "mesh_safe_frontier_fallback": True,
                    "requested_terminal_graph_key": [
                        int(value) for value in requested_terminal_key
                    ],
                }
                coverage_incomplete = True
                full_route_points, full_route_cells = _preflight_route_geometry(
                    current,
                    graph_keys,
                    graph,
                )
                full_failure = graph_safety_validator.route_clearance_failure(
                    full_route_points,
                    graph_keys,
                )
                if full_failure is None:
                    mesh_safe_frontier = True
                    mesh_safe_frontier_details = {
                        **safe_details,
                        "requested_terminal_graph_key": [
                            int(value) for value in requested_terminal_key
                        ],
                        "original_collision_failure": original_failure_payload,
                    }
        if full_failure is not None:
            failure_details = {
                **terminal_details,
                **graph_search_details,
                "start_component_id": component_id,
                "collision_failure": _preflight_clearance_failure_payload(
                    full_failure
                ),
            }
            if mesh_safe_frontier_details is not None:
                failure_details["mesh_safe_frontier_attempt"] = (
                    mesh_safe_frontier_details
                )
            else:
                failure_details["original_collision_failure"] = (
                    original_failure_payload
                )
            return _auto_dive_preflight_result(
                diagnostics,
                status=AUTO_DIVE_PREFLIGHT_FAILED,
                reason=f"route_collision:{full_failure.reason}",
                navigation_route_id=route_id,
                terminal_point=terminal_point,
                start_graph_key=start_key,
                terminal_graph_key=terminal_key,
                details=failure_details,
                coverage_incomplete=coverage_incomplete,
            )

    route_points, route_cells = _preflight_bounded_route_geometry(
        full_route_points,
        full_route_cells,
        max_keyframes=int(settings.max_keyframes),
    )
    if route_points is None or route_cells is None:
        return _auto_dive_preflight_result(
            diagnostics,
            status=AUTO_DIVE_PREFLIGHT_FAILED,
            reason="route_exceeds_keyframe_budget_without_safe_shortcut",
            navigation_route_id=route_id,
            terminal_point=terminal_point,
            start_graph_key=start_key,
            terminal_graph_key=terminal_key,
            details={
                **terminal_details,
                **graph_search_details,
                "full_route_point_count": len(full_route_points),
                "max_keyframes": int(settings.max_keyframes),
            },
            coverage_incomplete=coverage_incomplete,
        )
    bounded_failure = graph_safety_validator.route_clearance_failure(
        route_points,
        graph_keys,
    )
    if bounded_failure is not None:
        return _auto_dive_preflight_result(
            diagnostics,
            status=AUTO_DIVE_PREFLIGHT_FAILED,
            reason=f"bounded_route_collision:{bounded_failure.reason}",
            navigation_route_id=route_id,
            terminal_point=terminal_point,
            start_graph_key=start_key,
            terminal_graph_key=terminal_key,
            details={
                **terminal_details,
                **graph_search_details,
                "collision_failure": _preflight_clearance_failure_payload(
                    bounded_failure
                ),
            },
            coverage_incomplete=coverage_incomplete,
        )

    prefetched_chunk_ids = cached_volume.prefetch_for_points(route_points)
    route_length_m = path_length(route_points)
    duration_s = route_length_m / float(settings.speed_m_per_second)
    keyframe_payloads = route_keyframes_for_points(
        route_points,
        duration_s=duration_s,
        lookahead_distance_m=max(0.0, float(settings.lookahead_distance_m)),
    )
    # Probe roll is an analysis dimension only. Camera execution is always
    # roll-independent, including the initial preflight handoff.
    for payload in keyframe_payloads:
        payload["roll_deg"] = 0.0
    keyframes = [
        RouteKeyframe.from_mapping(payload, index=index)
        for index, payload in enumerate(keyframe_payloads)
    ]
    plan = AutoDivePlan(
        route=CameraRoute.from_keyframes(keyframes),
        centerline_path=None,
        route_points=route_points,
        route_cells=route_cells,
        circular_arc=False,
        route_length_m=route_length_m,
        duration_s=duration_s,
        render_distance_cells=max(1, int(settings.render_distance_cells)),
        selection_reason=(
            "preflight_mesh_safe_graph_frontier"
            if mesh_safe_frontier
            else "preflight_farthest_graph_terminal_true_3d"
        ),
        replan_at_end=mesh_safe_frontier,
        voxel_route_selection={
            "method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "route_geometry_source": "preflight_global_true_3d_graph",
            "start_key": [int(value) for value in start_key],
            "terminal_key": [int(value) for value in terminal_key],
            "terminal_rule": terminal_details.get("terminal_rule"),
            "terminal_selection_source": terminal_details.get(
                "terminal_selection_source"
            ),
            "terminal_graph_distance_m": terminal_details.get(
                "terminal_graph_distance_m"
            ),
            "terminal_unknown_boundary": bool(
                terminal_details.get("terminal_unknown_boundary", False)
            ),
            "graph_path_key_count": len(graph_keys),
            "graph_geometry_key_count": len(graph_keys),
            "full_route_point_count": len(full_route_points),
            "route_point_count": len(route_points),
            "prefetched_chunk_count": len(prefetched_chunk_ids),
            "coverage_incomplete": coverage_incomplete,
            "mesh_safe_frontier_fallback": mesh_safe_frontier,
        },
        terminal_reached=not mesh_safe_frontier,
        navigation_route_id=route_id,
        preflight_validated=True,
        navigation_atlas=cached_volume,
        navigation_graph=graph,
        navigation_graph_keys=tuple(graph_keys),
    )
    return _auto_dive_preflight_result(
        diagnostics,
        status=AUTO_DIVE_PREFLIGHT_READY,
        reason=(
            "validated_mesh_safe_graph_frontier_route"
            if mesh_safe_frontier
            else "validated_farthest_graph_terminal_route"
        ),
        plan=plan,
        navigation_route_id=route_id,
        terminal_point=terminal_point,
        start_graph_key=start_key,
        terminal_graph_key=terminal_key,
        route_point_count=len(route_points),
        coverage_incomplete=coverage_incomplete,
        details={
            **terminal_details,
            **graph_search_details,
            "start_component_id": component_id,
            "start_snap_distance_m": float(start_distance_m),
            "snap_tolerance_m": float(snap_tolerance_m),
            "prefetched_chunk_count": len(prefetched_chunk_ids),
        },
    )


def _auto_dive_preflight_result(
    diagnostics: AutoDiveDiagnosticSink | None,
    *,
    status: str,
    reason: str,
    plan: AutoDivePlan | None = None,
    navigation_route_id: str | None = None,
    terminal_point: Point | None = None,
    terminal_graph_key: VoxelGraphKey | None = None,
    start_graph_key: VoxelGraphKey | None = None,
    route_point_count: int = 0,
    coverage_incomplete: bool = False,
    details: Mapping[str, Any] | None = None,
) -> AutoDivePreflightResult:
    result = AutoDivePreflightResult(
        status=status,
        reason=reason,
        plan=plan,
        navigation_route_id=navigation_route_id,
        terminal_point=terminal_point,
        terminal_graph_key=terminal_graph_key,
        start_graph_key=start_graph_key,
        route_point_count=int(route_point_count),
        coverage_incomplete=bool(coverage_incomplete),
        details=dict(details or {}),
    )
    _record_auto_dive_diagnostic(
        diagnostics,
        "auto_dive_preflight",
        result.diagnostic_payload(),
    )
    return result


def _longest_navigation_route_id(manifest: Mapping[str, Any]) -> str | None:
    navigation = manifest.get(NAVIGATION_METADATA_KEY)
    routes = navigation.get("routes") if isinstance(navigation, Mapping) else None
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        return None
    candidates: list[tuple[float, str]] = []
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        route_id = route.get("id")
        try:
            length_m = float(route.get("length_m"))
        except (TypeError, ValueError):
            continue
        if (
            not isinstance(route_id, str)
            or not route_id
            or not math.isfinite(length_m)
            or length_m <= 0.0
        ):
            continue
        candidates.append((length_m, route_id))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[1]


def _preflight_select_graph_terminal(
    graph: NavigationVoxel3DGraph,
    *,
    start_key: VoxelGraphKey,
    component_id: int,
) -> tuple[VoxelGraphKey | None, dict[str, Any]]:
    """Select the farthest reachable terminal/frontier in graph space.

    ``terminal`` identifies a known local topological endpoint. An
    ``unknown_boundary`` node is also an eligible frontier: it is the end of
    the prepared evidence, not proof that the cave ends there. Both are
    evaluated by accumulated directed graph distance from the starting node,
    so a centerline endpoint or a Cartesian coordinate cannot choose the
    startup destination.
    """
    candidates = tuple(
        key
        for key, node in graph.nodes.items()
        if int(node.component_id) == int(component_id)
        and (bool(node.terminal) or bool(node.unknown_boundary))
    )
    terminal_candidate_count = sum(
        1
        for key in candidates
        if bool(graph.nodes[key].terminal)
        and not bool(graph.nodes[key].unknown_boundary)
    )
    unknown_candidate_count = sum(
        1 for key in candidates if bool(graph.nodes[key].unknown_boundary)
    )
    base_details = {
        "terminal_candidate_count": len(candidates),
        "terminal_candidate_terminal_count": terminal_candidate_count,
        "terminal_candidate_unknown_boundary_count": unknown_candidate_count,
    }
    if not candidates:
        return None, {
            **base_details,
            "reason": "prepared_graph_terminal_candidates_missing",
        }

    distances: dict[VoxelGraphKey, float] = {start_key: 0.0}
    queue: list[tuple[float, VoxelGraphKey]] = [(0.0, start_key)]
    expanded_count = 0
    max_expansions = max(len(graph.nodes) * 4, graph.edge_count + 1)
    while queue:
        distance_m, current_key = heapq.heappop(queue)
        if distance_m > distances.get(current_key, math.inf) + 1e-9:
            continue
        expanded_count += 1
        if expanded_count > max_expansions:
            return None, {
                **base_details,
                "reason": "graph_terminal_search_expansion_limit",
                "expanded_terminal_search_count": expanded_count,
                "terminal_search_max_expansions": max_expansions,
            }
        current_node = graph.nodes.get(current_key)
        if current_node is None:
            continue
        for edge in graph.outgoing(current_key):
            target_node = graph.nodes.get(edge.target)
            edge_distance_m = float(edge.distance_m)
            if (
                target_node is None
                or not edge.line_of_sight
                or edge.source != current_key
                or int(target_node.component_id) != int(component_id)
                or not math.isfinite(edge_distance_m)
                or edge_distance_m <= 0.0
            ):
                continue
            next_distance_m = distance_m + edge_distance_m
            if next_distance_m + 1e-9 >= distances.get(
                edge.target,
                math.inf,
            ):
                continue
            distances[edge.target] = next_distance_m
            heapq.heappush(queue, (next_distance_m, edge.target))

    reachable_candidates = tuple(
        key for key in candidates if key in distances
    )
    if not reachable_candidates:
        return None, {
            **base_details,
            "reason": "no_reachable_graph_terminal_candidate",
            "expanded_terminal_search_count": expanded_count,
            "terminal_search_max_expansions": max_expansions,
        }

    selected = max(
        reachable_candidates,
        key=lambda key: (
            distances[key],
            bool(
                graph.nodes[key].terminal
                and not graph.nodes[key].unknown_boundary
            ),
            key,
        ),
    )
    selected_node = graph.nodes[selected]
    selected_is_unknown = bool(selected_node.unknown_boundary)
    return selected, {
        **base_details,
        "reason": "farthest_reachable_graph_terminal_selected",
        "terminal_rule": (
            "farthest_reachable_true_3d_graph_frontier"
            if selected_is_unknown
            else "farthest_reachable_true_3d_graph_terminal"
        ),
        "terminal_selection_source": (
            "unknown_boundary_frontier"
            if selected_is_unknown
            else "graph_terminal"
        ),
        "terminal_reachable_candidate_count": len(reachable_candidates),
        "terminal_graph_distance_m": float(distances[selected]),
        "terminal_unknown_boundary": selected_is_unknown,
        "terminal_local_degree": int(selected_node.local_degree),
        "terminal_dead_end": bool(selected_node.dead_end),
        "terminal_clearance_m": float(selected_node.min_clearance_m),
        "expanded_terminal_search_count": expanded_count,
        "terminal_search_max_expansions": max_expansions,
    }


def _point_distance_squared(first: Point, second: Point) -> float:
    return sum((float(first[index]) - float(second[index])) ** 2 for index in range(3))


def _point_alignment(
    source: Point,
    target: Point,
    direction: np.ndarray,
) -> float:
    delta = np.asarray(target, dtype=np.float64) - np.asarray(source, dtype=np.float64)
    norm = float(np.linalg.norm(delta))
    if norm <= 1e-9:
        return 0.0
    return float(np.dot(delta / norm, direction))


def _preflight_graph_snap_tolerance_m(
    graph: NavigationVoxel3DGraph,
    *,
    cached_volume: NavigationVoxelAtlas,
) -> float:
    graph_scale = max((float(value) for value in graph.grid_size_m), default=0.0)
    return max(
        2.0 * graph_scale,
        2.0 * max(0.0, float(cached_volume.voxel_size_m)),
        1.0,
    )


def _preflight_nearest_graph_key(
    graph: NavigationVoxel3DGraph,
    point: Point,
    *,
    component_id: int | None = None,
    routable_only: bool = False,
) -> tuple[VoxelGraphKey | None, float]:
    candidates: list[VoxelGraphKey] = []
    for key, node in graph.nodes.items():
        if component_id is not None and int(node.component_id) != int(component_id):
            continue
        if routable_only and not any(
            edge.line_of_sight
            and edge.target in graph.nodes
            and int(graph.nodes[edge.target].component_id) == int(node.component_id)
            for edge in graph.outgoing(key)
        ):
            continue
        candidates.append(key)
    if not candidates:
        return None, math.inf
    selected = min(
        candidates,
        key=lambda key: (_point_distance_squared(graph.nodes[key].center, point), key),
    )
    return selected, math.sqrt(
        _point_distance_squared(graph.nodes[selected].center, point)
    )


def _preflight_global_graph_route(
    graph: NavigationVoxel3DGraph,
    *,
    start_key: VoxelGraphKey,
    terminal_key: VoxelGraphKey,
    preferred_direction: np.ndarray | None,
) -> tuple[tuple[VoxelGraphKey, ...] | None, dict[str, Any]]:
    """Run a bounded graph-wide Dijkstra search to the preflight terminal."""
    if start_key == terminal_key:
        return (start_key,), {"expanded_state_count": 0, "reason": "same_node"}

    scale = max((float(value) for value in graph.grid_size_m), default=1.0)
    start_state: tuple[VoxelGraphKey, VoxelGraphKey | None] = (start_key, None)
    distances: dict[tuple[VoxelGraphKey, VoxelGraphKey | None], float] = {
        start_state: 0.0
    }
    parents: dict[
        tuple[VoxelGraphKey, VoxelGraphKey | None],
        tuple[VoxelGraphKey, VoxelGraphKey | None] | None,
    ] = {start_state: None}
    queue: list[
        tuple[float, int, VoxelGraphKey, VoxelGraphKey | None]
    ] = [(0.0, 0, start_key, None)]
    serial = 0
    expanded_count = 0
    max_expansions = max(len(graph.nodes) * 4, graph.edge_count + 1)
    goal_state: tuple[VoxelGraphKey, VoxelGraphKey | None] | None = None
    while queue:
        cost, _serial, current_key, previous_key = heapq.heappop(queue)
        state = (current_key, previous_key)
        if cost > distances.get(state, math.inf) + 1e-9:
            continue
        expanded_count += 1
        if current_key == terminal_key:
            goal_state = state
            break
        if expanded_count > max_expansions:
            return None, {
                "reason": "graph_search_expansion_limit",
                "expanded_state_count": expanded_count,
                "max_expansions": max_expansions,
            }
        current_node = graph.nodes.get(current_key)
        if current_node is None:
            continue
        incoming_direction: np.ndarray | None = None
        if previous_key is not None:
            incoming_edge = _preflight_graph_edge_between(
                graph,
                previous_key,
                current_key,
            )
            if incoming_edge is not None:
                incoming_direction = np.asarray(
                    incoming_edge.direction,
                    dtype=np.float64,
                )
        for edge in graph.outgoing(current_key):
            target_node = graph.nodes.get(edge.target)
            if (
                target_node is None
                or not edge.line_of_sight
                or int(target_node.component_id) != int(current_node.component_id)
            ):
                continue
            direction = np.asarray(edge.direction, dtype=np.float64)
            alignment = 1.0
            if incoming_direction is not None:
                alignment = float(np.dot(incoming_direction, direction))
            elif preferred_direction is not None:
                alignment = float(np.dot(preferred_direction, direction))
            alignment = max(-1.0, min(1.0, alignment))
            edge_cost = (
                float(edge.distance_m)
                + scale * 0.20 * (1.0 - alignment)
                + scale
                * 0.35
                / (1.0 + max(0.0, float(edge.min_clearance_m)))
                + scale
                * 0.10
                / (1.0 + max(0.0, float(target_node.connectivity_score)))
            )
            next_state = (edge.target, current_key)
            next_cost = cost + edge_cost
            if next_cost + 1e-9 >= distances.get(next_state, math.inf):
                continue
            distances[next_state] = next_cost
            parents[next_state] = state
            serial += 1
            heapq.heappush(
                queue,
                (next_cost, serial, edge.target, current_key),
            )

    if goal_state is None:
        return None, {
            "reason": "no_graph_route_to_longest_terminal",
            "expanded_state_count": expanded_count,
            "max_expansions": max_expansions,
        }
    states = [goal_state]
    while parents[states[-1]] is not None:
        parent = parents[states[-1]]
        assert parent is not None
        states.append(parent)
    states.reverse()
    keys = tuple(state[0] for state in states)
    return keys, {
        "reason": "global_graph_route_found",
        "expanded_state_count": expanded_count,
        "max_expansions": max_expansions,
        "graph_route_cost": float(distances[goal_state]),
        "graph_path_key_count": len(keys),
    }


def _preflight_mesh_safe_graph_frontier(
    graph: NavigationVoxel3DGraph,
    *,
    start_key: VoxelGraphKey,
    component_id: int,
    graph_safety_validator: GraphRouteSafetyValidator,
) -> tuple[tuple[VoxelGraphKey, ...] | None, VoxelGraphKey | None, dict[str, Any]]:
    """Find the farthest graph frontier reachable through safe graph edges.

    The graph terminal search deliberately reasons about prepared topology,
    but a cached mesh can invalidate one or more graph edges.  When the
    requested longest route is mesh-blocked, preflight may authorize only the
    farthest mesh-safe frontier.  The controller then performs its normal
    continuous scan at that frontier; it never treats the shortened route as
    proof that the cave ends there.
    """
    candidates = tuple(
        key
        for key, node in graph.nodes.items()
        if key != start_key
        and int(node.component_id) == int(component_id)
        and (bool(node.terminal) or bool(node.unknown_boundary))
    )
    base_details = {
        "mesh_safe_frontier_candidate_count": len(candidates),
        "mesh_safe_frontier_terminal_count": sum(
            1
            for key in candidates
            if bool(graph.nodes[key].terminal)
            and not bool(graph.nodes[key].unknown_boundary)
        ),
        "mesh_safe_frontier_unknown_boundary_count": sum(
            1 for key in candidates if bool(graph.nodes[key].unknown_boundary)
        ),
    }
    if not candidates:
        return None, None, {
            **base_details,
            "mesh_safe_frontier_reason": "mesh_safe_frontier_candidates_missing",
        }

    distances: dict[VoxelGraphKey, float] = {start_key: 0.0}
    parents: dict[VoxelGraphKey, VoxelGraphKey | None] = {start_key: None}
    queue: list[tuple[float, VoxelGraphKey]] = [(0.0, start_key)]
    edge_safety_cache: dict[
        tuple[VoxelGraphKey, VoxelGraphKey], GraphRouteSafetyFailure | None
    ] = {}
    expanded_count = 0
    safe_edge_count = 0
    rejected_edge_count = 0
    max_expansions = max(len(graph.nodes) * 4, graph.edge_count + 1)
    while queue:
        distance_m, current_key = heapq.heappop(queue)
        if distance_m > distances.get(current_key, math.inf) + 1e-9:
            continue
        expanded_count += 1
        if expanded_count > max_expansions:
            return None, None, {
                **base_details,
                "mesh_safe_frontier_reason": "mesh_safe_frontier_search_expansion_limit",
                "expanded_mesh_safe_frontier_count": expanded_count,
                "mesh_safe_frontier_search_max_expansions": max_expansions,
                "mesh_safe_edge_count": safe_edge_count,
                "mesh_rejected_edge_count": rejected_edge_count,
            }
        current_node = graph.nodes.get(current_key)
        if current_node is None:
            continue
        for edge in graph.outgoing(current_key):
            target_node = graph.nodes.get(edge.target)
            if (
                target_node is None
                or not edge.line_of_sight
                or edge.source != current_key
                or int(target_node.component_id) != int(component_id)
            ):
                continue
            edge_key = (current_key, edge.target)
            failure = edge_safety_cache.get(edge_key)
            if edge_key not in edge_safety_cache:
                failure = graph_safety_validator.edge_clearance_failure(
                    current_key,
                    edge.target,
                )
                edge_safety_cache[edge_key] = failure
            if failure is not None:
                rejected_edge_count += 1
                continue
            safe_edge_count += 1
            next_distance_m = distance_m + float(edge.distance_m)
            if next_distance_m + 1e-9 >= distances.get(
                edge.target,
                math.inf,
            ):
                continue
            distances[edge.target] = next_distance_m
            parents[edge.target] = current_key
            heapq.heappush(queue, (next_distance_m, edge.target))

    reachable_candidates = tuple(
        key for key in candidates if key in distances
    )
    if not reachable_candidates:
        return None, None, {
            **base_details,
            "mesh_safe_frontier_reason": "no_mesh_safe_graph_frontier_reachable",
            "expanded_mesh_safe_frontier_count": expanded_count,
            "mesh_safe_frontier_search_max_expansions": max_expansions,
            "mesh_safe_edge_count": safe_edge_count,
            "mesh_rejected_edge_count": rejected_edge_count,
        }

    selected = max(
        reachable_candidates,
        key=lambda key: (
            distances[key],
            bool(
                graph.nodes[key].terminal
                and not graph.nodes[key].unknown_boundary
            ),
            key,
        ),
    )
    path: list[VoxelGraphKey] = [selected]
    while path[-1] != start_key:
        parent = parents.get(path[-1])
        if parent is None:
            return None, None, {
                **base_details,
                "mesh_safe_frontier_reason": "mesh_safe_frontier_parent_missing",
                "expanded_mesh_safe_frontier_count": expanded_count,
                "mesh_safe_edge_count": safe_edge_count,
                "mesh_rejected_edge_count": rejected_edge_count,
            }
        path.append(parent)
    path.reverse()
    selected_node = graph.nodes[selected]
    selected_is_unknown = bool(selected_node.unknown_boundary)
    return tuple(path), selected, {
        **base_details,
        "reason": "mesh_safe_graph_frontier_selected",
        "terminal_rule": (
            "farthest_mesh_safe_true_3d_graph_frontier"
            if selected_is_unknown
            else "farthest_mesh_safe_true_3d_graph_terminal"
        ),
        "terminal_selection_source": (
            "mesh_safe_unknown_boundary_frontier"
            if selected_is_unknown
            else "mesh_safe_graph_terminal"
        ),
        "terminal_reachable_candidate_count": len(reachable_candidates),
        "terminal_graph_distance_m": float(distances[selected]),
        "terminal_unknown_boundary": selected_is_unknown,
        "terminal_local_degree": int(selected_node.local_degree),
        "terminal_dead_end": bool(selected_node.dead_end),
        "terminal_clearance_m": float(selected_node.min_clearance_m),
        "expanded_terminal_search_count": expanded_count,
        "terminal_search_max_expansions": max_expansions,
        "mesh_safe_edge_count": safe_edge_count,
        "mesh_rejected_edge_count": rejected_edge_count,
        "mesh_safe_frontier_reachable_node_count": len(distances),
        "graph_path_key_count": len(path),
    }


def _preflight_graph_edge_between(
    graph: NavigationVoxel3DGraph,
    source: VoxelGraphKey,
    target: VoxelGraphKey,
) -> NavigationVoxel3DEdge | None:
    return next(
        (
            edge
            for edge in graph.outgoing(source)
            if edge.target == target and edge.line_of_sight
        ),
        None,
    )


def _preflight_route_geometry(
    current: Point,
    keys: Sequence[VoxelGraphKey],
    graph: NavigationVoxel3DGraph,
) -> tuple[tuple[Point, ...], tuple[FootprintCell, ...]]:
    points: list[Point] = [current]
    cells: list[FootprintCell] = []
    for key in keys:
        node = graph.nodes.get(key)
        if node is None:
            continue
        point = tuple(float(value) for value in node.center)
        if _point_distance_squared(points[-1], point) <= 1e-12:
            continue
        points.append(point)
        cells.append(node.footprint_cell)
    return tuple(points), tuple(cells)


def _preflight_bounded_route_geometry(
    points: tuple[Point, ...],
    cells: tuple[FootprintCell, ...],
    *,
    max_keyframes: int,
) -> tuple[tuple[Point, ...] | None, tuple[FootprintCell, ...] | None]:
    if len(points) <= max_keyframes:
        return points, cells
    stride = max(1, int(math.ceil((len(points) - 1) / max(1, max_keyframes - 1))))
    indices = list(range(0, len(points), stride))
    if indices[-1] != len(points) - 1:
        indices.append(len(points) - 1)
    if len(indices) > max_keyframes:
        return None, None
    bounded_points = tuple(points[index] for index in indices)
    bounded_cells = tuple(
        cells[index - 1]
        for index in indices
        if index > 0 and index - 1 < len(cells)
    )
    return bounded_points, bounded_cells


def _preflight_clearance_failure_payload(
    failure: _AutoDiveClearanceFailure | GraphRouteSafetyFailure,
) -> dict[str, Any]:
    if isinstance(failure, GraphRouteSafetyFailure):
        return failure.diagnostic_payload()
    return {
        "kind": str(failure.kind),
        "reason": str(failure.reason),
        "index": failure.index,
        "segment_index": failure.segment_index,
        "cell": (
            None
            if failure.cell is None
            else [int(value) for value in failure.cell]
        ),
        "chunk_cell": (
            None
            if failure.chunk_cell is None
            else [int(value) for value in failure.chunk_cell]
        ),
        "point": (
            None
            if failure.point is None
            else [float(value) for value in failure.point]
        ),
    }


def _validate_auto_dive_settings(settings: AutoDiveSettings) -> None:
    if int(settings.render_distance_cells) <= 0:
        raise NavigationConfigurationError("Guided Dive render distance must be positive")
    speed = float(settings.speed_m_per_second)
    if not math.isfinite(speed) or speed <= 0.0:
        raise NavigationConfigurationError("Guided Dive speed must be positive")
    vertical_fraction = float(settings.vertical_position_fraction)
    if (
        not math.isfinite(vertical_fraction)
        or not 0.0 <= vertical_fraction <= 1.0
    ):
        raise NavigationConfigurationError(
            "Guided Dive vertical position fraction must be between 0 and 1"
        )
    gap = float(settings.closed_loop_gap_fraction)
    if not math.isfinite(gap) or not 0.0 < gap < 1.0:
        raise NavigationConfigurationError(
            "Guided Dive closed-loop gap fraction must be between 0 and 1"
        )
    if int(settings.max_keyframes) < 2:
        raise NavigationConfigurationError("Guided Dive requires at least 2 keyframes")
    if int(settings.smoothing_radius_cells) < 0:
        raise NavigationConfigurationError(
            "Guided Dive smoothing radius cannot be negative"
        )
    lookahead = float(settings.lookahead_distance_m)
    if not math.isfinite(lookahead) or lookahead < 0.0:
        raise NavigationConfigurationError(
            "Guided Dive look-ahead distance cannot be negative"
        )
    voxel_size = float(settings.voxel_size_m)
    if not math.isfinite(voxel_size) or voxel_size <= 0.0:
        raise NavigationConfigurationError(
            "Guided Dive voxel size must be positive"
        )
    voxel_rank = int(settings.voxel_curvature_rank_threshold)
    if not 0 <= voxel_rank <= 100:
        raise NavigationConfigurationError(
            "Guided Dive voxel curvature rank must be between 0 and 100"
        )
    if int(settings.voxel_max_regions) < 0:
        raise NavigationConfigurationError(
            "Guided Dive voxel region count cannot be negative"
        )
    voxel_distance = float(settings.voxel_max_distance_m)
    if not math.isfinite(voxel_distance) or voxel_distance <= 0.0:
        raise NavigationConfigurationError(
            "Guided Dive voxel analysis distance must be positive"
        )
    if int(settings.voxel_max_cells) <= 0:
        raise NavigationConfigurationError(
            "Guided Dive voxel cell budget must be positive"
        )
    local_radius = float(settings.voxel_local_refinement_radius_m)
    if not math.isfinite(local_radius) or local_radius <= 0.0:
        raise NavigationConfigurationError(
            "Guided Dive local voxel refinement radius must be positive"
        )
    local_forward = float(settings.voxel_local_refinement_forward_m)
    if not math.isfinite(local_forward) or local_forward <= 0.0:
        raise NavigationConfigurationError(
            "Guided Dive local voxel refinement distance must be positive"
        )
    if int(settings.voxel_local_refinement_max_cells) <= 0:
        raise NavigationConfigurationError(
            "Guided Dive local voxel refinement cell budget must be positive"
        )
    if settings.planning_budget_s is not None:
        planning_budget = float(settings.planning_budget_s)
        if not math.isfinite(planning_budget) or planning_budget <= 0.0:
            raise NavigationConfigurationError(
                "Guided Dive planning budget must be positive"
            )
    minimum_graph_clearance = float(settings.minimum_graph_clearance_m)
    if (
        not math.isfinite(minimum_graph_clearance)
        or minimum_graph_clearance < 0.0
    ):
        raise NavigationConfigurationError(
            "Guided Dive minimum graph clearance must be finite and non-negative"
        )
    if not isinstance(
        settings.voxel_scoring_policy,
        NavigationVoxelScoringPolicy,
    ):
        raise NavigationConfigurationError(
            "Guided Dive voxel scoring policy is invalid"
        )


def _wall_aware_auto_dive_keyframe_payloads(
    keyframe_payloads: list[dict[str, Any]],
    *,
    route_points: tuple[Point, ...],
    centerline_path: CenterlinePath,
    settings: AutoDiveSettings,
    collision_validator: _AutoDiveCollisionValidator,
    planning_budget: _AutoDivePlanningBudget | None = None,
    diagnostics: AutoDiveDiagnosticSink | None = None,
) -> list[dict[str, Any]]:
    if len(keyframe_payloads) != len(route_points):
        return keyframe_payloads
    if len(route_points) < 2 or not collision_validator.component_cells:
        return keyframe_payloads

    cell_size = float(centerline_path.footprint_cell_size)
    steering_distance_m = max(
        float(settings.lookahead_distance_m),
        cell_size * 2.0,
    )
    steered: list[dict[str, Any]] = []
    for index, payload in enumerate(keyframe_payloads):
        if planning_budget is not None:
            planning_budget.check("keyframe_steering", diagnostics=diagnostics)
        try:
            base_yaw = float(payload["yaw_deg"])
            base_pitch = float(payload["pitch_deg"])
        except (KeyError, TypeError, ValueError):
            steered.append(dict(payload))
            continue
        yaw_deg, pitch_deg = _wall_aware_look_angles(
            route_points[index],
            base_yaw_deg=base_yaw,
            base_pitch_deg=base_pitch,
            distance_m=steering_distance_m,
            collision_validator=collision_validator,
            planning_budget=planning_budget,
            diagnostics=diagnostics,
        )
        updated = dict(payload)
        updated["yaw_deg"] = round(yaw_deg, 6)
        updated["pitch_deg"] = round(pitch_deg, 6)
        steered.append(updated)
    return steered


def _wall_aware_look_angles(
    source: Point,
    *,
    base_yaw_deg: float,
    base_pitch_deg: float,
    distance_m: float,
    collision_validator: _AutoDiveCollisionValidator,
    planning_budget: _AutoDivePlanningBudget | None = None,
    diagnostics: AutoDiveDiagnosticSink | None = None,
) -> tuple[float, float]:
    yaw_offsets = (0.0, -6.0, 6.0, -12.0, 12.0, -20.0, 20.0)
    pitch_offsets = (0.0, -4.0, 4.0, -8.0, 8.0)
    best_score: tuple[object, ...] | None = None
    best_angles = (float(base_yaw_deg), float(base_pitch_deg))
    default_score: tuple[object, ...] | None = None
    for yaw_offset in yaw_offsets:
        for pitch_offset in pitch_offsets:
            if planning_budget is not None:
                planning_budget.check(
                    "keyframe_look_search",
                    diagnostics=diagnostics,
                )
            yaw = float(base_yaw_deg) + yaw_offset
            pitch = max(-55.0, min(55.0, float(base_pitch_deg) + pitch_offset))
            direction = _direction_from_yaw_pitch(yaw, pitch)
            score = _look_direction_clearance_score(
                source,
                direction=direction,
                distance_m=distance_m,
                yaw_offset_deg=yaw_offset,
                pitch_offset_deg=pitch_offset,
                collision_validator=collision_validator,
            )
            if yaw_offset == 0.0 and pitch_offset == 0.0:
                default_score = score
            if best_score is None or score > best_score:
                best_score = score
                best_angles = (yaw, pitch)
    if default_score is not None and best_score is not None:
        if not _steered_look_is_materially_better(best_score, default_score):
            return float(base_yaw_deg), float(base_pitch_deg)
    return best_angles


def _steered_look_is_materially_better(
    candidate_score: tuple[object, ...],
    default_score: tuple[object, ...],
) -> bool:
    if bool(candidate_score[0]) and not bool(default_score[0]):
        return True
    if bool(candidate_score[0]) != bool(default_score[0]):
        return False
    candidate_clear_fraction = float(candidate_score[1])
    default_clear_fraction = float(default_score[1])
    if candidate_clear_fraction > default_clear_fraction + 0.10:
        return True
    if candidate_clear_fraction + 1e-9 < default_clear_fraction:
        return False
    candidate_margin = float(candidate_score[2])
    default_margin = float(default_score[2])
    if candidate_margin > default_margin + 0.25:
        return True
    if candidate_margin + 1e-9 < default_margin:
        return False
    candidate_lateral = int(candidate_score[3])
    default_lateral = int(default_score[3])
    if candidate_lateral > default_lateral:
        return True
    if candidate_lateral < default_lateral:
        return False
    return float(candidate_score[4]) > float(default_score[4]) + 0.5


def _direction_from_yaw_pitch(
    yaw_deg: float,
    pitch_deg: float,
) -> np.ndarray:
    yaw = math.radians(float(yaw_deg))
    pitch = math.radians(float(pitch_deg))
    horizontal = math.cos(pitch)
    return np.asarray(
        (
            math.cos(yaw) * horizontal,
            math.sin(pitch),
            math.sin(yaw) * horizontal,
        ),
        dtype=np.float64,
    )


def _direction_from_radians(
    yaw: float | None,
    pitch: float | None,
) -> np.ndarray | None:
    """Convert the planner's radian camera pose into a unit look vector."""
    if yaw is None or pitch is None:
        return None
    try:
        yaw_value = float(yaw)
        pitch_value = float(pitch)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(yaw_value) or not math.isfinite(pitch_value):
        return None
    horizontal = math.cos(pitch_value)
    return np.asarray(
        (
            math.cos(yaw_value) * horizontal,
            math.sin(pitch_value),
            math.sin(yaw_value) * horizontal,
        ),
        dtype=np.float64,
    )


def _look_direction_clearance_score(
    source: Point,
    *,
    direction: np.ndarray,
    distance_m: float,
    yaw_offset_deg: float,
    pitch_offset_deg: float,
    collision_validator: _AutoDiveCollisionValidator,
) -> tuple[object, ...]:
    norm = float(np.linalg.norm(direction))
    if norm <= 1e-9:
        return (False, 0.0, 0, 0.0, 0.0)
    unit = direction / norm
    cell_size = collision_validator.cell_size
    distance = max(cell_size * 0.5, float(distance_m))
    steps = max(2, int(math.ceil(distance / max(1e-9, cell_size * 0.25))))
    source_array = np.asarray(source, dtype=np.float64)
    clear = True
    min_lateral_score = math.inf
    total_lateral_score = 0.0
    min_margin_m = math.inf
    sample_count = 0
    clear_prefix = 0
    for step in range(1, steps + 1):
        point_array = source_array + unit * (distance * step / steps)
        point: Point = (
            float(point_array[0]),
            float(point_array[1]),
            float(point_array[2]),
        )
        cell = _footprint_cell_for_xz(
            (point[0], point[2]),
            cell_size,
        )
        lateral_score = int(
            collision_validator.centerline_path.clearance_scores.get(cell, 0)
        )
        margin = _look_point_clearance_margin_m(
            point,
            collision_validator=collision_validator,
        )
        point_clear = collision_validator.point_is_clear(point)
        if not point_clear:
            clear = False
        else:
            clear_prefix += 1
        min_lateral_score = min(min_lateral_score, lateral_score)
        total_lateral_score += lateral_score
        min_margin_m = min(min_margin_m, margin)
        sample_count += 1

    if sample_count <= 0:
        return (False, 0.0, 0, 0.0, 0.0)
    avg_lateral_score = total_lateral_score / sample_count
    clear_fraction = clear_prefix / sample_count
    if not math.isfinite(min_margin_m):
        min_margin_m = 0.0
    if not math.isfinite(min_lateral_score):
        min_lateral_score = 0
    angle_penalty = abs(float(yaw_offset_deg)) + abs(float(pitch_offset_deg)) * 0.5
    return (
        bool(clear),
        round(clear_fraction, 3),
        round(float(min_margin_m), 3),
        int(min_lateral_score),
        round(float(avg_lateral_score), 3),
        -round(angle_penalty, 3),
    )


def _look_point_clearance_margin_m(
    point: Point,
    *,
    collision_validator: _AutoDiveCollisionValidator,
) -> float:
    cell = _footprint_cell_for_xz(
        (point[0], point[2]),
        collision_validator.cell_size,
    )
    lateral_score = _lateral_clearance_score_at_point(
        point,
        centerline_path=collision_validator.centerline_path,
    )
    lateral_margin = collision_validator.cached_clearance_margins.get(cell)
    if lateral_margin is None:
        lateral_margin = max(
            0.0,
            (float(lateral_score) - 0.5) * collision_validator.cell_size,
        )
    y_range = collision_validator.cached_y_ranges.get(cell)
    if y_range is None:
        return float(lateral_margin)
    low_y, high_y = float(y_range[0]), float(y_range[1])
    y = float(point[1])
    if y < low_y:
        return min(float(lateral_margin), y - low_y)
    if y > high_y:
        return min(float(lateral_margin), high_y - y)
    vertical_margin = min(y - low_y, high_y - y)
    return min(float(lateral_margin), float(vertical_margin))


def _nearest_centerline_index(
    centerline_path: CenterlinePath,
    *,
    current_x: float,
    current_z: float,
) -> int:
    return min(
        range(len(centerline_path.cells)),
        key=lambda index: (
            (
                centerline_path.centers[centerline_path.cells[index]][0]
                - current_x
            )
            ** 2
            + (
                centerline_path.centers[centerline_path.cells[index]][1]
                - current_z
            )
            ** 2,
            index,
        ),
    )


def _select_auto_dive_cells(
    centerline_path: CenterlinePath,
    *,
    nearest_index: int,
    closed_loop_gap_fraction: float,
    current: np.ndarray | None = None,
    current_yaw: float | None = None,
    current_pitch: float | None = None,
) -> tuple[tuple[FootprintCell, ...], bool]:
    cells = centerline_path.cells
    if _centerline_cells_form_closed_loop(cells):
        forward = _open_arc_from_closed_loop(
            cells,
            start_index=nearest_index,
            gap_fraction=closed_loop_gap_fraction,
        )
        reverse = _open_arc_from_closed_loop(
            _reversed_centerline_loop_cells(cells),
            start_index=_reversed_loop_start_index(cells, nearest_index),
            gap_fraction=closed_loop_gap_fraction,
        )
        return _select_auto_dive_cells_for_view(
            centerline_path,
            (forward, reverse),
            current=current,
            current_yaw=current_yaw,
            current_pitch=current_pitch,
        ), True

    forward = tuple(cells[nearest_index:])
    reverse = tuple(reversed(cells[: nearest_index + 1]))
    return _select_auto_dive_cells_for_view(
        centerline_path,
        (forward, reverse),
        current=current,
        current_yaw=current_yaw,
        current_pitch=current_pitch,
    ), False


def _select_auto_dive_cells_for_view(
    centerline_path: CenterlinePath,
    candidates: tuple[tuple[FootprintCell, ...], ...],
    *,
    current: np.ndarray | None,
    current_yaw: float | None,
    current_pitch: float | None,
) -> tuple[FootprintCell, ...]:
    viable = tuple(candidate for candidate in candidates if len(candidate) >= 2)
    if not viable:
        return candidates[0] if candidates else ()
    if current is None:
        return viable[0]

    current_point: Point = (
        float(current[0]),
        float(current[1]),
        float(current[2]),
    )
    position_direction = _auto_dive_current_position_offset_direction(
        centerline_path,
        current=np.asarray(current, dtype=np.float64),
        current_point=current_point,
    )
    if current_yaw is None and position_direction is None:
        return viable[0]
    return max(
        viable,
        key=lambda candidate: _auto_dive_cell_direction_score(
            centerline_path,
            candidate,
            current_point=current_point,
            current=np.asarray(current, dtype=np.float64),
            current_yaw=current_yaw,
            current_pitch=current_pitch,
            position_direction=position_direction,
        ),
    )


def _auto_dive_cell_direction_score(
    centerline_path: CenterlinePath,
    cells: tuple[FootprintCell, ...],
    *,
    current_point: Point,
    current: np.ndarray,
    current_yaw: float | None,
    current_pitch: float | None,
    position_direction: np.ndarray | None,
) -> tuple[object, ...]:
    target = _auto_dive_direction_target_point(
        centerline_path,
        cells,
        current=current,
        current_point=current_point,
    )
    if target is None:
        return (False, False, -1.0, -1.0, -1.0, 0.0, -len(cells))
    view_alignment = (
        None
        if current_yaw is None
        else _mesh_recovery_view_alignment(
            current_point,
            target,
            current_yaw=current_yaw,
            current_pitch=current_pitch,
        )
    )
    position_alignment = _auto_dive_target_alignment_from_direction(
        current_point,
        target,
        position_direction,
    )
    alignment = (
        view_alignment
        if view_alignment is not None
        else position_alignment
    )
    if alignment is None:
        return (True, False, -1.0, -1.0, -1.0, 0.0, -len(cells))
    length_m = footprint_path_length(cells, centerline_path.centers)
    return (
        True,
        alignment >= _AUTO_DIVE_MESH_RECOVERY_FORWARD_ALIGNMENT,
        float(alignment),
        -1.0 if position_alignment is None else float(position_alignment),
        -1.0 if view_alignment is None else float(view_alignment),
        float(length_m),
        -len(cells),
    )


def _auto_dive_direction_target_point(
    centerline_path: CenterlinePath,
    cells: tuple[FootprintCell, ...],
    *,
    current: np.ndarray,
    current_point: Point,
) -> Point | None:
    target_cells = _route_cells_after_current_camera_progress(
        centerline_path,
        route_cells=cells,
        current=current,
    )
    threshold_m = max(
        0.5,
        float(centerline_path.footprint_cell_size) * 0.5,
    )
    for cell in target_cells:
        target = _mesh_recovery_point_for_cell(
            centerline_path,
            cell,
            fallback_y=float(current_point[1]),
        )
        if (
            float(
                np.linalg.norm(
                    np.asarray(target, dtype=np.float64)
                    - np.asarray(current_point, dtype=np.float64)
                )
            )
            >= threshold_m
        ):
            return target
    return None


def _auto_dive_current_position_offset_direction(
    centerline_path: CenterlinePath,
    *,
    current: np.ndarray,
    current_point: Point,
) -> np.ndarray | None:
    component_cells = centerline_path.component_cells
    if not component_cells:
        return None
    current_cell = _current_footprint_cell(centerline_path, current)
    if current_cell not in component_cells:
        current_cell = min(
            component_cells,
            key=lambda cell: (
                _cell_center_distance_squared(centerline_path, cell, current),
                cell,
            ),
        )
    anchor = _mesh_recovery_point_for_cell(
        centerline_path,
        current_cell,
        fallback_y=float(current_point[1]),
    )
    offset = (
        np.asarray(current_point, dtype=np.float64)
        - np.asarray(anchor, dtype=np.float64)
    )
    norm = float(np.linalg.norm(offset))
    threshold_m = max(
        0.25,
        float(centerline_path.footprint_cell_size) * 0.25,
    )
    if norm < threshold_m:
        return None
    return offset / norm


def _auto_dive_target_alignment_from_direction(
    current_point: Point,
    target_point: Point,
    direction: np.ndarray | None,
) -> float | None:
    if direction is None:
        return None
    target_vector = (
        np.asarray(target_point, dtype=np.float64)
        - np.asarray(current_point, dtype=np.float64)
    )
    target_norm = float(np.linalg.norm(target_vector))
    direction_norm = float(np.linalg.norm(direction))
    if target_norm <= 1e-9 or direction_norm <= 1e-9:
        return None
    return float(np.dot(target_vector / target_norm, direction / direction_norm))


def _reversed_centerline_loop_cells(
    cells: tuple[FootprintCell, ...],
) -> tuple[FootprintCell, ...]:
    loop_cells = cells[:-1] if cells and cells[0] == cells[-1] else cells
    return tuple(reversed(loop_cells))


def _reversed_loop_start_index(
    cells: tuple[FootprintCell, ...],
    nearest_index: int,
) -> int:
    loop_cells = cells[:-1] if cells and cells[0] == cells[-1] else cells
    if not loop_cells:
        return 0
    bounded_index = max(0, min(len(loop_cells) - 1, int(nearest_index)))
    return len(loop_cells) - 1 - bounded_index


def _centerline_cells_form_closed_loop(
    cells: tuple[FootprintCell, ...],
) -> bool:
    if len(cells) < 4:
        return False
    if cells[0] == cells[-1]:
        return True
    return cells[0] in footprint_neighbors(cells[-1])


def _open_arc_from_closed_loop(
    cells: tuple[FootprintCell, ...],
    *,
    start_index: int,
    gap_fraction: float,
) -> tuple[FootprintCell, ...]:
    loop_cells = cells[:-1] if cells[0] == cells[-1] else cells
    count = len(loop_cells)
    if count < 3:
        return loop_cells
    gap_count = max(1, int(round(count * float(gap_fraction))))
    keep_count = max(2, count - gap_count)
    start_index = max(0, min(count - 1, int(start_index)))
    return tuple(
        loop_cells[(start_index + offset) % count]
        for offset in range(keep_count)
    )


def _route_cells_connected_to_current_camera(
    centerline_path: CenterlinePath,
    *,
    route_cells: tuple[FootprintCell, ...],
    current: np.ndarray,
) -> tuple[FootprintCell, ...]:
    """Connect the selected centerline route to the current camera footprint."""
    component_cells = centerline_path.component_cells
    if not route_cells or not component_cells:
        return route_cells

    current_cell = _current_footprint_cell(centerline_path, current)
    if current_cell not in component_cells:
        current_cell = min(
            component_cells,
            key=lambda cell: (
                _cell_center_distance_squared(centerline_path, cell, current),
                cell,
            ),
        )

    connector = lowest_cost_footprint_path(
        component_cells,
        current_cell,
        route_cells[0],
        centerline_path.clearance_scores,
    )
    if not connector:
        return route_cells
    return _dedupe_consecutive_cells((*connector, *route_cells[1:]))


def _route_cells_after_current_camera_progress(
    centerline_path: CenterlinePath,
    *,
    route_cells: tuple[FootprintCell, ...],
    current: np.ndarray,
) -> tuple[FootprintCell, ...]:
    """Skip the current route cell so replans do not steer backward.

    Guided Dive routes always prepend the exact current camera point later. If a
    frequent replan also keeps the current cell center as the next waypoint,
    the camera can repeatedly steer back into the same local surface/center
    before making progress. Once the camera is inside the first route cell,
    aim at the next cell instead.
    """
    if len(route_cells) < 2:
        return route_cells
    current_cell = _current_footprint_cell(centerline_path, current)
    if current_cell != route_cells[0]:
        return route_cells
    return route_cells[1:]


def _auto_dive_xz_points_for_cells(
    centerline_path: CenterlinePath,
    *,
    route_cells: tuple[FootprintCell, ...],
    settings: AutoDiveSettings,
) -> tuple[PointXZ, ...]:
    """Return bend-preserving X/Z waypoints for an occupied footprint route."""
    waypoint_cells = _waypoint_cells_for_footprint_route(
        route_cells,
        cell_size=centerline_path.footprint_cell_size,
        settings=settings,
    )
    return tuple(
        _center_for_route_cell(centerline_path, cell)
        for cell in waypoint_cells
    )


def _waypoint_cells_for_auto_dive_route(
    centerline_path: CenterlinePath,
    *,
    route_cells: tuple[FootprintCell, ...],
    settings: AutoDiveSettings,
) -> tuple[FootprintCell, ...]:
    cached_points = getattr(centerline_path, "cached_points", None) or {}
    if cached_points:
        return _waypoint_cells_for_cached_route(
            route_cells,
            centerline_path=centerline_path,
            settings=settings,
        )
    return _waypoint_cells_for_footprint_route(
        route_cells,
        cell_size=centerline_path.footprint_cell_size,
        settings=settings,
    )


def _waypoint_cells_for_cached_route(
    route_cells: tuple[FootprintCell, ...],
    *,
    centerline_path: CenterlinePath,
    settings: AutoDiveSettings,
) -> tuple[FootprintCell, ...]:
    """Prefer dense cached navigation points to avoid chords through walls."""
    cells = _expand_route_cell_gaps_through_component(
        centerline_path,
        _dedupe_consecutive_cells(route_cells),
    )
    waypoint_limit = max(2, int(settings.max_keyframes))
    if len(cells) <= waypoint_limit:
        return cells

    return _waypoint_cells_for_footprint_route(
        cells,
        cell_size=centerline_path.footprint_cell_size,
        settings=settings,
    )


def _expand_route_cell_gaps_through_component(
    centerline_path: CenterlinePath,
    cells: tuple[FootprintCell, ...],
) -> tuple[FootprintCell, ...]:
    if len(cells) < 2:
        return cells
    expanded: list[FootprintCell] = [cells[0]]
    for first, second in zip(cells, cells[1:], strict=False):
        if max(abs(second[0] - first[0]), abs(second[1] - first[1])) <= 1:
            expanded.append(second)
            continue
        connector = lowest_cost_footprint_path(
            centerline_path.component_cells,
            first,
            second,
            centerline_path.clearance_scores,
        )
        if len(connector) >= 2:
            expanded.extend(connector[1:])
        else:
            expanded.append(second)
    return _dedupe_consecutive_cells(tuple(expanded))


def _auto_dive_points_for_waypoint_cells(
    centerline_path: CenterlinePath,
    *,
    waypoint_cells: tuple[FootprintCell, ...],
    route_xz: tuple[PointXZ, ...],
    manifest: Mapping[str, Any],
    settings: AutoDiveSettings,
    prefer_route_cell_centers: bool = False,
    fallback_y: float = 0.0,
) -> tuple[Point, ...]:
    cached_points = getattr(centerline_path, "cached_points", None) or {}
    if not cached_points:
        return route_points_for_xz_points(
            route_xz,
            manifest=manifest,
            y_search_radius_cells=max(0, int(settings.y_search_radius_cells)),
            vertical_position_fraction=float(settings.vertical_position_fraction),
        )

    route_points: list[Point] = []
    for cell, xz in zip(waypoint_cells, route_xz, strict=True):
        if prefer_route_cell_centers:
            y = _medial_y_for_route_cell(
                centerline_path,
                cell,
                fallback_y=float(fallback_y),
            )
            route_points.append((float(xz[0]), float(y), float(xz[1])))
            continue
        cached_point = cached_points.get(cell)
        if cached_point is not None:
            route_points.append(
                _cached_auto_dive_point_for_waypoint_cell(
                    centerline_path,
                    cell=cell,
                    cached_point=cached_point,
                )
            )
            continue
        route_points.append(
            route_points_for_xz_points(
                (xz,),
                manifest=manifest,
                y_search_radius_cells=max(0, int(settings.y_search_radius_cells)),
                vertical_position_fraction=float(settings.vertical_position_fraction),
            )[0]
        )
    return tuple(route_points)


def _cached_auto_dive_point_for_waypoint_cell(
    centerline_path: CenterlinePath,
    *,
    cell: FootprintCell,
    cached_point: Point,
) -> Point:
    point_cell = _footprint_cell_for_xz(
        (cached_point[0], cached_point[2]),
        centerline_path.footprint_cell_size,
    )
    if max(abs(point_cell[0] - cell[0]), abs(point_cell[1] - cell[1])) <= 1:
        return cached_point
    x, z = footprint_world_center(cell, centerline_path.footprint_cell_size)
    y = _medial_y_for_route_cell(
        centerline_path,
        cell,
        fallback_y=float(cached_point[1]),
    )
    return float(x), float(y), float(z)


def _auto_dive_initial_camera_position_groups(
    centerline_path: CenterlinePath,
    *,
    manifest: Mapping[str, Any],
    settings: AutoDiveSettings,
) -> tuple[tuple[Point, ...], ...]:
    """Return preferred startup positions before physical endpoint fallbacks."""
    endpoint_positions = _auto_dive_endpoint_positions(
        centerline_path,
        manifest=manifest,
        settings=settings,
    )
    cached_start_position = _auto_dive_cached_route_start_position(
        centerline_path,
        manifest=manifest,
        settings=settings,
    )
    if cached_start_position is None:
        return (endpoint_positions,) if endpoint_positions else ()

    if _should_prefer_cached_route_start(
        centerline_path,
        manifest=manifest,
        endpoint_positions=endpoint_positions,
    ):
        groups: list[tuple[Point, ...]] = [(cached_start_position,)]
        if endpoint_positions:
            groups.append(endpoint_positions)
        return tuple(groups)

    if endpoint_positions:
        return (endpoint_positions,)
    return ((cached_start_position,),)


def _auto_dive_cached_route_start_position(
    centerline_path: CenterlinePath,
    *,
    manifest: Mapping[str, Any],
    settings: AutoDiveSettings,
) -> Point | None:
    """Return the first cached route sample as a startup candidate."""
    if centerline_path.source != "cached_navigation_metadata":
        return None
    if not centerline_path.cells:
        return None
    first_cell = centerline_path.cells[0]
    cached_points = getattr(centerline_path, "cached_points", None) or {}
    cached_point = cached_points.get(first_cell)
    if cached_point is not None:
        return cached_point
    xz = _center_for_route_cell(centerline_path, first_cell)
    return route_points_for_xz_points(
        (xz,),
        manifest=manifest,
        y_search_radius_cells=max(0, int(settings.y_search_radius_cells)),
        vertical_position_fraction=float(settings.vertical_position_fraction),
    )[0]


def _should_prefer_cached_route_start(
    centerline_path: CenterlinePath,
    *,
    manifest: Mapping[str, Any],
    endpoint_positions: tuple[Point, ...],
) -> bool:
    """Return True when the cached route start should own startup orientation."""
    if centerline_path.source != "cached_navigation_metadata":
        return False
    if not endpoint_positions:
        return True
    return _selected_cached_route_defines_start_direction(manifest)


def _selected_cached_route_defines_start_direction(
    manifest: Mapping[str, Any],
) -> bool:
    navigation = manifest.get(NAVIGATION_METADATA_KEY)
    if not isinstance(navigation, Mapping):
        return False
    routes = navigation.get("routes")
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        return False
    selected_route_id = navigation.get("recommended_route_id")
    selected_route = None
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        if selected_route_id is None or route.get("id") == selected_route_id:
            selected_route = route
            break
    if not isinstance(selected_route, Mapping):
        return False
    if selected_route.get("starts_at_navigation_start"):
        return True
    return selected_route.get("selection_method") in (
        "navigation_start_to_farthest_endpoint_v1",
        "physical_endpoint_diameter_v1",
    )


def _auto_dive_endpoint_positions(
    centerline_path: CenterlinePath,
    *,
    manifest: Mapping[str, Any],
    settings: AutoDiveSettings,
) -> tuple[Point, ...]:
    endpoint_cells = _auto_dive_physical_endpoint_cells(
        centerline_path,
        settings=settings,
    )
    if not endpoint_cells:
        return ()
    cached_points = getattr(centerline_path, "cached_points", None) or {}
    positions: list[Point] = []
    for cell in endpoint_cells:
        cached_point = cached_points.get(cell)
        if cached_point is not None:
            positions.append(cached_point)
            continue
        xz = _center_for_route_cell(centerline_path, cell)
        positions.append(
            route_points_for_xz_points(
                (xz,),
                manifest=manifest,
                y_search_radius_cells=max(0, int(settings.y_search_radius_cells)),
                vertical_position_fraction=float(settings.vertical_position_fraction),
            )[0]
        )
    return _dedupe_consecutive_points(tuple(positions))


def _auto_dive_physical_endpoint_cells(
    centerline_path: CenterlinePath,
    *,
    settings: AutoDiveSettings,
) -> tuple[FootprintCell, ...]:
    """Return component-diameter endpoint cells, centered within local passage.

    Cached centerline routes are selected for traversal quality, not for startup
    placement. Their first/last cells can be the ends of a selected sub-path in
    a multi-phase or branching map. Startup needs physical cave endpoints, so
    use a two-sweep graph-diameter estimate over the full component footprint
    and then move each endpoint inward/laterally only enough to improve local
    clearance.
    """
    component = centerline_path.component_cells
    if not component:
        return tuple(centerline_path.cells[:1])
    if len(component) <= 2:
        return tuple(sorted(component))

    seed = _auto_dive_endpoint_seed_cell(centerline_path)
    first_endpoint = _furthest_component_cell(
        component,
        seed,
        cell_size=centerline_path.footprint_cell_size,
    )
    second_endpoint = _furthest_component_cell(
        component,
        first_endpoint,
        cell_size=centerline_path.footprint_cell_size,
    )
    search_radius = max(
        1,
        min(8, max(2, int(settings.smoothing_radius_cells))),
    )
    centered = (
        _centered_endpoint_cell(
            centerline_path,
            first_endpoint,
            search_radius_cells=search_radius,
        ),
        _centered_endpoint_cell(
            centerline_path,
            second_endpoint,
            search_radius_cells=search_radius,
        ),
    )
    return _dedupe_consecutive_cells(centered)


def _auto_dive_endpoint_seed_cell(centerline_path: CenterlinePath) -> FootprintCell:
    if centerline_path.cells:
        return centerline_path.cells[0]
    return min(centerline_path.component_cells)


def _furthest_component_cell(
    component: frozenset[FootprintCell],
    start: FootprintCell,
    *,
    cell_size: float,
) -> FootprintCell:
    distances = _component_distances_from(
        component,
        start,
        cell_size=cell_size,
        max_distance_m=None,
    )
    return max(
        distances,
        key=lambda cell: (
            distances[cell],
            footprint_cell_distance(start, cell),
            cell,
        ),
    )


def _centered_endpoint_cell(
    centerline_path: CenterlinePath,
    endpoint_cell: FootprintCell,
    *,
    search_radius_cells: int,
) -> FootprintCell:
    component = centerline_path.component_cells
    radius_m = max(0.0, float(search_radius_cells)) * float(
        centerline_path.footprint_cell_size,
    )
    distances = _component_distances_from(
        component,
        endpoint_cell,
        cell_size=centerline_path.footprint_cell_size,
        max_distance_m=radius_m,
    )
    if not distances:
        return endpoint_cell
    return max(
        distances,
        key=lambda cell: (
            centerline_path.clearance_scores.get(cell, 0),
            -distances[cell],
            -footprint_cell_distance(endpoint_cell, cell),
            cell,
        ),
    )


def _component_distances_from(
    component: frozenset[FootprintCell],
    start: FootprintCell,
    *,
    cell_size: float,
    max_distance_m: float | None,
) -> dict[FootprintCell, float]:
    if start not in component:
        start = min(
            component,
            key=lambda cell: (footprint_cell_distance(start, cell), cell),
        )
    max_distance = None if max_distance_m is None else max(0.0, float(max_distance_m))
    frontier: list[tuple[float, FootprintCell]] = [(0.0, start)]
    distances: dict[FootprintCell, float] = {start: 0.0}
    while frontier:
        current_distance, current = heapq.heappop(frontier)
        if current_distance > distances[current]:
            continue
        if max_distance is not None and current_distance > max_distance:
            continue
        for neighbor in navigable_footprint_neighbors(current, component):
            next_distance = (
                current_distance
                + footprint_cell_distance(current, neighbor) * float(cell_size)
            )
            if max_distance is not None and next_distance > max_distance:
                continue
            if next_distance >= distances.get(neighbor, math.inf):
                continue
            distances[neighbor] = next_distance
            heapq.heappush(frontier, (next_distance, neighbor))
    return distances


def _auto_dive_initial_plan_score(plan: AutoDivePlan) -> tuple[object, ...]:
    route_points = plan.route_points
    if not route_points:
        return (False, False, 0, 0.0, 0.0, 0.0)
    collision_validator = _AutoDiveCollisionValidator(plan.centerline_path)
    start_clear = collision_validator.point_is_clear(route_points[0])
    first_segment_clear = (
        len(route_points) >= 2
        and collision_validator.segment_is_clear(route_points[0], route_points[1])
    )
    first_samples = route_points[: min(8, len(route_points))]
    lateral_scores = _sampled_lateral_clearance_scores(
        first_samples,
        collision_validator=collision_validator,
    )
    clearance_margins = _sampled_clearance_margins_m(
        first_samples,
        collision_validator=collision_validator,
    )
    return (
        bool(start_clear),
        bool(first_segment_clear),
        min(lateral_scores) if lateral_scores else 0,
        (
            min(clearance_margins)
            if clearance_margins
            else 0.0
        ),
        (
            sum(lateral_scores) / len(lateral_scores)
            if lateral_scores
            else 0.0
        ),
        float(plan.route_length_m),
    )


def _select_best_auto_dive_route_candidate(
    centerline_path: CenterlinePath,
    *,
    waypoint_cells: tuple[FootprintCell, ...],
    route_points: tuple[Point, ...],
    current: np.ndarray,
    settings: AutoDiveSettings,
    collision_validator: _AutoDiveCollisionValidator,
    current_yaw: float | None = None,
    current_pitch: float | None = None,
    current_roll: float | None = None,
    current_travel_yaw: float | None = None,
    current_travel_pitch: float | None = None,
    avoid_positions: Sequence[Sequence[float]] | None = None,
    user_reposition: bool = False,
    force_hemisphere_scan: bool = False,
    voxel_route_active: bool = False,
    voxel_route_plan: NavigationVoxelRoutePlan | None = None,
    planning_budget: _AutoDivePlanningBudget | None = None,
    diagnostics: AutoDiveDiagnosticSink | None = None,
) -> _AutoDiveSelectedRoute:
    """Generate and score local route candidates, returning the safest route.

    This turns smoothing into candidate selection. Raw, Theta-relaxed,
    weighted-smoothed, and B-spline candidates compete on collision validity,
    lateral clearance, margin, forward progress, curvature, and vertical jerk.
    The caller still prepends the exact camera position after this selection.
    """
    decision_started_at = time.perf_counter()
    if planning_budget is not None:
        planning_budget.check("candidate_selection", diagnostics=diagnostics)
    specs = _auto_dive_candidate_specs(settings)
    route_samples = _AutoDiveRouteSamples(cells=waypoint_cells, points=route_points)
    # Candidate construction can call segment checks many times while probing
    # Theta/cone shortcuts. Keep those speculative probes footprint-only, then
    # apply the cached mesh guard once during final candidate scoring. This
    # keeps Guided Dive startup responsive while still applying cached mesh
    # during final candidate scoring and guarded fallback trimming.
    construction_collision_validator = (
        _AutoDiveCollisionValidator(centerline_path)
        if collision_validator.has_mesh_collision_guard
        else collision_validator
    )
    current_point: Point = (
        float(current[0]),
        float(current[1]),
        float(current[2]),
    )
    position_direction = _auto_dive_current_position_offset_direction(
        centerline_path,
        current=current,
        current_point=current_point,
    )
    if voxel_route_active:
        return _select_voxel_graph_auto_dive_route(
            centerline_path,
            route_samples=route_samples,
            current=current,
            current_point=current_point,
            settings=settings,
            current_yaw=current_yaw,
            current_pitch=current_pitch,
            current_roll=current_roll,
            current_travel_yaw=current_travel_yaw,
            current_travel_pitch=current_travel_pitch,
            avoid_positions=avoid_positions,
            user_reposition=user_reposition,
            force_hemisphere_scan=force_hemisphere_scan,
            voxel_route_plan=voxel_route_plan,
            collision_validator=collision_validator,
            diagnostics=diagnostics,
            decision_started_at=decision_started_at,
            planning_budget=planning_budget,
        )
    if len(specs) <= 1:
        candidate = _build_auto_dive_route_candidate(
            specs[0],
            ordinal=0,
            centerline_path=centerline_path,
            route_samples=route_samples,
            current=current,
            settings=settings,
            collision_validator=construction_collision_validator,
        )
        candidate_score = _score_auto_dive_route_candidate(
            candidate,
            current_point=current_point,
            collision_validator=collision_validator,
            planning_budget=planning_budget,
            diagnostics=diagnostics,
        )
        travel_filter_payload = _forward_travel_cone_filter_payload(
            [(candidate_score, candidate)],
            current_point=current_point,
            current_travel_yaw=current_travel_yaw,
            current_travel_pitch=current_travel_pitch,
            position_direction=position_direction,
            cell_size=collision_validator.cell_size,
        )
        if (
            current_travel_yaw is not None
            and not _candidate_route_is_within_travel_cone(
                candidate,
                candidate_score,
                current_point=current_point,
                current_travel_yaw=current_travel_yaw,
                current_travel_pitch=current_travel_pitch,
                position_direction=position_direction,
                cell_size=collision_validator.cell_size,
            )
        ):
            _record_auto_dive_diagnostic(
                diagnostics,
                "candidate_scores",
                {
                    "selected": "none",
                    "candidate_count": 1,
                    "mesh_collision_enabled": bool(
                        collision_validator.has_mesh_collision_guard
                    ),
                    "user_reposition": bool(user_reposition),
                    "candidates": [
                        _auto_dive_candidate_score_payload(
                            candidate_score,
                            candidate,
                        )
                    ],
                    "reason": "no_forward_travel_candidates",
                    "travel_cone_degrees": _AUTO_DIVE_FORWARD_TRAVEL_CONE_DEGREES,
                    "travel_filter": travel_filter_payload,
                    "decision_duration_ms": _auto_dive_duration_ms(
                        decision_started_at
                    ),
                },
            )
            raise NavigationConfigurationError(
                "Guided Dive found no route candidate in the forward travel cone"
            )
        _record_auto_dive_diagnostic(
            diagnostics,
            "candidate_scores",
            {
                "selected": candidate.name,
                "candidate_count": 1,
                "mesh_collision_enabled": bool(
                    collision_validator.has_mesh_collision_guard
                ),
                "user_reposition": bool(user_reposition),
                "travel_filter": travel_filter_payload,
                "decision_duration_ms": _auto_dive_duration_ms(
                    decision_started_at
                ),
                "candidates": [
                    {
                        "name": candidate.name,
                        "ordinal": candidate.ordinal,
                        "point_count": len(candidate.points),
                    }
                ],
            },
        )
        return _AutoDiveSelectedRoute(
            points=candidate.points,
            selection_reason="single_candidate",
            roll_deg=float(candidate.roll_deg),
        )

    candidates: list[_AutoDiveRouteCandidate] = []
    failed_candidates: list[dict[str, Any]] = []
    def append_candidate(ordinal: int, spec: _AutoDiveCandidateSpec) -> None:
        try:
            candidates.append(
                _build_auto_dive_route_candidate(
                    spec,
                    ordinal=ordinal,
                    centerline_path=centerline_path,
                    route_samples=route_samples,
                    current=current,
                    settings=settings,
                    collision_validator=construction_collision_validator,
                )
            )
        except AutoDivePlanningBudgetExceeded:
            raise
        except Exception as exc:
            failed_candidates.append(
                {
                    "name": spec.name,
                    "ordinal": ordinal,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    if planning_budget is not None:
        # Do not submit speculative work to a context manager during a
        # bounded replan: once the deadline is reached, executor shutdown
        # would otherwise wait for every worker before the exception escapes.
        for ordinal, spec in enumerate(specs):
            planning_budget.check(
                "candidate_construction",
                diagnostics=diagnostics,
            )
            append_candidate(ordinal, spec)
    else:
        worker_count = min(2, len(specs))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="AutoDiveCandidate",
        ) as executor:
            futures = {
                executor.submit(
                    _build_auto_dive_route_candidate,
                    spec,
                    ordinal=ordinal,
                    centerline_path=centerline_path,
                    route_samples=route_samples,
                    current=current,
                    settings=settings,
                    collision_validator=construction_collision_validator,
                ): (ordinal, spec)
                for ordinal, spec in enumerate(specs)
            }
            for future in as_completed(futures):
                ordinal, spec = futures[future]
                try:
                    candidates.append(future.result())
                except Exception as exc:
                    failed_candidates.append(
                        {
                            "name": spec.name,
                            "ordinal": ordinal,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
                    continue

    if not candidates:
        _record_auto_dive_diagnostic(
            diagnostics,
            "candidate_scores",
            {
                "selected": "fallback_raw_points",
                "candidate_count": 0,
                "mesh_collision_enabled": bool(
                    collision_validator.has_mesh_collision_guard
                ),
                "user_reposition": bool(user_reposition),
                "failed_candidates": failed_candidates,
                "decision_duration_ms": _auto_dive_duration_ms(
                    decision_started_at
                ),
            },
        )
        return _AutoDiveSelectedRoute(
            points=route_points,
            selection_reason="fallback_raw_points",
            roll_deg=0.0,
        )

    scored = [
        (
            _score_auto_dive_route_candidate(
                candidate,
                current_point=current_point,
                collision_validator=collision_validator,
                planning_budget=planning_budget,
                diagnostics=diagnostics,
            ),
            candidate,
        )
        for candidate in candidates
        if len(candidate.points) >= 1
    ]
    if (
        collision_validator.has_mesh_collision_guard
        and _mesh_recovery_is_enabled(collision_validator.mesh_guard)
        and scored
        and not any(score.mesh_clear for score, _candidate in scored)
    ):
        collision_validator = _materialize_auto_dive_voxel_volume(
            collision_validator
        )
        hemisphere_candidate = _build_hemisphere_probe_route_candidate(
            ordinal=max(candidate.ordinal for candidate in candidates) + 1,
            centerline_path=centerline_path,
            current=current,
            route_points=route_samples.points,
            current_yaw=current_yaw,
            current_pitch=current_pitch,
            current_roll=current_roll,
            current_travel_yaw=current_travel_yaw,
            current_travel_pitch=current_travel_pitch,
            collision_validator=collision_validator,
            avoid_positions=avoid_positions,
            settings=settings,
            planning_budget=planning_budget,
            diagnostics=diagnostics,
        )
        if hemisphere_candidate is not None:
            candidates.append(hemisphere_candidate)
            scored.append(
                (
                    _score_auto_dive_route_candidate(
                        hemisphere_candidate,
                        current_point=current_point,
                        collision_validator=collision_validator,
                        allow_low_lateral_clearance=True,
                        planning_budget=planning_budget,
                        diagnostics=diagnostics,
                    ),
                    hemisphere_candidate,
                )
            )
        # A local voxel route is only a candidate. If the exact mesh guard
        # rejects it, do not return to the same candidate and then trim the
        # centerline into a dead end. Run the complete 3D hemisphere scan so
        # lateral and vertical alternatives can compete from this position.
        if (
            hemisphere_candidate is not None
            and hemisphere_candidate.name == "voxel-local-frontier"
            and not any(score.route_clear for score, _candidate in scored)
        ):
            full_scan_candidate = _build_hemisphere_probe_route_candidate(
                ordinal=max(candidate.ordinal for candidate in candidates) + 1,
                centerline_path=centerline_path,
                current=current,
                route_points=route_samples.points,
                current_yaw=current_yaw,
                current_pitch=current_pitch,
                current_roll=current_roll,
                current_travel_yaw=current_travel_yaw,
                current_travel_pitch=current_travel_pitch,
                collision_validator=collision_validator,
                avoid_positions=avoid_positions,
                settings=settings,
                planning_budget=planning_budget,
                force_hemisphere_scan=True,
                diagnostics=diagnostics,
            )
            if full_scan_candidate is not None:
                candidates.append(full_scan_candidate)
                scored.append(
                    (
                        _score_auto_dive_route_candidate(
                            full_scan_candidate,
                            current_point=current_point,
                            collision_validator=collision_validator,
                            allow_low_lateral_clearance=True,
                            planning_budget=planning_budget,
                            diagnostics=diagnostics,
                        ),
                        full_scan_candidate,
                    )
                )
        recovery_candidate = (
            None
            if any(score.route_clear for score, _candidate in scored)
            else _build_mesh_recovery_auto_dive_route_candidate(
                ordinal=max(candidate.ordinal for candidate in candidates) + 1,
                centerline_path=centerline_path,
                route_samples=route_samples,
                current=current,
                current_yaw=current_yaw,
                current_pitch=current_pitch,
                current_travel_yaw=current_travel_yaw,
                current_travel_pitch=current_travel_pitch,
                avoid_positions=avoid_positions,
                # Never reverse a route when a directional pose or user
                # travel vector is available. If neither is available, the
                # search still has its scan/target ordering as a safe
                # fallback.
                allow_reverse_travel=False,
                collision_validator=collision_validator,
                diagnostics=diagnostics,
                planning_budget=planning_budget,
            )
        )
        if recovery_candidate is not None:
            candidates.append(recovery_candidate)
            scored.append(
                (
                    _score_auto_dive_route_candidate(
                        recovery_candidate,
                        current_point=current_point,
                        collision_validator=collision_validator,
                        planning_budget=planning_budget,
                        diagnostics=diagnostics,
                    ),
                    recovery_candidate,
                )
            )
    travel_filter_payload = _forward_travel_cone_filter_payload(
        scored,
        current_point=current_point,
        current_travel_yaw=current_travel_yaw,
        current_travel_pitch=current_travel_pitch,
        position_direction=position_direction,
        cell_size=collision_validator.cell_size,
    )
    scored = _forward_travel_cone_route_candidates(
        scored,
        current_point=current_point,
        current_travel_yaw=current_travel_yaw,
        current_travel_pitch=current_travel_pitch,
        position_direction=position_direction,
        cell_size=collision_validator.cell_size,
    )
    if not scored:
        _record_auto_dive_diagnostic(
            diagnostics,
            "candidate_scores",
            {
                "selected": "none",
                "candidate_count": len(candidates),
                "mesh_collision_enabled": bool(
                    collision_validator.has_mesh_collision_guard
                ),
                "user_reposition": bool(user_reposition),
                "failed_candidates": failed_candidates,
                "reason": "no_forward_travel_candidates",
                "travel_cone_degrees": _AUTO_DIVE_FORWARD_TRAVEL_CONE_DEGREES,
                "travel_filter": travel_filter_payload,
                "decision_duration_ms": _auto_dive_duration_ms(
                    decision_started_at
                ),
            },
        )
        raise NavigationConfigurationError(
            "Guided Dive found no route candidate in the forward travel cone"
        )

    selectable = [
        item
        for item in scored
        if item[0].route_clear and item[0].geometry_trusted
    ]
    selection_reason = "trusted_route_clear"
    if not selectable:
        selectable = [
            item
            for item in scored
            if (
                item[0].geometry_trusted
                and _dense_candidate_clearance_failure_is_tolerable(item[0])
                and not item[1].name.startswith("cone-")
            )
        ]
        selection_reason = "trusted_dense_low_clearance_fallback"
    if not selectable:
        selectable = [
            item
            for item in scored
            if item[0].route_clear
        ]
        selection_reason = "untrusted_route_clear_fallback"
    if not selectable:
        selectable = [
            item
            for item in scored
            if (
                item[0].entry_clear
                and item[0].mesh_clear
                and not item[1].name.startswith("cone-")
            )
        ]
        selection_reason = "entry_clear_fallback"
    if not selectable:
        selectable = [
            item
            for item in scored
            if item[1].name == "raw" and item[0].mesh_clear
        ]
        selection_reason = "raw_fallback"
    if not selectable and collision_validator.has_mesh_collision_guard:
        selectable = [
            item
            for item in scored
            if (
                _score_reports_mesh_intersection(item[0])
                and not item[1].name.startswith("cone-")
            )
        ]
        selection_reason = "mesh_compromised_prefix_fallback"
    if not selectable:
        selectable = [
            item
            for item in scored
            if item[1].name == "raw"
        ] or scored
        selection_reason = "raw_fallback"

    best_score, best_candidate = max(
        selectable,
        key=lambda item: (
            *_auto_dive_selection_sort_key(
                item,
                selection_reason=selection_reason,
                current_point=current_point,
                cell_size=collision_validator.cell_size,
            ),
            -item[1].ordinal,
        ),
    )
    if (
        best_candidate.name == "mesh-recovery"
        and selection_reason == "trusted_route_clear"
    ):
        selection_reason = "mesh_recovery_route_clear"
    elif (
        best_candidate.name == "hemisphere-probe"
        and selection_reason == "trusted_route_clear"
    ):
        selection_reason = "hemisphere_probe_route_clear"
    elif (
        best_candidate.name == "voxel-local-frontier"
        and selection_reason == "trusted_route_clear"
    ):
        selection_reason = "voxel_local_frontier_route_clear"
    selected_points = best_candidate.points
    selected_route_truncated = False
    selected_safe_prefix_length_m: float | None = None
    if selection_reason == "mesh_compromised_prefix_fallback":
        selected_points = _trim_auto_dive_candidate_before_clearance_failure(
            best_candidate,
            best_score.first_clearance_failure,
            current_point=current_point,
            cell_size=collision_validator.cell_size,
        )
        selected_route_truncated = selected_points != best_candidate.points
        selected_safe_prefix_length_m = path_length(
            _route_points_starting_at_current_camera(
                selected_points,
                np.asarray(current_point, dtype=np.float64),
            )
        )
    _record_auto_dive_diagnostic(
        diagnostics,
        "candidate_scores",
        {
            "selected": best_candidate.name,
            "selection_reason": selection_reason,
            "user_reposition": bool(user_reposition),
            "force_hemisphere_scan": bool(force_hemisphere_scan),
            "selected_geometry_trusted": bool(best_score.geometry_trusted),
            "selected_route_truncated": bool(selected_route_truncated),
            "selected_safe_prefix_length_m": selected_safe_prefix_length_m,
            "selected_replan_at_end": bool(
                best_candidate.name
                in {"mesh-recovery", "hemisphere-probe", "voxel-local-frontier"}
            ),
            "trusted_max_segment_cells": float(
                DEFAULT_AUTO_DIVE_TRUSTED_MAX_SEGMENT_CELLS
            ),
            "mesh_collision_enabled": bool(
                collision_validator.has_mesh_collision_guard
            ),
            "voxel_volume": _auto_dive_voxel_volume_payload(
                collision_validator
            ),
            "candidate_count": len(candidates),
            "failed_candidates": failed_candidates,
            "travel_filter": travel_filter_payload,
            "decision_duration_ms": _auto_dive_duration_ms(decision_started_at),
            "candidates": [
                _auto_dive_candidate_score_payload(score, candidate)
                for score, candidate in sorted(
                    scored,
                    key=lambda item: item[1].ordinal,
                )
            ],
        },
    )
    return _AutoDiveSelectedRoute(
        points=selected_points,
        selection_reason=selection_reason,
        route_truncated_by_mesh=bool(selected_route_truncated),
        mesh_safe_prefix_length_m=selected_safe_prefix_length_m,
        replan_at_end=bool(
            best_candidate.name
            in {"mesh-recovery", "hemisphere-probe", "voxel-local-frontier"}
        ),
        roll_deg=float(best_candidate.roll_deg),
    )


def _select_voxel_graph_auto_dive_route(
    centerline_path: CenterlinePath,
    *,
    route_samples: _AutoDiveRouteSamples,
    current: np.ndarray,
    current_point: Point,
    settings: AutoDiveSettings,
    current_yaw: float | None,
    current_pitch: float | None,
    current_roll: float | None,
    current_travel_yaw: float | None,
    current_travel_pitch: float | None,
    avoid_positions: Sequence[Sequence[float]] | None,
    user_reposition: bool,
    force_hemisphere_scan: bool,
    voxel_route_plan: NavigationVoxelRoutePlan | None,
    collision_validator: _AutoDiveCollisionValidator,
    diagnostics: AutoDiveDiagnosticSink | None,
    decision_started_at: float,
    planning_budget: _AutoDivePlanningBudget | None = None,
) -> _AutoDiveSelectedRoute:
    """Validate a cached voxel route with one exact mesh pass.

    A filled-space route does not need the seven speculative smoothing
    candidates. If its exact mesh check fails, the existing bounded recovery
    search remains the safety fallback; otherwise a safe prefix is returned.
    """
    graph_candidate = _AutoDiveRouteCandidate(
        ordinal=0,
        name="voxel-graph",
        cells=route_samples.cells,
        points=route_samples.points,
    )
    if planning_budget is not None:
        planning_budget.check("voxel_route_scoring", diagnostics=diagnostics)
    active_validator = collision_validator
    graph_score = _score_auto_dive_route_candidate(
        graph_candidate,
        current_point=current_point,
        collision_validator=active_validator,
        allow_low_lateral_clearance=True,
        planning_budget=planning_budget,
        diagnostics=diagnostics,
    )
    scored: list[tuple[_AutoDiveRouteCandidateScore, _AutoDiveRouteCandidate]] = [
        (graph_score, graph_candidate)
    ]
    hemisphere_candidate: _AutoDiveRouteCandidate | None = None
    recovery_candidate: _AutoDiveRouteCandidate | None = None
    should_scan_hemisphere = bool(
        force_hemisphere_scan or not graph_score.route_clear
    )
    if should_scan_hemisphere and (
        active_validator.voxel_volume is not None
        or active_validator.has_mesh_collision_guard
    ):
        if planning_budget is not None:
            planning_budget.check("mesh_recovery_setup", diagnostics=diagnostics)
        active_validator = _materialize_auto_dive_voxel_volume(active_validator)
        hemisphere_candidate = _build_hemisphere_probe_route_candidate(
            ordinal=1,
            centerline_path=centerline_path,
            current=current,
            route_points=route_samples.points,
            current_yaw=current_yaw,
            current_pitch=current_pitch,
            current_roll=current_roll,
            current_travel_yaw=current_travel_yaw,
            current_travel_pitch=current_travel_pitch,
            collision_validator=active_validator,
            avoid_positions=avoid_positions,
            settings=settings,
            scan_distance_m=(
                None
                if voxel_route_plan is None
                else voxel_route_plan.lookahead_distance_m
            ),
            planning_budget=planning_budget,
            force_hemisphere_scan=bool(force_hemisphere_scan),
            diagnostics=diagnostics,
        )
        if hemisphere_candidate is not None:
            scored.append(
                (
                    _score_auto_dive_route_candidate(
                        hemisphere_candidate,
                        current_point=current_point,
                        collision_validator=active_validator,
                        allow_low_lateral_clearance=True,
                        planning_budget=planning_budget,
                        diagnostics=diagnostics,
                    ),
                    hemisphere_candidate,
                )
            )
        # A local voxel route can be geometrically plausible yet fail the
        # exact mesh guard. In that case it must not suppress the complete
        # hemisphere scan; the missing passage may be lateral or vertical.
        if (
            not force_hemisphere_scan
            and hemisphere_candidate is not None
            and hemisphere_candidate.name == "voxel-local-frontier"
            and not any(score.route_clear for score, _candidate in scored)
        ):
            full_scan_candidate = _build_hemisphere_probe_route_candidate(
                ordinal=max(candidate.ordinal for _score, candidate in scored) + 1,
                centerline_path=centerline_path,
                current=current,
                route_points=route_samples.points,
                current_yaw=current_yaw,
                current_pitch=current_pitch,
                current_roll=current_roll,
                current_travel_yaw=current_travel_yaw,
                current_travel_pitch=current_travel_pitch,
                collision_validator=active_validator,
                avoid_positions=avoid_positions,
                settings=settings,
                scan_distance_m=(
                    None
                    if voxel_route_plan is None
                    else voxel_route_plan.lookahead_distance_m
                ),
                planning_budget=planning_budget,
                force_hemisphere_scan=True,
                diagnostics=diagnostics,
            )
            if full_scan_candidate is not None:
                scored.append(
                    (
                        _score_auto_dive_route_candidate(
                            full_scan_candidate,
                            current_point=current_point,
                            collision_validator=active_validator,
                            allow_low_lateral_clearance=True,
                            planning_budget=planning_budget,
                            diagnostics=diagnostics,
                        ),
                        full_scan_candidate,
                    )
                )
    if (
        not graph_score.route_clear
        and active_validator.has_mesh_collision_guard
        and _mesh_recovery_is_enabled(active_validator.mesh_guard)
        and not any(score.route_clear for score, _candidate in scored)
    ):
        recovery_candidate = _build_mesh_recovery_auto_dive_route_candidate(
            ordinal=2,
            centerline_path=centerline_path,
            route_samples=route_samples,
            current=current,
            current_yaw=current_yaw,
            current_pitch=current_pitch,
            current_travel_yaw=current_travel_yaw,
            current_travel_pitch=current_travel_pitch,
            avoid_positions=avoid_positions,
            allow_reverse_travel=False,
            collision_validator=active_validator,
            use_footprint_centers=True,
            diagnostics=diagnostics,
            planning_budget=planning_budget,
        )
        if recovery_candidate is not None:
            scored.append(
                (
                    _score_auto_dive_route_candidate(
                        recovery_candidate,
                        current_point=current_point,
                        collision_validator=active_validator,
                        allow_low_lateral_clearance=True,
                        planning_budget=planning_budget,
                        diagnostics=diagnostics,
                    ),
                    recovery_candidate,
                )
            )

    clear_candidates = [item for item in scored if item[0].route_clear]
    selected_route_truncated = False
    selected_safe_prefix_length_m: float | None = None
    if clear_candidates:
        best_score, best_candidate = max(
            clear_candidates,
            key=lambda item: _auto_dive_selection_sort_key(
                item,
                selection_reason="voxel_branch_lookahead",
                current_point=current_point,
                cell_size=active_validator.cell_size,
            ),
        )
        if best_candidate.name == "mesh-recovery":
            selection_reason = "voxel_branch_lookahead_mesh_recovery"
        elif best_candidate.name == "hemisphere-probe":
            selection_reason = "voxel_hemisphere_probe"
        elif best_candidate.name == "voxel-local-frontier":
            selection_reason = "voxel_local_frontier"
        elif (
            voxel_route_plan is not None
            and voxel_route_plan.prepared_graph
        ):
            selection_reason = str(
                voxel_route_plan.selection_reason
                or "prepared_forward_graph"
            )
        else:
            selection_reason = "voxel_branch_lookahead"
        selected_points = best_candidate.points
    else:
        best_score, best_candidate = max(
            scored,
            key=lambda item: _auto_dive_selection_sort_key(
                item,
                selection_reason="mesh_compromised_prefix_fallback",
                current_point=current_point,
                cell_size=active_validator.cell_size,
            ),
        )
        selection_reason = "voxel_branch_lookahead_mesh_prefix_fallback"
        selected_points = _trim_auto_dive_candidate_before_clearance_failure(
            best_candidate,
            best_score.first_clearance_failure,
            current_point=current_point,
            cell_size=active_validator.cell_size,
        )
        selected_route_truncated = selected_points != best_candidate.points
        selected_safe_prefix_length_m = path_length(
            _route_points_starting_at_current_camera(
                selected_points,
                np.asarray(current_point, dtype=np.float64),
            )
        )
    selected_replan_at_end = bool(
        not selected_route_truncated
        and (
            (
                voxel_route_plan is not None
                and voxel_route_plan.replan_at_lookahead
            )
            or best_candidate.name in {
                "hemisphere-probe",
                "voxel-local-frontier",
            }
        )
    )

    _record_auto_dive_diagnostic(
        diagnostics,
        "candidate_scores",
        {
            "selected": best_candidate.name,
            "selection_reason": selection_reason,
            "user_reposition": bool(user_reposition),
            "selected_geometry_trusted": bool(best_score.geometry_trusted),
            "selected_route_truncated": bool(selected_route_truncated),
            "selected_safe_prefix_length_m": selected_safe_prefix_length_m,
            "selected_replan_at_end": selected_replan_at_end,
            "route_geometry_source": (
                "voxel_3d_cell_centers"
                if voxel_route_plan is not None
                and voxel_route_plan.three_d_graph
                else "voxel_cell_centers"
                if voxel_route_plan is not None
                else "cached_navigation_points"
            ),
            "voxel_branch_lookahead": (
                None
                if voxel_route_plan is None
                else voxel_route_plan.diagnostic_payload()
            ),
            "mesh_collision_enabled": bool(
                active_validator.has_mesh_collision_guard
            ),
            "low_lateral_clearance_allowed": True,
            "voxel_route_entrance_guard": True,
            "voxel_route_progress_monotonic": False,
            "voxel_volume": _auto_dive_voxel_volume_payload(active_validator),
            "candidate_count": len(scored),
            "travel_filter": {
                "enabled": False,
                "reason": "voxel_entrance_band_and_heading",
                "before_count": len(scored),
                "after_count": len(scored),
            },
            "decision_duration_ms": _auto_dive_duration_ms(decision_started_at),
            "candidates": [
                _auto_dive_candidate_score_payload(score, candidate)
                for score, candidate in scored
            ],
        },
    )
    return _AutoDiveSelectedRoute(
        points=selected_points,
        selection_reason=selection_reason,
        route_truncated_by_mesh=bool(selected_route_truncated),
        mesh_safe_prefix_length_m=selected_safe_prefix_length_m,
        replan_at_end=selected_replan_at_end,
        roll_deg=float(best_candidate.roll_deg),
        terminal_reached=bool(
            voxel_route_plan is not None
            and voxel_route_plan.terminal_reached
            and best_candidate.name == "voxel-graph"
            and not selected_route_truncated
        ),
    )


def _record_auto_dive_diagnostic(
    diagnostics: AutoDiveDiagnosticSink | None,
    event: str,
    payload: Mapping[str, Any],
) -> None:
    if diagnostics is None:
        return
    try:
        diagnostics(event, payload)
    except Exception:
        return


def _auto_dive_duration_ms(started_at: float) -> float:
    return max(0.0, (time.perf_counter() - float(started_at)) * 1000.0)


def _make_auto_dive_voxel_builder(
    *,
    route_points: tuple[Point, ...],
    centerline_path: CenterlinePath,
    mesh_guard: CachedChunkMeshCollisionGuard | None,
    settings: AutoDiveSettings,
    diagnostics: AutoDiveDiagnosticSink | None,
) -> Callable[[], LocalVoxelVolume | None] | None:
    """Return a lazy local voxel builder for mesh-recovery replanning."""
    common_payload = {
        "method": VOXEL_VOLUME_METHOD,
        "curvature_method": CURVATURE_PROFILE_METHOD,
        "voxel_size_m": float(settings.voxel_size_m),
        "curvature_rank_threshold": int(
            settings.voxel_curvature_rank_threshold
        ),
        "max_regions": int(settings.voxel_max_regions),
        "max_distance_m": float(settings.voxel_max_distance_m),
        "max_cells": int(settings.voxel_max_cells),
    }
    if mesh_guard is None:
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_volume",
            {
                **common_payload,
                "built": False,
                "outcome": VOXEL_ANALYSIS_OUTCOME_MESH_GUARD_UNAVAILABLE,
            },
        )
        return None
    if not bool(settings.voxel_analysis_enabled):
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_volume",
            {
                **common_payload,
                "built": False,
                "outcome": VOXEL_ANALYSIS_OUTCOME_DISABLED,
            },
        )
        return None

    def build() -> LocalVoxelVolume | None:
        try:
            analysis = analyze_curvature_guided_voxel_volume(
                route_points,
                triangle_provider=mesh_guard.triangle_meshes_for_bounds,
                voxel_size_m=float(settings.voxel_size_m),
                curvature_rank_threshold=int(
                    settings.voxel_curvature_rank_threshold
                ),
                max_regions=int(settings.voxel_max_regions),
                max_distance_m=float(settings.voxel_max_distance_m),
                padding_m=max(
                    float(centerline_path.footprint_cell_size) * 0.5,
                    float(settings.voxel_size_m) * 2.0,
                ),
                max_voxels=int(settings.voxel_max_cells),
                window_points=max(
                    2,
                    min(5, int(settings.smoothing_radius_cells) or 2),
                ),
            )
        except Exception as exc:
            _record_auto_dive_diagnostic(
                diagnostics,
                "voxel_volume",
                {
                    **common_payload,
                    "built": False,
                    "outcome": VOXEL_ANALYSIS_OUTCOME_ERROR,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return None

        analysis_payload = analysis.diagnostic_payload()
        analysis_payload.update(
            {
                "point_count": int(analysis.profile.point_count),
                "curvature_sample_count": len(analysis.profile.samples),
                "curvature_region_count": len(analysis.profile.regions),
                "curvature_regions": [
                    {
                        "start_index": int(region.start_index),
                        "end_index": int(region.end_index),
                        "start_distance_m": float(region.start_distance_m),
                        "end_distance_m": float(region.end_distance_m),
                        "max_rank_0_100": int(region.max_rank_0_100),
                        "max_curvature_density_rad_per_m": float(
                            region.max_curvature_density_rad_per_m
                        ),
                    }
                    for region in analysis.profile.regions[:8]
                ],
                "curvature_regions_truncated": len(analysis.profile.regions) > 8,
            }
        )
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_volume",
            {
                **common_payload,
                **analysis_payload,
            },
        )
        return analysis.volume

    return build


def _make_auto_dive_local_frontier_voxel_builder(
    *,
    current: np.ndarray,
    forward: Sequence[float] | None,
    mesh_guard: CachedChunkMeshCollisionGuard | None,
    settings: AutoDiveSettings,
    diagnostics: AutoDiveDiagnosticSink | None,
    planning_budget: _AutoDivePlanningBudget | None = None,
) -> Callable[[], LocalVoxelVolume | None] | None:
    """Return a bounded 1 m field for local 3D frontier recovery."""
    common_payload = {
        "method": "local_frontier_surface_voxels_v1",
        "voxel_size_m": 1.0,
        "surface_inflation_m": 2.0,
        "radius_m": float(settings.voxel_local_refinement_radius_m),
        "forward_distance_m": float(
            settings.voxel_local_refinement_forward_m
        ),
        "max_cells": int(settings.voxel_local_refinement_max_cells),
    }
    if not bool(settings.voxel_analysis_enabled) or not bool(
        settings.voxel_local_refinement_enabled
    ):
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_local_refinement",
            {
                **common_payload,
                "built": False,
                "outcome": VOXEL_ANALYSIS_OUTCOME_DISABLED,
            },
        )
        return None
    if mesh_guard is None:
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_local_refinement",
            {
                **common_payload,
                "built": False,
                "outcome": VOXEL_ANALYSIS_OUTCOME_MESH_GUARD_UNAVAILABLE,
            },
        )
        return None

    try:
        current_point = tuple(float(value) for value in current)
    except (TypeError, ValueError):
        return None
    if len(current_point) != 3 or not all(
        math.isfinite(value) for value in current_point
    ):
        return None
    direction_values = tuple(
        float(value)
        for value in (
            (0.0, 0.0, 0.0) if forward is None else forward
        )
    )
    direction_norm = math.sqrt(
        sum(value * value for value in direction_values)
    )
    if direction_norm <= 1e-9:
        direction_values = (0.0, 0.0, 1.0)
        direction_norm = 1.0
    direction = tuple(value / direction_norm for value in direction_values)
    radius = max(4.0, float(settings.voxel_local_refinement_radius_m))
    forward_distance = max(
        radius,
        float(settings.voxel_local_refinement_forward_m),
    )
    bounds_min = tuple(
        current_point[axis]
        - radius
        + min(0.0, direction[axis] * forward_distance)
        for axis in range(3)
    )
    bounds_max = tuple(
        current_point[axis]
        + radius
        + max(0.0, direction[axis] * forward_distance)
        for axis in range(3)
    )

    def build() -> LocalVoxelVolume | None:
        try:
            if planning_budget is not None:
                planning_budget.check(
                    "local_frontier_mesh_sampling",
                    diagnostics=diagnostics,
                )
            meshes = tuple(
                mesh_guard.triangle_meshes_for_bounds(
                    bounds_min,
                    bounds_max,
                )
            )
            if not meshes:
                _record_auto_dive_diagnostic(
                    diagnostics,
                    "voxel_local_refinement",
                    {
                        **common_payload,
                        "built": False,
                        "outcome": VOXEL_ANALYSIS_OUTCOME_NO_TRIANGLES,
                        "bounds_min": [float(value) for value in bounds_min],
                        "bounds_max": [float(value) for value in bounds_max],
                    },
                )
                return None
            if planning_budget is not None:
                planning_budget.check(
                    "local_frontier_mesh_sampling",
                    diagnostics=diagnostics,
                )
            max_cells = max(
                4_096,
                min(
                    int(settings.voxel_local_refinement_max_cells),
                    131_072,
                ),
            )
            volume = build_surface_voxel_volume(
                meshes,
                bounds_min=bounds_min,
                bounds_max=bounds_max,
                config=VoxelVolumeConfig(
                    voxel_size_m=1.0,
                    surface_inflation_cells=2,
                    max_voxels=max_cells,
                    max_surface_samples=max(
                        16_384,
                        min(200_000, max_cells),
                    ),
                    max_clearance_search_cells=16,
                ),
                deadline_check=(
                    None
                    if planning_budget is None
                    else lambda: planning_budget.check(
                        "local_frontier_voxel_sampling",
                        diagnostics=diagnostics,
                    )
                ),
            )
            if planning_budget is not None:
                planning_budget.check(
                    "local_frontier_voxel_sampling",
                    diagnostics=diagnostics,
                )
        except AutoDivePlanningBudgetExceeded:
            raise
        except Exception as exc:
            _record_auto_dive_diagnostic(
                diagnostics,
                "voxel_local_refinement",
                {
                    **common_payload,
                    "built": False,
                    "outcome": VOXEL_ANALYSIS_OUTCOME_ERROR,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return None
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_local_refinement",
            {
                **common_payload,
                "built": True,
                "outcome": VOXEL_ANALYSIS_OUTCOME_BUILT,
                "actual_voxel_size_m": float(volume.voxel_size_m),
                "voxel_count": int(volume.voxel_count),
                "surface_cell_count": int(len(volume.surface_cells)),
                "triangle_count": int(volume.triangle_count),
                "surface_sample_count": int(volume.surface_sample_count),
                "sampling_truncated": bool(volume.sampling_truncated),
                "bounds_min": [float(value) for value in bounds_min],
                "bounds_max": [float(value) for value in bounds_max],
            },
        )
        return volume

    return build


def _materialize_auto_dive_voxel_volume(
    collision_validator: _AutoDiveCollisionValidator,
) -> _AutoDiveCollisionValidator:
    """Build the optional fine field or legacy volume once."""
    refinement_builder = collision_validator.voxel_refinement_builder
    if refinement_builder is not None:
        try:
            refinement = refinement_builder()
        except Exception:
            refinement = None
        return replace(
            collision_validator,
            voxel_refinement=refinement,
            voxel_refinement_builder=None,
        )
    builder = collision_validator.voxel_builder
    if builder is None:
        return collision_validator
    try:
        volume = builder()
    except Exception:
        volume = None
    return replace(
        collision_validator,
        voxel_volume=volume,
        voxel_builder=None,
    )


def _auto_dive_voxel_volume_payload(
    collision_validator: _AutoDiveCollisionValidator,
) -> dict[str, object] | None:
    volume = collision_validator.voxel_volume
    refinement = collision_validator.voxel_refinement
    if volume is None and refinement is None:
        return None
    payload = (
        {}
        if volume is None
        else volume.diagnostic_payload()
    )
    if volume is collision_validator.centerline_path.cached_voxel_volume:
        payload["source"] = "cache"
        payload["cache_metrics"] = getattr(
            collision_validator.centerline_path,
            "cached_voxel_metrics",
            None,
        )
    elif volume is not None:
        payload["source"] = "runtime"
    if refinement is not None:
        payload["local_refinement"] = refinement.diagnostic_payload()
        payload["local_refinement_source"] = "runtime"
    return payload


def _mesh_recovery_is_enabled(
    mesh_guard: CachedChunkMeshCollisionGuard | None,
) -> bool:
    if mesh_guard is None:
        return False
    return bool(getattr(mesh_guard, "mesh_recovery_enabled", True))


def _forward_travel_cone_route_candidates(
    scored: list[tuple[_AutoDiveRouteCandidateScore, _AutoDiveRouteCandidate]],
    *,
    current_point: Point,
    current_travel_yaw: float | None,
    current_travel_pitch: float | None,
    position_direction: np.ndarray | None,
    cell_size: float,
) -> list[tuple[_AutoDiveRouteCandidateScore, _AutoDiveRouteCandidate]]:
    if current_travel_yaw is None:
        return scored
    return [
        item
        for item in scored
        if _candidate_route_is_within_travel_cone(
            item[1],
            item[0],
            current_point=current_point,
            current_travel_yaw=current_travel_yaw,
            current_travel_pitch=current_travel_pitch,
            position_direction=position_direction,
            cell_size=cell_size,
        )
    ]


def _forward_travel_cone_filter_payload(
    scored: list[tuple[_AutoDiveRouteCandidateScore, _AutoDiveRouteCandidate]],
    *,
    current_point: Point,
    current_travel_yaw: float | None,
    current_travel_pitch: float | None,
    position_direction: np.ndarray | None,
    cell_size: float,
) -> dict[str, Any]:
    if current_travel_yaw is None:
        return {
            "enabled": False,
            "before_count": len(scored),
            "after_count": len(scored),
        }
    rejected: list[dict[str, Any]] = []
    accepted_count = 0
    for score, candidate in scored:
        alignment, target = _candidate_route_travel_alignment(
            candidate,
            score,
            current_point=current_point,
            current_travel_yaw=current_travel_yaw,
            current_travel_pitch=current_travel_pitch,
            position_direction=position_direction,
            cell_size=cell_size,
        )
        accepted = (
            alignment is not None
            and alignment >= _AUTO_DIVE_FORWARD_TRAVEL_CONE_ALIGNMENT
        )
        if accepted:
            accepted_count += 1
            continue
        if len(rejected) >= 8:
            continue
        rejected.append(
            {
                "name": candidate.name,
                "ordinal": int(candidate.ordinal),
                "alignment": None if alignment is None else float(alignment),
                "target": None if target is None else [float(value) for value in target],
                "route_clear": bool(score.route_clear),
                "mesh_clear": bool(score.mesh_clear),
                "entry_clear": bool(score.entry_clear),
                "length_m": float(score.length_m),
                "first_failure": _auto_dive_clearance_failure_payload(
                    score.first_clearance_failure
                ),
            }
        )
    return {
        "enabled": True,
        "cone_degrees": _AUTO_DIVE_FORWARD_TRAVEL_CONE_DEGREES,
        "before_count": len(scored),
        "after_count": accepted_count,
        "rejected": rejected,
    }


def _candidate_route_is_within_travel_cone(
    candidate: _AutoDiveRouteCandidate,
    score: _AutoDiveRouteCandidateScore,
    *,
    current_point: Point,
    current_travel_yaw: float | None,
    current_travel_pitch: float | None,
    position_direction: np.ndarray | None,
    cell_size: float,
) -> bool:
    alignment, _target = _candidate_route_travel_alignment(
        candidate,
        score,
        current_point=current_point,
        current_travel_yaw=current_travel_yaw,
        current_travel_pitch=current_travel_pitch,
        position_direction=position_direction,
        cell_size=cell_size,
    )
    return (
        alignment is not None
        and alignment >= _AUTO_DIVE_FORWARD_TRAVEL_CONE_ALIGNMENT
    )


def _candidate_route_travel_alignment(
    candidate: _AutoDiveRouteCandidate,
    score: _AutoDiveRouteCandidateScore,
    *,
    current_point: Point,
    current_travel_yaw: float | None,
    current_travel_pitch: float | None,
    position_direction: np.ndarray | None,
    cell_size: float,
) -> tuple[float | None, Point | None]:
    target_points = candidate.points
    if _score_reports_mesh_intersection(score):
        target_points = _trim_auto_dive_candidate_before_clearance_failure(
            candidate,
            score.first_clearance_failure,
            current_point=current_point,
            cell_size=cell_size,
        )
    route_points = _route_points_starting_at_current_camera(
        target_points,
        np.asarray(current_point, dtype=np.float64),
    )
    if len(route_points) < 2:
        return None, None
    first_move_target = _route_target_point_far_enough(
        route_points,
        current_point=current_point,
        cell_size=cell_size,
    )
    if first_move_target is None:
        return None, None

    def alignment_for(target: Point) -> float | None:
        if current_travel_yaw is not None:
            return _mesh_recovery_view_alignment(
                current_point,
                target,
                current_yaw=current_travel_yaw,
                current_pitch=current_travel_pitch,
            )
        return _auto_dive_target_alignment_from_direction(
            current_point,
            target,
            position_direction,
        )

    first_alignment = alignment_for(first_move_target)
    # A recovery path may need to step sideways around a blocking mesh before
    # it can resume the user's direction. Do not accept a path whose initial
    # step points backward, but let a later forward waypoint establish intent
    # for the final travel-cone decision. This keeps legitimate obstacle
    # avoidance possible while rejecting the old exactly-sideways fallback.
    if (
        candidate.name == "mesh-recovery"
        and first_alignment is not None
        and first_alignment >= 0.0
        and first_alignment < _AUTO_DIVE_FORWARD_TRAVEL_CONE_ALIGNMENT
    ):
        for target in route_points[2:]:
            alignment = alignment_for(target)
            if (
                alignment is not None
                and alignment >= _AUTO_DIVE_FORWARD_TRAVEL_CONE_ALIGNMENT
            ):
                return alignment, target
    return first_alignment, first_move_target


def _route_target_point_far_enough(
    route_points: tuple[Point, ...],
    *,
    current_point: Point,
    cell_size: float,
) -> Point | None:
    """Return the first meaningful movement target after the current camera."""
    current = np.asarray(current_point, dtype=np.float64)
    threshold_m = max(0.25, float(cell_size) * 0.25)
    for target in route_points[1:]:
        if (
            float(np.linalg.norm(np.asarray(target, dtype=np.float64) - current))
            >= threshold_m
        ):
            return target
    return None


def _auto_dive_candidate_score_payload(
    score: _AutoDiveRouteCandidateScore,
    candidate: _AutoDiveRouteCandidate,
) -> dict[str, Any]:
    return {
        "name": candidate.name,
        "ordinal": candidate.ordinal,
        "roll_deg": float(candidate.roll_deg),
        "route_clear": bool(score.route_clear),
        "entry_clear": bool(score.entry_clear),
        "mesh_clear": bool(score.mesh_clear),
        "geometry_trusted": bool(score.geometry_trusted),
        "min_lateral_clearance_cells": int(score.min_lateral_clearance_cells),
        "mean_lateral_clearance_cells": float(score.mean_lateral_clearance_cells),
        "min_clearance_margin_m": float(score.min_clearance_margin_m),
        "max_segment_length_m": float(score.max_segment_length_m),
        "max_segment_cells": float(score.max_segment_cells),
        "forward_progress_m": float(score.forward_progress_m),
        "pullback_penalty_m": float(score.pullback_penalty_m),
        "curvature_rad": float(score.curvature_rad),
        "vertical_jerk_m": float(score.vertical_jerk_m),
        "curvature_rad_per_m": float(score.curvature_rad_per_m),
        "vertical_jerk_m_per_m": float(score.vertical_jerk_m_per_m),
        "total_change_per_m": float(score.total_change_per_m),
        "length_m": float(score.length_m),
        "point_count": int(score.point_count),
        "first_clearance_failure": _auto_dive_clearance_failure_payload(
            score.first_clearance_failure
        ),
    }


def _auto_dive_clearance_failure_payload(
    failure: _AutoDiveClearanceFailure | None,
) -> dict[str, Any] | None:
    if failure is None:
        return None
    payload: dict[str, Any] = {
        "kind": failure.kind,
        "reason": failure.reason,
    }
    if failure.index is not None:
        payload["index"] = int(failure.index)
    if failure.segment_index is not None:
        payload["segment_index"] = int(failure.segment_index)
    if failure.cell is not None:
        payload["cell"] = [int(failure.cell[0]), int(failure.cell[1])]
    if failure.chunk_cell is not None:
        payload["chunk_cell"] = [
            int(failure.chunk_cell[0]),
            int(failure.chunk_cell[1]),
            int(failure.chunk_cell[2]),
        ]
    if failure.point is not None:
        payload["point"] = [float(value) for value in failure.point]
    if failure.first is not None:
        payload["first"] = [float(value) for value in failure.first]
    if failure.second is not None:
        payload["second"] = [float(value) for value in failure.second]
    return payload


def _dense_candidate_clearance_failure_is_tolerable(
    score: _AutoDiveRouteCandidateScore,
) -> bool:
    """Return whether a dense fallback failed only by hugging a wall.

    When footprint-only collision says a long shortcut is clear, that can be
    worse than a dense centerline route that briefly enters a tight cell:
    the long shortcut may be crossing span-filled or textureless wall space.
    Only low-lateral-clearance failures are allowed here; structural failures
    such as leaving the footprint or cutting a diagonal wall still disqualify
    the candidate.
    """
    if not score.entry_clear:
        return False
    if not score.mesh_clear:
        return False
    if score.first_clearance_failure is None:
        return True
    return score.first_clearance_failure.reason == "low_lateral_clearance"


def _dense_fallback_auto_dive_sort_key(
    score: _AutoDiveRouteCandidateScore,
) -> tuple[object, ...]:
    """Rank dense fallback routes by local validity, then smoothness.

    In this mode every candidate is geometry-trusted, but none is fully
    clearance-clear. Prefer the route that keeps the most margin and changes
    direction/elevation least, rather than the one that won only because it
    deleted more cave bends.
    """
    return (
        bool(score.entry_clear),
        int(score.min_lateral_clearance_cells),
        float(score.min_clearance_margin_m),
        -float(score.total_change_per_m),
        float(score.mean_lateral_clearance_cells),
        -float(score.max_segment_cells),
        float(score.forward_progress_m),
        -float(score.pullback_penalty_m),
        -float(score.length_m),
        -int(score.point_count),
    )


def _auto_dive_selection_sort_key(
    item: tuple[_AutoDiveRouteCandidateScore, _AutoDiveRouteCandidate],
    *,
    selection_reason: str,
    current_point: Point,
    cell_size: float,
) -> tuple[object, ...]:
    score, candidate = item
    if selection_reason == "trusted_dense_low_clearance_fallback":
        return _dense_fallback_auto_dive_sort_key(score)
    if selection_reason == "mesh_compromised_prefix_fallback":
        return _mesh_compromised_auto_dive_sort_key(
            score,
            candidate,
            current_point=current_point,
            cell_size=cell_size,
        )
    return score.sort_key


def _score_reports_mesh_intersection(score: _AutoDiveRouteCandidateScore) -> bool:
    failure = score.first_clearance_failure
    return failure is not None and failure.reason == "mesh_intersection"


def _mesh_compromised_auto_dive_sort_key(
    score: _AutoDiveRouteCandidateScore,
    candidate: _AutoDiveRouteCandidate,
    *,
    current_point: Point,
    cell_size: float,
) -> tuple[object, ...]:
    """Rank mesh-compromised routes by usable safe prefix.

    Until Guided Dive has a true mesh-aware rerouter, the best fallback is the
    route that moves farthest before the first cached-mesh intersection, then
    trims the planned route before that wall. This avoids the hard no-motion
    regression while still not knowingly planning through cached mesh.
    """
    trimmed_points = _trim_auto_dive_candidate_before_clearance_failure(
        candidate,
        score.first_clearance_failure,
        current_point=current_point,
        cell_size=cell_size,
    )
    safe_length_m = path_length(
        _route_points_starting_at_current_camera(
            trimmed_points,
            np.asarray(current_point, dtype=np.float64),
        )
    )
    net_progress_m = _trimmed_prefix_net_progress_m(
        trimmed_points,
        current_point=current_point,
    )
    return (
        net_progress_m > max(0.25, float(cell_size) * 0.25),
        float(net_progress_m),
        safe_length_m > 1e-6,
        float(safe_length_m),
        bool(score.entry_clear),
        bool(score.geometry_trusted),
        int(score.min_lateral_clearance_cells),
        float(score.min_clearance_margin_m),
        -float(score.total_change_per_m),
        float(score.mean_lateral_clearance_cells),
        -float(score.pullback_penalty_m),
    )


def _trimmed_prefix_net_progress_m(
    points: tuple[Point, ...],
    *,
    current_point: Point,
) -> float:
    if not points:
        return 0.0
    last = np.asarray(points[-1], dtype=np.float64)
    current = np.asarray(current_point, dtype=np.float64)
    return float(np.linalg.norm(last - current))


def _trim_auto_dive_candidate_before_clearance_failure(
    candidate: _AutoDiveRouteCandidate,
    failure: _AutoDiveClearanceFailure | None,
    *,
    current_point: Point,
    cell_size: float,
) -> tuple[Point, ...]:
    if (
        failure is None
        or failure.reason != "mesh_intersection"
        or failure.segment_index is None
    ):
        return candidate.points

    route_with_current = _route_points_starting_at_current_camera(
        candidate.points,
        np.asarray(current_point, dtype=np.float64),
    )
    segment_index = max(
        0,
        min(int(failure.segment_index), len(route_with_current) - 1),
    )
    trimmed: list[Point] = list(route_with_current[1 : segment_index + 1])
    safe_stop = _safe_stop_before_mesh_failure(
        failure,
        cell_size=cell_size,
    )
    if safe_stop is not None and (
        not trimmed or not _points_almost_equal(trimmed[-1], safe_stop)
    ):
        trimmed.append(safe_stop)
    return _dedupe_consecutive_points(tuple(trimmed))


def _safe_stop_before_mesh_failure(
    failure: _AutoDiveClearanceFailure,
    *,
    cell_size: float,
) -> Point | None:
    if failure.first is None or failure.point is None:
        return None
    first = np.asarray(failure.first, dtype=np.float64)
    hit = np.asarray(failure.point, dtype=np.float64)
    delta = hit - first
    distance = float(np.linalg.norm(delta))
    if not math.isfinite(distance) or distance <= 1e-6:
        return None
    pullback_m = max(
        DEFAULT_AUTO_DIVE_MESH_BOUNDARY_PULLBACK_M,
        float(cell_size) * 0.25,
    )
    stop_distance = distance - pullback_m
    if stop_distance <= 1e-4:
        return None
    stop = first + (delta / distance) * stop_distance
    return (float(stop[0]), float(stop[1]), float(stop[2]))


def _points_almost_equal(first: Point, second: Point) -> bool:
    return (
        abs(float(first[0]) - float(second[0])) <= 1e-6
        and abs(float(first[1]) - float(second[1])) <= 1e-6
        and abs(float(first[2]) - float(second[2])) <= 1e-6
    )


def _build_mesh_recovery_auto_dive_route_candidate(
    *,
    ordinal: int,
    centerline_path: CenterlinePath,
    route_samples: _AutoDiveRouteSamples,
    current: np.ndarray,
    current_yaw: float | None,
    current_pitch: float | None,
    current_travel_yaw: float | None,
    current_travel_pitch: float | None,
    avoid_positions: Sequence[Sequence[float]] | None,
    allow_reverse_travel: bool = False,
    collision_validator: _AutoDiveCollisionValidator,
    use_footprint_centers: bool = False,
    diagnostics: AutoDiveDiagnosticSink | None,
    planning_budget: _AutoDivePlanningBudget | None = None,
) -> _AutoDiveRouteCandidate | None:
    """Build a mesh-clear footprint route when all smooth candidates hit a wall."""
    if not collision_validator.has_mesh_collision_guard:
        return None
    if planning_budget is not None:
        planning_budget.check("mesh_recovery_route_build", diagnostics=diagnostics)
    current_point: Point = (
        float(current[0]),
        float(current[1]),
        float(current[2]),
    )
    start_cell = _current_footprint_cell(centerline_path, current)
    if start_cell not in centerline_path.component_cells:
        start_cell = min(
            centerline_path.component_cells,
            key=lambda cell: (
                _cell_center_distance_squared(centerline_path, cell, current),
                cell,
            ),
        )
    target_indices = _mesh_recovery_target_indices(
        route_samples.cells,
        start_cell=start_cell,
        component_cells=centerline_path.component_cells,
    )
    if not target_indices:
        return None

    cells = _mesh_clear_recovery_footprint_path(
        centerline_path,
        start_cell=start_cell,
        target_indices=target_indices,
        current_point=current_point,
        current_yaw=current_yaw,
        current_pitch=current_pitch,
        current_travel_yaw=current_travel_yaw,
        current_travel_pitch=current_travel_pitch,
        avoid_positions=avoid_positions,
        allow_reverse_travel=allow_reverse_travel,
        collision_validator=collision_validator,
        use_footprint_centers=use_footprint_centers,
        diagnostics=diagnostics,
        planning_budget=planning_budget,
    )
    if len(cells) < 2:
        return None

    points = tuple(
        _mesh_recovery_point_for_cell(
            centerline_path,
            cell,
            fallback_y=current_point[1],
            voxel_volume=collision_validator.voxel_volume,
            use_footprint_center=use_footprint_centers,
        )
        for cell in cells[1:]
    )
    points = _dedupe_consecutive_points(points, min_distance_m=1e-6)
    if not points:
        return None
    return _AutoDiveRouteCandidate(
        ordinal=ordinal,
        name="mesh-recovery",
        cells=tuple(cells),
        points=points,
    )


def _mesh_recovery_target_indices(
    route_cells: tuple[FootprintCell, ...],
    *,
    start_cell: FootprintCell,
    component_cells: frozenset[FootprintCell],
) -> dict[FootprintCell, int]:
    target_indices: dict[FootprintCell, int] = {}
    for index, cell in enumerate(_dedupe_consecutive_cells(route_cells)):
        if cell == start_cell or cell not in component_cells:
            continue
        target_indices[cell] = max(index, target_indices.get(cell, -1))
    return target_indices


def _mesh_clear_recovery_footprint_path(
    centerline_path: CenterlinePath,
    *,
    start_cell: FootprintCell,
    target_indices: Mapping[FootprintCell, int],
    current_point: Point,
    current_yaw: float | None,
    current_pitch: float | None,
    current_travel_yaw: float | None,
    current_travel_pitch: float | None,
    avoid_positions: Sequence[Sequence[float]] | None,
    allow_reverse_travel: bool = False,
    collision_validator: _AutoDiveCollisionValidator,
    use_footprint_centers: bool = False,
    diagnostics: AutoDiveDiagnosticSink | None = None,
    planning_budget: _AutoDivePlanningBudget | None = None,
) -> tuple[FootprintCell, ...]:
    component = centerline_path.component_cells
    if start_cell not in component:
        return ()
    max_clearance = max(
        centerline_path.clearance_scores.get(cell, 1)
        for cell in component
    )
    frontier: list[tuple[float, FootprintCell]] = [(0.0, start_cell)]
    previous: dict[FootprintCell, FootprintCell | None] = {start_cell: None}
    previous_steps: dict[FootprintCell, tuple[int, int] | None] = {
        start_cell: None,
    }
    costs: dict[FootprintCell, float] = {start_cell: 0.0}
    path_distances: dict[FootprintCell, float] = {start_cell: 0.0}
    turn_penalties: dict[FootprintCell, float] = {start_cell: 0.0}
    edge_cache: dict[tuple[FootprintCell, FootprintCell], bool] = {}
    point_cache: dict[FootprintCell, Point] = {
        start_cell: current_point,
    }
    avoid_cells = _mesh_recovery_avoid_cells(
        centerline_path,
        avoid_positions=avoid_positions,
    )
    best_cell: FootprintCell | None = None
    best_key: tuple[object, ...] | None = None
    best_candidate_payload: dict[str, Any] | None = None
    candidate_payloads: list[tuple[tuple[object, ...], dict[str, Any]]] = []
    visited_cells = 0
    edge_tests = 0
    edge_clear_count = 0
    edge_blocked_count = 0
    rejected_behind_count = 0
    rejected_avoided_count = 0
    rejected_too_close_count = 0
    budget_exhausted = False
    max_visited_cells = _AUTO_DIVE_MESH_RECOVERY_MAX_VISITED_CELLS
    max_edge_tests = _AUTO_DIVE_MESH_RECOVERY_MAX_EDGE_TESTS
    if planning_budget is not None and planning_budget.budget_s is not None:
        # A runtime replan must remain local even when a mesh query is slower
        # than expected. The cooperative deadline is still authoritative;
        # these smaller search caps keep the worker from spending the whole
        # deadline exploring a large component one edge at a time.
        max_visited_cells = min(
            max_visited_cells,
            max(32, int(float(planning_budget.budget_s) * 32.0)),
        )
        max_edge_tests = min(
            max_edge_tests,
            max(16, int(float(planning_budget.budget_s) * 8.0)),
        )
    selection_yaw = (
        current_travel_yaw
        if current_travel_yaw is not None
        else current_yaw
    )
    selection_pitch = (
        current_travel_pitch
        if current_travel_pitch is not None
        else current_pitch
    )
    position_direction = _auto_dive_current_position_offset_direction(
        centerline_path,
        current=np.asarray(current_point, dtype=np.float64),
        current_point=current_point,
    )
    last_cell: FootprintCell | None = None
    last_neighbor: FootprintCell | None = None

    def record_budget_abort(exc: AutoDivePlanningBudgetExceeded) -> None:
        top_candidates = [
            payload
            for _key, payload in sorted(
                candidate_payloads,
                key=lambda item: item[0],
                reverse=True,
            )[:12]
        ]
        payload: dict[str, Any] = {
            "selected": None,
            "voxel_volume": _auto_dive_voxel_volume_payload(
                collision_validator
            ),
            "start_cell": [int(start_cell[0]), int(start_cell[1])],
            "visited_cells": int(visited_cells),
            "target_count": int(len(target_indices)),
            "candidate_count": int(len(candidate_payloads)),
            "rejected_behind_count": int(rejected_behind_count),
            "rejected_avoided_count": int(rejected_avoided_count),
            "rejected_too_close_count": int(rejected_too_close_count),
            "edge_tests": int(edge_tests),
            "edge_clear_count": int(edge_clear_count),
            "edge_blocked_count": int(edge_blocked_count),
            "allow_reverse_travel": bool(allow_reverse_travel),
            "budget_exhausted": True,
            "aborted": True,
            "abort_phase": str(exc.phase),
            "budget_s": float(exc.budget_s),
            "budget_elapsed_s": float(exc.elapsed_s),
            "max_visited_cells": int(max_visited_cells),
            "max_edge_tests": int(max_edge_tests),
            "travel_cone_degrees": _AUTO_DIVE_FORWARD_TRAVEL_CONE_DEGREES,
            "scan_yaw_range_degrees": [-120.0, 120.0],
            "selection_turn_penalty_cells": float(
                _AUTO_DIVE_MESH_RECOVERY_SELECTION_TURN_PENALTY_CELLS
            ),
            "selection_turn_penalty_max_fraction": float(
                _AUTO_DIVE_MESH_RECOVERY_SELECTION_TURN_PENALTY_MAX_FRACTION
            ),
            "path_avoidance_radius_cells": int(
                _AUTO_DIVE_MESH_RECOVERY_PATH_AVOIDANCE_RADIUS_CELLS
            ),
            "top_candidates": top_candidates,
        }
        if best_candidate_payload is not None:
            payload["best_candidate"] = best_candidate_payload
        if last_cell is not None:
            payload["last_cell"] = [int(last_cell[0]), int(last_cell[1])]
        if last_neighbor is not None:
            payload["last_neighbor"] = [
                int(last_neighbor[0]),
                int(last_neighbor[1]),
            ]
        _record_auto_dive_diagnostic(
            diagnostics,
            "mesh_recovery_search",
            payload,
        )

    while frontier:
        if planning_budget is not None:
            try:
                planning_budget.check(
                    "mesh_recovery_search",
                    diagnostics=diagnostics,
                )
            except AutoDivePlanningBudgetExceeded as exc:
                record_budget_abort(exc)
                raise
        if visited_cells >= max_visited_cells:
            budget_exhausted = True
            break
        current_cost, cell = heapq.heappop(frontier)
        if current_cost > costs[cell]:
            continue
        last_cell = cell
        visited_cells += 1
        target_point = _mesh_recovery_graph_point(
            centerline_path,
            cell,
            current_point=current_point,
            start_cell=start_cell,
            point_cache=point_cache,
            voxel_volume=collision_validator.voxel_volume,
            use_footprint_center=use_footprint_centers,
        )
        net_distance_m = float(
            np.linalg.norm(
                np.asarray(target_point, dtype=np.float64)
                - np.asarray(current_point, dtype=np.float64)
            )
        )
        target_allowed_by_pose = (
            selection_yaw is not None
            or position_direction is not None
            or cell in target_indices
        )
        far_enough = net_distance_m >= max(
            0.5,
            centerline_path.footprint_cell_size * 0.5,
        )
        target_avoided = (
            far_enough
            and _mesh_recovery_target_is_avoided(
                target_point,
                avoid_positions=avoid_positions,
                cell_size=centerline_path.footprint_cell_size,
            )
        )
        if not far_enough:
            rejected_too_close_count += 1
        elif target_avoided:
            rejected_avoided_count += 1
        if target_allowed_by_pose and far_enough and not target_avoided:
            forward_alignment = _mesh_recovery_view_alignment(
                current_point,
                target_point,
                current_yaw=selection_yaw,
                current_pitch=selection_pitch,
            )
            position_alignment = _auto_dive_target_alignment_from_direction(
                current_point,
                target_point,
                position_direction,
            )
            direction_alignment = (
                forward_alignment
                if selection_yaw is not None
                else position_alignment
            )
            best_intent_alignment = (
                0.0
                if direction_alignment is None
                else float(direction_alignment)
            )
            target_in_front = (
                allow_reverse_travel
                or direction_alignment is None
                or (
                    direction_alignment
                    >= _AUTO_DIVE_MESH_RECOVERY_TARGET_ALIGNMENT
                )
            )
            if target_in_front:
                scan_in_cone, scan_alignment, scan_angle_penalty = (
                    _mesh_recovery_scan_alignment(
                        current_point,
                        target_point,
                        current_yaw=selection_yaw,
                        current_pitch=selection_pitch,
                    )
                )
                direct_alignment = _mesh_recovery_view_alignment(
                    current_point,
                    target_point,
                    current_yaw=current_yaw,
                    current_pitch=current_pitch,
                )
                path_distance_cells = float(path_distances.get(cell, current_cost))
                path_distance_m = (
                    path_distance_cells
                    * float(centerline_path.footprint_cell_size)
                )
                turn_penalty_rad = float(turn_penalties.get(cell, 0.0))
                selection_turn_penalty_uncapped_m = (
                    turn_penalty_rad
                    * _AUTO_DIVE_MESH_RECOVERY_SELECTION_TURN_PENALTY_CELLS
                    * float(centerline_path.footprint_cell_size)
                )
                selection_turn_penalty_cap_m = (
                    path_distance_m
                    * _AUTO_DIVE_MESH_RECOVERY_SELECTION_TURN_PENALTY_MAX_FRACTION
                )
                selection_turn_penalty_m = min(
                    selection_turn_penalty_uncapped_m,
                    selection_turn_penalty_cap_m,
                )
                straightness = net_distance_m / max(1e-9, path_distance_m)
                path_quality_m = (
                    path_distance_m - selection_turn_penalty_m
                )
                candidate_path = _mesh_recovery_cell_path(previous, cell)
                path_volume_m3 = sum(
                    _mesh_recovery_cell_available_volume(centerline_path, path_cell)
                    for path_cell in candidate_path
                )
                (
                    path_avoidance_count,
                    path_avoidance_min_distance_cells,
                    path_avoidance_cells,
                ) = _mesh_recovery_path_avoidance(
                    candidate_path,
                    avoid_cells=avoid_cells,
                    start_cell=start_cell,
                )
                path_avoidance_penalty_m = (
                    float(path_avoidance_count)
                    * float(centerline_path.footprint_cell_size)
                    * 8.0
                )
                selection_score_m = path_quality_m - path_avoidance_penalty_m
                cached_hotspot = _mesh_recovery_cached_hotspot_payload(
                    centerline_path,
                    cell,
                )
                cached_hotspot_score = (
                    0.0
                    if cached_hotspot is None
                    else float(cached_hotspot.get("score", 0.0))
                )
                cached_volume_per_route = (
                    0.0
                    if cached_hotspot is None
                    else float(cached_hotspot.get("volume_per_route_m", 0.0))
                )
                cached_volume_m3 = (
                    0.0
                    if cached_hotspot is None
                    else float(cached_hotspot.get("available_volume_m3", 0.0))
                )
                key = (
                    bool(scan_in_cone),
                    (
                        (selection_yaw is None and position_alignment is None)
                        or best_intent_alignment
                        >= _AUTO_DIVE_MESH_RECOVERY_FORWARD_ALIGNMENT
                    ),
                    -int(path_avoidance_count),
                    float(selection_score_m),
                    float(path_quality_m),
                    float(path_volume_m3),
                    float(cached_volume_per_route),
                    float(cached_volume_m3),
                    float(cached_hotspot_score),
                    float(path_distance_m),
                    float(net_distance_m),
                    float(straightness),
                    int(target_indices.get(cell, -1)),
                    float(best_intent_alignment),
                    (
                        -1.0
                        if position_alignment is None
                        else float(position_alignment)
                    ),
                    float(forward_alignment),
                    float(scan_alignment),
                    float(direct_alignment),
                    int(centerline_path.clearance_scores.get(cell, 1)),
                    -float(turn_penalty_rad),
                    -float(scan_angle_penalty),
                    cell,
                )
                candidate_payload = {
                    "cell": [int(cell[0]), int(cell[1])],
                    "target": [float(value) for value in target_point],
                    "selection_score_m": float(selection_score_m),
                    "path_quality_m": float(path_quality_m),
                    "path_volume_m3": float(path_volume_m3),
                    "path_distance_m": float(path_distance_m),
                    "net_distance_m": float(net_distance_m),
                    "straightness": float(straightness),
                    "route_target_index": int(target_indices.get(cell, -1)),
                    "intent_alignment": float(best_intent_alignment),
                    "position_alignment": (
                        None
                        if position_alignment is None
                        else float(position_alignment)
                    ),
                    "forward_alignment": float(forward_alignment),
                    "scan_alignment": float(scan_alignment),
                    "direct_alignment": float(direct_alignment),
                    "clearance_score": int(
                        centerline_path.clearance_scores.get(cell, 1)
                    ),
                    "turn_penalty_rad": float(turn_penalty_rad),
                    "selection_turn_penalty_m": float(selection_turn_penalty_m),
                    "selection_turn_penalty_uncapped_m": float(
                        selection_turn_penalty_uncapped_m
                    ),
                    "selection_turn_penalty_cap_m": float(
                        selection_turn_penalty_cap_m
                    ),
                    "path_avoidance_count": int(path_avoidance_count),
                    "path_avoidance_penalty_m": float(path_avoidance_penalty_m),
                    "path_avoidance_min_distance_cells": (
                        None
                        if path_avoidance_min_distance_cells is None
                        else float(path_avoidance_min_distance_cells)
                    ),
                    "path_avoidance_cells": [
                        [int(cell[0]), int(cell[1])]
                        for cell in path_avoidance_cells[:8]
                    ],
                    "path_avoidance_cells_truncated": (
                        len(path_avoidance_cells) > 8
                    ),
                    "path_cell_count": len(candidate_path),
                    "path_cells": [
                        [int(path_cell[0]), int(path_cell[1])]
                        for path_cell in candidate_path[:24]
                    ],
                    "path_cells_truncated": len(candidate_path) > 24,
                    "scan_angle_penalty_deg": float(scan_angle_penalty),
                }
                candidate_payload["cached_hotspot_score"] = float(
                    cached_hotspot_score
                )
                candidate_payload["cached_volume_per_route_m"] = float(
                    cached_volume_per_route
                )
                candidate_payload["cached_available_volume_m3"] = float(
                    cached_volume_m3
                )
                if cached_hotspot is not None:
                    candidate_payload["cached_hotspot"] = cached_hotspot
                candidate_payloads.append((key, candidate_payload))
                if best_key is None or key > best_key:
                    best_key = key
                    best_cell = cell
                    best_candidate_payload = candidate_payload
            else:
                rejected_behind_count += 1

        for neighbor in navigable_footprint_neighbors(cell, component):
            last_neighbor = neighbor
            if planning_budget is not None:
                try:
                    planning_budget.check(
                        "mesh_recovery_edge_search",
                        diagnostics=diagnostics,
                    )
                except AutoDivePlanningBudgetExceeded as exc:
                    record_budget_abort(exc)
                    raise
            edge_is_cached = (
                (cell, neighbor) in edge_cache
                or (neighbor, cell) in edge_cache
            )
            if (
                not edge_is_cached
                and edge_tests >= max_edge_tests
            ):
                budget_exhausted = True
                break
            edge_cache_count = len(edge_cache)
            edge_clear = _mesh_recovery_edge_is_clear(
                centerline_path,
                cell,
                neighbor,
                current_point=current_point,
                start_cell=start_cell,
                collision_validator=collision_validator,
                point_cache=point_cache,
                edge_cache=edge_cache,
                use_footprint_centers=use_footprint_centers,
            )
            if len(edge_cache) > edge_cache_count:
                edge_tests += 1
                if edge_clear:
                    edge_clear_count += 1
                else:
                    edge_blocked_count += 1
            if planning_budget is not None:
                try:
                    planning_budget.check(
                        "mesh_recovery_edge_result",
                        diagnostics=diagnostics,
                    )
                except AutoDivePlanningBudgetExceeded as exc:
                    record_budget_abort(exc)
                    raise
            if not edge_clear:
                continue
            step_distance = footprint_cell_distance(cell, neighbor)
            step = (neighbor[0] - cell[0], neighbor[1] - cell[1])
            turn_penalty = _mesh_recovery_turn_angle(
                previous_steps.get(cell),
                step,
            )
            clearance_penalty = (
                (max_clearance - centerline_path.clearance_scores.get(neighbor, 1))
                / max(1, max_clearance)
            )
            next_cost = (
                current_cost
                + step_distance * (1.0 + clearance_penalty)
                + turn_penalty * _AUTO_DIVE_MESH_RECOVERY_TURN_PENALTY_CELLS
            )
            if next_cost >= costs.get(neighbor, math.inf):
                continue
            costs[neighbor] = next_cost
            previous[neighbor] = cell
            previous_steps[neighbor] = step
            path_distances[neighbor] = (
                path_distances.get(cell, 0.0) + step_distance
            )
            turn_penalties[neighbor] = (
                turn_penalties.get(cell, 0.0) + turn_penalty
            )
            heapq.heappush(frontier, (next_cost, neighbor))
        if budget_exhausted:
            break

    if best_cell is None:
        _record_auto_dive_diagnostic(
            diagnostics,
            "mesh_recovery_search",
            {
                "selected": None,
                "voxel_volume": _auto_dive_voxel_volume_payload(
                    collision_validator
                ),
                "start_cell": [int(start_cell[0]), int(start_cell[1])],
                "visited_cells": int(visited_cells),
                "target_count": int(len(target_indices)),
                "candidate_count": int(len(candidate_payloads)),
                "rejected_behind_count": int(rejected_behind_count),
                "rejected_avoided_count": int(rejected_avoided_count),
                "rejected_too_close_count": int(rejected_too_close_count),
                "edge_tests": int(edge_tests),
                "edge_clear_count": int(edge_clear_count),
                "edge_blocked_count": int(edge_blocked_count),
                "allow_reverse_travel": bool(allow_reverse_travel),
                "budget_exhausted": bool(budget_exhausted),
                "max_visited_cells": int(max_visited_cells),
                "max_edge_tests": int(max_edge_tests),
                "travel_cone_degrees": _AUTO_DIVE_FORWARD_TRAVEL_CONE_DEGREES,
                "scan_yaw_range_degrees": [-120.0, 120.0],
                "selection_turn_penalty_cells": float(
                    _AUTO_DIVE_MESH_RECOVERY_SELECTION_TURN_PENALTY_CELLS
                ),
                "selection_turn_penalty_max_fraction": float(
                    _AUTO_DIVE_MESH_RECOVERY_SELECTION_TURN_PENALTY_MAX_FRACTION
                ),
                "path_avoidance_radius_cells": int(
                    _AUTO_DIVE_MESH_RECOVERY_PATH_AVOIDANCE_RADIUS_CELLS
                ),
                "avoid_cells": [
                    [int(cell[0]), int(cell[1])]
                    for cell in sorted(avoid_cells)[:16]
                ],
                "avoid_cells_truncated": len(avoid_cells) > 16,
            },
        )
        return ()
    path: list[FootprintCell] = []
    cell: FootprintCell | None = best_cell
    while cell is not None:
        path.append(cell)
        cell = previous[cell]
    selected_path = tuple(reversed(path))
    top_candidates = [
        payload
        for _key, payload in sorted(
            candidate_payloads,
            key=lambda item: item[0],
            reverse=True,
        )[:12]
    ]
    _record_auto_dive_diagnostic(
        diagnostics,
        "mesh_recovery_search",
        {
            "selected": [int(best_cell[0]), int(best_cell[1])],
            "voxel_volume": _auto_dive_voxel_volume_payload(
                collision_validator
            ),
            "selected_candidate": best_candidate_payload,
            "selected_path_cell_count": len(selected_path),
            "selected_path_cells": [
                [int(cell[0]), int(cell[1])]
                for cell in selected_path[:24]
            ],
            "selected_path_truncated": len(selected_path) > 24,
            "start_cell": [int(start_cell[0]), int(start_cell[1])],
            "visited_cells": int(visited_cells),
            "target_count": int(len(target_indices)),
            "candidate_count": int(len(candidate_payloads)),
            "rejected_behind_count": int(rejected_behind_count),
            "rejected_avoided_count": int(rejected_avoided_count),
            "rejected_too_close_count": int(rejected_too_close_count),
            "edge_tests": int(edge_tests),
            "edge_clear_count": int(edge_clear_count),
            "edge_blocked_count": int(edge_blocked_count),
            "allow_reverse_travel": bool(allow_reverse_travel),
            "budget_exhausted": bool(budget_exhausted),
            "max_visited_cells": int(max_visited_cells),
            "max_edge_tests": int(max_edge_tests),
            "travel_cone_degrees": _AUTO_DIVE_FORWARD_TRAVEL_CONE_DEGREES,
            "scan_yaw_range_degrees": [-120.0, 120.0],
            "selection_turn_penalty_cells": float(
                _AUTO_DIVE_MESH_RECOVERY_SELECTION_TURN_PENALTY_CELLS
            ),
            "selection_turn_penalty_max_fraction": float(
                _AUTO_DIVE_MESH_RECOVERY_SELECTION_TURN_PENALTY_MAX_FRACTION
            ),
            "path_avoidance_radius_cells": int(
                _AUTO_DIVE_MESH_RECOVERY_PATH_AVOIDANCE_RADIUS_CELLS
            ),
            "avoid_cells": [
                [int(cell[0]), int(cell[1])]
                for cell in sorted(avoid_cells)[:16]
            ],
            "avoid_cells_truncated": len(avoid_cells) > 16,
            "top_candidates": top_candidates,
        },
    )
    return selected_path


def _mesh_recovery_cell_path(
    previous: Mapping[FootprintCell, FootprintCell | None],
    cell: FootprintCell,
) -> tuple[FootprintCell, ...]:
    path: list[FootprintCell] = []
    current: FootprintCell | None = cell
    while current is not None:
        path.append(current)
        current = previous[current]
    return tuple(reversed(path))


def _mesh_recovery_avoid_cells(
    centerline_path: CenterlinePath,
    *,
    avoid_positions: Sequence[Sequence[float]] | None,
) -> frozenset[FootprintCell]:
    if not avoid_positions:
        return frozenset()
    cell_size = float(centerline_path.footprint_cell_size)
    cells: set[FootprintCell] = set()
    for position in avoid_positions:
        try:
            point = np.asarray(position, dtype=np.float64).reshape(3)
        except Exception:
            continue
        cell = (
            int(math.floor(float(point[0]) / cell_size)),
            int(math.floor(float(point[2]) / cell_size)),
        )
        if cell in centerline_path.component_cells:
            cells.add(cell)
    return frozenset(cells)


def _mesh_recovery_path_avoidance(
    path: tuple[FootprintCell, ...],
    *,
    avoid_cells: frozenset[FootprintCell],
    start_cell: FootprintCell,
) -> tuple[int, float | None, tuple[FootprintCell, ...]]:
    if not path or not avoid_cells:
        return 0, None, ()

    radius = int(_AUTO_DIVE_MESH_RECOVERY_PATH_AVOIDANCE_RADIUS_CELLS)
    hit_count = 0
    min_distance_cells: float | None = None
    hit_cells: list[FootprintCell] = []
    seen_hit_cells: set[FootprintCell] = set()
    for cell in path[1:]:
        if cell == start_cell:
            continue
        nearest = min(
            max(
                abs(int(cell[0]) - int(avoid_cell[0])),
                abs(int(cell[1]) - int(avoid_cell[1])),
            )
            for avoid_cell in avoid_cells
        )
        nearest_f = float(nearest)
        if min_distance_cells is None or nearest_f < min_distance_cells:
            min_distance_cells = nearest_f
        if nearest <= radius:
            hit_count += 1
            if cell not in seen_hit_cells:
                hit_cells.append(cell)
                seen_hit_cells.add(cell)
    return hit_count, min_distance_cells, tuple(hit_cells)


def _mesh_recovery_turn_angle(
    previous_step: tuple[int, int] | None,
    next_step: tuple[int, int],
) -> float:
    if previous_step is None:
        return 0.0
    previous_x, previous_z = previous_step
    next_x, next_z = next_step
    previous_norm = math.hypot(previous_x, previous_z)
    next_norm = math.hypot(next_x, next_z)
    if previous_norm <= 1e-9 or next_norm <= 1e-9:
        return 0.0
    dot = (previous_x * next_x) + (previous_z * next_z)
    alignment = max(-1.0, min(1.0, dot / (previous_norm * next_norm)))
    return float(math.acos(alignment))


def _mesh_recovery_cached_hotspot_payload(
    centerline_path: CenterlinePath,
    cell: FootprintCell,
) -> dict[str, float] | None:
    hotspots = getattr(centerline_path, "cached_recovery_hotspots", None) or {}
    hotspot = hotspots.get(cell)
    if not hotspot:
        volume = getattr(centerline_path, "cached_voxel_volume", None)
        if isinstance(volume, NavigationVoxelAtlas):
            metric = volume.cell_metrics.get(cell)
            if metric is not None:
                available_volume = max(
                    0.0,
                    float(getattr(metric, "available_volume_m3", 0.0)),
                )
                return {
                    "available_volume_m3": available_volume,
                    "volume_per_route_m": available_volume
                    / max(1e-6, float(centerline_path.footprint_cell_size)),
                    "voxel_mean_clearance_m": max(
                        0.0,
                        float(getattr(metric, "mean_clearance_m", 0.0)),
                    ),
                }
        return None
    payload: dict[str, float] = {}
    for key in (
        "score",
        "clearance_score",
        "straight_run_cells",
        "corridor_run_cells",
        "degree_score",
        "available_volume_m3",
        "volume_per_route_m",
        "voxel_mean_clearance_m",
    ):
        value = hotspot.get(key)
        if value is None:
            continue
        payload[key] = float(value)
    return payload or None


def _mesh_recovery_cell_available_volume(
    centerline_path: CenterlinePath,
    cell: FootprintCell,
) -> float:
    """Return cache-time filled volume for a recovery corridor cell."""
    hotspot = _mesh_recovery_cached_hotspot_payload(centerline_path, cell)
    if hotspot is None:
        return 0.0
    return max(0.0, float(hotspot.get("available_volume_m3", 0.0)))


def _mesh_recovery_edge_is_clear(
    centerline_path: CenterlinePath,
    first_cell: FootprintCell,
    second_cell: FootprintCell,
    *,
    current_point: Point,
    start_cell: FootprintCell,
    collision_validator: _AutoDiveCollisionValidator,
    point_cache: dict[FootprintCell, Point],
    edge_cache: dict[tuple[FootprintCell, FootprintCell], bool],
    use_footprint_centers: bool = False,
) -> bool:
    edge_key = (
        first_cell,
        second_cell,
    )
    reverse_key = (
        second_cell,
        first_cell,
    )
    if edge_key in edge_cache:
        return edge_cache[edge_key]
    if reverse_key in edge_cache:
        return edge_cache[reverse_key]

    first_point = _mesh_recovery_graph_point(
        centerline_path,
        first_cell,
        current_point=current_point,
        start_cell=start_cell,
        point_cache=point_cache,
        voxel_volume=collision_validator.voxel_volume,
        use_footprint_center=use_footprint_centers,
    )
    second_point = _mesh_recovery_graph_point(
        centerline_path,
        second_cell,
        current_point=current_point,
        start_cell=start_cell,
        point_cache=point_cache,
        voxel_volume=collision_validator.voxel_volume,
        use_footprint_center=use_footprint_centers,
    )
    clear = (
        collision_validator.segment_clearance_failure(
            first_point,
            second_point,
            allow_low_lateral_clearance=use_footprint_centers,
        )
        is None
    )
    edge_cache[edge_key] = clear
    return clear


def _mesh_recovery_graph_point(
    centerline_path: CenterlinePath,
    cell: FootprintCell,
    *,
    current_point: Point,
    start_cell: FootprintCell,
    point_cache: dict[FootprintCell, Point],
    voxel_volume: LocalVoxelVolume | NavigationVoxelAtlas | None = None,
    use_footprint_center: bool = False,
) -> Point:
    cached = point_cache.get(cell)
    if cached is not None:
        return cached
    if cell == start_cell:
        point_cache[cell] = current_point
        return current_point
    point = _mesh_recovery_point_for_cell(
        centerline_path,
        cell,
        fallback_y=current_point[1],
        voxel_volume=voxel_volume,
        use_footprint_center=use_footprint_center,
    )
    point_cache[cell] = point
    return point


def _mesh_recovery_point_for_cell(
    centerline_path: CenterlinePath,
    cell: FootprintCell,
    *,
    fallback_y: float,
    voxel_volume: LocalVoxelVolume | NavigationVoxelAtlas | None = None,
    use_footprint_center: bool = False,
) -> Point:
    if use_footprint_center:
        x, z = footprint_world_center(
            cell,
            centerline_path.footprint_cell_size,
        )
    else:
        x, z = _center_for_route_cell(centerline_path, cell)
    cached_y_ranges = getattr(centerline_path, "cached_y_ranges", None) or {}
    y_range = cached_y_ranges.get(cell)
    if use_footprint_center and y_range is None:
        # Voxel recovery is local to the current camera when the selected
        # atlas cell has no cached vertical envelope. Reusing a centerline
        # sample's Y here can jump the recovery point to an unrelated branch.
        y = float(fallback_y)
    else:
        y = _medial_y_for_route_cell(
            centerline_path,
            cell,
            fallback_y=fallback_y,
        )
    point = (float(x), float(y), float(z))
    # A whole-cave atlas has no reliable vertical envelope for cells that
    # were not part of the original centerline samples. Do not let refinement
    # move those recovery points to an arbitrary high-clearance voxel outside
    # the passage's known Y band; that creates a voxel/mesh disagreement.
    if voxel_volume is None or y_range is None:
        return point
    refined = voxel_volume.refine_point(
        point,
        footprint_cell=cell,
        footprint_cell_size=centerline_path.footprint_cell_size,
        y_range=y_range,
    )
    return point if refined is None else refined


def _mesh_recovery_view_alignment(
    current_point: Point,
    target_point: Point,
    *,
    current_yaw: float | None,
    current_pitch: float | None,
) -> float:
    if current_yaw is None:
        return 0.0
    dx = float(target_point[0]) - float(current_point[0])
    dy = float(target_point[1]) - float(current_point[1])
    dz = float(target_point[2]) - float(current_point[2])
    norm = math.sqrt(dx * dx + dy * dy + dz * dz)
    if norm <= 1e-9:
        return -1.0
    yaw = float(current_yaw)
    pitch = 0.0 if current_pitch is None else float(current_pitch)
    horizontal = math.cos(pitch)
    view_x = math.cos(yaw) * horizontal
    view_y = math.sin(pitch)
    view_z = math.sin(yaw) * horizontal
    return float((view_x * dx + view_y * dy + view_z * dz) / norm)


def _mesh_recovery_scan_alignment(
    current_point: Point,
    target_point: Point,
    *,
    current_yaw: float | None,
    current_pitch: float | None,
) -> tuple[bool, float, float]:
    if current_yaw is None:
        return True, 0.0, 0.0

    best_alignment = -1.0
    best_angle_penalty = math.inf
    base_pitch = 0.0 if current_pitch is None else float(current_pitch)
    min_pitch = math.radians(-55.0)
    max_pitch = math.radians(55.0)

    for yaw_offset_deg in _AUTO_DIVE_MESH_RECOVERY_SCAN_YAW_OFFSETS_DEG:
        yaw = float(current_yaw) + math.radians(float(yaw_offset_deg))
        for pitch_offset_deg in _AUTO_DIVE_MESH_RECOVERY_SCAN_PITCH_OFFSETS_DEG:
            pitch = max(
                min_pitch,
                min(
                    max_pitch,
                    base_pitch + math.radians(float(pitch_offset_deg)),
                ),
            )
            alignment = _mesh_recovery_view_alignment(
                current_point,
                target_point,
                current_yaw=yaw,
                current_pitch=pitch,
            )
            angle_penalty = abs(float(yaw_offset_deg)) + (
                abs(float(pitch_offset_deg)) * 0.5
            )
            if (alignment, -angle_penalty) > (
                best_alignment,
                -best_angle_penalty,
            ):
                best_alignment = alignment
                best_angle_penalty = angle_penalty

    return (
        best_alignment >= _AUTO_DIVE_MESH_RECOVERY_SCAN_CONE_ALIGNMENT,
        float(best_alignment),
        float(best_angle_penalty),
    )


def _build_hemisphere_probe_route_candidate(
    *,
    ordinal: int,
    centerline_path: CenterlinePath,
    current: np.ndarray,
    route_points: tuple[Point, ...],
    current_yaw: float | None,
    current_pitch: float | None,
    current_roll: float | None,
    current_travel_yaw: float | None,
    current_travel_pitch: float | None,
    collision_validator: _AutoDiveCollisionValidator,
    avoid_positions: Sequence[Sequence[float]] | None,
    settings: AutoDiveSettings,
    scan_distance_m: float | None = None,
    planning_budget: _AutoDivePlanningBudget | None = None,
    force_hemisphere_scan: bool = False,
    diagnostics: AutoDiveDiagnosticSink | None = None,
) -> _AutoDiveRouteCandidate | None:
    """Select one exact-mesh-checked route from a 3D hemisphere scan.

    The scan is intentionally local. The cache-time voxel graph remains the
    long-range branch selector; this routine finds a safe next leg when the
    graph/centerline route cannot be entered or when a turn requires a
    lateral/vertical camera reposition first.
    """
    current_point: Point = (
        float(current[0]),
        float(current[1]),
        float(current[2]),
    )
    forward = _hemisphere_scan_forward_vector(
        centerline_path,
        current=current,
        current_point=current_point,
        current_yaw=current_yaw,
        current_pitch=current_pitch,
        current_travel_yaw=current_travel_yaw,
        current_travel_pitch=current_travel_pitch,
        route_points=route_points,
    )
    if forward is None:
        _record_auto_dive_diagnostic(
            diagnostics,
            "hemisphere_scan",
            {
                "selected": None,
                "reason": "no_forward_direction",
                "direction_count": int(_AUTO_DIVE_HEMISPHERE_DIRECTION_COUNT),
                "roll_count": int(_AUTO_DIVE_HEMISPHERE_ROLL_COUNT),
            },
        )
        return None

    local_voxel_volume = collision_validator.voxel_refinement
    if local_voxel_volume is None:
        candidate_volume = collision_validator.voxel_volume
        if callable(getattr(candidate_volume, "find_forward_route", None)):
            local_voxel_volume = candidate_volume
    if local_voxel_volume is not None and not force_hemisphere_scan:
        if planning_budget is not None:
            planning_budget.check(
                "voxel_local_route_search",
                diagnostics=diagnostics,
            )
        local_deadline = (
            None
            if planning_budget is None
            else planning_budget.deadline_monotonic_s
        )
        if local_deadline is not None:
            # Leave time for exact mesh validation and route handoff after the
            # bounded voxel search returns a safe partial prefix.
            local_deadline -= min(
                0.75,
                max(0.10, float(planning_budget.budget_s or 0.0) * 0.15),
            )
        local_route = local_voxel_volume.find_forward_route(
            current_point,
            forward,
            max_distance_m=max(
                float(settings.voxel_local_refinement_forward_m),
                float(settings.lookahead_distance_m),
            ),
            max_nodes=int(settings.voxel_local_refinement_max_cells),
            min_target_distance_m=max(
                4.0,
                float(local_voxel_volume.voxel_size_m) * 4.0,
            ),
            deadline_monotonic_s=local_deadline,
        )
        _record_auto_dive_diagnostic(
            diagnostics,
            "voxel_local_route_search",
            {
                "selected": local_route is not None,
                "voxel_size_m": float(local_voxel_volume.voxel_size_m),
                "max_distance_m": float(
                    max(
                        float(settings.voxel_local_refinement_forward_m),
                        float(settings.lookahead_distance_m),
                    )
                ),
                "route": (
                    None
                    if local_route is None
                    else local_route.diagnostic_payload()
                ),
                "search_truncated": bool(
                    local_route is not None and local_route.search_truncated
                ),
            },
        )
        if local_route is not None and not force_hemisphere_scan:
            local_cells = tuple(
                _footprint_cell_for_xz(
                    (point[0], point[2]),
                    float(centerline_path.footprint_cell_size),
                )
                for point in local_route.points
            )
            if (
                planning_budget is not None
                and not local_route.search_truncated
            ):
                planning_budget.check(
                    "voxel_local_route_search_complete",
                    diagnostics=diagnostics,
                )
            return _AutoDiveRouteCandidate(
                ordinal=ordinal,
                name="voxel-local-frontier",
                cells=tuple(_dedupe_consecutive_cells(local_cells)),
                points=local_route.points,
            )

    cell_size = float(centerline_path.footprint_cell_size)
    probe_distance_m = max(
        cell_size * 3.0,
        min(
            max(
                cell_size * 3.0,
                float(
                    settings.lookahead_distance_m
                    if scan_distance_m is None
                    else scan_distance_m
                ),
            ),
            cell_size * _AUTO_DIVE_HEMISPHERE_PROBE_DISTANCE_CELLS,
        ),
    )
    voxel_volume = collision_validator.active_voxel_volume
    voxel_size_m = float(
        getattr(voxel_volume, "voxel_size_m", settings.voxel_size_m)
    )
    current_cell = _current_footprint_cell(centerline_path, current)
    start_progress = _hemisphere_progress_for_cell(
        voxel_volume,
        current_cell,
    )
    progress_tolerance_m = (
        cell_size * _AUTO_DIVE_HEMISPHERE_PROGRESS_TOLERANCE_CELLS
    )
    evaluations: list[_HemisphereProbeEvaluation] = []
    rejection_counts: dict[str, int] = {}
    generated_count = 0
    fine_supported_count = 0
    fine_blocked_count = 0
    fine_uncovered_count = 0

    probes = iter_hemisphere_probes(
        current_point,
        forward=forward,
        distance_m=probe_distance_m,
        cell_size_m=cell_size,
        voxel_size_m=voxel_size_m,
        current_roll_deg=(
            0.0
            if current_roll is None
            else math.degrees(float(current_roll))
        ),
        direction_count=_AUTO_DIVE_HEMISPHERE_DIRECTION_COUNT,
        roll_count=_AUTO_DIVE_HEMISPHERE_ROLL_COUNT,
    )
    for probe in probes:
        generated_count += 1
        if planning_budget is not None:
            planning_budget.check(
                "hemisphere_probe_coarse",
                diagnostics=diagnostics,
            )
        probe_points = _dedupe_consecutive_points(
            (probe.origin, probe.target),
            min_distance_m=1e-6,
        )
        if not probe_points:
            _increment_hemisphere_rejection(rejection_counts, "empty_probe")
            continue
        target_cell = _footprint_cell_for_xz(
            (probe.target[0], probe.target[2]),
            cell_size,
        )
        fine_target_result = collision_validator.probe_fine_point(
            probe.target,
            include_clearance=False,
        )
        if fine_target_result is None:
            fine_uncovered_count += 1
        elif fine_target_result[0]:
            fine_supported_count += 1
        else:
            fine_blocked_count += 1
        target_supported_by_fine_field = (
            fine_target_result is not None and fine_target_result[0]
        )
        if (
            target_cell not in collision_validator.component_cells
            and not target_supported_by_fine_field
        ):
            _increment_hemisphere_rejection(
                rejection_counts,
                "outside_footprint",
            )
            continue
        if not _hemisphere_vertical_probe_is_supported(
            current_point,
            probe,
            collision_validator=collision_validator,
            voxel_volume=voxel_volume,
            voxel_size_m=voxel_size_m,
        ):
            _increment_hemisphere_rejection(
                rejection_counts,
                "no_vertical_model",
            )
            continue
        target_vector = np.asarray(probe.target, dtype=np.float64) - current
        target_distance_m = float(np.linalg.norm(target_vector))
        if target_distance_m < max(0.5, cell_size * 0.5):
            _increment_hemisphere_rejection(rejection_counts, "too_close")
            continue
        target_alignment = float(
            np.dot(target_vector, np.asarray(forward, dtype=np.float64))
            / max(1e-9, target_distance_m)
        )
        origin_vector = np.asarray(probe.origin, dtype=np.float64) - current
        origin_forward_m = float(
            np.dot(origin_vector, np.asarray(forward, dtype=np.float64))
        )
        if origin_forward_m < -max(0.25, cell_size * 0.1):
            _increment_hemisphere_rejection(
                rejection_counts,
                "backward_origin_offset",
            )
            continue
        if target_alignment < _AUTO_DIVE_HEMISPHERE_MIN_TARGET_ALIGNMENT:
            _increment_hemisphere_rejection(
                rejection_counts,
                "backward_target",
            )
            continue
        if _mesh_recovery_target_is_avoided(
            probe.target,
            avoid_positions=avoid_positions,
            cell_size=cell_size,
        ):
            _increment_hemisphere_rejection(rejection_counts, "avoided_target")
            continue
        coarse_failure = _hemisphere_probe_coarse_failure(
            current_point,
            probe_points,
            collision_validator=collision_validator,
        )
        if coarse_failure is not None:
            _increment_hemisphere_rejection(rejection_counts, coarse_failure)
            continue

        voxel_metrics = _voxel_probe_path_metrics(
            (current_point, *probe_points),
            voxel_volume=voxel_volume,
            voxel_size_m=voxel_size_m,
        )
        if voxel_metrics is not None and (
            float(voxel_metrics["coverage_fraction"])
            < _AUTO_DIVE_HEMISPHERE_MIN_COVERAGE
            or float(voxel_metrics["free_fraction"]) < 0.55
        ):
            _increment_hemisphere_rejection(rejection_counts, "voxel_blocked")
            continue

        target_progress = _hemisphere_progress_for_cell(
            voxel_volume,
            target_cell,
        )
        progress_gain_m = (
            0.0
            if start_progress is None or target_progress is None
            else float(target_progress - start_progress)
        )
        if (
            start_progress is not None
            and target_progress is not None
            and progress_gain_m < -progress_tolerance_m
        ):
            _increment_hemisphere_rejection(rejection_counts, "entrance_progress")
            continue

        metric = _hemisphere_voxel_cell_metric(voxel_volume, target_cell)
        if voxel_volume is not None and isinstance(voxel_volume, NavigationVoxelAtlas):
            if (
                not target_supported_by_fine_field
                and (metric is None or int(metric.free_cell_count) <= 0)
            ):
                _increment_hemisphere_rejection(
                    rejection_counts,
                    "missing_filled_cell",
                )
                continue
        target_volume_m3 = (
            0.0 if metric is None else float(metric.available_volume_m3)
        )
        target_clearance_m = (
            0.0 if metric is None else float(metric.mean_clearance_m)
        )
        continuation_count = _hemisphere_continuation_count(
            voxel_volume,
            target_cell,
            progress_tolerance_m=progress_tolerance_m,
        )
        evaluations.append(
            _HemisphereProbeEvaluation(
                probe=probe,
                points=probe_points,
                target_cell=target_cell,
                target_alignment=target_alignment,
                progress_gain_m=progress_gain_m,
                target_volume_m3=target_volume_m3,
                target_clearance_m=target_clearance_m,
                continuation_count=continuation_count,
                voxel_coverage_fraction=(
                    1.0
                    if voxel_metrics is None
                    else float(voxel_metrics["coverage_fraction"])
                ),
                voxel_free_fraction=(
                    1.0
                    if voxel_metrics is None
                    else float(voxel_metrics["free_fraction"])
                ),
                voxel_mean_clearance_m=(
                    0.0
                    if voxel_metrics is None
                    else float(voxel_metrics["mean_clearance_m"])
                ),
            )
        )

    ordered_evaluations = sorted(
        evaluations,
        key=_hemisphere_probe_evaluation_sort_key,
        reverse=True,
    )
    exact_results: list[
        tuple[_AutoDiveRouteCandidateScore, _AutoDiveRouteCandidate, _HemisphereProbeEvaluation]
    ] = []
    for evaluation in ordered_evaluations[:_AUTO_DIVE_HEMISPHERE_MAX_EXACT_CANDIDATES]:
        if planning_budget is not None:
            planning_budget.check(
                "hemisphere_probe_exact",
                diagnostics=diagnostics,
            )
        candidate = _AutoDiveRouteCandidate(
            ordinal=ordinal,
            name="hemisphere-probe",
            cells=(current_cell, evaluation.target_cell),
            points=evaluation.points,
            roll_deg=float(evaluation.probe.roll_deg),
        )
        score = _score_auto_dive_route_candidate(
            candidate,
            current_point=current_point,
            collision_validator=collision_validator,
            allow_low_lateral_clearance=True,
            planning_budget=planning_budget,
            diagnostics=diagnostics,
        )
        exact_results.append((score, candidate, evaluation))

    selected_result = None
    if exact_results:
        selected_result = max(
            exact_results,
            key=lambda item: _hemisphere_exact_selection_key(
                item,
                current_roll=current_roll,
            ),
        )
    _record_auto_dive_diagnostic(
        diagnostics,
        "hemisphere_scan",
        {
            "selected": (
                None
                if selected_result is None
                else [
                    int(selected_result[2].target_cell[0]),
                    int(selected_result[2].target_cell[1]),
                ]
            ),
            "forward": [float(value) for value in forward],
            "probe_distance_m": float(probe_distance_m),
            "direction_count": int(_AUTO_DIVE_HEMISPHERE_DIRECTION_COUNT),
            "roll_count": int(_AUTO_DIVE_HEMISPHERE_ROLL_COUNT),
            "generated_count": int(generated_count),
            "coarse_candidate_count": len(evaluations),
            "exact_candidate_count": len(exact_results),
            "rejection_counts": rejection_counts,
            "fine_supported_count": int(fine_supported_count),
            "fine_blocked_count": int(fine_blocked_count),
            "fine_uncovered_count": int(fine_uncovered_count),
            "forced_full_scan": bool(force_hemisphere_scan),
            "start_progress_m": (
                None if start_progress is None else float(start_progress)
            ),
            "top_candidates": [
                _hemisphere_probe_evaluation_payload(evaluation)
                for evaluation in ordered_evaluations[
                    :_AUTO_DIVE_HEMISPHERE_MAX_DIAGNOSTIC_CANDIDATES
                ]
            ],
            "selected_score": (
                None
                if selected_result is None
                else _auto_dive_candidate_score_payload(
                    selected_result[0],
                    selected_result[1],
                )
            ),
        },
    )
    return None if selected_result is None else selected_result[1]


def _hemisphere_scan_forward_vector(
    centerline_path: CenterlinePath,
    *,
    current: np.ndarray,
    current_point: Point,
    current_yaw: float | None,
    current_pitch: float | None,
    current_travel_yaw: float | None,
    current_travel_pitch: float | None,
    route_points: tuple[Point, ...],
) -> tuple[float, float, float] | None:
    for yaw, pitch in (
        (current_travel_yaw, current_travel_pitch),
        (current_yaw, current_pitch),
    ):
        if yaw is None:
            continue
        direction = _direction_from_radians(
            float(yaw),
            0.0 if pitch is None else float(pitch),
        )
        if direction is not None and float(np.linalg.norm(direction)) > 1e-9:
            return tuple(float(value) for value in direction)  # type: ignore[return-value]

    position_direction = _auto_dive_current_position_offset_direction(
        centerline_path,
        current=current,
        current_point=current_point,
    )
    if position_direction is not None and float(np.linalg.norm(position_direction)) > 1e-9:
        return tuple(float(value) for value in position_direction)  # type: ignore[return-value]

    current_array = np.asarray(current_point, dtype=np.float64)
    for point in route_points:
        delta = np.asarray(point, dtype=np.float64) - current_array
        norm = float(np.linalg.norm(delta))
        if norm >= max(0.5, centerline_path.footprint_cell_size * 0.5):
            return tuple(float(value) for value in delta / norm)  # type: ignore[return-value]
    return None


def _hemisphere_probe_coarse_failure(
    current_point: Point,
    probe_points: tuple[Point, ...],
    *,
    collision_validator: _AutoDiveCollisionValidator,
) -> str | None:
    points = (current_point, *probe_points)
    for index, (first, second) in enumerate(zip(points, points[1:], strict=False)):
        if index == 0 and not collision_validator.point_is_clear(first):
            if not _route_segment_is_clear_after_start(
                first,
                second,
                collision_validator=collision_validator,
                allow_low_lateral_clearance=True,
            ):
                return "coarse_start_segment"
            continue
        failure = collision_validator.segment_clearance_failure(
            first,
            second,
            allow_low_lateral_clearance=True,
        )
        if failure is not None:
            return str(failure.reason)
    return None


def _hemisphere_vertical_probe_is_supported(
    current_point: Point,
    probe: HemisphereProbe,
    *,
    collision_validator: _AutoDiveCollisionValidator,
    voxel_volume: LocalVoxelVolume | NavigationVoxelAtlas | None,
    voxel_size_m: float,
) -> bool:
    """Keep legacy mesh-only recovery from probing outside known height."""
    if voxel_volume is not None or collision_validator.cached_y_ranges:
        return True
    tolerance = max(
        0.25,
        min(float(collision_validator.cell_size) * 0.25, float(voxel_size_m)),
    )
    return all(
        abs(float(point[1]) - float(current_point[1])) <= tolerance
        for point in (probe.origin, probe.target)
    )


def _voxel_probe_path_metrics(
    points: tuple[Point, ...],
    *,
    voxel_volume: LocalVoxelVolume | NavigationVoxelAtlas | None,
    voxel_size_m: float,
) -> dict[str, float | int] | None:
    if voxel_volume is None:
        return None
    probe_point = getattr(voxel_volume, "probe_point", None)
    if not callable(probe_point):
        return None
    sample_step = max(0.5, float(voxel_size_m) * 0.5)
    samples: list[Point] = [points[0]] if points else []
    for first, second in zip(points, points[1:], strict=False):
        distance = float(
            np.linalg.norm(
                np.asarray(second, dtype=np.float64)
                - np.asarray(first, dtype=np.float64)
            )
        )
        steps = max(1, int(math.ceil(distance / sample_step)))
        for step in range(1, steps + 1):
            fraction = step / steps
            samples.append(
                tuple(
                    float(first[axis] + (second[axis] - first[axis]) * fraction)
                    for axis in range(3)
                )
            )
    covered_count = 0
    free_count = 0
    blocked_count = 0
    clearances: list[float] = []
    for point in samples:
        result = probe_point(point, include_clearance=False)
        if result is None:
            continue
        covered_count += 1
        is_free, _clearance_m = result
        if is_free:
            free_count += 1
        else:
            blocked_count += 1
    if samples:
        # The hemisphere pass is a cheap occupancy filter. Clearance is
        # recomputed only for the small exact-candidate set; asking the local
        # distance field for every probe endpoint made a 1 m cache behave like
        # an unbounded runtime analysis.
        endpoint_result = probe_point(samples[-1], include_clearance=False)
        if endpoint_result is not None and endpoint_result[0]:
            clearances.append(float(endpoint_result[1]))
    if covered_count <= 0 or not samples:
        return None
    return {
        "sample_count": int(len(samples)),
        "covered_count": int(covered_count),
        "free_count": int(free_count),
        "blocked_count": int(blocked_count),
        "coverage_fraction": float(covered_count / len(samples)),
        "free_fraction": float(free_count / covered_count),
        "blocked_fraction": float(blocked_count / covered_count),
        "mean_clearance_m": float(
            sum(clearances) / max(1, len(clearances))
        ),
    }


def _hemisphere_progress_for_cell(
    voxel_volume: LocalVoxelVolume | NavigationVoxelAtlas | None,
    cell: FootprintCell,
) -> float | None:
    metric = _hemisphere_voxel_cell_metric(voxel_volume, cell)
    return None if metric is None else float(metric.progress_m)


def _hemisphere_voxel_cell_metric(
    voxel_volume: LocalVoxelVolume | NavigationVoxelAtlas | None,
    cell: FootprintCell,
) -> Any | None:
    if not isinstance(voxel_volume, NavigationVoxelAtlas):
        return None
    return voxel_volume.cell_metrics.get(cell)


def _hemisphere_continuation_count(
    voxel_volume: LocalVoxelVolume | NavigationVoxelAtlas | None,
    cell: FootprintCell,
    *,
    progress_tolerance_m: float,
) -> int:
    if not isinstance(voxel_volume, NavigationVoxelAtlas):
        return 0
    metric = voxel_volume.cell_metrics.get(cell)
    if metric is None:
        return 0
    return sum(
        1
        for neighbor in navigable_footprint_neighbors(
            cell,
            frozenset(voxel_volume.cell_metrics),
        )
        if neighbor in voxel_volume.cell_metrics
        and float(voxel_volume.cell_metrics[neighbor].progress_m)
        >= float(metric.progress_m) - float(progress_tolerance_m)
    )


def _hemisphere_probe_evaluation_sort_key(
    evaluation: _HemisphereProbeEvaluation,
) -> tuple[object, ...]:
    return (
        float(evaluation.progress_gain_m) >= 0.0,
        int(evaluation.continuation_count) > 0,
        float(evaluation.voxel_free_fraction),
        float(evaluation.voxel_coverage_fraction),
        float(evaluation.target_clearance_m),
        float(evaluation.voxel_mean_clearance_m),
        float(evaluation.target_volume_m3),
        float(evaluation.progress_gain_m),
        float(evaluation.target_alignment),
        -float(np.linalg.norm(evaluation.probe.origin_offset)),
        -int(evaluation.probe.direction_index),
        -int(evaluation.probe.roll_index),
        -int(evaluation.probe.offset_index),
    )


def _hemisphere_exact_selection_key(
    item: tuple[
        _AutoDiveRouteCandidateScore,
        _AutoDiveRouteCandidate,
        _HemisphereProbeEvaluation,
    ],
    *,
    current_roll: float | None,
) -> tuple[object, ...]:
    score, candidate, evaluation = item
    roll_delta = abs(
        (float(candidate.roll_deg) - math.degrees(float(current_roll or 0.0)) + 180.0)
        % 360.0
        - 180.0
    )
    return (
        bool(score.route_clear),
        bool(score.mesh_clear),
        bool(score.entry_clear),
        bool(evaluation.continuation_count > 0),
        float(evaluation.voxel_free_fraction),
        float(evaluation.target_clearance_m),
        float(evaluation.target_volume_m3),
        float(evaluation.progress_gain_m),
        float(score.forward_progress_m),
        -float(score.total_change_per_m),
        -float(roll_delta),
    )


def _hemisphere_probe_evaluation_payload(
    evaluation: _HemisphereProbeEvaluation,
) -> dict[str, Any]:
    probe = evaluation.probe
    return {
        "probe_index": int(probe.index),
        "direction_index": int(probe.direction_index),
        "roll_index": int(probe.roll_index),
        "offset_index": int(probe.offset_index),
        "origin": [float(value) for value in probe.origin],
        "target": [float(value) for value in probe.target],
        "origin_offset": [float(value) for value in probe.origin_offset],
        "offset_label": str(probe.offset_label),
        "direction": [float(value) for value in probe.direction],
        "forward_alignment": float(probe.forward_alignment),
        "roll_deg": float(probe.roll_deg),
        "target_cell": [
            int(evaluation.target_cell[0]),
            int(evaluation.target_cell[1]),
        ],
        "target_alignment": float(evaluation.target_alignment),
        "progress_gain_m": float(evaluation.progress_gain_m),
        "target_volume_m3": float(evaluation.target_volume_m3),
        "target_clearance_m": float(evaluation.target_clearance_m),
        "continuation_count": int(evaluation.continuation_count),
        "voxel_coverage_fraction": float(evaluation.voxel_coverage_fraction),
        "voxel_free_fraction": float(evaluation.voxel_free_fraction),
        "voxel_mean_clearance_m": float(evaluation.voxel_mean_clearance_m),
    }


def _increment_hemisphere_rejection(
    counts: dict[str, int],
    reason: str,
) -> None:
    counts[str(reason)] = int(counts.get(str(reason), 0)) + 1


def _mesh_recovery_target_is_avoided(
    target_point: Point,
    *,
    avoid_positions: Sequence[Sequence[float]] | None,
    cell_size: float,
) -> bool:
    if not avoid_positions:
        return False
    target = np.asarray(target_point, dtype=np.float64)
    threshold_m = max(1.0, float(cell_size) * 0.75)
    for position in avoid_positions:
        try:
            avoided = np.asarray(position, dtype=np.float64).reshape(3)
        except Exception:
            continue
        if float(np.linalg.norm(target - avoided)) <= threshold_m:
            return True
    return False


def _auto_dive_candidate_specs(
    settings: AutoDiveSettings,
) -> tuple[_AutoDiveCandidateSpec, ...]:
    radius = max(0, int(settings.smoothing_radius_cells))
    specs = [
        _AutoDiveCandidateSpec(
            name="raw",
            smoothing_radius_cells=0,
            use_theta=False,
            use_cone=False,
            use_weighted_smoothing=False,
            use_bspline=False,
        )
    ]
    if radius <= 0:
        return tuple(specs)

    radii = tuple(
        sorted(
            {
                radius,
            }
        )
    )
    for candidate_radius in radii:
        specs.extend(
            (
                _AutoDiveCandidateSpec(
                    name=f"theta-{candidate_radius}",
                    smoothing_radius_cells=candidate_radius,
                    use_theta=True,
                    use_cone=False,
                    use_weighted_smoothing=False,
                    use_bspline=False,
                ),
                _AutoDiveCandidateSpec(
                    name=f"weighted-{candidate_radius}",
                    smoothing_radius_cells=candidate_radius,
                    use_theta=False,
                    use_cone=False,
                    use_weighted_smoothing=True,
                    use_bspline=False,
                ),
                _AutoDiveCandidateSpec(
                    name=f"theta-weighted-{candidate_radius}",
                    smoothing_radius_cells=candidate_radius,
                    use_theta=True,
                    use_cone=False,
                    use_weighted_smoothing=True,
                    use_bspline=False,
                ),
                _AutoDiveCandidateSpec(
                    name=f"theta-repel-{candidate_radius}",
                    smoothing_radius_cells=candidate_radius,
                    use_theta=True,
                    use_cone=False,
                    use_weighted_smoothing=False,
                    use_bspline=False,
                    use_repulsion=True,
                ),
            )
        )
        specs.extend(
            (
                _AutoDiveCandidateSpec(
                    name=f"cone-theta-{candidate_radius}",
                    smoothing_radius_cells=candidate_radius,
                    use_theta=True,
                    use_cone=True,
                    use_weighted_smoothing=False,
                    use_bspline=False,
                ),
                _AutoDiveCandidateSpec(
                    name=f"cone-theta-weighted-repel-{candidate_radius}",
                    smoothing_radius_cells=candidate_radius,
                    use_theta=True,
                    use_cone=True,
                    use_weighted_smoothing=True,
                    use_bspline=False,
                    use_repulsion=True,
                ),
            )
        )
    return tuple(specs)


def _build_auto_dive_route_candidate(
    spec: _AutoDiveCandidateSpec,
    *,
    ordinal: int,
    centerline_path: CenterlinePath,
    route_samples: _AutoDiveRouteSamples,
    current: np.ndarray,
    settings: AutoDiveSettings,
    collision_validator: _AutoDiveCollisionValidator,
) -> _AutoDiveRouteCandidate:
    candidate_settings = replace(
        settings,
        smoothing_radius_cells=max(0, int(spec.smoothing_radius_cells)),
    )
    samples = route_samples
    if spec.use_cone:
        samples = _cone_relaxed_auto_dive_samples(
            centerline_path,
            samples,
            current=current,
            settings=candidate_settings,
            collision_validator=collision_validator,
        )

    if spec.use_theta:
        samples = _theta_relaxed_auto_dive_samples(
            samples,
            settings=candidate_settings,
            collision_validator=collision_validator,
        )

    points = samples.points
    if spec.use_weighted_smoothing:
        points = _smooth_cached_auto_dive_xz_values(
            centerline_path,
            waypoint_cells=samples.cells,
            route_points=points,
            settings=candidate_settings,
            collision_validator=collision_validator,
        )
        points = _smooth_cached_auto_dive_y_values(
            centerline_path,
            waypoint_cells=samples.cells,
            route_points=points,
            settings=candidate_settings,
        )

    if spec.use_repulsion:
        points = _repelled_auto_dive_points(
            centerline_path,
            waypoint_cells=samples.cells,
            route_points=points,
            settings=candidate_settings,
            collision_validator=collision_validator,
        )

    if spec.use_bspline:
        points = _bspline_smoothed_auto_dive_points(
            points,
            settings=candidate_settings,
            collision_validator=collision_validator,
        )

    return _AutoDiveRouteCandidate(
        ordinal=ordinal,
        name=spec.name,
        cells=samples.cells,
        points=_dedupe_consecutive_points(points),
    )


def _score_auto_dive_route_candidate(
    candidate: _AutoDiveRouteCandidate,
    *,
    current_point: Point,
    collision_validator: _AutoDiveCollisionValidator,
    allow_low_lateral_clearance: bool = False,
    planning_budget: _AutoDivePlanningBudget | None = None,
    diagnostics: AutoDiveDiagnosticSink | None = None,
) -> _AutoDiveRouteCandidateScore:
    if planning_budget is not None:
        planning_budget.check("candidate_score_entry", diagnostics=diagnostics)
    entry_clear = _entry_segment_is_clear(
        current_point,
        candidate.points[0],
        collision_validator=collision_validator,
    )
    route_with_current = _route_points_starting_at_current_camera(
        candidate.points,
        np.asarray(current_point, dtype=np.float64),
    )
    first_clearance_failure = collision_validator.route_clearance_failure(
        route_with_current,
        allow_low_lateral_clearance=allow_low_lateral_clearance,
    )
    if planning_budget is not None:
        planning_budget.check("candidate_score_mesh", diagnostics=diagnostics)
    first_mesh_failure = collision_validator.mesh_route_clearance_failure(
        route_with_current,
        planning_budget=planning_budget,
        diagnostics=diagnostics,
    )
    if planning_budget is not None:
        planning_budget.check("candidate_score_metrics", diagnostics=diagnostics)
    mesh_clear = first_mesh_failure is None
    route_clear = first_clearance_failure is None and mesh_clear
    reported_clearance_failure = first_mesh_failure or first_clearance_failure
    lateral_scores = _sampled_lateral_clearance_scores(
        route_with_current,
        collision_validator=collision_validator,
    )
    clearance_margins = _sampled_clearance_margins_m(
        route_with_current,
        collision_validator=collision_validator,
    )
    length_m = path_length(route_with_current)
    max_segment_length_m = _route_max_segment_length_m(route_with_current)
    max_segment_cells = _route_max_segment_xz_cells(
        route_with_current,
        cell_size=collision_validator.cell_size,
    )
    curvature_rad = _route_curvature_rad(route_with_current)
    vertical_jerk_m = _route_vertical_jerk_m(route_with_current)
    curvature_rad_per_m = curvature_rad / max(1e-9, length_m)
    vertical_jerk_m_per_m = vertical_jerk_m / max(1e-9, length_m)
    return _AutoDiveRouteCandidateScore(
        route_clear=route_clear,
        entry_clear=entry_clear,
        mesh_clear=mesh_clear,
        geometry_trusted=_route_geometry_is_trusted(max_segment_cells),
        min_lateral_clearance_cells=min(lateral_scores) if lateral_scores else 0,
        mean_lateral_clearance_cells=(
            sum(lateral_scores) / len(lateral_scores)
            if lateral_scores
            else 0.0
        ),
        min_clearance_margin_m=(
            min(clearance_margins)
            if clearance_margins
            else 0.0
        ),
        max_segment_length_m=max_segment_length_m,
        max_segment_cells=max_segment_cells,
        forward_progress_m=_candidate_forward_progress_m(route_with_current),
        pullback_penalty_m=_candidate_pullback_penalty_m(route_with_current),
        curvature_rad=curvature_rad,
        vertical_jerk_m=vertical_jerk_m,
        curvature_rad_per_m=curvature_rad_per_m,
        vertical_jerk_m_per_m=vertical_jerk_m_per_m,
        total_change_per_m=curvature_rad_per_m + vertical_jerk_m_per_m,
        length_m=length_m,
        point_count=len(route_with_current),
        first_clearance_failure=reported_clearance_failure,
    )


def _entry_segment_is_clear(
    current_point: Point,
    target_point: Point,
    *,
    collision_validator: _AutoDiveCollisionValidator,
) -> bool:
    if collision_validator.point_is_clear(current_point):
        return collision_validator.segment_is_clear(current_point, target_point)
    return _route_segment_is_clear_after_start(
        current_point,
        target_point,
        collision_validator=collision_validator,
    )


def _cone_relaxed_auto_dive_samples(
    centerline_path: CenterlinePath,
    route_samples: _AutoDiveRouteSamples,
    *,
    current: np.ndarray,
    settings: AutoDiveSettings,
    collision_validator: _AutoDiveCollisionValidator,
) -> _AutoDiveRouteSamples:
    """Return a local forward-cone route through the clearest nearby passage.

    The cached centerline remains the long-range intent. This routine only
    rewrites the local prefix: it searches a virtual cone ahead of the current
    camera, tries several clear target cells near future route anchors, and
    chooses the path with the best clearance and least change per meter.
    """
    if len(route_samples.cells) < 2 or len(route_samples.points) < 2:
        return route_samples
    component = collision_validator.component_cells
    if not component:
        return route_samples
    radius = max(1, int(settings.smoothing_radius_cells))
    cell_size = float(centerline_path.footprint_cell_size)
    current_cell = _cone_start_cell(
        centerline_path,
        route_samples,
        current=current,
    )
    if current_cell not in component:
        current_cell = min(
            component,
            key=lambda cell: (
                _cell_center_distance_squared(centerline_path, cell, current),
                cell,
            ),
        )

    current_point: Point = (
        float(current[0]),
        float(current[1]),
        float(current[2]),
    )
    start_cells = (
        (route_samples.cells[0],)
        if _cone_should_keep_start_sample(
            centerline_path,
            route_samples,
            current=current,
        )
        else ()
    )
    candidates = _chained_cone_candidate_samples(
        centerline_path,
        route_samples,
        current_cell=current_cell,
        current_point=current_point,
        start_cells=start_cells,
        radius_cells=radius,
        cell_size=cell_size,
        collision_validator=collision_validator,
    )

    if not candidates:
        return route_samples
    _score, samples = max(
        candidates,
        key=lambda item: item[0].sort_key,
    )
    return samples


def _chained_cone_candidate_samples(
    centerline_path: CenterlinePath,
    route_samples: _AutoDiveRouteSamples,
    *,
    current_cell: FootprintCell,
    current_point: Point,
    start_cells: tuple[FootprintCell, ...],
    radius_cells: int,
    cell_size: float,
    collision_validator: _AutoDiveCollisionValidator,
) -> list[tuple[_AutoDiveRouteCandidateScore, _AutoDiveRouteSamples]]:
    """Build full route candidates from a beam of Dijkstra-planned cones."""
    anchor_indices = _cone_chain_anchor_indices(
        route_samples,
        radius_cells=radius_cells,
    )
    if not anchor_indices:
        return []

    beam_width = _cone_chain_beam_width(radius_cells)
    chains = [
        _ConeChainCandidate(
            cells=(current_cell,),
            anchor_index=0,
            cost=0.0,
        )
    ]
    for anchor_index in anchor_indices:
        expanded: list[tuple[tuple[object, ...], _ConeChainCandidate]] = []
        for chain in chains:
            expanded.extend(
                _expand_cone_chain_candidate(
                    centerline_path,
                    route_samples,
                    chain,
                    anchor_index=anchor_index,
                    current_point=current_point,
                    radius_cells=radius_cells,
                    cell_size=cell_size,
                    collision_validator=collision_validator,
                )
            )
        if not expanded:
            break
        chains = [
            candidate
            for _key, candidate in sorted(
                expanded,
                key=lambda item: item[0],
                reverse=True,
            )[:beam_width]
        ]

    candidates: list[tuple[_AutoDiveRouteCandidateScore, _AutoDiveRouteSamples]] = []
    for chain in chains:
        if chain.anchor_index <= 0 or len(chain.cells) < 2:
            continue
        candidate_cells = _dedupe_consecutive_cells(
            (
                *start_cells,
                *chain.cells,
                *route_samples.cells[chain.anchor_index + 1 :],
            )
        )
        if len(candidate_cells) < 2:
            continue
        candidate_points = _points_for_cone_cells(
            centerline_path,
            candidate_cells,
            route_samples=route_samples,
            current=current_point,
        )
        candidate_points = _dedupe_consecutive_points(candidate_points)
        if len(candidate_points) < 2:
            continue
        candidate = _AutoDiveRouteCandidate(
            ordinal=0,
            name="cone-chain",
            cells=candidate_cells,
            points=candidate_points,
        )
        score = _score_auto_dive_route_candidate(
            candidate,
            current_point=current_point,
            collision_validator=collision_validator,
        )
        candidates.append(
            (
                score,
                _AutoDiveRouteSamples(
                    cells=candidate_cells,
                    points=candidate_points,
                ),
            )
        )
    return candidates


def _cone_chain_anchor_indices(
    route_samples: _AutoDiveRouteSamples,
    *,
    radius_cells: int,
) -> tuple[int, ...]:
    last_index = len(route_samples.cells) - 1
    if last_index < 1:
        return ()
    radius = max(1, int(radius_cells))
    depth = max(2, min(3, 1 + radius // 4))
    spacing = max(2, min(radius, 8))
    return tuple(
        sorted(
            {
                min(last_index, max(1, spacing * step))
                for step in range(1, depth + 1)
            }
        )
    )


def _cone_chain_beam_width(radius_cells: int) -> int:
    return max(1, min(2, 1 + int(radius_cells) // 8))


def _expand_cone_chain_candidate(
    centerline_path: CenterlinePath,
    route_samples: _AutoDiveRouteSamples,
    chain: _ConeChainCandidate,
    *,
    anchor_index: int,
    current_point: Point,
    radius_cells: int,
    cell_size: float,
    collision_validator: _AutoDiveCollisionValidator,
) -> list[tuple[tuple[object, ...], _ConeChainCandidate]]:
    start_cell = chain.cells[-1]
    start_point = _point_for_cone_cell(
        centerline_path,
        start_cell,
        route_samples=route_samples,
        current=current_point,
    )
    anchor_cell = route_samples.cells[anchor_index]
    anchor_point = route_samples.points[anchor_index]
    forward = _cone_forward_unit(start_point, anchor_point)
    if forward is None:
        return []
    anchor_projection = _cone_projection_m(
        centerline_path,
        start_point,
        anchor_cell,
        forward,
    )
    cone_length_m = max(
        cell_size * 2.0,
        anchor_projection + cell_size * max(2.0, radius_cells * 0.35),
    )
    allowed_cells = _cone_allowed_cells(
        centerline_path,
        current=start_point,
        forward=forward,
        length_m=cone_length_m,
        radius_cells=radius_cells,
    )
    if not allowed_cells:
        return []
    allowed_cells.add(start_cell)
    allowed_cells.add(anchor_cell)
    frozen_allowed = frozenset(allowed_cells)
    expanded: list[tuple[tuple[object, ...], _ConeChainCandidate]] = []
    for target_cell in _cone_target_cells(
        centerline_path,
        anchor_cell=anchor_cell,
        allowed_cells=frozen_allowed,
        current=start_point,
        forward=forward,
        anchor_projection_m=anchor_projection,
        radius_cells=radius_cells,
    )[:4]:
        prefix_cells, prefix_cost = _lowest_change_cone_path_with_cost(
            centerline_path,
            start=start_cell,
            target=target_cell,
            allowed_cells=frozen_allowed,
            forward=forward,
        )
        if len(prefix_cells) < 2:
            continue
        connector_cells: tuple[FootprintCell, ...] = ()
        connector_cost = 0.0
        if target_cell != anchor_cell:
            connector_cells = lowest_cost_footprint_path(
                collision_validator.component_cells,
                target_cell,
                anchor_cell,
                centerline_path.clearance_scores,
            )
            if len(connector_cells) < 2:
                continue
            connector_cost = _footprint_path_change_cost(
                centerline_path,
                connector_cells,
            )
        cells = _dedupe_consecutive_cells(
            (
                *chain.cells,
                *prefix_cells[1:],
                *connector_cells[1:],
            )
        )
        if len(cells) < 2:
            continue
        cost = chain.cost + prefix_cost + connector_cost
        points = _points_for_cone_cells(
            centerline_path,
            cells,
            route_samples=route_samples,
            current=current_point,
        )
        candidate = _AutoDiveRouteCandidate(
            ordinal=0,
            name="cone-chain-prefix",
            cells=cells,
            points=_dedupe_consecutive_points(points),
        )
        score = _score_auto_dive_route_candidate(
            candidate,
            current_point=current_point,
            collision_validator=collision_validator,
        )
        expanded.append(
            (
                _cone_chain_rank_key(score, cost),
                _ConeChainCandidate(
                    cells=cells,
                    anchor_index=anchor_index,
                    cost=cost,
                ),
            )
        )
    return expanded


def _cone_chain_rank_key(
    score: _AutoDiveRouteCandidateScore,
    cost: float,
) -> tuple[object, ...]:
    return (*score.sort_key, -float(cost))


def _cone_should_keep_start_sample(
    centerline_path: CenterlinePath,
    route_samples: _AutoDiveRouteSamples,
    *,
    current: np.ndarray,
) -> bool:
    if not route_samples.cells or not route_samples.points:
        return False
    first_point = np.asarray(route_samples.points[0], dtype=np.float64)
    distance = float(np.linalg.norm(first_point - np.asarray(current, dtype=np.float64)))
    return distance <= float(centerline_path.footprint_cell_size) * 0.05


def _cone_start_cell(
    centerline_path: CenterlinePath,
    route_samples: _AutoDiveRouteSamples,
    *,
    current: np.ndarray,
) -> FootprintCell:
    current_cell = _current_footprint_cell(centerline_path, current)
    if not route_samples.cells or not route_samples.points:
        return current_cell
    sample_limit = min(4, len(route_samples.cells), len(route_samples.points))
    nearest_index = min(
        range(sample_limit),
        key=lambda index: (
            float(
                np.sum(
                    (
                        np.asarray(route_samples.points[index], dtype=np.float64)
                        - np.asarray(current, dtype=np.float64)
                    )
                    ** 2
                )
            ),
            index,
        ),
    )
    nearest_point = np.asarray(route_samples.points[nearest_index], dtype=np.float64)
    distance = float(np.linalg.norm(nearest_point - np.asarray(current, dtype=np.float64)))
    if distance <= float(centerline_path.footprint_cell_size) * 0.5:
        return route_samples.cells[nearest_index]
    return current_cell


def _cone_forward_unit(
    current: Point,
    target: Point,
) -> np.ndarray | None:
    vector = np.asarray(
        (float(target[0]) - current[0], float(target[2]) - current[2]),
        dtype=np.float64,
    )
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-9:
        return None
    return vector / norm


def _cone_projection_m(
    centerline_path: CenterlinePath,
    current: Point,
    cell: FootprintCell,
    forward: np.ndarray,
) -> float:
    center = _center_for_route_cell(centerline_path, cell)
    vector = np.asarray(
        (center[0] - current[0], center[1] - current[2]),
        dtype=np.float64,
    )
    return float(np.dot(vector, forward))


def _cone_lateral_m(
    centerline_path: CenterlinePath,
    current: Point,
    cell: FootprintCell,
    forward: np.ndarray,
) -> float:
    center = _center_for_route_cell(centerline_path, cell)
    vector = np.asarray(
        (center[0] - current[0], center[1] - current[2]),
        dtype=np.float64,
    )
    projection = float(np.dot(vector, forward))
    lateral = vector - forward * projection
    return float(np.linalg.norm(lateral))


def _cone_allowed_cells(
    centerline_path: CenterlinePath,
    *,
    current: Point,
    forward: np.ndarray,
    length_m: float,
    radius_cells: int,
) -> set[FootprintCell]:
    cell_size = float(centerline_path.footprint_cell_size)
    radius = max(1, int(radius_cells))
    min_width_m = cell_size * max(2.0, min(6.0, radius * 0.5))
    max_width_m = cell_size * max(3.0, float(radius))
    allowed: set[FootprintCell] = set()
    for cell in centerline_path.component_cells:
        projection = _cone_projection_m(centerline_path, current, cell, forward)
        if projection < -cell_size or projection > length_m:
            continue
        lateral = _cone_lateral_m(centerline_path, current, cell, forward)
        cone_width = min(max_width_m, min_width_m + max(0.0, projection) * 0.75)
        if lateral <= cone_width + cell_size * 0.5:
            allowed.add(cell)
    return allowed


def _cone_target_cells(
    centerline_path: CenterlinePath,
    *,
    anchor_cell: FootprintCell,
    allowed_cells: frozenset[FootprintCell],
    current: Point,
    forward: np.ndarray,
    anchor_projection_m: float,
    radius_cells: int,
) -> tuple[FootprintCell, ...]:
    cell_size = float(centerline_path.footprint_cell_size)
    radius = max(1, int(radius_cells))
    projection_window_m = cell_size * max(1.5, radius * 0.35)
    local_radius = max(1, radius // 2)
    candidates = [
        cell
        for cell in allowed_cells
        if footprint_cell_distance(cell, anchor_cell) <= local_radius
        and abs(
            _cone_projection_m(centerline_path, current, cell, forward)
            - anchor_projection_m
        )
        <= projection_window_m
    ]
    if anchor_cell in allowed_cells:
        candidates.append(anchor_cell)
    deduped = tuple(sorted(set(candidates)))
    ranked = sorted(
        deduped,
        key=lambda cell: (
            centerline_path.clearance_scores.get(cell, 0),
            -_cone_lateral_m(centerline_path, current, cell, forward),
            -footprint_cell_distance(cell, anchor_cell),
            cell,
        ),
        reverse=True,
    )
    return tuple(ranked[:8])


def _lowest_change_cone_path(
    centerline_path: CenterlinePath,
    *,
    start: FootprintCell,
    target: FootprintCell,
    allowed_cells: frozenset[FootprintCell],
    forward: np.ndarray,
) -> tuple[FootprintCell, ...]:
    path, _cost = _lowest_change_cone_path_with_cost(
        centerline_path,
        start=start,
        target=target,
        allowed_cells=allowed_cells,
        forward=forward,
    )
    return path


def _lowest_change_cone_path_with_cost(
    centerline_path: CenterlinePath,
    *,
    start: FootprintCell,
    target: FootprintCell,
    allowed_cells: frozenset[FootprintCell],
    forward: np.ndarray,
) -> tuple[tuple[FootprintCell, ...], float]:
    if start not in allowed_cells or target not in allowed_cells:
        return (), math.inf
    if start == target:
        return (start,), 0.0
    max_clearance = max(
        1,
        max(
            centerline_path.clearance_scores.get(cell, 1)
            for cell in allowed_cells
        ),
    )
    initial_step = _initial_cone_step(forward)
    start_state = (start, initial_step)
    frontier: list[tuple[float, int, tuple[FootprintCell, tuple[int, int]]]] = [
        (0.0, 0, start_state)
    ]
    costs: dict[tuple[FootprintCell, tuple[int, int]], float] = {
        start_state: 0.0,
    }
    previous: dict[
        tuple[FootprintCell, tuple[int, int]],
        tuple[FootprintCell, tuple[int, int]] | None,
    ] = {start_state: None}
    sequence = 0
    target_state: tuple[FootprintCell, tuple[int, int]] | None = None
    while frontier:
        current_cost, _sequence, state = heapq.heappop(frontier)
        cell, previous_step = state
        if current_cost > costs[state]:
            continue
        if cell == target:
            target_state = state
            break
        for neighbor in navigable_footprint_neighbors(cell, allowed_cells):
            step = _footprint_step(cell, neighbor)
            step_distance = footprint_cell_distance(cell, neighbor)
            clearance = centerline_path.clearance_scores.get(neighbor, 1)
            clearance_penalty = (max_clearance - clearance) / max_clearance
            turn_penalty = _footprint_step_turn_rad(previous_step, step)
            next_cost = (
                current_cost
                + step_distance * (1.0 + 1.5 * clearance_penalty)
                + turn_penalty * 0.5
            )
            next_state = (neighbor, step)
            if next_cost >= costs.get(next_state, math.inf):
                continue
            sequence += 1
            costs[next_state] = next_cost
            previous[next_state] = state
            heapq.heappush(frontier, (next_cost, sequence, next_state))

    if target_state is None:
        return (), math.inf
    path: list[FootprintCell] = []
    state: tuple[FootprintCell, tuple[int, int]] | None = target_state
    while state is not None:
        path.append(state[0])
        state = previous[state]
    return tuple(reversed(path)), costs[target_state]


def _footprint_path_change_cost(
    centerline_path: CenterlinePath,
    cells: tuple[FootprintCell, ...],
) -> float:
    if len(cells) < 2:
        return 0.0
    max_clearance = max(1, max(centerline_path.clearance_scores.values()))
    cost = 0.0
    previous_step = _footprint_step(cells[0], cells[1])
    for first, second in zip(cells, cells[1:], strict=False):
        step = _footprint_step(first, second)
        step_distance = footprint_cell_distance(first, second)
        clearance = centerline_path.clearance_scores.get(second, 1)
        clearance_penalty = (max_clearance - clearance) / max_clearance
        turn_penalty = _footprint_step_turn_rad(previous_step, step)
        cost += step_distance * (1.0 + 1.5 * clearance_penalty) + turn_penalty * 0.5
        previous_step = step
    return cost


def _initial_cone_step(forward: np.ndarray) -> tuple[int, int]:
    x = int(math.copysign(1, float(forward[0]))) if abs(float(forward[0])) >= 0.25 else 0
    z = int(math.copysign(1, float(forward[1]))) if abs(float(forward[1])) >= 0.25 else 0
    if x == 0 and z == 0:
        x = 1
    return x, z


def _footprint_step_turn_rad(
    first: tuple[int, int],
    second: tuple[int, int],
) -> float:
    first_vector = np.asarray(first, dtype=np.float64)
    second_vector = np.asarray(second, dtype=np.float64)
    first_norm = float(np.linalg.norm(first_vector))
    second_norm = float(np.linalg.norm(second_vector))
    if first_norm <= 1e-9 or second_norm <= 1e-9:
        return 0.0
    cosine = float(np.dot(first_vector, second_vector) / (first_norm * second_norm))
    cosine = max(-1.0, min(1.0, cosine))
    return math.acos(cosine)


def _points_for_cone_cells(
    centerline_path: CenterlinePath,
    cells: tuple[FootprintCell, ...],
    *,
    route_samples: _AutoDiveRouteSamples,
    current: Point,
) -> tuple[Point, ...]:
    return tuple(
        _point_for_cone_cell(
            centerline_path,
            cell,
            route_samples=route_samples,
            current=current,
        )
        for cell in cells
    )


def _point_for_cone_cell(
    centerline_path: CenterlinePath,
    cell: FootprintCell,
    *,
    route_samples: _AutoDiveRouteSamples,
    current: Point,
) -> Point:
    cached_points = getattr(centerline_path, "cached_points", None) or {}
    cached_point = cached_points.get(cell)
    if cached_point is not None:
        return cached_point

    x, z = _center_for_route_cell(centerline_path, cell)
    y_ranges = getattr(centerline_path, "cached_y_ranges", None) or {}
    y_range = y_ranges.get(cell)
    if y_range is not None:
        y = (float(y_range[0]) + float(y_range[1])) * 0.5
    elif route_samples.cells and route_samples.points:
        nearest_index = min(
            range(len(route_samples.cells)),
            key=lambda index: (
                footprint_cell_distance(cell, route_samples.cells[index]),
                index,
            ),
        )
        y = float(route_samples.points[nearest_index][1])
    else:
        y = float(current[1])
    if y_range is not None:
        y = min(max(y, float(y_range[0])), float(y_range[1]))
    return float(x), float(y), float(z)


def _route_segment_is_clear_after_start(
    first: Point,
    second: Point,
    *,
    collision_validator: _AutoDiveCollisionValidator,
    allow_low_lateral_clearance: bool = False,
) -> bool:
    distance = math.sqrt(
        (second[0] - first[0]) ** 2
        + (second[1] - first[1]) ** 2
        + (second[2] - first[2]) ** 2
    )
    if distance <= 1e-9:
        return False
    steps = max(
        1,
        int(math.ceil(distance / max(1e-9, collision_validator.cell_size * 0.25))),
    )
    start_step = min(steps, max(1, int(math.ceil(steps * 0.20))))
    previous_cell: FootprintCell | None = None
    for step in range(start_step, steps + 1):
        t = step / steps
        point = (
            first[0] + (second[0] - first[0]) * t,
            first[1] + (second[1] - first[1]) * t,
            first[2] + (second[2] - first[2]) * t,
        )
        if (
            collision_validator.point_clearance_failure(
                point,
                enforce_lateral_clearance=not allow_low_lateral_clearance,
            )
            is not None
        ):
            return False
        cell = _footprint_cell_for_xz(
            (point[0], point[2]),
            collision_validator.cell_size,
        )
        if (
            previous_cell is not None
            and cell != previous_cell
            and not collision_validator.allow_native_graph_transitions
        ):
            if not _footprint_transition_stays_in_footprint(
                previous_cell,
                cell,
                component_cells=collision_validator.component_cells,
            ):
                return False
        previous_cell = cell
    return True


def _sampled_lateral_clearance_scores(
    route_points: tuple[Point, ...],
    *,
    collision_validator: _AutoDiveCollisionValidator,
) -> tuple[int, ...]:
    scores: list[int] = []
    for point in _sampled_route_points(
        route_points,
        cell_size=collision_validator.cell_size,
    ):
        scores.append(
            _lateral_clearance_score_at_point(
                point,
                centerline_path=collision_validator.centerline_path,
            )
        )
    return tuple(scores)


def _sampled_clearance_margins_m(
    route_points: tuple[Point, ...],
    *,
    collision_validator: _AutoDiveCollisionValidator,
) -> tuple[float, ...]:
    margins: list[float] = []
    for point in _sampled_route_points(
        route_points,
        cell_size=collision_validator.cell_size,
    ):
        cell = _footprint_cell_for_xz(
            (point[0], point[2]),
            collision_validator.cell_size,
        )
        margin = collision_validator.cached_clearance_margins.get(cell)
        if margin is None:
            lateral_score = _lateral_clearance_score_at_point(
                point,
                centerline_path=collision_validator.centerline_path,
            )
            margin = max(
                0.0,
                (float(lateral_score) - 0.5) * collision_validator.cell_size,
            )
        margins.append(max(0.0, float(margin)))
    return tuple(margins)


def _sampled_route_points(
    route_points: tuple[Point, ...],
    *,
    cell_size: float,
) -> tuple[Point, ...]:
    if not route_points:
        return ()
    sampled: list[Point] = [route_points[0]]
    for first, second in zip(route_points, route_points[1:], strict=False):
        distance = math.sqrt(
            (second[0] - first[0]) ** 2
            + (second[1] - first[1]) ** 2
            + (second[2] - first[2]) ** 2
        )
        steps = max(1, int(math.ceil(distance / max(1e-9, cell_size * 0.5))))
        for step in range(1, steps + 1):
            t = step / steps
            sampled.append(
                (
                    first[0] + (second[0] - first[0]) * t,
                    first[1] + (second[1] - first[1]) * t,
                    first[2] + (second[2] - first[2]) * t,
                )
            )
    return tuple(sampled)


def _route_geometry_is_trusted(max_segment_cells: float) -> bool:
    return (
        float(max_segment_cells)
        <= float(DEFAULT_AUTO_DIVE_TRUSTED_MAX_SEGMENT_CELLS) + 1e-9
    )


def _route_max_segment_length_m(route_points: tuple[Point, ...]) -> float:
    if len(route_points) < 2:
        return 0.0
    return max(
        math.sqrt(
            (second[0] - first[0]) ** 2
            + (second[1] - first[1]) ** 2
            + (second[2] - first[2]) ** 2
        )
        for first, second in zip(route_points, route_points[1:], strict=False)
    )


def _route_max_segment_xz_cells(
    route_points: tuple[Point, ...],
    *,
    cell_size: float,
) -> float:
    if len(route_points) < 2:
        return 0.0
    max_segment_m = max(
        math.hypot(second[0] - first[0], second[2] - first[2])
        for first, second in zip(route_points, route_points[1:], strict=False)
    )
    return max_segment_m / max(1e-9, float(cell_size))


def _candidate_forward_progress_m(route_points: tuple[Point, ...]) -> float:
    if len(route_points) < 2:
        return 0.0
    start = route_points[0]
    end = route_points[-1]
    return math.sqrt(
        (end[0] - start[0]) ** 2
        + (end[1] - start[1]) ** 2
        + (end[2] - start[2]) ** 2
    )


def _candidate_pullback_penalty_m(route_points: tuple[Point, ...]) -> float:
    if len(route_points) < 3:
        return 0.0
    start = np.asarray(route_points[0], dtype=np.float64)
    first_step = np.asarray(route_points[1], dtype=np.float64) - start
    overall = np.asarray(route_points[-1], dtype=np.float64) - start
    overall_norm = float(np.linalg.norm(overall))
    first_norm = float(np.linalg.norm(first_step))
    if overall_norm <= 1e-9 or first_norm <= 1e-9:
        return 0.0
    projection = float(np.dot(first_step, overall / overall_norm))
    return max(0.0, -projection)


def _route_curvature_rad(route_points: tuple[Point, ...]) -> float:
    if len(route_points) < 3:
        return 0.0
    total = 0.0
    for previous, current, next_point in zip(
        route_points,
        route_points[1:],
        route_points[2:],
        strict=False,
    ):
        first = np.asarray(current, dtype=np.float64) - np.asarray(
            previous,
            dtype=np.float64,
        )
        second = np.asarray(next_point, dtype=np.float64) - np.asarray(
            current,
            dtype=np.float64,
        )
        first_norm = float(np.linalg.norm(first))
        second_norm = float(np.linalg.norm(second))
        if first_norm <= 1e-9 or second_norm <= 1e-9:
            continue
        cosine = float(np.dot(first, second) / (first_norm * second_norm))
        cosine = max(-1.0, min(1.0, cosine))
        total += math.acos(cosine)
    return total


def _route_vertical_jerk_m(route_points: tuple[Point, ...]) -> float:
    if len(route_points) < 3:
        return 0.0
    total = 0.0
    for previous, current, next_point in zip(
        route_points,
        route_points[1:],
        route_points[2:],
        strict=False,
    ):
        total += abs(next_point[1] - 2.0 * current[1] + previous[1])
    return total


def _theta_relaxed_auto_dive_samples(
    route_samples: _AutoDiveRouteSamples,
    *,
    settings: AutoDiveSettings,
    collision_validator: _AutoDiveCollisionValidator,
) -> _AutoDiveRouteSamples:
    """Apply a Theta*-style line-of-sight relaxation to route samples."""
    if len(route_samples.points) <= 2:
        return route_samples
    radius = int(settings.smoothing_radius_cells)
    if radius <= 0:
        return route_samples
    max_skip = max(2, radius)

    relaxed_cells = [route_samples.cells[0]]
    relaxed_points = [route_samples.points[0]]
    current_index = 0
    last_index = len(route_samples.points) - 1
    while current_index < last_index:
        next_index = current_index + 1
        search_limit = min(last_index, current_index + max_skip)
        for candidate_index in range(search_limit, current_index, -1):
            if collision_validator.segment_is_clear(
                route_samples.points[current_index],
                route_samples.points[candidate_index],
            ):
                next_index = candidate_index
                break
        relaxed_cells.append(route_samples.cells[next_index])
        relaxed_points.append(route_samples.points[next_index])
        current_index = next_index

    return _AutoDiveRouteSamples(
        cells=tuple(relaxed_cells),
        points=tuple(relaxed_points),
    )


def _smooth_cached_auto_dive_xz_values(
    centerline_path: CenterlinePath,
    *,
    waypoint_cells: tuple[FootprintCell, ...],
    route_points: tuple[Point, ...],
    settings: AutoDiveSettings,
    collision_validator: _AutoDiveCollisionValidator,
) -> tuple[Point, ...]:
    """Apply cave-constrained runtime X/Z smoothing to cached route samples."""
    cached_points = getattr(centerline_path, "cached_points", None) or {}
    if not cached_points:
        return route_points
    if len(route_points) <= 2:
        return route_points

    radius = int(settings.smoothing_radius_cells)
    if radius <= 0:
        return route_points
    radius = min(radius, max(1, len(route_points) - 1))
    if not collision_validator.component_cells:
        return route_points

    candidates: list[Point] = list(route_points)
    for index, point in enumerate(route_points):
        if index == 0 or index == len(route_points) - 1:
            continue
        candidate_xz = _smoothed_xz_at_index(
            route_points,
            index=index,
            radius=radius,
        )
        safe_xz = _project_xz_to_route_footprint(
            centerline_path,
            waypoint_cells=waypoint_cells,
            index=index,
            xz=candidate_xz,
            radius=radius,
            prefer_clearance=collision_validator.has_route_clearance_metadata,
        )
        candidates[index] = (safe_xz[0], point[1], safe_xz[1])

    smoothed: list[Point] = list(route_points)
    for index in range(1, len(route_points) - 1):
        candidate = candidates[index]
        if (
            _route_segment_stays_in_footprint(
                smoothed[index - 1],
                candidate,
                component_cells=collision_validator.component_cells,
                cell_size=collision_validator.cell_size,
            )
            and _route_segment_stays_in_footprint(
                candidate,
                route_points[index + 1],
                component_cells=collision_validator.component_cells,
                cell_size=collision_validator.cell_size,
            )
            and collision_validator.segment_is_clear(smoothed[index - 1], candidate)
            and collision_validator.segment_is_clear(candidate, route_points[index + 1])
        ):
            smoothed[index] = candidate

    result = tuple(smoothed)
    if collision_validator.route_is_clear(result):
        return result
    return route_points


def _smoothed_xz_at_index(
    route_points: tuple[Point, ...],
    *,
    index: int,
    radius: int,
) -> PointXZ:
    start = max(0, index - radius)
    end = min(len(route_points), index + radius + 1)
    weighted_x = 0.0
    weighted_z = 0.0
    total_weight = 0.0
    for neighbor_index in range(start, end):
        distance = abs(neighbor_index - index)
        weight = float(radius + 1 - distance)
        weighted_x += route_points[neighbor_index][0] * weight
        weighted_z += route_points[neighbor_index][2] * weight
        total_weight += weight
    return weighted_x / max(1e-9, total_weight), weighted_z / max(1e-9, total_weight)


def _project_xz_to_route_footprint(
    centerline_path: CenterlinePath,
    *,
    waypoint_cells: tuple[FootprintCell, ...],
    index: int,
    xz: PointXZ,
    radius: int,
    prefer_clearance: bool = False,
) -> PointXZ:
    cell_size = centerline_path.footprint_cell_size
    component_cells = centerline_path.component_cells
    original_cell = waypoint_cells[index]
    candidate_cell = _footprint_cell_for_xz(xz, cell_size)
    original_clearance = centerline_path.clearance_scores.get(original_cell, 1)
    candidate_clearance = centerline_path.clearance_scores.get(candidate_cell, 0)
    allowed_clearance_loss = 0 if prefer_clearance else 1
    if (
        candidate_cell in component_cells
        and candidate_clearance >= max(1, original_clearance - allowed_clearance_loss)
    ):
        return xz

    start = max(0, index - radius)
    end = min(len(waypoint_cells), index + radius + 1)
    local_candidates = tuple(
        (cell_index, waypoint_cells[cell_index])
        for cell_index in range(start, end)
        if waypoint_cells[cell_index] in component_cells
    )
    if not local_candidates and original_cell in component_cells:
        local_candidates = ((index, original_cell),)
    if not local_candidates:
        return xz

    _best_index, best_cell = min(
        local_candidates,
        key=lambda item: (
            (
                -centerline_path.clearance_scores.get(item[1], 1)
                if prefer_clearance
                else 0
            ),
            _xz_distance_squared(_center_for_route_cell(centerline_path, item[1]), xz),
            -centerline_path.clearance_scores.get(item[1], 1),
            abs(index - item[0]),
            item[1],
        ),
    )
    return _center_for_route_cell(centerline_path, best_cell)


def _route_segments_stay_in_footprint(
    route_points: tuple[Point, ...],
    *,
    component_cells: frozenset[FootprintCell],
    cell_size: float,
) -> bool:
    return all(
        _route_segment_stays_in_footprint(
            first,
            second,
            component_cells=component_cells,
            cell_size=cell_size,
        )
        for first, second in zip(route_points, route_points[1:], strict=False)
    )


def _route_segment_is_clear(
    first: Point,
    second: Point,
    *,
    collision_validator: _AutoDiveCollisionValidator,
) -> bool:
    return _route_segment_clearance_failure(
        first,
        second,
        collision_validator=collision_validator,
    ) is None


def _route_segment_clearance_failure(
    first: Point,
    second: Point,
    *,
    collision_validator: _AutoDiveCollisionValidator,
    segment_index: int | None = None,
    allow_low_lateral_clearance: bool = False,
) -> _AutoDiveClearanceFailure | None:
    distance = math.sqrt(
        (second[0] - first[0]) ** 2
        + (second[1] - first[1]) ** 2
        + (second[2] - first[2]) ** 2
    )
    if distance <= 1e-9:
        return _AutoDiveClearanceFailure(
            kind="segment",
            reason="zero_length_segment",
            segment_index=segment_index,
            first=first,
            second=second,
        )
    steps = max(
        1,
        int(math.ceil(distance / max(1e-9, collision_validator.cell_size * 0.25))),
    )
    previous_cell: FootprintCell | None = None
    for step in range(steps + 1):
        t = step / steps
        point = (
            first[0] + (second[0] - first[0]) * t,
            first[1] + (second[1] - first[1]) * t,
            first[2] + (second[2] - first[2]) * t,
        )
        point_failure = collision_validator.point_clearance_failure(
            point,
            index=step,
            segment_index=segment_index,
            kind="segment_point",
            enforce_lateral_clearance=not allow_low_lateral_clearance,
        )
        if point_failure is not None:
            return _AutoDiveClearanceFailure(
                kind=point_failure.kind,
                reason=point_failure.reason,
                index=point_failure.index,
                segment_index=segment_index,
                cell=point_failure.cell,
                point=point,
                first=first,
                second=second,
            )
        cell = _footprint_cell_for_xz(
            (point[0], point[2]),
            collision_validator.cell_size,
        )
        if (
            previous_cell is not None
            and cell != previous_cell
            and not collision_validator.allow_native_graph_transitions
        ):
            if not _footprint_transition_stays_in_footprint(
                previous_cell,
                cell,
                component_cells=collision_validator.component_cells,
            ):
                return _AutoDiveClearanceFailure(
                    kind="segment",
                    reason="invalid_footprint_transition",
                    index=step,
                    segment_index=segment_index,
                    cell=cell,
                    point=point,
                    first=first,
                    second=second,
                )
        previous_cell = cell
    mesh_failure = _route_segment_mesh_collision_failure(
        first,
        second,
        collision_validator=collision_validator,
        segment_index=segment_index,
    )
    if mesh_failure is not None:
        return mesh_failure
    return None


def _route_segment_mesh_collision_failure(
    first: Point,
    second: Point,
    *,
    collision_validator: _AutoDiveCollisionValidator,
    segment_index: int | None = None,
) -> _AutoDiveClearanceFailure | None:
    if collision_validator.mesh_guard is None:
        return None
    hit = collision_validator.mesh_guard.segment_collision(first, second)
    if hit is None:
        return None
    hit_cell = _footprint_cell_for_xz(
        (hit.point[0], hit.point[2]),
        collision_validator.cell_size,
    )
    return _AutoDiveClearanceFailure(
        kind="segment",
        reason="mesh_intersection",
        segment_index=segment_index,
        cell=hit_cell,
        chunk_cell=hit.chunk_cell,
        point=hit.point,
        first=first,
        second=second,
    )


def _route_segment_stays_in_footprint(
    first: Point,
    second: Point,
    *,
    component_cells: frozenset[FootprintCell],
    cell_size: float,
) -> bool:
    distance = math.hypot(second[0] - first[0], second[2] - first[2])
    steps = max(1, int(math.ceil(distance / max(1e-9, cell_size * 0.25))))
    previous_cell: FootprintCell | None = None
    for step in range(steps + 1):
        t = step / steps
        x = first[0] + (second[0] - first[0]) * t
        z = first[2] + (second[2] - first[2]) * t
        cell = _footprint_cell_for_xz((x, z), cell_size)
        if cell not in component_cells:
            return False
        if previous_cell is not None and cell != previous_cell:
            if not _footprint_transition_stays_in_footprint(
                previous_cell,
                cell,
                component_cells=component_cells,
            ):
                return False
        previous_cell = cell
    return True


def _footprint_transition_stays_in_footprint(
    first: FootprintCell,
    second: FootprintCell,
    *,
    component_cells: frozenset[FootprintCell],
) -> bool:
    dx = second[0] - first[0]
    dz = second[1] - first[1]
    if max(abs(dx), abs(dz)) > 1:
        return False
    if abs(dx) == 1 and abs(dz) == 1:
        if (first[0] + dx, first[1]) not in component_cells:
            return False
        if (first[0], first[1] + dz) not in component_cells:
            return False
    return True


def _footprint_cell_for_xz(xz: PointXZ, cell_size: float) -> FootprintCell:
    return (
        int(math.floor(float(xz[0]) / cell_size)),
        int(math.floor(float(xz[1]) / cell_size)),
    )


def _xz_distance_squared(first: PointXZ, second: PointXZ) -> float:
    return (first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2


def _lateral_clearance_score_at_point(
    point: Point,
    *,
    centerline_path: CenterlinePath,
) -> int:
    cell = _footprint_cell_for_xz(
        (point[0], point[2]),
        centerline_path.footprint_cell_size,
    )
    return int(centerline_path.clearance_scores.get(cell, 0))


def _minimum_required_lateral_clearance_score(
    centerline_path: CenterlinePath,
) -> int:
    if not centerline_path.clearance_scores:
        return 1
    max_score = max(centerline_path.clearance_scores.values())
    return 2 if max_score >= 3 else 1


def _smooth_cached_auto_dive_y_values(
    centerline_path: CenterlinePath,
    *,
    waypoint_cells: tuple[FootprintCell, ...],
    route_points: tuple[Point, ...],
    settings: AutoDiveSettings,
) -> tuple[Point, ...]:
    """Apply runtime Y smoothing to cached 3D route samples.

    Cache metadata stores raw per-cell medial passage heights. The viewer owns
    smoothing so the anticipation radius can be tuned without rebuilding cache.
    Per-cell Y ranges, when available, keep the smoothed point inside the
    detected vertical gap for that footprint cell.
    """
    cached_points = getattr(centerline_path, "cached_points", None) or {}
    if not cached_points:
        return route_points
    if len(route_points) <= 2:
        return route_points

    radius = int(settings.smoothing_radius_cells)
    if radius <= 0:
        return route_points
    radius = min(radius, max(1, len(route_points) - 1))
    cached_y_ranges = getattr(centerline_path, "cached_y_ranges", None) or {}

    smoothed: list[Point] = []
    for index, point in enumerate(route_points):
        if index == 0 or index == len(route_points) - 1:
            smoothed.append(point)
            continue
        start = max(0, index - radius)
        end = min(len(route_points), index + radius + 1)
        weighted_sum = 0.0
        total_weight = 0.0
        for neighbor_index in range(start, end):
            distance = abs(neighbor_index - index)
            weight = float(radius + 1 - distance)
            weighted_sum += route_points[neighbor_index][1] * weight
            total_weight += weight
        y = weighted_sum / max(1e-9, total_weight)
        y_range = cached_y_ranges.get(waypoint_cells[index])
        if y_range is not None:
            low_y, high_y = y_range
            y = min(max(y, low_y), high_y)
        smoothed.append((point[0], y, point[2]))
    return tuple(smoothed)


def _repelled_auto_dive_points(
    centerline_path: CenterlinePath,
    *,
    waypoint_cells: tuple[FootprintCell, ...],
    route_points: tuple[Point, ...],
    settings: AutoDiveSettings,
    collision_validator: _AutoDiveCollisionValidator,
) -> tuple[Point, ...]:
    """Nudge interior route points toward locally clearer passage centers."""
    if len(route_points) <= 2:
        return route_points
    if not collision_validator.component_cells:
        return route_points

    search_radius = _auto_dive_repulsion_search_radius_cells(settings)
    repelled: list[Point] = list(route_points)
    for index in range(1, len(route_points) - 1):
        previous = repelled[index - 1]
        point = route_points[index]
        next_point = route_points[index + 1]
        best_point = point
        best_score = _repelled_auto_dive_point_score(
            point,
            previous=previous,
            next_point=next_point,
            original_point=point,
            collision_validator=collision_validator,
        )
        for candidate in _repelled_auto_dive_point_candidates(
            centerline_path,
            waypoint_cells=waypoint_cells,
            route_points=route_points,
            index=index,
            search_radius_cells=search_radius,
            collision_validator=collision_validator,
        ):
            score = _repelled_auto_dive_point_score(
                candidate,
                previous=previous,
                next_point=next_point,
                original_point=point,
                collision_validator=collision_validator,
            )
            if score > best_score:
                best_score = score
                best_point = candidate
        repelled[index] = best_point
    return tuple(repelled)


def _auto_dive_repulsion_search_radius_cells(
    settings: AutoDiveSettings,
) -> int:
    radius = max(1, int(settings.smoothing_radius_cells))
    return max(1, min(2, radius // 2))


def _repelled_auto_dive_point_candidates(
    centerline_path: CenterlinePath,
    *,
    waypoint_cells: tuple[FootprintCell, ...],
    route_points: tuple[Point, ...],
    index: int,
    search_radius_cells: int,
    collision_validator: _AutoDiveCollisionValidator,
) -> tuple[Point, ...]:
    point = route_points[index]
    point_cell = _footprint_cell_for_xz(
        (point[0], point[2]),
        collision_validator.cell_size,
    )
    waypoint_cell = (
        waypoint_cells[index]
        if 0 <= index < len(waypoint_cells)
        else point_cell
    )
    anchor_cells = {point_cell, waypoint_cell}
    candidate_cells: set[FootprintCell] = set()
    radius = max(1, int(search_radius_cells))
    for anchor_cell in anchor_cells:
        for dx in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                candidate_cell = (anchor_cell[0] + dx, anchor_cell[1] + dz)
                if candidate_cell not in collision_validator.component_cells:
                    continue
                if footprint_cell_distance(anchor_cell, candidate_cell) > radius:
                    continue
                candidate_cells.add(candidate_cell)

    if point_cell in collision_validator.component_cells:
        candidate_cells.add(point_cell)
    if waypoint_cell in collision_validator.component_cells:
        candidate_cells.add(waypoint_cell)

    ordered_cells = sorted(
        candidate_cells,
        key=lambda cell: (
            -centerline_path.clearance_scores.get(cell, 1),
            footprint_cell_distance(point_cell, cell),
            cell,
        ),
    )
    candidates: list[Point] = [point]
    for cell in ordered_cells[:24]:
        candidates.append(
            _repelled_auto_dive_point_for_cell(
                centerline_path,
                cell,
                fallback_y=point[1],
            )
        )
    return _dedupe_consecutive_points(tuple(candidates), min_distance_m=1e-6)


def _repelled_auto_dive_point_for_cell(
    centerline_path: CenterlinePath,
    cell: FootprintCell,
    *,
    fallback_y: float,
) -> Point:
    x, z = _center_for_route_cell(centerline_path, cell)
    y = _medial_y_for_route_cell(
        centerline_path,
        cell,
        fallback_y=fallback_y,
    )
    return (float(x), float(y), float(z))


def _medial_y_for_route_cell(
    centerline_path: CenterlinePath,
    cell: FootprintCell,
    *,
    fallback_y: float,
) -> float:
    cached_y_ranges = getattr(centerline_path, "cached_y_ranges", None) or {}
    y_range = cached_y_ranges.get(cell)
    if y_range is not None:
        return (float(y_range[0]) + float(y_range[1])) * 0.5
    cached_points = getattr(centerline_path, "cached_points", None) or {}
    cached_point = cached_points.get(cell)
    if cached_point is not None:
        return float(cached_point[1])
    return float(fallback_y)


def _repelled_auto_dive_point_score(
    point: Point,
    *,
    previous: Point,
    next_point: Point,
    original_point: Point,
    collision_validator: _AutoDiveCollisionValidator,
) -> tuple[object, ...]:
    first_failure = collision_validator.segment_clearance_failure(previous, point)
    second_failure = collision_validator.segment_clearance_failure(point, next_point)
    clear_segment_count = int(first_failure is None) + int(second_failure is None)
    point_clear = collision_validator.point_clearance_failure(point) is None
    cell = _footprint_cell_for_xz(
        (point[0], point[2]),
        collision_validator.cell_size,
    )
    lateral_score = _lateral_clearance_score_at_point(
        point,
        centerline_path=collision_validator.centerline_path,
    )
    clearance_margin_m = _look_point_clearance_margin_m(
        point,
        collision_validator=collision_validator,
    )
    local_curvature = _route_curvature_rad((previous, point, next_point))
    local_vertical_change = abs(next_point[1] - 2.0 * point[1] + previous[1])
    midpoint = (
        (previous[0] + next_point[0]) * 0.5,
        (previous[1] + next_point[1]) * 0.5,
        (previous[2] + next_point[2]) * 0.5,
    )
    midpoint_distance = math.sqrt(
        (point[0] - midpoint[0]) ** 2
        + (point[1] - midpoint[1]) ** 2
        + (point[2] - midpoint[2]) ** 2
    )
    move_distance = math.sqrt(
        (point[0] - original_point[0]) ** 2
        + (point[1] - original_point[1]) ** 2
        + (point[2] - original_point[2]) ** 2
    )
    return (
        clear_segment_count,
        bool(point_clear),
        round(float(clearance_margin_m), 3),
        int(lateral_score),
        -round(float(local_curvature), 3),
        -round(float(local_vertical_change), 3),
        -round(float(midpoint_distance), 3),
        -round(float(move_distance), 3),
        cell,
    )


def _bspline_smoothed_auto_dive_points(
    route_points: tuple[Point, ...],
    *,
    settings: AutoDiveSettings,
    collision_validator: _AutoDiveCollisionValidator,
) -> tuple[Point, ...]:
    """Fit a collision-checked cubic B-spline candidate over route points."""
    radius = int(settings.smoothing_radius_cells)
    if radius <= 0 or len(route_points) < 4:
        return route_points
    max_keyframes = max(2, int(settings.max_keyframes))
    samples_per_segment = min(
        max(2, min(4, radius)),
        max(1, max_keyframes // max(1, len(route_points) - 1)),
    )
    if samples_per_segment < 2:
        return route_points

    candidate: list[Point] = [route_points[0]]
    for index in range(len(route_points) - 1):
        p0 = route_points[max(0, index - 1)]
        p1 = route_points[index]
        p2 = route_points[index + 1]
        p3 = route_points[min(len(route_points) - 1, index + 2)]
        for sample_index in range(1, samples_per_segment + 1):
            t = sample_index / samples_per_segment
            point = _cubic_bspline_point(p0, p1, p2, p3, t)
            candidate.append(point)
    candidate[-1] = route_points[-1]
    candidate_points = _dedupe_consecutive_points(tuple(candidate))
    if collision_validator.route_is_clear(candidate_points):
        return candidate_points
    return route_points


def _cubic_bspline_point(
    p0: Point,
    p1: Point,
    p2: Point,
    p3: Point,
    t: float,
) -> Point:
    t = max(0.0, min(1.0, float(t)))
    t2 = t * t
    t3 = t2 * t
    b0 = (1.0 - 3.0 * t + 3.0 * t2 - t3) / 6.0
    b1 = (4.0 - 6.0 * t2 + 3.0 * t3) / 6.0
    b2 = (1.0 + 3.0 * t + 3.0 * t2 - 3.0 * t3) / 6.0
    b3 = t3 / 6.0
    return (
        p0[0] * b0 + p1[0] * b1 + p2[0] * b2 + p3[0] * b3,
        p0[1] * b0 + p1[1] * b1 + p2[1] * b2 + p3[1] * b3,
        p0[2] * b0 + p1[2] * b1 + p2[2] * b2 + p3[2] * b3,
    )


def _waypoint_cells_for_footprint_route(
    route_cells: tuple[FootprintCell, ...],
    *,
    cell_size: float,
    settings: AutoDiveSettings,
) -> tuple[FootprintCell, ...]:
    cells = _dedupe_consecutive_cells(route_cells)
    if len(cells) <= 2:
        return cells

    spacing = settings.keyframe_spacing_m
    if spacing is None:
        spacing = max(float(cell_size) * 4.0, 5.0)
    spacing = max(float(cell_size), float(spacing))
    spacing_since_waypoint = 0.0
    waypoint_limit = max(2, int(settings.max_keyframes))
    waypoints = [cells[0]]

    for index in range(1, len(cells) - 1):
        previous_cell = cells[index - 1]
        cell = cells[index]
        next_cell = cells[index + 1]
        previous_direction = _footprint_step(previous_cell, cell)
        next_direction = _footprint_step(cell, next_cell)
        spacing_since_waypoint += (
            footprint_cell_distance(previous_cell, cell) * float(cell_size)
        )
        must_keep_for_bend = previous_direction != next_direction
        can_keep_for_spacing = len(waypoints) < waypoint_limit - 1
        if must_keep_for_bend or (
            can_keep_for_spacing and spacing_since_waypoint >= spacing
        ):
            waypoints.append(cell)
            spacing_since_waypoint = 0.0

    waypoints.append(cells[-1])
    return tuple(waypoints)


def _current_footprint_cell(
    centerline_path: CenterlinePath,
    current: np.ndarray,
) -> FootprintCell:
    cell_size = centerline_path.footprint_cell_size
    return (
        int(math.floor(float(current[0]) / cell_size)),
        int(math.floor(float(current[2]) / cell_size)),
    )


def _center_for_route_cell(
    centerline_path: CenterlinePath,
    cell: FootprintCell,
) -> PointXZ:
    return centerline_path.centers.get(
        cell,
        footprint_world_center(cell, centerline_path.footprint_cell_size),
    )


def _cell_center_distance_squared(
    centerline_path: CenterlinePath,
    cell: FootprintCell,
    current: np.ndarray,
) -> float:
    center = _center_for_route_cell(centerline_path, cell)
    return (
        (center[0] - float(current[0])) ** 2
        + (center[1] - float(current[2])) ** 2
    )


def _footprint_step(
    first: FootprintCell,
    second: FootprintCell,
) -> tuple[int, int]:
    return (
        int(math.copysign(1, second[0] - first[0]))
        if second[0] != first[0]
        else 0,
        int(math.copysign(1, second[1] - first[1]))
        if second[1] != first[1]
        else 0,
    )


def _dedupe_consecutive_cells(
    cells: tuple[FootprintCell, ...],
) -> tuple[FootprintCell, ...]:
    deduped: list[FootprintCell] = []
    for cell in cells:
        if not deduped or deduped[-1] != cell:
            deduped.append(cell)
    return tuple(deduped)


def _dedupe_consecutive_points(
    points: tuple[Point, ...],
    *,
    min_distance_m: float = 1e-4,
) -> tuple[Point, ...]:
    deduped: list[Point] = []
    min_distance_sq = max(0.0, float(min_distance_m)) ** 2
    for point in points:
        if not deduped:
            deduped.append(point)
            continue
        previous = deduped[-1]
        distance_sq = sum(
            (float(point[index]) - float(previous[index])) ** 2
            for index in range(3)
        )
        if distance_sq > min_distance_sq:
            deduped.append(point)
    return tuple(deduped)


def _route_points_starting_at_current_camera(
    route_points: tuple[Point, ...],
    current: np.ndarray,
) -> tuple[Point, ...]:
    """Return route points with the current camera position as the first point."""
    current_point: Point = (
        float(current[0]),
        float(current[1]),
        float(current[2]),
    )
    if not route_points:
        return (current_point,)
    first_point = route_points[0]
    distance_squared = sum(
        (first_point[index] - current_point[index]) ** 2
        for index in range(3)
    )
    if distance_squared <= 1e-12:
        return route_points
    return (current_point, *route_points)


def _footprint_cells_length(
    centerline_path: CenterlinePath,
    cells: tuple[FootprintCell, ...],
) -> float:
    if len(cells) < 2:
        return 0.0
    centers = {
        cell: _center_for_route_cell(centerline_path, cell)
        for cell in cells
    }
    return footprint_path_length(cells, centers)


def route_progress_fraction(route: CameraRoute, elapsed_s: float) -> float:
    """Return bounded elapsed progress for UI display."""
    return max(0.0, min(1.0, float(elapsed_s) / max(1e-9, route.duration_s)))
