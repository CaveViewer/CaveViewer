"""Sparse local voxel analysis for curvature-guided navigation recovery.

This module is deliberately independent of the centerline implementation. It
builds a bounded surface-distance field from cached triangle meshes and offers
point refinement inside an existing navigation cell. Exact triangle collision
checks remain authoritative; the voxel field is an inexpensive local guide
that can be removed or replaced without changing centerline policy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from collections import deque
from dataclasses import dataclass, field
import heapq
import math
import time

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

_LOCAL_26_NEIGHBOR_OFFSETS = tuple(
    (dx, dy, dz)
    for dx in (-1, 0, 1)
    for dy in (-1, 0, 1)
    for dz in (-1, 0, 1)
    if (dx, dy, dz) != (0, 0, 0)
)
_LOCAL_CARDINAL_NEIGHBOR_OFFSETS = tuple(
    offset
    for offset in _LOCAL_26_NEIGHBOR_OFFSETS
    if sum(abs(value) for value in offset) == 1
)

DEFAULT_VOXEL_SIZE_M = 1.0
DEFAULT_VOXEL_CURVATURE_RANK_THRESHOLD = 65
DEFAULT_VOXEL_MAX_REGIONS = 2
DEFAULT_VOXEL_MAX_DISTANCE_M = 256.0
DEFAULT_VOXEL_MAX_CELLS = 120_000
DEFAULT_VOXEL_MAX_SURFACE_SAMPLES = 200_000
DEFAULT_VOXEL_SURFACE_INFLATION_CELLS = 1
DEFAULT_VOXEL_MAX_CLEARANCE_SEARCH_CELLS = 16
DEFAULT_VOXEL_LOCAL_REFINEMENT_RADIUS_M = 16.0
DEFAULT_VOXEL_LOCAL_REFINEMENT_FORWARD_M = 32.0
DEFAULT_VOXEL_LOCAL_REFINEMENT_MAX_CELLS = 65_536
_RUNTIME_CLEARANCE_CACHE_MAX_ENTRIES = 4096

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
VOXEL_ANALYSIS_OUTCOME_CACHE_HIT = "cache_hit"
VOXEL_ANALYSIS_OUTCOME_CACHE_MISS = "cache_miss"


@dataclass(frozen=True)
class VoxelVolumeConfig:
    """Bounded local voxel construction parameters."""

    voxel_size_m: float = DEFAULT_VOXEL_SIZE_M
    # X and Z retain ``voxel_size_m`` for compatibility.  A separate Y size
    # lets cache generation preserve sub-metre-high passages without paying
    # the cubic cost of shrinking every axis.
    vertical_voxel_size_m: float | None = None
    surface_inflation_cells: int = DEFAULT_VOXEL_SURFACE_INFLATION_CELLS
    max_voxels: int = DEFAULT_VOXEL_MAX_CELLS
    max_surface_samples: int = DEFAULT_VOXEL_MAX_SURFACE_SAMPLES
    max_clearance_search_cells: int = (
        DEFAULT_VOXEL_MAX_CLEARANCE_SEARCH_CELLS
    )

    def validated(self) -> "VoxelVolumeConfig":
        size = float(self.voxel_size_m)
        if not math.isfinite(size) or size <= 0.0:
            raise ValueError("voxel size must be a positive finite number")
        vertical_size = float(
            size
            if self.vertical_voxel_size_m is None
            else self.vertical_voxel_size_m
        )
        if not math.isfinite(vertical_size) or vertical_size <= 0.0:
            raise ValueError(
                "vertical voxel size must be a positive finite number"
            )
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
            vertical_voxel_size_m=vertical_size,
            surface_inflation_cells=int(self.surface_inflation_cells),
            max_voxels=int(self.max_voxels),
            max_surface_samples=int(self.max_surface_samples),
            max_clearance_search_cells=int(self.max_clearance_search_cells),
        )


@dataclass(frozen=True)
class LocalVoxelRoute:
    """A bounded route found in a fine local voxel field."""

    points: tuple[Point, ...]
    indices: tuple[VoxelIndex, ...]
    explored_voxel_count: int
    free_voxel_count: int
    branch_free_voxel_count: int
    target_connectivity: int
    target_clearance_m: float
    forward_progress_m: float
    distance_m: float
    vertical_change_m: float
    unknown_boundary_count: int
    boundary_reached: bool
    search_truncated: bool = False

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "point_count": len(self.points),
            "voxel_count": len(self.indices),
            "explored_voxel_count": int(self.explored_voxel_count),
            "free_voxel_count": int(self.free_voxel_count),
            "branch_free_voxel_count": int(self.branch_free_voxel_count),
            "target_connectivity": int(self.target_connectivity),
            "target_clearance_m": float(self.target_clearance_m),
            "forward_progress_m": float(self.forward_progress_m),
            "distance_m": float(self.distance_m),
            "vertical_change_m": float(self.vertical_change_m),
            "unknown_boundary_count": int(self.unknown_boundary_count),
            "boundary_reached": bool(self.boundary_reached),
            "search_truncated": bool(self.search_truncated),
            "first_points": [
                [float(value) for value in point]
                for point in self.points[:8]
            ],
            "last_points": [
                [float(value) for value in point]
                for point in self.points[-8:]
            ],
        }


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
    vertical_voxel_size_m: float | None = None
    _runtime_clearance_cache: dict[VoxelIndex, float] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Create a bounded runtime cache for repeated probe samples."""
        horizontal_size = float(self.voxel_size_m)
        vertical_size = float(
            horizontal_size
            if self.vertical_voxel_size_m is None
            else self.vertical_voxel_size_m
        )
        if (
            not math.isfinite(horizontal_size)
            or horizontal_size <= 0.0
            or not math.isfinite(vertical_size)
            or vertical_size <= 0.0
        ):
            raise ValueError("voxel cell sizes must be positive and finite")
        object.__setattr__(self, "voxel_size_m", horizontal_size)
        object.__setattr__(self, "vertical_voxel_size_m", vertical_size)
        object.__setattr__(self, "_runtime_clearance_cache", {})

    @property
    def cell_size_m(self) -> tuple[float, float, float]:
        """Return the physical X/Y/Z size of one orthogonal cell."""
        return (
            float(self.voxel_size_m),
            float(self.vertical_voxel_size_m),
            float(self.voxel_size_m),
        )

    @property
    def cell_volume_m3(self) -> float:
        """Return the physical volume represented by one cell."""
        x_size, y_size, z_size = self.cell_size_m
        return float(x_size * y_size * z_size)

    @property
    def cell_diagonal_m(self) -> float:
        """Return the physical diagonal of one cell."""
        return float(math.sqrt(sum(size * size for size in self.cell_size_m)))

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
            self.origin[index] + self.shape[index] * self.cell_size_m[index]
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
            _stable_floor_grid_coordinate(
                (float(point[axis]) - self.origin[axis])
                / self.cell_size_m[axis]
            )
            for axis in range(3)
        )  # type: ignore[return-value]

    def voxel_center(self, index: VoxelIndex) -> Point:
        """Return the world-space center of a voxel."""
        return tuple(
            self.origin[axis]
            + (int(index[axis]) + 0.5) * self.cell_size_m[axis]
            for axis in range(3)
        )  # type: ignore[return-value]

    def point_is_surface_occupied(self, point: Sequence[float]) -> bool:
        """Return whether a point falls in an inflated sampled surface voxel."""
        if not self.contains_point(point):
            return False
        return self.voxel_index(point) in self.surface_cells

    def probe_point(
        self,
        point: Sequence[float],
        *,
        include_clearance: bool = True,
    ) -> tuple[bool, float] | None:
        """Return ``(free, clearance_m)`` for a cached point, if covered.

        This is intentionally a surface-field query rather than a second
        flood fill. Runtime recovery samples short connected probe segments;
        the exact mesh guard remains authoritative for the selected route.
        Coarse callers can disable clearance to avoid a repeated distance-field
        search while retaining the free/occupied result.
        ``None`` means that this local volume does not cover the point.
        """
        if not self.contains_point(point):
            return None
        index = self.voxel_index(point)
        if index in self.surface_cells:
            return False, 0.0
        return True, (
            float(self.surface_clearance_m(index))
            if include_clearance
            else 0.0
        )

    def find_forward_route(
        self,
        current: Sequence[float],
        forward: Sequence[float],
        *,
        max_distance_m: float = DEFAULT_VOXEL_LOCAL_REFINEMENT_FORWARD_M,
        max_nodes: int = DEFAULT_VOXEL_LOCAL_REFINEMENT_MAX_CELLS,
        min_target_distance_m: float = 4.0,
        deadline_monotonic_s: float | None = None,
        edge_safety_check: Callable[[Point, Point], bool] | None = None,
        allow_diagonal: bool = True,
    ) -> "LocalVoxelRoute | None":
        """Find a bounded, heading-aware route through fine free voxels.

        This search deliberately operates on the local surface field rather
        than the coarse whole-cave graph. It explores every 26-connected
        neighbor in the current forward hemisphere, preserves vertical
        movement, and ranks branches by forward reach, continuation volume,
        and local connectivity. Unknown cells remain boundaries; they are
        never treated as free route nodes.
        """
        try:
            current_point = tuple(float(value) for value in current)
            forward_values = tuple(float(value) for value in forward)
        except (TypeError, ValueError):
            return None
        if (
            len(current_point) != 3
            or len(forward_values) != 3
            or not all(math.isfinite(value) for value in current_point)
            or not all(math.isfinite(value) for value in forward_values)
        ):
            return None
        forward_norm = math.sqrt(sum(value * value for value in forward_values))
        if forward_norm <= 1e-9:
            return None
        forward_unit = tuple(value / forward_norm for value in forward_values)
        size = max(1e-6, float(self.voxel_size_m))
        max_distance = max(size * 3.0, float(max_distance_m))
        node_limit = max(64, int(max_nodes))
        target_distance = max(size * 3.0, float(min_target_distance_m))
        deadline = (
            None
            if deadline_monotonic_s is None
            else float(deadline_monotonic_s)
        )
        if deadline is not None and not math.isfinite(deadline):
            deadline = None
        try:
            start = self.voxel_index(current_point)
        except (TypeError, ValueError):
            return None
        if not self.contains_index(start):
            return None

        queue: deque[tuple[VoxelIndex, VoxelIndex | None]] = deque(
            [(start, None)]
        )
        previous: dict[VoxelIndex, VoxelIndex | None] = {start: None}
        branch_stats: dict[VoxelIndex, dict[str, float | int | bool]] = {}
        branch_targets: dict[VoxelIndex, tuple[tuple[float, ...], VoxelIndex]] = {}
        best_branch_targets: dict[
            VoxelIndex,
            tuple[tuple[float, ...], VoxelIndex],
        ] = {}
        free_nodes = 0
        unknown_boundary_count = 0
        boundary_reached = False
        search_truncated = False
        expanded_nodes = 0
        surface_cells = self.surface_cells
        neighbor_offsets = (
            _LOCAL_26_NEIGHBOR_OFFSETS
            if allow_diagonal
            else _LOCAL_CARDINAL_NEIGHBOR_OFFSETS
        )

        def dot_from_current(index: VoxelIndex) -> tuple[float, float, float]:
            point = self.voxel_center(index)
            return tuple(
                float(point[axis] - current_point[axis])
                for axis in range(3)
            )

        def forward_projection(delta: Sequence[float]) -> float:
            return sum(float(delta[axis]) * forward_unit[axis] for axis in range(3))

        def distance_of(delta: Sequence[float]) -> float:
            return math.sqrt(sum(float(value) * float(value) for value in delta))

        def candidate_key(
            delta: Sequence[float],
            degree: int,
        ) -> tuple[float, ...]:
            distance = distance_of(delta)
            projection = forward_projection(delta)
            return (
                projection,
                distance,
                float(degree),
            )

        while queue and len(previous) < node_limit:
            if (
                deadline is not None
                and expanded_nodes % 128 == 0
                and time.perf_counter() >= deadline
            ):
                search_truncated = True
                break
            index, branch = queue.popleft()
            expanded_nodes += 1
            delta = dot_from_current(index)
            distance = distance_of(delta)
            projection = forward_projection(delta)
            if index != start and projection < -size * 0.5:
                continue
            if not self.contains_index(index):
                unknown_boundary_count += 1
                boundary_reached = True
                continue
            if index in surface_cells and index != start:
                continue
            free_nodes += 1
            if branch is not None:
                stats = branch_stats.setdefault(
                    branch,
                    {
                        "free_count": 0,
                        "max_projection_m": 0.0,
                        "max_distance_m": 0.0,
                        "max_degree": 0,
                        "boundary_reached": False,
                    },
                )
                stats["free_count"] = int(stats["free_count"]) + 1
                stats["max_projection_m"] = max(
                    float(stats["max_projection_m"]),
                    projection,
                )
                stats["max_distance_m"] = max(
                    float(stats["max_distance_m"]),
                    distance,
                )

            free_neighbors = 0
            for offset in neighbor_offsets:
                neighbor = (
                    index[0] + offset[0],
                    index[1] + offset[1],
                    index[2] + offset[2],
                )
                if not self.contains_index(neighbor):
                    unknown_boundary_count += 1
                    boundary_reached = True
                    if branch is not None:
                        branch_stats[branch]["boundary_reached"] = True
                    continue
                neighbor_delta = dot_from_current(neighbor)
                neighbor_distance = distance_of(neighbor_delta)
                if neighbor_distance > max_distance + size * 0.75:
                    continue
                if forward_projection(neighbor_delta) < -size * 0.5:
                    continue
                # The first executable step is the camera handoff, not an
                # exploratory graph move. Keep it in the current forward
                # half-space so a local route cannot ask the controller to
                # move backward before turning around an obstacle. Once the
                # route has left the seed, the broader voxel search may use
                # lateral/vertical turns that remain globally forward.
                if index == start and forward_projection(neighbor_delta) < 0.0:
                    continue
                if neighbor in surface_cells:
                    continue
                if edge_safety_check is not None:
                    first_point = (
                        current_point
                        if index == start
                        else self.voxel_center(index)
                    )
                    second_point = self.voxel_center(neighbor)
                    if not edge_safety_check(first_point, second_point):
                        continue
                free_neighbors += 1
                if neighbor in previous:
                    continue
                next_branch = (
                    offset
                    if index == start
                    else branch
                )
                previous[neighbor] = index
                queue.append((neighbor, next_branch))

            if branch is not None:
                branch_stats[branch]["max_degree"] = max(
                    int(branch_stats[branch]["max_degree"]),
                    free_neighbors,
                )
                key = candidate_key(delta, free_neighbors)
                prior = best_branch_targets.get(branch)
                if prior is None or key > prior[0]:
                    best_branch_targets[branch] = (key, index)
                if distance >= target_distance:
                    prior = branch_targets.get(branch)
                    if prior is None or key > prior[0]:
                        branch_targets[branch] = (key, index)

        if queue and len(previous) >= node_limit:
            search_truncated = True

        target_branches = branch_targets or best_branch_targets
        if not target_branches:
            return None

        def branch_key(
            branch: VoxelIndex,
        ) -> tuple[float, ...]:
            stats = branch_stats[branch]
            free_count = int(stats["free_count"])
            volume_bonus = math.log1p(max(0, free_count)) * size * 1.5
            return (
                2.0 * float(stats["max_projection_m"])
                + 0.8 * float(stats["max_distance_m"])
                + volume_bonus
                + 0.25 * size * float(stats["max_degree"]),
                float(stats["max_projection_m"]),
                float(stats["max_distance_m"]),
                float(free_count),
            )

        selected_branch = max(target_branches, key=branch_key)
        target_index = target_branches[selected_branch][1]
        path_indices: list[VoxelIndex] = []
        index: VoxelIndex | None = target_index
        while index is not None:
            path_indices.append(index)
            index = previous.get(index)
        path_indices.reverse()
        if not path_indices or path_indices[0] != start:
            return None
        points = (current_point,) + tuple(
            self.voxel_center(index) for index in path_indices
        )
        points = _dedupe_points(points)
        if len(points) < 2:
            return None
        target_point = points[-1]
        target_result = self.probe_point(target_point, include_clearance=True)
        target_clearance = (
            0.0
            if target_result is None
            else float(target_result[1])
        )
        stats = branch_stats[selected_branch]
        return LocalVoxelRoute(
            points=points,
            indices=tuple(path_indices),
            explored_voxel_count=len(previous),
            free_voxel_count=free_nodes,
            branch_free_voxel_count=int(stats["free_count"]),
            target_connectivity=int(stats["max_degree"]),
            target_clearance_m=target_clearance,
            forward_progress_m=float(stats["max_projection_m"]),
            distance_m=float(stats["max_distance_m"]),
            vertical_change_m=float(target_point[1] - current_point[1]),
            unknown_boundary_count=unknown_boundary_count,
            boundary_reached=bool(
                boundary_reached or stats["boundary_reached"]
            ),
            search_truncated=bool(search_truncated),
        )

    def surface_clearance_m(self, index: VoxelIndex) -> float:
        """Estimate distance to the nearest sampled surface voxel."""
        if not self.contains_index(index):
            return 0.0
        cached = self._runtime_clearance_cache.get(index)
        if cached is not None:
            return float(cached)
        if not self.surface_cells:
            clearance = float(
                self.max_clearance_search_cells + 1
            ) * min(self.cell_size_m)
            self._cache_clearance(index, clearance)
            return clearance
        best_clearance = math.inf
        for radius in range(self.max_clearance_search_cells + 1):
            for candidate in _shell_indices(index, radius):
                if self.contains_index(candidate) and candidate in self.surface_cells:
                    distance_m = math.sqrt(
                        sum(
                            (
                                (candidate[axis] - index[axis])
                                * self.cell_size_m[axis]
                            )
                            ** 2
                            for axis in range(3)
                        )
                    )
                    best_clearance = min(best_clearance, distance_m)
        clearance = (
            best_clearance
            if math.isfinite(best_clearance)
            else float(self.max_clearance_search_cells + 1)
            * min(self.cell_size_m)
        )
        self._cache_clearance(index, clearance)
        return clearance

    def _cache_clearance(self, index: VoxelIndex, clearance: float) -> None:
        if len(self._runtime_clearance_cache) < _RUNTIME_CLEARANCE_CACHE_MAX_ENTRIES:
            self._runtime_clearance_cache[index] = float(clearance)

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

    def corridor_volume_metrics(
        self,
        points: Sequence[Sequence[float]],
        *,
        max_clearance_samples: int = 8192,
    ) -> dict[str, float | int | bool]:
        """Measure bounded free space reachable from route samples.

        This is a corridor-volume signal, not a watertight solid-volume
        reconstruction. Surface voxels are treated as hard barriers and a
        six-connected flood fill starts at the supplied route samples. The
        bounded local box and the route seeds keep an open cave mouth from
        turning the entire world into a candidate volume.
        """
        seed_indices = self._free_seed_indices(points)
        clearance_by_cell = self._filled_free_cell_clearance_from_seeds(
            seed_indices
        )
        if not clearance_by_cell:
            return {
                "seed_count": int(len(seed_indices)),
                "free_cell_count": 0,
                "available_volume_m3": 0.0,
                "surface_fraction": self._surface_fraction(),
                "min_clearance_m": 0.0,
                "mean_clearance_m": 0.0,
                "clearance_sample_count": 0,
                "flood_fill_truncated": False,
            }

        ordered_cells = sorted(clearance_by_cell)
        sample_limit = max(1, int(max_clearance_samples))
        stride = max(1, int(math.ceil(len(ordered_cells) / sample_limit)))
        clearance_values = [
            float(clearance_by_cell[index])
            for index in ordered_cells[::stride]
        ]
        return {
            "seed_count": int(len(seed_indices)),
            "free_cell_count": int(len(clearance_by_cell)),
            "available_volume_m3": float(
                len(clearance_by_cell) * self.cell_volume_m3
            ),
            "surface_fraction": self._surface_fraction(),
            "min_clearance_m": float(min(clearance_values)),
            "mean_clearance_m": float(
                sum(clearance_values) / max(1, len(clearance_values))
            ),
            "clearance_sample_count": int(len(clearance_values)),
            "flood_fill_truncated": False,
        }

    def filled_free_cell_clearance_m(
        self,
        points: Sequence[Sequence[float]],
    ) -> dict[VoxelIndex, float]:
        """Return the bounded filled free-space cells reached from ``points``.

        Surface voxels are barriers and the flood fill is limited to this
        local volume. The result is intentionally a plain mapping so callers
        can aggregate it into a coarser navigation graph without coupling the
        graph to this voxel implementation.
        """
        seed_indices = self._free_seed_indices(points)
        return self._filled_free_cell_clearance_from_seeds(seed_indices)

    def iter_all_free_cell_clearance_m(
        self,
    ) -> Iterator[tuple[VoxelIndex, float]]:
        """Yield every non-surface cell in this bounded local field.

        Fixed V12 chunks are merged before their terminal component is
        selected.  Iterating the bounded dense field prevents a locally
        imperfect seed from deleting valid seam evidence; sampled surface
        cells remain hard barriers and overlapping occupancy still wins in
        the global merge.
        """
        clearance_map = self._surface_clearance_distance_map()
        fallback_distance_m = (
            float(self.max_clearance_search_cells + 1)
            * min(self.cell_size_m)
        )
        for x_index in range(self.shape[0]):
            for y_index in range(self.shape[1]):
                for z_index in range(self.shape[2]):
                    index = (x_index, y_index, z_index)
                    if index in self.surface_cells:
                        continue
                    yield (
                        index,
                        float(clearance_map.get(index, fallback_distance_m)),
                    )

    def _filled_free_cell_clearance_from_seeds(
        self,
        seed_indices: Sequence[VoxelIndex],
    ) -> dict[VoxelIndex, float]:
        free_cells = self._reachable_free_cells(seed_indices)
        if not free_cells:
            return {}
        clearance_map = self._surface_clearance_distance_map()
        fallback_distance_m = (
            float(self.max_clearance_search_cells + 1)
            * min(self.cell_size_m)
        )
        return {
            index: float(clearance_map.get(index, fallback_distance_m))
            for index in free_cells
        }

    def diagnostic_payload(self) -> dict[str, object]:
        """Return bounded diagnostics suitable for the Guided Dive blackbox."""
        return {
            "voxel_size_m": float(self.voxel_size_m),
            "vertical_voxel_size_m": float(self.vertical_voxel_size_m),
            "cell_size_m": [float(value) for value in self.cell_size_m],
            "origin": [float(value) for value in self.origin],
            "bounds_min": [float(value) for value in self.bounds_min],
            "bounds_max": [float(value) for value in self.bounds_max],
            "voxel_shape": [int(value) for value in self.shape],
            "voxel_count": int(self.voxel_count),
            "surface_cells": len(self.surface_cells),
            "surface_occupied_volume_m3": float(
                len(self.surface_cells) * self.cell_volume_m3
            ),
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
            int(
                math.floor(
                    (float(lower) - self.origin[axis])
                    / self.cell_size_m[axis]
                )
            ),
        )
        last = min(
            self.shape[axis] - 1,
            int(
                math.floor(
                    (float(upper) - self.origin[axis])
                    / self.cell_size_m[axis]
                )
            ),
        )
        if last < first:
            return ()
        return tuple(range(first, last + 1))

    def _free_seed_indices(
        self,
        points: Sequence[Sequence[float]],
    ) -> tuple[VoxelIndex, ...]:
        seeds: set[VoxelIndex] = set()
        for point in points:
            try:
                index = self.voxel_index(point)
            except (TypeError, ValueError):
                continue
            if not self.contains_index(index):
                continue
            if index not in self.surface_cells:
                seeds.add(index)
                continue
            # A centerline sample can land in an inflated surface voxel after
            # coarse rasterization. Find the nearest unblocked voxel without
            # allowing a seed to jump across the whole local model.
            for radius in range(1, self.max_clearance_search_cells + 2):
                candidate = next(
                    (
                        item
                        for item in _shell_indices(index, radius)
                        if self.contains_index(item)
                        and item not in self.surface_cells
                    ),
                    None,
                )
                if candidate is not None:
                    seeds.add(candidate)
                    break
        return tuple(sorted(seeds))

    def _reachable_free_cells(
        self,
        seeds: Sequence[VoxelIndex],
    ) -> frozenset[VoxelIndex]:
        visited = set(seeds)
        queue = deque(seeds)
        while queue:
            current = queue.popleft()
            for neighbor in _six_neighbor_indices(current):
                if not self.contains_index(neighbor):
                    continue
                if neighbor in self.surface_cells or neighbor in visited:
                    continue
                visited.add(neighbor)
                queue.append(neighbor)
        return frozenset(visited)

    def _surface_fraction(self) -> float:
        return float(len(self.surface_cells) / max(1, self.voxel_count))

    def _surface_clearance_distance_map(self) -> dict[VoxelIndex, float]:
        """Return a bounded physical-distance map from surface cells."""
        if not self.surface_cells:
            return {}
        distances = {index: 0.0 for index in self.surface_cells}
        steps = {index: 0 for index in self.surface_cells}
        queue: list[tuple[float, VoxelIndex]] = [
            (0.0, index) for index in self.surface_cells
        ]
        heapq.heapify(queue)
        while queue:
            current_distance, current = heapq.heappop(queue)
            if current_distance > distances.get(current, math.inf) + 1e-12:
                continue
            current_steps = steps[current]
            if current_steps >= self.max_clearance_search_cells:
                continue
            for offset in _LOCAL_26_NEIGHBOR_OFFSETS:
                neighbor = tuple(
                    current[axis] + offset[axis] for axis in range(3)
                )
                if not self.contains_index(neighbor):
                    continue
                edge_distance = math.sqrt(
                    sum(
                        (offset[axis] * self.cell_size_m[axis]) ** 2
                        for axis in range(3)
                    )
                )
                next_distance = current_distance + edge_distance
                existing = distances.get(neighbor)
                if existing is not None and next_distance >= existing - 1e-12:
                    continue
                distances[neighbor] = next_distance
                steps[neighbor] = current_steps + 1
                heapq.heappush(queue, (next_distance, neighbor))
        return distances


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
    max_distance_m: float | None = DEFAULT_VOXEL_MAX_DISTANCE_M,
    padding_m: float | None = None,
    max_voxels: int = DEFAULT_VOXEL_MAX_CELLS,
    max_surface_samples: int = DEFAULT_VOXEL_MAX_SURFACE_SAMPLES,
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
            max_surface_samples=max_surface_samples,
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
    max_distance_m: float | None = DEFAULT_VOXEL_MAX_DISTANCE_M,
    padding_m: float | None = None,
    max_voxels: int = DEFAULT_VOXEL_MAX_CELLS,
    max_surface_samples: int = DEFAULT_VOXEL_MAX_SURFACE_SAMPLES,
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
        max_surface_samples=max_surface_samples,
        window_points=window_points,
    )
    return analysis.profile, analysis.volume


