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
DEFAULT_CENTERLINE_ROUTE_TARGET_CHUNKS = 3.0
DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS = 1
CENTERLINE_ROUTE_VERTICAL_POSITION_FRACTION = 0.65
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


@dataclass(frozen=True)
class _ChunkLoadInfo:
    center: Point
    materials: frozenset[str]
    textures: frozenset[str]


@dataclass(frozen=True)
class _RouteComplexityScore:
    score: float
    chunk_count: int
    material_count: int
    texture_count: int


def generate_centerline_route_scenario(
    manifest: Mapping[str, Any],
    template: BenchmarkScenario,
    *,
    name: str = "devils-eye-xl-centerline-route-v1",
    keyframe_count: int = DEFAULT_CENTERLINE_ROUTE_KEYFRAMES,
    candidate_limit: int = DEFAULT_CENTERLINE_ROUTE_CANDIDATE_LIMIT,
    endpoint_percentile: float = DEFAULT_CENTERLINE_ROUTE_ENDPOINT_PERCENTILE,
    target_length_m: float | None = None,
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
    route_complexity_scores = _route_complexity_scores(
        full_path_cells,
        centers=full_path_centers,
        manifest=manifest,
        render_distance=template.render_distance,
        y_search_radius_cells=max(0, int(y_search_radius_cells)),
    )
    resolved_target_length_m = _target_centerline_route_length_m(
        chunk_size=_positive_float(manifest.get("chunk_size"), "chunk_size"),
        measurement_duration_s=template.measurement_seconds,
        target_length_m=target_length_m,
    )
    target_length_is_override = target_length_m is not None
    resolved_target_speed_m_per_second = (
        resolved_target_length_m / template.measurement_seconds
    )
    path_cells, segment_start_m, segment_end_m, pivot_cell = (
        _select_footprint_path_segment(
            full_path_cells,
            centers=full_path_centers,
            clearance_scores=clearance_scores,
            complexity_scores=route_complexity_scores,
            target_length_m=resolved_target_length_m,
        )
    )
    route_xz_points = _sample_footprint_route_points(
        full_path_cells,
        centers=full_path_centers,
        start_distance_m=segment_start_m,
        end_distance_m=segment_end_m,
        keyframe_count=max(1, int(keyframe_count)),
    )
    route_cells = _nearest_footprint_cells_for_points(
        route_xz_points,
        path_cells=path_cells,
        centers=full_path_centers,
    )
    route_points = _route_points_for_xz_points(
        route_xz_points,
        manifest=manifest,
        y_search_radius_cells=max(0, int(y_search_radius_cells)),
    )
    route_scores = [clearance_scores[cell] for cell in route_cells]
    path_scores = [clearance_scores[cell] for cell in path_cells]
    route_complexities = [route_complexity_scores[cell] for cell in route_cells]
    path_complexities = [route_complexity_scores[cell] for cell in path_cells]
    route_length_m = _path_length(route_points)
    actual_route_speed_m_per_second = route_length_m / template.measurement_seconds
    route = _keyframes_for_points(
        route_points,
        duration_s=template.measurement_seconds,
        start_time_s=template.warmup_seconds,
        hold_start=True,
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
                "highest chunk/texture complexity segment through the "
                "vertex-derived cave footprint centerline"
            ),
            "clearance_definition": (
                "grid distance from the footprint boundary in footprint cells"
            ),
            "complexity_definition": (
                "normalized sum of render-distance forward-view chunk count "
                "and unique texture count from the route camera direction"
            ),
            "route_selection_strategy": "max_visible_chunk_texture_complexity_v1",
            "warmup_behavior": "hold_first_keyframe_until_measurement",
            "route_travel_start_s": round(template.warmup_seconds, 3),
            "route_travel_duration_s": round(template.measurement_seconds, 3),
            "y_strategy": "local_vertical_center_v1",
            "vertical_position_fraction": (
                CENTERLINE_ROUTE_VERTICAL_POSITION_FRACTION
            ),
            "y_search_radius_cells": max(0, int(y_search_radius_cells)),
            "footprint_cell_size_m": round(footprint.cell_size, 3),
            "footprint_cell_count": len(footprint.cells),
            "footprint_component_size": len(component),
            "endpoint_clearance_percentile": float(endpoint_percentile),
            "endpoint_threshold_clearance_cells": int(endpoint_threshold),
            "target_route_length_m": round(resolved_target_length_m, 3),
            "target_route_length_source": (
                "explicit_meters"
                if target_length_is_override
                else "default_chunk_widths"
            ),
            "target_route_length_chunks": (
                None
                if target_length_is_override
                else DEFAULT_CENTERLINE_ROUTE_TARGET_CHUNKS
            ),
            "target_route_speed_m_per_second": round(
                resolved_target_speed_m_per_second,
                6,
            ),
            "target_route_speed_m_per_minute": round(
                resolved_target_speed_m_per_second * 60.0,
                3,
            ),
            "actual_route_speed_m_per_second": round(
                actual_route_speed_m_per_second,
                6,
            ),
            "actual_route_speed_m_per_minute": round(
                actual_route_speed_m_per_second * 60.0,
                3,
            ),
            "full_path_cell_count": len(full_path_cells),
            "full_path_length_m": round(full_path_length_m, 3),
            "segment_start_m": round(segment_start_m, 3),
            "segment_end_m": round(segment_end_m, 3),
            "complexity_render_distance_chunks": template.render_distance,
            "complexity_pivot_cell": list(pivot_cell),
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
            "max_route_complexity_score": round(
                max(item.score for item in route_complexities),
                3,
            ),
            "mean_route_complexity_score": round(
                sum(item.score for item in route_complexities)
                / len(route_complexities),
                3,
            ),
            "max_route_visible_chunks": max(
                item.chunk_count for item in route_complexities
            ),
            "mean_route_visible_chunks": round(
                sum(item.chunk_count for item in route_complexities)
                / len(route_complexities),
                3,
            ),
            "max_route_unique_textures": max(
                item.texture_count for item in route_complexities
            ),
            "mean_route_unique_textures": round(
                sum(item.texture_count for item in route_complexities)
                / len(route_complexities),
                3,
            ),
            "max_route_unique_materials": max(
                item.material_count for item in route_complexities
            ),
            "mean_route_unique_materials": round(
                sum(item.material_count for item in route_complexities)
                / len(route_complexities),
                3,
            ),
            "max_path_complexity_score": round(
                max(item.score for item in path_complexities),
                3,
            ),
            "max_path_visible_chunks": max(
                item.chunk_count for item in path_complexities
            ),
            "max_path_unique_textures": max(
                item.texture_count for item in path_complexities
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


def _chunk_load_infos(manifest: Mapping[str, Any]) -> dict[Cell, _ChunkLoadInfo]:
    chunks = manifest.get("chunks")
    if not isinstance(chunks, Mapping) or not chunks:
        raise BenchmarkConfigurationError("manifest contains no chunks")
    material_textures = _material_texture_map(manifest)
    parsed: dict[Cell, _ChunkLoadInfo] = {}
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
        materials = frozenset(
            material_name
            for material in info.get("materials", ())
            if (material_name := str(material).strip())
        )
        textures = frozenset(
            material_textures[material]
            for material in materials
            if material_textures.get(material)
        )
        parsed[cell] = _ChunkLoadInfo(
            center=tuple(
                (bounds_min[index] + bounds_max[index]) / 2.0
                for index in range(3)
            ),
            materials=materials,
            textures=textures,
        )
    return parsed


def _material_texture_map(manifest: Mapping[str, Any]) -> dict[str, str]:
    raw_materials = manifest.get("mtl_materials", {})
    if not isinstance(raw_materials, Mapping):
        return {}
    material_textures: dict[str, str] = {}
    for material, texture in raw_materials.items():
        if not isinstance(texture, str) or not texture.strip():
            continue
        material_textures[str(material)] = texture.strip()
    return material_textures


def _route_complexity_scores(
    path_cells: tuple[FootprintCell, ...],
    *,
    centers: Mapping[FootprintCell, PointXZ],
    manifest: Mapping[str, Any],
    render_distance: int,
    y_search_radius_cells: int,
) -> dict[FootprintCell, _RouteComplexityScore]:
    if not path_cells:
        raise BenchmarkConfigurationError("cannot score an empty centerline path")
    chunk_infos = _chunk_load_infos(manifest)
    columns = _chunk_columns(manifest)
    chunk_size = columns[0]
    raw_scores: dict[FootprintCell, _RouteComplexityScore] = {}
    radius = max(0, int(render_distance))
    max_distance_m = max(chunk_size, (radius + 0.75) * chunk_size)
    max_distance_sq = max_distance_m * max_distance_m
    forward_slop_m = chunk_size * 0.5
    cone_tan = math.tan(math.radians(75.0 * 0.5)) * 1.6

    route_points = tuple(
        _vertical_center_point_for_xz(
            columns,
            target_x=centers[path_cell][0],
            target_z=centers[path_cell][1],
            local_radius_cells=y_search_radius_cells,
        )
        for path_cell in path_cells
    )

    for index, path_cell in enumerate(path_cells):
        route_point = route_points[index]
        forward = _route_forward_vector(route_points, index)
        materials: set[str] = set()
        textures: set[str] = set()
        chunk_count = 0
        for info in chunk_infos.values():
            rel = (
                info.center[0] - route_point[0],
                info.center[1] - route_point[1],
                info.center[2] - route_point[2],
            )
            distance_sq = rel[0] * rel[0] + rel[1] * rel[1] + rel[2] * rel[2]
            if distance_sq > max_distance_sq:
                continue
            depth = rel[0] * forward[0] + rel[1] * forward[1] + rel[2] * forward[2]
            if depth < -forward_slop_m:
                continue
            lateral_sq = max(0.0, distance_sq - depth * depth)
            visible_radius = max(chunk_size, depth) * cone_tan + chunk_size
            if lateral_sq > visible_radius * visible_radius:
                continue
            chunk_count += 1
            materials.update(info.materials)
            textures.update(info.textures)
        raw_scores[path_cell] = _RouteComplexityScore(
            score=0.0,
            chunk_count=chunk_count,
            material_count=len(materials),
            texture_count=len(textures),
        )

    max_chunks = max(1, max(item.chunk_count for item in raw_scores.values()))
    max_textures = max(item.texture_count for item in raw_scores.values())
    use_materials_as_texture_proxy = max_textures <= 0
    texture_denominator = max(
        1,
        max(
            (
                item.material_count
                if use_materials_as_texture_proxy
                else item.texture_count
            )
            for item in raw_scores.values()
        ),
    )
    return {
        cell: _RouteComplexityScore(
            score=(
                score.chunk_count / max_chunks
                + (
                    score.material_count
                    if use_materials_as_texture_proxy
                    else score.texture_count
                )
                / texture_denominator
            ),
            chunk_count=score.chunk_count,
            material_count=score.material_count,
            texture_count=score.texture_count,
        )
        for cell, score in raw_scores.items()
    }


def _route_forward_vector(points: tuple[Point, ...], index: int) -> Point:
    if len(points) <= 1:
        return 1.0, 0.0, 0.0
    if index < len(points) - 1:
        source = points[index]
        target = points[index + 1]
    else:
        source = points[index - 1]
        target = points[index]
    dx = target[0] - source[0]
    dy = target[1] - source[1]
    dz = target[2] - source[2]
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    if length <= 1e-9:
        return 1.0, 0.0, 0.0
    return dx / length, dy / length, dz / length


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
    *,
    chunk_size: float,
    measurement_duration_s: float,
    target_length_m: float | None,
) -> float:
    if target_length_m is not None:
        target = float(target_length_m)
        if not math.isfinite(target) or target <= 0.0:
            raise BenchmarkConfigurationError(
                "centerline target route length must be a positive number"
            )
        return target
    duration = float(measurement_duration_s)
    if not math.isfinite(duration) or duration <= 0.0:
        raise BenchmarkConfigurationError(
            "centerline route duration must be a positive number"
        )
    return float(chunk_size) * DEFAULT_CENTERLINE_ROUTE_TARGET_CHUNKS


def _select_footprint_path_segment(
    path_cells: tuple[FootprintCell, ...],
    *,
    centers: Mapping[FootprintCell, PointXZ],
    clearance_scores: Mapping[FootprintCell, int],
    complexity_scores: Mapping[FootprintCell, _RouteComplexityScore],
    target_length_m: float,
) -> tuple[tuple[FootprintCell, ...], float, float, FootprintCell]:
    if len(path_cells) <= 2:
        distances = _footprint_cumulative_distances(path_cells, centers)
        pivot = path_cells[
            _centerline_segment_pivot_index(
                path_cells,
                distances=distances,
                clearance_scores=clearance_scores,
                complexity_scores=complexity_scores,
            )
        ]
        return path_cells, 0.0, distances[-1], pivot
    distances = _footprint_cumulative_distances(path_cells, centers)
    total = distances[-1]
    pivot_index = _centerline_segment_pivot_index(
        path_cells,
        distances=distances,
        clearance_scores=clearance_scores,
        complexity_scores=complexity_scores,
    )
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


def _centerline_segment_pivot_index(
    path_cells: tuple[FootprintCell, ...],
    *,
    distances: list[float],
    clearance_scores: Mapping[FootprintCell, int],
    complexity_scores: Mapping[FootprintCell, _RouteComplexityScore],
) -> int:
    midpoint = distances[-1] / 2.0
    return max(
        range(len(path_cells)),
        key=lambda index: (
            complexity_scores[path_cells[index]].score,
            complexity_scores[path_cells[index]].texture_count,
            complexity_scores[path_cells[index]].chunk_count,
            clearance_scores[path_cells[index]],
            -abs(distances[index] - midpoint),
            -index,
        ),
    )


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


def _sample_footprint_route_points(
    path_cells: tuple[FootprintCell, ...],
    *,
    centers: Mapping[FootprintCell, PointXZ],
    start_distance_m: float,
    end_distance_m: float,
    keyframe_count: int,
) -> tuple[PointXZ, ...]:
    if not path_cells:
        raise BenchmarkConfigurationError("cannot sample an empty footprint path")
    distances = _footprint_cumulative_distances(path_cells, centers)
    total = distances[-1]
    start = max(0.0, min(float(start_distance_m), total))
    end = max(start, min(float(end_distance_m), total))
    if keyframe_count <= 1 or len(path_cells) == 1 or end - start <= 1e-9:
        return (_interpolated_footprint_point(path_cells, centers, distances, start),)

    return tuple(
        _interpolated_footprint_point(
            path_cells,
            centers,
            distances,
            start + (end - start) * index / max(1, keyframe_count - 1),
        )
        for index in range(keyframe_count)
    )


def _interpolated_footprint_point(
    path_cells: tuple[FootprintCell, ...],
    centers: Mapping[FootprintCell, PointXZ],
    distances: list[float],
    target_distance_m: float,
) -> PointXZ:
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


def _nearest_footprint_cells_for_points(
    points: tuple[PointXZ, ...],
    *,
    path_cells: tuple[FootprintCell, ...],
    centers: Mapping[FootprintCell, PointXZ],
) -> tuple[FootprintCell, ...]:
    if not points:
        return ()
    if not path_cells:
        raise BenchmarkConfigurationError(
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


def _route_points_for_xz_points(
    route_xz_points: tuple[PointXZ, ...],
    *,
    manifest: Mapping[str, Any],
    y_search_radius_cells: int,
) -> tuple[Point, ...]:
    columns = _chunk_columns(manifest)
    points: list[Point] = []
    for x, z in route_xz_points:
        x, y, z = _vertical_center_point_for_xz(
            columns,
            target_x=x,
            target_z=z,
            local_radius_cells=y_search_radius_cells,
        )
        points.append((x, y, z))
    return tuple(points)


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
    return min_y + (max_y - min_y) * CENTERLINE_ROUTE_VERTICAL_POSITION_FRACTION


def _keyframes_for_points(
    points: tuple[Point, ...],
    *,
    duration_s: float,
    start_time_s: float = 0.0,
    hold_start: bool = False,
) -> list[dict[str, Any]]:
    if not points:
        raise BenchmarkConfigurationError("generated route has no points")
    distances = _cumulative_distances(points)
    total_distance = distances[-1]
    keyframes: list[dict[str, Any]] = []
    for index, point in enumerate(points):
        if total_distance > 0:
            time_s = (
                float(start_time_s)
                + float(duration_s) * distances[index] / total_distance
            )
        else:
            time_s = (
                float(start_time_s)
                + float(duration_s) * index / max(1, len(points) - 1)
            )
        yaw_deg, pitch_deg = _look_angles(points, index)
        keyframes.append(
            {
                "time_s": round(time_s, 6),
                "position": [round(float(value), 6) for value in point],
                "yaw_deg": round(yaw_deg, 6),
                "pitch_deg": round(pitch_deg, 6),
            }
        )
    if hold_start and start_time_s > 0.0:
        first = dict(keyframes[0])
        first["time_s"] = 0.0
        keyframes.insert(0, first)
        keyframes[1]["time_s"] = round(float(start_time_s), 6)
    else:
        keyframes[0]["time_s"] = round(float(start_time_s), 6)
    if len(keyframes) > 1:
        keyframes[-1]["time_s"] = round(
            float(start_time_s) + float(duration_s),
            6,
        )
    return keyframes


def _look_angles(points: tuple[Point, ...], index: int) -> tuple[float, float]:
    if len(points) == 1:
        return 0.0, 0.0
    if index < len(points) - 1:
        source = points[index]
        target = points[index + 1]
    else:
        source = points[index - 1]
        target = points[index]
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


def _chunk_cell_for_point(point: Point, chunk_size: float) -> Cell:
    return tuple(
        int(math.floor(coordinate / chunk_size))
        for coordinate in point
    )  # type: ignore[return-value]


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
