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
from collections import OrderedDict
import hashlib
import heapq
import math
import os
import threading
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
    DEFAULT_VOXEL_LOCAL_REFINEMENT_FORWARD_M,
    DEFAULT_VOXEL_LOCAL_REFINEMENT_MAX_CELLS,
    DEFAULT_VOXEL_SIZE_M,
    LocalVoxelVolume,
    LocalVoxelRoute,
    TriangleProvider,
    VoxelVolumeConfig,
    build_surface_voxel_volume,
)
from caveviewer.core.navigation.voxel_graph import (
    DEFAULT_GRAPH_MAX_EDGE_DISTANCE_CELLS,
    DEFAULT_GRAPH_MAX_EDGES_PER_NODE,
    NAVIGATION_VOXEL_GRAPH_METHOD as PREPARED_NAVIGATION_VOXEL_GRAPH_METHOD,
    NavigationVoxelGraph,
    NavigationVoxelGraphEdge,
    NavigationVoxelGraphNode,
    build_navigation_voxel_graph,
    deserialize_navigation_voxel_graph,
    serialize_navigation_voxel_graph,
)
from caveviewer.core.navigation.voxel_graph_3d import (
    DEFAULT_3D_GRAPH_MAX_EDGE_DISTANCE_CELLS,
    DEFAULT_3D_GRAPH_MAX_EDGES_PER_NODE,
    DEFAULT_3D_GRAPH_MAX_NODES,
    DEFAULT_3D_GRAPH_MAX_EDGES,
    LEGACY_NAVIGATION_MESH_3D_GRAPH_METHOD,
    NAVIGATION_VOXEL_3D_GRAPH_METHOD,
    NavigationVoxel3DEdge,
    NavigationVoxel3DGraph,
    NavigationVoxel3DMetric,
    VoxelGraphKey,
    accumulate_navigation_voxel_3d_sample,
    build_navigation_voxel_3d_graph,
    deserialize_navigation_voxel_3d_graph,
    finalize_navigation_voxel_3d_metrics,
    serialize_navigation_voxel_3d_graph,
    shortest_navigation_voxel_3d_graph_path,
)
from caveviewer.core.navigation.mesh_graph import (
    MESH_NAVIGATION_GRAPH_METHOD,
    MeshEdgeSafetyCheck,
    MeshNavigationGraphAnchor,
    MeshNavigationGraphBuildResult,
    MeshNavigationGraphConfig,
    build_goal_directed_seeded_mesh_navigation_path_graph,
)
from caveviewer.core.navigation.voxel_store import (
    DEFAULT_NAVIGATION_VOXEL_CHUNK_MAX_BYTES,
    DEFAULT_NAVIGATION_VOXEL_CHUNK_MAX_RESIDENT,
    DiskNavigationVoxelChunkStore,
    InMemoryNavigationVoxelChunkStore,
    NavigationVoxelChunkDescriptor,
    NavigationVoxelChunkStore,
    NAVIGATION_VOXEL_CHUNK_STORAGE_METHOD,
)


NAVIGATION_VOXEL_CACHE_VERSION = 10
NAVIGATION_VOXEL_CACHE_METHOD = "whole_cave_voxel_atlas_v10"
# Version 10 adds a compact mesh-derived roadmap alongside the existing voxel
# graph.  The voxel atlas remains available for probes and bounded local
# recovery, but Guided Dive treats the direct-mesh roadmap as its production
# route authority after a rebuild.
_PREVIOUS_NAVIGATION_VOXEL_CACHE_VERSION = 9
_PREVIOUS_NAVIGATION_VOXEL_CACHE_METHOD = "whole_cave_voxel_atlas_v9"
_OLDER_NAVIGATION_VOXEL_CACHE_VERSION = 8
_OLDER_NAVIGATION_VOXEL_CACHE_METHOD = "whole_cave_voxel_atlas_v8"
_ANCIENT_NAVIGATION_VOXEL_CACHE_VERSION = 7
_ANCIENT_NAVIGATION_VOXEL_CACHE_METHOD = "whole_cave_voxel_atlas_v7"
_HISTORIC_NAVIGATION_VOXEL_CACHE_VERSION = 6
_HISTORIC_NAVIGATION_VOXEL_CACHE_METHOD = "whole_cave_voxel_atlas_v6"
_LEGACY_PREPARED_NAVIGATION_VOXEL_CACHE_VERSION = 3
_LEGACY_PREPARED_NAVIGATION_VOXEL_CACHE_METHOD = "whole_cave_voxel_atlas_v3"
_LEGACY_NAVIGATION_VOXEL_CACHE_VERSION = 1
_LEGACY_NAVIGATION_VOXEL_CACHE_METHOD = "curvature_corridor_voxels_v1"
NAVIGATION_VOXEL_CACHE_NAME = "navigation_voxels.json"
NAVIGATION_VOXEL_CACHE_MAX_BYTES = 256 * 1024 * 1024
NAVIGATION_VOXEL_ATLAS_MODEL_METHOD = "navigation_voxel_atlas_v10"
_PREVIOUS_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD = "navigation_voxel_atlas_v9"
_OLDER_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD = "navigation_voxel_atlas_v8"
_ANCIENT_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD = "navigation_voxel_atlas_v7"
_HISTORIC_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD = "navigation_voxel_atlas_v6"
_LEGACY_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD = "navigation_voxel_atlas_v3"

# Replans run on a worker, but repeatedly decoding the whole-cave sidecar still
# consumed most of the worker budget on consumer hardware. Keep a small,
# signature-keyed process cache of the parsed payload and restored route model.
# The file signature invalidates both entries when the cache is rebuilt.
_RUNTIME_VOXEL_PAYLOAD_CACHE_LIMIT = 4
_RUNTIME_VOXEL_MODEL_CACHE_LIMIT = 8
_RUNTIME_PROBE_CACHE_MAX_ENTRIES = 16_384
_runtime_voxel_cache_lock = threading.RLock()
_runtime_voxel_payload_cache: OrderedDict[
    tuple[str, int, int], Mapping[str, object]
] = OrderedDict()
_runtime_voxel_model_cache: OrderedDict[
    tuple[str, int, int, str], LocalVoxelVolume | NavigationVoxelAtlas
] = OrderedDict()

# Guided Dive cache construction is now the accuracy tier. Runtime planning
# still stays bounded, but the offline cache is allowed to preserve much more
# of the sparse 1 m navigation field.
DEFAULT_CACHE_VOXEL_SIZE_M = DEFAULT_VOXEL_SIZE_M
DEFAULT_CACHE_VOXEL_RANK_THRESHOLD = DEFAULT_VOXEL_CURVATURE_RANK_THRESHOLD
DEFAULT_CACHE_VOXEL_MAX_REGIONS = DEFAULT_VOXEL_MAX_REGIONS
DEFAULT_CACHE_VOXEL_MAX_CELLS = 65_536
DEFAULT_CACHE_VOXEL_MAX_SURFACE_SAMPLES = 250_000
DEFAULT_CACHE_VOXEL_MAX_ROUTES = 4
DEFAULT_CACHE_VOXEL_WINDOW_POINTS = 3
DEFAULT_CACHE_VOXEL_TILE_SIZE_M = 64.0
DEFAULT_CACHE_VOXEL_MAX_TILES = 256
# The mesh roadmap is intentionally a single easiest-terminal experiment.
# Keep only a short metadata ingress to connect the user-visible route start
# to the selected voxel spine; the rest of the centerline is not topology.
DEFAULT_MESH_GRAPH_ENTRY_SEED_CELLS = 12
DEFAULT_MESH_GRAPH_ENTRY_SEED_POINTS = 8
# The metadata route begins near the normal camera entry but its first voxel
# spine node need not be the one that has a mesh-clear connector from that
# pose. Preserve a small, bounded true-3D neighborhood as mesh-roadmap anchor
# candidates so preflight can choose the first exact-safe handoff.
DEFAULT_MESH_GRAPH_ENTRY_ANCHOR_RADIUS_M = 24.0
# A bounded second pass repairs footprint cells missed by the distributed
# surface-sampling budget. It is deliberately separate from the main budget:
# one missed component cell should not force the whole cave to use a much
# larger resident surface field.
DEFAULT_CACHE_VOXEL_COVERAGE_REPAIR_SAMPLE_BUDGET = 262_144
DEFAULT_CACHE_VOXEL_MAX_TILE_CELLS = 65_536
DEFAULT_CACHE_VOXEL_MAX_CELL_METRICS = 65_536
DEFAULT_CACHE_FINE_VOXEL_SIZE_M = 1.0
DEFAULT_CACHE_FINE_TILE_RADIUS_M = 16.0
# Fine tiles form a contiguous corridor along the prepared easiest-terminal
# graph spine, rather than a sparse collection of bend/frontier samples or an
# unrelated imported centerline. The budget covers roughly 1.1 km at the
# default 16 m radius and 12 m spacing; it remains bounded and fails closed
# for a longer selected path.
DEFAULT_CACHE_FINE_MAX_TILES = 96
DEFAULT_CACHE_FINE_MAX_TILE_CELLS = 131_072
# A fixed-route portal is allowed to use a fine tile only when that tile has
# sampled every relevant cached triangle.  This is deliberately a per-tile
# budget: splitting a cave corridor into more tiles must not make each tile's
# mesh evidence sparser.
DEFAULT_CACHE_FINE_MAX_SURFACE_SAMPLES = 250_000
# Fine occupancy proposes local graph nodes; exact cached-mesh checks remain
# the authority for executable geometry.  Do not inflate a 1 m fine surface
# cell here: a valid graph/camera point can share a cell with a sloped wall.
DEFAULT_CACHE_FINE_SURFACE_INFLATION_CELLS = 0
DEFAULT_CACHE_GRAPH_MAX_NODES = DEFAULT_3D_GRAPH_MAX_NODES
DEFAULT_CACHE_GRAPH_MAX_EDGES = DEFAULT_3D_GRAPH_MAX_EDGES
DEFAULT_CACHE_GRAPH_MAX_EDGE_DISTANCE_CELLS = (
    DEFAULT_3D_GRAPH_MAX_EDGE_DISTANCE_CELLS
)
DEFAULT_CACHE_GRAPH_MAX_EDGES_PER_NODE = DEFAULT_3D_GRAPH_MAX_EDGES_PER_NODE
NAVIGATION_VOXEL_GRAPH_METHOD = NAVIGATION_VOXEL_3D_GRAPH_METHOD
NAVIGATION_VOXEL_FOOTPRINT_GRAPH_METHOD = PREPARED_NAVIGATION_VOXEL_GRAPH_METHOD
_PREVIOUS_NAVIGATION_VOXEL_GRAPH_METHOD = "voxel_filled_component_graph_v1"
NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD = "voxel_branch_lookahead_v1"
DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_DISTANCE_M = 256.0
DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_CELLS = 32
DEFAULT_NAVIGATION_VOXEL_BRANCH_MAX_CANDIDATES = 8
DEFAULT_NAVIGATION_VOXEL_BRANCH_MAX_EXPANSIONS = 2_048
# A route may turn sideways, but an explicit travel direction must never select
# the first step of a branch that points back toward the entrance.
DEFAULT_NAVIGATION_VOXEL_MIN_FORWARD_ALIGNMENT = 0.0
NAVIGATION_VOXEL_SCORING_POLICY_METHOD = "connectivity_forward_volume_v1"
NAVIGATION_VOXEL_LOOP_POLICY_AVOID = "avoid"
NAVIGATION_VOXEL_LOOP_POLICY_ALLOW_FORWARD = "allow_forward"
NAVIGATION_VOXEL_LOOP_POLICIES = frozenset(
    {
        NAVIGATION_VOXEL_LOOP_POLICY_AVOID,
        NAVIGATION_VOXEL_LOOP_POLICY_ALLOW_FORWARD,
    }
)
DEFAULT_NAVIGATION_VOXEL_CONNECTIVITY_WEIGHT = 1.0
DEFAULT_NAVIGATION_VOXEL_SMOOTH_FORWARD_WEIGHT = 1.0
DEFAULT_NAVIGATION_VOXEL_VOLUME_WEIGHT = 0.15
DEFAULT_NAVIGATION_VOXEL_CLEARANCE_WEIGHT = 0.10
DEFAULT_NAVIGATION_VOXEL_TURN_WEIGHT = 0.55
DEFAULT_NAVIGATION_VOXEL_BACKTRACK_WEIGHT = 1.0
DEFAULT_NAVIGATION_VOXEL_GRAPH_MAX_EDGE_DISTANCE_CELLS = (
    DEFAULT_GRAPH_MAX_EDGE_DISTANCE_CELLS
)
DEFAULT_NAVIGATION_VOXEL_GRAPH_MAX_EDGES_PER_NODE = (
    DEFAULT_GRAPH_MAX_EDGES_PER_NODE
)


@dataclass(frozen=True)
class NavigationVoxelCacheConfig:
    """Accuracy-tier cache-time voxel and graph construction settings."""

    voxel_size_m: float = DEFAULT_CACHE_VOXEL_SIZE_M
    curvature_rank_threshold: int = DEFAULT_CACHE_VOXEL_RANK_THRESHOLD
    max_regions: int = DEFAULT_CACHE_VOXEL_MAX_REGIONS
    max_cells: int = DEFAULT_CACHE_VOXEL_MAX_CELLS
    max_surface_samples: int = DEFAULT_CACHE_VOXEL_MAX_SURFACE_SAMPLES
    max_routes: int = DEFAULT_CACHE_VOXEL_MAX_ROUTES
    window_points: int = DEFAULT_CACHE_VOXEL_WINDOW_POINTS
    tile_size_m: float = DEFAULT_CACHE_VOXEL_TILE_SIZE_M
    max_tiles: int = DEFAULT_CACHE_VOXEL_MAX_TILES
    coverage_repair_sample_budget: int = (
        DEFAULT_CACHE_VOXEL_COVERAGE_REPAIR_SAMPLE_BUDGET
    )
    fine_voxel_size_m: float = DEFAULT_CACHE_FINE_VOXEL_SIZE_M
    fine_tile_radius_m: float = DEFAULT_CACHE_FINE_TILE_RADIUS_M
    max_fine_tiles: int = DEFAULT_CACHE_FINE_MAX_TILES
    max_fine_tile_cells: int = DEFAULT_CACHE_FINE_MAX_TILE_CELLS
    fine_max_surface_samples: int = DEFAULT_CACHE_FINE_MAX_SURFACE_SAMPLES
    graph_max_nodes: int = DEFAULT_CACHE_GRAPH_MAX_NODES
    graph_max_edges: int = DEFAULT_CACHE_GRAPH_MAX_EDGES
    graph_max_edge_distance_cells: int = DEFAULT_CACHE_GRAPH_MAX_EDGE_DISTANCE_CELLS
    graph_max_edges_per_node: int = DEFAULT_CACHE_GRAPH_MAX_EDGES_PER_NODE
    mesh_graph_enabled: bool = True
    # These are roadmap waypoint spacings, not source-voxel resolution. The
    # seeded component uses a uniform 2 m execution lattice and accepts
    # topology only through exact cached-mesh neighbor checks.
    mesh_graph_horizontal_sample_spacing_m: float = 2.0
    mesh_graph_vertical_sample_spacing_m: float = 2.0
    mesh_graph_minimum_clearance_m: float = 0.25
    mesh_graph_max_nodes: int = 96_000
    mesh_graph_max_edges_per_node: int = 16
    mesh_graph_max_edge_candidates_per_node: int = 32
    mesh_graph_max_edge_candidates_per_direction: int = 2
    mesh_graph_max_edge_distance_m: float = 16.0
    mesh_graph_max_vertical_edge_distance_m: float = 8.0
    mesh_graph_entry_anchor_radius_m: float = (
        DEFAULT_MESH_GRAPH_ENTRY_ANCHOR_RADIUS_M
    )

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
        coverage_repair_sample_budget = max(
            0,
            int(self.coverage_repair_sample_budget),
        )
        fine_size = float(self.fine_voxel_size_m)
        if not math.isfinite(fine_size) or fine_size <= 0.0:
            raise ValueError("fine voxel size must be positive and finite")
        fine_radius = float(self.fine_tile_radius_m)
        if not math.isfinite(fine_radius) or fine_radius <= 0.0:
            raise ValueError("fine voxel tile radius must be positive and finite")
        max_fine_tiles = max(0, int(self.max_fine_tiles))
        max_fine_cells = max(1, int(self.max_fine_tile_cells))
        fine_max_surface_samples = max(
            4_096,
            int(self.fine_max_surface_samples),
        )
        graph_max_nodes = max(2, int(self.graph_max_nodes))
        graph_max_edges = max(1, int(self.graph_max_edges))
        graph_max_edge_distance_cells = max(
            1,
            int(self.graph_max_edge_distance_cells),
        )
        graph_max_edges_per_node = max(6, int(self.graph_max_edges_per_node))
        mesh_graph_config = MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=self.mesh_graph_horizontal_sample_spacing_m,
            vertical_sample_spacing_m=self.mesh_graph_vertical_sample_spacing_m,
            minimum_clearance_m=self.mesh_graph_minimum_clearance_m,
            max_nodes=self.mesh_graph_max_nodes,
            max_edges_per_node=self.mesh_graph_max_edges_per_node,
            max_edge_candidates_per_node=(
                self.mesh_graph_max_edge_candidates_per_node
            ),
            max_edge_candidates_per_direction=(
                self.mesh_graph_max_edge_candidates_per_direction
            ),
            max_edge_distance_m=self.mesh_graph_max_edge_distance_m,
            max_vertical_edge_distance_m=self.mesh_graph_max_vertical_edge_distance_m,
        ).validated()
        entry_anchor_radius = float(self.mesh_graph_entry_anchor_radius_m)
        if not math.isfinite(entry_anchor_radius) or entry_anchor_radius <= 0.0:
            raise ValueError("mesh graph entry anchor radius must be positive")
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
            coverage_repair_sample_budget=coverage_repair_sample_budget,
            fine_voxel_size_m=fine_size,
            fine_tile_radius_m=fine_radius,
            max_fine_tiles=max_fine_tiles,
            max_fine_tile_cells=max_fine_cells,
            fine_max_surface_samples=fine_max_surface_samples,
            graph_max_nodes=graph_max_nodes,
            graph_max_edges=graph_max_edges,
            graph_max_edge_distance_cells=graph_max_edge_distance_cells,
            graph_max_edges_per_node=graph_max_edges_per_node,
            mesh_graph_enabled=bool(self.mesh_graph_enabled),
            mesh_graph_horizontal_sample_spacing_m=(
                mesh_graph_config.horizontal_sample_spacing_m
            ),
            mesh_graph_vertical_sample_spacing_m=(
                mesh_graph_config.vertical_sample_spacing_m
            ),
            mesh_graph_minimum_clearance_m=(
                mesh_graph_config.minimum_clearance_m
            ),
            mesh_graph_max_nodes=mesh_graph_config.max_nodes,
            mesh_graph_max_edges_per_node=(
                mesh_graph_config.max_edges_per_node
            ),
            mesh_graph_max_edge_candidates_per_node=(
                mesh_graph_config.max_edge_candidates_per_node
            ),
            mesh_graph_max_edge_candidates_per_direction=(
                mesh_graph_config.max_edge_candidates_per_direction
            ),
            mesh_graph_max_edge_distance_m=(
                mesh_graph_config.max_edge_distance_m
            ),
            mesh_graph_max_vertical_edge_distance_m=(
                mesh_graph_config.max_vertical_edge_distance_m
            ),
            mesh_graph_entry_anchor_radius_m=entry_anchor_radius,
        )

    def mesh_navigation_graph_config(self) -> MeshNavigationGraphConfig:
        """Return the normalized direct-mesh roadmap configuration."""
        return MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=self.mesh_graph_horizontal_sample_spacing_m,
            vertical_sample_spacing_m=self.mesh_graph_vertical_sample_spacing_m,
            minimum_clearance_m=self.mesh_graph_minimum_clearance_m,
            max_nodes=self.mesh_graph_max_nodes,
            max_edges_per_node=self.mesh_graph_max_edges_per_node,
            max_edge_candidates_per_node=(
                self.mesh_graph_max_edge_candidates_per_node
            ),
            max_edge_candidates_per_direction=(
                self.mesh_graph_max_edge_candidates_per_direction
            ),
            max_edge_distance_m=self.mesh_graph_max_edge_distance_m,
            max_vertical_edge_distance_m=self.mesh_graph_max_vertical_edge_distance_m,
        ).validated()


@dataclass(frozen=True)
class NavigationVoxelCacheBuildResult:
    """Result of an optional cache-time navigation voxel pass."""

    payload: dict[str, object]
    built_route_count: int
    recommended_route_id: str | None
    chunked_payload: dict[str, object] | None = None
    chunk_payloads: Mapping[str, Mapping[str, object]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class NavigationVoxelCellMetric:
    """Filled free-space measurements associated with one footprint cell."""

    available_volume_m3: float
    free_cell_count: int
    min_clearance_m: float
    mean_clearance_m: float
    progress_m: float
    center_y_m: float = 0.0


@dataclass(frozen=True)
class NavigationVoxelScoringPolicy:
    """Per-request priorities for bounded voxel branch selection.

    Reverse edges remain illegal in every policy. ``allow_forward`` only lets
    a bounded search revisit a previously seen voxel when the new edge still
    lies in the incoming forward hemisphere; the revisit receives an explicit
    backtracking penalty and is still bounded by the heading-state search.
    """

    connectivity_weight: float = DEFAULT_NAVIGATION_VOXEL_CONNECTIVITY_WEIGHT
    smooth_forward_weight: float = DEFAULT_NAVIGATION_VOXEL_SMOOTH_FORWARD_WEIGHT
    volume_weight: float = DEFAULT_NAVIGATION_VOXEL_VOLUME_WEIGHT
    clearance_weight: float = DEFAULT_NAVIGATION_VOXEL_CLEARANCE_WEIGHT
    turn_weight: float = DEFAULT_NAVIGATION_VOXEL_TURN_WEIGHT
    backtrack_weight: float = DEFAULT_NAVIGATION_VOXEL_BACKTRACK_WEIGHT
    loop_policy: str = NAVIGATION_VOXEL_LOOP_POLICY_AVOID

    def __post_init__(self) -> None:
        for name in (
            "connectivity_weight",
            "smooth_forward_weight",
            "volume_weight",
            "clearance_weight",
            "turn_weight",
            "backtrack_weight",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"voxel {name} must be finite and non-negative")
        if self.loop_policy not in NAVIGATION_VOXEL_LOOP_POLICIES:
            raise ValueError(
                "voxel loop policy must be 'avoid' or 'allow_forward'"
            )

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "method": NAVIGATION_VOXEL_SCORING_POLICY_METHOD,
            "connectivity_weight": float(self.connectivity_weight),
            "smooth_forward_weight": float(self.smooth_forward_weight),
            "volume_weight": float(self.volume_weight),
            "clearance_weight": float(self.clearance_weight),
            "turn_weight": float(self.turn_weight),
            "backtrack_weight": float(self.backtrack_weight),
            "loop_policy": str(self.loop_policy),
            "reverse_edges": "rejected",
        }

    def branch_sort_key(
        self,
        score: "NavigationVoxelBranchScore",
    ) -> tuple[object, ...]:
        """Return the architecture's lexicographic branch priority.

        Safety/topology validity comes first. Among viable candidates the
        order is connectivity, smooth forward progress, backtracking penalty,
        and finally volume as a comfort tie-breaker.
        """
        return (
            not score.dead_end,
            bool(score.unknown_boundary),
            float(self.connectivity_weight) * float(score.connectivity_score),
            float(self.smooth_forward_weight)
            * float(score.smooth_forward_score),
            -float(self.backtrack_weight) * float(score.backtrack_penalty),
            float(self.volume_weight) * float(score.volume_score),
            int(score.onward_exit_count),
            float(score.reached_distance_m),
            -float(score.path_cost_m),
        )


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
    connectivity_score: float = 0.0
    heading_state_count: int = 0
    unknown_boundary: bool = False
    branch_start_key: tuple[int, ...] | None = None
    target_key: tuple[int, ...] | None = None
    graph_method: str = NAVIGATION_VOXEL_FOOTPRINT_GRAPH_METHOD
    revisited_footprint_count: int = 0
    entrance_floor_rejections: int = 0
    route_volume_m3: float = 0.0
    smooth_forward_score: float = 0.0
    volume_score: float = 0.0
    backtrack_penalty: float = 0.0
    weighted_score: float = 0.0
    scoring_policy_method: str = NAVIGATION_VOXEL_SCORING_POLICY_METHOD

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
            "branch_start_key": (
                None
                if self.branch_start_key is None
                else [int(value) for value in self.branch_start_key]
            ),
            "target_key": (
                None
                if self.target_key is None
                else [int(value) for value in self.target_key]
            ),
            "graph_method": str(self.graph_method),
            "reached_distance_m": float(self.reached_distance_m),
            "continuation_distance_m": float(self.continuation_distance_m),
            "onward_exit_count": int(self.onward_exit_count),
            "frontier_count": int(self.frontier_count),
            "first_step_alignment": float(self.first_step_alignment),
            "path_cost_m": float(self.path_cost_m),
            "expanded_count": int(self.expanded_count),
            "dead_end": bool(self.dead_end),
            "target_is_terminal": bool(self.target_is_terminal),
            "connectivity_score": float(self.connectivity_score),
            "heading_state_count": int(self.heading_state_count),
            "unknown_boundary": bool(self.unknown_boundary),
            "revisited_footprint_count": int(self.revisited_footprint_count),
            "entrance_floor_rejections": int(
                self.entrance_floor_rejections
            ),
            "route_volume_m3": float(self.route_volume_m3),
            "smooth_forward_score": float(self.smooth_forward_score),
            "volume_score": float(self.volume_score),
            "backtrack_penalty": float(self.backtrack_penalty),
            "weighted_score": float(self.weighted_score),
            "scoring_policy_method": str(self.scoring_policy_method),
        }