def build_surface_voxel_volume(
    triangle_meshes: Iterable[np.ndarray],
    *,
    bounds_min: Sequence[float],
    bounds_max: Sequence[float],
    config: VoxelVolumeConfig | None = None,
    deadline_check: Callable[[], None] | None = None,
) -> LocalVoxelVolume:
    """Rasterize cached triangle surfaces into a bounded sparse voxel field.

    ``deadline_check`` is a cooperative owner-supplied guard. It is called at
    bounded intervals so a runtime recovery field cannot outlive the route
    handoff window. A raised exception aborts construction; no partial volume
    is returned.
    """
    resolved = (config or VoxelVolumeConfig()).validated()
    lower = _point(bounds_min)
    upper = _point(bounds_max)
    if any(upper[index] <= lower[index] for index in range(3)):
        raise ValueError("voxel bounds must have positive extent")
    voxel_size, vertical_voxel_size, origin, shape = _fit_volume_geometry(
        lower,
        upper,
        resolved,
    )
    cell_size = (voxel_size, vertical_voxel_size, voxel_size)
    blocked: set[VoxelIndex] = set()
    triangle_count = 0
    sample_count = 0
    sampling_truncated = False
    lower_array = np.asarray(lower, dtype=np.float64)
    upper_array = np.asarray(upper, dtype=np.float64)
    for mesh in triangle_meshes:
        if deadline_check is not None:
            deadline_check()
        triangles = _normalise_triangles(mesh)
        if triangles is None:
            continue
        triangle_lower = triangles.min(axis=1)
        triangle_upper = triangles.max(axis=1)
        intersecting = np.all(
            triangle_upper >= lower_array,
            axis=1,
        ) & np.all(
            upper_array >= triangle_lower,
            axis=1,
        )
        for triangle in triangles[intersecting]:
            if deadline_check is not None:
                deadline_check()
            triangle_count += 1
            steps = _triangle_steps(triangle, min(cell_size))
            for sample in _triangle_samples(triangle, steps):
                if (
                    deadline_check is not None
                    and sample_count % 128 == 0
                ):
                    deadline_check()
                if sample_count >= resolved.max_surface_samples:
                    sampling_truncated = True
                    break
                index = _voxel_index(sample, origin, cell_size)
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
        vertical_voxel_size_m=vertical_voxel_size,
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
    max_distance_m: float | None,
    profile: CurvatureProfile,
) -> tuple[Point, ...]:
    normalized = tuple(_point(point) for point in points)
    selected: set[int] = set()
    for region in regions:
        start = max(0, int(region.start_index))
        end = min(len(normalized) - 1, int(region.end_index))
        for index in range(start, end + 1):
            if (
                max_distance_m is None
                or profile.cumulative_distances_m[index] <= float(max_distance_m)
            ):
                selected.add(index)
    return tuple(normalized[index] for index in sorted(selected))


