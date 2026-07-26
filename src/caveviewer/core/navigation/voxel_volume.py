"""Sparse local voxel analysis for curvature-guided navigation recovery.

This module is deliberately independent of the centerline implementation. It
builds a bounded surface-distance field from cached triangle meshes and offers
point refinement inside an existing navigation cell. Exact triangle collision
checks remain authoritative; the voxel field is an inexpensive local guide
that can be removed or replaced without changing centerline policy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
import math

import numpy as np

from caveviewer.core.navigation.curvature import (
    CurvatureProfile,
    CurvatureRegion,
    Point,
    analyze_polyline_curvature,
    select_curvature_regions,
)


VoxelIndex = tuple[int, int, int]
TriangleProvider = Callable[[Point, Point], Iterable[np.ndarray]]
VOXEL_VOLUME_METHOD = "local_surface_distance_v1"

DEFAULT_VOXEL_SIZE_M = 2.0
DEFAULT_VOXEL_CURVATURE_RANK_THRESHOLD = 65
DEFAULT_VOXEL_MAX_REGIONS = 2
DEFAULT_VOXEL_MAX_DISTANCE_M = 256.0
DEFAULT_VOXEL_MAX_CELLS = 120_000
DEFAULT_VOXEL_MAX_SURFACE_SAMPLES = 200_000
DEFAULT_VOXEL_SURFACE_INFLATION_CELLS = 1

VOXEL_ANALYSIS_OUTCOME_BUILT = "built"
VOXEL_ANALYSIS_OUTCOME_NO_CURVATURE_REGION = (
    "no_curvature_region_in_horizon"
)
VOXEL_ANALYSIS_OUTCOME_NO_REGION_POINTS = "no_region_points"
VOXEL_ANALYSIS_OUTCOME_NO_TRIANGLES = "no_triangles_in_bounds"
VOXEL_ANALYSIS_OUTCOME_NO_SURFACE_SAMPLES = "no_surface_samples"
VOXEL_ANALYSIS_OUTCOME_ERROR = "error"
VOXEL_ANALYSIS_OUTCOME_DISABLED = "disabled"
VOXEL_ANALYSIS_OUTCOME_MESH_GUARD_UNAVAILABLE = "mesh_guard_unavailable"


@dataclass(frozen=True)
class VoxelVolumeConfig:
    """Bounded local voxel construction parameters."""

    voxel_size_m: float = DEFAULT_VOXEL_SIZE_M
    surface_inflation_cells: int = DEFAULT_VOXEL_SURFACE_INFLATION_CELLS
    max_voxels: int = DEFAULT_VOXEL_MAX_CELLS
    max_surface_samples: int = DEFAULT_VOXEL_MAX_SURFACE_SAMPLES
    max_clearance_search_cells: int = 8

    def validated(self) -> "VoxelVolumeConfig":
        size = float(self.voxel_size_m)
        if not math.isfinite(size) or size <= 0.0:
            raise ValueError("voxel size must be a positive finite number")
        if int(self.surface_inflation_cells) < 0:
            raise ValueError("voxel surface inflation cannot be negative")
        if int(self.max_voxels) <= 0:
            raise ValueError("voxel max_voxels must be positive")
        if int(self.max_surface_samples) <= 0:
            raise ValueError("voxel max_surface_samples must be positive")
        if int(self.max_clearance_search_cells) < 0:
            raise ValueError("voxel clearance search cannot be negative")
        return VoxelVolumeConfig(
            voxel_size_m=size,
            surface_inflation_cells=int(self.surface_inflation_cells),
            max_voxels=int(self.max_voxels),
            max_surface_samples=int(self.max_surface_samples),
            max_clearance_search_cells=int(self.max_clearance_search_cells),
        )


@dataclass(frozen=True)
class LocalVoxelVolume:
    """Bounded sparse surface occupancy and clearance field."""

    voxel_size_m: float
    origin: Point
    shape: tuple[int, int, int]
    surface_cells: frozenset[VoxelIndex]
    triangle_count: int
    surface_sample_count: int
    sampling_truncated: bool
    max_clearance_search_cells: int

    @property
    def voxel_count(self) -> int:
        """Return the dense local volume capacity, not sparse occupancy."""
        return int(self.shape[0] * self.shape[1] * self.shape[2])

    @property
    def bounds_min(self) -> Point:
        """Return the inclusive world-space lower bound."""
        return self.origin

    @property
    def bounds_max(self) -> Point:
        """Return the exclusive world-space upper bound."""
        return tuple(
            self.origin[index] + self.shape[index] * self.voxel_size_m
            for index in range(3)
        )  # type: ignore[return-value]

    def contains_point(self, point: Sequence[float]) -> bool:
        """Return whether a world point lies inside this local volume."""
        try:
            index = self.voxel_index(point)
        except (TypeError, ValueError):
            return False
        return self.contains_index(index)

    def contains_index(self, index: VoxelIndex) -> bool:
        """Return whether a voxel index lies inside the local volume."""
        return all(
            0 <= int(index[axis]) < self.shape[axis]
            for axis in range(3)
        )

    def voxel_index(self, point: Sequence[float]) -> VoxelIndex:
        """Map a world point to a local voxel index."""
        if len(point) != 3:
            raise ValueError("voxel points must be three-dimensional")
        return tuple(
            int(
                math.floor(
                    (float(point[axis]) - self.origin[axis])
                    / self.voxel_size_m
                )
            )
            for axis in range(3)
        )  # type: ignore[return-value]

    def voxel_center(self, index: VoxelIndex) -> Point:
        """Return the world-space center of a voxel."""
        return tuple(
            self.origin[axis] + (int(index[axis]) + 0.5) * self.voxel_size_m
            for axis in range(3)
        )  # type: ignore[return-value]

    def point_is_surface_occupied(self, point: Sequence[float]) -> bool:
        """Return whether a point falls in an inflated sampled surface voxel."""
        if not self.contains_point(point):
            return False
        return self.voxel_index(point) in self.surface_cells

    def surface_clearance_m(self, index: VoxelIndex) -> float:
        """Estimate distance to the nearest sampled surface voxel."""
        if not self.contains_index(index):
            return 0.0
        if not self.surface_cells:
            return float(self.max_clearance_search_cells + 1) * self.voxel_size_m
        for radius in range(self.max_clearance_search_cells + 1):
            for candidate in _shell_indices(index, radius):
                if self.contains_index(candidate) and candidate in self.surface_cells:
                    distance_cells = math.sqrt(
                        sum(
                            (candidate[axis] - index[axis]) ** 2
                            for axis in range(3)
                        )
                    )
                    return distance_cells * self.voxel_size_m
        return float(self.max_clearance_search_cells + 1) * self.voxel_size_m

    def refine_point(
        self,
        desired: Sequence[float],
        *,
        footprint_cell: tuple[int, int],
        footprint_cell_size: float,
        y_range: tuple[float, float] | None = None,
        max_candidates: int = 4096,
    ) -> Point | None:
        """Choose a higher-clearance point inside one coarse footprint cell.

        The existing footprint and Y range remain hard bounds. This method
        only moves a recovery waypoint within that envelope and returns
        ``None`` when the local voxel volume cannot provide a candidate.
        """
        if len(desired) != 3:
            return None
        if not self.surface_cells:
            return (
                float(desired[0]),
                float(desired[1]),
                float(desired[2]),
            )
        cell_size = float(footprint_cell_size)
        if not math.isfinite(cell_size) or cell_size <= 0.0:
            return None
        try:
            desired_point = tuple(float(value) for value in desired)
        except (TypeError, ValueError):
            return None
        x_min = float(footprint_cell[0]) * cell_size
        x_max = x_min + cell_size
        z_min = float(footprint_cell[1]) * cell_size
        z_max = z_min + cell_size
        if y_range is None:
            y_min, y_max = self.bounds_min[1], self.bounds_max[1]
        else:
            y_min, y_max = sorted((float(y_range[0]), float(y_range[1])))
        x_indices = self._indices_for_axis(x_min, x_max, axis=0)
        y_indices = self._indices_for_axis(y_min, y_max, axis=1)
        z_indices = self._indices_for_axis(z_min, z_max, axis=2)
        if not x_indices or not y_indices or not z_indices:
            return None

        candidates = _sample_axis_product(
            x_indices,
            y_indices,
            z_indices,
            max_candidates=max(1, int(max_candidates)),
        )
        best_key: tuple[float, float] | None = None
        best_point: Point | None = None
        for index in candidates:
            if index in self.surface_cells:
                continue
            point = self.voxel_center(index)
            if not (
                x_min <= point[0] < x_max
                and z_min <= point[2] < z_max
                and y_min <= point[1] <= y_max
            ):
                continue
            clearance = self.surface_clearance_m(index)
            distance_squared = sum(
                (point[axis] - desired_point[axis]) ** 2
                for axis in range(3)
            )
            key = (clearance, -distance_squared)
            if best_key is None or key > best_key:
                best_key = key
                best_point = point
        return best_point

    def diagnostic_payload(self) -> dict[str, object]:
        """Return bounded diagnostics suitable for the Guided Dive blackbox."""
        return {
            "voxel_size_m": float(self.voxel_size_m),
            "origin": [float(value) for value in self.origin],
            "bounds_min": [float(value) for value in self.bounds_min],
            "bounds_max": [float(value) for value in self.bounds_max],
            "voxel_shape": [int(value) for value in self.shape],
            "voxel_count": int(self.voxel_count),
            "surface_cells": len(self.surface_cells),
            "triangle_count": int(self.triangle_count),
            "surface_sample_count": int(self.surface_sample_count),
            "sampling_truncated": bool(self.sampling_truncated),
        }

    def _indices_for_axis(
        self,
        lower: float,
        upper: float,
        *,
        axis: int,
    ) -> tuple[int, ...]:
        first = max(
            0,
            int(math.floor((float(lower) - self.origin[axis]) / self.voxel_size_m)),
        )
        last = min(
            self.shape[axis] - 1,
            int(math.floor((float(upper) - self.origin[axis]) / self.voxel_size_m)),
        )
        if last < first:
            return ()
        return tuple(range(first, last + 1))


@dataclass(frozen=True)
class CurvatureGuidedVoxelAnalysis:
    """Explain the result of one bounded curvature-guided voxel attempt."""

    profile: CurvatureProfile
    volume: LocalVoxelVolume | None
    outcome: str
    selected_regions: tuple[CurvatureRegion, ...] = ()
    region_point_count: int = 0
    bounds_min: Point | None = None
    bounds_max: Point | None = None
    triangle_count: int = 0
    surface_sample_count: int = 0
    sampling_truncated: bool = False

    def diagnostic_payload(self) -> dict[str, object]:
        """Return bounded construction diagnostics, including null outcomes."""
        return {
            "outcome": str(self.outcome),
            "built": self.volume is not None,
            "selected_region_count": len(self.selected_regions),
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
                for region in self.selected_regions
            ],
            "region_point_count": int(self.region_point_count),
            "bounds_min": (
                None
                if self.bounds_min is None
                else [float(value) for value in self.bounds_min]
            ),
            "bounds_max": (
                None
                if self.bounds_max is None
                else [float(value) for value in self.bounds_max]
            ),
            "triangle_count": int(self.triangle_count),
            "surface_sample_count": int(self.surface_sample_count),
            "sampling_truncated": bool(self.sampling_truncated),
            "volume": (
                None
                if self.volume is None
                else self.volume.diagnostic_payload()
            ),
        }


def analyze_curvature_guided_voxel_volume(
    points: Sequence[Sequence[float]],
    *,
    triangle_provider: TriangleProvider,
    voxel_size_m: float = DEFAULT_VOXEL_SIZE_M,
    curvature_rank_threshold: int = DEFAULT_VOXEL_CURVATURE_RANK_THRESHOLD,
    max_regions: int = DEFAULT_VOXEL_MAX_REGIONS,
    max_distance_m: float = DEFAULT_VOXEL_MAX_DISTANCE_M,
    padding_m: float | None = None,
    max_voxels: int = DEFAULT_VOXEL_MAX_CELLS,
    window_points: int = 3,
) -> CurvatureGuidedVoxelAnalysis:
    """Analyze curvature and return an explicit bounded voxel outcome."""
    profile = analyze_polyline_curvature(points, window_points=window_points)
    regions = select_curvature_regions(
        profile,
        minimum_rank=curvature_rank_threshold,
        max_regions=max_regions,
        max_start_distance_m=max_distance_m,
    )
    if not regions:
        return CurvatureGuidedVoxelAnalysis(
            profile=profile,
            volume=None,
            outcome=VOXEL_ANALYSIS_OUTCOME_NO_CURVATURE_REGION,
        )

    region_points = _points_for_regions(points, regions, max_distance_m, profile)
    if not region_points:
        return CurvatureGuidedVoxelAnalysis(
            profile=profile,
            volume=None,
            outcome=VOXEL_ANALYSIS_OUTCOME_NO_REGION_POINTS,
            selected_regions=regions,
        )
    size = float(voxel_size_m)
    if not math.isfinite(size) or size <= 0.0:
        raise ValueError("voxel size must be a positive finite number")
    padding = max(size * 2.0, 0.0 if padding_m is None else float(padding_m))
    bounds_min = tuple(
        min(float(point[axis]) for point in region_points) - padding
        for axis in range(3)
    )
    bounds_max = tuple(
        max(float(point[axis]) for point in region_points) + padding
        for axis in range(3)
    )
    triangle_meshes = triangle_provider(bounds_min, bounds_max)
    volume = build_surface_voxel_volume(
        triangle_meshes,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        config=VoxelVolumeConfig(
            voxel_size_m=size,
            max_voxels=max_voxels,
        ),
    )
    if volume.triangle_count == 0:
        outcome = VOXEL_ANALYSIS_OUTCOME_NO_TRIANGLES
        usable_volume = None
    elif volume.surface_sample_count == 0:
        outcome = VOXEL_ANALYSIS_OUTCOME_NO_SURFACE_SAMPLES
        usable_volume = None
    else:
        outcome = VOXEL_ANALYSIS_OUTCOME_BUILT
        usable_volume = volume
    return CurvatureGuidedVoxelAnalysis(
        profile=profile,
        volume=usable_volume,
        outcome=outcome,
        selected_regions=regions,
        region_point_count=len(region_points),
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        triangle_count=int(volume.triangle_count),
        surface_sample_count=int(volume.surface_sample_count),
        sampling_truncated=bool(volume.sampling_truncated),
    )


def build_curvature_guided_voxel_volume(
    points: Sequence[Sequence[float]],
    *,
    triangle_provider: TriangleProvider,
    voxel_size_m: float = DEFAULT_VOXEL_SIZE_M,
    curvature_rank_threshold: int = DEFAULT_VOXEL_CURVATURE_RANK_THRESHOLD,
    max_regions: int = DEFAULT_VOXEL_MAX_REGIONS,
    max_distance_m: float = DEFAULT_VOXEL_MAX_DISTANCE_M,
    padding_m: float | None = None,
    max_voxels: int = DEFAULT_VOXEL_MAX_CELLS,
    window_points: int = 3,
) -> tuple[CurvatureProfile, LocalVoxelVolume | None]:
    """Analyze curvature and build a bounded volume around selected regions."""
    analysis = analyze_curvature_guided_voxel_volume(
        points,
        triangle_provider=triangle_provider,
        voxel_size_m=voxel_size_m,
        curvature_rank_threshold=curvature_rank_threshold,
        max_regions=max_regions,
        max_distance_m=max_distance_m,
        padding_m=padding_m,
        max_voxels=max_voxels,
        window_points=window_points,
    )
    return analysis.profile, analysis.volume


def build_surface_voxel_volume(
    triangle_meshes: Iterable[np.ndarray],
    *,
    bounds_min: Sequence[float],
    bounds_max: Sequence[float],
    config: VoxelVolumeConfig | None = None,
) -> LocalVoxelVolume:
    """Rasterize cached triangle surfaces into a bounded sparse voxel field."""
    resolved = (config or VoxelVolumeConfig()).validated()
    lower = _point(bounds_min)
    upper = _point(bounds_max)
    if any(upper[index] <= lower[index] for index in range(3)):
        raise ValueError("voxel bounds must have positive extent")
    voxel_size, origin, shape = _fit_volume_geometry(lower, upper, resolved)
    blocked: set[VoxelIndex] = set()
    triangle_count = 0
    sample_count = 0
    sampling_truncated = False
    for mesh in triangle_meshes:
        triangles = _normalise_triangles(mesh)
        if triangles is None:
            continue
        for triangle in triangles:
            triangle_lower = triangle.min(axis=0)
            triangle_upper = triangle.max(axis=0)
            if not _aabb_intersects(
                triangle_lower,
                triangle_upper,
                np.asarray(lower, dtype=np.float64),
                np.asarray(upper, dtype=np.float64),
            ):
                continue
            triangle_count += 1
            steps = _triangle_steps(triangle, voxel_size)
            for sample in _triangle_samples(triangle, steps):
                if sample_count >= resolved.max_surface_samples:
                    sampling_truncated = True
                    break
                index = _voxel_index(sample, origin, voxel_size)
                if not _contains_index(index, shape):
                    continue
                sample_count += 1
                _add_inflated_cell(
                    blocked,
                    index,
                    shape=shape,
                    inflation=resolved.surface_inflation_cells,
                )
            if sampling_truncated:
                break
        if sampling_truncated:
            break
    return LocalVoxelVolume(
        voxel_size_m=voxel_size,
        origin=origin,
        shape=shape,
        surface_cells=frozenset(blocked),
        triangle_count=triangle_count,
        surface_sample_count=sample_count,
        sampling_truncated=sampling_truncated,
        max_clearance_search_cells=resolved.max_clearance_search_cells,
    )


def _points_for_regions(
    points: Sequence[Sequence[float]],
    regions: Sequence[CurvatureRegion],
    max_distance_m: float,
    profile: CurvatureProfile,
) -> tuple[Point, ...]:
    normalized = tuple(_point(point) for point in points)
    selected: set[int] = set()
    for region in regions:
        start = max(0, int(region.start_index))
        end = min(len(normalized) - 1, int(region.end_index))
        for index in range(start, end + 1):
            if profile.cumulative_distances_m[index] <= float(max_distance_m):
                selected.add(index)
    return tuple(normalized[index] for index in sorted(selected))


def _point(value: Sequence[float]) -> Point:
    if len(value) != 3:
        raise ValueError("voxel points must be three-dimensional")
    point = (float(value[0]), float(value[1]), float(value[2]))
    if not all(math.isfinite(coordinate) for coordinate in point):
        raise ValueError("voxel points must be finite")
    return point


def _fit_volume_geometry(
    bounds_min: Point,
    bounds_max: Point,
    config: VoxelVolumeConfig,
) -> tuple[float, Point, tuple[int, int, int]]:
    size = float(config.voxel_size_m)
    while True:
        origin = tuple(
            math.floor(bounds_min[axis] / size) * size
            for axis in range(3)
        )  # type: ignore[assignment]
        shape = tuple(
            max(
                1,
                int(math.ceil((bounds_max[axis] - origin[axis]) / size)) + 1,
            )
            for axis in range(3)
        )
        count = shape[0] * shape[1] * shape[2]
        if count <= config.max_voxels:
            return size, origin, shape  # type: ignore[return-value]
        size *= max(1.05, (count / config.max_voxels) ** (1.0 / 3.0))


def _normalise_triangles(mesh: np.ndarray) -> np.ndarray | None:
    triangles = np.asarray(mesh, dtype=np.float64)
    if triangles.ndim == 2 and triangles.shape[1] == 3:
        if len(triangles) % 3 != 0:
            return None
        triangles = triangles.reshape(-1, 3, 3)
    if triangles.ndim != 3 or triangles.shape[1:] != (3, 3):
        return None
    if not np.all(np.isfinite(triangles)):
        return None
    return triangles


def _triangle_steps(triangle: np.ndarray, voxel_size: float) -> int:
    edge_lengths = [
        float(np.linalg.norm(triangle[(index + 1) % 3] - triangle[index]))
        for index in range(3)
    ]
    return max(1, min(64, int(math.ceil(max(edge_lengths) / voxel_size))))


def _triangle_samples(triangle: np.ndarray, steps: int) -> Iterable[np.ndarray]:
    first, second, third = triangle
    denominator = float(max(1, steps))
    for first_index in range(steps + 1):
        for second_index in range(steps + 1 - first_index):
            third_index = steps - first_index - second_index
            yield (
                first * (first_index / denominator)
                + second * (second_index / denominator)
                + third * (third_index / denominator)
            )


def _voxel_index(point: Sequence[float], origin: Point, size: float) -> VoxelIndex:
    return tuple(
        int(math.floor((float(point[axis]) - origin[axis]) / size))
        for axis in range(3)
    )  # type: ignore[return-value]


def _contains_index(index: VoxelIndex, shape: tuple[int, int, int]) -> bool:
    return all(0 <= index[axis] < shape[axis] for axis in range(3))


def _add_inflated_cell(
    blocked: set[VoxelIndex],
    index: VoxelIndex,
    *,
    shape: tuple[int, int, int],
    inflation: int,
) -> None:
    radius = max(0, int(inflation))
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                candidate = (index[0] + dx, index[1] + dy, index[2] + dz)
                if _contains_index(candidate, shape):
                    blocked.add(candidate)


def _shell_indices(center: VoxelIndex, radius: int) -> Iterable[VoxelIndex]:
    if radius == 0:
        yield center
        return
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                if max(abs(dx), abs(dy), abs(dz)) != radius:
                    continue
                yield (center[0] + dx, center[1] + dy, center[2] + dz)


def _sample_axis_product(
    x_indices: Sequence[int],
    y_indices: Sequence[int],
    z_indices: Sequence[int],
    *,
    max_candidates: int,
) -> Iterable[VoxelIndex]:
    total = len(x_indices) * len(y_indices) * len(z_indices)
    stride = max(1, int(math.ceil((total / max_candidates) ** (1.0 / 3.0))))
    sampled_x = _strided_indices(x_indices, stride)
    sampled_y = _strided_indices(y_indices, stride)
    sampled_z = _strided_indices(z_indices, stride)
    return (
        (x_index, y_index, z_index)
        for x_index in sampled_x
        for y_index in sampled_y
        for z_index in sampled_z
    )


def _strided_indices(indices: Sequence[int], stride: int) -> tuple[int, ...]:
    if len(indices) <= 1 or stride <= 1:
        return tuple(indices)
    sampled = list(indices[::stride])
    if sampled[-1] != indices[-1]:
        sampled.append(indices[-1])
    return tuple(sampled)


def _aabb_intersects(
    first_min: np.ndarray,
    first_max: np.ndarray,
    second_min: np.ndarray,
    second_max: np.ndarray,
) -> bool:
    return bool(
        np.all(first_max >= second_min)
        and np.all(second_max >= first_min)
    )