def _navigation_smooth_forward_score(
    *,
    first_step_alignment: float,
    continuation_distance_m: float,
    lookahead_distance_m: float,
) -> float:
    """Normalize heading smoothness and useful forward reach to [0, 1]."""
    alignment = max(0.0, min(1.0, float(first_step_alignment)))
    horizon = max(1e-6, float(lookahead_distance_m))
    continuation = max(
        0.0,
        min(1.0, float(continuation_distance_m) / horizon),
    )
    return 0.65 * alignment + 0.35 * continuation


def _navigation_volume_score(route_volume_m3: float) -> float:
    """Compress route volume so it remains a comfort tie-breaker."""
    return math.log1p(max(0.0, float(route_volume_m3)))


def _navigation_weighted_branch_score(
    *,
    policy: NavigationVoxelScoringPolicy,
    connectivity_score: float,
    smooth_forward_score: float,
    volume_score: float,
    backtrack_penalty: float,
) -> float:
    return (
        float(policy.connectivity_weight) * float(connectivity_score)
        + float(policy.smooth_forward_weight) * float(smooth_forward_score)
        + float(policy.volume_weight) * float(volume_score)
        - float(policy.backtrack_weight) * float(backtrack_penalty)
    )


@dataclass(frozen=True)
class _NavigationVoxelBranchEvaluation:
    """Internal branch score paired with its bounded route prefix."""

    score: NavigationVoxelBranchScore
    path: tuple[FootprintCell, ...]
    sort_key: tuple[object, ...]


