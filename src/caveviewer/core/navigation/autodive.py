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


@dataclass(frozen=True)
class _ConeChainCandidate:
    cells: tuple[FootprintCell, ...]
    anchor_index: int
    cost: float


@dataclass(frozen=True)
class _AutoDiveClearanceFailure:
    kind: str
    reason: str
    index: int | None = None
    segment_index: int | None = None
    cell: FootprintCell | None = None
    point: Point | None = None
    first: Point | None = None
    second: Point | None = None


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
            int(self.min_lateral_clearance_cells),
            float(self.min_clearance_margin_m),
            float(self.mean_lateral_clearance_cells),
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
        return self.point_clearance_failure(point) is None

    def point_clearance_failure(
        self,
        point: Point,
        *,
        index: int | None = None,
        segment_index: int | None = None,
        kind: str = "point",
    ) -> _AutoDiveClearanceFailure | None:
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
        if not self.has_route_clearance_metadata:
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
    ) -> _AutoDiveClearanceFailure | None:
        return _route_segment_clearance_failure(
            first,
            second,
            collision_validator=self,
            segment_index=segment_index,
        )

    def route_is_clear(self, route_points: tuple[Point, ...]) -> bool:
        return self.route_clearance_failure(route_points) is None

    def route_clearance_failure(
        self,
        route_points: tuple[Point, ...],
    ) -> _AutoDiveClearanceFailure | None:
        if not route_points:
            return _AutoDiveClearanceFailure(
                kind="route",
                reason="empty_route",
            )
        for index, point in enumerate(route_points):
            failure = self.point_clearance_failure(point, index=index)
            if failure is not None:
                return failure
        for segment_index, (first, second) in enumerate(
            zip(route_points, route_points[1:], strict=False)
        ):
            failure = self.segment_clearance_failure(
                first,
                second,
                segment_index=segment_index,
            )
            if failure is not None:
                return failure
        return None


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
    keyframe_payloads = _wall_aware_auto_dive_keyframe_payloads(
        keyframe_payloads,
        route_points=route_points,
        centerline_path=centerline_path,
        settings=settings,
        collision_validator=collision_validator,
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


def _wall_aware_auto_dive_keyframe_payloads(
    keyframe_payloads: list[dict[str, Any]],
    *,
    route_points: tuple[Point, ...],
    centerline_path: CenterlinePath,
    settings: AutoDiveSettings,
    collision_validator: _AutoDiveCollisionValidator,
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
) -> tuple[float, float]:
    yaw_offsets = (0.0, -6.0, 6.0, -12.0, 12.0, -20.0, 20.0)
    pitch_offsets = (0.0, -4.0, 4.0, -8.0, 8.0)
    best_score: tuple[object, ...] | None = None
    best_angles = (float(base_yaw_deg), float(base_pitch_deg))
    default_score: tuple[object, ...] | None = None
    for yaw_offset in yaw_offsets:
        for pitch_offset in pitch_offsets:
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
            current=current,
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

    selectable = [
        item
        for item in scored
        if item[0].route_clear
    ]
    if not selectable:
        selectable = [
            item
            for item in scored
            if item[0].entry_clear and not item[1].name.startswith("cone-")
        ]
    if not selectable:
        selectable = [
            item
            for item in scored
            if item[1].name == "raw"
        ] or scored

    _best_score, best_candidate = max(
        selectable,
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
    if failure.point is not None:
        payload["point"] = [float(value) for value in failure.point]
    if failure.first is not None:
        payload["first"] = [float(value) for value in failure.first]
    if failure.second is not None:
        payload["second"] = [float(value) for value in failure.second]
    return payload


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
) -> _AutoDiveRouteCandidateScore:
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
        route_with_current
    )
    route_clear = first_clearance_failure is None
    lateral_scores = _sampled_lateral_clearance_scores(
        route_with_current,
        collision_validator=collision_validator,
    )
    clearance_margins = _sampled_clearance_margins_m(
        route_with_current,
        collision_validator=collision_validator,
    )
    length_m = path_length(route_with_current)
    curvature_rad = _route_curvature_rad(route_with_current)
    vertical_jerk_m = _route_vertical_jerk_m(route_with_current)
    curvature_rad_per_m = curvature_rad / max(1e-9, length_m)
    vertical_jerk_m_per_m = vertical_jerk_m / max(1e-9, length_m)
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
        curvature_rad=curvature_rad,
        vertical_jerk_m=vertical_jerk_m,
        curvature_rad_per_m=curvature_rad_per_m,
        vertical_jerk_m_per_m=vertical_jerk_m_per_m,
        total_change_per_m=curvature_rad_per_m + vertical_jerk_m_per_m,
        length_m=length_m,
        point_count=len(route_with_current),
        first_clearance_failure=first_clearance_failure,
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
        if previous_cell is not None and cell != previous_cell:
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
    return None


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
