"""User-facing centerline Auto Dive route planning."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import heapq
import math
from typing import Any

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
from caveviewer.core.navigation.route import (
    CameraRoute,
    NavigationConfigurationError,
    RouteKeyframe,
    path_length,
    route_keyframes_for_points,
)


DEFAULT_AUTO_DIVE_RENDER_DISTANCE_CELLS = 10
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

AutoDiveDiagnosticSink = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True)
class AutoDiveSettings:
    """Configuration for user-facing centerline Auto Dive planning."""

    render_distance_cells: int = DEFAULT_AUTO_DIVE_RENDER_DISTANCE_CELLS
    speed_m_per_second: float = DEFAULT_AUTO_DIVE_SPEED_M_PER_SECOND
    y_search_radius_cells: int = DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS
    vertical_position_fraction: float = DEFAULT_AUTO_DIVE_VERTICAL_POSITION_FRACTION
    closed_loop_gap_fraction: float = DEFAULT_AUTO_DIVE_CLOSED_LOOP_GAP_FRACTION
    max_keyframes: int = DEFAULT_AUTO_DIVE_MAX_KEYFRAMES
    keyframe_spacing_m: float | None = None
    smoothing_radius_cells: int = DEFAULT_AUTO_DIVE_SMOOTHING_RADIUS_CELLS
    lookahead_distance_m: float = DEFAULT_AUTO_DIVE_LOOKAHEAD_DISTANCE_M


@dataclass(frozen=True)
class _AutoDiveRouteSamples:
    cells: tuple[FootprintCell, ...]
    points: tuple[Point, ...]


@dataclass(frozen=True)
class _AutoDiveCandidateSpec:
    name: str
    smoothing_radius_cells: int
    use_theta: bool
    use_weighted_smoothing: bool
    use_bspline: bool


@dataclass(frozen=True)
class _AutoDiveRouteCandidate:
    ordinal: int
    name: str
    cells: tuple[FootprintCell, ...]
    points: tuple[Point, ...]


@dataclass(frozen=True)
class _AutoDiveRouteCandidateScore:
    route_clear: bool
    entry_clear: bool
    min_lateral_clearance_cells: int
    mean_lateral_clearance_cells: float
    min_clearance_margin_m: float
    forward_progress_m: float
    pullback_penalty_m: float
    curvature_rad: float
    vertical_jerk_m: float
    length_m: float
    point_count: int

    @property
    def sort_key(self) -> tuple[object, ...]:
        return (
            bool(self.route_clear),
            bool(self.entry_clear),
            int(self.min_lateral_clearance_cells),
            float(self.min_clearance_margin_m),
            float(self.mean_lateral_clearance_cells),
            float(self.forward_progress_m),
            -float(self.pullback_penalty_m),
            -float(self.curvature_rad),
            -float(self.vertical_jerk_m),
            -float(self.length_m),
            -int(self.point_count),
        )


@dataclass(frozen=True)
class _AutoDiveCollisionValidator:
    """Route collision seam for Auto Dive path candidates.

    This is deliberately small and runtime-only. Today it validates against the
    cached navigation footprint and cached vertical gap ranges when available.
    A mesh BVH validator can be plugged in behind the same point/segment API
    once mesh collision metadata is cached.
    """

    centerline_path: CenterlinePath

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

    def point_is_clear(self, point: Point) -> bool:
        cell = _footprint_cell_for_xz((point[0], point[2]), self.cell_size)
        if cell not in self.component_cells:
            return False
        y_range = self.cached_y_ranges.get(cell)
        if y_range is not None:
            tolerance = max(1e-6, self.cell_size * 0.01)
            if not y_range[0] - tolerance <= point[1] <= y_range[1] + tolerance:
                return False
        if not self.has_route_clearance_metadata:
            return True
        return _lateral_clearance_score_at_point(
            point,
            centerline_path=self.centerline_path,
        ) >= _minimum_required_lateral_clearance_score(self.centerline_path)

    def segment_is_clear(self, first: Point, second: Point) -> bool:
        return _route_segment_is_clear(
            first,
            second,
            collision_validator=self,
        )

    def route_is_clear(self, route_points: tuple[Point, ...]) -> bool:
        if not route_points:
            return False
        if not all(self.point_is_clear(point) for point in route_points):
            return False
        return all(
            self.segment_is_clear(first, second)
            for first, second in zip(route_points, route_points[1:], strict=False)
        )


@dataclass(frozen=True)
class AutoDivePlan:
    """Finite Auto Dive route derived from a manifest centerline."""

    route: CameraRoute
    centerline_path: CenterlinePath
    route_points: tuple[Point, ...]
    route_cells: tuple[FootprintCell, ...]
    circular_arc: bool
    route_length_m: float
    duration_s: float
    render_distance_cells: int

    @property
    def route_points_xz(self) -> tuple[PointXZ, ...]:
        """Return route points projected into minimap X/Z space."""
        return tuple((point[0], point[2]) for point in self.route_points)


def build_auto_dive_initial_camera_pose(
    manifest: Mapping[str, Any],
    *,
    settings: AutoDiveSettings | None = None,
) -> RouteKeyframe:
    """Return a safe endpoint camera pose for a newly loaded map.

    Viewer startup historically used the first manifest chunk center. On maps
    imported in phases, that can place the camera in the middle of a passage
    or close to a cave face, which makes the first Auto Dive replan fight its
    way out of a bad local pose. This helper chooses one endpoint of the
    selected centerline route, then returns the first route keyframe looking
    down the clearest available initial segment.
    """
    settings = settings or AutoDiveSettings()
    _validate_auto_dive_settings(settings)
    centerline_path = cached_centerline_path(manifest)
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
            "Auto Dive initial camera requires a centerline endpoint"
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
                    )
                )
            except NavigationConfigurationError:
                continue
        if plans:
            best_plan = max(plans, key=_auto_dive_initial_plan_score)
            return best_plan.route.keyframes[0]

    raise NavigationConfigurationError(
        "Auto Dive initial camera could not build an endpoint route"
    )


def build_centerline_auto_dive_plan(
    manifest: Mapping[str, Any],
    *,
    current_position: tuple[float, float, float] | np.ndarray,
    settings: AutoDiveSettings | None = None,
    diagnostics: AutoDiveDiagnosticSink | None = None,
) -> AutoDivePlan:
    """Build a finite centerline Auto Dive route near the current camera.

    The route uses the longest manifest-derived centerline and deliberately
    ignores texture or chunk complexity. If the selected path is circular, the
    returned route uses an open arc with a gap instead of a complete loop.
    """
    settings = settings or AutoDiveSettings()
    _validate_auto_dive_settings(settings)
    current = np.asarray(current_position, dtype=np.float64)
    if current.shape != (3,):
        raise NavigationConfigurationError("current_position must be a 3D point")

    centerline_path = cached_centerline_path(manifest)
    if centerline_path is None:
        centerline_path = generate_centerline_path(
            manifest,
            component_selection=CENTERLINE_COMPONENT_SELECTION_LONGEST_PATH,
        )
    if len(centerline_path.cells) < 2:
        raise NavigationConfigurationError("Auto Dive requires a multi-point centerline")

    nearest_index = _nearest_centerline_index(
        centerline_path,
        current_x=float(current[0]),
        current_z=float(current[2]),
    )
    route_cells, circular_arc = _select_auto_dive_cells(
        centerline_path,
        nearest_index=nearest_index,
        closed_loop_gap_fraction=settings.closed_loop_gap_fraction,
    )
    if len(route_cells) < 2:
        raise NavigationConfigurationError("Auto Dive route is too short")

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
        _center_for_route_cell(centerline_path, cell)
        for cell in waypoint_cells
    )
    route_points = _auto_dive_points_for_waypoint_cells(
        centerline_path,
        waypoint_cells=waypoint_cells,
        route_xz=route_xz,
        manifest=manifest,
        settings=settings,
    )
    collision_validator = _AutoDiveCollisionValidator(centerline_path)
    route_points = _select_best_auto_dive_route_candidate(
        centerline_path,
        waypoint_cells=waypoint_cells,
        route_points=route_points,
        current=current,
        settings=settings,
        collision_validator=collision_validator,
        diagnostics=diagnostics,
    )
    route_points = _route_points_starting_at_current_camera(route_points, current)
    route_points = _dedupe_consecutive_points(route_points)
    length_m = path_length(route_points)
    if length_m <= 1e-6:
        raise NavigationConfigurationError("Auto Dive route has no travel distance")
    duration_s = length_m / float(settings.speed_m_per_second)
    keyframe_payloads = route_keyframes_for_points(
        route_points,
        duration_s=duration_s,
        lookahead_distance_m=max(0.0, float(settings.lookahead_distance_m)),
    )
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
    )


def _validate_auto_dive_settings(settings: AutoDiveSettings) -> None:
    if int(settings.render_distance_cells) <= 0:
        raise NavigationConfigurationError("Auto Dive render distance must be positive")
    speed = float(settings.speed_m_per_second)
    if not math.isfinite(speed) or speed <= 0.0:
        raise NavigationConfigurationError("Auto Dive speed must be positive")
    vertical_fraction = float(settings.vertical_position_fraction)
    if (
        not math.isfinite(vertical_fraction)
        or not 0.0 <= vertical_fraction <= 1.0
    ):
        raise NavigationConfigurationError(
            "Auto Dive vertical position fraction must be between 0 and 1"
        )
    gap = float(settings.closed_loop_gap_fraction)
    if not math.isfinite(gap) or not 0.0 < gap < 1.0:
        raise NavigationConfigurationError(
            "Auto Dive closed-loop gap fraction must be between 0 and 1"
        )
    if int(settings.max_keyframes) < 2:
        raise NavigationConfigurationError("Auto Dive requires at least 2 keyframes")
    if int(settings.smoothing_radius_cells) < 0:
        raise NavigationConfigurationError(
            "Auto Dive smoothing radius cannot be negative"
        )
    lookahead = float(settings.lookahead_distance_m)
    if not math.isfinite(lookahead) or lookahead < 0.0:
        raise NavigationConfigurationError(
            "Auto Dive look-ahead distance cannot be negative"
        )


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
) -> tuple[tuple[FootprintCell, ...], bool]:
    cells = centerline_path.cells
    if _centerline_cells_form_closed_loop(cells):
        return (
            _open_arc_from_closed_loop(
                cells,
                start_index=nearest_index,
                gap_fraction=closed_loop_gap_fraction,
            ),
            True,
        )

    return tuple(cells[nearest_index:]), False


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

    Auto Dive routes always prepend the exact current camera point later. If a
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
    cells = _dedupe_consecutive_cells(route_cells)
    waypoint_limit = max(2, int(settings.max_keyframes))
    if len(cells) <= waypoint_limit:
        return cells

    return _waypoint_cells_for_footprint_route(
        cells,
        cell_size=centerline_path.footprint_cell_size,
        settings=settings,
    )


def _auto_dive_points_for_waypoint_cells(
    centerline_path: CenterlinePath,
    *,
    waypoint_cells: tuple[FootprintCell, ...],
    route_xz: tuple[PointXZ, ...],
    manifest: Mapping[str, Any],
    settings: AutoDiveSettings,
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
        cached_point = cached_points.get(cell)
        if cached_point is not None:
            route_points.append(cached_point)
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
    diagnostics: AutoDiveDiagnosticSink | None = None,
) -> tuple[Point, ...]:
    """Generate and score local route candidates, returning the safest route.

    This turns smoothing into candidate selection. Raw, Theta-relaxed,
    weighted-smoothed, and B-spline candidates compete on collision validity,
    lateral clearance, margin, forward progress, curvature, and vertical jerk.
    The caller still prepends the exact camera position after this selection.
    """
    specs = _auto_dive_candidate_specs(settings)
    route_samples = _AutoDiveRouteSamples(cells=waypoint_cells, points=route_points)
    if len(specs) <= 1:
        candidate = _build_auto_dive_route_candidate(
            specs[0],
            ordinal=0,
            centerline_path=centerline_path,
            route_samples=route_samples,
            settings=settings,
            collision_validator=collision_validator,
        )
        _record_auto_dive_diagnostic(
            diagnostics,
            "candidate_scores",
            {
                "selected": candidate.name,
                "candidate_count": 1,
                "candidates": [
                    {
                        "name": candidate.name,
                        "ordinal": candidate.ordinal,
                        "point_count": len(candidate.points),
                    }
                ],
            },
        )
        return candidate.points

    candidates: list[_AutoDiveRouteCandidate] = []
    failed_candidates: list[dict[str, Any]] = []
    worker_count = min(4, len(specs))
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
                settings=settings,
                collision_validator=collision_validator,
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
                "failed_candidates": failed_candidates,
            },
        )
        return route_points

    current_point: Point = (
        float(current[0]),
        float(current[1]),
        float(current[2]),
    )
    scored = [
        (
            _score_auto_dive_route_candidate(
                candidate,
                current_point=current_point,
                collision_validator=collision_validator,
            ),
            candidate,
        )
        for candidate in candidates
        if len(candidate.points) >= 1
    ]
    if not scored:
        _record_auto_dive_diagnostic(
            diagnostics,
            "candidate_scores",
            {
                "selected": "fallback_raw_points",
                "candidate_count": len(candidates),
                "failed_candidates": failed_candidates,
                "reason": "no_scoreable_candidates",
            },
        )
        return route_points

    _best_score, best_candidate = max(
        scored,
        key=lambda item: (*item[0].sort_key, -item[1].ordinal),
    )
    _record_auto_dive_diagnostic(
        diagnostics,
        "candidate_scores",
        {
            "selected": best_candidate.name,
            "candidate_count": len(candidates),
            "failed_candidates": failed_candidates,
            "candidates": [
                _auto_dive_candidate_score_payload(score, candidate)
                for score, candidate in sorted(
                    scored,
                    key=lambda item: item[1].ordinal,
                )
            ],
        },
    )
    return best_candidate.points


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


def _auto_dive_candidate_score_payload(
    score: _AutoDiveRouteCandidateScore,
    candidate: _AutoDiveRouteCandidate,
) -> dict[str, Any]:
    return {
        "name": candidate.name,
        "ordinal": candidate.ordinal,
        "route_clear": bool(score.route_clear),
        "entry_clear": bool(score.entry_clear),
        "min_lateral_clearance_cells": int(score.min_lateral_clearance_cells),
        "mean_lateral_clearance_cells": float(score.mean_lateral_clearance_cells),
        "min_clearance_margin_m": float(score.min_clearance_margin_m),
        "forward_progress_m": float(score.forward_progress_m),
        "pullback_penalty_m": float(score.pullback_penalty_m),
        "curvature_rad": float(score.curvature_rad),
        "vertical_jerk_m": float(score.vertical_jerk_m),
        "length_m": float(score.length_m),
        "point_count": int(score.point_count),
    }


def _auto_dive_candidate_specs(
    settings: AutoDiveSettings,
) -> tuple[_AutoDiveCandidateSpec, ...]:
    radius = max(0, int(settings.smoothing_radius_cells))
    specs = [
        _AutoDiveCandidateSpec(
            name="raw",
            smoothing_radius_cells=0,
            use_theta=False,
            use_weighted_smoothing=False,
            use_bspline=False,
        )
    ]
    if radius <= 0:
        return tuple(specs)

    radii = tuple(
        sorted(
            {
                max(1, radius // 2),
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
                    use_weighted_smoothing=False,
                    use_bspline=False,
                ),
                _AutoDiveCandidateSpec(
                    name=f"weighted-{candidate_radius}",
                    smoothing_radius_cells=candidate_radius,
                    use_theta=False,
                    use_weighted_smoothing=True,
                    use_bspline=False,
                ),
                _AutoDiveCandidateSpec(
                    name=f"theta-weighted-{candidate_radius}",
                    smoothing_radius_cells=candidate_radius,
                    use_theta=True,
                    use_weighted_smoothing=True,
                    use_bspline=False,
                ),
                _AutoDiveCandidateSpec(
                    name=f"weighted-bspline-{candidate_radius}",
                    smoothing_radius_cells=candidate_radius,
                    use_theta=False,
                    use_weighted_smoothing=True,
                    use_bspline=True,
                ),
                _AutoDiveCandidateSpec(
                    name=f"theta-weighted-bspline-{candidate_radius}",
                    smoothing_radius_cells=candidate_radius,
                    use_theta=True,
                    use_weighted_smoothing=True,
                    use_bspline=True,
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
    settings: AutoDiveSettings,
    collision_validator: _AutoDiveCollisionValidator,
) -> _AutoDiveRouteCandidate:
    candidate_settings = replace(
        settings,
        smoothing_radius_cells=max(0, int(spec.smoothing_radius_cells)),
    )
    samples = route_samples
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
) -> _AutoDiveRouteCandidateScore:
    route_clear = collision_validator.route_is_clear(candidate.points)
    entry_clear = _entry_segment_is_clear(
        current_point,
        candidate.points[0],
        collision_validator=collision_validator,
    )
    route_with_current = _route_points_starting_at_current_camera(
        candidate.points,
        np.asarray(current_point, dtype=np.float64),
    )
    lateral_scores = _sampled_lateral_clearance_scores(
        route_with_current,
        collision_validator=collision_validator,
    )
    clearance_margins = _sampled_clearance_margins_m(
        route_with_current,
        collision_validator=collision_validator,
    )
    return _AutoDiveRouteCandidateScore(
        route_clear=route_clear,
        entry_clear=entry_clear,
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
        forward_progress_m=_candidate_forward_progress_m(route_with_current),
        pullback_penalty_m=_candidate_pullback_penalty_m(route_with_current),
        curvature_rad=_route_curvature_rad(route_with_current),
        vertical_jerk_m=_route_vertical_jerk_m(route_with_current),
        length_m=path_length(route_with_current),
        point_count=len(route_with_current),
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


def _route_segment_is_clear_after_start(
    first: Point,
    second: Point,
    *,
    collision_validator: _AutoDiveCollisionValidator,
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
        if not collision_validator.point_is_clear(point):
            return False
        cell = _footprint_cell_for_xz(
            (point[0], point[2]),
            collision_validator.cell_size,
        )
        if previous_cell is not None and cell != previous_cell:
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
    distance = math.sqrt(
        (second[0] - first[0]) ** 2
        + (second[1] - first[1]) ** 2
        + (second[2] - first[2]) ** 2
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
        if not collision_validator.point_is_clear(point):
            return False
        cell = _footprint_cell_for_xz(
            (point[0], point[2]),
            collision_validator.cell_size,
        )
        if previous_cell is not None and cell != previous_cell:
            if not _footprint_transition_stays_in_footprint(
                previous_cell,
                cell,
                component_cells=collision_validator.component_cells,
            ):
                return False
        previous_cell = cell
    return True


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
