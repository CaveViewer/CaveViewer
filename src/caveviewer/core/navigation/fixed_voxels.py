"""Fixed-resolution orthogonal surface fields for navigation caches.

The legacy voxel builder enlarges its voxel size when a requested region does
not fit a dense-cell budget.  Current caches must never do that. This module instead
subdivides aligned world regions until every local field fits while retaining
the requested per-axis voxel size. Surface-sampling exhaustion triggers
further spatial subdivision and ultimately fails closed; a truncated field is
never returned for publication.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
import math

from caveviewer.core.navigation.centerline import Point
from caveviewer.core.navigation.voxel_volume import (
    LocalVoxelVolume,
    TriangleProvider,
    VoxelVolumeConfig,
    build_surface_voxel_volume,
)


FIXED_ORTHOGONAL_VOXEL_METHOD = "fixed_orthogonal_voxel_chunks_v2"


def segment_voxel_probe_fractions(
    first: Sequence[float],
    second: Sequence[float],
    *,
    lattice_spacing_m: float | Sequence[float],
    maximum_sample_spacing_m: float,
) -> tuple[float, ...]:
    """Return partition-invariant samples for a fixed voxel lattice.

    Uniform distance samples are not enough for diagonal motion: a segment
    can clip a voxel for less than the sampling interval. Include every
    crossed lattice plane and one point inside every interval between those
    crossings. Consequently, splitting the same camera segment into runtime
    checkpoints cannot reveal an occupied voxel that preflight skipped.

    Endpoints are intentionally omitted because segment validators already
    probe them explicitly.
    """
    start = _finite_point(first, "segment start")
    stop = _finite_point(second, "segment end")
    if isinstance(lattice_spacing_m, Sequence) and not isinstance(
        lattice_spacing_m,
        (str, bytes),
    ):
        if len(lattice_spacing_m) != 3:
            raise ValueError("voxel lattice size must contain three axes")
        lattice_sizes = tuple(
            _positive_finite(value, "voxel lattice axis size")
            for value in lattice_spacing_m
        )
    else:
        lattice = _positive_finite(
            lattice_spacing_m,
            "voxel lattice spacing",
        )
        lattice_sizes = (lattice, lattice, lattice)
    sample_spacing = _positive_finite(
        maximum_sample_spacing_m,
        "segment probe sample spacing",
    )

    distance_m = math.dist(start, stop)
    sample_count = max(1, int(math.ceil(distance_m / sample_spacing)))
    fractions = {
        float(index) / float(sample_count)
        for index in range(1, sample_count)
    }
    boundary_fractions: set[float] = set()
    for axis in range(3):
        lattice = lattice_sizes[axis]
        axis_start = float(start[axis])
        axis_stop = float(stop[axis])
        delta = axis_stop - axis_start
        if abs(delta) <= 1e-12:
            continue
        lower = min(axis_start, axis_stop)
        upper = max(axis_start, axis_stop)
        first_boundary_index = int(math.floor(lower / lattice)) + 1
        last_boundary_index = int(math.ceil(upper / lattice)) - 1
        for boundary_index in range(
            first_boundary_index,
            last_boundary_index + 1,
        ):
            boundary = float(boundary_index) * lattice
            fraction = (boundary - axis_start) / delta
            if 1e-12 < fraction < 1.0 - 1e-12:
                boundary_fractions.add(float(fraction))

    fractions.update(boundary_fractions)
    interval_boundaries = (0.0, *sorted(boundary_fractions), 1.0)
    fractions.update(
        (lower + upper) * 0.5
        for lower, upper in zip(
            interval_boundaries,
            interval_boundaries[1:],
            strict=False,
        )
        if upper - lower > 1e-12
    )
    return tuple(
        sorted(
            fraction
            for fraction in fractions
            if 1e-12 < fraction < 1.0 - 1e-12
        )
    )


@dataclass(frozen=True)
class FixedVoxelRegion:
    """One world-space region and its candidate inside-space seeds."""

    bounds_min: Point
    bounds_max: Point
    seed_points: tuple[Point, ...] = ()


@dataclass(frozen=True)
class FixedVoxelTileBuildResult:
    """Complete fixed-resolution tiles and bounded build diagnostics."""

    tiles: tuple[LocalVoxelVolume, ...]
    tile_seed_points: tuple[tuple[Point, ...], ...]
    details: dict[str, object]


def build_fixed_orthogonal_voxel_tiles(
    regions: Sequence[FixedVoxelRegion],
    *,
    triangle_provider: TriangleProvider,
    voxel_size_m: float = 1.0,
    vertical_voxel_size_m: float = 0.25,
    chunk_edge_m: float = 32.0,
    max_chunks: int = 4_096,
    max_voxels_per_chunk: int = 65_536,
    max_surface_samples_per_chunk: int = 250_000,
    surface_inflation_cells: int = 0,
) -> FixedVoxelTileBuildResult:
    """Rasterize regions without changing their requested X/Y/Z resolution.

    Regions are aligned outward to the global voxel lattice, then split along
    cell boundaries.  A tile is retained when it has surface evidence or a
    seed point.  Empty unseeded tiles are discarded.  Overlap at chunk seams
    is intentional so a wall sampled on either side remains visible to both
    local probes.
    """
    horizontal_size = _positive_finite(voxel_size_m, "voxel size")
    vertical_size = _positive_finite(
        vertical_voxel_size_m,
        "vertical voxel size",
    )
    cell_size = (horizontal_size, vertical_size, horizontal_size)
    edge = _positive_finite(chunk_edge_m, "chunk edge")
    chunk_limit = max(1, int(max_chunks))
    voxel_limit = max(8, int(max_voxels_per_chunk))
    sample_limit = max(1, int(max_surface_samples_per_chunk))
    inflation = max(0, int(surface_inflation_cells))
    edge_cells = tuple(
        max(1, int(math.floor(edge / axis_size + 1e-9)))
        for axis_size in cell_size
    )

    queue: deque[FixedVoxelRegion] = deque()
    for region in regions:
        normalized = _aligned_region(region, cell_size=cell_size)
        queue.extend(
            _split_to_edge_limit(
                normalized,
                cell_size=cell_size,
                edge_cells=edge_cells,
            )
        )
    if len(queue) > chunk_limit:
        raise ValueError("fixed navigation voxel chunk limit exceeded")

    tiles: list[LocalVoxelVolume] = []
    seeds_by_tile: list[tuple[Point, ...]] = []
    attempted_count = 0
    discarded_empty_count = 0
    subdivision_count = 0
    total_triangle_count = 0
    total_surface_sample_count = 0
    while queue:
        if len(tiles) + len(queue) > chunk_limit:
            raise ValueError("fixed navigation voxel chunk limit exceeded")
        region = queue.popleft()
        shape = _predicted_shape(region, cell_size=cell_size)
        if math.prod(shape) > voxel_limit:
            subdivisions = _split_region(region, cell_size=cell_size)
            if subdivisions is None:
                raise ValueError(
                    "fixed navigation voxel region cannot fit chunk capacity"
                )
            queue.extendleft(reversed(subdivisions))
            subdivision_count += 1
            continue

        attempted_count += 1
        tile = build_surface_voxel_volume(
            triangle_provider(region.bounds_min, region.bounds_max),
            bounds_min=region.bounds_min,
            bounds_max=region.bounds_max,
            config=VoxelVolumeConfig(
                voxel_size_m=horizontal_size,
                vertical_voxel_size_m=vertical_size,
                surface_inflation_cells=inflation,
                max_voxels=voxel_limit,
                max_surface_samples=sample_limit,
            ),
        )
        if not math.isclose(
            float(tile.voxel_size_m),
            horizontal_size,
            rel_tol=0.0,
            abs_tol=1e-7,
        ):
            raise ValueError("fixed navigation voxelizer changed resolution")
        if not math.isclose(
            float(tile.vertical_voxel_size_m),
            vertical_size,
            rel_tol=0.0,
            abs_tol=1e-7,
        ):
            raise ValueError(
                "fixed navigation voxelizer changed vertical resolution"
            )
        if tile.sampling_truncated:
            subdivisions = _split_region(region, cell_size=cell_size)
            if subdivisions is None:
                raise ValueError(
                    "fixed navigation voxel surface sampling remains truncated"
                )
            queue.extendleft(reversed(subdivisions))
            subdivision_count += 1
            continue

        tile_seeds = _seed_points_for_region(
            region.seed_points,
            bounds_min=region.bounds_min,
            bounds_max=region.bounds_max,
        )
        if tile.triangle_count <= 0 and not tile_seeds:
            discarded_empty_count += 1
            continue
        tiles.append(tile)
        seeds_by_tile.append(tile_seeds)
        total_triangle_count += int(tile.triangle_count)
        total_surface_sample_count += int(tile.surface_sample_count)

    return FixedVoxelTileBuildResult(
        tiles=tuple(tiles),
        tile_seed_points=tuple(seeds_by_tile),
        details={
            "method": FIXED_ORTHOGONAL_VOXEL_METHOD,
            "voxel_size_m": float(horizontal_size),
            "vertical_voxel_size_m": float(vertical_size),
            "cell_size_m": [float(value) for value in cell_size],
            "chunk_edge_m": [
                float(edge_cells[axis] * cell_size[axis])
                for axis in range(3)
            ],
            "chunk_count": len(tiles),
            "attempted_chunk_count": int(attempted_count),
            "subdivision_count": int(subdivision_count),
            "discarded_empty_chunk_count": int(discarded_empty_count),
            "max_chunks": int(chunk_limit),
            "max_voxels_per_chunk": int(voxel_limit),
            "max_surface_samples_per_chunk": int(sample_limit),
            "surface_inflation_cells": int(inflation),
            "sampling_truncated": False,
            "triangle_count": int(total_triangle_count),
            "surface_sample_count": int(total_surface_sample_count),
        },
    )


def _aligned_region(
    region: FixedVoxelRegion,
    *,
    cell_size: tuple[float, float, float],
) -> FixedVoxelRegion:
    lower = _finite_point(region.bounds_min, "region lower bound")
    upper = _finite_point(region.bounds_max, "region upper bound")
    if any(upper[axis] <= lower[axis] for axis in range(3)):
        raise ValueError("fixed navigation voxel region has no positive extent")
    aligned_lower = tuple(
        math.floor(lower[axis] / cell_size[axis]) * cell_size[axis]
        for axis in range(3)
    )
    aligned_upper = tuple(
        math.ceil(upper[axis] / cell_size[axis]) * cell_size[axis]
        for axis in range(3)
    )
    seeds = tuple(
        _finite_point(point, "region seed point")
        for point in region.seed_points
    )
    return FixedVoxelRegion(
        bounds_min=aligned_lower,  # type: ignore[arg-type]
        bounds_max=aligned_upper,  # type: ignore[arg-type]
        seed_points=seeds,
    )


def _split_to_edge_limit(
    region: FixedVoxelRegion,
    *,
    cell_size: tuple[float, float, float],
    edge_cells: tuple[int, int, int],
) -> tuple[FixedVoxelRegion, ...]:
    intervals = []
    for axis in range(3):
        axis_size = cell_size[axis]
        first = int(round(region.bounds_min[axis] / axis_size))
        stop = int(round(region.bounds_max[axis] / axis_size))
        intervals.append(
            tuple(
                (
                    index * axis_size,
                    min(stop, index + edge_cells[axis]) * axis_size,
                )
                for index in range(first, stop, edge_cells[axis])
            )
        )
    result = []
    for x_bounds in intervals[0]:
        for y_bounds in intervals[1]:
            for z_bounds in intervals[2]:
                lower = (x_bounds[0], y_bounds[0], z_bounds[0])
                upper = (x_bounds[1], y_bounds[1], z_bounds[1])
                result.append(
                    FixedVoxelRegion(
                        bounds_min=lower,
                        bounds_max=upper,
                        seed_points=_seed_points_for_region(
                            region.seed_points,
                            bounds_min=lower,
                            bounds_max=upper,
                        ),
                    )
                )
    return tuple(result)


def _predicted_shape(
    region: FixedVoxelRegion,
    *,
    cell_size: tuple[float, float, float],
) -> tuple[int, int, int]:
    return tuple(  # type: ignore[return-value]
        max(
            1,
            int(
                math.ceil(
                    (region.bounds_max[axis] - region.bounds_min[axis])
                    / cell_size[axis]
                )
            )
            + 1,
        )
        for axis in range(3)
    )


def _split_region(
    region: FixedVoxelRegion,
    *,
    cell_size: tuple[float, float, float],
) -> tuple[FixedVoxelRegion, FixedVoxelRegion] | None:
    cell_counts = tuple(
        max(
            1,
            int(
                round(
                    (region.bounds_max[axis] - region.bounds_min[axis])
                    / cell_size[axis]
                )
            ),
        )
        for axis in range(3)
    )
    axis = max(range(3), key=lambda value: (cell_counts[value], -value))
    if cell_counts[axis] <= 1:
        return None
    first_count = cell_counts[axis] // 2
    split = region.bounds_min[axis] + first_count * cell_size[axis]
    first_upper = list(region.bounds_max)
    first_upper[axis] = split
    second_lower = list(region.bounds_min)
    second_lower[axis] = split
    first_bounds_max = tuple(first_upper)
    second_bounds_min = tuple(second_lower)
    return (
        FixedVoxelRegion(
            bounds_min=region.bounds_min,
            bounds_max=first_bounds_max,  # type: ignore[arg-type]
            seed_points=_seed_points_for_region(
                region.seed_points,
                bounds_min=region.bounds_min,
                bounds_max=first_bounds_max,
            ),
        ),
        FixedVoxelRegion(
            bounds_min=second_bounds_min,  # type: ignore[arg-type]
            bounds_max=region.bounds_max,
            seed_points=_seed_points_for_region(
                region.seed_points,
                bounds_min=second_bounds_min,
                bounds_max=region.bounds_max,
            ),
        ),
    )


def _seed_points_for_region(
    points: Sequence[Point],
    *,
    bounds_min: Sequence[float],
    bounds_max: Sequence[float],
) -> tuple[Point, ...]:
    return tuple(
        point
        for point in points
        if all(
            float(bounds_min[axis]) - 1e-9
            <= float(point[axis])
            <= float(bounds_max[axis]) + 1e-9
            for axis in range(3)
        )
    )


def _positive_finite(value: float, name: str) -> float:
    resolved = float(value)
    if not math.isfinite(resolved) or resolved <= 0.0:
        raise ValueError(f"fixed navigation {name} must be positive and finite")
    return resolved


def _finite_point(value: Sequence[float], name: str) -> Point:
    if len(value) != 3:
        raise ValueError(f"fixed navigation {name} must contain three values")
    try:
        point = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"fixed navigation {name} is malformed") from exc
    if not all(math.isfinite(item) for item in point):
        raise ValueError(f"fixed navigation {name} must be finite")
    return point  # type: ignore[return-value]
