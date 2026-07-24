"""User-facing centerline Auto Dive route planning."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from typing import Any

import numpy as np

from caveviewer.core.navigation.centerline import (
    CENTERLINE_COMPONENT_SELECTION_LONGEST_PATH,
    CENTERLINE_ROUTE_WALL_CLEARANCE_MIN_CELLS,
    CENTERLINE_ROUTE_WALL_CLEARANCE_PUSH_FRACTION,
    DEFAULT_CENTERLINE_ROUTE_SPEED_M_PER_SECOND,
    DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS,
    CenterlinePath,
    FootprintCell,
    Point,
    PointXZ,
    footprint_path_length,
    footprint_neighbors,
    generate_centerline_path,
    push_route_points_toward_path_centers,
    route_points_for_xz_points,
    sample_footprint_route_points,
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


@dataclass(frozen=True)
class AutoDiveSettings:
    """Configuration for user-facing centerline Auto Dive planning."""

    render_distance_cells: int = DEFAULT_AUTO_DIVE_RENDER_DISTANCE_CELLS
    speed_m_per_second: float = DEFAULT_CENTERLINE_ROUTE_SPEED_M_PER_SECOND
    y_search_radius_cells: int = DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS
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

    route_xz = _sample_auto_dive_xz_points(
        centerline_path,
        route_cells=route_cells,
        settings=settings,
    )
    wall_clearance = push_route_points_toward_path_centers(
        route_xz,
        path_cells=route_cells,
        centers=centerline_path.centers,
        clearance_scores=centerline_path.clearance_scores,
        minimum_clearance_cells=CENTERLINE_ROUTE_WALL_CLEARANCE_MIN_CELLS,
        push_fraction=CENTERLINE_ROUTE_WALL_CLEARANCE_PUSH_FRACTION,
    )
    route_points = route_points_for_xz_points(
        wall_clearance.points,
        manifest=manifest,
        y_search_radius_cells=max(0, int(settings.y_search_radius_cells)),
    )
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


def _sample_auto_dive_xz_points(
    centerline_path: CenterlinePath,
    *,
    route_cells: tuple[FootprintCell, ...],
    settings: AutoDiveSettings,
) -> tuple[PointXZ, ...]:
    route_length = _footprint_cells_length(centerline_path, route_cells)
    spacing = settings.keyframe_spacing_m
    if spacing is None:
        spacing = max(centerline_path.footprint_cell_size * 4.0, 5.0)
    spacing = max(1e-6, float(spacing))
    keyframe_count = min(
        int(settings.max_keyframes),
        max(2, int(math.ceil(route_length / spacing)) + 1),
    )
    return sample_footprint_route_points(
        route_cells,
        centers=centerline_path.centers,
        start_distance_m=0.0,
        end_distance_m=route_length,
        keyframe_count=keyframe_count,
    )


def _footprint_cells_length(
    centerline_path: CenterlinePath,
    cells: tuple[FootprintCell, ...],
) -> float:
    if len(cells) < 2:
        return 0.0
    return footprint_path_length(cells, centerline_path.centers)


def route_progress_fraction(route: CameraRoute, elapsed_s: float) -> float:
    """Return bounded elapsed progress for UI display."""
    return max(0.0, min(1.0, float(elapsed_s) / max(1e-9, route.duration_s)))
