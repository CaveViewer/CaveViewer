"""Manifest-derived cave centerline planning primitives."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import heapq
import math
from typing import Any

from caveviewer.core.navigation.route import (
    NavigationConfigurationError,
    cumulative_distances,
    path_length,
)


Cell = tuple[int, int, int]
FootprintCell = tuple[int, int]
Point = tuple[float, float, float]
PointXZ = tuple[float, float]

DEFAULT_CENTERLINE_ROUTE_KEYFRAMES = 8
DEFAULT_CENTERLINE_ROUTE_CANDIDATE_LIMIT = 96
DEFAULT_CENTERLINE_ROUTE_ENDPOINT_PERCENTILE = 70.0
DEFAULT_CENTERLINE_ROUTE_SPEED_FEET_PER_MINUTE = 50.0
DEFAULT_CENTERLINE_ROUTE_SPEED_M_PER_SECOND = (
    DEFAULT_CENTERLINE_ROUTE_SPEED_FEET_PER_MINUTE * 0.3048 / 60.0
)
DEFAULT_CENTERLINE_ROUTE_MIN_CHUNKS = 0.5
DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS = 1
CENTERLINE_ROUTE_VERTICAL_POSITION_FRACTION = 0.50
CENTERLINE_ROUTE_WALL_CLEARANCE_MIN_CELLS = 2
CENTERLINE_ROUTE_WALL_CLEARANCE_PUSH_FRACTION = 0.85
CENTERLINE_ROUTE_SELECTION_MIDPOINT = "centerline_midpoint_v1"
CENTERLINE_COMPONENT_SELECTION_CLEAREST = "clearest_component_v1"
CENTERLINE_COMPONENT_SELECTION_LONGEST_PATH = "longest_path_v1"

_NEIGHBOR_OFFSETS_8 = tuple(
    (dx, dz)
    for dx in (-1, 0, 1)
    for dz in (-1, 0, 1)
    if (dx, dz) != (0, 0)
)


@dataclass(frozen=True)
class CenterlinePath:
    """Manifest-derived passage centerline independent of load scoring."""

    source: str
    footprint_cell_size: float
    footprint_cell_count: int
    component_size: int
    component_cells: frozenset[FootprintCell]
    cells: tuple[FootprintCell, ...]
    centers: Mapping[FootprintCell, PointXZ]
    clearance_scores: dict[FootprintCell, int]
    endpoint_percentile: float
    endpoint_threshold_clearance_cells: int
    length_m: float
    cached_points: Mapping[FootprintCell, Point] | None = None
    cached_y_ranges: Mapping[FootprintCell, tuple[float, float]] | None = None
    cached_clearance_margins: Mapping[FootprintCell, float] | None = None
    cached_recovery_hotspots: Mapping[FootprintCell, Mapping[str, float]] | None = None
    # Kept as ``Any`` so the replaceable centerline primitive does not import
    # the optional voxel implementation. Runtime navigation treats this as an
    # optional local voxel field or tiled atlas when the sidecar is available.
    cached_voxel_volume: Any | None = None
    cached_voxel_metrics: Mapping[str, Any] | None = None

    @property
    def points_xz(self) -> tuple[PointXZ, ...]:
        """Return centerline points in world X/Z coordinates."""
        return tuple(self.centers[cell] for cell in self.cells)


@dataclass(frozen=True)
class CenterlineSegment:
    """A selected segment of a manifest-derived centerline path."""

    cells: tuple[FootprintCell, ...]
    start_distance_m: float
    end_distance_m: float
    pivot_cell: FootprintCell
    selection_strategy: str


@dataclass(frozen=True)
class WallClearanceAdjustment:
    """Diagnostics for route points adjusted away from low-clearance cells."""

    points: tuple[PointXZ, ...]
    adjusted_count: int
    max_adjustment_m: float
    mean_adjustment_m: float


@dataclass(frozen=True)
class _Footprint:
    cells: frozenset[FootprintCell]
    cell_size: float
    source: str


@dataclass(frozen=True)
class _ChunkColumnSample:
    min_y: float
    max_y: float


def generate_centerline_path(
    manifest: Mapping[str, Any],
    *,
    candidate_limit: int = DEFAULT_CENTERLINE_ROUTE_CANDIDATE_LIMIT,
    endpoint_percentile: float = DEFAULT_CENTERLINE_ROUTE_ENDPOINT_PERCENTILE,
    component_selection: str = CENTERLINE_COMPONENT_SELECTION_CLEAREST,
) -> CenterlinePath:
    """Generate the cave passage centerline from footprint geometry only.

    This reusable navigation primitive depends on the manifest footprint and
    clearance field only. Texture files, materials, chunk density, and
    render-distance complexity are deliberately outside this function.
    """
    footprint = _parse_footprint(manifest)
    clearance_scores = clearance_scores_for_footprint(footprint.cells)
    component, full_path_cells, endpoint_threshold = _select_centerline_path(
        footprint.cells,
        clearance_scores,
        candidate_limit=max(2, int(candidate_limit)),
        endpoint_percentile=float(endpoint_percentile),
        component_selection=component_selection,
        cell_size=footprint.cell_size,
    )
    component_centers = {
        cell: footprint_world_center(cell, footprint.cell_size)
        for cell in component
    }
    full_path_length_m = footprint_path_length(full_path_cells, component_centers)
    return CenterlinePath(
        source=footprint.source,
        footprint_cell_size=footprint.cell_size,
        footprint_cell_count=len(footprint.cells),
        component_size=len(component),
        component_cells=frozenset(component),
        cells=tuple(full_path_cells),
        centers=component_centers,
        clearance_scores=clearance_scores,
        endpoint_percentile=float(endpoint_percentile),
        endpoint_threshold_clearance_cells=int(endpoint_threshold),
        length_m=full_path_length_m,
    )


def generate_centerline_paths(
    manifest: Mapping[str, Any],
    *,
    candidate_limit: int = DEFAULT_CENTERLINE_ROUTE_CANDIDATE_LIMIT,
    endpoint_percentile: float = DEFAULT_CENTERLINE_ROUTE_ENDPOINT_PERCENTILE,
) -> tuple[CenterlinePath, ...]:
    """Generate one centerline path per connected cave footprint component.

    Paths are sorted by descending path length. This function is intended for
    cache metadata generation, where preserving multiple cave components is
    useful and a later viewer can pick the best available route without
    recalculating the centerline from scratch.
    """
    footprint = _parse_footprint(manifest)
    clearance_scores = clearance_scores_for_footprint(footprint.cells)
    paths: list[CenterlinePath] = []
    for component in _centerline_components(footprint.cells):
        path_cells, endpoint_threshold = _centerline_component_path(
            component,
            clearance_scores,
            candidate_limit=max(2, int(candidate_limit)),
            endpoint_percentile=float(endpoint_percentile),
        )
        if len(path_cells) < 2:
            continue
        component_centers = {
            cell: footprint_world_center(cell, footprint.cell_size)
            for cell in component
        }
        path_length_m = footprint_path_length(path_cells, component_centers)
        paths.append(
            CenterlinePath(
                source=footprint.source,
                footprint_cell_size=footprint.cell_size,
                footprint_cell_count=len(footprint.cells),
                component_size=len(component),
                component_cells=frozenset(component),
                cells=tuple(path_cells),
                centers=component_centers,
                clearance_scores=clearance_scores,
                endpoint_percentile=float(endpoint_percentile),
                endpoint_threshold_clearance_cells=int(endpoint_threshold),
                length_m=path_length_m,
            )
        )
    return tuple(
        sorted(
            paths,
            key=lambda path: (
                path.length_m,
                len(path.cells),
                path.component_size,
            ),
            reverse=True,
        )
    )


def target_centerline_route_length_m(
    *,
    chunk_size: float,
    measurement_duration_s: float,
    target_length_m: float | None,
) -> tuple[float, str]:
    """Resolve a centerline route length from explicit meters or diver speed."""
    if target_length_m is not None:
        target = float(target_length_m)
        if not math.isfinite(target) or target <= 0.0:
            raise NavigationConfigurationError(
                "centerline target route length must be a positive number"
            )
        return target, "explicit_meters"
    duration = float(measurement_duration_s)
    if not math.isfinite(duration) or duration <= 0.0:
        raise NavigationConfigurationError(
            "centerline route duration must be a positive number"
        )
    speed_length = DEFAULT_CENTERLINE_ROUTE_SPEED_M_PER_SECOND * duration
    minimum_length = float(chunk_size) * DEFAULT_CENTERLINE_ROUTE_MIN_CHUNKS
    if minimum_length > speed_length:
        return minimum_length, "default_diver_speed_min_half_chunk"
    return speed_length, "default_diver_speed"


def select_centerline_path_segment(
    centerline_path: CenterlinePath,
    *,
    target_length_m: float,
    pivot_index: int,
    selection_strategy: str,
) -> CenterlineSegment:
    """Select a bounded segment around a centerline pivot index."""
    path_cells = centerline_path.cells
    distances = footprint_cumulative_distances(path_cells, centerline_path.centers)
    segment_cells, start_m, end_m, pivot_cell = select_footprint_path_segment(
        path_cells,
        centers=centerline_path.centers,
        distances=distances,
        target_length_m=target_length_m,
        pivot_index=pivot_index,
    )
    return CenterlineSegment(
        cells=segment_cells,
        start_distance_m=start_m,
        end_distance_m=end_m,
        pivot_cell=pivot_cell,
        selection_strategy=selection_strategy,
    )


def centerline_segment_midpoint_index(distances: list[float]) -> int:
    """Return the path-cell index nearest the centerline distance midpoint."""
    if not distances:
        raise NavigationConfigurationError("cannot select a pivot on an empty path")
    midpoint = distances[-1] / 2.0
    return min(
        range(len(distances)),
        key=lambda index: (
            abs(distances[index] - midpoint),
            index,
        ),
    )


def select_footprint_path_segment(
    path_cells: tuple[FootprintCell, ...],
    *,
    centers: Mapping[FootprintCell, PointXZ],
    distances: list[float],
    target_length_m: float,
    pivot_index: int,
) -> tuple[tuple[FootprintCell, ...], float, float, FootprintCell]:
    """Select a footprint path segment of roughly target length."""
    if len(path_cells) <= 2:
        pivot = path_cells[max(0, min(len(path_cells) - 1, pivot_index))]
        return path_cells, 0.0, distances[-1], pivot
    total = distances[-1]
    pivot_index = max(0, min(len(path_cells) - 1, pivot_index))
    pivot_cell = path_cells[pivot_index]
    if total <= target_length_m:
        return path_cells, 0.0, total, pivot_cell

    start_distance = max(0.0, distances[pivot_index] - target_length_m / 2.0)
    end_distance = start_distance + target_length_m
    if end_distance > total:
        end_distance = total
        start_distance = max(0.0, end_distance - target_length_m)

    start_index = 0
    for index, distance in enumerate(distances):
        if distance > start_distance:
            break
        start_index = index

    end_index = len(path_cells) - 1
    for index, distance in enumerate(distances):
        if distance >= end_distance:
            end_index = index
            break

    if end_index <= start_index:
        end_index = min(len(path_cells) - 1, start_index + 1)
    return (
        tuple(path_cells[start_index : end_index + 1]),
        start_distance,
        end_distance,
        pivot_cell,
    )


def sample_footprint_route_points(
    path_cells: tuple[FootprintCell, ...],
    *,
    centers: Mapping[FootprintCell, PointXZ],
    start_distance_m: float,
    end_distance_m: float,
    keyframe_count: int,
) -> tuple[PointXZ, ...]:
    """Sample X/Z points along a selected footprint path segment."""
    if not path_cells:
        raise NavigationConfigurationError("cannot sample an empty footprint path")
    distances = footprint_cumulative_distances(path_cells, centers)
    total = distances[-1]
    start = max(0.0, min(float(start_distance_m), total))
    end = max(start, min(float(end_distance_m), total))
    if keyframe_count <= 1 or len(path_cells) == 1 or end - start <= 1e-9:
        return (interpolated_footprint_point(path_cells, centers, distances, start),)

    return tuple(
        interpolated_footprint_point(
            path_cells,
            centers,
            distances,
            start + (end - start) * index / max(1, keyframe_count - 1),
        )
        for index in range(keyframe_count)
    )


def interpolated_footprint_point(
    path_cells: tuple[FootprintCell, ...],
    centers: Mapping[FootprintCell, PointXZ],
    distances: list[float],
    target_distance_m: float,
) -> PointXZ:
    """Return an interpolated X/Z point on a footprint path."""
    if target_distance_m <= 0.0:
        return centers[path_cells[0]]
    if target_distance_m >= distances[-1]:
        return centers[path_cells[-1]]

    for index in range(1, len(distances)):
        if distances[index] < target_distance_m:
            continue
        previous_distance = distances[index - 1]
        segment_length = distances[index] - previous_distance
        if segment_length <= 1e-9:
            return centers[path_cells[index]]
        ratio = (target_distance_m - previous_distance) / segment_length
        first = centers[path_cells[index - 1]]
        second = centers[path_cells[index]]
        return (
            first[0] + (second[0] - first[0]) * ratio,
            first[1] + (second[1] - first[1]) * ratio,
        )
    return centers[path_cells[-1]]


def push_route_points_toward_path_centers(
    points: tuple[PointXZ, ...],
    *,
    path_cells: tuple[FootprintCell, ...],
    centers: Mapping[FootprintCell, PointXZ],
    clearance_scores: Mapping[FootprintCell, int],
    minimum_clearance_cells: int,
    push_fraction: float,
) -> WallClearanceAdjustment:
    """Push low-clearance sampled X/Z points toward centerline cell centers."""
    if not points:
        return WallClearanceAdjustment((), 0, 0.0, 0.0)
    if not path_cells:
        raise NavigationConfigurationError(
            "cannot adjust route wall clearance against an empty footprint path"
        )
    fraction = max(0.0, min(1.0, float(push_fraction)))
    adjusted: list[PointXZ] = []
    adjustments: list[float] = []

    for point in points:
        nearest_cell = min(
            path_cells,
            key=lambda cell: (
                (centers[cell][0] - point[0]) ** 2
                + (centers[cell][1] - point[1]) ** 2,
                cell,
            ),
        )
        if clearance_scores[nearest_cell] >= minimum_clearance_cells:
            adjusted_point = point
        else:
            center = centers[nearest_cell]
            adjusted_point = (
                point[0] + (center[0] - point[0]) * fraction,
                point[1] + (center[1] - point[1]) * fraction,
            )
        adjustment = math.hypot(
            adjusted_point[0] - point[0],
            adjusted_point[1] - point[1],
        )
        adjustments.append(adjustment)
        adjusted.append(adjusted_point)

    return WallClearanceAdjustment(
        points=tuple(adjusted),
        adjusted_count=sum(1 for adjustment in adjustments if adjustment > 1e-6),
        max_adjustment_m=max(adjustments),
        mean_adjustment_m=sum(adjustments) / len(adjustments),
    )


def nearest_footprint_cells_for_points(
    points: tuple[PointXZ, ...],
    *,
    path_cells: tuple[FootprintCell, ...],
    centers: Mapping[FootprintCell, PointXZ],
) -> tuple[FootprintCell, ...]:
    """Map X/Z points to nearest cells on a known footprint path."""
    if not points:
        return ()
    if not path_cells:
        raise NavigationConfigurationError(
            "cannot match route points against an empty footprint path"
        )
    return tuple(
        min(
            path_cells,
            key=lambda cell: (
                (centers[cell][0] - point[0]) ** 2
                + (centers[cell][1] - point[1]) ** 2,
                cell,
            ),
        )
        for point in points
    )


def route_points_for_xz_points(
    route_xz_points: tuple[PointXZ, ...],
    *,
    manifest: Mapping[str, Any],
    y_search_radius_cells: int,
    vertical_position_fraction: float = CENTERLINE_ROUTE_VERTICAL_POSITION_FRACTION,
) -> tuple[Point, ...]:
    """Lift X/Z route points to 3D using local vertical center sampling."""
    columns = chunk_columns(manifest)
    points: list[Point] = []
    for x, z in route_xz_points:
        x, y, z = vertical_center_point_for_xz(
            columns,
            target_x=x,
            target_z=z,
            local_radius_cells=y_search_radius_cells,
            vertical_position_fraction=vertical_position_fraction,
        )
        points.append((x, y, z))
    return tuple(points)


def parse_chunk_centers(manifest: Mapping[str, Any]) -> dict[Cell, Point]:
    """Parse manifest chunks into world-space chunk centers."""
    chunks = manifest.get("chunks")
    if not isinstance(chunks, Mapping) or not chunks:
        raise NavigationConfigurationError("manifest contains no chunks")
    parsed: dict[Cell, Point] = {}
    for cell_key, info in chunks.items():
        cell = parse_cell_key(str(cell_key))
        if not isinstance(info, Mapping):
            raise NavigationConfigurationError(f"invalid chunk metadata for {cell_key}")
        try:
            bounds_min = tuple(float(value) for value in info["bounds_min"])
            bounds_max = tuple(float(value) for value in info["bounds_max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NavigationConfigurationError(
                f"chunk {cell_key} is missing valid bounds"
            ) from exc
        if len(bounds_min) != 3 or len(bounds_max) != 3:
            raise NavigationConfigurationError(f"chunk {cell_key} bounds must be 3D")
        parsed[cell] = tuple(
            (bounds_min[index] + bounds_max[index]) / 2.0
            for index in range(3)
        )
    return parsed


def chunk_columns(
    manifest: Mapping[str, Any],
) -> tuple[float, dict[FootprintCell, list[_ChunkColumnSample]]]:
    """Parse manifest chunks into X/Z columns with vertical bounds."""
    chunk_size = positive_manifest_float(manifest.get("chunk_size"), "chunk_size")
    chunks = manifest.get("chunks")
    if not isinstance(chunks, Mapping) or not chunks:
        raise NavigationConfigurationError("manifest contains no chunks")
    columns: dict[FootprintCell, list[_ChunkColumnSample]] = {}
    for cell_key, info in chunks.items():
        cell = parse_cell_key(str(cell_key))
        if not isinstance(info, Mapping):
            raise NavigationConfigurationError(f"invalid chunk metadata for {cell_key}")
        try:
            bounds_min = tuple(float(value) for value in info["bounds_min"])
            bounds_max = tuple(float(value) for value in info["bounds_max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NavigationConfigurationError(
                f"chunk {cell_key} is missing valid bounds"
            ) from exc
        if len(bounds_min) != 3 or len(bounds_max) != 3:
            raise NavigationConfigurationError(f"chunk {cell_key} bounds must be 3D")
        columns.setdefault((cell[0], cell[2]), []).append(
            _ChunkColumnSample(
                min_y=min(bounds_min[1], bounds_max[1]),
                max_y=max(bounds_min[1], bounds_max[1]),
            )
        )
    return chunk_size, columns


def vertical_center_point_for_xz(
    columns: tuple[float, Mapping[FootprintCell, list[_ChunkColumnSample]]],
    *,
    target_x: float,
    target_z: float,
    local_radius_cells: int,
    search_radius_cells: int = 12,
    vertical_position_fraction: float = CENTERLINE_ROUTE_VERTICAL_POSITION_FRACTION,
) -> Point:
    """Return a 3D point at the local vertical center near target X/Z."""
    chunk_size, column_values = columns
    target_cx = int(math.floor(target_x / chunk_size))
    target_cz = int(math.floor(target_z / chunk_size))

    exact_column = (target_cx, target_cz)
    if exact_column in column_values:
        y = vertical_center_y_for_local_columns(
            column_values,
            center=exact_column,
            radius=local_radius_cells,
            vertical_position_fraction=vertical_position_fraction,
        )
        return target_x, y, target_z

    for radius in range(1, search_radius_cells + 1):
        best_dist = math.inf
        best_col: FootprintCell | None = None
        for dx in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                if max(abs(dx), abs(dz)) != radius:
                    continue
                col = (target_cx + dx, target_cz + dz)
                if col not in column_values:
                    continue
                dist = dx * dx + dz * dz
                if dist < best_dist:
                    best_dist = dist
                    best_col = col
        if best_col is not None:
            y = vertical_center_y_for_local_columns(
                column_values,
                center=best_col,
                radius=local_radius_cells,
                vertical_position_fraction=vertical_position_fraction,
            )
            return (
                (best_col[0] + 0.5) * chunk_size,
                y,
                (best_col[1] + 0.5) * chunk_size,
            )

    closest_col = min(
        column_values,
        key=lambda col: (
            (col[0] - target_cx) ** 2 + (col[1] - target_cz) ** 2,
            col,
        ),
    )
    y = vertical_center_y_for_local_columns(
        column_values,
        center=closest_col,
        radius=local_radius_cells,
        vertical_position_fraction=vertical_position_fraction,
    )
    return (
        (closest_col[0] + 0.5) * chunk_size,
        y,
        (closest_col[1] + 0.5) * chunk_size,
    )


def vertical_center_y_for_local_columns(
    column_values: Mapping[FootprintCell, list[_ChunkColumnSample]],
    *,
    center: FootprintCell,
    radius: int,
    vertical_position_fraction: float = CENTERLINE_ROUTE_VERTICAL_POSITION_FRACTION,
) -> float:
    """Return local vertical center Y around a chunk column."""
    samples = [
        sample
        for dx in range(-radius, radius + 1)
        for dz in range(-radius, radius + 1)
        for sample in column_values.get((center[0] + dx, center[1] + dz), ())
    ]
    if not samples:
        samples = list(column_values[center])
    min_y = min(sample.min_y for sample in samples)
    max_y = max(sample.max_y for sample in samples)
    fraction = min(1.0, max(0.0, float(vertical_position_fraction)))
    return min_y + (max_y - min_y) * fraction


def footprint_path_length(
    path_cells: tuple[FootprintCell, ...],
    centers: Mapping[FootprintCell, PointXZ],
) -> float:
    return footprint_cumulative_distances(path_cells, centers)[-1]


def footprint_cumulative_distances(
    path_cells: tuple[FootprintCell, ...],
    centers: Mapping[FootprintCell, PointXZ],
) -> list[float]:
    points = tuple((centers[cell][0], 0.0, centers[cell][1]) for cell in path_cells)
    return cumulative_distances(points)


def positive_manifest_float(value: object, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise NavigationConfigurationError(
            f"manifest {field_name} must be a positive number"
        ) from exc
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise NavigationConfigurationError(
            f"manifest {field_name} must be a positive number"
        )
    return parsed


def parse_cell_key(value: str) -> Cell:
    parts = value.split("_")
    if len(parts) != 3:
        raise NavigationConfigurationError(f"invalid chunk cell key: {value!r}")
    try:
        return tuple(int(part) for part in parts)  # type: ignore[return-value]
    except ValueError as exc:
        raise NavigationConfigurationError(f"invalid chunk cell key: {value!r}") from exc


def chunk_cell_for_point(point: Point, chunk_size: float) -> Cell:
    return tuple(
        int(math.floor(coordinate / chunk_size))
        for coordinate in point
    )  # type: ignore[return-value]


def cell_distance(first: Cell, second: Cell) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def footprint_world_center(cell: FootprintCell, cell_size: float) -> PointXZ:
    return (
        (cell[0] + 0.5) * cell_size,
        (cell[1] + 0.5) * cell_size,
    )


def footprint_cell_distance(first: FootprintCell, second: FootprintCell) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def neighbors(cell: Cell) -> list[Cell]:
    return [
        (cell[0] + dx, cell[1] + dy, cell[2] + dz)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
        if (dx, dy, dz) != (0, 0, 0)
    ]


def clearance_scores_for_footprint(
    cells: frozenset[FootprintCell],
) -> dict[FootprintCell, int]:
    if not cells:
        raise NavigationConfigurationError("cannot score an empty footprint")
    boundary = {
        cell
        for cell in cells
        if any(neighbor not in cells for neighbor in footprint_neighbors(cell))
    }
    if not boundary:
        boundary = set(cells)
    scores = {cell: 1 for cell in boundary}
    queue = deque(sorted(boundary))
    while queue:
        current = queue.popleft()
        next_score = scores[current] + 1
        for neighbor in footprint_neighbors(current):
            if neighbor not in cells or neighbor in scores:
                continue
            scores[neighbor] = next_score
            queue.append(neighbor)
    return scores


def footprint_neighbors(cell: FootprintCell) -> list[FootprintCell]:
    return [
        (cell[0] + dx, cell[1] + dz)
        for dx, dz in _NEIGHBOR_OFFSETS_8
    ]


def footprint_path_is_circular(cells: tuple[FootprintCell, ...]) -> bool:
    """Return whether a footprint path closes back on itself."""
    if len(cells) < 4:
        return False
    if cells[0] == cells[-1]:
        return True
    return cells[0] in footprint_neighbors(cells[-1])


def navigable_footprint_neighbors(
    cell: FootprintCell,
    cells: set[FootprintCell] | frozenset[FootprintCell],
) -> list[FootprintCell]:
    """Return footprint neighbors without cutting diagonally through wall corners."""
    navigable = []
    for neighbor in footprint_neighbors(cell):
        if neighbor not in cells:
            continue
        dx = neighbor[0] - cell[0]
        dz = neighbor[1] - cell[1]
        if abs(dx) == 1 and abs(dz) == 1:
            if (cell[0] + dx, cell[1]) not in cells:
                continue
            if (cell[0], cell[1] + dz) not in cells:
                continue
        navigable.append(neighbor)
    return navigable


def lowest_cost_footprint_path(
    component: set[FootprintCell] | frozenset[FootprintCell],
    start: FootprintCell,
    end: FootprintCell,
    clearance_scores: Mapping[FootprintCell, int],
) -> tuple[FootprintCell, ...]:
    """Return a wall-safe low-cost path through occupied footprint cells."""
    return _lowest_cost_centerline_path(set(component), start, end, clearance_scores)


def _parse_footprint(manifest: Mapping[str, Any]) -> _Footprint:
    if "footprint_cells" in manifest and "footprint_cell_size" in manifest:
        return _parse_vertex_footprint(manifest)
    return _parse_chunk_column_footprint(manifest)


def _parse_vertex_footprint(manifest: Mapping[str, Any]) -> _Footprint:
    cell_size = positive_manifest_float(
        manifest.get("footprint_cell_size"),
        "footprint_cell_size",
    )
    flat = manifest.get("footprint_cells")
    if not isinstance(flat, list) or len(flat) < 2:
        raise NavigationConfigurationError("manifest contains no footprint cells")
    if len(flat) % 2 != 0:
        raise NavigationConfigurationError("manifest footprint cells must be x/z pairs")
    cells: set[FootprintCell] = set()
    for index in range(0, len(flat), 2):
        try:
            cells.add((int(flat[index]), int(flat[index + 1])))
        except (TypeError, ValueError) as exc:
            raise NavigationConfigurationError(
                "manifest footprint cells must contain integer x/z pairs"
            ) from exc
    if not cells:
        raise NavigationConfigurationError("manifest contains no footprint cells")
    return _Footprint(
        cells=frozenset(cells),
        cell_size=cell_size,
        source="vertex_footprint_manifest",
    )


def _parse_chunk_column_footprint(manifest: Mapping[str, Any]) -> _Footprint:
    cell_size = positive_manifest_float(manifest.get("chunk_size"), "chunk_size")
    chunks = parse_chunk_centers(manifest)
    cells = frozenset((cell[0], cell[2]) for cell in chunks)
    if not cells:
        raise NavigationConfigurationError("manifest contains no occupied columns")
    return _Footprint(
        cells=cells,
        cell_size=cell_size,
        source="chunk_manifest_columns",
    )


def _select_centerline_component(
    cells: frozenset[FootprintCell],
    clearance_scores: Mapping[FootprintCell, int],
) -> set[FootprintCell]:
    components = _centerline_components(cells)
    if not components:
        raise NavigationConfigurationError("could not select a centerline component")
    return max(
        components,
        key=lambda component: (
            max(clearance_scores[cell] for cell in component),
            sum(clearance_scores[cell] for cell in component),
            len(component),
        ),
    )


def _centerline_components(cells: frozenset[FootprintCell]) -> list[set[FootprintCell]]:
    components: list[set[FootprintCell]] = []
    seen: set[FootprintCell] = set()
    for cell in sorted(cells):
        if cell in seen:
            continue
        component: set[FootprintCell] = set()
        queue = deque([cell])
        seen.add(cell)
        while queue:
            current = queue.popleft()
            component.add(current)
            for neighbor in navigable_footprint_neighbors(current, cells):
                if neighbor in cells and neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        components.append(component)
    return components


def _select_centerline_path(
    cells: frozenset[FootprintCell],
    clearance_scores: Mapping[FootprintCell, int],
    *,
    candidate_limit: int,
    endpoint_percentile: float,
    component_selection: str,
    cell_size: float,
) -> tuple[set[FootprintCell], tuple[FootprintCell, ...], int]:
    normalized_selection = str(component_selection).strip().lower()
    if normalized_selection == CENTERLINE_COMPONENT_SELECTION_CLEAREST:
        component = _select_centerline_component(cells, clearance_scores)
        path_cells, threshold = _centerline_component_path(
            component,
            clearance_scores,
            candidate_limit=candidate_limit,
            endpoint_percentile=endpoint_percentile,
        )
        return component, path_cells, threshold
    if normalized_selection != CENTERLINE_COMPONENT_SELECTION_LONGEST_PATH:
        raise NavigationConfigurationError(
            f"unsupported centerline component selection: {component_selection}"
        )

    candidates = []
    for component in _centerline_components(cells):
        path_cells, threshold = _centerline_component_path(
            component,
            clearance_scores,
            candidate_limit=candidate_limit,
            endpoint_percentile=endpoint_percentile,
        )
        centers = {
            cell: footprint_world_center(cell, cell_size)
            for cell in path_cells
        }
        candidates.append(
            (
                footprint_path_length(path_cells, centers),
                len(path_cells),
                len(component),
                component,
                path_cells,
                threshold,
            )
        )
    if not candidates:
        raise NavigationConfigurationError("could not select a centerline component")
    _, _, _, component, path_cells, threshold = max(
        candidates,
        key=lambda item: (item[0], item[1], item[2]),
    )
    return component, path_cells, threshold


def _centerline_component_path(
    component: set[FootprintCell],
    clearance_scores: Mapping[FootprintCell, int],
    *,
    candidate_limit: int,
    endpoint_percentile: float,
) -> tuple[tuple[FootprintCell, ...], int]:
    if len(component) == 1:
        cell = next(iter(component))
        return (cell,), clearance_scores[cell]
    threshold = _clearance_threshold(
        (clearance_scores[cell] for cell in component),
        percentile=endpoint_percentile,
    )
    candidates = [
        cell
        for cell in sorted(component, key=lambda cell: (-clearance_scores[cell], cell))
        if clearance_scores[cell] >= threshold
    ][: min(candidate_limit, len(component))]
    if len(candidates) < 2:
        candidates = sorted(
            component,
            key=lambda cell: (-clearance_scores[cell], cell),
        )[: min(2, len(component))]
    _, start, end = max(
        (
            (footprint_cell_distance(first, second), first, second)
            for index, first in enumerate(candidates)
            for second in candidates[index + 1 :]
        ),
        key=lambda item: (
            item[0],
            clearance_scores[item[1]] + clearance_scores[item[2]],
        ),
    )
    return (
        _lowest_cost_centerline_path(component, start, end, clearance_scores),
        int(threshold),
    )


def _clearance_threshold(
    values_iterable: Iterable[int],
    *,
    percentile: float,
) -> int:
    values = sorted(int(value) for value in values_iterable)
    if not values:
        raise NavigationConfigurationError(
            "cannot compute an empty clearance threshold"
        )
    pct = max(0.0, min(100.0, float(percentile)))
    index = min(len(values) - 1, max(0, int((pct / 100.0) * len(values))))
    return int(values[index])


def _lowest_cost_centerline_path(
    component: set[FootprintCell],
    start: FootprintCell,
    end: FootprintCell,
    clearance_scores: Mapping[FootprintCell, int],
) -> tuple[FootprintCell, ...]:
    max_clearance = max(clearance_scores[cell] for cell in component)
    frontier: list[tuple[float, FootprintCell]] = [(0.0, start)]
    previous: dict[FootprintCell, FootprintCell | None] = {start: None}
    costs: dict[FootprintCell, float] = {start: 0.0}

    while frontier:
        current_cost, current = heapq.heappop(frontier)
        if current == end:
            break
        if current_cost > costs[current]:
            continue
        for neighbor in navigable_footprint_neighbors(current, component):
            if neighbor not in component:
                continue
            step_distance = footprint_cell_distance(current, neighbor)
            clearance_penalty = (
                (max_clearance - clearance_scores[neighbor])
                / max(1, max_clearance)
            )
            next_cost = current_cost + step_distance * (1.0 + clearance_penalty)
            if next_cost >= costs.get(neighbor, math.inf):
                continue
            costs[neighbor] = next_cost
            previous[neighbor] = current
            heapq.heappush(frontier, (next_cost, neighbor))

    if end not in previous:
        return ()
    path: list[FootprintCell] = []
    current: FootprintCell | None = end
    while current is not None:
        path.append(current)
        current = previous[current]
    return tuple(reversed(path))
