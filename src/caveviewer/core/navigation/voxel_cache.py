"""Optional cache-time voxel models for curvature-guided navigation.

The cache artifact in this module is intentionally separate from the render
manifest. It stores a bounded, compressed surface voxel model and compact
volume metrics for each centerline route that contains a qualifying curved
region. Older caches simply have no artifact and continue through the normal
centerline/runtime fallback path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import base64
import binascii
import math
import os
import zlib

import numpy as np

from caveviewer.core.json_io import load_bounded_json
from caveviewer.core.navigation.centerline import (
    FootprintCell,
    Point,
    footprint_path_length,
    footprint_world_center,
)
from caveviewer.core.navigation.curvature import CURVATURE_PROFILE_METHOD
from caveviewer.core.navigation.voxel_volume import (
    DEFAULT_VOXEL_CURVATURE_RANK_THRESHOLD,
    DEFAULT_VOXEL_MAX_CELLS,
    DEFAULT_VOXEL_MAX_REGIONS,
    DEFAULT_VOXEL_MAX_SURFACE_SAMPLES,
    DEFAULT_VOXEL_SIZE_M,
    LocalVoxelVolume,
    TriangleProvider,
    analyze_curvature_guided_voxel_volume,
)


NAVIGATION_VOXEL_CACHE_VERSION = 1
NAVIGATION_VOXEL_CACHE_METHOD = "curvature_corridor_voxels_v1"
NAVIGATION_VOXEL_CACHE_NAME = "navigation_voxels.json"
NAVIGATION_VOXEL_CACHE_MAX_BYTES = 64 * 1024 * 1024

# These are deliberately smaller than the interactive settings. Cache
# construction can touch more than one route, so it must remain a bounded
# import-time cost on consumer hardware.
DEFAULT_CACHE_VOXEL_SIZE_M = DEFAULT_VOXEL_SIZE_M
DEFAULT_CACHE_VOXEL_RANK_THRESHOLD = DEFAULT_VOXEL_CURVATURE_RANK_THRESHOLD
DEFAULT_CACHE_VOXEL_MAX_REGIONS = DEFAULT_VOXEL_MAX_REGIONS
DEFAULT_CACHE_VOXEL_MAX_CELLS = 32_768
DEFAULT_CACHE_VOXEL_MAX_SURFACE_SAMPLES = 50_000
DEFAULT_CACHE_VOXEL_MAX_ROUTES = 4
DEFAULT_CACHE_VOXEL_WINDOW_POINTS = 3


@dataclass(frozen=True)
class NavigationVoxelCacheConfig:
    """Bounded cache-time voxel construction settings."""

    voxel_size_m: float = DEFAULT_CACHE_VOXEL_SIZE_M
    curvature_rank_threshold: int = DEFAULT_CACHE_VOXEL_RANK_THRESHOLD
    max_regions: int = DEFAULT_CACHE_VOXEL_MAX_REGIONS
    max_cells: int = DEFAULT_CACHE_VOXEL_MAX_CELLS
    max_surface_samples: int = DEFAULT_CACHE_VOXEL_MAX_SURFACE_SAMPLES
    max_routes: int = DEFAULT_CACHE_VOXEL_MAX_ROUTES
    window_points: int = DEFAULT_CACHE_VOXEL_WINDOW_POINTS

    def validated(self) -> "NavigationVoxelCacheConfig":
        size = float(self.voxel_size_m)
        if not math.isfinite(size) or size <= 0.0:
            raise ValueError("cache voxel size must be positive and finite")
        rank = max(0, min(100, int(self.curvature_rank_threshold)))
        max_regions = max(0, int(self.max_regions))
        max_cells = max(1, int(self.max_cells))
        max_samples = max(1, int(self.max_surface_samples))
        max_routes = max(1, int(self.max_routes))
        window_points = max(1, int(self.window_points))
        return NavigationVoxelCacheConfig(
            voxel_size_m=size,
            curvature_rank_threshold=rank,
            max_regions=max_regions,
            max_cells=max_cells,
            max_surface_samples=max_samples,
            max_routes=max_routes,
            window_points=window_points,
        )


@dataclass(frozen=True)
class NavigationVoxelCacheBuildResult:
    """Result of an optional cache-time navigation voxel pass."""

    payload: dict[str, object]
    built_route_count: int
    recommended_route_id: str | None


def build_navigation_voxel_cache(
    manifest: Mapping[str, object],
    navigation_metadata: dict[str, object],
    *,
    triangle_provider: TriangleProvider,
    config: NavigationVoxelCacheConfig | None = None,
) -> NavigationVoxelCacheBuildResult:
    """Build bounded voxel models and volume summaries for cached routes.

    ``navigation_metadata`` is updated in place with small route summaries;
    the returned payload contains the larger compressed models for the
    sidecar file. The route recommendation is changed only when a built model
    exists, and an explicit navigation-start route remains authoritative.
    """
    resolved = (config or NavigationVoxelCacheConfig()).validated()
    routes = navigation_metadata.get("routes")
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        return NavigationVoxelCacheBuildResult(
            payload=_empty_payload(resolved),
            built_route_count=0,
            recommended_route_id=None,
        )

    model_routes: dict[str, object] = {}
    route_summaries: dict[str, Mapping[str, object]] = {}
    built_route_ids: list[str] = []
    for route_index, route_value in enumerate(routes):
        if route_index >= resolved.max_routes:
            break
        if not isinstance(route_value, dict):
            continue
        route_id = _route_id(route_value, route_index)
        points = _route_points(route_value)
        summary = _analyze_route(
            manifest,
            route_value,
            points,
            triangle_provider=triangle_provider,
            config=resolved,
        )
        route_value["voxel_corridor"] = summary
        route_summaries[route_id] = summary
        if not bool(summary.get("built")):
            continue
        model = summary.pop("_model", None)
        if not isinstance(model, Mapping):
            continue
        model_routes[route_id] = {
            "summary": dict(summary),
            "model": dict(model),
        }
        built_route_ids.append(route_id)
        _augment_recovery_hotspots_with_volume(route_value, summary)

    recommended_route_id = _select_recommended_route_id(
        navigation_metadata,
        route_summaries,
    )
    if recommended_route_id is not None:
        navigation_metadata["recommended_route_id"] = recommended_route_id
        navigation_metadata["route_selection_method"] = (
            "largest_cached_curvature_volume_v1"
        )
    payload: dict[str, object] = {
        "version": NAVIGATION_VOXEL_CACHE_VERSION,
        "method": NAVIGATION_VOXEL_CACHE_METHOD,
        "voxel_size_m": float(resolved.voxel_size_m),
        "curvature_method": CURVATURE_PROFILE_METHOD,
        "curvature_rank_threshold": int(resolved.curvature_rank_threshold),
        "max_regions": int(resolved.max_regions),
        "max_cells": int(resolved.max_cells),
        "max_surface_samples": int(resolved.max_surface_samples),
        "routes": model_routes,
    }
    if model_routes:
        navigation_metadata["voxel_cache"] = {
            "version": NAVIGATION_VOXEL_CACHE_VERSION,
            "method": NAVIGATION_VOXEL_CACHE_METHOD,
            "path": NAVIGATION_VOXEL_CACHE_NAME,
            "route_count": len(model_routes),
            "built_route_count": len(built_route_ids),
        }
    return NavigationVoxelCacheBuildResult(
        payload=payload,
        built_route_count=len(built_route_ids),
        recommended_route_id=recommended_route_id,
    )


def load_cached_navigation_voxel_volume(
    cache_dir: str | os.PathLike[str] | None,
    manifest: Mapping[str, object],
    route_id: str,
) -> LocalVoxelVolume | None:
    """Load one optional route voxel model from its bounded sidecar."""
    if not cache_dir:
        return None
    navigation = manifest.get("navigation")
    if not isinstance(navigation, Mapping):
        return None
    descriptor = navigation.get("voxel_cache")
    if not isinstance(descriptor, Mapping):
        return None
    if descriptor.get("version") != NAVIGATION_VOXEL_CACHE_VERSION:
        return None
    if descriptor.get("method") != NAVIGATION_VOXEL_CACHE_METHOD:
        return None
    relative_path = descriptor.get("path")
    if relative_path != NAVIGATION_VOXEL_CACHE_NAME:
        return None
    path = os.path.join(os.fspath(cache_dir), NAVIGATION_VOXEL_CACHE_NAME)
    try:
        payload = load_bounded_json(
            path,
            max_bytes=NAVIGATION_VOXEL_CACHE_MAX_BYTES,
            description="navigation voxel cache",
        )
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("version") != NAVIGATION_VOXEL_CACHE_VERSION:
        return None
    if payload.get("method") != NAVIGATION_VOXEL_CACHE_METHOD:
        return None
    route_models = payload.get("routes")
    if not isinstance(route_models, Mapping):
        return None
    route_payload = route_models.get(str(route_id))
    if not isinstance(route_payload, Mapping):
        return None
    model = route_payload.get("model")
    if not isinstance(model, Mapping):
        return None
    try:
        return deserialize_local_voxel_volume(model)
    except (TypeError, ValueError, binascii.Error, zlib.error):
        return None


def serialize_local_voxel_volume(volume: LocalVoxelVolume) -> dict[str, object]:
    """Return a compact, JSON-safe representation of one bounded model."""
    cells = np.asarray(sorted(volume.surface_cells), dtype=np.int32)
    if cells.size == 0:
        cells = np.empty((0, 3), dtype=np.int32)
    else:
        cells = cells.reshape(-1, 3)
    compressed = zlib.compress(cells.tobytes(order="C"), level=6)
    return {
        "version": 1,
        "method": "sparse_surface_voxels_zlib_int32_v1",
        "voxel_size_m": float(volume.voxel_size_m),
        "origin": [float(value) for value in volume.origin],
        "shape": [int(value) for value in volume.shape],
        "surface_cell_count": int(len(cells)),
        "surface_cells_encoding": "zlib_base64_int32_xyz",
        "surface_cells": base64.b64encode(compressed).decode("ascii"),
        "triangle_count": int(volume.triangle_count),
        "surface_sample_count": int(volume.surface_sample_count),
        "sampling_truncated": bool(volume.sampling_truncated),
        "max_clearance_search_cells": int(volume.max_clearance_search_cells),
    }


def deserialize_local_voxel_volume(
    payload: Mapping[str, object],
    *,
    max_voxels: int = DEFAULT_VOXEL_MAX_CELLS * 4,
) -> LocalVoxelVolume:
    """Validate and restore a bounded sparse surface voxel model."""
    if payload.get("version") != 1:
        raise ValueError("unsupported navigation voxel model version")
    if payload.get("method") != "sparse_surface_voxels_zlib_int32_v1":
        raise ValueError("unsupported navigation voxel model method")
    size = _positive_float(payload.get("voxel_size_m"), "voxel size")
    origin = _point(payload.get("origin"), "voxel origin")
    shape_values = _integer_sequence(payload.get("shape"), 3, "voxel shape")
    if any(value <= 0 for value in shape_values):
        raise ValueError("cached navigation voxel shape is not positive")
    shape = tuple(shape_values)
    voxel_count = shape[0] * shape[1] * shape[2]
    if voxel_count > max(1, int(max_voxels)):
        raise ValueError("cached navigation voxel model is too large")
    if payload.get("surface_cells_encoding") != "zlib_base64_int32_xyz":
        raise ValueError("unsupported navigation voxel cell encoding")
    encoded = payload.get("surface_cells")
    if not isinstance(encoded, str):
        raise ValueError("cached navigation voxel cells are missing")
    compressed = base64.b64decode(encoded, validate=True)
    max_raw_bytes = max(1, int(max_voxels)) * 3 * np.dtype(np.int32).itemsize
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(compressed, max_raw_bytes + 1)
    if (
        len(raw) > max_raw_bytes
        or decompressor.unconsumed_tail
        or decompressor.unused_data
        or not decompressor.eof
    ):
        raise ValueError("cached navigation voxel cells are too large")
    raw += decompressor.flush(max_raw_bytes + 1 - len(raw))
    if len(raw) > max_raw_bytes:
        raise ValueError("cached navigation voxel cells are too large")
    if len(raw) % (3 * np.dtype(np.int32).itemsize) != 0:
        raise ValueError("cached navigation voxel cells are malformed")
    cells_array = np.frombuffer(raw, dtype=np.int32).reshape(-1, 3)
    expected_count = int(payload.get("surface_cell_count", len(cells_array)))
    if expected_count != len(cells_array):
        raise ValueError("cached navigation voxel cell count is inconsistent")
    cells: set[tuple[int, int, int]] = set()
    for row in cells_array:
        index = (int(row[0]), int(row[1]), int(row[2]))
        if not all(0 <= index[axis] < shape[axis] for axis in range(3)):
            raise ValueError("cached navigation voxel cell is outside bounds")
        cells.add(index)
    return LocalVoxelVolume(
        voxel_size_m=size,
        origin=origin,
        shape=shape,  # type: ignore[arg-type]
        surface_cells=frozenset(cells),
        triangle_count=max(0, int(payload.get("triangle_count", 0))),
        surface_sample_count=max(0, int(payload.get("surface_sample_count", 0))),
        sampling_truncated=bool(payload.get("sampling_truncated", False)),
        max_clearance_search_cells=max(
            0,
            int(payload.get("max_clearance_search_cells", 8)),
        ),
    )


def _analyze_route(
    manifest: Mapping[str, object],
    route: Mapping[str, object],
    points: tuple[Point, ...],
    *,
    triangle_provider: TriangleProvider,
    config: NavigationVoxelCacheConfig,
) -> dict[str, object]:
    common: dict[str, object] = {
        "version": NAVIGATION_VOXEL_CACHE_VERSION,
        "method": NAVIGATION_VOXEL_CACHE_METHOD,
        "curvature_method": CURVATURE_PROFILE_METHOD,
        "voxel_size_m": float(config.voxel_size_m),
        "curvature_rank_threshold": int(config.curvature_rank_threshold),
        "max_regions": int(config.max_regions),
        "max_cells": int(config.max_cells),
        "max_surface_samples": int(config.max_surface_samples),
        "point_count": len(points),
    }
    if len(points) < 3 or config.max_regions == 0:
        common["outcome"] = "insufficient_route_points"
        common["built"] = False
        return common
    try:
        analysis = analyze_curvature_guided_voxel_volume(
            points,
            triangle_provider=triangle_provider,
            voxel_size_m=config.voxel_size_m,
            curvature_rank_threshold=config.curvature_rank_threshold,
            max_regions=config.max_regions,
            max_distance_m=None,
            padding_m=max(
                _route_cell_size(route, manifest) * 0.5,
                config.voxel_size_m * 2.0,
            ),
            max_voxels=config.max_cells,
            max_surface_samples=config.max_surface_samples,
            window_points=config.window_points,
        )
    except Exception as exc:
        common.update(
            {
                "outcome": "error",
                "built": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        return common

    common.update(
        {
            "outcome": str(analysis.outcome),
            "built": analysis.volume is not None,
            "curvature_sample_count": len(analysis.profile.samples),
            "curvature_region_count": len(analysis.profile.regions),
            "selected_region_count": len(analysis.selected_regions),
            "selected_regions": [
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
                for region in analysis.selected_regions
            ],
            "bounds_min": _point_payload(analysis.bounds_min),
            "bounds_max": _point_payload(analysis.bounds_max),
            "triangle_count": int(analysis.triangle_count),
            "surface_sample_count": int(analysis.surface_sample_count),
            "sampling_truncated": bool(analysis.sampling_truncated),
        }
    )
    if analysis.volume is None:
        return common

    metrics = analysis.volume.corridor_volume_metrics(points)
    route_length = _route_length(route, points, manifest)
    available_volume = float(metrics.get("available_volume_m3", 0.0))
    common.update(
        {
            **metrics,
            "route_length_m": float(route_length),
            "volume_per_route_m": float(
                available_volume / max(1e-6, route_length)
            ),
            "model": serialize_local_voxel_volume(analysis.volume),
        }
    )
    # ``model`` is needed by the sidecar but is intentionally removed from the
    # small manifest summary by the caller.
    common["_model"] = common.pop("model")
    return common


def _augment_recovery_hotspots_with_volume(
    route: dict[str, object],
    summary: Mapping[str, object],
) -> None:
    hotspots = route.get("recovery_hotspots")
    if not isinstance(hotspots, dict):
        return
    cells = _flat_cells(hotspots.get("cells"))
    if not cells:
        return
    bounds_min = _point_tuple(summary.get("bounds_min"))
    bounds_max = _point_tuple(summary.get("bounds_max"))
    if bounds_min is None or bounds_max is None:
        return
    route_cell_size = _positive_float(
        route.get("footprint_cell_size"),
        "route footprint cell size",
    )
    available_volume = float(summary.get("available_volume_m3", 0.0))
    volume_per_route = float(summary.get("volume_per_route_m", 0.0))
    mean_clearance = float(summary.get("mean_clearance_m", 0.0))
    volume_values: list[float] = []
    per_route_values: list[float] = []
    clearance_values: list[float] = []
    for cell in cells:
        x, z = footprint_world_center(cell, route_cell_size)
        inside = (
            bounds_min[0] <= x < bounds_max[0]
            and bounds_min[2] <= z < bounds_max[2]
        )
        volume_values.append(available_volume if inside else 0.0)
        per_route_values.append(volume_per_route if inside else 0.0)
        clearance_values.append(mean_clearance if inside else 0.0)
    hotspots["available_volume_m3"] = volume_values
    hotspots["volume_per_route_m"] = per_route_values
    hotspots["voxel_mean_clearance_m"] = clearance_values


def _select_recommended_route_id(
    navigation_metadata: Mapping[str, object],
    summaries: Mapping[str, Mapping[str, object]],
) -> str | None:
    built = [
        (route_id, summary)
        for route_id, summary in summaries.items()
        if bool(summary.get("built"))
    ]
    if not built:
        return None
    routes = navigation_metadata.get("routes")
    route_by_id = {
        str(route.get("id")): route
        for route in routes
        if isinstance(route, Mapping) and route.get("id") is not None
    } if isinstance(routes, Sequence) and not isinstance(routes, (str, bytes)) else {}
    navigation_start = navigation_metadata.get("navigation_start")
    if navigation_start is not None:
        start_built = [
            item
            for item in built
            if bool(route_by_id.get(item[0], {}).get("starts_at_navigation_start"))
        ]
        if start_built:
            built = start_built
        else:
            return None
    return max(
        built,
        key=lambda item: (
            float(item[1].get("available_volume_m3", 0.0)),
            float(item[1].get("volume_per_route_m", 0.0)),
            float(route_by_id.get(item[0], {}).get("length_m", 0.0)),
            item[0],
        ),
    )[0]


def _empty_payload(config: NavigationVoxelCacheConfig) -> dict[str, object]:
    return {
        "version": NAVIGATION_VOXEL_CACHE_VERSION,
        "method": NAVIGATION_VOXEL_CACHE_METHOD,
        "voxel_size_m": float(config.voxel_size_m),
        "curvature_method": CURVATURE_PROFILE_METHOD,
        "routes": {},
    }


def _route_id(route: Mapping[str, object], index: int) -> str:
    value = route.get("id")
    return str(value) if value is not None else f"centerline-{index}"


def _route_points(route: Mapping[str, object]) -> tuple[Point, ...]:
    value = route.get("points")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    if len(value) % 3:
        return ()
    points: list[Point] = []
    for index in range(0, len(value), 3):
        try:
            point = (float(value[index]), float(value[index + 1]), float(value[index + 2]))
        except (TypeError, ValueError):
            return ()
        if not all(math.isfinite(coordinate) for coordinate in point):
            return ()
        points.append(point)
    return tuple(points)


def _route_cell_size(route: Mapping[str, object], manifest: Mapping[str, object]) -> float:
    return _positive_float(
        route.get("footprint_cell_size", manifest.get("footprint_cell_size")),
        "route footprint cell size",
    )


def _route_length(
    route: Mapping[str, object],
    points: tuple[Point, ...],
    manifest: Mapping[str, object],
) -> float:
    raw_length = route.get("length_m")
    try:
        length = float(raw_length)
    except (TypeError, ValueError):
        length = 0.0
    if math.isfinite(length) and length > 0.0:
        return length
    cells = _flat_cells(route.get("cells"))
    if len(cells) >= 2:
        return footprint_path_length(
            cells,
            {
                cell: footprint_world_center(
                    cell,
                    _route_cell_size(route, manifest),
                )
                for cell in cells
            },
        )
    if len(points) >= 2:
        return float(
            sum(
                math.dist(first, second)
                for first, second in zip(points, points[1:], strict=False)
            )
        )
    return 0.0


def _point_payload(point: Point | None) -> list[float] | None:
    if point is None:
        return None
    return [float(value) for value in point]


def _point(value: object, field_name: str) -> Point:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field_name} must be a 3D sequence")
    if len(value) != 3:
        raise ValueError(f"{field_name} must be a 3D sequence")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{field_name} must be finite")
    return result  # type: ignore[return-value]


def _point_tuple(value: object) -> Point | None:
    try:
        return _point(value, "point")
    except (TypeError, ValueError):
        return None


def _positive_float(value: object, field_name: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ValueError(f"{field_name} must be positive and finite")
    return parsed


def _integer_sequence(value: object, expected: int, field_name: str) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field_name} is malformed")
    if len(value) != expected:
        raise ValueError(f"{field_name} is malformed")
    try:
        return tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is malformed") from exc


def _flat_cells(value: object) -> tuple[FootprintCell, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    if len(value) % 2:
        return ()
    cells: list[FootprintCell] = []
    for index in range(0, len(value), 2):
        try:
            cells.append((int(value[index]), int(value[index + 1])))
        except (TypeError, ValueError):
            return ()
    return tuple(cells)
