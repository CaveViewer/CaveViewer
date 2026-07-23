"""Manifest-derived benchmark route generation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Any, Mapping

from caveviewer.gui.benchmark import (
    BenchmarkConfigurationError,
    BenchmarkScenario,
    SCENARIO_VERSION,
)


Cell = tuple[int, int, int]
Point = tuple[float, float, float]

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


@dataclass(frozen=True)
class DenseRoute:
    """Generated dense-route payload and diagnostics."""

    scenario_payload: dict[str, Any]
    path_cells: tuple[Cell, ...]
    route_cells: tuple[Cell, ...]
    density_scores: dict[Cell, int]


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


def _keyframes_for_points(
    points: tuple[Point, ...],
    *,
    duration_s: float,
) -> list[dict[str, Any]]:
    if not points:
        raise BenchmarkConfigurationError("dense route has no points")
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


def _neighbors(cell: Cell) -> list[Cell]:
    return [
        (cell[0] + dx, cell[1] + dy, cell[2] + dz)
        for dx, dy, dz in _NEIGHBOR_OFFSETS_26
    ]
