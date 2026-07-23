"""Manifest-derived benchmark route generation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import heapq
import math
from typing import Any, Iterable, Mapping

from caveviewer.gui.benchmark import (
    BenchmarkConfigurationError,
    BenchmarkScenario,
    SCENARIO_VERSION,
)


Cell = tuple[int, int, int]
FootprintCell = tuple[int, int]
Point = tuple[float, float, float]
PointXZ = tuple[float, float]

DEFAULT_CENTERLINE_ROUTE_KEYFRAMES = 8
DEFAULT_CENTERLINE_ROUTE_CANDIDATE_LIMIT = 96
DEFAULT_CENTERLINE_ROUTE_ENDPOINT_PERCENTILE = 70.0
DEFAULT_CENTERLINE_ROUTE_TARGET_CELLS = 24
DEFAULT_CENTERLINE_ROUTE_Y_BIAS = 0.65
DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS = 1
DEFAULT_DENSE_ROUTE_KEYFRAMES = 8
DEFAULT_DENSE_ROUTE_PERCENTILE = 90.0
DEFAULT_DENSE_ROUTE_CANDIDATE_LIMIT = 64
_NEIGHBOR_OFFSETS_26 = tuple(
    (dx, dy, dz)
    for dx in (-1, 0, 1)
    for dy in (-1, 0, 1)
    for dz in (-1, 0, 1)
    if (dx, dy, dz) != (0, 0, 0)
)
_NEIGHBOR_OFFSETS_8 = tuple(
    (dx, dz)
    for dx in (-1, 0, 1)
    for dz in (-1, 0, 1)
    if (dx, dz) != (0, 0)
)


@dataclass(frozen=True)
class DenseRoute:
    """Generated dense-route payload and diagnostics."""

    scenario_payload: dict[str, Any]
    path_cells: tuple[Cell, ...]
    route_cells: tuple[Cell, ...]
    density_scores: dict[Cell, int]


@dataclass(frozen=True)
class CenterlineRoute:
    """Generated centerline-route payload and diagnostics."""

    scenario_payload: dict[str, Any]
    path_cells: tuple[FootprintCell, ...]
    route_cells: tuple[FootprintCell, ...]
    clearance_scores: dict[FootprintCell, int]


def generate_centerline_route_scenario(
    manifest: Mapping[str, Any],
    template: BenchmarkScenario,
    *,
    name: str = "devils-eye-xl-centerline-route-v1",
    keyframe_count: int = DEFAULT_CENTERLINE_ROUTE_KEYFRAMES,
    candidate_limit: int = DEFAULT_CENTERLINE_ROUTE_CANDIDATE_LIMIT,
    endpoint_percentile: float = DEFAULT_CENTERLINE_ROUTE_ENDPOINT_PERCENTILE,
    target_length_m: float | None = None,
    y_bias: float = DEFAULT_CENTERLINE_ROUTE_Y_BIAS,
    y_search_radius_cells: int = DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS,
) -> CenterlineRoute:
    """Generate a deterministic virtual route through the passage center.

    The preferred input is the manifest's fine-grained ``footprint_cells`` field,
    which the chunker derives from source vertex positions. The route follows a
    high-clearance path through that footprint, where clearance is grid distance
    from the footprint boundary. Y is estimated from the nearest occupied chunk
    column so the route stays near actual cave geometry.

    This is still a benchmark path, not a collision-checked navigation mesh.
    """
    footprint = _parse_footprint(manifest)
    clearance_scores = _clearance_scores(footprint.cells)
    component = _select_centerline_component(footprint.cells, clearance_scores)
    full_path_cells, endpoint_threshold = _centerline_component_path(
        component,
        clearance_scores,
        candidate_limit=max(2, int(candidate_limit)),
        endpoint_percentile=float(endpoint_percentile),
    )
    full_path_centers = {
        cell: _footprint_world_center(cell, footprint.cell_size)
        for cell in full_path_cells
    }
    full_path_length_m = _footprint_path_length(full_path_cells, full_path_centers)
    resolved_target_length_m = _target_centerline_route_length_m(
        footprint.cell_size,
        keyframe_count=int(keyframe_count),
        target_length_m=target_length_m,
    )
    path_cells = _limit_footprint_path_length(
        full_path_cells,
        centers=full_path_centers,
        clearance_scores=clearance_scores,
        target_length_m=resolved_target_length_m,
    )
    route_cells = _sample_footprint_route_cells(
        path_cells,
        centers={
            cell: _footprint_world_center(cell, footprint.cell_size)
            for cell in path_cells
        },
        keyframe_count=max(1, int(keyframe_count)),
    )
    route_points = _route_points_for_footprint_cells(
        route_cells,
        manifest=manifest,
        footprint_cell_size=footprint.cell_size,
        y_bias=float(y_bias),
        y_search_radius_cells=max(0, int(y_search_radius_cells)),
    )
    route_scores = [clearance_scores[cell] for cell in route_cells]
    path_scores = [clearance_scores[cell] for cell in path_cells]
    route_length_m = _path_length(route_points)
    route = _keyframes_for_points(
        route_points,
        duration_s=template.total_duration_seconds,
    )
    payload = {
        "version": SCENARIO_VERSION,
        "name": name,
        "position_mode": "absolute",
        "warmup_seconds": template.warmup_seconds,
        "measurement_seconds": template.measurement_seconds,
        "max_runtime_seconds": template.max_runtime_seconds,
        "window_size": list(template.window_size),
        "render_distance": template.render_distance,
        "sample_every_n_frames": template.sample_every_n_frames,
        "stutter_thresholds_ms": list(template.stutter_thresholds_ms),
        "metadata": {
            **dict(template.metadata),
            "route_mode": "auto_centerline_v1",
            "route_source": footprint.source,
            "centerline_definition": (
                "representative high-clearance segment through the "
                "vertex-derived cave footprint"
            ),
            "clearance_definition": (
                "grid distance from the footprint boundary in footprint cells"
            ),
            "y_strategy": "local_vertical_center_v1",
            "y_bias": float(y_bias),
            "y_search_radius_cells": max(0, int(y_search_radius_cells)),
            "footprint_cell_size_m": round(footprint.cell_size, 3),
            "footprint_cell_count": len(footprint.cells),
            "footprint_component_size": len(component),
            "endpoint_clearance_percentile": float(endpoint_percentile),
            "endpoint_threshold_clearance_cells": int(endpoint_threshold),
            "target_route_length_m": round(resolved_target_length_m, 3),
            "full_path_cell_count": len(full_path_cells),
            "full_path_length_m": round(full_path_length_m, 3),
            "path_cell_count": len(path_cells),
            "route_keyframe_count": len(route),
            "route_length_m": round(route_length_m, 3),
            "min_route_y": round(min(point[1] for point in route_points), 3),
            "max_route_y": round(max(point[1] for point in route_points), 3),
            "max_clearance_cells": max(clearance_scores.values()),
            "max_clearance_m": round(
                max(clearance_scores.values()) * footprint.cell_size,
                3,
            ),
            "max_route_clearance_cells": max(route_scores),
            "min_route_clearance_cells": min(route_scores),
            "mean_route_clearance_cells": round(
                sum(route_scores) / len(route_scores),
                3,
            ),
            "mean_route_clearance_m": round(
                (sum(route_scores) / len(route_scores)) * footprint.cell_size,
                3,
            ),
            "mean_path_clearance_cells": round(
                sum(path_scores) / len(path_scores),
                3,
            ),
        },
        "route": route,
    }
    BenchmarkScenario.from_mapping(payload)
    return CenterlineRoute(
        scenario_payload=payload,
        path_cells=tuple(path_cells),
        route_cells=tuple(route_cells),
        clearance_scores=clearance_scores,
    )


def generate_dense_chunk_route_scenario(
    manifest: Mapping[str, Any],
    template: BenchmarkScenario,
    *,
    name: str = "devils-eye-xl-dense-route-v1",
    dense_percentile: float = DEFAULT_DENSE_ROUTE_PERCENTILE,
    keyframe_count: int = DEFAULT_DENSE_ROUTE_KEYFRAMES,
    candidate_limit: int = DEFAULT_DENSE_ROUTE_CANDIDATE_LIMIT,
) -> DenseRoute:
    """Generate a deterministic route through dense occupied chunk regions.

    Density is the number of occupied chunk cells inside the same cube radius
    used by runtime streaming for the benchmark render distance. This is a
    load proxy, not a navigation-mesh path.
    """
    chunks = _parse_chunks(manifest)
    cells = tuple(chunks)
    density_scores = _density_scores(cells, radius=template.render_distance)
    dense_cells, threshold = _dense_cell_set(
        density_scores,
        percentile=dense_percentile,
        min_cells=max(1, min(int(keyframe_count), len(cells))),
    )
    component = _select_dense_component(dense_cells, density_scores)
    path_cells = _dense_component_path(
        component,
        density_scores,
        candidate_limit=max(2, int(candidate_limit)),
    )
    route_cells = _sample_route_cells(
        path_cells,
        centers=chunks,
        keyframe_count=max(1, int(keyframe_count)),
    )
    route_points = tuple(chunks[cell] for cell in route_cells)
    route_scores = [density_scores[cell] for cell in route_cells]
    path_scores = [density_scores[cell] for cell in path_cells]
    route_length_m = _path_length(route_points)
    route = _keyframes_for_points(
        route_points,
        duration_s=template.total_duration_seconds,
    )
    payload = {
        "version": SCENARIO_VERSION,
        "name": name,
        "position_mode": "absolute",
        "warmup_seconds": template.warmup_seconds,
        "measurement_seconds": template.measurement_seconds,
        "max_runtime_seconds": template.max_runtime_seconds,
        "window_size": list(template.window_size),
        "render_distance": template.render_distance,
        "sample_every_n_frames": template.sample_every_n_frames,
        "stutter_thresholds_ms": list(template.stutter_thresholds_ms),
        "metadata": {
            **dict(template.metadata),
            "route_mode": "auto_dense_chunks_v1",
            "route_source": "chunk_manifest",
            "density_definition": (
                "occupied chunks within the scenario render_distance cube"
            ),
            "dense_percentile": float(dense_percentile),
            "dense_threshold_chunks": int(threshold),
            "occupied_chunk_count": len(cells),
            "dense_component_size": len(component),
            "path_cell_count": len(path_cells),
            "route_keyframe_count": len(route),
            "route_length_m": round(route_length_m, 3),
            "max_neighborhood_chunks": max(density_scores.values()),
            "max_route_neighborhood_chunks": max(route_scores),
            "min_route_neighborhood_chunks": min(route_scores),
            "mean_route_neighborhood_chunks": round(
                sum(route_scores) / len(route_scores),
                3,
            ),
            "mean_path_neighborhood_chunks": round(
                sum(path_scores) / len(path_scores),
                3,
            ),
        },
        "route": route,
    }
    BenchmarkScenario.from_mapping(payload)
    return DenseRoute(
        scenario_payload=payload,
        path_cells=tuple(path_cells),
        route_cells=tuple(route_cells),
        density_scores=density_scores,
    )


@dataclass(frozen=True)
class _Footprint:
    cells: frozenset[FootprintCell]
    cell_size: float
    source: str


@dataclass(frozen=True)
class _ChunkColumnSample:
    min_y: float
    max_y: float


def _parse_footprint(manifest: Mapping[str, Any]) -> _Footprint:
    if "footprint_cells" in manifest and "footprint_cell_size" in manifest:
        return _parse_vertex_footprint(manifest)
    return _parse_chunk_column_footprint(manifest)


def _parse_vertex_footprint(manifest: Mapping[str, Any]) -> _Footprint:
    cell_size = _positive_float(
        manifest.get("footprint_cell_size"),
        "footprint_cell_size",
    )
    flat = manifest.get("footprint_cells")
    if not isinstance(flat, list) or len(flat) < 2:
        raise BenchmarkConfigurationError("manifest contains no footprint cells")
    if len(flat) % 2 != 0:
        raise BenchmarkConfigurationError("manifest footprint cells must be x/z pairs")
    cells: set[FootprintCell] = set()
    for index in range(0, len(flat), 2):
        try:
            cells.add((int(flat[index]), int(flat[index + 1])))
        except (TypeError, ValueError) as exc:
            raise BenchmarkConfigurationError(
                "manifest footprint cells must contain integer x/z pairs"
            ) from exc
    if not cells:
        raise BenchmarkConfigurationError("manifest contains no footprint cells")
    return _Footprint(
        cells=frozenset(cells),
        cell_size=cell_size,
        source="vertex_footprint_manifest",
    )


def _parse_chunk_column_footprint(manifest: Mapping[str, Any]) -> _Footprint:
    cell_size = _positive_float(manifest.get("chunk_size"), "chunk_size")
    chunks = _parse_chunks(manifest)
    cells = frozenset((cell[0], cell[2]) for cell in chunks)
    if not cells:
        raise BenchmarkConfigurationError("manifest contains no occupied columns")
    return _Footprint(
        cells=cells,
        cell_size=cell_size,
        source="chunk_manifest_columns",
    )


def _positive_float(value: object, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BenchmarkConfigurationError(
            f"manifest {field_name} must be a positive number"
        ) from exc
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise BenchmarkConfigurationError(
            f"manifest {field_name} must be a positive number"
        )
    return parsed


def _parse_chunks(manifest: Mapping[str, Any]) -> dict[Cell, Point]:
    chunks = manifest.get("chunks")
    if not isinstance(chunks, Mapping) or not chunks:
        raise BenchmarkConfigurationError("manifest contains no chunks")
    parsed: dict[Cell, Point] = {}
    for cell_key, info in chunks.items():
        cell = _parse_cell_key(str(cell_key))
        if not isinstance(info, Mapping):
            raise BenchmarkConfigurationError(f"invalid chunk metadata for {cell_key}")
        try:
            bounds_min = tuple(float(value) for value in info["bounds_min"])
            bounds_max = tuple(float(value) for value in info["bounds_max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkConfigurationError(
                f"chunk {cell_key} is missing valid bounds"
            ) from exc
        if len(bounds_min) != 3 or len(bounds_max) != 3:
            raise BenchmarkConfigurationError(f"chunk {cell_key} bounds must be 3D")
        parsed[cell] = tuple(
            (bounds_min[index] + bounds_max[index]) / 2.0
            for index in range(3)
        )
    return parsed


def _parse_cell_key(value: str) -> Cell:
    parts = value.split("_")
    if len(parts) != 3:
        raise BenchmarkConfigurationError(f"invalid chunk cell key: {value!r}")
    try:
        return tuple(int(part) for part in parts)  # type: ignore[return-value]
    except ValueError as exc:
        raise BenchmarkConfigurationError(f"invalid chunk cell key: {value!r}") from exc


def _density_scores(cells: tuple[Cell, ...], *, radius: int) -> dict[Cell, int]:
    available = set(cells)
    scores: dict[Cell, int] = {}
    for cell in cells:
        cx, cy, cz = cell
        scores[cell] = sum(
            (cx + dx, cy + dy, cz + dz) in available
            for dx in range(-radius, radius + 1)
            for dy in range(-radius, radius + 1)
            for dz in range(-radius, radius + 1)
        )
    return scores


def _dense_cell_set(
    density_scores: Mapping[Cell, int],
    *,
    percentile: float,
    min_cells: int,
) -> tuple[set[Cell], int]:
    values = sorted(density_scores.values())
    pct = max(0.0, min(100.0, float(percentile)))
    index = min(len(values) - 1, max(0, int((pct / 100.0) * len(values))))
    threshold = values[index]
    dense_cells = {cell for cell, score in density_scores.items() if score >= threshold}
    if len(dense_cells) < min_cells:
        sorted_cells = sorted(
            density_scores,
            key=lambda cell: (-density_scores[cell], cell),
        )
        dense_cells = set(sorted_cells[:min_cells])
        threshold = min(density_scores[cell] for cell in dense_cells)
    return dense_cells, int(threshold)


def _clearance_scores(cells: frozenset[FootprintCell]) -> dict[FootprintCell, int]:
    if not cells:
        raise BenchmarkConfigurationError("cannot score an empty footprint")
    boundary = {
        cell
        for cell in cells
        if any(neighbor not in cells for neighbor in _footprint_neighbors(cell))
    }
    if not boundary:
        boundary = set(cells)
    scores = {cell: 1 for cell in boundary}
    queue = deque(sorted(boundary))
    while queue:
        current = queue.popleft()
        next_score = scores[current] + 1
        for neighbor in _footprint_neighbors(current):
            if neighbor not in cells or neighbor in scores:
                continue
            scores[neighbor] = next_score
            queue.append(neighbor)
    return scores


def _select_centerline_component(
    cells: frozenset[FootprintCell],
    clearance_scores: Mapping[FootprintCell, int],
) -> set[FootprintCell]:
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
            for neighbor in _footprint_neighbors(current):
                if neighbor in cells and neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        components.append(component)
    if not components:
        raise BenchmarkConfigurationError("could not select a centerline component")
    return max(
        components,
        key=lambda component: (
            max(clearance_scores[cell] for cell in component),
            sum(clearance_scores[cell] for cell in component),
            len(component),
        ),
    )


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
            (_footprint_cell_distance(first, second), first, second)
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
        raise BenchmarkConfigurationError("cannot compute an empty clearance threshold")
    pct = max(0.0, min(100.0, float(percentile)))
    index = min(len(values) - 1, max(0, int((pct / 100.0) * len(values))))
    return int(values[index])


def _target_centerline_route_length_m(
    footprint_cell_size: float,
    *,
    keyframe_count: int,
    target_length_m: float | None,
) -> float:
    if target_length_m is not None:
        target = float(target_length_m)
        if not math.isfinite(target) or target <= 0.0:
            raise BenchmarkConfigurationError(
                "centerline target route length must be a positive number"
            )
        return target
    target_cells = max(
        DEFAULT_CENTERLINE_ROUTE_TARGET_CELLS,
        max(1, int(keyframe_count)) * 3,
    )
    return footprint_cell_size * target_cells


def _limit_footprint_path_length(
    path_cells: tuple[FootprintCell, ...],
    *,
    centers: Mapping[FootprintCell, PointXZ],
    clearance_scores: Mapping[FootprintCell, int],
    target_length_m: float,
) -> tuple[FootprintCell, ...]:
    if len(path_cells) <= 2:
        return path_cells
    distances = _footprint_cumulative_distances(path_cells, centers)
    total = distances[-1]
    if total <= target_length_m:
        return path_cells

    midpoint = total / 2.0
    pivot_index = max(
        range(len(path_cells)),
        key=lambda index: (
            clearance_scores[path_cells[index]],
            -abs(distances[index] - midpoint),
            -index,
        ),
    )
    start_distance = max(0.0, distances[pivot_index] - target_length_m / 2.0)
    end_distance = start_distance + target_length_m
    if end_distance > total:
        end_distance = total
        start_distance = max(0.0, end_distance - target_length_m)

    start_index = min(
        range(len(distances)),
        key=lambda index: (abs(distances[index] - start_distance), index),
    )
    end_index = min(
        range(len(distances)),
        key=lambda index: (abs(distances[index] - end_distance), -index),
    )
    if end_index <= start_index:
        end_index = min(len(path_cells) - 1, start_index + 1)
    return tuple(path_cells[start_index : end_index + 1])


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
        for neighbor in _footprint_neighbors(current):
            if neighbor not in component:
                continue
            step_distance = _footprint_cell_distance(current, neighbor)
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
        return (start, end)
    path: list[FootprintCell] = []
    current: FootprintCell | None = end
    while current is not None:
        path.append(current)
        current = previous[current]
    return tuple(reversed(path))


def _select_dense_component(
    dense_cells: set[Cell],
    density_scores: Mapping[Cell, int],
) -> set[Cell]:
    components: list[set[Cell]] = []
    seen: set[Cell] = set()
    for cell in sorted(dense_cells):
        if cell in seen:
            continue
        component: set[Cell] = set()
        queue = deque([cell])
        seen.add(cell)
        while queue:
            current = queue.popleft()
            component.add(current)
            for neighbor in _neighbors(current):
                if neighbor in dense_cells and neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        components.append(component)
    if not components:
        raise BenchmarkConfigurationError("could not select a dense route component")
    return max(
        components,
        key=lambda component: (
            max(density_scores[cell] for cell in component),
            sum(density_scores[cell] for cell in component),
            len(component),
        ),
    )


def _dense_component_path(
    component: set[Cell],
    density_scores: Mapping[Cell, int],
    *,
    candidate_limit: int,
) -> tuple[Cell, ...]:
    if len(component) == 1:
        return tuple(component)
    candidates = sorted(
        component,
        key=lambda cell: (-density_scores[cell], cell),
    )[: min(candidate_limit, len(component))]
    _, start, end = max(
        (
            (_cell_distance(first, second), first, second)
            for index, first in enumerate(candidates)
            for second in candidates[index + 1 :]
        ),
        key=lambda item: (item[0], density_scores[item[1]] + density_scores[item[2]]),
    )
    return _shortest_path(component, start, end, density_scores)


def _shortest_path(
    component: set[Cell],
    start: Cell,
    end: Cell,
    density_scores: Mapping[Cell, int],
) -> tuple[Cell, ...]:
    queue = deque([start])
    previous: dict[Cell, Cell | None] = {start: None}
    while queue:
        current = queue.popleft()
        if current == end:
            break
        neighbors = [
            neighbor
            for neighbor in _neighbors(current)
            if neighbor in component and neighbor not in previous
        ]
        for neighbor in sorted(
            neighbors,
            key=lambda cell: (-density_scores[cell], _cell_distance(cell, end), cell),
        ):
            previous[neighbor] = current
            queue.append(neighbor)
    if end not in previous:
        return (start, end)
    path: list[Cell] = []
    current: Cell | None = end
    while current is not None:
        path.append(current)
        current = previous[current]
    return tuple(reversed(path))


def _sample_route_cells(
    path_cells: tuple[Cell, ...],
    *,
    centers: Mapping[Cell, Point],
    keyframe_count: int,
) -> tuple[Cell, ...]:
    if len(path_cells) <= keyframe_count:
        return path_cells
    distances = _cumulative_distances(tuple(centers[cell] for cell in path_cells))
    total = distances[-1]
    selected: list[Cell] = []
    for index in range(keyframe_count):
        target = total * index / max(1, keyframe_count - 1)
        closest_index = min(
            range(len(distances)),
            key=lambda candidate: (
                abs(distances[candidate] - target),
                candidate,
            ),
        )
        cell = path_cells[closest_index]
        if not selected or selected[-1] != cell:
            selected.append(cell)
    return tuple(selected)


def _sample_footprint_route_cells(
    path_cells: tuple[FootprintCell, ...],
    *,
    centers: Mapping[FootprintCell, PointXZ],
    keyframe_count: int,
) -> tuple[FootprintCell, ...]:
    if len(path_cells) <= keyframe_count:
        return path_cells
    distances = _footprint_cumulative_distances(path_cells, centers)
    total = distances[-1]
    selected: list[FootprintCell] = []
    for index in range(keyframe_count):
        target = total * index / max(1, keyframe_count - 1)
        closest_index = min(
            range(len(distances)),
            key=lambda candidate: (
                abs(distances[candidate] - target),
                candidate,
            ),
        )
        cell = path_cells[closest_index]
        if not selected or selected[-1] != cell:
            selected.append(cell)
    return tuple(selected)


def _footprint_path_length(
    path_cells: tuple[FootprintCell, ...],
    centers: Mapping[FootprintCell, PointXZ],
) -> float:
    return _footprint_cumulative_distances(path_cells, centers)[-1]


def _footprint_cumulative_distances(
    path_cells: tuple[FootprintCell, ...],
    centers: Mapping[FootprintCell, PointXZ],
) -> list[float]:
    points = tuple((centers[cell][0], 0.0, centers[cell][1]) for cell in path_cells)
    return _cumulative_distances(points)


def _route_points_for_footprint_cells(
    route_cells: tuple[FootprintCell, ...],
    *,
    manifest: Mapping[str, Any],
    footprint_cell_size: float,
    y_bias: float,
    y_search_radius_cells: int,
) -> tuple[Point, ...]:
    _validate_y_bias(y_bias)
    columns = _chunk_columns(manifest)
    points: list[Point] = []
    for cell in route_cells:
        x, z = _footprint_world_center(cell, footprint_cell_size)
        x, y, z = _vertical_center_point_for_xz(
            columns,
            target_x=x,
            target_z=z,
            y_bias=y_bias,
            local_radius_cells=y_search_radius_cells,
        )
        points.append((x, y, z))
    return tuple(points)


def _validate_y_bias(value: float) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise BenchmarkConfigurationError(
            "centerline Y bias must be a finite value between 0.0 and 1.0"
        )


def _chunk_columns(
    manifest: Mapping[str, Any],
) -> tuple[float, dict[FootprintCell, list[_ChunkColumnSample]]]:
    chunk_size = _positive_float(manifest.get("chunk_size"), "chunk_size")
    chunks = manifest.get("chunks")
    if not isinstance(chunks, Mapping) or not chunks:
        raise BenchmarkConfigurationError("manifest contains no chunks")
    columns: dict[FootprintCell, list[_ChunkColumnSample]] = {}
    for cell_key, info in chunks.items():
        cell = _parse_cell_key(str(cell_key))
        if not isinstance(info, Mapping):
            raise BenchmarkConfigurationError(f"invalid chunk metadata for {cell_key}")
        try:
            bounds_min = tuple(float(value) for value in info["bounds_min"])
            bounds_max = tuple(float(value) for value in info["bounds_max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkConfigurationError(
                f"chunk {cell_key} is missing valid bounds"
            ) from exc
        if len(bounds_min) != 3 or len(bounds_max) != 3:
            raise BenchmarkConfigurationError(f"chunk {cell_key} bounds must be 3D")
        columns.setdefault((cell[0], cell[2]), []).append(
            _ChunkColumnSample(
                min_y=min(bounds_min[1], bounds_max[1]),
                max_y=max(bounds_min[1], bounds_max[1]),
            )
        )
    return chunk_size, columns


def _vertical_center_point_for_xz(
    columns: tuple[float, Mapping[FootprintCell, list[_ChunkColumnSample]]],
    *,
    target_x: float,
    target_z: float,
    y_bias: float,
    local_radius_cells: int,
    search_radius_cells: int = 12,
) -> Point:
    chunk_size, column_values = columns
    target_cx = int(math.floor(target_x / chunk_size))
    target_cz = int(math.floor(target_z / chunk_size))

    exact_column = (target_cx, target_cz)
    if exact_column in column_values:
        y = _vertical_center_y_for_local_columns(
            column_values,
            center=exact_column,
            radius=local_radius_cells,
            y_bias=y_bias,
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
            y = _vertical_center_y_for_local_columns(
                column_values,
                center=best_col,
                radius=local_radius_cells,
                y_bias=y_bias,
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
    y = _vertical_center_y_for_local_columns(
        column_values,
        center=closest_col,
        radius=local_radius_cells,
        y_bias=y_bias,
    )
    return (
        (closest_col[0] + 0.5) * chunk_size,
        y,
        (closest_col[1] + 0.5) * chunk_size,
    )


def _vertical_center_y_for_local_columns(
    column_values: Mapping[FootprintCell, list[_ChunkColumnSample]],
    *,
    center: FootprintCell,
    radius: int,
    y_bias: float,
) -> float:
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
    return min_y + (max_y - min_y) * y_bias


def _keyframes_for_points(
    points: tuple[Point, ...],
    *,
    duration_s: float,
) -> list[dict[str, Any]]:
    if not points:
        raise BenchmarkConfigurationError("generated route has no points")
    distances = _cumulative_distances(points)
    total_distance = distances[-1]
    keyframes: list[dict[str, Any]] = []
    for index, point in enumerate(points):
        if total_distance > 0:
            time_s = float(duration_s) * distances[index] / total_distance
        else:
            time_s = float(duration_s) * index / max(1, len(points) - 1)
        yaw_deg, pitch_deg = _look_angles(points, index)
        keyframes.append(
            {
                "time_s": round(time_s, 6),
                "position": [round(float(value), 6) for value in point],
                "yaw_deg": round(yaw_deg, 6),
                "pitch_deg": round(pitch_deg, 6),
            }
        )
    keyframes[0]["time_s"] = 0.0
    if len(keyframes) > 1:
        keyframes[-1]["time_s"] = round(float(duration_s), 6)
    return keyframes


def _look_angles(points: tuple[Point, ...], index: int) -> tuple[float, float]:
    if len(points) == 1:
        return 0.0, 0.0
    source = points[index]
    target = points[index + 1] if index < len(points) - 1 else points[index - 1]
    dx = target[0] - source[0]
    dy = target[1] - source[1]
    dz = target[2] - source[2]
    horizontal = math.hypot(dx, dz)
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    if distance < 1e-9:
        return 0.0, 0.0
    yaw = math.degrees(math.atan2(dz, dx))
    pitch = math.degrees(math.atan2(dy, horizontal))
    return yaw, pitch


def _cumulative_distances(points: tuple[Point, ...]) -> list[float]:
    distances = [0.0]
    for first, second in zip(points, points[1:]):
        distances.append(distances[-1] + _point_distance(first, second))
    return distances


def _path_length(points: tuple[Point, ...]) -> float:
    return sum(
        _point_distance(first, second)
        for first, second in zip(points, points[1:])
    )


def _point_distance(first: Point, second: Point) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def _cell_distance(first: Cell, second: Cell) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def _footprint_world_center(cell: FootprintCell, cell_size: float) -> PointXZ:
    return (
        (cell[0] + 0.5) * cell_size,
        (cell[1] + 0.5) * cell_size,
    )


def _footprint_cell_distance(first: FootprintCell, second: FootprintCell) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _neighbors(cell: Cell) -> list[Cell]:
    return [
        (cell[0] + dx, cell[1] + dy, cell[2] + dz)
        for dx, dy, dz in _NEIGHBOR_OFFSETS_26
    ]


def _footprint_neighbors(cell: FootprintCell) -> list[FootprintCell]:
    return [
        (cell[0] + dx, cell[1] + dz)
        for dx, dz in _NEIGHBOR_OFFSETS_8
    ]
