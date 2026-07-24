"""Manifest-derived benchmark route generation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Any, Mapping

from caveviewer.core.navigation.centerline import (
    CENTERLINE_ROUTE_SELECTION_MIDPOINT,
    CENTERLINE_ROUTE_VERTICAL_POSITION_FRACTION,
    CENTERLINE_ROUTE_WALL_CLEARANCE_MIN_CELLS,
    CENTERLINE_ROUTE_WALL_CLEARANCE_PUSH_FRACTION,
    DEFAULT_CENTERLINE_ROUTE_CANDIDATE_LIMIT,
    DEFAULT_CENTERLINE_ROUTE_ENDPOINT_PERCENTILE,
    DEFAULT_CENTERLINE_ROUTE_KEYFRAMES,
    DEFAULT_CENTERLINE_ROUTE_MIN_CHUNKS,
    DEFAULT_CENTERLINE_ROUTE_SPEED_FEET_PER_MINUTE,
    DEFAULT_CENTERLINE_ROUTE_SPEED_M_PER_SECOND,
    DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS,
    Cell,
    CenterlinePath,
    CenterlineSegment,
    FootprintCell,
    Point,
    PointXZ,
    cell_distance,
    centerline_segment_midpoint_index,
    chunk_columns,
    footprint_cumulative_distances,
    generate_centerline_path,
    nearest_footprint_cells_for_points,
    neighbors,
    parse_cell_key,
    parse_chunk_centers,
    positive_manifest_float,
    push_route_points_toward_path_centers,
    route_points_for_xz_points,
    sample_footprint_route_points,
    select_centerline_path_segment,
    target_centerline_route_length_m,
    vertical_center_point_for_xz,
)
from caveviewer.core.navigation.route import (
    cumulative_distances,
    path_length,
    route_keyframes_for_points,
)
from caveviewer.benchmarking.results import (
    BenchmarkConfigurationError,
    BenchmarkScenario,
    SCENARIO_VERSION,
)


CENTERLINE_ROUTE_SELECTION_MAX_COMPLEXITY = "max_visible_chunk_texture_complexity_v1"
CENTERLINE_ROUTE_SELECTION_STRATEGIES = frozenset(
    {
        CENTERLINE_ROUTE_SELECTION_MAX_COMPLEXITY,
        CENTERLINE_ROUTE_SELECTION_MIDPOINT,
    }
)
DEFAULT_DENSE_ROUTE_KEYFRAMES = 8
DEFAULT_DENSE_ROUTE_PERCENTILE = 90.0
DEFAULT_DENSE_ROUTE_CANDIDATE_LIMIT = 64


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
    name: str = "auto-centerline-route-v1",
    keyframe_count: int = DEFAULT_CENTERLINE_ROUTE_KEYFRAMES,
    candidate_limit: int = DEFAULT_CENTERLINE_ROUTE_CANDIDATE_LIMIT,
    endpoint_percentile: float = DEFAULT_CENTERLINE_ROUTE_ENDPOINT_PERCENTILE,
    target_length_m: float | None = None,
    y_search_radius_cells: int = DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS,
    selection_strategy: str = CENTERLINE_ROUTE_SELECTION_MAX_COMPLEXITY,
) -> CenterlineRoute:
    """Generate a deterministic virtual route through the passage center.

    The preferred input is the manifest's fine-grained ``footprint_cells`` field,
    which the chunker derives from source vertex positions. The route follows a
    high-clearance path through that footprint, where clearance is grid distance
    from the footprint boundary. Y is estimated from the nearest occupied chunk
    column so the route stays near actual cave geometry.

    This is still a benchmark path, not a collision-checked navigation mesh.
    """
    if selection_strategy not in CENTERLINE_ROUTE_SELECTION_STRATEGIES:
        raise BenchmarkConfigurationError(
            f"unsupported centerline route selection strategy: {selection_strategy}"
        )
    centerline_path = generate_centerline_path(
        manifest,
        candidate_limit=candidate_limit,
        endpoint_percentile=endpoint_percentile,
    )
    route_complexity_scores: Mapping[FootprintCell, _RouteComplexityScore]
    if selection_strategy == CENTERLINE_ROUTE_SELECTION_MAX_COMPLEXITY:
        route_complexity_scores = _route_complexity_scores(
            centerline_path.cells,
            centers=centerline_path.centers,
            manifest=manifest,
            render_distance=template.render_distance,
            y_search_radius_cells=max(0, int(y_search_radius_cells)),
        )
    else:
        route_complexity_scores = {}
    chunk_size = positive_manifest_float(manifest.get("chunk_size"), "chunk_size")
    (
        resolved_target_length_m,
        default_length_source,
    ) = target_centerline_route_length_m(
        chunk_size=chunk_size,
        measurement_duration_s=template.measurement_seconds,
        target_length_m=target_length_m,
    )
    target_length_is_override = target_length_m is not None
    resolved_target_speed_m_per_second = (
        resolved_target_length_m / template.measurement_seconds
    )
    selected_segment = _select_centerline_path_segment(
        centerline_path,
        target_length_m=resolved_target_length_m,
        selection_strategy=selection_strategy,
        complexity_scores=route_complexity_scores,
    )
    sampled_route_xz_points = sample_footprint_route_points(
        centerline_path.cells,
        centers=centerline_path.centers,
        start_distance_m=selected_segment.start_distance_m,
        end_distance_m=selected_segment.end_distance_m,
        keyframe_count=max(1, int(keyframe_count)),
    )
    wall_clearance_adjustment = push_route_points_toward_path_centers(
        sampled_route_xz_points,
        path_cells=selected_segment.cells,
        centers=centerline_path.centers,
        clearance_scores=centerline_path.clearance_scores,
        minimum_clearance_cells=CENTERLINE_ROUTE_WALL_CLEARANCE_MIN_CELLS,
        push_fraction=CENTERLINE_ROUTE_WALL_CLEARANCE_PUSH_FRACTION,
    )
    route_xz_points = wall_clearance_adjustment.points
    route_cells = nearest_footprint_cells_for_points(
        route_xz_points,
        path_cells=selected_segment.cells,
        centers=centerline_path.centers,
    )
    route_points = route_points_for_xz_points(
        route_xz_points,
        manifest=manifest,
        y_search_radius_cells=max(0, int(y_search_radius_cells)),
    )
    route_scores = [centerline_path.clearance_scores[cell] for cell in route_cells]
    path_scores = [
        centerline_path.clearance_scores[cell] for cell in selected_segment.cells
    ]
    if route_complexity_scores:
        route_complexities = [route_complexity_scores[cell] for cell in route_cells]
        path_complexities = [
            route_complexity_scores[cell] for cell in selected_segment.cells
        ]
        complexity_metadata = {
            "complexity_definition": (
                "normalized sum of render-distance forward-view chunk count "
                "and unique texture count from the route camera direction"
            ),
            "complexity_render_distance_chunks": template.render_distance,
            "complexity_pivot_cell": list(selected_segment.pivot_cell),
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
        }
    else:
        complexity_metadata = {
            "complexity_definition": (
                "not used; route segment selected from centerline geometry only"
            ),
            "complexity_render_distance_chunks": None,
            "complexity_pivot_cell": None,
        }
    route_length_m = path_length(route_points)
    actual_route_speed_m_per_second = route_length_m / template.measurement_seconds
    route = route_keyframes_for_points(
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
            "route_source": centerline_path.source,
            "centerline_definition": (
                "vertex-derived cave footprint centerline; segment selection "
                "is controlled separately from centerline generation"
            ),
            "clearance_definition": (
                "grid distance from the footprint boundary in footprint cells"
            ),
            "wall_clearance_strategy": (
                "push_low_clearance_xz_toward_nearest_centerline_cell_center_v1"
            ),
            "wall_clearance_minimum_cells": (
                CENTERLINE_ROUTE_WALL_CLEARANCE_MIN_CELLS
            ),
            "wall_clearance_push_fraction": (
                CENTERLINE_ROUTE_WALL_CLEARANCE_PUSH_FRACTION
            ),
            "wall_clearance_adjusted_points": (
                wall_clearance_adjustment.adjusted_count
            ),
            "wall_clearance_max_adjustment_m": round(
                wall_clearance_adjustment.max_adjustment_m,
                3,
            ),
            "wall_clearance_mean_adjustment_m": round(
                wall_clearance_adjustment.mean_adjustment_m,
                3,
            ),
            **complexity_metadata,
            "route_selection_strategy": selected_segment.selection_strategy,
            "warmup_behavior": "hold_first_keyframe_until_measurement",
            "route_travel_start_s": round(template.warmup_seconds, 3),
            "route_travel_duration_s": round(template.measurement_seconds, 3),
            "y_strategy": "local_vertical_center_v1",
            "vertical_position_fraction": (
                CENTERLINE_ROUTE_VERTICAL_POSITION_FRACTION
            ),
            "y_search_radius_cells": max(0, int(y_search_radius_cells)),
            "footprint_cell_size_m": round(centerline_path.footprint_cell_size, 3),
            "footprint_cell_count": centerline_path.footprint_cell_count,
            "footprint_component_size": centerline_path.component_size,
            "endpoint_clearance_percentile": float(endpoint_percentile),
            "endpoint_threshold_clearance_cells": (
                centerline_path.endpoint_threshold_clearance_cells
            ),
            "target_route_length_m": round(resolved_target_length_m, 3),
            "target_route_length_source": (
                "explicit_meters"
                if target_length_is_override
                else default_length_source
            ),
            "target_route_length_chunks": (
                None
                if target_length_is_override
                else round(
                    resolved_target_length_m / chunk_size,
                    6,
                )
            ),
            "target_route_minimum_length_chunks": (
                None
                if target_length_is_override
                else DEFAULT_CENTERLINE_ROUTE_MIN_CHUNKS
            ),
            "target_route_speed_m_per_second": round(
                resolved_target_speed_m_per_second,
                6,
            ),
            "target_route_speed_m_per_minute": round(
                resolved_target_speed_m_per_second * 60.0,
                3,
            ),
            "target_route_speed_feet_per_minute": round(
                resolved_target_speed_m_per_second * 60.0 / 0.3048,
                3,
            ),
            "target_route_speed_source": (
                "explicit_length"
                if target_length_is_override
                else "default_50_ft_per_minute"
            ),
            "actual_route_speed_m_per_second": round(
                actual_route_speed_m_per_second,
                6,
            ),
            "actual_route_speed_m_per_minute": round(
                actual_route_speed_m_per_second * 60.0,
                3,
            ),
            "full_path_cell_count": len(centerline_path.cells),
            "full_path_length_m": round(centerline_path.length_m, 3),
            "segment_start_m": round(selected_segment.start_distance_m, 3),
            "segment_end_m": round(selected_segment.end_distance_m, 3),
            "segment_pivot_cell": list(selected_segment.pivot_cell),
            "path_cell_count": len(selected_segment.cells),
            "route_keyframe_count": len(route),
            "route_length_m": round(route_length_m, 3),
            "min_route_y": round(min(point[1] for point in route_points), 3),
            "max_route_y": round(max(point[1] for point in route_points), 3),
            "max_clearance_cells": max(centerline_path.clearance_scores.values()),
            "max_clearance_m": round(
                max(centerline_path.clearance_scores.values())
                * centerline_path.footprint_cell_size,
                3,
            ),
            "max_route_clearance_cells": max(route_scores),
            "min_route_clearance_cells": min(route_scores),
            "mean_route_clearance_cells": round(
                sum(route_scores) / len(route_scores),
                3,
            ),
            "mean_route_clearance_m": round(
                (sum(route_scores) / len(route_scores))
                * centerline_path.footprint_cell_size,
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
        path_cells=tuple(selected_segment.cells),
        route_cells=tuple(route_cells),
        clearance_scores=centerline_path.clearance_scores,
    )


def generate_dense_chunk_route_scenario(
    manifest: Mapping[str, Any],
    template: BenchmarkScenario,
    *,
    name: str = "auto-dense-chunk-route-v1",
    dense_percentile: float = DEFAULT_DENSE_ROUTE_PERCENTILE,
    keyframe_count: int = DEFAULT_DENSE_ROUTE_KEYFRAMES,
    candidate_limit: int = DEFAULT_DENSE_ROUTE_CANDIDATE_LIMIT,
) -> DenseRoute:
    """Generate a deterministic route through dense occupied chunk regions.

    Density is the number of occupied chunk cells inside the same cube radius
    used by runtime streaming for the benchmark render distance. This is a
    load proxy, not a navigation-mesh path.
    """
    chunks = parse_chunk_centers(manifest)
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
    route_length_m = path_length(route_points)
    route = route_keyframes_for_points(
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

def _chunk_load_infos(manifest: Mapping[str, Any]) -> dict[Cell, _ChunkLoadInfo]:
    chunks = manifest.get("chunks")
    if not isinstance(chunks, Mapping) or not chunks:
        raise BenchmarkConfigurationError("manifest contains no chunks")
    material_textures = _material_texture_map(manifest)
    parsed: dict[Cell, _ChunkLoadInfo] = {}
    for cell_key, info in chunks.items():
        cell = parse_cell_key(str(cell_key))
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
    columns = chunk_columns(manifest)
    chunk_size = columns[0]
    raw_scores: dict[FootprintCell, _RouteComplexityScore] = {}
    radius = max(0, int(render_distance))
    max_distance_m = max(chunk_size, (radius + 0.75) * chunk_size)
    max_distance_sq = max_distance_m * max_distance_m
    forward_slop_m = chunk_size * 0.5
    cone_tan = math.tan(math.radians(75.0 * 0.5)) * 1.6

    route_points = tuple(
        vertical_center_point_for_xz(
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


def _select_centerline_path_segment(
    centerline_path: CenterlinePath,
    *,
    target_length_m: float,
    selection_strategy: str,
    complexity_scores: Mapping[FootprintCell, _RouteComplexityScore],
) -> CenterlineSegment:
    path_cells = centerline_path.cells
    distances = footprint_cumulative_distances(path_cells, centerline_path.centers)
    if selection_strategy == CENTERLINE_ROUTE_SELECTION_MAX_COMPLEXITY:
        pivot_index = _centerline_segment_complexity_pivot_index(
            path_cells,
            distances=distances,
            clearance_scores=centerline_path.clearance_scores,
            complexity_scores=complexity_scores,
        )
    elif selection_strategy == CENTERLINE_ROUTE_SELECTION_MIDPOINT:
        pivot_index = centerline_segment_midpoint_index(distances)
    else:
        raise BenchmarkConfigurationError(
            f"unsupported centerline route selection strategy: {selection_strategy}"
        )
    return select_centerline_path_segment(
        centerline_path,
        target_length_m=target_length_m,
        pivot_index=pivot_index,
        selection_strategy=selection_strategy,
    )


def _centerline_segment_complexity_pivot_index(
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
            for neighbor in neighbors(current):
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
            (cell_distance(first, second), first, second)
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
        neighbor_cells = [
            neighbor
            for neighbor in neighbors(current)
            if neighbor in component and neighbor not in previous
        ]
        for neighbor in sorted(
            neighbor_cells,
            key=lambda cell: (-density_scores[cell], cell_distance(cell, end), cell),
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
    distances = cumulative_distances(tuple(centers[cell] for cell in path_cells))
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