@dataclass(frozen=True)
class _NavigationVoxel3DBranchEvaluation:
    """Internal true-3D branch score paired with voxel centers."""

    score: NavigationVoxelBranchScore
    path: tuple[VoxelGraphKey, ...]
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
    prepared_graph: bool = False
    heading_state_count: int = 0
    connectivity_score: float = 0.0
    terminal_reached: bool = False
    unknown_boundary_reached: bool = False
    dead_end_rejections: int = 0
    world_points: tuple[Point, ...] = ()
    three_d_graph: bool = False
    graph_keys: tuple[VoxelGraphKey, ...] = ()
    entrance_progress_floor_m: float | None = None
    entrance_guard_tolerance_m: float | None = None
    entrance_guard_source: str | None = None
    scoring_policy: Mapping[str, object] | None = None

    def diagnostic_payload(self) -> dict[str, object]:
        """Return route-selection details suitable for the debug log."""
        return {
            "method": (
                NAVIGATION_VOXEL_GRAPH_METHOD
                if self.three_d_graph
                else (
                    self.branch_score.graph_method
                    if self.branch_score is not None
                    else NAVIGATION_VOXEL_FOOTPRINT_GRAPH_METHOD
                )
            ),
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
            "prepared_graph": bool(self.prepared_graph),
            "three_d_graph": bool(self.three_d_graph),
            "world_point_count": len(self.world_points),
            "graph_key_count": len(self.graph_keys),
            "graph_keys": [
                [int(value) for value in key]
                for key in self.graph_keys[:8]
            ],
            "heading_state_count": int(self.heading_state_count),
            "connectivity_score": float(self.connectivity_score),
            "terminal_reached": bool(self.terminal_reached),
            "unknown_boundary_reached": bool(self.unknown_boundary_reached),
            "dead_end_rejections": int(self.dead_end_rejections),
            "entrance_progress_floor_m": (
                None
                if self.entrance_progress_floor_m is None
                else float(self.entrance_progress_floor_m)
            ),
            "entrance_guard_tolerance_m": (
                None
                if self.entrance_guard_tolerance_m is None
                else float(self.entrance_guard_tolerance_m)
            ),
            "entrance_guard_source": self.entrance_guard_source,
            "scoring_policy": (
                None
                if self.scoring_policy is None
                else dict(self.scoring_policy)
            ),
            "progress_guard_mode": (
                "entrance_band_only"
                if self.entrance_progress_floor_m is not None
                else "legacy_graph_policy"
            ),
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
            "first_world_points": [
                [float(value) for value in point]
                for point in self.world_points[:8]
            ],
            "last_world_points": [
                [float(value) for value in point]
                for point in self.world_points[-8:]
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
    prepared_graph: NavigationVoxelGraph | None = None
    prepared_3d_graph: NavigationVoxel3DGraph | None = None
    prepared_mesh_graph: NavigationVoxel3DGraph | None = None
    # Maximum bounded camera-to-roadmap ingress considered at preflight.  The
    # connector is still accepted only after exact voxel and cached-mesh
    # validation; persisting this build policy prevents runtime behavior from
    # depending on per-map environment variables.
    mesh_graph_entry_anchor_radius_m: float = 0.0
    fine_tiles: tuple[LocalVoxelVolume, ...] = ()
    chunk_store: NavigationVoxelChunkStore | None = None
    _probe_tile_bucket_size_m: float = field(
        init=False,
        repr=False,
        compare=False,
    )
    _probe_tiles: tuple[LocalVoxelVolume, ...] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _probe_tile_index: Mapping[tuple[int, int], tuple[int, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _probe_result_cache: dict[
        tuple[Point, bool],
        tuple[bool, float] | None,
    ] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Build a small immutable spatial dispatch table for point probes."""
        probe_tiles = tuple(self.fine_tiles) + tuple(self.tiles)
        object.__setattr__(self, "_probe_tiles", probe_tiles)
        object.__setattr__(self, "_probe_result_cache", {})
        if not probe_tiles:
            object.__setattr__(self, "_probe_tile_bucket_size_m", 1.0)
            object.__setattr__(self, "_probe_tile_index", {})
            return

        horizontal_sizes = sorted(
            max(
                float(tile.bounds_max[0] - tile.bounds_min[0]),
                float(tile.bounds_max[2] - tile.bounds_min[2]),
                1.0,
            )
            for tile in probe_tiles
        )
        bucket_size = horizontal_sizes[len(horizontal_sizes) // 2]
        index: dict[tuple[int, int], list[int]] = {}
        for tile_index, tile in enumerate(probe_tiles):
            min_x = math.floor(tile.bounds_min[0] / bucket_size)
            max_x = math.floor(tile.bounds_max[0] / bucket_size)
            min_z = math.floor(tile.bounds_min[2] / bucket_size)
            max_z = math.floor(tile.bounds_max[2] / bucket_size)
            for bucket_x in range(min_x, max_x + 1):
                for bucket_z in range(min_z, max_z + 1):
                    index.setdefault((bucket_x, bucket_z), []).append(tile_index)
        object.__setattr__(self, "_probe_tile_bucket_size_m", bucket_size)
        object.__setattr__(
            self,
            "_probe_tile_index",
            {
                key: tuple(value)
                for key, value in index.items()
            },
        )

    @property
    def voxel_count(self) -> int:
        """Return coarse atlas capacity without double-counting refinements."""
        if self.chunk_store is not None and not self.tiles:
            return int(
                sum(
                    descriptor.voxel_count
                    for descriptor in self.chunk_store.descriptors(
                        fine_only=False,
                    )
                )
            )
        return int(sum(tile.voxel_count for tile in self.tiles))

    @property
    def fine_voxel_count(self) -> int:
        """Return capacity added by persisted fine frontier tiles."""
        if self.chunk_store is not None and not self.fine_tiles:
            return int(
                sum(
                    descriptor.voxel_count
                    for descriptor in self.chunk_store.descriptors(
                        fine_only=True,
                    )
                )
            )
        return int(sum(tile.voxel_count for tile in self.fine_tiles))

    @property
    def tile_count(self) -> int:
        """Return the number of coarse chunks represented by this atlas."""
        if self.chunk_store is not None and not self.tiles:
            return len(self.chunk_store.descriptors(fine_only=False))
        return len(self.tiles)

    @property
    def fine_tile_count(self) -> int:
        """Return the number of fine chunks represented by this atlas."""
        if self.chunk_store is not None and not self.fine_tiles:
            return len(self.chunk_store.descriptors(fine_only=True))
        return len(self.fine_tiles)

    @property
    def surface_cells(self) -> frozenset[tuple[int, int, int]]:
        """Expose sparse occupancy for compatibility with local fields."""
        cells: set[tuple[int, int, int]] = set()
        for tile in tuple(self.tiles) + tuple(self.fine_tiles):
            cells.update(tile.surface_cells)
        if self.chunk_store is not None and not cells:
            for chunk_id in self.chunk_store.resident_chunk_ids():
                tile = self.chunk_store.get_chunk(chunk_id)
                if tile is not None:
                    cells.update(tile.surface_cells)
        return frozenset(cells)

    @property
    def voxel_size_m(self) -> float:
        all_tiles = tuple(self.tiles) + tuple(self.fine_tiles)
        if not all_tiles:
            if self.chunk_store is None:
                return 0.0
            return min(
                (
                    float(descriptor.voxel_size_m)
                    for descriptor in self.chunk_store.descriptors()
                ),
                default=0.0,
            )
        return float(min(tile.voxel_size_m for tile in all_tiles))

    @property
    def fine_voxel_size_m(self) -> float:
        """Return the finest persisted local refinement resolution."""
        if not self.fine_tiles:
            if self.chunk_store is None:
                return 0.0
            return min(
                (
                    float(descriptor.voxel_size_m)
                    for descriptor in self.chunk_store.descriptors(
                        fine_only=True,
                    )
                ),
                default=0.0,
            )
        return float(min(tile.voxel_size_m for tile in self.fine_tiles))

    @property
    def navigation_cell_count(self) -> int:
        """Return the number of prepared 3D nodes or footprint cells."""
        if self.has_prepared_3d_graph:
            return len(self.prepared_3d_graph.nodes)
        return len(self.cell_metrics)

    @property
    def navigation_3d_cell_count(self) -> int:
        """Return the number of independent X/Y/Z navigation nodes."""
        return (
            0
            if self.prepared_3d_graph is None
            else len(self.prepared_3d_graph.nodes)
        )

    @property
    def mesh_navigation_cell_count(self) -> int:
        """Return the number of direct-mesh roadmap nodes, if persisted."""
        return (
            0
            if self.prepared_mesh_graph is None
            else len(self.prepared_mesh_graph.nodes)
        )

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
        if self.has_prepared_3d_graph:
            return max(
                (
                    float(node.progress_m)
                    for node in self.prepared_3d_graph.nodes.values()
                ),
                default=0.0,
            )
        return max(
            (float(metric.progress_m) for metric in self.cell_metrics.values()),
            default=0.0,
        )

    @property
    def has_prepared_graph(self) -> bool:
        """Return whether cache-time heading/visibility data is available."""
        return self.prepared_graph is not None and bool(
            self.prepared_graph.nodes
        )

    @property
    def has_prepared_3d_graph(self) -> bool:
        """Return whether the true 3D cache graph is available."""
        return self.prepared_3d_graph is not None and bool(
            self.prepared_3d_graph.nodes
        )

    @property
    def has_prepared_mesh_graph(self) -> bool:
        """Return whether a direct-mesh free-space roadmap is available."""
        return self.prepared_mesh_graph is not None and bool(
            self.prepared_mesh_graph.nodes
        )

    @property
    def authoritative_graph(self) -> NavigationVoxel3DGraph | None:
        """Return the preferred graph without discarding voxel evidence.

        Version-10 caches use the direct-mesh roadmap for production routing.
        Older readable sidecars retain their voxel graph for compatibility
        callers only; authority checks decide whether that fallback is legal.
        """
        if self.has_prepared_mesh_graph:
            return self.prepared_mesh_graph
        return self.prepared_3d_graph

    @property
    def has_authoritative_graph(self) -> bool:
        graph = self.authoritative_graph
        return graph is not None and bool(graph.nodes)

    @property
    def authoritative_motion_geometry_safe(self) -> bool:
        graph = self.authoritative_graph
        return bool(graph is not None and graph.motion_geometry_safe)

    @property
    def prepared_3d_motion_geometry_safe(self) -> bool:
        """Return whether prepared graph centers may drive the camera."""
        return bool(
            self.prepared_3d_graph is not None
            and self.prepared_3d_graph.motion_geometry_safe
        )

    @property
    def bounds_min(self) -> Point:
        all_tiles = tuple(self.tiles) + tuple(self.fine_tiles)
        if not all_tiles:
            descriptors = (
                ()
                if self.chunk_store is None
                else self.chunk_store.descriptors()
            )
            if not descriptors:
                return (0.0, 0.0, 0.0)
            return tuple(
                min(descriptor.bounds_min[axis] for descriptor in descriptors)
                for axis in range(3)
            )  # type: ignore[return-value]
        return tuple(
            min(tile.bounds_min[axis] for tile in all_tiles)
            for axis in range(3)
        )  # type: ignore[return-value]

    @property
    def bounds_max(self) -> Point:
        all_tiles = tuple(self.tiles) + tuple(self.fine_tiles)
        if not all_tiles:
            descriptors = (
                ()
                if self.chunk_store is None
                else self.chunk_store.descriptors()
            )
            if not descriptors:
                return (0.0, 0.0, 0.0)
            return tuple(
                max(descriptor.bounds_max[axis] for descriptor in descriptors)
                for axis in range(3)
            )  # type: ignore[return-value]
        return tuple(
            max(tile.bounds_max[axis] for tile in all_tiles)
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
        tiles: list[LocalVoxelVolume] = list(self.tiles)
        tiles.extend(self.fine_tiles)
        if not tiles and self.chunk_store is not None:
            chunk_ids = self.chunk_store.chunk_ids_for_point(desired_point)
            tiles = [
                tile
                for chunk_id in chunk_ids
                if (tile := self.chunk_store.get_chunk(chunk_id)) is not None
            ]
        for tile in tiles:
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

    def probe_point(
        self,
        point: Sequence[float],
        *,
        include_clearance: bool = True,
    ) -> tuple[bool, float] | None:
        """Return the best cached surface-field result for one point."""
        cache_point: Point | None = None
        try:
            candidate = tuple(float(value) for value in point)
        except (TypeError, ValueError):
            candidate = ()
        if len(candidate) == 3 and all(
            math.isfinite(value) for value in candidate
        ):
            cache_point = candidate  # type: ignore[assignment]
        cache_key = (
            (cache_point, bool(include_clearance))
            if cache_point is not None
            else None
        )
        if cache_key is not None and cache_key in self._probe_result_cache:
            return self._probe_result_cache[cache_key]
        result = self._probe_point_uncached(
            point,
            include_clearance=include_clearance,
        )
        if (
            cache_key is not None
            and len(self._probe_result_cache) < _RUNTIME_PROBE_CACHE_MAX_ENTRIES
        ):
            self._probe_result_cache[cache_key] = result
        return result

    def _probe_point_uncached(
        self,
        point: Sequence[float],
        *,
        include_clearance: bool,
    ) -> tuple[bool, float] | None:
        if self.chunk_store is not None and not self._probe_tiles:
            fine_result = self.probe_fine_point(
                point,
                include_clearance=include_clearance,
            )
            if fine_result is not None and fine_result[0]:
                return fine_result
            # A fine surface voxel is a conservative one-metre raster cell.
            # It can contain a valid graph point beside a sloped triangle, so
            # let independent coarse free-space evidence decide that point.
            # Every executable segment still reaches the exact mesh guard.
            fine_occupied = fine_result is not None
            free_clearances: list[float] = []
            occupied = False
            for chunk_id in self.chunk_store.chunk_ids_for_point(
                point,
                fine_only=False,
            ):
                tile = self.chunk_store.get_chunk(chunk_id)
                if tile is None:
                    continue
                result = tile.probe_point(
                    point,
                    include_clearance=include_clearance,
                )
                if result is None:
                    continue
                is_free, clearance_m = result
                if is_free:
                    free_clearances.append(float(clearance_m))
                else:
                    occupied = True
            if free_clearances:
                return True, max(free_clearances)
            if occupied:
                return False, 0.0
            if fine_occupied:
                return False, 0.0
            return None
        try:
            bucket = (
                math.floor(float(point[0]) / self._probe_tile_bucket_size_m),
                math.floor(float(point[2]) / self._probe_tile_bucket_size_m),
            )
        except (IndexError, TypeError, ValueError, ZeroDivisionError):
            return None
        free_clearances: list[float] = []
        occupied = False
        fine_result = self.probe_fine_point(
            point,
            include_clearance=include_clearance,
        )
        if fine_result is not None and fine_result[0]:
            return fine_result
        fine_occupied = fine_result is not None
        for tile_index in self._probe_tile_index.get(bucket, ()):
            if tile_index < len(self.fine_tiles):
                continue
            tile = self._probe_tiles[tile_index]
            result = tile.probe_point(
                point,
                include_clearance=include_clearance,
            )
            if result is None:
                continue
            is_free, clearance_m = result
            if is_free:
                free_clearances.append(float(clearance_m))
            else:
                occupied = True
        if free_clearances:
            return True, max(free_clearances)
        if occupied:
            return False, 0.0
        if fine_occupied:
            return False, 0.0
        return None

    def fine_tile_for_point(
        self,
        point: Sequence[float],
    ) -> LocalVoxelVolume | None:
        """Return the persisted fine tile covering a world point, if any."""
        if self.chunk_store is not None and not self.fine_tiles:
            for chunk_id in self.chunk_store.chunk_ids_for_point(
                point,
                fine_only=True,
            ):
                tile = self.chunk_store.get_chunk(chunk_id)
                if tile is not None and tile.contains_point(point):
                    return tile
            return None
        try:
            bucket = (
                math.floor(
                    float(point[0]) / self._probe_tile_bucket_size_m
                ),
                math.floor(
                    float(point[2]) / self._probe_tile_bucket_size_m
                ),
            )
        except (IndexError, TypeError, ValueError, ZeroDivisionError):
            return None
        for tile_index in self._probe_tile_index.get(bucket, ()):
            if tile_index >= len(self.fine_tiles):
                continue
            tile = self.fine_tiles[tile_index]
            if tile.contains_point(point):
                return tile
        return None

    def fine_tiles_covering_points(
        self,
        points: Sequence[Sequence[float]],
    ) -> tuple[LocalVoxelVolume, ...]:
        """Return persisted fine tiles that cover every requested point.

        A fixed-route refinement bridge must stay inside one 1 m evidence
        field. Keeping this lookup on the atlas lets disk-backed caches retain
        their LRU behaviour instead of materializing every fine tile merely to
        find an overlap.
        """
        normalized: list[Point] = []
        for point in points:
            if len(point) != 3:
                return ()
            try:
                candidate = tuple(float(value) for value in point)
            except (TypeError, ValueError):
                return ()
            if not all(math.isfinite(value) for value in candidate):
                return ()
            normalized.append(candidate)  # type: ignore[arg-type]
        if not normalized:
            return ()
        if self.chunk_store is not None and not self.fine_tiles:
            candidate_ids: set[str] | None = None
            for point in normalized:
                point_ids = set(
                    self.chunk_store.chunk_ids_for_point(
                        point,
                        fine_only=True,
                    )
                )
                candidate_ids = (
                    point_ids
                    if candidate_ids is None
                    else candidate_ids & point_ids
                )
                if not candidate_ids:
                    return ()
            return tuple(
                tile
                for chunk_id in sorted(candidate_ids or ())
                if (tile := self.chunk_store.get_chunk(chunk_id)) is not None
                and all(tile.contains_point(point) for point in normalized)
            )
        return tuple(
            tile
            for tile in self.fine_tiles
            if all(tile.contains_point(point) for point in normalized)
        )

    def probe_fine_point(
        self,
        point: Sequence[float],
        *,
        include_clearance: bool = True,
    ) -> tuple[bool, float] | None:
        """Query only persisted fine frontier tiles."""
        if self.chunk_store is not None and not self.fine_tiles:
            free_clearances: list[float] = []
            occupied = False
            for chunk_id in self.chunk_store.chunk_ids_for_point(
                point,
                fine_only=True,
            ):
                tile = self.chunk_store.get_chunk(chunk_id)
                if tile is None:
                    continue
                result = tile.probe_point(
                    point,
                    include_clearance=include_clearance,
                )
                if result is None:
                    continue
                if result[0]:
                    free_clearances.append(float(result[1]))
                else:
                    occupied = True
            if free_clearances:
                return True, max(free_clearances)
            if occupied:
                return False, 0.0
            return None
        free_clearances: list[float] = []
        occupied = False
        for tile in self.fine_tiles:
            result = tile.probe_point(
                point,
                include_clearance=include_clearance,
            )
            if result is None:
                continue
            if result[0]:
                free_clearances.append(float(result[1]))
            else:
                occupied = True
        if free_clearances:
            return True, max(free_clearances)
        if occupied:
            return False, 0.0
        return None

    def prefetch_for_points(
        self,
        points: Sequence[Sequence[float]],
    ) -> tuple[str, ...]:
        """Prefetch chunks intersecting a bounded navigation horizon."""
        if self.chunk_store is None:
            return ()
        chunk_ids: list[str] = []
        for point in points:
            chunk_ids.extend(self.chunk_store.chunk_ids_for_point(point))
        return self.chunk_store.prefetch(tuple(dict.fromkeys(chunk_ids)))

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
    ) -> LocalVoxelRoute | None:
        """Search the fine tile containing the current frontier."""
        tile = self.fine_tile_for_point(current)
        if tile is None:
            return None
        candidates: list[LocalVoxelRoute] = []
        for tile in (tile,):
            route = tile.find_forward_route(
                current,
                forward,
                max_distance_m=max_distance_m,
                max_nodes=max_nodes,
                min_target_distance_m=min_target_distance_m,
                deadline_monotonic_s=deadline_monotonic_s,
                edge_safety_check=edge_safety_check,
                allow_diagonal=allow_diagonal,
            )
            if route is not None:
                candidates.append(route)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda route: (
                float(route.forward_progress_m),
                int(route.branch_free_voxel_count),
                float(route.distance_m),
                int(route.target_connectivity),
            ),
        )

    def corridor_volume_metrics(
        self,
        points: Sequence[Sequence[float]],
    ) -> dict[str, float | int | bool]:
        """Aggregate corridor metrics across all atlas tiles."""
        if self.chunk_store is not None and not self.tiles:
            chunk_ids: list[str] = []
            for point in points:
                chunk_ids.extend(
                    self.chunk_store.chunk_ids_for_point(point)
                )
            tiles = [
                tile
                for chunk_id in dict.fromkeys(chunk_ids)
                if (tile := self.chunk_store.get_chunk(chunk_id)) is not None
            ]
        else:
            tiles = list(self.tiles)
        metrics = [
            tile.corridor_volume_metrics(
                tuple(
                    point
                    for point in points
                    if tile.contains_point(point)
                )
            )
            for tile in tiles
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
        voxel_capacity = sum(tile.voxel_count for tile in tiles)
        surface_cells = sum(len(tile.surface_cells) for tile in tiles)
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
        edge_safety_check: Callable[
            [VoxelGraphKey, VoxelGraphKey], bool
        ] | None = None,
        max_expansions: int = DEFAULT_NAVIGATION_VOXEL_BRANCH_MAX_EXPANSIONS,
        max_route_cells: int = 512,
        backtrack_tolerance_m: float | None = None,
        min_progress_gain_m: float | None = None,
        lookahead_distance_m: float | None = None,
        lookahead_cells: int = DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_CELLS,
        max_branch_candidates: int = DEFAULT_NAVIGATION_VOXEL_BRANCH_MAX_CANDIDATES,
        scoring_policy: NavigationVoxelScoringPolicy | None = None,
        diagnostics: Callable[[str, Mapping[str, object]], None] | None = None,
        deadline_check: Callable[[], None] | None = None,
    ) -> NavigationVoxelRoutePlan | None:
        """Select a forward branch with bounded continuation lookahead.

        A current cache uses the prepared true-3D graph as the route source.
        The footprint graph remains available only for readable older caches.
        The cache entrance is a small no-return boundary, but progress is not
        otherwise monotonic: a forward heading-aware branch may move to a
        shallower or deeper region. Each immediate branch is explored only to
        a bounded lookahead and scored by its future connectivity.
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
        policy = scoring_policy or NavigationVoxelScoringPolicy()
        if deadline_check is not None:
            deadline_check()
        if self.has_authoritative_graph:
            return self._plan_prepared_3d_graph_route(
                component_cells=component_cells,
                position=position,
                footprint_cell_size=cell_size,
                preferred_direction=preferred_direction,
                edge_safety_check=edge_safety_check,
                max_expansions=max_expansions,
                max_route_cells=max_route_cells,
                backtrack_tolerance_m=backtrack_tolerance_m,
                min_progress_gain_m=min_progress_gain_m,
                lookahead_distance_m=lookahead_distance_m,
                lookahead_cells=lookahead_cells,
                max_branch_candidates=max_branch_candidates,
                scoring_policy=policy,
                diagnostics=diagnostics,
                deadline_check=deadline_check,
            )
        if self.has_prepared_graph:
            return self._plan_prepared_graph_route(
                component_cells=component_cells,
                position=position,
                footprint_cell_size=cell_size,
                preferred_direction=preferred_direction,
                max_expansions=max_expansions,
                max_route_cells=max_route_cells,
                backtrack_tolerance_m=backtrack_tolerance_m,
                min_progress_gain_m=min_progress_gain_m,
                lookahead_distance_m=lookahead_distance_m,
                lookahead_cells=lookahead_cells,
                max_branch_candidates=max_branch_candidates,
                scoring_policy=policy,
                diagnostics=diagnostics,
                deadline_check=deadline_check,
            )
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
            if deadline_check is not None:
                deadline_check()
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
                scoring_policy=policy,
                deadline_check=deadline_check,
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
            scoring_policy=policy.diagnostic_payload(),
        )

    def _plan_prepared_3d_graph_route(
        self,
        *,
        component_cells: Sequence[FootprintCell]
        | set[FootprintCell]
        | frozenset[FootprintCell],
        position: tuple[float, float, float],
        footprint_cell_size: float,
        preferred_direction: Sequence[float] | None,
        edge_safety_check: Callable[
            [VoxelGraphKey, VoxelGraphKey], bool
        ] | None,
        max_expansions: int,
        max_route_cells: int,
        backtrack_tolerance_m: float | None,
        min_progress_gain_m: float | None,
        lookahead_distance_m: float | None,
        lookahead_cells: int,
        max_branch_candidates: int,
        scoring_policy: NavigationVoxelScoringPolicy,
        diagnostics: Callable[[str, Mapping[str, object]], None] | None,
        deadline_check: Callable[[], None] | None,
    ) -> NavigationVoxelRoutePlan | None:
        """Search independent X/Y/Z nodes with bounded heading-aware states."""
        graph = self.authoritative_graph
        if graph is None:
            return None
        if deadline_check is not None:
            deadline_check()
        # True-3D graph nodes use the graph's native sparse voxel coordinates.
        # The caller's component_cells belong to the coarse centerline grid and
        # must never be compared directly with node.footprint_cell.
        del component_cells
        graph_index = graph.runtime_index
        if len(graph_index.keys) < 2:
            _record_voxel_route_diagnostic(
                diagnostics,
                "voxel_route_rejected",
                {
                    "reason": "true_3d_graph_too_small",
                    "prepared_graph": True,
                    "true_3d": True,
                    "graph_node_count": len(graph_index.keys),
                },
            )
            return None

        # Select the nearest node from the routable subset first.  A sparse
        # cache can contain an isolated sample closer to the camera than the
        # actual prepared route; anchoring the component to that sample would
        # make a valid route appear unavailable at startup.
        nearest_key, _nearest_distance_squared = graph_index.nearest_key(
            position,
            routable_only=True,
        )
        if nearest_key is None:
            nearest_key, _nearest_distance_squared = graph_index.nearest_key(
                position,
            )
        if nearest_key is None:
            return None
        graph_component_id = int(graph.nodes[nearest_key].component_id)
        graph_keys = graph_index.key_sets_by_component.get(
            graph_component_id,
            frozenset(),
        )
        # Keep the search in the graph-native topological component.  The
        # nearest routable node above normally is already in this set, but
        # recomputing it here makes the invariant explicit after filtering.
        route_start_keys = graph_index.routable_keys_by_component.get(
            graph_component_id,
            frozenset(),
        )
        current_key, _current_distance_squared = graph_index.nearest_key(
            position,
            component_id=graph_component_id,
            routable_only=bool(route_start_keys),
        )
        if current_key is None:
            return None
        current_node = graph.nodes[current_key]
        start_progress = float(current_node.progress_m)
        graph_scale = max(
            float(footprint_cell_size),
            float(self.voxel_size_m),
            *(float(value) for value in graph.grid_size_m),
        )
        tolerance, entrance_guard_source = (
            _true_3d_entrance_guard_tolerance(
                footprint_cell_size=footprint_cell_size,
                voxel_size_m=self.voxel_size_m,
                graph_grid_size_m=graph.grid_size_m,
                backtrack_tolerance_m=backtrack_tolerance_m,
            )
        )
        # ``min_progress_gain_m`` remains part of the shared planner API for
        # older footprint graphs. True-3D routing uses heading and topology;
        # it must not turn centerline depth into a monotonic altitude/depth
        # constraint.
        minimum_progress = graph_index.component_min_progress.get(
            graph_component_id
        )
        if minimum_progress is None:
            minimum_progress = min(
                float(graph.nodes[key].progress_m) for key in graph_keys
            )
        minimum_progress = float(minimum_progress)
        entrance_band = minimum_progress + tolerance
        # Keep the whole initial entrance band out of later route prefixes,
        # including when planning starts inside that band. Otherwise a route
        # can leave the entrance, turn through a vertical cross-section, and
        # legally re-enter a different voxel at the same entrance depth.
        entrance_progress_floor = entrance_band
        heading = _normalised_graph_direction(preferred_direction)
        requested_lookahead = (
            DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_DISTANCE_M
            if lookahead_distance_m is None
            else float(lookahead_distance_m)
        )
        if not math.isfinite(requested_lookahead) or requested_lookahead <= 0.0:
            requested_lookahead = DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_DISTANCE_M
        lookahead_distance = max(graph_scale * 2.0, requested_lookahead)
        expansion_limit = max(1, int(max_expansions))
        branch_edges: list[
            tuple[NavigationVoxel3DEdge, float]
        ] = []
        outgoing_edges = graph.outgoing(current_key)
        rejected_nontraversable = 0
        rejected_component = 0
        rejected_entrance_floor = 0
        rejected_backward = 0
        rejected_safety = 0
        allowed_initial_entrance_edges = 0
        for edge in outgoing_edges:
            if deadline_check is not None:
                deadline_check()
            if not edge.line_of_sight or edge.target not in graph_keys:
                rejected_nontraversable += 1
                continue
            if not _true_3d_edge_stays_in_component(
                graph,
                edge,
                component_id=graph_component_id,
            ):
                rejected_component += 1
                continue
            if (
                edge_safety_check is not None
                and not edge_safety_check(current_key, edge.target)
            ):
                rejected_safety += 1
                continue
            target_node = graph.nodes[edge.target]
            if target_node.progress_m < entrance_progress_floor - 1e-6:
                # The entrance guard applies after the first departure edge.
                # A camera can start inside the guarded band while the next
                # safe graph sample is still inside that same band; rejecting
                # that edge makes the planner report no route at startup.
                # Keep the initial departure bounded and do not allow it to
                # backtrack farther than the entrance tolerance. Every later
                # edge remains subject to the full floor in the branch search.
                if (
                    start_progress > entrance_progress_floor + 1e-6
                    or target_node.progress_m
                    < start_progress - tolerance - 1e-6
                ):
                    rejected_entrance_floor += 1
                    continue
                allowed_initial_entrance_edges += 1
            alignment = (
                1.0
                if heading is None
                else _graph_dot(heading, edge.direction)
            )
            # In 3-D, forward/reverse is the signed dot product. A cross
            # product is perpendicular to the travel vector and cannot encode
            # whether a candidate points back toward the entrance.
            if heading is not None and alignment < 0.0:
                rejected_backward += 1
                continue
            branch_edges.append((edge, alignment))

        edge_filter = {
            "outgoing_edge_count": len(outgoing_edges),
            "rejected_nontraversable_edges": int(rejected_nontraversable),
            "rejected_component_edges": int(rejected_component),
            "rejected_entrance_floor_edges": int(rejected_entrance_floor),
            "rejected_backward_edges": int(rejected_backward),
            "rejected_safety_edges": int(rejected_safety),
            "allowed_initial_entrance_edges": int(
                allowed_initial_entrance_edges
            ),
            "accepted_forward_edges": len(branch_edges),
        }
        _record_voxel_route_diagnostic(
            diagnostics,
            "voxel_route_edge_filter",
            {
                "prepared_graph": True,
                "true_3d": True,
                "start_key": [int(value) for value in current_key],
                "start_cell": [
                    int(current_node.footprint_cell[0]),
                    int(current_node.footprint_cell[1]),
                ],
                "start_progress_m": start_progress,
                "graph_component_id": graph_component_id,
                "graph_native_component_filter": True,
                "minimum_cached_progress_m": float(minimum_progress),
                "entrance_progress_floor_m": float(
                    entrance_progress_floor
                ),
                "entrance_guard_tolerance_m": float(tolerance),
                "entrance_guard_source": entrance_guard_source,
                "heading": _graph_direction_payload(heading),
                "scoring_policy": scoring_policy.diagnostic_payload(),
                **edge_filter,
            },
        )

        branch_edges.sort(
            key=lambda item: (
                float(scoring_policy.connectivity_weight)
                * float(graph.nodes[item[0].target].connectivity_score),
                float(scoring_policy.smooth_forward_weight)
                * float(item[1]),
                float(scoring_policy.volume_weight)
                * _navigation_volume_score(
                    float(graph.nodes[item[0].target].available_volume_m3)
                ),
                float(graph.nodes[item[0].target].progress_m),
                -float(item[0].distance_m),
            ),
            reverse=True,
        )
        if not branch_edges:
            _record_voxel_route_diagnostic(
                diagnostics,
                "voxel_route_rejected",
                {
                    "reason": "no_forward_continuation",
                    "prepared_graph": True,
                    "true_3d": True,
                    "start_key": [int(value) for value in current_key],
                    "start_cell": [
                        int(current_node.footprint_cell[0]),
                        int(current_node.footprint_cell[1]),
                    ],
                    "start_progress_m": start_progress,
                    "graph_component_id": graph_component_id,
                    "entrance_progress_floor_m": float(
                        entrance_progress_floor
                    ),
                    "entrance_guard_tolerance_m": float(tolerance),
                    "entrance_guard_source": entrance_guard_source,
                    "heading": _graph_direction_payload(heading),
                    **edge_filter,
                },
            )
            return None

        branch_budget = max(
            8,
            expansion_limit // max(1, len(branch_edges)),
        )
        evaluations: list[_NavigationVoxel3DBranchEvaluation] = []
        for edge, alignment in branch_edges:
            if deadline_check is not None:
                deadline_check()
            evaluation = _evaluate_prepared_3d_graph_branch(
                graph=graph,
                graph_keys=graph_keys,
                graph_component_id=graph_component_id,
                current_key=current_key,
                first_edge=edge,
                first_alignment=alignment,
                edge_safety_check=edge_safety_check,
                entrance_progress_floor_m=entrance_progress_floor,
                lookahead_distance_m=lookahead_distance,
                lookahead_cells=max(4, int(lookahead_cells)),
                expansion_budget=branch_budget,
                graph_scale_m=graph_scale,
                scoring_policy=scoring_policy,
                deadline_check=deadline_check,
            )
            if evaluation is not None:
                evaluations.append(evaluation)

        if not evaluations:
            _record_voxel_route_diagnostic(
                diagnostics,
                "voxel_route_rejected",
                {
                    "reason": "true_3d_graph_search_empty",
                    "prepared_graph": True,
                    "true_3d": True,
                    "start_key": [int(value) for value in current_key],
                    "entrance_progress_floor_m": float(
                        entrance_progress_floor
                    ),
                    "entrance_guard_tolerance_m": float(tolerance),
                    "entrance_guard_source": entrance_guard_source,
                    "heading": _graph_direction_payload(heading),
                    "branch_count": len(branch_edges),
                    **edge_filter,
                },
            )
            return None

        continuing = [
            item
            for item in evaluations
            if not item.score.dead_end
            and (
                item.score.continuation_distance_m > 0.0
                or item.score.unknown_boundary
            )
        ]
        terminal_candidates = [
            item
            for item in evaluations
            if item.score.target_is_terminal
            and not item.score.unknown_boundary
        ]
        if continuing:
            selected = max(continuing, key=lambda item: item.sort_key)
            terminal_reached = False
            selection_reason = "prepared_true_3d_graph"
        elif terminal_candidates:
            selected = max(
                terminal_candidates,
                key=lambda item: scoring_policy.branch_sort_key(item.score),
            )
            terminal_reached = True
            selection_reason = "cave_terminal"
        else:
            _record_voxel_route_diagnostic(
                diagnostics,
                "voxel_route_rejected",
                {
                    "reason": "no_non_dead_end_forward_branch",
                    "prepared_graph": True,
                    "true_3d": True,
                    "start_key": [int(value) for value in current_key],
                    "entrance_progress_floor_m": float(
                        entrance_progress_floor
                    ),
                    "entrance_guard_tolerance_m": float(tolerance),
                    "entrance_guard_source": entrance_guard_source,
                    "heading": _graph_direction_payload(heading),
                    "scoring_policy": scoring_policy.diagnostic_payload(),
                    "branch_count": len(branch_edges),
                    **edge_filter,
                    "dead_end_rejections": sum(
                        1 for item in evaluations if item.score.dead_end
                    ),
                    "branch_candidates": [
                        item.score.diagnostic_payload()
                        for item in sorted(
                            evaluations,
                            key=lambda item: item.sort_key,
                            reverse=True,
                        )[: max(1, int(max_branch_candidates))]
                    ],
                },
            )
            return None

        path_keys = _bound_voxel_route_keys(
            _expand_true_3d_path(selected.path, graph.nodes),
            max_route_cells=max_route_cells,
        )
        graph_world_points = tuple(graph.nodes[key].center for key in path_keys)
        # The nearest graph node is a routing anchor, not necessarily the
        # camera's current position.  Replans can begin after the camera has
        # moved past one or more anchors; retaining a stale prefix would make
        # the new route point backward.  Keep the actual position as the
        # route origin and omit only the leading anchors that are either the
        # current graph sample or clearly behind the requested travel
        # direction.  Forward anchors remain in the route so their
        # collision-safe geometry is preserved.
        world_points_list = [position]
        graph_start_index = 0
        skipped_graph_anchor_count = 0
        while graph_start_index < len(graph_world_points):
            graph_point = graph_world_points[graph_start_index]
            graph_offset = tuple(
                float(graph_point[index]) - float(position[index])
                for index in range(3)
            )
            graph_point_is_current = (
                sum(value * value for value in graph_offset) <= 1e-12
            )
            graph_point_is_backward = bool(
                heading is not None
                and not graph_point_is_current
                and _graph_dot(graph_offset, heading) < -1e-6
            )
            if not graph_point_is_current and not graph_point_is_backward:
                break
            graph_start_index += 1
            skipped_graph_anchor_count += 1
        if graph_start_index < len(graph_world_points):
            world_points_list.append(graph_world_points[graph_start_index])
            world_points_list.extend(graph_world_points[graph_start_index + 1:])
        world_points = tuple(world_points_list)
        if len(world_points) < 2:
            return None
        route_cells = _project_3d_route_cells(path_keys, graph.nodes)
        if not route_cells:
            route_cells = (current_node.footprint_cell,)
        goal_key = selected.score.target_key or selected.path[-1]
        goal_node = graph.nodes[goal_key]
        route_volume = sum(
            float(graph.nodes[key].available_volume_m3)
            for key in path_keys
            if key in graph.nodes
        )
        ordered_scores = tuple(
            item.score
            for item in sorted(
                evaluations,
                key=lambda item: item.sort_key,
                reverse=True,
            )[: max(1, int(max_branch_candidates))]
        )
        _record_voxel_route_diagnostic(
            diagnostics,
            "voxel_prepared_3d_graph_selection",
            {
                "prepared_graph": graph.diagnostic_payload(),
                "selection_reason": selection_reason,
                "true_3d": True,
                "start_key": [int(value) for value in current_key],
                "goal_key": [int(value) for value in goal_key],
                "start_cell": [
                    int(current_node.footprint_cell[0]),
                    int(current_node.footprint_cell[1]),
                ],
                "goal_cell": [
                    int(goal_node.footprint_cell[0]),
                    int(goal_node.footprint_cell[1]),
                ],
                "heading": _graph_direction_payload(heading),
                "scoring_policy": scoring_policy.diagnostic_payload(),
                "entrance_progress_floor_m": float(entrance_progress_floor),
                "entrance_guard_tolerance_m": float(tolerance),
                "entrance_guard_source": entrance_guard_source,
                "progress_guard_mode": "entrance_band_only",
                "branch_count": len(branch_edges),
                **edge_filter,
                "branch_candidates": [
                    item.diagnostic_payload() for item in ordered_scores
                ],
                "dead_end_rejections": sum(
                    1 for item in evaluations if item.score.dead_end
                ),
                "skipped_leading_graph_anchor_count": int(
                    skipped_graph_anchor_count
                ),
            },
        )
        return NavigationVoxelRoutePlan(
            cells=route_cells,
            start_cell=current_node.footprint_cell,
            goal_cell=goal_node.footprint_cell,
            start_progress_m=start_progress,
            goal_progress_m=float(goal_node.progress_m),
            goal_volume_m3=float(goal_node.available_volume_m3),
            route_volume_m3=float(route_volume),
            goal_clearance_m=float(goal_node.mean_clearance_m),
            expanded_count=sum(
                int(item.score.expanded_count) for item in evaluations
            ),
            selection_reason=selection_reason,
            lookahead_distance_m=float(lookahead_distance),
            replan_at_lookahead=not terminal_reached,
            branch_score=selected.score,
            branch_candidates=ordered_scores,
            scoring_policy=scoring_policy.diagnostic_payload(),
            prepared_graph=True,
            heading_state_count=sum(
                int(item.score.heading_state_count) for item in evaluations
            ),
            connectivity_score=float(selected.score.connectivity_score),
            terminal_reached=terminal_reached,
            unknown_boundary_reached=bool(selected.score.unknown_boundary),
            dead_end_rejections=sum(
                1 for item in evaluations if item.score.dead_end
            ),
            world_points=world_points,
            three_d_graph=True,
            graph_keys=path_keys,
            entrance_progress_floor_m=float(entrance_progress_floor),
            entrance_guard_tolerance_m=float(tolerance),
            entrance_guard_source=entrance_guard_source,
        )

    def _plan_prepared_graph_route(
        self,
        *,
        component_cells: Sequence[FootprintCell]
        | set[FootprintCell]
        | frozenset[FootprintCell],
        position: tuple[float, float, float],
        footprint_cell_size: float,
        preferred_direction: Sequence[float] | None,
        max_expansions: int,
        max_route_cells: int,
        backtrack_tolerance_m: float | None,
        min_progress_gain_m: float | None,
        lookahead_distance_m: float | None,
        lookahead_cells: int,
        max_branch_candidates: int,
        scoring_policy: NavigationVoxelScoringPolicy,
        diagnostics: Callable[[str, Mapping[str, object]], None] | None,
        deadline_check: Callable[[], None] | None,
    ) -> NavigationVoxelRoutePlan | None:
        """Search the persisted graph with position-and-heading states."""
        graph = self.prepared_graph
        if graph is None:
            return None
        if deadline_check is not None:
            deadline_check()
        component = {
            (int(cell[0]), int(cell[1]))
            for cell in component_cells
            if len(cell) == 2
        }
        graph_cells = {
            cell
            for cell in graph.nodes
            if cell in self.cell_metrics
            and cell in component
            and int(self.cell_metrics[cell].free_cell_count) > 0
        }
        if len(graph_cells) < 2:
            return None
        current_cell = _nearest_prepared_graph_cell(
            graph_cells,
            graph.nodes,
            position=position,
            cell_size=footprint_cell_size,
        )
        if current_cell is None:
            return None
        current_metric = self.cell_metrics[current_cell]
        start_progress = float(current_metric.progress_m)
        tolerance = (
            max(float(footprint_cell_size), float(self.voxel_size_m))
            if backtrack_tolerance_m is None
            else max(0.0, float(backtrack_tolerance_m))
        )
        progress_gain = (
            max(float(footprint_cell_size) * 0.5, float(self.voxel_size_m))
            if min_progress_gain_m is None
            else max(0.0, float(min_progress_gain_m))
        )
        heading = _normalised_graph_direction(preferred_direction)
        requested_lookahead = (
            DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_DISTANCE_M
            if lookahead_distance_m is None
            else float(lookahead_distance_m)
        )
        if not math.isfinite(requested_lookahead) or requested_lookahead <= 0.0:
            requested_lookahead = DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_DISTANCE_M
        lookahead_distance = max(
            float(footprint_cell_size) * 4.0,
            requested_lookahead,
        )
        expansion_limit = max(1, int(max_expansions))
        branch_edges: list[tuple[NavigationVoxelGraphEdge, float]] = []
        rejected_backward = 0
        for edge in graph.outgoing(current_cell):
            if deadline_check is not None:
                deadline_check()
            if not edge.line_of_sight or edge.target not in graph_cells:
                continue
            target_progress = float(self.cell_metrics[edge.target].progress_m)
            if target_progress < start_progress - tolerance:
                continue
            alignment = (
                1.0
                if heading is None
                else _graph_dot(heading, edge.direction)
            )
            if heading is not None and alignment < 0.0:
                rejected_backward += 1
                continue
            if (
                edge.target != current_cell
                and target_progress < start_progress + progress_gain * 0.10
                and len(graph.outgoing(current_cell)) > 1
            ):
                continue
            branch_edges.append((edge, alignment))

        if not branch_edges:
            _record_voxel_route_diagnostic(
                diagnostics,
                "voxel_route_rejected",
                {
                    "reason": "no_forward_continuation",
                    "prepared_graph": True,
                    "start_cell": [int(current_cell[0]), int(current_cell[1])],
                    "start_progress_m": float(start_progress),
                    "heading": _graph_direction_payload(heading),
                    "rejected_backward_edges": int(rejected_backward),
                },
            )
            return None

        branch_budget = max(
            4,
            expansion_limit // max(1, len(branch_edges)),
        )
        evaluations: list[_NavigationVoxelBranchEvaluation] = []
        for edge, alignment in branch_edges:
            if deadline_check is not None:
                deadline_check()
            evaluation = _evaluate_prepared_graph_branch(
                graph=graph,
                metrics=self.cell_metrics,
                current_cell=current_cell,
                first_edge=edge,
                first_alignment=alignment,
                start_progress=start_progress,
                tolerance=tolerance,
                progress_gain=progress_gain,
                lookahead_distance_m=lookahead_distance,
                lookahead_cells=max(4, int(lookahead_cells)),
                expansion_budget=branch_budget,
                cell_size_m=footprint_cell_size,
                scoring_policy=scoring_policy,
                deadline_check=deadline_check,
            )
            if evaluation is not None:
                evaluations.append(evaluation)

        if not evaluations:
            _record_voxel_route_diagnostic(
                diagnostics,
                "voxel_route_rejected",
                {
                    "reason": "prepared_graph_search_empty",
                    "prepared_graph": True,
                    "start_cell": [int(current_cell[0]), int(current_cell[1])],
                    "heading": _graph_direction_payload(heading),
                    "branch_count": len(branch_edges),
                    "scoring_policy": scoring_policy.diagnostic_payload(),
                },
            )
            return None

        continuing = [
            item
            for item in evaluations
            if not item.score.dead_end
            and (
                item.score.continuation_distance_m > 0.0
                or item.score.unknown_boundary
            )
        ]
        terminal_candidates = [
            item
            for item in evaluations
            if item.score.target_is_terminal
            and not item.score.unknown_boundary
        ]
        if continuing:
            selected = max(continuing, key=lambda item: item.sort_key)
            terminal_reached = False
            selection_reason = "prepared_forward_graph"
        elif terminal_candidates:
            selected = max(
                terminal_candidates,
                key=lambda item: scoring_policy.branch_sort_key(item.score),
            )
            terminal_reached = True
            selection_reason = "cave_terminal"
        else:
            _record_voxel_route_diagnostic(
                diagnostics,
                "voxel_route_rejected",
                {
                    "reason": "no_non_dead_end_forward_branch",
                    "prepared_graph": True,
                    "start_cell": [int(current_cell[0]), int(current_cell[1])],
                    "heading": _graph_direction_payload(heading),
                    "scoring_policy": scoring_policy.diagnostic_payload(),
                    "branch_count": len(branch_edges),
                    "dead_end_rejections": sum(
                        1 for item in evaluations if item.score.dead_end
                    ),
                    "branch_candidates": [
                        item.score.diagnostic_payload()
                        for item in sorted(
                            evaluations,
                            key=lambda item: item.sort_key,
                            reverse=True,
                        )[: max(1, int(max_branch_candidates))]
                    ],
                },
            )
            return None

        path_tuple = _bound_voxel_route_cells(
            selected.path,
            max_route_cells=max_route_cells,
        )
        if len(path_tuple) < 2:
            return None
        goal_cell = selected.score.target_cell
        goal_metric = self.cell_metrics[goal_cell]
        route_volume = sum(
            float(self.cell_metrics[cell].available_volume_m3)
            for cell in path_tuple
            if cell in self.cell_metrics
        )
        ordered_scores = tuple(
            item.score
            for item in sorted(
                evaluations,
                key=lambda item: item.sort_key,
                reverse=True,
            )[: max(1, int(max_branch_candidates))]
        )
        _record_voxel_route_diagnostic(
            diagnostics,
            "voxel_prepared_graph_selection",
            {
                "prepared_graph": graph.diagnostic_payload(),
                "selection_reason": selection_reason,
                "start_cell": [int(current_cell[0]), int(current_cell[1])],
                "goal_cell": [int(goal_cell[0]), int(goal_cell[1])],
                "heading": _graph_direction_payload(heading),
                "scoring_policy": scoring_policy.diagnostic_payload(),
                "branch_count": len(branch_edges),
                "branch_candidates": [
                    item.diagnostic_payload()
                    for item in ordered_scores
                ],
                "dead_end_rejections": sum(
                    1 for item in evaluations if item.score.dead_end
                ),
            },
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
            selection_reason=selection_reason,
            lookahead_distance_m=float(lookahead_distance),
            replan_at_lookahead=not terminal_reached,
            branch_score=selected.score,
            branch_candidates=ordered_scores,
            scoring_policy=scoring_policy.diagnostic_payload(),
            prepared_graph=True,
            heading_state_count=sum(
                int(item.score.heading_state_count) for item in evaluations
            ),
            connectivity_score=float(selected.score.connectivity_score),
            terminal_reached=terminal_reached,
            unknown_boundary_reached=bool(selected.score.unknown_boundary),
            dead_end_rejections=sum(
                1 for item in evaluations if item.score.dead_end
            ),
        )

    def diagnostic_payload(self) -> dict[str, object]:
        """Return bounded atlas diagnostics for the Guided Dive blackbox."""
        chunk_store_stats = (
            None if self.chunk_store is None else self.chunk_store.stats()
        )
        tile_sizes = [float(tile.voxel_size_m) for tile in self.tiles]
        fine_tile_sizes = [float(tile.voxel_size_m) for tile in self.fine_tiles]
        if self.chunk_store is not None:
            if not tile_sizes:
                tile_sizes = [
                    float(descriptor.voxel_size_m)
                    for descriptor in self.chunk_store.descriptors(
                        fine_only=False,
                    )
                ]
            if not fine_tile_sizes:
                fine_tile_sizes = [
                    float(descriptor.voxel_size_m)
                    for descriptor in self.chunk_store.descriptors(
                        fine_only=True,
                    )
                ]
        surface_occupied_volume_m3 = sum(
            len(tile.surface_cells) * tile.voxel_size_m ** 3
            for tile in self.tiles
        )
        if self.chunk_store is not None and not self.tiles:
            surface_occupied_volume_m3 = sum(
                descriptor.surface_cell_count * descriptor.voxel_size_m ** 3
                for descriptor in self.chunk_store.descriptors(
                    fine_only=False,
                )
            )
        return {
            "model_kind": NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
            "cache_quality_profile": "mesh_roadmap_graph_native_v1",
            "coverage_scope": self.coverage_scope,
            "tile_count": int(self.tile_count),
            "fine_tile_count": int(self.fine_tile_count),
            "voxel_size_m": float(self.voxel_size_m),
            "voxel_size_max_m": max(tile_sizes, default=0.0),
            "fine_voxel_size_m": min(fine_tile_sizes, default=0.0),
            "fine_voxel_size_max_m": max(fine_tile_sizes, default=0.0),
            "bounds_min": [float(value) for value in self.bounds_min],
            "bounds_max": [float(value) for value in self.bounds_max],
            "voxel_count": int(self.voxel_count),
            "fine_voxel_count": int(self.fine_voxel_count),
            "surface_cells": len(self.surface_cells),
            "surface_occupied_volume_m3": float(surface_occupied_volume_m3),
            "triangle_count": int(sum(tile.triangle_count for tile in self.tiles)),
            "surface_sample_count": int(
                sum(tile.surface_sample_count for tile in self.tiles)
            ),
            "sampling_truncated": any(
                bool(tile.sampling_truncated) for tile in self.tiles
            ),
            "navigation_graph_method": (
                MESH_NAVIGATION_GRAPH_METHOD
                if self.has_prepared_mesh_graph
                else (
                    NAVIGATION_VOXEL_GRAPH_METHOD
                    if self.has_prepared_3d_graph
                    else (
                        self.prepared_graph.method
                        if self.prepared_graph is not None
                        else NAVIGATION_VOXEL_GRAPH_METHOD
                    )
                )
            ),
            "graph_routing_authority": (
                "prepared_mesh_free_space_graph"
                if self.has_prepared_mesh_graph
                else (
                    "prepared_true_3d_voxel_graph"
                    if self.has_prepared_3d_graph
                    else "prepared_footprint_graph"
                )
            ),
            "graph_resolution_m": (
                None
                if self.prepared_3d_graph is None
                else [
                    float(value)
                    for value in self.prepared_3d_graph.grid_size_m
                ]
            ),
            "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "branch_lookahead_default_distance_m": float(
                DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_DISTANCE_M
            ),
            "branch_lookahead_default_cells": int(
                DEFAULT_NAVIGATION_VOXEL_LOOKAHEAD_CELLS
            ),
            "navigation_cell_count": int(self.navigation_cell_count),
            "navigation_3d_cell_count": int(self.navigation_3d_cell_count),
            "mesh_navigation_cell_count": int(self.mesh_navigation_cell_count),
            "filled_free_cell_count": int(self.filled_free_cell_count),
            "max_progress_m": float(self.max_progress_m),
            "prepared_graph": (
                None
                if self.prepared_graph is None
                else self.prepared_graph.diagnostic_payload()
            ),
            "prepared_3d_graph": (
                None
                if self.prepared_3d_graph is None
                else self.prepared_3d_graph.diagnostic_payload()
            ),
            "prepared_mesh_graph": (
                None
                if self.prepared_mesh_graph is None
                else self.prepared_mesh_graph.diagnostic_payload()
            ),
            "prepared_3d_motion_geometry_safe": bool(
                self.prepared_3d_motion_geometry_safe
            ),
            "authoritative_motion_geometry_safe": bool(
                self.authoritative_motion_geometry_safe
            ),
            "chunk_store": (
                chunk_store_stats
            ),
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


def _graph_direction_payload(
    direction: tuple[float, float, float] | None,
) -> list[float] | None:
    if direction is None:
        return None
    return [float(value) for value in direction]


def _true_3d_entrance_guard_tolerance(
    *,
    footprint_cell_size: float,
    voxel_size_m: float,
    graph_grid_size_m: tuple[float, float, float],
    backtrack_tolerance_m: float | None,
) -> tuple[float, str]:
    """Return a no-return tolerance without coupling it to vertical coarsening.

    The prepared true-3D graph may coarsen Y much more aggressively than X/Z
    to stay within the consumer-hardware node budget. That vertical spacing is
    not a meaningful horizontal entrance boundary. The coarse centerline
    footprint is also not used: it can be ten metres wide while the graph
    itself remains at one-metre resolution. The default guard therefore uses
    only the raw voxel and horizontal graph scales. Callers may still provide
    an explicit tolerance for a map-specific entrance.
    """
    del footprint_cell_size
    if backtrack_tolerance_m is not None:
        return max(0.0, float(backtrack_tolerance_m)), (
            "explicit_backtrack_tolerance"
        )
    horizontal_graph_scale = max(
        0.0,
        float(graph_grid_size_m[0]),
        float(graph_grid_size_m[2]),
    )
    return max(
        1e-6,
        float(voxel_size_m),
        horizontal_graph_scale,
    ), "horizontal_voxel_spacing"


def _normalised_graph_direction(
    direction: Sequence[float] | None,
) -> tuple[float, float, float] | None:
    if direction is None:
        return None
    try:
        if len(direction) != 3:
            return None
        values = tuple(float(value) for value in direction)
    except (TypeError, ValueError):
        return None
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 1e-9:
        return None
    return tuple(value / norm for value in values)  # type: ignore[return-value]


def _graph_dot(
    first: Sequence[float],
    second: Sequence[float],
) -> float:
    return sum(float(first[index]) * float(second[index]) for index in range(3))


def _nearest_prepared_3d_graph_key(
    graph_keys: set[VoxelGraphKey],
    nodes: Mapping[VoxelGraphKey, object],
    *,
    position: tuple[float, float, float],
) -> VoxelGraphKey | None:
    """Find the closest prepared 3D node without collapsing its height."""
    if not graph_keys:
        return None
    return min(
        graph_keys,
        key=lambda key: (
            sum(
                (
                    float(nodes[key].center[axis]) - float(position[axis])
                )
                ** 2
                for axis in range(3)
            ),
            key,
        ),
    )


def _prepared_3d_graph_edge_cost(
    edge: NavigationVoxel3DEdge,
    *,
    target_node: object,
    alignment: float,
    graph_scale_m: float,
    scoring_policy: NavigationVoxelScoringPolicy,
) -> float:
    """Cost one prepared true-3D edge using the active request policy."""
    scale = max(1e-6, float(graph_scale_m))
    turn_penalty = (
        max(0.0, 1.0 - float(alignment))
        * scale
        * float(scoring_policy.turn_weight)
    )
    connectivity_penalty = (
        scale
        * float(scoring_policy.connectivity_weight)
        / (1.0 + max(0.0, float(target_node.connectivity_score)))
    )
    clearance_penalty = (
        scale
        * float(scoring_policy.clearance_weight)
        / (1.0 + max(0.0, float(edge.min_clearance_m)))
    )
    volume_penalty = (
        scale
        * float(scoring_policy.volume_weight)
        / (
            1.0
            + _navigation_volume_score(
                float(target_node.available_volume_m3)
            )
        )
    )
    return (
        float(edge.distance_m)
        + turn_penalty
        + connectivity_penalty
        + clearance_penalty
        + volume_penalty
    )


def _evaluate_prepared_3d_graph_branch(
    *,
    graph: NavigationVoxel3DGraph,
    graph_keys: set[VoxelGraphKey],
    graph_component_id: int,
    current_key: VoxelGraphKey,
    first_edge: NavigationVoxel3DEdge,
    first_alignment: float,
    edge_safety_check: Callable[
        [VoxelGraphKey, VoxelGraphKey], bool
    ] | None,
    entrance_progress_floor_m: float,
    lookahead_distance_m: float,
    lookahead_cells: int,
    expansion_budget: int,
    graph_scale_m: float,
    scoring_policy: NavigationVoxelScoringPolicy,
    deadline_check: Callable[[], None] | None = None,
) -> _NavigationVoxel3DBranchEvaluation | None:
    """Explore a bounded true-3D branch and retain its best partial prefix."""
    first_node = graph.nodes.get(first_edge.target)
    if first_node is None:
        return None
    initial_cost = _prepared_3d_graph_edge_cost(
        first_edge,
        target_node=first_node,
        alignment=first_alignment,
        graph_scale_m=graph_scale_m,
        scoring_policy=scoring_policy,
    )
    initial_path = (current_key, first_edge.target)
    queue: list[
        tuple[
            float,
            int,
            VoxelGraphKey,
            VoxelGraphKey,
            float,
            int,
            tuple[VoxelGraphKey, ...],
            float,
        ]
    ] = [
        (
            initial_cost,
            0,
            first_edge.target,
            current_key,
            float(first_edge.distance_m),
            1,
            initial_path,
            float(first_node.connectivity_score),
        )
    ]
    best_cost: dict[tuple[VoxelGraphKey, VoxelGraphKey], float] = {
        (first_edge.target, current_key): initial_cost
    }
    frontier: list[tuple[object, ...]] = []
    terminal: list[tuple[object, ...]] = []
    best_partial: tuple[object, ...] | None = None
    entrance_floor_rejections = 0
    expanded = 0
    state_counter = 1
    while queue and expanded < max(1, int(expansion_budget)):
        if deadline_check is not None:
            deadline_check()
        (
            cost,
            _queue_index,
            key,
            previous,
            distance,
            depth,
            path,
            connectivity_total,
        ) = heapq.heappop(queue)
        if cost > best_cost.get((key, previous), float("inf")) + 1e-9:
            continue
        expanded += 1
        node = graph.nodes.get(key)
        if node is None:
            continue
        state = (
            cost,
            depth,
            key,
            previous,
            distance,
            path,
            connectivity_total,
        )
        if best_partial is None or (
            _true_3d_partial_key(state, graph)
            > _true_3d_partial_key(best_partial, graph)
        ):
            best_partial = state

        outgoing: list[tuple[NavigationVoxel3DEdge, float]] = []
        for edge in graph.outgoing(key):
            if deadline_check is not None:
                deadline_check()
            if (
                not edge.line_of_sight
                or edge.target not in graph_keys
                or edge.target == previous
            ):
                continue
            if (
                edge.target in path
                and scoring_policy.loop_policy
                == NAVIGATION_VOXEL_LOOP_POLICY_AVOID
            ):
                continue
            if not _true_3d_edge_stays_in_component(
                graph,
                edge,
                component_id=graph_component_id,
            ):
                continue
            if (
                edge_safety_check is not None
                and not edge_safety_check(key, edge.target)
            ):
                continue
            target_node = graph.nodes.get(edge.target)
            if target_node is None:
                continue
            if target_node.progress_m < entrance_progress_floor_m - 1e-6:
                entrance_floor_rejections += 1
                continue
            incoming_direction = (
                first_edge.direction
                if depth == 1
                else _incoming_direction_3d(path, graph)
            )
            # A non-negative dot product keeps the route in the complete
            # forward hemisphere, including vertical and diagonal turns. It
            # deliberately does not compare centerline progress, so a valid
            # forward segment may enter a shallower region.
            alignment = _graph_dot(incoming_direction, edge.direction)
            if alignment < 0.0:
                continue
            outgoing.append((edge, alignment))

        has_forward_progress = _true_3d_has_progress_edge(
            graph,
            path,
            graph_keys=graph_keys,
            graph_component_id=graph_component_id,
            entrance_progress_floor_m=entrance_progress_floor_m,
            edge_safety_check=edge_safety_check,
            forward_only=True,
        )
        if (node.terminal or node.local_degree <= 1) and not has_forward_progress:
            # Same-depth samples can describe the cross-section of a terminal
            # room; they are not evidence of a route beyond that room. A
            # shallower/deeper edge, however, keeps the branch alive when it
            # is aligned with the current heading.
            terminal.append(state)
            continue

        if (
            distance >= float(lookahead_distance_m)
            or depth >= max(1, int(lookahead_cells))
        ):
            frontier.append(state)
            continue
        if not outgoing:
            if node.unknown_boundary:
                terminal.append(state)
            continue
        for edge, alignment in outgoing:
            if deadline_check is not None:
                deadline_check()
            target_node = graph.nodes.get(edge.target)
            if target_node is None:
                continue
            next_cost = cost + _prepared_3d_graph_edge_cost(
                edge,
                target_node=target_node,
                alignment=alignment,
                graph_scale_m=graph_scale_m,
                scoring_policy=scoring_policy,
            )
            state_key = (edge.target, key)
            if next_cost >= best_cost.get(state_key, float("inf")) - 1e-9:
                continue
            best_cost[state_key] = next_cost
            state_counter += 1
            heapq.heappush(
                queue,
                (
                    next_cost,
                    state_counter,
                    edge.target,
                    key,
                    distance + float(edge.distance_m),
                    depth + 1,
                    path + (edge.target,),
                    connectivity_total + float(target_node.connectivity_score),
                ),
            )

    candidates = [*frontier, *terminal]
    if not candidates and best_partial is not None:
        candidates.append(best_partial)
    if not candidates:
        return None
    selected = max(
        candidates,
        key=lambda item: (
            not _prepared_3d_path_is_dead_end(
                graph,
                item[5],
                graph_keys=graph_keys,
                graph_component_id=graph_component_id,
                entrance_progress_floor_m=entrance_progress_floor_m,
                edge_safety_check=edge_safety_check,
            ),
            bool(graph.nodes[item[5][-1]].unknown_boundary),
            float(scoring_policy.connectivity_weight)
            * float(item[6])
            / max(1, len(item[5]) - 1),
            float(scoring_policy.smooth_forward_weight)
            * _navigation_smooth_forward_score(
                first_step_alignment=first_alignment,
                continuation_distance_m=float(item[4]),
                lookahead_distance_m=lookahead_distance_m,
            ),
            -float(scoring_policy.backtrack_weight)
            * float(
                _true_3d_path_revisit_count(
                    _expand_true_3d_path(item[5], graph.nodes),
                    graph.nodes,
                )
            ),
            float(scoring_policy.volume_weight)
            * _navigation_volume_score(
                sum(
                    max(0.0, float(graph.nodes[key].available_volume_m3))
                    for key in item[5]
                    if key in graph.nodes
                )
            ),
            float(item[4]),
            float(item[1]),
            -float(item[0]),
        ),
    )
    cost, depth, target, _previous, distance, path, connectivity_total = selected
    target_node = graph.nodes[target]
    target_has_forward_progress = _true_3d_has_progress_edge(
        graph,
        path,
        graph_keys=graph_keys,
        graph_component_id=graph_component_id,
        entrance_progress_floor_m=entrance_progress_floor_m,
        edge_safety_check=edge_safety_check,
        forward_only=True,
    )
    target_is_terminal = bool(
        (target_node.terminal or target_node.local_degree <= 1)
        and not target_node.unknown_boundary
        and not target_has_forward_progress
    )
    expanded_path = _expand_true_3d_path(path, graph.nodes)
    revisited_footprint_count = _true_3d_path_revisit_count(
        expanded_path,
        graph.nodes,
    )
    unknown_boundary = any(
        graph.nodes[key].unknown_boundary
        for key in path
        if key in graph.nodes
    )
    dead_end = _prepared_3d_path_is_dead_end(
        graph,
        path,
        graph_keys=graph_keys,
        graph_component_id=graph_component_id,
        entrance_progress_floor_m=entrance_progress_floor_m,
        edge_safety_check=edge_safety_check,
    ) or (
        target_is_terminal and not unknown_boundary
    ) or (
        not target_node.terminal
        and _true_3d_has_progress_edge(
            graph,
            path,
            graph_keys=graph_keys,
            graph_component_id=graph_component_id,
            entrance_progress_floor_m=entrance_progress_floor_m,
            edge_safety_check=edge_safety_check,
            forward_only=False,
        )
        and not target_has_forward_progress
    )
    continuation_distance = 0.0 if target_is_terminal else float(distance)
    onward_exit_count = sum(
        1
        for edge in graph.outgoing(target)
        if edge.target not in path
        and edge.target in graph_keys
        and (
            edge_safety_check is None
            or edge_safety_check(target, edge.target)
        )
        and graph.nodes[edge.target].progress_m
        >= entrance_progress_floor_m - 1e-6
    )
    average_connectivity = connectivity_total / max(1, len(path) - 1)
    route_volume_m3 = sum(
        max(0.0, float(graph.nodes[key].available_volume_m3))
        for key in path
        if key in graph.nodes
    )
    smooth_forward_score = _navigation_smooth_forward_score(
        first_step_alignment=first_alignment,
        continuation_distance_m=continuation_distance,
        lookahead_distance_m=lookahead_distance_m,
    )
    volume_score = _navigation_volume_score(route_volume_m3)
    backtrack_penalty = float(revisited_footprint_count)
    weighted_score = _navigation_weighted_branch_score(
        policy=scoring_policy,
        connectivity_score=average_connectivity,
        smooth_forward_score=smooth_forward_score,
        volume_score=volume_score,
        backtrack_penalty=backtrack_penalty,
    )
    score = NavigationVoxelBranchScore(
        branch_start_cell=graph.nodes[first_edge.target].footprint_cell,
        target_cell=target_node.footprint_cell,
        reached_distance_m=float(distance),
        continuation_distance_m=float(continuation_distance),
        onward_exit_count=int(onward_exit_count),
        frontier_count=len(frontier),
        first_step_alignment=float(first_alignment),
        path_cost_m=float(cost),
        expanded_count=int(expanded),
        dead_end=bool(dead_end),
        target_is_terminal=target_is_terminal,
        connectivity_score=float(average_connectivity),
        heading_state_count=int(expanded),
        unknown_boundary=bool(unknown_boundary),
        branch_start_key=tuple(first_edge.target),
        target_key=tuple(target),
        graph_method=NAVIGATION_VOXEL_3D_GRAPH_METHOD,
        revisited_footprint_count=int(revisited_footprint_count),
        entrance_floor_rejections=int(entrance_floor_rejections),
        route_volume_m3=float(route_volume_m3),
        smooth_forward_score=float(smooth_forward_score),
        volume_score=float(volume_score),
        backtrack_penalty=float(backtrack_penalty),
        weighted_score=float(weighted_score),
    )
    sort_key = scoring_policy.branch_sort_key(score)
    return _NavigationVoxel3DBranchEvaluation(
        score=score,
        path=tuple(path),
        sort_key=sort_key,
    )


def _true_3d_partial_key(
    state: tuple[object, ...],
    graph: NavigationVoxel3DGraph,
) -> tuple[object, ...]:
    """Rank partial search states without using an unbounded global search."""
    path = state[5]
    target = path[-1]
    node = graph.nodes[target]
    return (
        not node.dead_end,
        bool(node.unknown_boundary),
        -int(
            _true_3d_path_revisit_count(
                _expand_true_3d_path(path, graph.nodes),
                graph.nodes,
            )
        ),
        float(state[4]),
        float(state[6]),
        float(state[1]),
        -float(state[0]),
    )


def _incoming_direction_3d(
    path: Sequence[VoxelGraphKey],
    graph: NavigationVoxel3DGraph,
) -> tuple[float, float, float]:
    if len(path) < 2:
        return (1.0, 0.0, 0.0)
    source, target = path[-2], path[-1]
    edge = next(
        (edge for edge in graph.outgoing(source) if edge.target == target),
        None,
    )
    return (1.0, 0.0, 0.0) if edge is None else edge.direction


def _true_3d_edge_stays_in_component(
    graph: NavigationVoxel3DGraph,
    edge: NavigationVoxel3DEdge,
    *,
    component_id: int | None = None,
    component_cells: set[FootprintCell] | None = None,
) -> bool:
    """Return whether an edge remains inside one prepared graph component.

    ``component_cells`` is retained only for readable legacy callers. Native
    true-3D routing uses graph component IDs; applying a coarse footprint
    corner test to fine graph nodes reintroduces the coordinate mismatch that
    prevented Guided Dive startup.
    """
    source = graph.nodes.get(edge.source)
    target = graph.nodes.get(edge.target)
    if source is None or target is None:
        return False
    if component_id is not None:
        return (
            int(source.component_id) == int(component_id)
            and int(target.component_id) == int(component_id)
        )
    if component_cells is None:
        return int(source.component_id) == int(target.component_id)
    first = source.footprint_cell
    last = target.footprint_cell
    if first not in component_cells or last not in component_cells:
        return False
    steps = max(1, abs(last[0] - first[0]), abs(last[1] - first[1]))
    previous = first
    for index in range(1, steps + 1):
        fraction = float(index) / float(steps)
        current = (
            int(math.floor(first[0] + (last[0] - first[0]) * fraction + 0.5)),
            int(math.floor(first[1] + (last[1] - first[1]) * fraction + 0.5)),
        )
        if not _true_3d_footprint_step_is_valid(
            previous,
            current,
            component_cells=component_cells,
        ):
            return False
        previous = current
    return True


def _true_3d_footprint_step_is_valid(
    first: FootprintCell,
    second: FootprintCell,
    *,
    component_cells: set[FootprintCell],
) -> bool:
    if first not in component_cells or second not in component_cells:
        return False
    delta_x = second[0] - first[0]
    delta_z = second[1] - first[1]
    if max(abs(delta_x), abs(delta_z)) > 1:
        return False
    if abs(delta_x) == 1 and abs(delta_z) == 1:
        return (
            (first[0] + delta_x, first[1]) in component_cells
            and (first[0], first[1] + delta_z) in component_cells
        )
    return True


def _true_3d_has_progress_edge(
    graph: NavigationVoxel3DGraph,
    path: Sequence[VoxelGraphKey],
    *,
    graph_keys: set[VoxelGraphKey],
    graph_component_id: int,
    entrance_progress_floor_m: float,
    edge_safety_check: Callable[
        [VoxelGraphKey, VoxelGraphKey], bool
    ] | None,
    forward_only: bool,
) -> bool:
    """Return whether an unvisited edge changes cached route progress."""
    if not path:
        return False
    current = path[-1]
    current_node = graph.nodes.get(current)
    if current_node is None:
        return False
    incoming_direction = _incoming_direction_3d(path, graph)
    for edge in graph.outgoing(current):
        if (
            not edge.line_of_sight
            or edge.target not in graph_keys
            or edge.target in path
        ):
            continue
        if not _true_3d_edge_stays_in_component(
            graph,
            edge,
            component_id=graph_component_id,
        ):
            continue
        if (
            edge_safety_check is not None
            and not edge_safety_check(current, edge.target)
        ):
            continue
        target = graph.nodes.get(edge.target)
        if target is None or target.progress_m < entrance_progress_floor_m - 1e-6:
            continue
        if abs(float(target.progress_m) - float(current_node.progress_m)) <= 1e-6:
            continue
        if forward_only and _graph_dot(incoming_direction, edge.direction) < 0.0:
            continue
        return True
    return False


def _true_3d_has_forward_continuation(
    graph: NavigationVoxel3DGraph,
    path: Sequence[VoxelGraphKey],
    *,
    graph_keys: set[VoxelGraphKey],
    graph_component_id: int | None,
    entrance_progress_floor_m: float,
    edge_safety_check: Callable[
        [VoxelGraphKey, VoxelGraphKey], bool
    ] | None,
) -> bool:
    """Return whether a cached edge continues in the current heading."""
    if len(path) < 2:
        return False
    incoming_direction = _incoming_direction_3d(path, graph)
    for edge in graph.outgoing(path[-1]):
        if (
            not edge.line_of_sight
            or edge.target not in graph_keys
            or edge.target in path
        ):
            continue
        if not _true_3d_edge_stays_in_component(
            graph,
            edge,
            component_id=graph_component_id,
        ):
            continue
        if (
            edge_safety_check is not None
            and not edge_safety_check(path[-1], edge.target)
        ):
            continue
        target = graph.nodes.get(edge.target)
        if target is None or target.progress_m < entrance_progress_floor_m - 1e-6:
            continue
        # Forward/reverse is a dot-product question. Keep the complete
        # hemisphere, including right-angle and vertical turns.
        if _graph_dot(incoming_direction, edge.direction) >= 0.0:
            return True
    return False


def _prepared_3d_path_is_dead_end(
    graph: NavigationVoxel3DGraph,
    path: Sequence[VoxelGraphKey],
    *,
    graph_keys: set[VoxelGraphKey] | None = None,
    graph_component_id: int | None = None,
    entrance_progress_floor_m: float = float("-inf"),
    edge_safety_check: Callable[
        [VoxelGraphKey, VoxelGraphKey], bool
    ] | None = None,
) -> bool:
    """Ignore the entrance-side part of a true-3D branch after a junction."""
    last_junction_index = 0
    for index, key in enumerate(path):
        node = graph.nodes.get(key)
        if node is None:
            continue
        if node.local_degree >= 3 or node.unknown_boundary:
            last_junction_index = index
    allowed_keys = graph_keys or set(graph.nodes)
    allowed_component_id = graph_component_id
    if allowed_component_id is None and allowed_keys:
        first_node = graph.nodes.get(next(iter(allowed_keys)))
        if first_node is not None:
            allowed_component_id = int(first_node.component_id)
    for index, key in enumerate(
        path[last_junction_index + 1 :],
        start=last_junction_index + 1,
    ):
        node = graph.nodes.get(key)
        if node is None or not node.dead_end:
            continue
        prefix = path[: index + 1]
        if _true_3d_has_forward_continuation(
            graph,
            prefix,
            graph_keys=allowed_keys,
            graph_component_id=allowed_component_id,
            entrance_progress_floor_m=entrance_progress_floor_m,
            edge_safety_check=edge_safety_check,
        ):
            continue
        return True
    return False


def _bound_voxel_route_keys(
    keys: Sequence[VoxelGraphKey],
    *,
    max_route_cells: int,
) -> tuple[VoxelGraphKey, ...]:
    limit = max(2, int(max_route_cells))
    if len(keys) <= limit:
        return tuple(keys)
    stride = max(1, math.ceil((len(keys) - 1) / max(1, limit - 1)))
    bounded = list(keys[::stride])
    if bounded[-1] != keys[-1]:
        bounded.append(keys[-1])
    return tuple(bounded[:limit])


def _project_3d_route_cells(
    keys: Sequence[VoxelGraphKey],
    nodes: Mapping[VoxelGraphKey, object],
) -> tuple[FootprintCell, ...]:
    cells: list[FootprintCell] = []
    for key in keys:
        cell = nodes[key].footprint_cell
        if not cells or cells[-1] != cell:
            cells.append(cell)
    return tuple(cells)


def _true_3d_path_revisit_count(
    keys: Sequence[VoxelGraphKey],
    nodes: Mapping[VoxelGraphKey, object],
) -> int:
    """Count non-consecutive returns to a footprint corridor cell.

    Consecutive samples may legitimately share a footprint while changing
    elevation in a stacked passage. A later return to that footprint is a
    stronger signal that a candidate is circling through a room or retracing
    a route, so it receives a selection penalty without forbidding valid
    stacked 3-D movement.
    """
    seen: set[FootprintCell] = set()
    previous: FootprintCell | None = None
    revisits = 0
    for key in keys:
        node = nodes.get(key)
        if node is None:
            continue
        cell = node.footprint_cell
        if cell in seen and cell != previous:
            revisits += 1
        seen.add(cell)
        previous = cell
    return revisits


def _expand_true_3d_path(
    keys: Sequence[VoxelGraphKey],
    nodes: Mapping[VoxelGraphKey, object],
) -> tuple[VoxelGraphKey, ...]:
    """Insert cached grid cells crossed by any-angle edges.

    The prepared graph may contain a line-of-sight shortcut, but the runtime
    collision guard validates footprint transitions one cell at a time. The
    intermediate nodes are already guaranteed free by cache-time line-of-
    sight construction, so exposing them as route samples preserves both
    representations without reverting to centerline geometry.
    """
    if len(keys) < 2:
        return tuple(keys)
    expanded: list[VoxelGraphKey] = [keys[0]]
    for source, target in zip(keys, keys[1:], strict=False):
        steps = max(abs(target[axis] - source[axis]) for axis in range(3))
        for step in range(1, max(1, steps) + 1):
            fraction = float(step) / float(max(1, steps))
            candidate = tuple(
                int(math.floor(
                    float(source[axis])
                    + (float(target[axis] - source[axis]) * fraction)
                    + 0.5
                ))
                for axis in range(3)
            )
            if candidate not in nodes:
                candidate = target
            if expanded[-1] != candidate:
                expanded.append(candidate)
    return tuple(expanded)


def _nearest_prepared_graph_cell(
    graph_cells: set[FootprintCell],
    nodes: Mapping[FootprintCell, NavigationVoxelGraphNode],
    *,
    position: tuple[float, float, float],
    cell_size: float,
) -> FootprintCell | None:
    if not graph_cells:
        return None
    return min(
        graph_cells,
        key=lambda cell: (
            (
                (float(cell[0]) + 0.5) * float(cell_size)
                - float(position[0])
            )
            ** 2
            + (
                float(nodes[cell].center_y_m) - float(position[1])
            )
            ** 2
            + (
                (float(cell[1]) + 0.5) * float(cell_size)
                - float(position[2])
            )
            ** 2,
            cell,
        ),
    )


def _prepared_graph_edge_cost(
    edge: NavigationVoxelGraphEdge,
    *,
    target_node: NavigationVoxelGraphNode,
    alignment: float,
    cell_size: float,
    scoring_policy: NavigationVoxelScoringPolicy,
) -> float:
    """Cost a compatibility-graph edge using the active request policy."""
    turn_penalty = (
        max(0.0, 1.0 - float(alignment))
        * float(cell_size)
        * float(scoring_policy.turn_weight)
    )
    connectivity_penalty = (
        float(cell_size)
        * float(scoring_policy.connectivity_weight)
        / (1.0 + max(0.0, float(target_node.connectivity_score)))
    )
    clearance_penalty = (
        float(cell_size)
        * float(scoring_policy.clearance_weight)
        / (1.0 + max(0.0, float(edge.min_clearance_m)))
    )
    volume_penalty = (
        float(cell_size)
        * float(scoring_policy.volume_weight)
        / (
            1.0
            + _navigation_volume_score(
                float(target_node.available_volume_m3)
            )
        )
    )
    return (
        float(edge.distance_m)
        + turn_penalty
        + connectivity_penalty
        + clearance_penalty
        + volume_penalty
    )


def _evaluate_prepared_graph_branch(
    *,
    graph: NavigationVoxelGraph,
    metrics: Mapping[FootprintCell, NavigationVoxelCellMetric],
    current_cell: FootprintCell,
    first_edge: NavigationVoxelGraphEdge,
    first_alignment: float,
    start_progress: float,
    tolerance: float,
    progress_gain: float,
    lookahead_distance_m: float,
    lookahead_cells: int,
    expansion_budget: int,
    cell_size_m: float,
    scoring_policy: NavigationVoxelScoringPolicy,
    deadline_check: Callable[[], None] | None = None,
) -> _NavigationVoxelBranchEvaluation | None:
    """Explore one first edge while retaining the incoming heading in state."""
    first_node = graph.nodes.get(first_edge.target)
    if first_node is None:
        return None
    cell_size = max(1e-6, float(cell_size_m))
    initial_cost = _prepared_graph_edge_cost(
        first_edge,
        target_node=first_node,
        alignment=first_alignment,
        cell_size=cell_size,
        scoring_policy=scoring_policy,
    )
    initial_path = (current_cell, first_edge.target)
    queue: list[
        tuple[
            float,
            int,
            FootprintCell,
            FootprintCell,
            float,
            int,
            tuple[FootprintCell, ...],
            float,
        ]
    ] = [
        (
            initial_cost,
            0,
            first_edge.target,
            current_cell,
            float(first_edge.distance_m),
            1,
            initial_path,
            float(first_node.connectivity_score),
        )
    ]
    best_cost: dict[tuple[FootprintCell, FootprintCell], float] = {
        (first_edge.target, current_cell): initial_cost
    }
    frontier: list[
        tuple[
            float,
            int,
            FootprintCell,
            FootprintCell,
            float,
            tuple[FootprintCell, ...],
            float,
        ]
    ] = []
    terminal: list[
        tuple[
            float,
            int,
            FootprintCell,
            FootprintCell,
            float,
            tuple[FootprintCell, ...],
            float,
        ]
    ] = []
    expanded = 0
    state_counter = 1
    best_partial: tuple[
        float,
        int,
        FootprintCell,
        FootprintCell,
        float,
        tuple[FootprintCell, ...],
        float,
    ] | None = None
    while queue and expanded < max(1, int(expansion_budget)):
        if deadline_check is not None:
            deadline_check()
        (
            cost,
            _queue_index,
            cell,
            previous,
            distance,
            depth,
            path,
            connectivity_total,
        ) = heapq.heappop(queue)
        if cost > best_cost.get((cell, previous), float("inf")) + 1e-9:
            continue
        expanded += 1
        node = graph.nodes.get(cell)
        if node is None:
            continue
        partial = (
            cost,
            depth,
            cell,
            previous,
            distance,
            path,
            connectivity_total,
        )
        if best_partial is None or (
            float(partial[4]),
            float(partial[6]),
            int(partial[1]),
            -float(partial[0]),
        ) > (
            float(best_partial[4]),
            float(best_partial[6]),
            int(best_partial[1]),
            -float(best_partial[0]),
        ):
            best_partial = partial
        outgoing: list[tuple[NavigationVoxelGraphEdge, float]] = []
        for edge in graph.outgoing(cell):
            if deadline_check is not None:
                deadline_check()
            if not edge.line_of_sight or edge.target == previous:
                continue
            if (
                edge.target in path
                and scoring_policy.loop_policy
                == NAVIGATION_VOXEL_LOOP_POLICY_AVOID
            ):
                continue
            target_metric = metrics.get(edge.target)
            if target_metric is None:
                continue
            target_progress = float(target_metric.progress_m)
            if target_progress < start_progress - tolerance:
                continue
            incoming_direction = (
                first_edge.direction
                if depth == 1
                else _incoming_direction(path, graph)
            )
            alignment = _graph_dot(incoming_direction, edge.direction)
            if alignment < 0.0:
                continue
            if (
                cell == first_edge.target
                and target_progress < start_progress + progress_gain * 0.10
                and len(graph.outgoing(current_cell)) > 1
            ):
                continue
            outgoing.append((edge, alignment))

        if (
            distance >= float(lookahead_distance_m)
            or depth >= max(1, int(lookahead_cells))
        ):
            frontier.append(
                (
                    cost,
                    depth,
                    cell,
                    previous,
                    distance,
                    path,
                    connectivity_total,
                )
            )
            continue
        if not outgoing:
            if node.terminal or node.unknown_boundary:
                terminal.append(
                    (
                        cost,
                        depth,
                        cell,
                        previous,
                        distance,
                        path,
                        connectivity_total,
                    )
                )
            continue
        for edge, alignment in outgoing:
            if deadline_check is not None:
                deadline_check()
            target_node = graph.nodes.get(edge.target)
            if target_node is None:
                continue
            next_cost = cost + _prepared_graph_edge_cost(
                edge,
                target_node=target_node,
                alignment=alignment,
                cell_size=cell_size,
                scoring_policy=scoring_policy,
            )
            state = (edge.target, cell)
            if next_cost >= best_cost.get(state, float("inf")) - 1e-9:
                continue
            best_cost[state] = next_cost
            state_counter += 1
            heapq.heappush(
                queue,
                (
                    next_cost,
                    state_counter,
                    edge.target,
                    cell,
                    distance + float(edge.distance_m),
                    depth + 1,
                    path + (edge.target,),
                    connectivity_total + float(target_node.connectivity_score),
                ),
            )

    candidates = [*frontier, *terminal]
    if not candidates and best_partial is not None:
        candidates.append(best_partial)
    if not candidates:
        return None
    selected = max(
        candidates,
        key=lambda item: (
            bool(
                item[5][-1] in graph.nodes
                and graph.nodes[item[5][-1]].unknown_boundary
            ),
            not _prepared_path_is_dead_end(graph, item[5]),
            float(scoring_policy.connectivity_weight)
            * float(item[6])
            / max(1, len(item[5]) - 1),
            float(scoring_policy.smooth_forward_weight)
            * _navigation_smooth_forward_score(
                first_step_alignment=first_alignment,
                continuation_distance_m=float(item[4]),
                lookahead_distance_m=lookahead_distance_m,
            ),
            -float(scoring_policy.backtrack_weight)
            * float(
                sum(
                    1
                    for index, cell in enumerate(item[5])
                    if cell in item[5][:index]
                )
            ),
            float(scoring_policy.volume_weight)
            * _navigation_volume_score(
                sum(
                    max(0.0, float(metrics[cell].available_volume_m3))
                    for cell in item[5]
                    if cell in metrics
                )
            ),
            float(item[4]),
            float(item[6]),
            float(item[2][0]),
            float(item[2][1]),
            -float(item[0]),
        ),
    )
    cost, depth, target, _previous, distance, path, connectivity_total = selected
    target_node = graph.nodes[target]
    target_is_terminal = bool(
        target_node.terminal and not target_node.unknown_boundary
    )
    unknown_boundary = any(
        graph.nodes[cell].unknown_boundary
        for cell in path
        if cell in graph.nodes
    )
    dead_end = _prepared_path_is_dead_end(graph, path) or (
        target_is_terminal and not unknown_boundary
    )
    continuation_distance = (
        0.0
        if target_is_terminal
        else float(distance)
    )
    onward_exit_count = sum(
        1
        for edge in graph.outgoing(target)
        if edge.target not in path
        and edge.target in metrics
        and float(metrics[edge.target].progress_m)
        >= start_progress - tolerance
    )
    average_connectivity = connectivity_total / max(1, len(path) - 1)
    route_volume_m3 = sum(
        max(0.0, float(metrics[cell].available_volume_m3))
        for cell in path
        if cell in metrics
    )
    smooth_forward_score = _navigation_smooth_forward_score(
        first_step_alignment=first_alignment,
        continuation_distance_m=continuation_distance,
        lookahead_distance_m=lookahead_distance_m,
    )
    volume_score = _navigation_volume_score(route_volume_m3)
    backtrack_penalty = float(
        sum(
            1
            for index, cell in enumerate(path)
            if cell in path[:index]
        )
    )
    weighted_score = _navigation_weighted_branch_score(
        policy=scoring_policy,
        connectivity_score=average_connectivity,
        smooth_forward_score=smooth_forward_score,
        volume_score=volume_score,
        backtrack_penalty=backtrack_penalty,
    )
    score = NavigationVoxelBranchScore(
        branch_start_cell=first_edge.target,
        target_cell=target,
        reached_distance_m=float(distance),
        continuation_distance_m=float(continuation_distance),
        onward_exit_count=int(onward_exit_count),
        frontier_count=len(frontier),
        first_step_alignment=float(first_alignment),
        path_cost_m=float(cost),
        expanded_count=int(expanded),
        dead_end=bool(dead_end),
        target_is_terminal=target_is_terminal,
        connectivity_score=float(average_connectivity),
        heading_state_count=int(expanded),
        unknown_boundary=bool(unknown_boundary),
        route_volume_m3=float(route_volume_m3),
        revisited_footprint_count=int(backtrack_penalty),
        smooth_forward_score=float(smooth_forward_score),
        volume_score=float(volume_score),
        backtrack_penalty=float(backtrack_penalty),
        weighted_score=float(weighted_score),
    )
    sort_key = scoring_policy.branch_sort_key(score)
    return _NavigationVoxelBranchEvaluation(
        score=score,
        path=tuple(path),
        sort_key=sort_key,
    )


def _path_distance(
    path: Sequence[FootprintCell],
    graph: NavigationVoxelGraph,
) -> float:
    distance = 0.0
    for source, target in zip(path, path[1:], strict=False):
        edge = next(
            (
                edge
                for edge in graph.outgoing(source)
                if edge.target == target
            ),
            None,
        )
        distance += float(edge.distance_m if edge is not None else 0.0)
    return distance


def _prepared_path_is_dead_end(
    graph: NavigationVoxelGraph,
    path: Sequence[FootprintCell],
) -> bool:
    """Ignore the entrance-side branch after a path reaches a junction."""
    last_junction_index = 0
    for index, cell in enumerate(path):
        node = graph.nodes.get(cell)
        if node is None:
            continue
        if node.local_degree >= 3 or node.unknown_boundary:
            last_junction_index = index
    return any(
        graph.nodes[cell].dead_end
        for cell in path[last_junction_index + 1 :]
        if cell in graph.nodes
    )


def _incoming_direction(
    path: Sequence[FootprintCell],
    graph: NavigationVoxelGraph,
) -> tuple[float, float, float]:
    if len(path) < 2:
        return (1.0, 0.0, 0.0)
    source, target = path[-2], path[-1]
    edge = next(
        (edge for edge in graph.outgoing(source) if edge.target == target),
        None,
    )
    return (1.0, 0.0, 0.0) if edge is None else edge.direction


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
    scoring_policy: NavigationVoxelScoringPolicy,
    deadline_check: Callable[[], None] | None = None,
) -> _NavigationVoxelBranchEvaluation | None:
    """Score one immediate branch without searching to a global endpoint."""
    initial_cost = _voxel_graph_edge_cost(
        current_cell,
        branch_start,
        cell_size=cell_size,
        voxel_size_m=1.0,
        metric=metrics[branch_start],
        preferred_direction=preferred_direction,
        scoring_policy=scoring_policy,
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
        if deadline_check is not None:
            deadline_check()
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
            if deadline_check is not None:
                deadline_check()
            edge_cost = _voxel_graph_edge_cost(
                cell,
                neighbor,
                cell_size=cell_size,
                voxel_size_m=1.0,
                metric=metrics[neighbor],
                preferred_direction=preferred_direction,
                scoring_policy=scoring_policy,
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
        if deadline_check is not None:
            deadline_check()
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
                deadline_check=deadline_check,
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

    route_volume_m3 = float(
        sum(
            max(0.0, float(metrics[cell].available_volume_m3))
            for cell in path
            if cell in metrics
        )
    )
    first_step_alignment = _cell_direction_alignment(
        current_cell,
        branch_start,
        preferred_direction,
    )
    smooth_forward_score = _navigation_smooth_forward_score(
        first_step_alignment=first_step_alignment,
        continuation_distance_m=float(continuation),
        lookahead_distance_m=lookahead_distance_m,
    )
    volume_score = _navigation_volume_score(route_volume_m3)
    backtrack_penalty = float(
        sum(
            1
            for index, cell in enumerate(path)
            if cell in path[:index]
        )
    )
    weighted_score = _navigation_weighted_branch_score(
        policy=scoring_policy,
        connectivity_score=0.0,
        smooth_forward_score=smooth_forward_score,
        volume_score=volume_score,
        backtrack_penalty=backtrack_penalty,
    )
    score = NavigationVoxelBranchScore(
        branch_start_cell=branch_start,
        target_cell=target,
        reached_distance_m=reached_distance,
        continuation_distance_m=float(continuation),
        onward_exit_count=int(exit_count),
        frontier_count=int(frontier_count),
        first_step_alignment=first_step_alignment,
        path_cost_m=reached_distance,
        expanded_count=int(expanded_count),
        dead_end=bool(dead_end),
        target_is_terminal=bool(target_is_terminal),
        route_volume_m3=route_volume_m3,
        revisited_footprint_count=int(backtrack_penalty),
        smooth_forward_score=float(smooth_forward_score),
        volume_score=float(volume_score),
        backtrack_penalty=float(backtrack_penalty),
        weighted_score=float(weighted_score),
    )
    sort_key = scoring_policy.branch_sort_key(score) + (
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
    deadline_check: Callable[[], None] | None = None,
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
        if deadline_check is not None:
            deadline_check()
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
            if deadline_check is not None:
                deadline_check()
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
    scoring_policy: NavigationVoxelScoringPolicy,
) -> float:
    """Return a topology/direction cost for bounded branch exploration.

    Volume is a small comfort term. Topology and forward continuation are
    still evaluated by the branch score before it can influence selection.
    """
    base_distance = max(1e-6, footprint_cell_distance(first, second) * cell_size)
    del voxel_size_m
    # Direction is a soft cost: a turn is valid even when the user's last
    # displacement points along the previous leg, but a backward first step
    # must not beat a forward continuation with comparable topology.
    alignment = _cell_direction_alignment(first, second, preferred_direction)
    turn_multiplier = 1.0
    if preferred_direction is not None and alignment < 0.25:
        # Preserve the compatibility planner's small soft turn cost. Its
        # footprint direction is not the true-3D incoming heading, so applying
        # the full 3-D turn weight here would distort its geometric horizon.
        turn_multiplier += (0.25 - alignment) * 0.15
    # The compatibility graph stores only 2-D footprint distances. Keep its
    # horizon geometric; volume and clearance are applied to the final branch
    # score, otherwise a nominal four-metre lookahead can stop one cell early.
    del metric, scoring_policy
    return base_distance * turn_multiplier


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
    mesh_edge_is_clear: MeshEdgeSafetyCheck | None = None,
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
            chunked_payload=None,
            chunk_payloads={},
        )

    model_routes: dict[str, object] = {}
    chunked_model_routes: dict[str, object] = {}
    chunk_payloads: dict[str, Mapping[str, object]] = {}
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
            route_id=route_id,
            triangle_provider=triangle_provider,
            mesh_edge_is_clear=mesh_edge_is_clear,
            config=resolved,
        )
        certified_component_cells = summary.pop(
            "_certified_component_cells",
            None,
        )
        if (
            isinstance(certified_component_cells, Sequence)
            and not isinstance(certified_component_cells, (str, bytes))
        ):
            normalized_cells = tuple(
                sorted(
                    {
                        (int(cell[0]), int(cell[1]))
                        for cell in certified_component_cells
                        if isinstance(cell, Sequence)
                        and not isinstance(cell, (str, bytes))
                        and len(cell) == 2
                    }
                )
            )
            if normalized_cells:
                original_cells = _flat_cells(
                    route_value.get("component_cells")
                )
                original_y_ranges = _route_y_ranges(
                    route_value.get("component_y_ranges"),
                    original_cells,
                )
                route_value["component_cells"] = [
                    int(value)
                    for cell in normalized_cells
                    for value in cell
                ]
                route_value["component_size"] = len(normalized_cells)
                route_value["footprint_cell_count"] = len(normalized_cells)
                if original_y_ranges:
                    y_range_by_cell = dict(
                        zip(
                            original_cells,
                            original_y_ranges,
                            strict=False,
                        )
                    )
                    normalized_y_ranges = tuple(
                        y_range_by_cell[cell]
                        for cell in normalized_cells
                        if cell in y_range_by_cell
                    )
                    if len(normalized_y_ranges) == len(normalized_cells):
                        route_value["component_y_ranges"] = [
                            float(value)
                            for y_range in normalized_y_ranges
                            for value in y_range
                        ]
                    else:
                        route_value.pop("component_y_ranges", None)
        route_value["voxel_corridor"] = summary
        route_summaries[route_id] = summary
        if not bool(summary.get("built")):
            continue
        model = summary.pop("_model", None)
        if not isinstance(model, Mapping):
            continue
        chunked_model = summary.pop("_chunked_model", None)
        route_chunk_payloads = summary.pop("_chunk_payloads", {})
        model_routes[route_id] = {
            "summary": dict(summary),
            "model": dict(model),
        }
        if isinstance(chunked_model, Mapping):
            chunked_model_routes[route_id] = {
                "summary": dict(summary),
                "model": dict(chunked_model),
            }
        if isinstance(route_chunk_payloads, Mapping):
            for relative_path, chunk_payload in route_chunk_payloads.items():
                if (
                    isinstance(relative_path, str)
                    and isinstance(chunk_payload, Mapping)
                ):
                    chunk_payloads[relative_path] = chunk_payload
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
        "coverage_repair_sample_budget": int(
            resolved.coverage_repair_sample_budget
        ),
        "fine_voxel_size_m": float(resolved.fine_voxel_size_m),
        "fine_tile_radius_m": float(resolved.fine_tile_radius_m),
        "max_fine_tiles": int(resolved.max_fine_tiles),
        "max_fine_tile_cells": int(resolved.max_fine_tile_cells),
        "fine_max_surface_samples": int(resolved.fine_max_surface_samples),
        "graph_max_nodes": int(resolved.graph_max_nodes),
        "graph_max_edges": int(resolved.graph_max_edges),
        "graph_max_edge_distance_cells": int(
            resolved.graph_max_edge_distance_cells
        ),
        "graph_max_edges_per_node": int(resolved.graph_max_edges_per_node),
        "mesh_graph_enabled": bool(resolved.mesh_graph_enabled),
        "mesh_graph_horizontal_sample_spacing_m": float(
            resolved.mesh_graph_horizontal_sample_spacing_m
        ),
        "mesh_graph_vertical_sample_spacing_m": float(
            resolved.mesh_graph_vertical_sample_spacing_m
        ),
        "mesh_graph_minimum_clearance_m": float(
            resolved.mesh_graph_minimum_clearance_m
        ),
        "mesh_graph_max_nodes": int(resolved.mesh_graph_max_nodes),
        "mesh_graph_max_edges_per_node": int(
            resolved.mesh_graph_max_edges_per_node
        ),
        "mesh_graph_max_edge_candidates_per_node": int(
            resolved.mesh_graph_max_edge_candidates_per_node
        ),
        "mesh_graph_max_edge_candidates_per_direction": int(
            resolved.mesh_graph_max_edge_candidates_per_direction
        ),
        "mesh_graph_max_edge_distance_m": float(
            resolved.mesh_graph_max_edge_distance_m
        ),
        "mesh_graph_max_vertical_edge_distance_m": float(
            resolved.mesh_graph_max_vertical_edge_distance_m
        ),
        "mesh_graph_entry_anchor_radius_m": float(
            resolved.mesh_graph_entry_anchor_radius_m
        ),
        "graph_routing_authority": "prepared_mesh_free_space_graph",
        "cache_quality_profile": "mesh_roadmap_graph_native_v1",
        "coverage_scope": "entire_cave_component",
        "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
        "mesh_navigation_graph_method": MESH_NAVIGATION_GRAPH_METHOD,
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
            "coverage_repair_sample_budget": int(
                resolved.coverage_repair_sample_budget
            ),
            "fine_voxel_size_m": float(resolved.fine_voxel_size_m),
            "fine_tile_radius_m": float(resolved.fine_tile_radius_m),
            "max_fine_tiles": int(resolved.max_fine_tiles),
            "max_fine_tile_cells": int(resolved.max_fine_tile_cells),
            "fine_max_surface_samples": int(resolved.fine_max_surface_samples),
            "graph_max_nodes": int(resolved.graph_max_nodes),
            "graph_max_edges": int(resolved.graph_max_edges),
            "graph_max_edge_distance_cells": int(
                resolved.graph_max_edge_distance_cells
            ),
            "graph_max_edges_per_node": int(resolved.graph_max_edges_per_node),
            "mesh_graph_enabled": bool(resolved.mesh_graph_enabled),
            "mesh_graph_horizontal_sample_spacing_m": float(
                resolved.mesh_graph_horizontal_sample_spacing_m
            ),
            "mesh_graph_vertical_sample_spacing_m": float(
                resolved.mesh_graph_vertical_sample_spacing_m
            ),
            "mesh_graph_minimum_clearance_m": float(
                resolved.mesh_graph_minimum_clearance_m
            ),
            "mesh_graph_max_nodes": int(resolved.mesh_graph_max_nodes),
            "mesh_graph_max_edges_per_node": int(
                resolved.mesh_graph_max_edges_per_node
            ),
            "mesh_graph_max_edge_candidates_per_node": int(
                resolved.mesh_graph_max_edge_candidates_per_node
            ),
            "mesh_graph_max_edge_candidates_per_direction": int(
                resolved.mesh_graph_max_edge_candidates_per_direction
            ),
            "mesh_graph_max_edge_distance_m": float(
                resolved.mesh_graph_max_edge_distance_m
            ),
            "mesh_graph_max_vertical_edge_distance_m": float(
                resolved.mesh_graph_max_vertical_edge_distance_m
            ),
            "mesh_graph_entry_anchor_radius_m": float(
                resolved.mesh_graph_entry_anchor_radius_m
            ),
            "graph_routing_authority": "prepared_mesh_free_space_graph",
            "mesh_navigation_graph_method": MESH_NAVIGATION_GRAPH_METHOD,
            "cache_quality_profile": "mesh_roadmap_graph_native_v1",
            "storage_method": NAVIGATION_VOXEL_CHUNK_STORAGE_METHOD,
            "chunk_directory": "navigation_voxel_chunks",
            "chunk_count": int(len(chunk_payloads)),
        }
    chunked_payload: dict[str, object] | None = None
    if chunked_model_routes:
        chunked_payload = dict(payload)
        chunked_payload.update(
            {
                "storage_method": NAVIGATION_VOXEL_CHUNK_STORAGE_METHOD,
                "chunk_directory": "navigation_voxel_chunks",
                "chunk_count": int(len(chunk_payloads)),
                "routes": chunked_model_routes,
            }
        )
    return NavigationVoxelCacheBuildResult(
        payload=payload,
        built_route_count=len(built_route_ids),
        recommended_route_id=recommended_route_id,
        chunked_payload=chunked_payload,
        chunk_payloads=chunk_payloads,
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
        signature_info = os.stat(path)
    except OSError:
        return None
    signature = (
        os.path.abspath(path),
        int(getattr(signature_info, "st_mtime_ns", 0)),
        int(signature_info.st_size),
    )
    model_key = (*signature, str(route_id))
    with _runtime_voxel_cache_lock:
        cached_model = _runtime_voxel_model_cache.get(model_key)
        if cached_model is not None:
            _runtime_voxel_model_cache.move_to_end(model_key)
            return cached_model

        payload = _runtime_voxel_payload_cache.get(signature)
        if payload is not None:
            _runtime_voxel_payload_cache.move_to_end(signature)
        else:
            payload = None
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
            _runtime_voxel_payload_cache[signature] = payload
            _runtime_voxel_payload_cache.move_to_end(signature)
            while len(_runtime_voxel_payload_cache) > _RUNTIME_VOXEL_PAYLOAD_CACHE_LIMIT:
                _runtime_voxel_payload_cache.popitem(last=False)

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
        chunk_store = _navigation_voxel_chunk_store_from_model(
            model,
            cache_dir=os.fspath(cache_dir),
        )
        restored = deserialize_navigation_voxel_volume(
            model,
            chunk_store=chunk_store,
        )
        # Build the immutable graph index while the cache model is first
        # materialized.  Subsequent preflight/replan requests reuse the same
        # indexed graph object through the process-local model cache instead
        # of paying the topology-index cost on the first route request.
        if isinstance(restored, NavigationVoxelAtlas):
            if restored.prepared_3d_graph is not None:
                _ = restored.prepared_3d_graph.runtime_index
            if restored.prepared_mesh_graph is not None:
                _ = restored.prepared_mesh_graph.runtime_index
    except (TypeError, ValueError, binascii.Error, zlib.error):
        return None
    with _runtime_voxel_cache_lock:
        _runtime_voxel_model_cache[model_key] = restored
        _runtime_voxel_model_cache.move_to_end(model_key)
        while len(_runtime_voxel_model_cache) > _RUNTIME_VOXEL_MODEL_CACHE_LIMIT:
            _evicted_key, evicted_model = _runtime_voxel_model_cache.popitem(
                last=False
            )
            del _evicted_key
            close_store = getattr(
                getattr(evicted_model, "chunk_store", None),
                "close",
                None,
            )
            if callable(close_store):
                close_store()
    return restored


def _navigation_voxel_chunk_store_from_model(
    model: Mapping[str, object],
    *,
    cache_dir: str,
) -> NavigationVoxelChunkStore | None:
    """Restore the bounded disk backend described by a graph-only model."""
    raw_store = model.get("chunk_store")
    if raw_store is None:
        return None
    if not isinstance(raw_store, Mapping):
        raise ValueError("cached navigation voxel chunk store is malformed")
    if raw_store.get("method") != NAVIGATION_VOXEL_CHUNK_STORAGE_METHOD:
        raise ValueError("unsupported navigation voxel chunk store method")
    if raw_store.get("root") != "navigation_voxel_chunks":
        raise ValueError("cached navigation voxel chunk root is invalid")
    raw_chunks = raw_store.get("chunks")
    if not isinstance(raw_chunks, Sequence) or isinstance(raw_chunks, (str, bytes)):
        raise ValueError("cached navigation voxel chunk descriptors are missing")
    if len(raw_chunks) > (
        DEFAULT_CACHE_VOXEL_MAX_TILES + DEFAULT_CACHE_FINE_MAX_TILES
    ):
        raise ValueError("cached navigation voxel chunk descriptor count is too large")
    descriptors = tuple(
        NavigationVoxelChunkDescriptor.from_payload(raw_chunk)
        for raw_chunk in raw_chunks
    )
    try:
        declared_chunk_count = int(
            raw_store.get("chunk_count", len(descriptors))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("cached navigation voxel chunk count is malformed") from exc
    for descriptor in descriptors:
        relative_path = descriptor.relative_path
        if (
            not relative_path
            or os.path.isabs(relative_path)
            or os.path.normpath(relative_path) != relative_path
            or not relative_path.startswith("navigation_voxel_chunks/")
        ):
            raise ValueError("cached navigation voxel chunk path is invalid")
    coarse_count = sum(descriptor.kind == "coarse" for descriptor in descriptors)
    fine_count = sum(descriptor.kind == "fine" for descriptor in descriptors)
    try:
        expected_coarse_count = int(model.get("tile_count", coarse_count))
        expected_fine_count = int(
            model.get("fine_tile_count", fine_count)
        )
        max_resident = int(
            raw_store.get(
                "max_resident_chunks",
                DEFAULT_NAVIGATION_VOXEL_CHUNK_MAX_RESIDENT,
            )
        )
        max_chunk_bytes = int(
            raw_store.get(
                "max_chunk_bytes",
                DEFAULT_NAVIGATION_VOXEL_CHUNK_MAX_BYTES,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("cached navigation voxel chunk limits are malformed") from exc
    if (
        declared_chunk_count != len(descriptors)
        or declared_chunk_count <= 0
        or expected_coarse_count != coarse_count
        or expected_fine_count != fine_count
        or expected_coarse_count <= 0
        or max_resident <= 0
        or max_chunk_bytes <= 0
    ):
        raise ValueError("cached navigation voxel chunk index is inconsistent")
    return DiskNavigationVoxelChunkStore(
        cache_dir,
        descriptors,
        decoder=deserialize_local_voxel_volume,
        max_resident_chunks=max_resident,
        max_chunk_bytes=max_chunk_bytes,
    )


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
            "cache_quality_profile": "mesh_roadmap_graph_native_v1",
            "coverage_scope": volume.coverage_scope,
            "tile_count": len(volume.tiles),
            "fine_tile_count": len(volume.fine_tiles),
            "tiles": [serialize_local_voxel_volume(tile) for tile in volume.tiles],
            "fine_tiles": [
                serialize_local_voxel_volume(tile)
                for tile in volume.fine_tiles
            ],
            "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "mesh_navigation_graph_method": MESH_NAVIGATION_GRAPH_METHOD,
            "graph_routing_authority": "prepared_mesh_free_space_graph",
            "footprint_graph_method": NAVIGATION_VOXEL_FOOTPRINT_GRAPH_METHOD,
            "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "cell_metrics": _serialize_cell_metrics(volume.cell_metrics),
            "prepared_graph": (
                None
                if volume.prepared_graph is None
                else serialize_navigation_voxel_graph(volume.prepared_graph)
            ),
            "prepared_3d_graph": (
                None
                if volume.prepared_3d_graph is None
                else serialize_navigation_voxel_3d_graph(volume.prepared_3d_graph)
            ),
            "prepared_mesh_graph": (
                None
                if volume.prepared_mesh_graph is None
                else serialize_navigation_voxel_3d_graph(volume.prepared_mesh_graph)
            ),
            "mesh_graph_entry_anchor_radius_m": float(
                volume.mesh_graph_entry_anchor_radius_m
            ),
        }
    return serialize_local_voxel_volume(volume)


def _serialize_navigation_voxel_chunked(
    volume: NavigationVoxelAtlas,
    *,
    route_id: str,
) -> tuple[dict[str, object], dict[str, Mapping[str, object]]]:
    """Split dense atlas tiles from the graph/index sidecar payload.

    The regular serializer remains embedded for compatibility with existing
    callers and older tests. Cache publication uses this second representation
    so only the graph, metrics, and compact chunk descriptors are loaded during
    route planning; individual dense voxel fields are decoded on demand.
    """
    model = serialize_navigation_voxel_volume(volume)
    route_digest = hashlib.sha256(str(route_id).encode("utf-8")).hexdigest()[:16]
    chunk_root = "navigation_voxel_chunks"
    route_root = f"{chunk_root}/route-{route_digest}"
    descriptors: list[dict[str, object]] = []
    chunk_payloads: dict[str, Mapping[str, object]] = {}

    for kind, tiles in (
        ("coarse", volume.tiles),
        ("fine", volume.fine_tiles),
    ):
        for index, tile in enumerate(tiles):
            chunk_id = f"{kind}-{index:06d}"
            relative_path = f"{route_root}/{chunk_id}.json"
            descriptor = NavigationVoxelChunkDescriptor.from_volume(
                chunk_id,
                kind,
                tile,
                relative_path=relative_path,
            )
            descriptors.append(descriptor.payload())
            chunk_payloads[relative_path] = serialize_local_voxel_volume(tile)

    chunked_model = dict(model)
    chunked_model.pop("tiles", None)
    chunked_model.pop("fine_tiles", None)
    chunked_model["chunk_store"] = {
        "method": NAVIGATION_VOXEL_CHUNK_STORAGE_METHOD,
        "root": chunk_root,
        "max_resident_chunks": int(DEFAULT_NAVIGATION_VOXEL_CHUNK_MAX_RESIDENT),
        "max_chunk_bytes": int(DEFAULT_NAVIGATION_VOXEL_CHUNK_MAX_BYTES),
        "chunk_count": int(len(descriptors)),
        "chunks": descriptors,
    }
    return chunked_model, chunk_payloads


def deserialize_navigation_voxel_volume(
    payload: Mapping[str, object],
    *,
    max_tiles: int = DEFAULT_CACHE_VOXEL_MAX_TILES,
    chunk_store: NavigationVoxelChunkStore | None = None,
) -> LocalVoxelVolume | NavigationVoxelAtlas:
    """Restore a legacy local field or a validated bounded voxel atlas."""
    atlas_method = payload.get("method")
    if atlas_method in {
        NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _PREVIOUS_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _OLDER_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _ANCIENT_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _HISTORIC_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _LEGACY_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
    }:
        expected_versions = {
            NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: NAVIGATION_VOXEL_CACHE_VERSION,
            _PREVIOUS_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: _PREVIOUS_NAVIGATION_VOXEL_CACHE_VERSION,
            _OLDER_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: _OLDER_NAVIGATION_VOXEL_CACHE_VERSION,
            _ANCIENT_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: _ANCIENT_NAVIGATION_VOXEL_CACHE_VERSION,
            _HISTORIC_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: _HISTORIC_NAVIGATION_VOXEL_CACHE_VERSION,
            _LEGACY_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: _LEGACY_PREPARED_NAVIGATION_VOXEL_CACHE_VERSION,
        }
        expected_version = expected_versions[atlas_method]
        if payload.get("version") != expected_version:
            raise ValueError("unsupported navigation voxel atlas version")
        raw_tiles = payload.get("tiles")
        if chunk_store is None and (
            not isinstance(raw_tiles, Sequence)
            or isinstance(raw_tiles, (str, bytes))
        ):
            raise ValueError("cached navigation voxel atlas tiles are missing")
        if chunk_store is not None and (
            raw_tiles is None
            or (
                isinstance(raw_tiles, Sequence)
                and not isinstance(raw_tiles, (str, bytes))
                and len(raw_tiles) == 0
            )
        ):
            raw_tiles = ()
        if not isinstance(raw_tiles, Sequence) or isinstance(raw_tiles, (str, bytes)):
            raise ValueError("cached navigation voxel atlas tiles are malformed")
        try:
            tile_count = int(payload.get("tile_count", len(raw_tiles)))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "cached navigation voxel atlas tile count is malformed"
            ) from exc
        if chunk_store is None and (tile_count != len(raw_tiles) or tile_count <= 0):
            raise ValueError("cached navigation voxel atlas tile count is inconsistent")
        if chunk_store is None and tile_count > max(1, int(max_tiles)):
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
        fine_tiles: list[LocalVoxelVolume] = []
        raw_fine_tiles = payload.get("fine_tiles", ())
        if chunk_store is not None and raw_fine_tiles is None:
            raw_fine_tiles = ()
        if not isinstance(raw_fine_tiles, Sequence) or isinstance(
            raw_fine_tiles,
            (str, bytes),
        ):
            raw_fine_tiles = ()
        if len(raw_fine_tiles) > DEFAULT_CACHE_FINE_MAX_TILES:
            raise ValueError("cached navigation voxel fine tile count is too large")
        for raw_tile in raw_fine_tiles:
            if not isinstance(raw_tile, Mapping):
                raise ValueError("cached navigation voxel fine tile is malformed")
            fine_tiles.append(
                deserialize_local_voxel_volume(
                    raw_tile,
                    max_voxels=DEFAULT_CACHE_FINE_MAX_TILE_CELLS,
                )
            )
        cell_metrics = _deserialize_cell_metrics(payload.get("cell_metrics"))
        prepared_graph = None
        prepared_3d_graph = None
        prepared_mesh_graph = None
        if atlas_method in {
            NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
            _PREVIOUS_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        }:
            graph_3d_payload = payload.get("prepared_3d_graph")
            if graph_3d_payload is not None:
                prepared_3d_graph = deserialize_navigation_voxel_3d_graph(
                    graph_3d_payload,
                    max_nodes=DEFAULT_3D_GRAPH_MAX_NODES,
                    max_edges=DEFAULT_3D_GRAPH_MAX_EDGES,
                )
        if atlas_method == NAVIGATION_VOXEL_ATLAS_MODEL_METHOD:
            mesh_graph_payload = payload.get("prepared_mesh_graph")
            if mesh_graph_payload is not None:
                prepared_mesh_graph = deserialize_navigation_voxel_3d_graph(
                    mesh_graph_payload,
                    max_nodes=DEFAULT_3D_GRAPH_MAX_NODES,
                    max_edges=DEFAULT_3D_GRAPH_MAX_EDGES,
                )
                if prepared_mesh_graph.method not in {
                    MESH_NAVIGATION_GRAPH_METHOD,
                    LEGACY_NAVIGATION_MESH_3D_GRAPH_METHOD,
                }:
                    raise ValueError("cached mesh navigation graph method is invalid")
        if atlas_method in {
            NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
            _PREVIOUS_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        }:
            graph_payload = payload.get("prepared_graph")
            if graph_payload is not None:
                prepared_graph = deserialize_navigation_voxel_graph(graph_payload)
        resolved_chunk_store = chunk_store
        if resolved_chunk_store is None:
            resolved_chunk_store = InMemoryNavigationVoxelChunkStore(
                tuple(tiles),
                tuple(fine_tiles),
            )
        try:
            mesh_entry_radius_m = float(
                payload.get("mesh_graph_entry_anchor_radius_m", 0.0)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "cached mesh graph entry anchor radius is malformed"
            ) from exc
        if not math.isfinite(mesh_entry_radius_m) or mesh_entry_radius_m < 0.0:
            raise ValueError(
                "cached mesh graph entry anchor radius is invalid"
            )
        return NavigationVoxelAtlas(
            tiles=tuple(tiles),
            coverage_scope=str(
                payload.get("coverage_scope", "entire_cave_component")
            ),
            cell_metrics=cell_metrics,
            prepared_graph=prepared_graph,
            prepared_3d_graph=prepared_3d_graph,
            prepared_mesh_graph=prepared_mesh_graph,
            mesh_graph_entry_anchor_radius_m=mesh_entry_radius_m,
            fine_tiles=tuple(fine_tiles),
            chunk_store=resolved_chunk_store,
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
                float(metric.center_y_m),
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
            or len(raw) not in {7, 8}
        ):
            raise ValueError("cached navigation voxel cell metric is malformed")
        try:
            cell = (int(raw[0]), int(raw[1]))
            progress = float(raw[2])
            volume = float(raw[3])
            free_count = int(raw[4])
            minimum_clearance = float(raw[5])
            mean_clearance = float(raw[6])
            center_y = 0.0 if len(raw) == 7 else float(raw[7])
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
                    center_y,
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
            center_y_m=center_y,
        )
    return metrics


def deserialize_local_voxel_volume(
    payload: Mapping[str, object],
    *,
    max_voxels: int = DEFAULT_VOXEL_MAX_CELLS * 4,
) -> LocalVoxelVolume | NavigationVoxelAtlas:
    """Validate and restore a bounded sparse surface voxel model."""
    if payload.get("method") in {
        NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _PREVIOUS_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _OLDER_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _ANCIENT_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _HISTORIC_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
    }:
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
    route_id: str,
    triangle_provider: TriangleProvider,
    mesh_edge_is_clear: MeshEdgeSafetyCheck | None,
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
        "coverage_repair_sample_budget": int(
            config.coverage_repair_sample_budget
        ),
        "fine_voxel_size_m": float(config.fine_voxel_size_m),
        "fine_tile_radius_m": float(config.fine_tile_radius_m),
        "max_fine_tiles": int(config.max_fine_tiles),
        "max_fine_tile_cells": int(config.max_fine_tile_cells),
        "fine_max_surface_samples": int(config.fine_max_surface_samples),
        "graph_max_nodes": int(config.graph_max_nodes),
        "graph_max_edges": int(config.graph_max_edges),
        "graph_max_edge_distance_cells": int(
            config.graph_max_edge_distance_cells
        ),
        "graph_max_edges_per_node": int(config.graph_max_edges_per_node),
        "graph_routing_authority": "prepared_mesh_free_space_graph",
        "cache_quality_profile": "mesh_roadmap_graph_native_v1",
        "coverage_scope": "entire_cave_component",
        "coverage_includes_preceding_curvature": True,
        "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
        "mesh_navigation_graph_method": MESH_NAVIGATION_GRAPH_METHOD,
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
            mesh_edge_is_clear=mesh_edge_is_clear,
            config=config,
            selected_regions=selected_regions,
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
    chunked_model, chunk_payloads = _serialize_navigation_voxel_chunked(
        atlas,
        route_id=route_id,
    )
    common["chunk_storage_method"] = NAVIGATION_VOXEL_CHUNK_STORAGE_METHOD
    common["chunk_count"] = int(len(chunk_payloads))
    common["_chunked_model"] = chunked_model
    common["_chunk_payloads"] = chunk_payloads
    # ``model`` is needed by the sidecar but is intentionally removed from the
    # small manifest summary by the caller.
    common["_model"] = common.pop("model")
    return common


def _cache_graph_base_grid_size(
    filled_cell_count: int,
    *,
    base_voxel_size: float,
    max_nodes: int,
) -> tuple[float, float, float]:
    """Choose bounded horizontal graph buckets before metric materialization.

    The filled-space flood fill can produce millions of 1 m samples on a
    large map. Keeping those samples as Python dictionary entries until the
    later graph coarsening pass creates a needless multi-gigabyte peak. A
    power-of-two horizontal bucket reduces that temporary cardinality to
    roughly one quarter of the final node budget while retaining the base
    vertical resolution for stacked passages.
    """
    base = max(1e-6, float(base_voxel_size))
    target_nodes = max(1_024, int(max_nodes) // 4)
    horizontal_factor = 1
    while (
        int(filled_cell_count) > target_nodes * horizontal_factor * horizontal_factor
        and horizontal_factor < 64
    ):
        horizontal_factor *= 2
    horizontal_size = base * horizontal_factor
    return horizontal_size, base, horizontal_size


def _build_route_voxel_atlas(
    manifest: Mapping[str, object],
    route: Mapping[str, object],
    points: tuple[Point, ...],
    *,
    triangle_provider: TriangleProvider,
    mesh_edge_is_clear: MeshEdgeSafetyCheck | None,
    config: NavigationVoxelCacheConfig,
    selected_regions: Sequence[object] = (),
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
    sampling_cells = _flat_cells(route.get("voxel_sampling_cells"))
    if not sampling_cells:
        sampling_cells = component_cells
    y_ranges = _route_y_ranges(route.get("component_y_ranges"), component_cells)
    fallback_y_range = _fallback_y_range(manifest, points)
    component_cell_set = set(component_cells)
    requested_component_cell_set = set(component_cell_set)
    tile_size = _tile_size_for_component(
        sampling_cells,
        cell_size=cell_size,
        requested_tile_size=config.tile_size_m,
        max_tiles=config.max_tiles,
    )
    groups = _component_tile_groups(
        sampling_cells,
        cell_size=cell_size,
        tile_size=tile_size,
    )
    padding = max(config.voxel_size_m * 2.0, cell_size * 0.25)
    progress_distances = _component_progress_distances(
        component_cell_set,
        route,
        cell_size=cell_size,
    )
    tiles: list[LocalVoxelVolume] = []
    tile_seed_points: list[tuple[Point, ...]] = []
    total_metrics: list[dict[str, float | int | bool]] = []
    cell_accumulators: dict[FootprintCell, list[float]] = {}
    true_3d_accumulator: dict[VoxelGraphKey, list[float]] = {}
    true_3d_base_voxel_size = max(
        1e-6,
        min(
            float(config.voxel_size_m),
            float(config.fine_voxel_size_m),
        ),
    )
    total_samples = 0
    total_triangles = 0
    sampling_truncated = False
    skipped_tiles = 0
    total_filled_cell_count = 0

    def retain_tile(
        tile: LocalVoxelVolume,
        tile_points: Sequence[Point],
    ) -> bool:
        """Retain one tile and merge its bounded footprint metrics."""
        nonlocal total_samples
        nonlocal total_triangles
        nonlocal sampling_truncated
        nonlocal skipped_tiles
        nonlocal total_filled_cell_count

        total_triangles += int(tile.triangle_count)
        total_samples += int(tile.surface_sample_count)
        sampling_truncated = sampling_truncated or bool(tile.sampling_truncated)
        if tile.triangle_count <= 0 or tile.surface_sample_count <= 0:
            skipped_tiles += 1
            return False
        tiles.append(tile)
        tile_seed_points.append(tuple(tile_points))
        filled_cells = tile.filled_free_cell_clearance_m(tile_points)
        total_filled_cell_count += len(filled_cells)
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
            low_y, high_y = _cell_y_range(
                cell,
                y_ranges,
                fallback_y_range,
            )
            if center[1] < low_y or center[1] > high_y:
                continue
            accumulator = cell_accumulators.setdefault(
                cell,
                [0.0, 0.0, float("inf"), 0.0, 0.0],
            )
            accumulator[0] += 1.0
            accumulator[1] += float(clearance_m)
            accumulator[2] = min(accumulator[2], float(clearance_m))
            accumulator[3] += float(tile.voxel_size_m ** 3)
            accumulator[4] += float(center[1])
        return True

    for group_index, cells in enumerate(groups):
        target_cells = tuple(
            cell for cell in cells if cell in component_cell_set
        )
        if not target_cells:
            continue
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
            target_cells,
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
        retain_tile(tile, tile_points)

    sampled_footprints = set(cell_accumulators)
    missing_cells_before_repair = sorted(
        component_cell_set - sampled_footprints
    )
    repair_tile_capacity = max(0, int(config.max_tiles) - len(tiles))
    repair_groups = _component_tile_groups(
        missing_cells_before_repair,
        cell_size=cell_size,
        tile_size=tile_size,
    )[:repair_tile_capacity]
    sampling_cells_by_tile: dict[tuple[int, int], list[FootprintCell]] = {}
    for cell in sampling_cells:
        x, z = footprint_world_center(cell, cell_size)
        key = (math.floor(x / tile_size), math.floor(z / tile_size))
        sampling_cells_by_tile.setdefault(key, []).append(cell)
    repair_sample_budget_remaining = max(
        0,
        int(config.coverage_repair_sample_budget),
    )
    repair_attempted = 0
    repair_built = 0
    for index, repair_cells in enumerate(repair_groups):
        if repair_sample_budget_remaining <= 0:
            break
        repair_attempted += len(repair_cells)
        remaining_groups = max(1, len(repair_groups) - index)
        repair_sample_budget = min(
            16_384,
            max(
                1_024,
                math.ceil(repair_sample_budget_remaining / remaining_groups),
            ),
        )
        first_x, first_z = footprint_world_center(
            repair_cells[0],
            cell_size,
        )
        repair_tile_key = (
            math.floor(first_x / tile_size),
            math.floor(first_z / tile_size),
        )
        bounds_cells = tuple(
            sampling_cells_by_tile.get(repair_tile_key, list(repair_cells))
        )
        bounds_min, bounds_max = _component_tile_bounds(
            bounds_cells,
            cell_size=cell_size,
            y_ranges=y_ranges,
            fallback_y_range=fallback_y_range,
            padding=padding,
        )
        tile_points = _tile_seed_points(
            repair_cells,
            cell_size=cell_size,
            y_ranges=y_ranges,
            fallback_y_range=fallback_y_range,
        )
        try:
            repair_tile = build_surface_voxel_volume(
                triangle_provider(bounds_min, bounds_max),
                bounds_min=bounds_min,
                bounds_max=bounds_max,
                config=VoxelVolumeConfig(
                    voxel_size_m=config.voxel_size_m,
                    max_voxels=min(
                        config.max_cells,
                        DEFAULT_CACHE_VOXEL_MAX_TILE_CELLS,
                    ),
                    max_surface_samples=repair_sample_budget,
                ),
            )
        except Exception:
            repair_sample_budget_remaining -= repair_sample_budget
            continue
        repair_sample_budget_remaining -= repair_sample_budget
        if retain_tile(repair_tile, tile_points):
            repair_built += 1

    missing_cells_after_repair = sorted(
        component_cell_set - set(cell_accumulators)
    )
    certified_component_cell_set = set(cell_accumulators)
    excluded_component_cell_set = (
        requested_component_cell_set - certified_component_cell_set
    )
    # The surface-derived component is a candidate scope.  A cell only
    # becomes part of the certified component after the bounded mesh-backed
    # voxel pass produces free-space evidence for it.  This prevents inferred
    # 2-D span bridges with no sampled geometry from becoming graph boundary
    # frontiers, while preserving the original candidate count in diagnostics.
    component_cell_set = certified_component_cell_set
    component_cells = tuple(sorted(component_cell_set))

    if not tiles:
        return None, {}, {
            "coverage_cell_count": len(component_cells),
            "sampling_support_cell_count": len(sampling_cells),
            "tile_count": 0,
            "coverage_scope": coverage_scope,
            "coverage_includes_preceding_curvature": coverage_scope
            == "entire_cave_component",
            "tile_size_m": float(tile_size),
            "tiles_skipped": int(skipped_tiles),
            "triangle_count": int(total_triangles),
            "surface_sample_count": int(total_samples),
            "sampling_truncated": bool(sampling_truncated),
            "coverage_repair_attempted_cell_count": int(repair_attempted),
            "coverage_repair_built_tile_count": int(repair_built),
            "coverage_repair_remaining_cell_count": len(
                missing_cells_after_repair
            ),
            "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "navigation_cell_count": 0,
        }

    true_3d_base_grid_size = _cache_graph_base_grid_size(
        total_filled_cell_count,
        base_voxel_size=true_3d_base_voxel_size,
        max_nodes=config.graph_max_nodes,
    )
    # The first pass above already produced the bounded per-footprint metrics.
    # Revisit each retained tile only for the graph samples, now that the
    # measured filled-cell count lets us choose a coarse horizontal bucket.
    # This avoids retaining millions of 1 m Python dictionary entries before
    # the graph coarsening pass while keeping the vertical bucket at 1 m when
    # the configured voxel size permits it.
    for tile, tile_points in zip(tiles, tile_seed_points):
        filled_cells = tile.filled_free_cell_clearance_m(tile_points)
        for voxel_index, clearance_m in filled_cells.items():
            center = tile.voxel_center(voxel_index)
            cell = (
                math.floor(center[0] / cell_size),
                math.floor(center[2] / cell_size),
            )
            if cell not in component_cell_set:
                continue
            low_y, high_y = _cell_y_range(
                cell,
                y_ranges,
                fallback_y_range,
            )
            if center[1] < low_y or center[1] > high_y:
                continue
            accumulate_navigation_voxel_3d_sample(
                true_3d_accumulator,
                center,
                grid_size_m=true_3d_base_grid_size,
                clearance_m=float(clearance_m),
                volume_m3=float(tile.voxel_size_m ** 3),
                progress_m=float(progress_distances.get(cell, 0.0)),
            )

    cell_metrics = {
        cell: NavigationVoxelCellMetric(
            available_volume_m3=float(accumulator[3]),
            free_cell_count=int(accumulator[0]),
            min_clearance_m=float(accumulator[2]),
            mean_clearance_m=float(accumulator[1] / max(1.0, accumulator[0])),
            progress_m=float(progress_distances[cell]),
            center_y_m=float(accumulator[4] / max(1.0, accumulator[0])),
        )
        for cell, accumulator in cell_accumulators.items()
        if cell in progress_distances and accumulator[0] > 0.0
    }
    true_3d_metrics, true_3d_grid_size = finalize_navigation_voxel_3d_metrics(
        true_3d_accumulator,
        grid_size_m=true_3d_base_grid_size,
        footprint_cell_size_m=cell_size,
        max_nodes=config.graph_max_nodes,
        max_vertical_factor=4,
    )
    true_3d_unknown_boundary = _true_3d_unknown_boundary_keys(
        true_3d_metrics,
        component_cell_set=component_cell_set,
        covered_footprint_cells=component_cell_set,
        sampling_truncated=sampling_truncated,
    )
    prepared_3d_graph = build_navigation_voxel_3d_graph(
        true_3d_metrics,
        grid_size_m=true_3d_grid_size,
        max_edge_distance_cells=config.graph_max_edge_distance_cells,
        max_edges_per_node=config.graph_max_edges_per_node,
        max_total_edges=config.graph_max_edges,
        unknown_boundary=true_3d_unknown_boundary,
    )
    if bool(config.mesh_graph_enabled) and mesh_edge_is_clear is not None:
        provisional_atlas = NavigationVoxelAtlas(
            tuple(tiles),
            coverage_scope=coverage_scope,
            cell_metrics=cell_metrics,
            prepared_3d_graph=prepared_3d_graph,
        )
        mesh_config = config.mesh_navigation_graph_config()
        mesh_build = _build_adaptive_seeded_mesh_navigation_path(
            points,
            footprint_cell_size_m=cell_size,
            component_cells=requested_component_cell_set,
            point_probe=lambda point: provisional_atlas.probe_point(
                point,
                include_clearance=True,
            ),
            edge_is_clear=mesh_edge_is_clear,
            coarse_config=mesh_config,
            fine_spacing_m=float(config.fine_voxel_size_m),
        )
        prepared_mesh_graph = mesh_build.graph
        mesh_graph_details = dict(mesh_build.details)
    elif not bool(config.mesh_graph_enabled):
        prepared_mesh_graph = None
        mesh_graph_details = {
            "method": MESH_NAVIGATION_GRAPH_METHOD,
            "reason": "mesh_graph_disabled",
        }
    else:
        prepared_mesh_graph = None
        mesh_graph_details = {
            "method": MESH_NAVIGATION_GRAPH_METHOD,
            "reason": "mesh_edge_guard_unavailable",
        }
    fine_seed_points, fine_seed_details = _fine_prepared_graph_seed_points(
        prepared_mesh_graph or prepared_3d_graph,
        route_points=points,
        selected_regions=selected_regions,
        max_tiles=config.max_fine_tiles,
        fine_tile_radius_m=config.fine_tile_radius_m,
    )
    fine_tiles = _build_fine_frontier_tiles(
        fine_seed_points,
        triangle_provider=triangle_provider,
        config=config,
    )
    fine_tile_coverage = _fine_seed_tile_coverage_details(
        fine_seed_points,
        fine_tiles,
    )
    if bool(fine_seed_details.get("fine_graph_spine_available", False)):
        built_coverage_complete = bool(
            fine_tile_coverage["fine_built_tile_seed_coverage_complete"]
        )
        fine_tile_coverage = {
            **fine_tile_coverage,
            "fine_graph_spine_built_tile_coverage_complete": (
                built_coverage_complete
            ),
            "fine_graph_spine_coverage_complete": bool(
                fine_seed_details.get("fine_graph_spine_coverage_complete", False)
                and built_coverage_complete
            ),
        }
    atlas = NavigationVoxelAtlas(
        tuple(tiles),
        coverage_scope=coverage_scope,
        cell_metrics=cell_metrics,
        prepared_3d_graph=prepared_3d_graph,
        prepared_mesh_graph=prepared_mesh_graph,
        mesh_graph_entry_anchor_radius_m=float(
            config.mesh_graph_entry_anchor_radius_m
        ),
        fine_tiles=tuple(fine_tiles),
    )
    metrics = _aggregate_tile_metrics(total_metrics, atlas)
    details = {
        "bounds_min": _point_payload(atlas.bounds_min),
        "bounds_max": _point_payload(atlas.bounds_max),
        "tile_size_m": float(tile_size),
        "tile_count": len(tiles),
        "fine_tile_count": len(fine_tiles),
        **fine_seed_details,
        **fine_tile_coverage,
        "fine_route_seed_count": len(fine_seed_points),
        "fine_route_seed_spacing_m": float(
            max(4.0, float(config.fine_tile_radius_m) * 0.75)
        ),
        "fine_voxel_size_m": float(atlas.fine_voxel_size_m),
        "fine_voxel_size_max_m": max(
            (float(tile.voxel_size_m) for tile in fine_tiles),
            default=0.0,
        ),
        "coverage_cell_count": len(component_cells),
        "coverage_requested_cell_count": len(requested_component_cell_set),
        "coverage_excluded_cell_count": len(excluded_component_cell_set),
        "sampling_support_cell_count": len(sampling_cells),
        "coverage_scope": coverage_scope,
        "coverage_includes_preceding_curvature": coverage_scope
        == "entire_cave_component",
        "tiles_skipped": int(skipped_tiles),
        "coverage_repair_attempted_cell_count": int(repair_attempted),
        "coverage_repair_built_tile_count": int(repair_built),
        "coverage_repair_remaining_cell_count": len(
            missing_cells_after_repair
        ),
        "model_kind": NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        "triangle_count": int(total_triangles),
        "surface_sample_count": int(total_samples),
        "sampling_truncated": bool(sampling_truncated),
        "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
        "mesh_navigation_graph_method": MESH_NAVIGATION_GRAPH_METHOD,
        "mesh_graph_routing_authority": "prepared_mesh_free_space_graph",
        "footprint_graph_method": NAVIGATION_VOXEL_FOOTPRINT_GRAPH_METHOD,
        "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
        "navigation_cell_count": int(atlas.navigation_cell_count),
        "filled_free_cell_count": int(atlas.filled_free_cell_count),
        "progress_max_m": float(atlas.max_progress_m),
        "prepared_graph": None,
        "prepared_3d_graph": prepared_3d_graph.diagnostic_payload(),
        "prepared_mesh_graph": mesh_graph_details,
        "navigation_3d_cell_count": int(len(prepared_3d_graph.nodes)),
        "mesh_navigation_cell_count": int(atlas.mesh_navigation_cell_count),
        "graph_grid_size_m": [
            float(value) for value in prepared_3d_graph.grid_size_m
        ],
        "graph_resolution_m": float(true_3d_base_grid_size[1]),
        "graph_base_grid_size_m": [
            float(value) for value in true_3d_base_grid_size
        ],
        "graph_native_routing": True,
        "fine_sampling_truncated": any(
            bool(tile.sampling_truncated) for tile in fine_tiles
        ),
        "_certified_component_cells": tuple(sorted(component_cell_set)),
    }
    return atlas, metrics, details


def _build_adaptive_seeded_mesh_navigation_path(
    route_points: Sequence[Point],
    *,
    footprint_cell_size_m: float,
    component_cells: set[FootprintCell],
    point_probe: Callable[[Point], tuple[bool, float] | None],
    edge_is_clear: MeshEdgeSafetyCheck,
    coarse_config: MeshNavigationGraphConfig,
    fine_spacing_m: float,
) -> MeshNavigationGraphBuildResult:
    """Build one fixed path, refining only when 2 m loses the terminal.

    Intermediate metadata points are breadcrumbs, not known cave terminals.
    A coarse goal-directed search is accepted only when it reaches the final
    route hint. Otherwise a fine search gets one bounded route-corridor retry.
    Failure publishes no mesh graph instead of mislabeling a short disconnected
    prefix as a successful terminal route.
    """
    points = tuple(route_points)
    if len(points) < 2:
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                "method": MESH_NAVIGATION_GRAPH_METHOD,
                "reason": "adaptive_mesh_route_points_missing",
            },
        )
    corridor_cells = _mesh_entry_route_sampling_cells(
        points,
        footprint_cell_size_m=footprint_cell_size_m,
        component_cells=component_cells,
    )
    coarse_goal_config = MeshNavigationGraphConfig(
        horizontal_sample_spacing_m=(
            coarse_config.horizontal_sample_spacing_m
        ),
        vertical_sample_spacing_m=coarse_config.vertical_sample_spacing_m,
        minimum_clearance_m=coarse_config.minimum_clearance_m,
        max_nodes=coarse_config.max_nodes,
        max_edges_per_node=coarse_config.max_edges_per_node,
        max_edge_candidates_per_node=min(
            12,
            coarse_config.max_edge_candidates_per_node,
        ),
        max_edge_candidates_per_direction=(
            coarse_config.max_edge_candidates_per_direction
        ),
        max_edge_distance_m=coarse_config.max_edge_distance_m,
        max_vertical_edge_distance_m=(
            coarse_config.max_vertical_edge_distance_m
        ),
        max_interval_points_per_column=(
            coarse_config.max_interval_points_per_column
        ),
        ray_merge_epsilon_m=coarse_config.ray_merge_epsilon_m,
    ).validated()
    coarse = build_goal_directed_seeded_mesh_navigation_path_graph(
        points[:DEFAULT_MESH_GRAPH_ENTRY_SEED_POINTS],
        footprint_cell_size_m=footprint_cell_size_m,
        component_cells=corridor_cells,
        point_probe=point_probe,
        edge_is_clear=edge_is_clear,
        terminal_point=points[-1],
        route_guide_points=points,
        config=coarse_goal_config,
    )
    coarse_details = dict(coarse.details)
    if coarse.graph is not None:
        return MeshNavigationGraphBuildResult(
            graph=coarse.graph,
            details={
                **coarse_details,
                "adaptive_retry_used": False,
                "known_terminal_reached": True,
                "adaptive_corridor_cell_count": len(corridor_cells),
                "adaptive_coarse_spacing_m": float(
                    coarse_goal_config.horizontal_sample_spacing_m
                ),
            },
        )

    fine_spacing = min(
        float(coarse_config.horizontal_sample_spacing_m),
        max(0.25, float(fine_spacing_m)),
    )
    fine_config = MeshNavigationGraphConfig(
        horizontal_sample_spacing_m=fine_spacing,
        vertical_sample_spacing_m=fine_spacing,
        minimum_clearance_m=coarse_config.minimum_clearance_m,
        max_nodes=coarse_config.max_nodes,
        max_edges_per_node=coarse_config.max_edges_per_node,
        max_edge_candidates_per_node=min(
            12,
            coarse_config.max_edge_candidates_per_node,
        ),
        max_edge_candidates_per_direction=(
            coarse_config.max_edge_candidates_per_direction
        ),
        max_edge_distance_m=coarse_config.max_edge_distance_m,
        max_vertical_edge_distance_m=(
            coarse_config.max_vertical_edge_distance_m
        ),
        max_interval_points_per_column=(
            coarse_config.max_interval_points_per_column
        ),
        ray_merge_epsilon_m=coarse_config.ray_merge_epsilon_m,
    ).validated()
    fine = build_goal_directed_seeded_mesh_navigation_path_graph(
        points[:DEFAULT_MESH_GRAPH_ENTRY_SEED_POINTS],
        footprint_cell_size_m=footprint_cell_size_m,
        component_cells=corridor_cells,
        point_probe=point_probe,
        edge_is_clear=edge_is_clear,
        terminal_point=points[-1],
        route_guide_points=points,
        config=fine_config,
    )
    fine_details = dict(fine.details)
    shared_details = {
        "adaptive_retry_used": True,
        "known_terminal_reached": fine.graph is not None,
        "coarse_reason": str(coarse_details.get("reason", "")),
        "coarse_maximum_route_guide_index_seen": int(
            coarse_details.get("maximum_route_guide_index_seen", -1)
        ),
        "coarse_maximum_route_guide_fraction_seen": float(
            coarse_details.get("maximum_route_guide_fraction_seen", 0.0)
        ),
        "coarse_persisted_path_node_count": int(
            coarse_details.get("persisted_path_node_count", 0)
        ),
        "adaptive_corridor_cell_count": len(corridor_cells),
        "adaptive_coarse_spacing_m": float(
            coarse_goal_config.horizontal_sample_spacing_m
        ),
        "adaptive_fine_spacing_m": float(fine_spacing),
    }
    if fine.graph is not None:
        return MeshNavigationGraphBuildResult(
            graph=fine.graph,
            details={**fine_details, **shared_details},
        )
    return MeshNavigationGraphBuildResult(
        graph=None,
        details={
            **fine_details,
            **shared_details,
            "reason": "adaptive_mesh_known_terminal_unreachable",
            "adaptive_reason": str(fine_details.get("reason", "")),
        },
    )


def _mesh_prepared_spine_sampling_envelope(
    graph: NavigationVoxel3DGraph,
    *,
    route_points: Sequence[Point],
    component_cells: set[FootprintCell],
    fallback_route_cells: Sequence[FootprintCell],
) -> tuple[
    tuple[FootprintCell, ...],
    tuple[Point, ...],
    tuple[VoxelGraphKey, ...],
    dict[str, object],
]:
    """Return a bounded direct-mesh sampling corridor and verified target hint.

    The existing voxel graph is used here only as an offline *sampling
    envelope*.  It never contributes nodes or edges to the mesh roadmap. The
    direct-mesh graph receives the selected spine plus the route metadata
    corridor, with rasterized cells between long prepared-graph edges. This
    remains a narrow one-path envelope rather than a whole-component halo.
    """
    component = set(component_cells)
    fallback = set(fallback_route_cells) & component
    if not graph.nodes or not route_points or not component:
        cells = tuple(sorted(fallback))
        return (
            cells,
            (),
            (),
            {
                "mesh_graph_sampling_scope": "metadata_route_corridor_fallback",
                "mesh_graph_envelope_cell_count": len(cells),
                "mesh_graph_spine_available": False,
                "mesh_graph_spine_reason": "prepared_graph_or_anchor_missing",
            },
        )
    anchor = route_points[0]
    start_key, _distance_squared = graph.runtime_index.nearest_key(
        anchor,
        routable_only=True,
    )
    if start_key is None or start_key not in graph.nodes:
        cells = tuple(sorted(fallback))
        return (
            cells,
            (),
            (),
            {
                "mesh_graph_sampling_scope": "metadata_route_corridor_fallback",
                "mesh_graph_envelope_cell_count": len(cells),
                "mesh_graph_spine_available": False,
                "mesh_graph_spine_reason": "prepared_graph_start_missing",
            },
        )
    component_id = int(graph.nodes[start_key].component_id)
    terminals = tuple(
        key
        for key in graph.runtime_index.terminal_keys_by_component.get(
            component_id,
            (),
        )
        if key != start_key
        and bool(graph.nodes[key].terminal)
        and not bool(graph.nodes[key].unknown_boundary)
    )
    if not terminals:
        cells = tuple(sorted(fallback))
        return (
            cells,
            (),
            (),
            {
                "mesh_graph_sampling_scope": "metadata_route_corridor_fallback",
                "mesh_graph_envelope_cell_count": len(cells),
                "mesh_graph_spine_available": False,
                "mesh_graph_spine_reason": "prepared_graph_terminal_missing",
                "mesh_graph_spine_start_key": [int(value) for value in start_key],
            },
        )
    distances, expanded_count, expansion_limited = (
        graph.runtime_index.shortest_distances_to_candidates(
            start_key,
            terminals,
            component_id=component_id,
            max_expansions=max(len(graph.nodes) * 4, int(graph.edge_count) + 1),
        )
    )
    reachable = tuple(key for key in terminals if key in distances)
    if expansion_limited or not reachable:
        cells = tuple(sorted(fallback))
        return (
            cells,
            (),
            (),
            {
                "mesh_graph_sampling_scope": "metadata_route_corridor_fallback",
                "mesh_graph_envelope_cell_count": len(cells),
                "mesh_graph_spine_available": False,
                "mesh_graph_spine_reason": (
                    "prepared_graph_terminal_search_limited"
                    if expansion_limited
                    else "prepared_graph_terminal_unreachable"
                ),
                "mesh_graph_spine_start_key": [int(value) for value in start_key],
                "mesh_graph_spine_terminal_search_expanded_count": int(
                    expanded_count
                ),
            },
        )
    terminal_key = min(
        reachable,
        key=lambda key: (
            float(distances[key]),
            -float(graph.nodes[key].min_clearance_m),
            -float(graph.nodes[key].available_volume_m3),
            key,
        ),
    )
    spine_keys, spine_details = shortest_navigation_voxel_3d_graph_path(
        graph,
        start_key=start_key,
        terminal_key=terminal_key,
    )
    if spine_keys is None:
        cells = tuple(sorted(fallback))
        return (
            cells,
            (),
            (),
            {
                "mesh_graph_sampling_scope": "metadata_route_corridor_fallback",
                "mesh_graph_envelope_cell_count": len(cells),
                "mesh_graph_spine_available": False,
                "mesh_graph_spine_reason": str(
                    spine_details.get("reason", "prepared_graph_spine_missing")
                ),
                "mesh_graph_spine_start_key": [int(value) for value in start_key],
                "mesh_graph_spine_terminal_key": [
                    int(value) for value in terminal_key
                ],
            },
        )
    spine_cells = {
        graph.nodes[key].footprint_cell
        for key in spine_keys
        if key in graph.nodes and graph.nodes[key].footprint_cell in component
    }
    corridor_cells = set(fallback)
    for first_key, second_key in zip(spine_keys, spine_keys[1:], strict=False):
        first_node = graph.nodes.get(first_key)
        second_node = graph.nodes.get(second_key)
        if first_node is None or second_node is None:
            continue
        corridor_cells.update(
            _rasterized_footprint_line_cells(
                first_node.footprint_cell,
                second_node.footprint_cell,
            )
        )
    cells = tuple(sorted((spine_cells | corridor_cells) & component))
    if not cells:
        cells = tuple(sorted(fallback))
    terminal_point = tuple(float(value) for value in graph.nodes[terminal_key].center)
    return (
        cells,
        (terminal_point,),
        tuple(spine_keys),
        {
            "mesh_graph_sampling_scope": (
                "prepared_easiest_terminal_spine_and_metadata_corridor_v1"
            ),
            "mesh_graph_envelope_cell_count": len(cells),
            "mesh_graph_metadata_corridor_cell_count": len(fallback),
            "mesh_graph_spine_available": True,
            "mesh_graph_spine_start_key": [int(value) for value in start_key],
            "mesh_graph_spine_terminal_key": [int(value) for value in terminal_key],
            "mesh_graph_spine_key_count": len(spine_keys),
            "mesh_graph_spine_route_length_m": float(
                spine_details.get("graph_route_cost", 0.0)
            ),
            "mesh_graph_spine_terminal_search_expanded_count": int(expanded_count),
        },
    )


def _rasterized_footprint_line_cells(
    first: FootprintCell,
    second: FootprintCell,
) -> tuple[FootprintCell, ...]:
    """Return the discrete footprint cells crossed by one graph edge."""
    delta_x = int(second[0]) - int(first[0])
    delta_z = int(second[1]) - int(first[1])
    count = max(abs(delta_x), abs(delta_z), 1)
    return tuple(
        (
            int(round(int(first[0]) + float(delta_x) * index / count)),
            int(round(int(first[1]) + float(delta_z) * index / count)),
        )
        for index in range(count + 1)
    )


def _mesh_entry_route_sampling_cells(
    route_points: Sequence[Point],
    *,
    footprint_cell_size_m: float,
    component_cells: set[FootprintCell],
) -> tuple[FootprintCell, ...]:
    """Return a bounded component corridor around entry route coordinates.

    Imported route metadata can associate a point on a negative-coordinate
    cell boundary with the adjacent positive-side cell. Mesh anchors use the
    actual world coordinate, so limiting them to only the stored route cell
    can discard an otherwise exact-safe entry path. Derive cells from the
    bounded entry points, rasterize between them, and retain one neighboring
    cell for the roadmap's small lateral anchor offsets.
    """
    cell_size = float(footprint_cell_size_m)
    if (
        not math.isfinite(cell_size)
        or cell_size <= 0.0
        or not component_cells
    ):
        return ()
    point_cells = tuple(
        (
            int(math.floor(float(point[0]) / cell_size)),
            int(math.floor(float(point[2]) / cell_size)),
        )
        for point in route_points
        if len(point) == 3
        and all(math.isfinite(float(value)) for value in point)
    )
    if not point_cells:
        return ()
    corridor = set(point_cells)
    for first, second in zip(point_cells, point_cells[1:], strict=False):
        corridor.update(_rasterized_footprint_line_cells(first, second))
    expanded = {
        (cell[0] + delta_x, cell[1] + delta_z)
        for cell in corridor
        for delta_x in (-1, 0, 1)
        for delta_z in (-1, 0, 1)
    }
    return tuple(sorted(expanded & set(component_cells)))


def _mesh_spine_roadmap_anchors(
    graph: NavigationVoxel3DGraph,
    spine_keys: Sequence[VoxelGraphKey],
    *,
    atlas: NavigationVoxelAtlas,
    allowed_cells: set[FootprintCell],
    route_seed_points: Sequence[Point],
    footprint_cell_size_m: float,
    horizontal_spacing_m: float,
    vertical_spacing_m: float,
    entry_anchor_radius_m: float = DEFAULT_MESH_GRAPH_ENTRY_ANCHOR_RADIUS_M,
) -> tuple[tuple[MeshNavigationGraphAnchor, ...], dict[str, object]]:
    """Create a bounded local lattice around one known voxel spine.

    The retained voxel samples establish that these candidates belong to the
    cached cave component.  They are not edges: the mesh roadmap subsequently
    discards any point too close to a triangle and decides every connection
    through the exact mesh collision guard.
    """
    horizontal = max(0.25, float(horizontal_spacing_m))
    vertical = max(0.25, float(vertical_spacing_m))
    cell_size = max(0.25, float(footprint_cell_size_m))
    valid_keys = tuple(key for key in spine_keys if key in graph.nodes)
    if len(valid_keys) < 2 or not allowed_cells:
        return (), {
            "mesh_graph_anchor_source": "voxel_spine_interpolated_lattice_v1",
            "mesh_graph_anchor_reason": "spine_or_sampling_scope_missing",
            "mesh_graph_anchor_count": 0,
        }

    spine_points = tuple(
        tuple(float(value) for value in graph.nodes[key].center)
        for key in valid_keys
    )
    entry_radius = max(horizontal, float(entry_anchor_radius_m))
    entry_radius_squared = entry_radius * entry_radius
    entry_seed_points = tuple(route_seed_points[:1])
    entry_component_id = int(graph.nodes[valid_keys[0]].component_id)
    entry_graph_points = tuple(
        tuple(float(value) for value in node.center)
        for key, node in sorted(graph.nodes.items())
        if int(node.component_id) == entry_component_id
        and node.footprint_cell in allowed_cells
        and any(
            sum(
                (float(node.center[axis]) - float(seed[axis])) ** 2
                for axis in range(3)
            )
            <= entry_radius_squared + 1e-9
            for seed in entry_seed_points
        )
    )
    # Entry-route samples take precedence when multiple sources quantize to
    # the same 2 m roadmap key. A coarse prepared-spine interpolation may cut
    # across a wall while the imported entry corridor bends safely around it.
    # The route remains only a candidate source: voxel probes and exact mesh
    # point/edge checks still decide whether anything is retained.
    base_points = list(
        _interpolated_mesh_anchor_points(route_seed_points, horizontal)
    )
    # Retain exact source graph centers in the entry neighborhood. Interpolated
    # route samples are useful corridor coverage, but can miss the one nearby
    # voxel point that has a clear camera connector through a noisy scan mesh.
    base_points.extend(entry_graph_points)
    base_points.extend(
        _interpolated_mesh_anchor_points(spine_points, horizontal)
    )

    lateral_offsets = (
        (0.0, 0.0),
        (-horizontal, 0.0),
        (horizontal, 0.0),
        (0.0, -horizontal),
        (0.0, horizontal),
    )
    vertical_offsets = (0.0, -vertical, vertical)
    candidates: dict[
        VoxelGraphKey,
        MeshNavigationGraphAnchor,
    ] = {}
    probe_count = 0
    outside_scope_count = 0
    unavailable_probe_count = 0
    blocked_probe_count = 0
    duplicate_count = 0
    for base in base_points:
        for offset_y in vertical_offsets:
            for offset_x, offset_z in lateral_offsets:
                point = (
                    float(base[0]) + offset_x,
                    float(base[1]) + offset_y,
                    float(base[2]) + offset_z,
                )
                cell = (
                    int(math.floor(point[0] / cell_size)),
                    int(math.floor(point[2] / cell_size)),
                )
                if cell not in allowed_cells:
                    outside_scope_count += 1
                    continue
                key = (
                    int(math.floor(point[0] / horizontal)),
                    int(math.floor(point[1] / vertical)),
                    int(math.floor(point[2] / horizontal)),
                )
                probe_count += 1
                probe = atlas.probe_point(point, include_clearance=True)
                if probe is None:
                    unavailable_probe_count += 1
                    continue
                if not bool(probe[0]):
                    blocked_probe_count += 1
                    continue
                candidate = MeshNavigationGraphAnchor(
                    point=point,
                    footprint_cell=cell,
                    clearance_m=max(0.0, float(probe[1])),
                )
                existing = candidates.get(key)
                if existing is not None:
                    duplicate_count += 1
                    candidate_rank = (
                        -float(candidate.clearance_m),
                        candidate.point,
                    )
                    existing_rank = (
                        -float(existing.clearance_m),
                        existing.point,
                    )
                    if candidate_rank >= existing_rank:
                        continue
                candidates[key] = candidate
    return tuple(candidates[key] for key in sorted(candidates)), {
        "mesh_graph_anchor_source": "voxel_spine_interpolated_lattice_v1",
        "mesh_graph_anchor_base_point_count": len(base_points),
        "mesh_graph_anchor_route_seed_count": len(route_seed_points),
        "mesh_graph_entry_anchor_radius_m": entry_radius,
        "mesh_graph_entry_graph_node_count": len(entry_graph_points),
        "mesh_graph_anchor_probe_count": probe_count,
        "mesh_graph_anchor_outside_scope_count": outside_scope_count,
        "mesh_graph_anchor_unavailable_probe_count": unavailable_probe_count,
        "mesh_graph_anchor_blocked_probe_count": blocked_probe_count,
        "mesh_graph_anchor_duplicate_count": duplicate_count,
        "mesh_graph_anchor_count": len(candidates),
        "mesh_graph_anchor_horizontal_spacing_m": horizontal,
        "mesh_graph_anchor_vertical_spacing_m": vertical,
    }


def _interpolated_mesh_anchor_points(
    points: Sequence[Point],
    spacing_m: float,
) -> tuple[Point, ...]:
    """Return deterministic spacing-limited points for a candidate corridor."""
    finite_points = tuple(
        tuple(float(value) for value in point)
        for point in points
        if len(point) == 3 and all(math.isfinite(float(value)) for value in point)
    )
    if not finite_points:
        return ()
    result: list[Point] = []
    for first, second in zip(finite_points, finite_points[1:], strict=False):
        distance_m = math.sqrt(
            sum((second[axis] - first[axis]) ** 2 for axis in range(3))
        )
        count = max(1, int(math.ceil(distance_m / max(0.25, spacing_m))))
        result.extend(
            tuple(
                float(first[axis])
                + (float(second[axis]) - float(first[axis]))
                * float(index)
                / float(count)
                for axis in range(3)
            )
            for index in range(count)
        )
    result.append(finite_points[-1])
    return tuple(result)


def _true_3d_unknown_boundary_keys(
    metrics: Mapping[VoxelGraphKey, NavigationVoxel3DMetric],
    *,
    component_cell_set: set[FootprintCell],
    covered_footprint_cells: set[FootprintCell] | None = None,
    sampling_truncated: bool,
) -> set[VoxelGraphKey]:
    """Mark nodes adjacent to an unrepresented navigable footprint cell.

    A surface-sampling budget can be exhausted after a valid portion of the
    cave has been sampled.  That quality flag does not prove that every
    sampled graph node is at an unknown topological boundary.  Only a
    navigable footprint neighbor absent from the prepared metrics is evidence
    of an unknown continuation; ``sampling_truncated`` remains diagnostic
    metadata for cache quality and does not broaden that boundary claim.
    """
    del sampling_truncated
    sampled_footprints = (
        set(covered_footprint_cells)
        if covered_footprint_cells is not None
        else {metric.footprint_cell for metric in metrics.values()}
    )
    unknown: set[VoxelGraphKey] = set()
    for key, metric in metrics.items():
        neighboring_component_cells = navigable_footprint_neighbors(
            metric.footprint_cell,
            component_cell_set,
        )
        if any(cell not in sampled_footprints for cell in neighboring_component_cells):
            unknown.add(key)
    return unknown


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


def _fine_prepared_graph_seed_points(
    graph: NavigationVoxel3DGraph,
    *,
    route_points: Sequence[Point],
    selected_regions: Sequence[object],
    max_tiles: int,
    fine_tile_radius_m: float,
) -> tuple[tuple[Point, ...], dict[str, object]]:
    """Seed refinement along the cache's easiest known-terminal graph spine.

    The imported centerline is useful cache-generation input, but it is not
    the runtime route authority.  A sparse leading-centerline corridor can
    therefore leave a valid prepared-graph route without fine evidence just
    where a coarse edge needs an exact local repair.  This selects the same
    nearest known terminal used by easiest-terminal preflight and covers its
    physical graph path instead.  If the graph cannot provide that bounded
    spine, retain the older metadata-polyline corridor as a compatibility
    fallback; runtime still fails closed when a required repair is absent.
    """
    fallback = _fine_frontier_seed_points(
        route_points,
        selected_regions=selected_regions,
        max_tiles=max_tiles,
        fine_tile_radius_m=fine_tile_radius_m,
    )
    spacing_m = max(4.0, float(fine_tile_radius_m) * 0.75)
    fallback_details: dict[str, object] = {
        "fine_route_seed_method": "metadata_polyline_fallback_v1",
        "fine_route_seed_spacing_m": float(spacing_m),
        "fine_graph_spine_available": False,
    }
    if not route_points or not graph.nodes:
        return fallback, {
            **fallback_details,
            "fine_graph_spine_reason": "prepared_graph_or_route_points_missing",
        }
    try:
        anchor = tuple(float(value) for value in route_points[0])
    except (TypeError, ValueError):
        return fallback, {
            **fallback_details,
            "fine_graph_spine_reason": "prepared_graph_anchor_invalid",
        }
    if len(anchor) != 3 or not all(math.isfinite(value) for value in anchor):
        return fallback, {
            **fallback_details,
            "fine_graph_spine_reason": "prepared_graph_anchor_invalid",
        }
    index = graph.runtime_index
    start_key, _distance_squared = index.nearest_key(
        anchor,
        routable_only=True,
    )
    if start_key is None or start_key not in graph.nodes:
        return fallback, {
            **fallback_details,
            "fine_graph_spine_reason": "prepared_graph_start_missing",
        }
    component_id = int(graph.nodes[start_key].component_id)
    candidates = tuple(
        key
        for key in index.terminal_keys_by_component.get(component_id, ())
        if key != start_key
        and bool(graph.nodes[key].terminal)
        and not bool(graph.nodes[key].unknown_boundary)
    )
    if not candidates:
        return fallback, {
            **fallback_details,
            "fine_graph_spine_reason": "prepared_graph_terminal_missing",
            "fine_graph_spine_start_key": [int(value) for value in start_key],
        }
    distances, expanded_count, expansion_limited = (
        index.shortest_distances_to_candidates(
            start_key,
            candidates,
            component_id=component_id,
            max_expansions=max(len(graph.nodes) * 4, int(graph.edge_count) + 1),
        )
    )
    reachable = tuple(key for key in candidates if key in distances)
    if expansion_limited or not reachable:
        return fallback, {
            **fallback_details,
            "fine_graph_spine_reason": (
                "prepared_graph_terminal_search_limited"
                if expansion_limited
                else "prepared_graph_terminal_unreachable"
            ),
            "fine_graph_spine_start_key": [int(value) for value in start_key],
            "fine_graph_spine_terminal_search_expanded_count": int(expanded_count),
        }
    terminal_key = min(
        reachable,
        key=lambda key: (
            float(distances[key]),
            -float(graph.nodes[key].min_clearance_m),
            -float(graph.nodes[key].available_volume_m3),
            key,
        ),
    )
    spine_keys, spine_details = shortest_navigation_voxel_3d_graph_path(
        graph,
        start_key=start_key,
        terminal_key=terminal_key,
    )
    if spine_keys is None:
        return fallback, {
            **fallback_details,
            "fine_graph_spine_reason": str(
                spine_details.get("reason", "prepared_graph_spine_missing")
            ),
            "fine_graph_spine_start_key": [int(value) for value in start_key],
            "fine_graph_spine_terminal_key": [
                int(value) for value in terminal_key
            ],
        }
    spine_points = tuple(
        tuple(float(value) for value in graph.nodes[key].center)
        for key in spine_keys
    )
    seeds = _fine_frontier_seed_points(
        spine_points,
        selected_regions=(),
        max_tiles=max_tiles,
        fine_tile_radius_m=fine_tile_radius_m,
    )
    terminal_point = spine_points[-1]
    radius = max(1e-6, float(fine_tile_radius_m))
    coverage_complete = bool(seeds) and all(
        abs(float(seeds[-1][axis]) - float(terminal_point[axis]))
        <= radius + 1e-9
        for axis in range(3)
    )
    return seeds, {
        "fine_route_seed_method": "prepared_easiest_terminal_graph_spine_v1",
        "fine_route_seed_spacing_m": float(spacing_m),
        "fine_graph_spine_available": True,
        "fine_graph_spine_start_key": [int(value) for value in start_key],
        "fine_graph_spine_terminal_key": [int(value) for value in terminal_key],
        "fine_graph_spine_key_count": len(spine_keys),
        "fine_graph_spine_route_length_m": float(
            spine_details.get("graph_route_cost", 0.0)
        ),
        "fine_graph_spine_coverage_complete": bool(coverage_complete),
        "fine_graph_spine_terminal_search_expanded_count": int(expanded_count),
}


def _fine_frontier_seed_points(
    points: Sequence[Point],
    *,
    selected_regions: Sequence[object],
    max_tiles: int,
    fine_tile_radius_m: float,
) -> tuple[Point, ...]:
    """Select a contiguous bounded fine-field corridor along one route.

    A fixed route needs usable 1 m evidence at every coarse-edge repair, not
    a sparse set of visually interesting bends.  The tile budget is therefore
    spent in route order at a spacing comfortably inside the tile radius.  A
    bounded budget still fails closed once the certified corridor ends.
    """
    del selected_regions
    limit = max(0, int(max_tiles))
    if limit <= 0 or not points:
        return ()
    radius = max(1e-6, float(fine_tile_radius_m))
    spacing_m = max(4.0, radius * 0.75)
    seeds: list[Point] = []

    def point(value: Sequence[float]) -> Point | None:
        if len(value) != 3:
            return None
        candidate = tuple(float(item) for item in value)
        if not all(math.isfinite(item) for item in candidate):
            return None
        return candidate  # type: ignore[return-value]

    def append(candidate: Point) -> bool:
        if len(seeds) >= limit:
            return False
        if seeds and sum(
            (seeds[-1][axis] - candidate[axis]) ** 2
            for axis in range(3)
        ) <= 1e-12:
            return True
        seeds.append(candidate)
        return True

    first = point(points[0])
    if first is None or not append(first):
        return tuple(seeds)
    previous = first
    distance_until_seed = spacing_m
    for raw_target in points[1:]:
        target = point(raw_target)
        if target is None:
            continue
        segment_start = previous
        delta = tuple(target[axis] - segment_start[axis] for axis in range(3))
        segment_length = math.sqrt(sum(value * value for value in delta))
        while segment_length + 1e-9 >= distance_until_seed:
            fraction = distance_until_seed / max(segment_length, 1e-12)
            candidate = tuple(
                segment_start[axis] + delta[axis] * fraction
                for axis in range(3)
            )
            if not append(candidate):
                return tuple(seeds)
            segment_start = candidate
            delta = tuple(target[axis] - segment_start[axis] for axis in range(3))
            segment_length = math.sqrt(sum(value * value for value in delta))
            distance_until_seed = spacing_m
        distance_until_seed -= segment_length
        previous = target
    if len(seeds) < limit:
        append(previous)
    return tuple(seeds)


def _build_fine_frontier_tiles(
    seed_points: Sequence[Point],
    *,
    triangle_provider: TriangleProvider,
    config: NavigationVoxelCacheConfig,
) -> tuple[LocalVoxelVolume, ...]:
    """Build sparse 1 m refinement tiles around bounded route frontiers."""
    if not seed_points or config.max_fine_tiles <= 0:
        return ()
    tile_limit = max(1, int(config.max_fine_tiles))
    # Fine tiles are fixed-route evidence, not a share of the coarse atlas's
    # global sampling quota.  A per-tile cap avoids the old failure mode where
    # increasing corridor coverage made every tile truncate earlier.
    sample_budget = max(4_096, int(config.fine_max_surface_samples))
    radius = max(
        float(config.fine_voxel_size_m) * 4.0,
        float(config.fine_tile_radius_m),
    )
    tiles: list[LocalVoxelVolume] = []
    for point in seed_points[:tile_limit]:
        bounds_min = tuple(float(point[axis] - radius) for axis in range(3))
        bounds_max = tuple(float(point[axis] + radius) for axis in range(3))
        try:
            tile = build_surface_voxel_volume(
                triangle_provider(bounds_min, bounds_max),
                bounds_min=bounds_min,
                bounds_max=bounds_max,
                config=VoxelVolumeConfig(
                    voxel_size_m=float(config.fine_voxel_size_m),
                    surface_inflation_cells=(
                        DEFAULT_CACHE_FINE_SURFACE_INFLATION_CELLS
                    ),
                    max_voxels=int(config.max_fine_tile_cells),
                    max_surface_samples=sample_budget,
                    max_clearance_search_cells=16,
                ),
            )
        except Exception:
            continue
        if tile.triangle_count <= 0 or tile.surface_sample_count <= 0:
            continue
        tiles.append(tile)
    return tuple(tiles)


def _fine_seed_tile_coverage_details(
    seed_points: Sequence[Point],
    fine_tiles: Sequence[LocalVoxelVolume],
) -> dict[str, object]:
    """Report whether cache generation kept a tile around every route seed."""
    uncovered_indices = tuple(
        index
        for index, point in enumerate(seed_points)
        if not any(tile.contains_point(point) for tile in fine_tiles)
    )
    return {
        "fine_built_tile_seed_coverage_complete": not uncovered_indices,
        "fine_built_tile_uncovered_seed_count": len(uncovered_indices),
        "fine_built_tile_uncovered_seed_examples": [
            _point_payload(seed_points[index])
            for index in uncovered_indices[:8]
        ],
    }


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
            / max(1, sum(tile.voxel_count for tile in atlas.tiles))
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
    """Accept current prepared graphs and readable older sidecars."""
    return (version, method) in {
        (NAVIGATION_VOXEL_CACHE_VERSION, NAVIGATION_VOXEL_CACHE_METHOD),
        (
            _PREVIOUS_NAVIGATION_VOXEL_CACHE_VERSION,
            _PREVIOUS_NAVIGATION_VOXEL_CACHE_METHOD,
        ),
        (
            _OLDER_NAVIGATION_VOXEL_CACHE_VERSION,
            _OLDER_NAVIGATION_VOXEL_CACHE_METHOD,
        ),
        (
            _ANCIENT_NAVIGATION_VOXEL_CACHE_VERSION,
            _ANCIENT_NAVIGATION_VOXEL_CACHE_METHOD,
        ),
        (
            _HISTORIC_NAVIGATION_VOXEL_CACHE_VERSION,
            _HISTORIC_NAVIGATION_VOXEL_CACHE_METHOD,
        ),
        (
            _LEGACY_PREPARED_NAVIGATION_VOXEL_CACHE_VERSION,
            _LEGACY_PREPARED_NAVIGATION_VOXEL_CACHE_METHOD,
        ),
        (
            _LEGACY_NAVIGATION_VOXEL_CACHE_VERSION,
            _LEGACY_NAVIGATION_VOXEL_CACHE_METHOD,
        ),
    }


def supported_navigation_voxel_cache_identity(
    version: object,
    method: object,
) -> bool:
    """Return whether a navigation sidecar can be read by this build."""
    return _supported_cache_identity(version, method)


def _empty_payload(config: NavigationVoxelCacheConfig) -> dict[str, object]:
    return {
        "version": NAVIGATION_VOXEL_CACHE_VERSION,
        "method": NAVIGATION_VOXEL_CACHE_METHOD,
        "voxel_size_m": float(config.voxel_size_m),
        "curvature_method": CURVATURE_PROFILE_METHOD,
        "tile_size_m": float(config.tile_size_m),
        "max_tiles": int(config.max_tiles),
        "fine_voxel_size_m": float(config.fine_voxel_size_m),
        "fine_tile_radius_m": float(config.fine_tile_radius_m),
        "max_fine_tiles": int(config.max_fine_tiles),
        "max_fine_tile_cells": int(config.max_fine_tile_cells),
        "fine_max_surface_samples": int(config.fine_max_surface_samples),
        "graph_max_nodes": int(config.graph_max_nodes),
        "graph_max_edges": int(config.graph_max_edges),
        "graph_max_edge_distance_cells": int(
            config.graph_max_edge_distance_cells
        ),
        "graph_max_edges_per_node": int(config.graph_max_edges_per_node),
        "mesh_graph_enabled": bool(config.mesh_graph_enabled),
        "mesh_graph_horizontal_sample_spacing_m": float(
            config.mesh_graph_horizontal_sample_spacing_m
        ),
        "mesh_graph_vertical_sample_spacing_m": float(
            config.mesh_graph_vertical_sample_spacing_m
        ),
        "mesh_graph_minimum_clearance_m": float(
            config.mesh_graph_minimum_clearance_m
        ),
        "mesh_graph_max_nodes": int(config.mesh_graph_max_nodes),
        "mesh_graph_max_edge_candidates_per_node": int(
            config.mesh_graph_max_edge_candidates_per_node
        ),
        "mesh_graph_max_edge_candidates_per_direction": int(
            config.mesh_graph_max_edge_candidates_per_direction
        ),
        "mesh_graph_entry_anchor_radius_m": float(
            config.mesh_graph_entry_anchor_radius_m
        ),
        "mesh_navigation_graph_method": MESH_NAVIGATION_GRAPH_METHOD,
        "graph_routing_authority": "prepared_mesh_free_space_graph",
        "cache_quality_profile": "mesh_roadmap_graph_native_v1",
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
