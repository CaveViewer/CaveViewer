"""User-facing centerline Auto Dive route planning."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from typing import Any

import numpy as np

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


def build_centerline_auto_dive_plan(
    manifest: Mapping[str, Any],
    *,
    current_position: tuple[float, float, float] | np.ndarray,
    settings: AutoDiveSettings | None = None,
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
    route_xz = _auto_dive_xz_points_for_cells(
        centerline_path,
        route_cells=route_cells,
        settings=settings,
    )
    route_points = route_points_for_xz_points(
        route_xz,
        manifest=manifest,
        y_search_radius_cells=max(0, int(settings.y_search_radius_cells)),
        vertical_position_fraction=float(settings.vertical_position_fraction),
    )
    route_points = _route_points_starting_at_current_camera(route_points, current)
    length_m = path_length(route_points)
    if length_m <= 1e-6:
        raise NavigationConfigurationError("Auto Dive route has no travel distance")
    duration_s = length_m / float(settings.speed_m_per_second)
    keyframe_payloads = route_keyframes_for_points(
        route_points,
        duration_s=duration_s,
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

    forward = tuple(cells[nearest_index:])
    backward = tuple(reversed(cells[: nearest_index + 1]))
    forward_length = _footprint_cells_length(centerline_path, forward)
    backward_length = _footprint_cells_length(centerline_path, backward)
    if forward_length >= backward_length:
        return forward, False
    return backward, False


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
