"""Cache-time voxel atlases for whole-cave navigation.

The cache artifact in this module is intentionally separate from the render
manifest. It stores a bounded, compressed atlas of local surface voxel models
covering every cell in a navigable cave component. Tiling keeps import-time
memory bounded while preserving useful resolution before, through, and after
high-curvature regions. Older single-window sidecars remain readable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import base64
import binascii
import heapq
import math
import os
import zlib

import numpy as np

from caveviewer.core.json_io import load_bounded_json
from caveviewer.core.navigation.centerline import (
    FootprintCell,
    Point,
    footprint_cell_distance,
    footprint_path_length,
    footprint_world_center,
    navigable_footprint_neighbors,
)
from caveviewer.core.navigation.curvature import (
    CURVATURE_PROFILE_METHOD,
    analyze_polyline_curvature,
    select_curvature_regions,
)
from caveviewer.core.navigation.voxel_volume import (
    DEFAULT_VOXEL_CURVATURE_RANK_THRESHOLD,
    DEFAULT_VOXEL_MAX_CELLS,
    DEFAULT_VOXEL_MAX_REGIONS,
    DEFAULT_VOXEL_MAX_SURFACE_SAMPLES,
    DEFAULT_VOXEL_SIZE_M,
    LocalVoxelVolume,
    TriangleProvider,
    VoxelVolumeConfig,
    build_surface_voxel_volume,
)


NAVIGATION_VOXEL_CACHE_VERSION = 2
NAVIGATION_VOXEL_CACHE_METHOD = "whole_cave_voxel_atlas_v2"
_LEGACY_NAVIGATION_VOXEL_CACHE_VERSION = 1
_LEGACY_NAVIGATION_VOXEL_CACHE_METHOD = "curvature_corridor_voxels_v1"
NAVIGATION_VOXEL_CACHE_NAME = "navigation_voxels.json"
NAVIGATION_VOXEL_CACHE_MAX_BYTES = 64 * 1024 * 1024
NAVIGATION_VOXEL_ATLAS_MODEL_METHOD = "navigation_voxel_atlas_v2"

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
DEFAULT_CACHE_VOXEL_TILE_SIZE_M = 64.0
DEFAULT_CACHE_VOXEL_MAX_TILES = 256
DEFAULT_CACHE_VOXEL_MAX_TILE_CELLS = 8_192
DEFAULT_CACHE_VOXEL_MAX_CELL_METRICS = 16_384
NAVIGATION_VOXEL_GRAPH_METHOD = "voxel_filled_component_graph_v1"
NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD = "voxel_branch_lookahead_v1"
DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_DISTANCE_M = 256.0
DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_CELLS = 32
DEFAULT_NAVIGATION_VOXEL_BRANCH_MAX_CANDIDATES = 8
DEFAULT_NAVIGATION_VOXEL_BRANCH_MAX_EXPANSIONS = 2_048
# A route may turn sideways, but an explicit travel direction must never select
# the first step of a branch that points back toward the entrance.
DEFAULT_NAVIGATION_VOXEL_MIN_FORWARD_ALIGNMENT = 0.0


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
    tile_size_m: float = DEFAULT_CACHE_VOXEL_TILE_SIZE_M
    max_tiles: int = DEFAULT_CACHE_VOXEL_MAX_TILES

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
        tile_size = float(self.tile_size_m)
        if not math.isfinite(tile_size) or tile_size <= 0.0:
            raise ValueError("cache voxel tile size must be positive and finite")
        max_tiles = max(1, int(self.max_tiles))
        return NavigationVoxelCacheConfig(
            voxel_size_m=size,
            curvature_rank_threshold=rank,
            max_regions=max_regions,
            max_cells=max_cells,
            max_surface_samples=max_samples,
            max_routes=max_routes,
            window_points=window_points,
            tile_size_m=tile_size,
            max_tiles=max_tiles,
        )


@dataclass(frozen=True)
class NavigationVoxelCacheBuildResult:
    """Result of an optional cache-time navigation voxel pass."""

    payload: dict[str, object]
    built_route_count: int
    recommended_route_id: str | None


@dataclass(frozen=True)
class NavigationVoxelCellMetric:
    """Filled free-space measurements associated with one footprint cell."""

    available_volume_m3: float
    free_cell_count: int
    min_clearance_m: float
    mean_clearance_m: float
    progress_m: float


@dataclass(frozen=True)
class NavigationVoxelBranchScore:
    """Topology-only score for one bounded forward branch."""

    branch_start_cell: FootprintCell
    target_cell: FootprintCell
    reached_distance_m: float
    continuation_distance_m: float
    onward_exit_count: int
    frontier_count: int
    first_step_alignment: float
    path_cost_m: float
    expanded_count: int
    dead_end: bool
    target_is_terminal: bool

    def diagnostic_payload(self) -> dict[str, object]:
        """Return bounded branch evidence for the navigation blackbox."""
        return {
            "branch_start_cell": [
                int(self.branch_start_cell[0]),
                int(self.branch_start_cell[1]),
            ],
            "target_cell": [
                int(self.target_cell[0]),
                int(self.target_cell[1]),
            ],
            "reached_distance_m": float(self.reached_distance_m),
            "continuation_distance_m": float(self.continuation_distance_m),
            "onward_exit_count": int(self.onward_exit_count),
            "frontier_count": int(self.frontier_count),
            "first_step_alignment": float(self.first_step_alignment),
            "path_cost_m": float(self.path_cost_m),
            "expanded_count": int(self.expanded_count),
            "dead_end": bool(self.dead_end),
            "target_is_terminal": bool(self.target_is_terminal),
        }


@dataclass(frozen=True)
class _NavigationVoxelBranchEvaluation:
    """Internal branch score paired with its bounded route prefix."""

    score: NavigationVoxelBranchScore
    path: tuple[FootprintCell, ...]
    sort_key: tuple[object, ...]


@dataclass(frozen=True)
class NavigationVoxelRoutePlan:
    """A bounded forward route selected from the cached filled-space graph."""

    cells: tuple[FootprintCell, ...]
    start_cell: FootprintCell
    goal_cell: FootprintCell
    start_progress_m: float
    goal_progress_m: float
    goal_volume_m3: float
    route_volume_m3: float
    goal_clearance_m: float
    expanded_count: int
    selection_reason: str = "voxel_branch_lookahead"
    lookahead_distance_m: float = 0.0
    replan_at_lookahead: bool = True
    branch_score: NavigationVoxelBranchScore | None = None
    branch_candidates: tuple[NavigationVoxelBranchScore, ...] = ()

    def diagnostic_payload(self) -> dict[str, object]:
        """Return route-selection details suitable for the debug log."""
        return {
            "method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "selection_reason": self.selection_reason,
            "cell_count": len(self.cells),
            "start_cell": [int(value) for value in self.start_cell],
            "goal_cell": [int(value) for value in self.goal_cell],
            "start_progress_m": float(self.start_progress_m),
            "goal_progress_m": float(self.goal_progress_m),
            "forward_progress_m": float(
                self.goal_progress_m - self.start_progress_m
            ),
            "goal_volume_m3": float(self.goal_volume_m3),
            "route_volume_m3": float(self.route_volume_m3),
            "goal_clearance_m": float(self.goal_clearance_m),
            "expanded_count": int(self.expanded_count),
            "lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "lookahead_distance_m": float(self.lookahead_distance_m),
            "replan_at_lookahead": bool(self.replan_at_lookahead),
            "cross_section_scoring": "deferred",
            "branch": (
                None
                if self.branch_score is None
                else self.branch_score.diagnostic_payload()
            ),
            "branch_candidates": [
                score.diagnostic_payload()
                for score in self.branch_candidates
            ],
            "first_cells": [
                [int(cell[0]), int(cell[1])]
                for cell in self.cells[:8]
            ],
            "last_cells": [
                [int(cell[0]), int(cell[1])]
                for cell in self.cells[-8:]
            ],
        }


@dataclass(frozen=True)
class NavigationVoxelAtlas:
    """A bounded collection of local voxel fields covering one cave route.

    Each tile has its own dense capacity limit. The atlas therefore avoids the
    unusably coarse voxel size that a single dense box would require for a
    long cave, while still allowing runtime recovery to refine points across
    the whole cached component.
    """

    tiles: tuple[LocalVoxelVolume, ...]
    coverage_scope: str = "entire_cave_component"
    cell_metrics: Mapping[FootprintCell, NavigationVoxelCellMetric] = field(
        default_factory=dict
    )

    @property
    def voxel_count(self) -> int:
        return int(sum(tile.voxel_count for tile in self.tiles))

    @property
    def surface_cells(self) -> frozenset[tuple[int, int, int]]:
        """Expose sparse occupancy for compatibility with local fields."""
        cells: set[tuple[int, int, int]] = set()
        for tile in self.tiles:
            cells.update(tile.surface_cells)
        return frozenset(cells)

    @property
    def voxel_size_m(self) -> float:
        if not self.tiles:
            return 0.0
        return float(min(tile.voxel_size_m for tile in self.tiles))

    @property
    def navigation_cell_count(self) -> int:
        """Return the number of footprint cells with filled-space metrics."""
        return len(self.cell_metrics)

    @property
    def filled_free_cell_count(self) -> int:
        """Return the aggregate number of free voxels represented by metrics."""
        return sum(
            max(0, int(metric.free_cell_count))
            for metric in self.cell_metrics.values()
        )

    @property
    def max_progress_m(self) -> float:
        """Return the deepest cached graph progress from its entrance seed."""
        return max(
            (float(metric.progress_m) for metric in self.cell_metrics.values()),
            default=0.0,
        )

    @property
    def bounds_min(self) -> Point:
        if not self.tiles:
            return (0.0, 0.0, 0.0)
        return tuple(
            min(tile.bounds_min[axis] for tile in self.tiles)
            for axis in range(3)
        )  # type: ignore[return-value]

    @property
    def bounds_max(self) -> Point:
        if not self.tiles:
            return (0.0, 0.0, 0.0)
        return tuple(
            max(tile.bounds_max[axis] for tile in self.tiles)
            for axis in range(3)
        )  # type: ignore[return-value]

    def refine_point(
        self,
        desired: Sequence[float],
        *,
        footprint_cell: FootprintCell,
        footprint_cell_size: float,
        y_range: tuple[float, float] | None = None,
        max_candidates: int = 4096,
    ) -> Point | None:
        """Refine a point using the best local tile that covers its cell."""
        candidates: list[tuple[float, float, Point]] = []
        desired_point = tuple(float(value) for value in desired)
        for tile in self.tiles:
            candidate = tile.refine_point(
                desired_point,
                footprint_cell=footprint_cell,
                footprint_cell_size=footprint_cell_size,
                y_range=y_range,
                max_candidates=max_candidates,
            )
            if candidate is None:
                continue
            try:
                index = tile.voxel_index(candidate)
                clearance = tile.surface_clearance_m(index)
            except (TypeError, ValueError):
                clearance = 0.0
            distance_squared = sum(
                (candidate[axis] - desired_point[axis]) ** 2
                for axis in range(3)
            )
            candidates.append((float(clearance), -distance_squared, candidate))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[:2])[2]

    def corridor_volume_metrics(
        self,
        points: Sequence[Sequence[float]],
    ) -> dict[str, float | int | bool]:
        """Aggregate corridor metrics across all atlas tiles."""
        metrics = [
            tile.corridor_volume_metrics(
                tuple(
                    point
                    for point in points
                    if tile.contains_point(point)
                )
            )
            for tile in self.tiles
        ]
        if not metrics:
            return {
                "seed_count": 0,
                "free_cell_count": 0,
                "available_volume_m3": 0.0,
                "surface_fraction": 0.0,
                "min_clearance_m": 0.0,
                "mean_clearance_m": 0.0,
                "clearance_sample_count": 0,
                "flood_fill_truncated": False,
            }
        sample_count = sum(int(item["clearance_sample_count"]) for item in metrics)
        mean_clearance = sum(
            float(item["mean_clearance_m"])
            * int(item["clearance_sample_count"])
            for item in metrics
        ) / max(1, sample_count)
        clearance_values = [
            float(item["min_clearance_m"])
            for item in metrics
            if int(item["clearance_sample_count"]) > 0
        ]
        voxel_capacity = sum(tile.voxel_count for tile in self.tiles)
        surface_cells = sum(len(tile.surface_cells) for tile in self.tiles)
        return {
            "seed_count": sum(int(item["seed_count"]) for item in metrics),
            "free_cell_count": sum(
                int(item["free_cell_count"]) for item in metrics
            ),
            "available_volume_m3": sum(
                float(item["available_volume_m3"]) for item in metrics
            ),
            "surface_fraction": float(surface_cells / max(1, voxel_capacity)),
            "min_clearance_m": min(clearance_values, default=0.0),
            "mean_clearance_m": float(mean_clearance),
            "clearance_sample_count": int(sample_count),
            "flood_fill_truncated": any(
                bool(item["flood_fill_truncated"]) for item in metrics
            ),
        }

    def plan_footprint_route(
        self,
        component_cells: Sequence[FootprintCell]
        | set[FootprintCell]
        | frozenset[FootprintCell],
        *,
        current_position: Sequence[float],
        footprint_cell_size: float,
        preferred_direction: Sequence[float] | None = None,
        max_expansions: int = DEFAULT_NAVIGATION_VOXEL_BRANCH_MAX_EXPANSIONS,
        max_route_cells: int = 512,
        backtrack_tolerance_m: float | None = None,
        min_progress_gain_m: float | None = None,
        lookahead_distance_m: float | None = None,
        lookahead_cells: int = DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_CELLS,
        max_branch_candidates: int = DEFAULT_NAVIGATION_VOXEL_BRANCH_MAX_CANDIDATES,
        diagnostics: Callable[[str, Mapping[str, object]], None] | None = None,
    ) -> NavigationVoxelRoutePlan | None:
        """Select a forward branch with bounded continuation lookahead.

        The graph is intentionally a coarse footprint graph over the filled
        3D voxel measurements. Progress from the cache entrance is a hard
        guard against returning to the entrance, but it is not the branch
        objective. Each immediate branch is explored only to a bounded
        lookahead and then scored by whether it continues beyond that horizon
        or terminates in a cul-de-sac. Cross-section metrics remain available
        on the selected cells but are deliberately not part of this first
        topology policy.
        """
        try:
            cell_size = float(footprint_cell_size)
            position = tuple(float(value) for value in current_position)
        except (TypeError, ValueError):
            return None
        if (
            not math.isfinite(cell_size)
            or cell_size <= 0.0
            or len(position) != 3
            or not all(math.isfinite(value) for value in position)
        ):
            return None
        component = {
            (int(cell[0]), int(cell[1]))
            for cell in component_cells
            if len(cell) == 2
        }
        graph_cells = {
            cell
            for cell in component
            if cell in self.cell_metrics
            and int(self.cell_metrics[cell].free_cell_count) > 0
        }
        if len(graph_cells) < 2:
            return None

        current_cell = (
            math.floor(position[0] / cell_size),
            math.floor(position[2] / cell_size),
        )
        if current_cell not in graph_cells:
            current_cell = min(
                graph_cells,
                key=lambda cell: (
                    (cell[0] + 0.5) * cell_size - position[0]
                ) ** 2
                + ((cell[1] + 0.5) * cell_size - position[2]) ** 2,
            )
        start_metric = self.cell_metrics[current_cell]
        start_progress = float(start_metric.progress_m)
        tolerance = (
            max(cell_size, self.voxel_size_m)
            if backtrack_tolerance_m is None
            else max(0.0, float(backtrack_tolerance_m))
        )
        progress_gain = (
            max(cell_size * 0.5, self.voxel_size_m)
            if min_progress_gain_m is None
            else max(0.0, float(min_progress_gain_m))
        )
        direction_xz = _normalised_xz_direction(preferred_direction)

        requested_lookahead = (
            DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_DISTANCE_M
            if lookahead_distance_m is None
            else float(lookahead_distance_m)
        )
        if not math.isfinite(requested_lookahead) or requested_lookahead <= 0.0:
            requested_lookahead = DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_DISTANCE_M
        lookahead_distance = max(cell_size * 4.0, requested_lookahead)
        lookahead_cell_limit = max(4, int(lookahead_cells))
        expansion_limit = max(1, int(max_expansions))
        branch_limit = max(
            1,
            min(int(max_branch_candidates), expansion_limit),
        )
        branch_starts = [
            neighbor
            for neighbor in navigable_footprint_neighbors(
                current_cell,
                graph_cells,
            )
            if float(self.cell_metrics[neighbor].progress_m)
            >= start_progress - tolerance
            and float(self.cell_metrics[neighbor].progress_m)
            >= start_progress + progress_gain * 0.25
        ]
        if not branch_starts:
            branch_starts = [
                neighbor
                for neighbor in navigable_footprint_neighbors(
                    current_cell,
                    graph_cells,
                )
                if float(self.cell_metrics[neighbor].progress_m)
                >= start_progress - tolerance
            ]
        branch_starts = sorted(
            set(branch_starts),
            key=lambda cell: (
                -_cell_direction_alignment(
                    current_cell,
                    cell,
                    direction_xz,
                ),
                -float(self.cell_metrics[cell].progress_m),
                cell,
            ),
        )[:branch_limit]
        if not branch_starts:
            _record_voxel_route_diagnostic(
                diagnostics,
                "voxel_route_rejected",
                {
                    "reason": "no_forward_progress_neighbor",
                    "start_cell": [int(current_cell[0]), int(current_cell[1])],
                    "start_progress_m": float(start_progress),
                    "preferred_direction": _direction_payload(direction_xz),
                    "min_forward_alignment": float(
                        DEFAULT_NAVIGATION_VOXEL_MIN_FORWARD_ALIGNMENT
                    ),
                },
            )
            return None

        branch_budget = max(1, expansion_limit // max(1, len(branch_starts)))
        evaluations: list[_NavigationVoxelBranchEvaluation] = []
        for branch_start in branch_starts:
            evaluation = _evaluate_voxel_branch(
                current_cell=current_cell,
                branch_start=branch_start,
                graph_cells=graph_cells,
                metrics=self.cell_metrics,
                start_progress=start_progress,
                cell_size=cell_size,
                tolerance=tolerance,
                progress_gain=progress_gain,
                preferred_direction=direction_xz,
                lookahead_distance_m=lookahead_distance,
                lookahead_cells=lookahead_cell_limit,
                expansion_budget=branch_budget,
            )
            if evaluation is not None:
                evaluations.append(evaluation)
        if not evaluations:
            _record_voxel_route_diagnostic(
                diagnostics,
                "voxel_route_rejected",
                {
                    "reason": "no_evaluated_branch",
                    "start_cell": [int(current_cell[0]), int(current_cell[1])],
                    "start_progress_m": float(start_progress),
                    "preferred_direction": _direction_payload(direction_xz),
                    "candidate_count": 0,
                    "min_forward_alignment": float(
                        DEFAULT_NAVIGATION_VOXEL_MIN_FORWARD_ALIGNMENT
                    ),
                },
            )
            return None

        continuing = [
            item
            for item in evaluations
            if not item.score.dead_end
            and float(item.score.continuation_distance_m) > 0.0
        ]
        if not continuing:
            _record_voxel_route_diagnostic(
                diagnostics,
                "voxel_route_rejected",
                {
                    "reason": "no_non_dead_end_branch",
                    "start_cell": [int(current_cell[0]), int(current_cell[1])],
                    "start_progress_m": float(start_progress),
                    "preferred_direction": _direction_payload(direction_xz),
                    "candidate_count": len(evaluations),
                    "non_dead_end_count": 0,
                    "min_forward_alignment": float(
                        DEFAULT_NAVIGATION_VOXEL_MIN_FORWARD_ALIGNMENT
                    ),
                    "branch_candidates": [
                        item.score.diagnostic_payload()
                        for item in sorted(
                            evaluations,
                            key=lambda item: item.sort_key,
                            reverse=True,
                        )[:branch_limit]
                    ],
                },
            )
            return None

        direction_filtered = continuing
        if direction_xz is not None:
            direction_filtered = [
                item
                for item in continuing
                if float(item.score.first_step_alignment)
                >= DEFAULT_NAVIGATION_VOXEL_MIN_FORWARD_ALIGNMENT
            ]
            if not direction_filtered:
                _record_voxel_route_diagnostic(
                    diagnostics,
                    "voxel_route_rejected",
                    {
                        "reason": "no_forward_direction_branch",
                        "start_cell": [
                            int(current_cell[0]),
                            int(current_cell[1]),
                        ],
                        "start_progress_m": float(start_progress),
                        "preferred_direction": _direction_payload(direction_xz),
                        "candidate_count": len(evaluations),
                        "non_dead_end_count": len(continuing),
                        "forward_aligned_count": 0,
                        "min_forward_alignment": float(
                            DEFAULT_NAVIGATION_VOXEL_MIN_FORWARD_ALIGNMENT
                        ),
                        "branch_candidates": [
                            item.score.diagnostic_payload()
                            for item in sorted(
                                continuing,
                                key=lambda item: item.sort_key,
                                reverse=True,
                            )[:branch_limit]
                        ],
                    },
                )
                return None

        selected = max(direction_filtered, key=lambda item: item.sort_key)
        path_tuple = _bound_voxel_route_cells(
            selected.path,
            max_route_cells=max_route_cells,
        )
        goal_cell = selected.score.target_cell
        goal_metric = self.cell_metrics[goal_cell]
        route_volume = sum(
            float(self.cell_metrics[cell].available_volume_m3)
            for cell in path_tuple
        )
        ordered_scores = tuple(
            item.score
            for item in sorted(
                evaluations,
                key=lambda item: item.sort_key,
                reverse=True,
            )
        )
        return NavigationVoxelRoutePlan(
            cells=path_tuple,
            start_cell=current_cell,
            goal_cell=goal_cell,
            start_progress_m=start_progress,
            goal_progress_m=float(goal_metric.progress_m),
            goal_volume_m3=float(goal_metric.available_volume_m3),
            route_volume_m3=float(route_volume),
            goal_clearance_m=float(goal_metric.mean_clearance_m),
            expanded_count=sum(
                int(item.score.expanded_count) for item in evaluations
            ),
            selection_reason="voxel_branch_lookahead",
            lookahead_distance_m=float(lookahead_distance),
            replan_at_lookahead=True,
            branch_score=selected.score,
            branch_candidates=ordered_scores,
        )

    def diagnostic_payload(self) -> dict[str, object]:
        """Return bounded atlas diagnostics for the Guided Dive blackbox."""
        tile_sizes = [float(tile.voxel_size_m) for tile in self.tiles]
        return {
            "model_kind": NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
            "coverage_scope": self.coverage_scope,
            "tile_count": len(self.tiles),
            "voxel_size_m": float(self.voxel_size_m),
            "voxel_size_max_m": max(tile_sizes, default=0.0),
            "bounds_min": [float(value) for value in self.bounds_min],
            "bounds_max": [float(value) for value in self.bounds_max],
            "voxel_count": int(self.voxel_count),
            "surface_cells": len(self.surface_cells),
            "surface_occupied_volume_m3": float(
                sum(
                    len(tile.surface_cells) * tile.voxel_size_m ** 3
                    for tile in self.tiles
                )
            ),
            "triangle_count": int(sum(tile.triangle_count for tile in self.tiles)),
            "surface_sample_count": int(
                sum(tile.surface_sample_count for tile in self.tiles)
            ),
            "sampling_truncated": any(
                bool(tile.sampling_truncated) for tile in self.tiles
            ),
            "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "branch_lookahead_default_distance_m": float(
                DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_DISTANCE_M
            ),
            "branch_lookahead_default_cells": int(
                DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_CELLS
            ),
            "navigation_cell_count": int(self.navigation_cell_count),
            "filled_free_cell_count": int(self.filled_free_cell_count),
            "max_progress_m": float(self.max_progress_m),
        }


def _record_voxel_route_diagnostic(
    diagnostics: Callable[[str, Mapping[str, object]], None] | None,
    event: str,
    payload: Mapping[str, object],
) -> None:
    if diagnostics is None:
        return
    try:
        diagnostics(event, payload)
    except Exception:
        return


def _direction_payload(
    direction: tuple[float, float] | None,
) -> list[float] | None:
    if direction is None:
        return None
    return [float(direction[0]), float(direction[1])]


def _evaluate_voxel_branch(
    *,
    current_cell: FootprintCell,
    branch_start: FootprintCell,
    graph_cells: set[FootprintCell],
    metrics: Mapping[FootprintCell, NavigationVoxelCellMetric],
    start_progress: float,
    cell_size: float,
    tolerance: float,
    progress_gain: float,
    preferred_direction: tuple[float, float] | None,
    lookahead_distance_m: float,
    lookahead_cells: int,
    expansion_budget: int,
) -> _NavigationVoxelBranchEvaluation | None:
    """Score one immediate branch without searching to a global endpoint."""
    initial_cost = _voxel_graph_edge_cost(
        current_cell,
        branch_start,
        cell_size=cell_size,
        voxel_size_m=1.0,
        metric=metrics[branch_start],
        preferred_direction=preferred_direction,
    )
    distances: dict[FootprintCell, float] = {branch_start: initial_cost}
    depths: dict[FootprintCell, int] = {branch_start: 1}
    predecessors: dict[FootprintCell, FootprintCell] = {
        branch_start: current_cell
    }
    queue: list[tuple[float, FootprintCell]] = [(initial_cost, branch_start)]
    frontier: list[FootprintCell] = []
    terminal: list[FootprintCell] = []
    expanded_count = 0
    expansion_limit = max(1, int(expansion_budget))
    main_search_budget = min(
        expansion_limit,
        max(1, int(math.ceil(expansion_limit * 0.67))),
    )
    max_distance = max(
        lookahead_distance_m * 0.75,
        cell_size * 2.0,
    )
    while queue and expanded_count < main_search_budget:
        distance, cell = heapq.heappop(queue)
        if distance > distances.get(cell, float("inf")) + 1e-9:
            continue
        expanded_count += 1
        if (
            distance >= lookahead_distance_m
            or depths.get(cell, 1) >= max(1, int(lookahead_cells))
        ):
            frontier.append(cell)
            continue
        neighbors = _voxel_branch_neighbors(
            cell,
            previous=predecessors.get(cell),
            graph_cells=graph_cells,
            metrics=metrics,
            start_progress=start_progress,
            tolerance=tolerance,
            progress_gain=progress_gain,
            current_cell=current_cell,
        )
        if not neighbors:
            terminal.append(cell)
            continue
        for neighbor in neighbors:
            edge_cost = _voxel_graph_edge_cost(
                cell,
                neighbor,
                cell_size=cell_size,
                voxel_size_m=1.0,
                metric=metrics[neighbor],
                preferred_direction=preferred_direction,
            )
            next_distance = distance + edge_cost
            next_depth = depths.get(cell, 1) + 1
            if next_distance + 1e-9 >= distances.get(
                neighbor,
                float("inf"),
            ):
                continue
            distances[neighbor] = next_distance
            depths[neighbor] = next_depth
            predecessors[neighbor] = cell
            heapq.heappush(queue, (next_distance, neighbor))

    candidates: list[tuple[FootprintCell, float, int, bool, int]] = []
    frontier_candidates = frontier[:16]
    for index, cell in enumerate(frontier_candidates):
        remaining_budget = expansion_limit - expanded_count
        if remaining_budget <= 0:
            break
        probe_budget = max(
            1,
            remaining_budget
            // max(1, len(frontier_candidates) - index),
        )
        path = _reconstruct_voxel_branch_path(
            current_cell,
            cell,
            predecessors,
        )
        if not path:
            continue
        probe_distance, exit_count, probe_terminal, probe_expanded = (
            _probe_voxel_branch_continuation(
                frontier_cell=cell,
                graph_cells=graph_cells,
                metrics=metrics,
                start_progress=start_progress,
                tolerance=tolerance,
                blocked_cells=frozenset(path),
                max_distance_m=max_distance,
                max_cells=max(4, int(lookahead_cells // 2)),
                expansion_budget=probe_budget,
            )
        )
        expanded_count += probe_expanded
        dead_end = (
            probe_terminal
            and probe_distance < max(cell_size * 2.0, lookahead_distance_m * 0.25)
            and exit_count <= 0
        )
        candidates.append(
            (
                cell,
                probe_distance,
                exit_count,
                dead_end,
                probe_expanded,
            )
        )

    if candidates:
        target, continuation, exit_count, dead_end, _probe_expanded = max(
            candidates,
            key=lambda item: (
                not item[3],
                float(item[1]),
                int(item[2]),
                float(distances.get(item[0], 0.0)),
                -int(depths.get(item[0], 0)),
            ),
        )
        path = _reconstruct_voxel_branch_path(
            current_cell,
            target,
            predecessors,
        )
        if not path:
            return None
        reached_distance = float(distances.get(target, 0.0))
        target_is_terminal = bool(dead_end)
        frontier_count = len(candidates)
    elif terminal:
        target = max(
            terminal,
            key=lambda cell: (
                float(distances.get(cell, 0.0)),
                -int(depths.get(cell, 0)),
            ),
        )
        path = _reconstruct_voxel_branch_path(
            current_cell,
            target,
            predecessors,
        )
        if not path:
            return None
        continuation = 0.0
        exit_count = 0
        dead_end = True
        reached_distance = float(distances.get(target, 0.0))
        target_is_terminal = True
        frontier_count = 0
    else:
        # A budget-limited branch that never reached the lookahead horizon is
        # still a usable candidate, but it is ranked below a branch with
        # explicit onward evidence.
        target = max(
            distances,
            key=lambda cell: float(distances.get(cell, 0.0)),
        )
        path = _reconstruct_voxel_branch_path(
            current_cell,
            target,
            predecessors,
        )
        if not path:
            return None
        continuation = 0.0
        exit_count = 0
        dead_end = True
        reached_distance = float(distances.get(target, 0.0))
        target_is_terminal = False
        frontier_count = 0

    score = NavigationVoxelBranchScore(
        branch_start_cell=branch_start,
        target_cell=target,
        reached_distance_m=reached_distance,
        continuation_distance_m=float(continuation),
        onward_exit_count=int(exit_count),
        frontier_count=int(frontier_count),
        first_step_alignment=_cell_direction_alignment(
            current_cell,
            branch_start,
            preferred_direction,
        ),
        path_cost_m=reached_distance,
        expanded_count=int(expanded_count),
        dead_end=bool(dead_end),
        target_is_terminal=bool(target_is_terminal),
    )
    sort_key: tuple[object, ...] = (
        not score.dead_end,
        float(score.continuation_distance_m),
        float(score.reached_distance_m),
        int(score.onward_exit_count),
        float(score.first_step_alignment),
        -float(score.path_cost_m),
        -int(score.branch_start_cell[0]),
        -int(score.branch_start_cell[1]),
    )
    return _NavigationVoxelBranchEvaluation(
        score=score,
        path=tuple(path),
        sort_key=sort_key,
    )


def _voxel_branch_neighbors(
    cell: FootprintCell,
    *,
    previous: FootprintCell | None,
    graph_cells: set[FootprintCell],
    metrics: Mapping[FootprintCell, NavigationVoxelCellMetric],
    start_progress: float,
    tolerance: float,
    progress_gain: float,
    current_cell: FootprintCell,
) -> tuple[FootprintCell, ...]:
    neighbors: list[FootprintCell] = []
    for neighbor in navigable_footprint_neighbors(cell, graph_cells):
        # The entrance/current cell is a hard no-return boundary for a local
        # branch search. Without this guard, a turn can re-enter the current
        # cell through a different neighbor and the bounded route collapses
        # back to the camera position.
        if neighbor == previous or neighbor == current_cell:
            continue
        progress = float(metrics[neighbor].progress_m)
        if progress < start_progress - tolerance:
            continue
        if (
            cell == current_cell
            and progress < start_progress + progress_gain * 0.25
        ):
            continue
        neighbors.append(neighbor)
    return tuple(sorted(neighbors))


def _reconstruct_voxel_branch_path(
    start: FootprintCell,
    target: FootprintCell,
    predecessors: Mapping[FootprintCell, FootprintCell],
) -> tuple[FootprintCell, ...]:
    path: list[FootprintCell] = [target]
    while path[-1] != start:
        previous = predecessors.get(path[-1])
        if previous is None:
            return ()
        path.append(previous)
    path.reverse()
    return tuple(path)


def _probe_voxel_branch_continuation(
    *,
    frontier_cell: FootprintCell,
    graph_cells: set[FootprintCell],
    metrics: Mapping[FootprintCell, NavigationVoxelCellMetric],
    start_progress: float,
    tolerance: float,
    blocked_cells: frozenset[FootprintCell],
    max_distance_m: float,
    max_cells: int,
    expansion_budget: int,
) -> tuple[float, int, bool, int]:
    """Probe only beyond a frontier to distinguish continuation from rooms."""
    distances: dict[FootprintCell, float] = {frontier_cell: 0.0}
    depths: dict[FootprintCell, int] = {frontier_cell: 0}
    queue: list[tuple[float, FootprintCell]] = [(0.0, frontier_cell)]
    max_reached = 0.0
    exit_count = 0
    terminal = False
    expanded = 0
    while queue and expanded < max(1, int(expansion_budget)):
        distance, cell = heapq.heappop(queue)
        if distance > distances.get(cell, float("inf")) + 1e-9:
            continue
        expanded += 1
        neighbors = [
            neighbor
            for neighbor in navigable_footprint_neighbors(cell, graph_cells)
            if neighbor not in blocked_cells
            and float(metrics[neighbor].progress_m)
            >= start_progress - tolerance
        ]
        if not neighbors:
            terminal = True
            continue
        for neighbor in neighbors:
            edge = footprint_cell_distance(cell, neighbor)
            next_distance = distance + max(1e-6, edge)
            next_depth = depths.get(cell, 0) + 1
            if next_distance > max_distance_m or next_depth >= max_cells:
                max_reached = max(max_reached, next_distance)
                exit_count += 1
                continue
            if next_distance + 1e-9 >= distances.get(
                neighbor,
                float("inf"),
            ):
                continue
            distances[neighbor] = next_distance
            depths[neighbor] = next_depth
            max_reached = max(max_reached, next_distance)
            heapq.heappush(queue, (next_distance, neighbor))
    return max_reached, exit_count, terminal, expanded


def _normalised_xz_direction(
    direction: Sequence[float] | None,
) -> tuple[float, float] | None:
    if direction is None:
        return None
    try:
        if len(direction) != 3:
            return None
        x = float(direction[0])
        z = float(direction[2])
    except (TypeError, ValueError):
        return None
    norm = math.hypot(x, z)
    if not math.isfinite(norm) or norm <= 1e-9:
        return None
    return x / norm, z / norm


def _cell_direction_alignment(
    first: FootprintCell,
    second: FootprintCell,
    direction: tuple[float, float] | None,
) -> float:
    if direction is None:
        return 0.0
    delta_x = second[0] - first[0]
    delta_z = second[1] - first[1]
    length = math.hypot(delta_x, delta_z)
    if length <= 1e-9:
        return 0.0
    return float(
        (delta_x / length) * direction[0]
        + (delta_z / length) * direction[1]
    )


def _voxel_graph_edge_cost(
    first: FootprintCell,
    second: FootprintCell,
    *,
    cell_size: float,
    voxel_size_m: float,
    metric: NavigationVoxelCellMetric,
    preferred_direction: tuple[float, float] | None,
) -> float:
    """Return a topology/direction cost for bounded branch exploration.

    Filled volume and clearance are intentionally not used here. They remain
    attached to each cell for diagnostics and the later cross-section policy,
    but using them during this first lookahead pass would make a large room
    win before the planner has checked whether that room continues.
    """
    base_distance = max(1e-6, footprint_cell_distance(first, second) * cell_size)
    del metric, voxel_size_m
    # Direction is a soft cost: a turn is valid even when the user's last
    # displacement points along the previous leg, but a backward first step
    # must not beat a forward continuation with comparable topology.
    cost_multiplier = 1.0
    alignment = _cell_direction_alignment(first, second, preferred_direction)
    if preferred_direction is not None and alignment < 0.0:
        cost_multiplier += min(1.5, -alignment * 1.25)
    elif preferred_direction is not None and alignment < 0.25:
        cost_multiplier += (0.25 - alignment) * 0.15
    return base_distance * cost_multiplier


def _bound_voxel_route_cells(
    cells: tuple[FootprintCell, ...],
    *,
    max_route_cells: int,
) -> tuple[FootprintCell, ...]:
    limit = max(2, int(max_route_cells))
    if len(cells) <= limit:
        return cells
    stride = max(1, math.ceil((len(cells) - 1) / max(1, limit - 1)))
    bounded = list(cells[::stride])
    if bounded[-1] != cells[-1]:
        bounded.append(cells[-1])
    return tuple(bounded[:limit])


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
            "largest_cached_cave_volume_v2"
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
        "tile_size_m": float(resolved.tile_size_m),
        "max_tiles": int(resolved.max_tiles),
        "coverage_scope": "entire_cave_component",
        "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
        "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
        "routes": model_routes,
    }
    if model_routes:
        navigation_metadata["voxel_cache"] = {
            "version": NAVIGATION_VOXEL_CACHE_VERSION,
            "method": NAVIGATION_VOXEL_CACHE_METHOD,
            "path": NAVIGATION_VOXEL_CACHE_NAME,
            "route_count": len(model_routes),
            "built_route_count": len(built_route_ids),
            "coverage_scope": "entire_cave_component",
            "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "tile_size_m": float(resolved.tile_size_m),
            "max_tiles": int(resolved.max_tiles),
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
) -> LocalVoxelVolume | NavigationVoxelAtlas | None:
    """Load one optional route voxel model from its bounded sidecar."""
    if not cache_dir:
        return None
    navigation = manifest.get("navigation")
    if not isinstance(navigation, Mapping):
        return None
    descriptor = navigation.get("voxel_cache")
    if not isinstance(descriptor, Mapping):
        return None
    descriptor_version = descriptor.get("version")
    descriptor_method = descriptor.get("method")
    if not _supported_cache_identity(descriptor_version, descriptor_method):
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
    if not _supported_cache_identity(payload.get("version"), payload.get("method")):
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
        return deserialize_navigation_voxel_volume(model)
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


def serialize_navigation_voxel_volume(
    volume: LocalVoxelVolume | NavigationVoxelAtlas,
) -> dict[str, object]:
    """Serialize either a legacy local field or the whole-cave atlas."""
    if isinstance(volume, NavigationVoxelAtlas):
        return {
            "version": NAVIGATION_VOXEL_CACHE_VERSION,
            "method": NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
            "coverage_scope": volume.coverage_scope,
            "tile_count": len(volume.tiles),
            "tiles": [serialize_local_voxel_volume(tile) for tile in volume.tiles],
            "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "cell_metrics": _serialize_cell_metrics(volume.cell_metrics),
        }
    return serialize_local_voxel_volume(volume)


def deserialize_navigation_voxel_volume(
    payload: Mapping[str, object],
    *,
    max_tiles: int = DEFAULT_CACHE_VOXEL_MAX_TILES,
) -> LocalVoxelVolume | NavigationVoxelAtlas:
    """Restore a legacy local field or a validated bounded voxel atlas."""
    if payload.get("method") == NAVIGATION_VOXEL_ATLAS_MODEL_METHOD:
        if payload.get("version") != NAVIGATION_VOXEL_CACHE_VERSION:
            raise ValueError("unsupported navigation voxel atlas version")
        raw_tiles = payload.get("tiles")
        if not isinstance(raw_tiles, Sequence) or isinstance(raw_tiles, (str, bytes)):
            raise ValueError("cached navigation voxel atlas tiles are missing")
        try:
            tile_count = int(payload.get("tile_count", len(raw_tiles)))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "cached navigation voxel atlas tile count is malformed"
            ) from exc
        if tile_count != len(raw_tiles) or tile_count <= 0:
            raise ValueError("cached navigation voxel atlas tile count is inconsistent")
        if tile_count > max(1, int(max_tiles)):
            raise ValueError("cached navigation voxel atlas has too many tiles")
        tiles: list[LocalVoxelVolume] = []
        for raw_tile in raw_tiles:
            if not isinstance(raw_tile, Mapping):
                raise ValueError("cached navigation voxel atlas tile is malformed")
            tiles.append(
                deserialize_local_voxel_volume(
                    raw_tile,
                    max_voxels=DEFAULT_CACHE_VOXEL_MAX_TILE_CELLS,
                )
            )
        cell_metrics = _deserialize_cell_metrics(payload.get("cell_metrics"))
        return NavigationVoxelAtlas(
            tiles=tuple(tiles),
            coverage_scope=str(
                payload.get("coverage_scope", "entire_cave_component")
            ),
            cell_metrics=cell_metrics,
        )
    return deserialize_local_voxel_volume(payload)


def _serialize_cell_metrics(
    metrics: Mapping[FootprintCell, NavigationVoxelCellMetric],
) -> list[list[float | int]]:
    """Serialize the bounded coarse graph without repeating object keys."""
    serialized: list[list[float | int]] = []
    for cell, metric in sorted(metrics.items())[:DEFAULT_CACHE_VOXEL_MAX_CELL_METRICS]:
        serialized.append(
            [
                int(cell[0]),
                int(cell[1]),
                float(metric.progress_m),
                float(metric.available_volume_m3),
                int(metric.free_cell_count),
                float(metric.min_clearance_m),
                float(metric.mean_clearance_m),
            ]
        )
    return serialized


def _deserialize_cell_metrics(
    value: object,
) -> dict[FootprintCell, NavigationVoxelCellMetric]:
    if value is None:
        return {}
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("cached navigation voxel cell metrics are malformed")
    if len(value) > DEFAULT_CACHE_VOXEL_MAX_CELL_METRICS:
        raise ValueError("cached navigation voxel cell metrics are too large")
    metrics: dict[FootprintCell, NavigationVoxelCellMetric] = {}
    for raw in value:
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes))
            or len(raw) != 7
        ):
            raise ValueError("cached navigation voxel cell metric is malformed")
        try:
            cell = (int(raw[0]), int(raw[1]))
            progress = float(raw[2])
            volume = float(raw[3])
            free_count = int(raw[4])
            minimum_clearance = float(raw[5])
            mean_clearance = float(raw[6])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "cached navigation voxel cell metric is malformed"
            ) from exc
        if (
            not all(
                math.isfinite(number)
                for number in (
                    progress,
                    volume,
                    minimum_clearance,
                    mean_clearance,
                )
            )
            or progress < 0.0
            or volume < 0.0
            or free_count < 0
            or minimum_clearance < 0.0
            or mean_clearance < 0.0
            or cell in metrics
        ):
            raise ValueError("cached navigation voxel cell metric is invalid")
        metrics[cell] = NavigationVoxelCellMetric(
            available_volume_m3=volume,
            free_cell_count=free_count,
            min_clearance_m=minimum_clearance,
            mean_clearance_m=mean_clearance,
            progress_m=progress,
        )
    return metrics


def deserialize_local_voxel_volume(
    payload: Mapping[str, object],
    *,
    max_voxels: int = DEFAULT_VOXEL_MAX_CELLS * 4,
) -> LocalVoxelVolume | NavigationVoxelAtlas:
    """Validate and restore a bounded sparse surface voxel model."""
    if payload.get("method") == NAVIGATION_VOXEL_ATLAS_MODEL_METHOD:
        return deserialize_navigation_voxel_volume(payload)
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
        "tile_size_m": float(config.tile_size_m),
        "max_tiles": int(config.max_tiles),
        "coverage_scope": "entire_cave_component",
        "coverage_includes_preceding_curvature": True,
        "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
        "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
        "point_count": len(points),
    }
    if len(points) < 2:
        common["outcome"] = "insufficient_route_points"
        common["built"] = False
        return common
    try:
        profile = analyze_polyline_curvature(
            points,
            window_points=config.window_points,
        )
        selected_regions = select_curvature_regions(
            profile,
            minimum_rank=config.curvature_rank_threshold,
            max_regions=config.max_regions,
            max_start_distance_m=None,
        )
        atlas, metrics, atlas_details = _build_route_voxel_atlas(
            manifest,
            route,
            points,
            triangle_provider=triangle_provider,
            config=config,
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
            "outcome": "built" if atlas is not None else "no_surface_samples",
            "built": atlas is not None,
            "curvature_sample_count": len(profile.samples),
            "curvature_region_count": len(profile.regions),
            "selected_region_count": len(selected_regions),
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
                for region in selected_regions
            ],
            **atlas_details,
        }
    )
    if atlas is None:
        return common

    route_length = _route_length(route, points, manifest)
    available_volume = float(metrics.get("available_volume_m3", 0.0))
    common.update(
        {
            **metrics,
            "route_length_m": float(route_length),
            "volume_per_route_m": float(
                available_volume / max(1e-6, route_length)
            ),
            "model": serialize_navigation_voxel_volume(atlas),
        }
    )
    # ``model`` is needed by the sidecar but is intentionally removed from the
    # small manifest summary by the caller.
    common["_model"] = common.pop("model")
    return common


def _build_route_voxel_atlas(
    manifest: Mapping[str, object],
    route: Mapping[str, object],
    points: tuple[Point, ...],
    *,
    triangle_provider: TriangleProvider,
    config: NavigationVoxelCacheConfig,
) -> tuple[
    NavigationVoxelAtlas | None,
    dict[str, float | int | bool],
    dict[str, object],
]:
    """Build bounded voxel tiles for every cell in one cave component."""
    component_cells = _flat_cells(route.get("component_cells"))
    coverage_scope = (
        "entire_cave_component"
        if component_cells
        else "route_cells_fallback"
    )
    if not component_cells:
        component_cells = _flat_cells(route.get("cells"))
    if not component_cells:
        return None, {}, {
            "coverage_cell_count": 0,
            "tile_count": 0,
            "coverage_scope": coverage_scope,
            "coverage_includes_preceding_curvature": False,
            "triangle_count": 0,
            "surface_sample_count": 0,
            "sampling_truncated": False,
        }

    cell_size = _route_cell_size(route, manifest)
    y_ranges = _route_y_ranges(route.get("component_y_ranges"), component_cells)
    fallback_y_range = _fallback_y_range(manifest, points)
    component_cell_set = set(component_cells)
    tile_size = _tile_size_for_component(
        component_cells,
        cell_size=cell_size,
        requested_tile_size=config.tile_size_m,
        max_tiles=config.max_tiles,
    )
    groups = _component_tile_groups(
        component_cells,
        cell_size=cell_size,
        tile_size=tile_size,
    )
    padding = max(config.voxel_size_m * 2.0, cell_size * 0.25)
    tiles: list[LocalVoxelVolume] = []
    total_metrics: list[dict[str, float | int | bool]] = []
    cell_accumulators: dict[FootprintCell, list[float]] = {}
    total_samples = 0
    total_triangles = 0
    sampling_truncated = False
    skipped_tiles = 0

    for group_index, cells in enumerate(groups):
        remaining_groups = max(1, len(groups) - group_index)
        remaining_samples = max(0, config.max_surface_samples - total_samples)
        if remaining_samples <= 0:
            sampling_truncated = True
            break
        bounds_min, bounds_max = _component_tile_bounds(
            cells,
            cell_size=cell_size,
            y_ranges=y_ranges,
            fallback_y_range=fallback_y_range,
            padding=padding,
        )
        tile_points = _tile_seed_points(
            cells,
            cell_size=cell_size,
            y_ranges=y_ranges,
            fallback_y_range=fallback_y_range,
        )
        tile_sample_budget = max(
            1,
            min(
                remaining_samples,
                max(128, math.ceil(remaining_samples / remaining_groups)),
            ),
        )
        tile = build_surface_voxel_volume(
            triangle_provider(bounds_min, bounds_max),
            bounds_min=bounds_min,
            bounds_max=bounds_max,
            config=VoxelVolumeConfig(
                voxel_size_m=config.voxel_size_m,
                max_voxels=min(
                    config.max_cells,
                    DEFAULT_CACHE_VOXEL_MAX_TILE_CELLS,
                ),
                max_surface_samples=tile_sample_budget,
            ),
        )
        total_triangles += int(tile.triangle_count)
        total_samples += int(tile.surface_sample_count)
        sampling_truncated = sampling_truncated or bool(tile.sampling_truncated)
        if tile.triangle_count <= 0 or tile.surface_sample_count <= 0:
            skipped_tiles += 1
            continue
        tiles.append(tile)
        filled_cells = tile.filled_free_cell_clearance_m(tile_points)
        total_metrics.append(
            _metrics_for_filled_cells(tile, tile_points, filled_cells)
        )
        for voxel_index, clearance_m in filled_cells.items():
            center = tile.voxel_center(voxel_index)
            cell = (
                math.floor(center[0] / cell_size),
                math.floor(center[2] / cell_size),
            )
            if cell not in component_cell_set:
                continue
            accumulator = cell_accumulators.setdefault(
                cell,
                [0.0, 0.0, float("inf"), 0.0],
            )
            accumulator[0] += 1.0
            accumulator[1] += float(clearance_m)
            accumulator[2] = min(accumulator[2], float(clearance_m))
            accumulator[3] += float(tile.voxel_size_m ** 3)

    if not tiles:
        return None, {}, {
            "coverage_cell_count": len(component_cells),
            "tile_count": 0,
            "coverage_scope": coverage_scope,
            "coverage_includes_preceding_curvature": coverage_scope
            == "entire_cave_component",
            "tile_size_m": float(tile_size),
            "tiles_skipped": int(skipped_tiles),
            "triangle_count": int(total_triangles),
            "surface_sample_count": int(total_samples),
            "sampling_truncated": bool(sampling_truncated),
            "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "navigation_cell_count": 0,
        }

    progress_distances = _component_progress_distances(
        component_cell_set,
        route,
        cell_size=cell_size,
    )
    cell_metrics = {
        cell: NavigationVoxelCellMetric(
            available_volume_m3=float(accumulator[3]),
            free_cell_count=int(accumulator[0]),
            min_clearance_m=float(accumulator[2]),
            mean_clearance_m=float(accumulator[1] / max(1.0, accumulator[0])),
            progress_m=float(progress_distances[cell]),
        )
        for cell, accumulator in cell_accumulators.items()
        if cell in progress_distances and accumulator[0] > 0.0
    }
    atlas = NavigationVoxelAtlas(
        tuple(tiles),
        coverage_scope=coverage_scope,
        cell_metrics=cell_metrics,
    )
    metrics = _aggregate_tile_metrics(total_metrics, atlas)
    details = {
        "bounds_min": _point_payload(atlas.bounds_min),
        "bounds_max": _point_payload(atlas.bounds_max),
        "tile_size_m": float(tile_size),
        "tile_count": len(tiles),
        "coverage_cell_count": len(component_cells),
        "coverage_scope": coverage_scope,
        "coverage_includes_preceding_curvature": coverage_scope
        == "entire_cave_component",
        "tiles_skipped": int(skipped_tiles),
        "model_kind": NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        "triangle_count": int(total_triangles),
        "surface_sample_count": int(total_samples),
        "sampling_truncated": bool(sampling_truncated),
        "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
        "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
        "navigation_cell_count": int(atlas.navigation_cell_count),
        "filled_free_cell_count": int(atlas.filled_free_cell_count),
        "progress_max_m": float(atlas.max_progress_m),
    }
    return atlas, metrics, details


def _tile_seed_points(
    cells: Sequence[FootprintCell],
    *,
    cell_size: float,
    y_ranges: Mapping[FootprintCell, tuple[float, float]],
    fallback_y_range: tuple[float, float],
) -> tuple[Point, ...]:
    """Return one bounded flood-fill seed for every cell in a tile."""
    points: list[Point] = []
    for cell in cells:
        x, z = footprint_world_center(cell, cell_size)
        low_y, high_y = _cell_y_range(cell, y_ranges, fallback_y_range)
        points.append((x, (low_y + high_y) * 0.5, z))
    return tuple(points)


def _metrics_for_filled_cells(
    tile: LocalVoxelVolume,
    seed_points: Sequence[Point],
    filled_cells: Mapping[tuple[int, int, int], float],
) -> dict[str, float | int | bool]:
    """Convert one filled tile into the existing corridor metric shape."""
    if not filled_cells:
        return {
            "seed_count": sum(
                1 for point in seed_points if tile.contains_point(point)
            ),
            "free_cell_count": 0,
            "available_volume_m3": 0.0,
            "surface_fraction": float(
                len(tile.surface_cells) / max(1, tile.voxel_count)
            ),
            "min_clearance_m": 0.0,
            "mean_clearance_m": 0.0,
            "clearance_sample_count": 0,
            "flood_fill_truncated": False,
        }
    ordered = sorted(filled_cells)
    sample_limit = 8192
    stride = max(1, math.ceil(len(ordered) / sample_limit))
    values = [float(filled_cells[index]) for index in ordered[::stride]]
    return {
        "seed_count": sum(
            1 for point in seed_points if tile.contains_point(point)
        ),
        "free_cell_count": len(filled_cells),
        "available_volume_m3": float(
            len(filled_cells) * tile.voxel_size_m ** 3
        ),
        "surface_fraction": float(
            len(tile.surface_cells) / max(1, tile.voxel_count)
        ),
        "min_clearance_m": min(values),
        "mean_clearance_m": float(sum(values) / max(1, len(values))),
        "clearance_sample_count": len(values),
        "flood_fill_truncated": False,
    }


def _component_progress_distances(
    component: set[FootprintCell],
    route: Mapping[str, object],
    *,
    cell_size: float,
) -> dict[FootprintCell, float]:
    """Measure graph depth from the cached route entrance through the component."""
    route_cells = _flat_cells(route.get("cells"))
    start = next((cell for cell in route_cells if cell in component), None)
    if start is None and component:
        start = min(component)
    if start is None:
        return {}
    distances: dict[FootprintCell, float] = {start: 0.0}
    queue: list[tuple[float, FootprintCell]] = [(0.0, start)]
    while queue:
        distance, cell = heapq.heappop(queue)
        if distance > distances.get(cell, float("inf")) + 1e-9:
            continue
        for neighbor in navigable_footprint_neighbors(cell, component):
            next_distance = distance + max(
                1e-6,
                footprint_cell_distance(cell, neighbor) * cell_size,
            )
            if next_distance + 1e-9 >= distances.get(
                neighbor,
                float("inf"),
            ):
                continue
            distances[neighbor] = next_distance
            heapq.heappush(queue, (next_distance, neighbor))
    return distances


def _aggregate_tile_metrics(
    metrics: Sequence[Mapping[str, float | int | bool]],
    atlas: NavigationVoxelAtlas,
) -> dict[str, float | int | bool]:
    if not metrics:
        return atlas.corridor_volume_metrics(())
    sample_count = sum(int(item.get("clearance_sample_count", 0)) for item in metrics)
    weighted_mean = sum(
        float(item.get("mean_clearance_m", 0.0))
        * int(item.get("clearance_sample_count", 0))
        for item in metrics
    ) / max(1, sample_count)
    return {
        "seed_count": sum(int(item.get("seed_count", 0)) for item in metrics),
        "free_cell_count": sum(
            int(item.get("free_cell_count", 0)) for item in metrics
        ),
        "available_volume_m3": sum(
            float(item.get("available_volume_m3", 0.0)) for item in metrics
        ),
        "surface_fraction": float(
            sum(len(tile.surface_cells) for tile in atlas.tiles)
            / max(1, atlas.voxel_count)
        ),
        "min_clearance_m": min(
            (
                float(item.get("min_clearance_m", 0.0))
                for item in metrics
                if int(item.get("clearance_sample_count", 0)) > 0
            ),
            default=0.0,
        ),
        "mean_clearance_m": float(weighted_mean),
        "clearance_sample_count": int(sample_count),
        "flood_fill_truncated": any(
            bool(item.get("flood_fill_truncated", False)) for item in metrics
        ),
    }


def _component_tile_groups(
    cells: Sequence[FootprintCell],
    *,
    cell_size: float,
    tile_size: float,
) -> tuple[tuple[FootprintCell, ...], ...]:
    grouped: dict[tuple[int, int], list[FootprintCell]] = {}
    for cell in cells:
        x, z = footprint_world_center(cell, cell_size)
        key = (
            math.floor(x / tile_size),
            math.floor(z / tile_size),
        )
        grouped.setdefault(key, []).append(cell)
    return tuple(
        tuple(sorted(grouped[key]))
        for key in sorted(grouped)
    )


def _tile_size_for_component(
    cells: Sequence[FootprintCell],
    *,
    cell_size: float,
    requested_tile_size: float,
    max_tiles: int,
) -> float:
    tile_size = max(float(requested_tile_size), cell_size * 2.0)
    for _ in range(32):
        groups = _component_tile_groups(
            cells,
            cell_size=cell_size,
            tile_size=tile_size,
        )
        if len(groups) <= max(1, int(max_tiles)):
            return tile_size
        tile_size *= max(1.25, math.sqrt(len(groups) / max(1, max_tiles)))
    return tile_size


def _component_tile_bounds(
    cells: Sequence[FootprintCell],
    *,
    cell_size: float,
    y_ranges: Mapping[FootprintCell, tuple[float, float]],
    fallback_y_range: tuple[float, float],
    padding: float,
) -> tuple[Point, Point]:
    centers = [footprint_world_center(cell, cell_size) for cell in cells]
    low_y = min(
        _cell_y_range(cell, y_ranges, fallback_y_range)[0]
        for cell in cells
    )
    high_y = max(
        _cell_y_range(cell, y_ranges, fallback_y_range)[1]
        for cell in cells
    )
    if high_y <= low_y:
        high_y = low_y + max(cell_size, padding * 2.0)
    return (
        # Keep X/Z tile footprints disjoint so summed corridor volumes do not
        # count the padding around neighboring tiles more than once.
        min(x for x, _z in centers) - cell_size * 0.5,
        low_y - padding,
        min(z for _x, z in centers) - cell_size * 0.5,
    ), (
        max(x for x, _z in centers) + cell_size * 0.5,
        high_y + padding,
        max(z for _x, z in centers) + cell_size * 0.5,
    )


def _cell_y_range(
    cell: FootprintCell,
    y_ranges: Mapping[FootprintCell, tuple[float, float]],
    fallback: tuple[float, float],
) -> tuple[float, float]:
    value = y_ranges.get(cell, fallback)
    return tuple(
        sorted((float(value[0]), float(value[1])))
    )  # type: ignore[return-value]


def _route_y_ranges(
    value: object,
    cells: Sequence[FootprintCell],
) -> dict[FootprintCell, tuple[float, float]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return {}
    if len(value) != len(cells) * 2:
        return {}
    parsed: dict[FootprintCell, tuple[float, float]] = {}
    for index, cell in enumerate(cells):
        try:
            low, high = float(value[index * 2]), float(value[index * 2 + 1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(low) and math.isfinite(high):
            parsed[cell] = tuple(sorted((low, high)))
    return parsed


def _fallback_y_range(
    manifest: Mapping[str, object],
    points: Sequence[Point],
) -> tuple[float, float]:
    values = [float(point[1]) for point in points]
    chunks = manifest.get("chunks")
    if isinstance(chunks, Mapping):
        for info in chunks.values():
            if not isinstance(info, Mapping):
                continue
            lower_bounds = info.get("bounds_min")
            upper_bounds = info.get("bounds_max")
            if (
                not isinstance(lower_bounds, Sequence)
                or isinstance(lower_bounds, (str, bytes))
                or not isinstance(upper_bounds, Sequence)
                or isinstance(upper_bounds, (str, bytes))
                or len(lower_bounds) != 3
                or len(upper_bounds) != 3
            ):
                continue
            try:
                lower = float(lower_bounds[1])
                upper = float(upper_bounds[1])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if math.isfinite(lower) and math.isfinite(upper):
                values.extend((lower, upper))
    if not values:
        values = [0.0, 1.0]
    return min(values), max(values)


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


def _supported_cache_identity(version: object, method: object) -> bool:
    """Accept the current atlas and the previous local sidecar format."""
    return (version, method) in {
        (NAVIGATION_VOXEL_CACHE_VERSION, NAVIGATION_VOXEL_CACHE_METHOD),
        (
            _LEGACY_NAVIGATION_VOXEL_CACHE_VERSION,
            _LEGACY_NAVIGATION_VOXEL_CACHE_METHOD,
        ),
    }


def _empty_payload(config: NavigationVoxelCacheConfig) -> dict[str, object]:
    return {
        "version": NAVIGATION_VOXEL_CACHE_VERSION,
        "method": NAVIGATION_VOXEL_CACHE_METHOD,
        "voxel_size_m": float(config.voxel_size_m),
        "curvature_method": CURVATURE_PROFILE_METHOD,
        "tile_size_m": float(config.tile_size_m),
        "max_tiles": int(config.max_tiles),
        "coverage_scope": "entire_cave_component",
        "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
        "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
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