def _point(value: Sequence[float]) -> Point:
    if len(value) != 3:
        raise ValueError("voxel points must be three-dimensional")
    point = (float(value[0]), float(value[1]), float(value[2]))
    if not all(math.isfinite(coordinate) for coordinate in point):
        raise ValueError("voxel points must be finite")
    return point


def _dedupe_points(points: Sequence[Point]) -> tuple[Point, ...]:
    """Remove repeated adjacent local voxel route points."""
    result: list[Point] = []
    for point in points:
        normalized = _point(point)
        if result and all(
            abs(normalized[axis] - result[-1][axis]) <= 1e-9
            for axis in range(3)
        ):
            continue
        result.append(normalized)
    return tuple(result)


def _fit_volume_geometry(
    bounds_min: Point,
    bounds_max: Point,
    config: VoxelVolumeConfig,
) -> tuple[float, float, Point, tuple[int, int, int]]:
    horizontal_size = float(config.voxel_size_m)
    vertical_size = float(config.vertical_voxel_size_m)
    while True:
        cell_size = (horizontal_size, vertical_size, horizontal_size)
        origin = tuple(
            math.floor(bounds_min[axis] / cell_size[axis]) * cell_size[axis]
            for axis in range(3)
        )  # type: ignore[assignment]
        shape = tuple(
            max(
                1,
                int(
                    math.ceil(
                        (bounds_max[axis] - origin[axis])
                        / cell_size[axis]
                    )
                )
                + 1,
            )
            for axis in range(3)
        )
        count = shape[0] * shape[1] * shape[2]
        if count <= config.max_voxels:
            return (  # type: ignore[return-value]
                horizontal_size,
                vertical_size,
                origin,
                shape,
            )
        scale = max(1.05, (count / config.max_voxels) ** (1.0 / 3.0))
        horizontal_size *= scale
        vertical_size *= scale


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
    return max(1, min(256, int(math.ceil(max(edge_lengths) / voxel_size))))


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


def _voxel_index(
    point: Sequence[float],
    origin: Point,
    cell_size: Sequence[float],
) -> VoxelIndex:
    return tuple(
        _stable_floor_grid_coordinate(
            (float(point[axis]) - origin[axis]) / float(cell_size[axis])
        )
        for axis in range(3)
    )  # type: ignore[return-value]


def _stable_floor_grid_coordinate(value: float) -> int:
    """Floor a grid coordinate without smearing exact boundary samples."""
    nearest = round(float(value))
    if math.isclose(float(value), nearest, rel_tol=0.0, abs_tol=1e-9):
        return int(nearest)
    return int(math.floor(float(value)))


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


def _six_neighbor_indices(center: VoxelIndex) -> Iterable[VoxelIndex]:
    x, y, z = center
    yield (x - 1, y, z)
    yield (x + 1, y, z)
    yield (x, y - 1, z)
    yield (x, y + 1, z)
    yield (x, y, z - 1)
    yield (x, y, z + 1)


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
