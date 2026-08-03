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
from caveviewer.core.navigation.cubic_graph import (
    CUBIC_VOXEL_GRAPH_METHOD,
    CubicVoxelGraphBuildResult,
    CubicVoxelKey,
    CubicVoxelLimitExceededError,
    SparseCubicVoxelGraph,
    build_cubic_graph_from_local_volumes,
)
from caveviewer.core.navigation.fixed_voxels import (
    FIXED_ORTHOGONAL_VOXEL_METHOD,
    FixedVoxelRegion,
    build_fixed_orthogonal_voxel_tiles,
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
    build_exact_cubic_spine_navigation_path_graph,
    build_goal_directed_seeded_mesh_navigation_path_graph,
    build_validated_mesh_path_graph,
)
from caveviewer.core.navigation.voxel_store import (
    DEFAULT_NAVIGATION_VOXEL_CHUNK_MAX_BYTES,
    DEFAULT_NAVIGATION_VOXEL_CHUNK_MAX_RESIDENT,
    DiskNavigationVoxelChunkStore,
    InMemoryNavigationVoxelChunkStore,
    NavigationVoxelChunkDescriptor,
    NavigationVoxelChunkStore,
    NAVIGATION_VOXEL_CHUNK_STORAGE_METHOD,
    navigation_voxel_chunk_relative_path_parts,
)


NAVIGATION_VOXEL_CACHE_VERSION = 12
NAVIGATION_VOXEL_CACHE_METHOD = "fixed_orthogonal_route_atlas_v12"
# Version 12 preserves the bounded 1 m horizontal field while sampling Y at
# 0.25 m. A half-metre-high passage therefore retains an interior layer
# without the 64x cost of 0.25 m isotropic cells. Exact cached-mesh checks
# remain runtime authority for every executable segment.
_PREVIOUS_NAVIGATION_VOXEL_CACHE_VERSION = 11
_PREVIOUS_NAVIGATION_VOXEL_CACHE_METHOD = "fixed_isotropic_route_atlas_v11"
_V10_NAVIGATION_VOXEL_CACHE_VERSION = 10
_V10_NAVIGATION_VOXEL_CACHE_METHOD = "whole_cave_voxel_atlas_v10"
_V9_NAVIGATION_VOXEL_CACHE_VERSION = 9
_V9_NAVIGATION_VOXEL_CACHE_METHOD = "whole_cave_voxel_atlas_v9"
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
NAVIGATION_VOXEL_ATLAS_MODEL_METHOD = "navigation_voxel_atlas_v12"
MeshPointSupportCheck = Callable[[Point, float, float], bool]
_PREVIOUS_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD = "navigation_voxel_atlas_v11"
_V10_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD = "navigation_voxel_atlas_v10"
_V9_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD = "navigation_voxel_atlas_v9"
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
DEFAULT_CACHE_VERTICAL_VOXEL_SIZE_M = 0.25
DEFAULT_CACHE_VOXEL_RANK_THRESHOLD = DEFAULT_VOXEL_CURVATURE_RANK_THRESHOLD
DEFAULT_CACHE_VOXEL_MAX_REGIONS = DEFAULT_VOXEL_MAX_REGIONS
DEFAULT_CACHE_VOXEL_MAX_CELLS = 65_536
DEFAULT_CACHE_VOXEL_MAX_SURFACE_SAMPLES = 250_000
DEFAULT_CACHE_VOXEL_MAX_ROUTES = 4
DEFAULT_CACHE_VOXEL_WINDOW_POINTS = 3
NAVIGATION_ROUTE_SELECTION_LONGEST_SAFE_NON_CIRCULAR = (
    "longest_safe_non_circular_certified_route_v1"
)
DEFAULT_CACHE_VOXEL_TILE_SIZE_M = 32.0
DEFAULT_CACHE_VOXEL_MAX_TILES = 4_096
DEFAULT_CACHE_VOXEL_ROUTE_CORRIDOR_RADIUS_M = 16.0
DEFAULT_CACHE_CUBIC_MAX_VOXELS = 8_388_608
# Long routes can exceed the packed graph budget even though one terminal path
# needs only a narrow tube. V12 automatically narrows that search tube under
# capacity pressure; it never changes the requested orthogonal resolution.
# The offline accuracy tier permits up to eight million packed keys so a
# 0.25 m vertical field can retain at least the horizontal uncertainty of a
# coarse surface-footprint cell on long caves.
MIN_CACHE_VOXEL_ROUTE_CORRIDOR_RADIUS_M = 4.0
MAX_MESH_GRAPH_FINE_RETRY_TUBE_RADIUS_M = 8.0
MIN_CACHE_VOXEL_MEANINGFUL_ROUTE_LENGTH_M = 32.0
DEFAULT_CUBIC_TERMINAL_CANDIDATE_LIMIT = 64
DEFAULT_CUBIC_INGRESS_CANDIDATE_LIMIT = 128
# A candidate may be collision-free because it lies in the unbounded void
# outside an open scan. Probe far enough to span large rooms while keeping
# cache-time mesh queries local and deterministic.
DEFAULT_MESH_OPPOSING_SUPPORT_DISTANCE_M = 128.0
# Guided Dive certifies exactly the requested source-zero-to-final route.
# Retrying later source ranges recreated the historical short-dive bug.
DEFAULT_MESH_TERMINAL_ROUTE_ATTEMPT_LIMIT = 1
# A broad metadata-to-voxel snap may be necessary when the authored endpoint
# sits outside sampled free space. Executable alternatives must still remain
# local to that selected endpoint: one 26-neighbor cubic shell is enough to
# escape a blocked center connector without turning an earlier route prefix
# into another terminal.
DEFAULT_CUBIC_TERMINAL_NEIGHBOR_RADIUS_VOXELS = math.sqrt(3.0)
MAX_CACHE_FIXED_VOXEL_SIZE_M = 1.0
MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M = 0.25
# The mesh roadmap is intentionally one complete selected-terminal path.
# Keep only a short metadata ingress to connect the user-visible route start
# to the selected voxel spine; the rest of the centerline is not topology.
DEFAULT_MESH_GRAPH_ENTRY_SEED_CELLS = 12
DEFAULT_MESH_GRAPH_ENTRY_SEED_POINTS = 8
# A half-metre execution lattice is the one universal bounded retry when a
# valid fixed voxel component is disconnected only by horizontal lattice
# alignment. It starts in a 4 m horizontal envelope and may widen once to 8 m
# after an exhaustive non-capacity failure. The source evidence remains fixed
# at 1 m-or-finer X/Z
# and 0.25 m-or-finer Y cells,
# and exact voxel membership plus cached-mesh checks still authorize every
# retry node and edge.
DEFAULT_MESH_GRAPH_FINE_RETRY_SPACING_M = 0.5
# The metadata route begins near the normal camera entry but its first voxel
# spine node need not be the one that has a mesh-clear connector from that
# pose. Preserve a small, bounded true-3D neighborhood as mesh-roadmap anchor
# candidates so preflight can choose the first exact-safe handoff.
# Imported OBJ vertices are surface evidence, never executable positions. A
# configuration may retain a broader runtime camera-to-roadmap search, but the
# source-derived ingress locator must never authorize a farther relocation.
MAX_OBJ_SOURCE_INGRESS_SNAP_DISTANCE_M = 24.0
# A 10 m surface-footprint step can represent a steep shaft whose bounded
# free-space intervals move by more than one mesh-roadmap edge.  This is only
# a proposal limit between two specific surface-derived intervals: the packed
# free component and cached-mesh checks must still prove every 0.25 m step.
MAX_SURFACE_GAP_VERTICAL_TRANSITION_M = 24.0
DEFAULT_MESH_GRAPH_ENTRY_ANCHOR_RADIUS_M = (
    MAX_OBJ_SOURCE_INGRESS_SNAP_DISTANCE_M
)
# A bounded second pass repairs footprint cells missed by the distributed
# surface-sampling budget. It is deliberately separate from the main budget:
# one missed component cell should not force the whole cave to use a much
# larger resident surface field.
DEFAULT_CACHE_VOXEL_COVERAGE_REPAIR_SAMPLE_BUDGET = 262_144
DEFAULT_CACHE_VOXEL_MAX_TILE_CELLS = 65_536
DEFAULT_CACHE_VOXEL_MAX_CELL_METRICS = 65_536
DEFAULT_CACHE_FINE_VOXEL_SIZE_M = 1.0
DEFAULT_CACHE_FINE_VERTICAL_VOXEL_SIZE_M = 0.25
DEFAULT_CACHE_FINE_TILE_RADIUS_M = 16.0
# Fine tiles form a contiguous corridor along the prepared selected-terminal
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
    vertical_voxel_size_m: float = DEFAULT_CACHE_VERTICAL_VOXEL_SIZE_M
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
    fine_vertical_voxel_size_m: float = (
        DEFAULT_CACHE_FINE_VERTICAL_VOXEL_SIZE_M
    )
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
    # authoritative path uses the same orthogonal 1 m x 0.25 m x 1 m lattice
    # by default and accepts topology only through exact cached-mesh checks.
    mesh_graph_horizontal_sample_spacing_m: float = 1.0
    mesh_graph_vertical_sample_spacing_m: float = 0.25
    mesh_graph_minimum_clearance_m: float = 0.25
    mesh_graph_max_nodes: int = 262_144
    mesh_graph_max_edges_per_node: int = 16
    mesh_graph_max_edge_candidates_per_node: int = 32
    mesh_graph_max_edge_candidates_per_direction: int = 2
    mesh_graph_max_edge_distance_m: float = 16.0
    mesh_graph_max_vertical_edge_distance_m: float = 8.0
    mesh_graph_entry_anchor_radius_m: float = (
        DEFAULT_MESH_GRAPH_ENTRY_ANCHOR_RADIUS_M
    )
    route_corridor_radius_m: float = DEFAULT_CACHE_VOXEL_ROUTE_CORRIDOR_RADIUS_M
    cubic_component_max_voxels: int = DEFAULT_CACHE_CUBIC_MAX_VOXELS

    def validated(self) -> "NavigationVoxelCacheConfig":
        size = float(self.voxel_size_m)
        if not math.isfinite(size) or size <= 0.0:
            raise ValueError("cache voxel size must be positive and finite")
        if size > MAX_CACHE_FIXED_VOXEL_SIZE_M + 1e-9:
            raise ValueError("V12 horizontal cache voxels must be 1 m or finer")
        vertical_size = float(self.vertical_voxel_size_m)
        if not math.isfinite(vertical_size) or vertical_size <= 0.0:
            raise ValueError("vertical cache voxel size must be positive and finite")
        if vertical_size > MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9:
            raise ValueError("V12 vertical cache voxels must be 0.25 m or finer")
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
        fine_vertical_size = float(self.fine_vertical_voxel_size_m)
        if not math.isfinite(fine_vertical_size) or fine_vertical_size <= 0.0:
            raise ValueError(
                "fine vertical voxel size must be positive and finite"
            )
        if fine_vertical_size > MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9:
            raise ValueError(
                "V12 fine vertical cache voxels must be 0.25 m or finer"
            )
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
        if (
            mesh_graph_config.vertical_sample_spacing_m
            > MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9
        ):
            raise ValueError(
                "V12 authoritative mesh graph Y spacing must be 0.25 m or finer"
            )
        entry_anchor_radius = float(self.mesh_graph_entry_anchor_radius_m)
        if not math.isfinite(entry_anchor_radius) or entry_anchor_radius <= 0.0:
            raise ValueError("mesh graph entry anchor radius must be positive")
        route_corridor_radius = float(self.route_corridor_radius_m)
        if (
            not math.isfinite(route_corridor_radius)
            or route_corridor_radius <= 0.0
        ):
            raise ValueError("route corridor radius must be positive")
        cubic_component_max_voxels = max(
            2,
            int(self.cubic_component_max_voxels),
        )
        return NavigationVoxelCacheConfig(
            voxel_size_m=size,
            vertical_voxel_size_m=vertical_size,
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
            fine_vertical_voxel_size_m=fine_vertical_size,
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
            route_corridor_radius_m=route_corridor_radius,
            cubic_component_max_voxels=cubic_component_max_voxels,
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
    # ``fixed_isotropic_voxel_size_m`` is retained as the horizontal X/Z
    # compatibility field for older callers. V12 pairs it with an explicit
    # vertical size and never treats the two as interchangeable.
    fixed_isotropic_voxel_size_m: float = 0.0
    fixed_vertical_voxel_size_m: float = 0.0
    surface_overlap_occupied_wins: bool = False
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
        fixed_size = float(self.fixed_isotropic_voxel_size_m)
        if not math.isfinite(fixed_size) or fixed_size < 0.0:
            raise ValueError("fixed isotropic voxel size is invalid")
        vertical_size = float(self.fixed_vertical_voxel_size_m)
        if not math.isfinite(vertical_size) or vertical_size < 0.0:
            raise ValueError("fixed vertical voxel size is invalid")
        if (fixed_size > 0.0) != (vertical_size > 0.0):
            # Legacy V11 callers set only the scalar field. Normalize that
            # representation to cubic cells without weakening V12 checks.
            if fixed_size > 0.0 and vertical_size == 0.0:
                object.__setattr__(
                    self,
                    "fixed_vertical_voxel_size_m",
                    fixed_size,
                )
            else:
                raise ValueError("fixed navigation voxel sizes are incomplete")
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
    def vertical_voxel_size_m(self) -> float:
        """Return the finest persisted vertical resolution."""
        all_tiles = tuple(self.tiles) + tuple(self.fine_tiles)
        if all_tiles:
            return float(
                min(
                    getattr(tile, "vertical_voxel_size_m", tile.voxel_size_m)
                    for tile in all_tiles
                )
            )
        if self.chunk_store is not None:
            return min(
                (
                    float(
                        getattr(
                            descriptor,
                            "vertical_voxel_size_m",
                            descriptor.voxel_size_m,
                        )
                    )
                    for descriptor in self.chunk_store.descriptors()
                ),
                default=0.0,
            )
        return float(self.fixed_vertical_voxel_size_m)

    @property
    def fixed_voxel_cell_size_m(self) -> tuple[float, float, float]:
        """Return the current fixed X/Y/Z cache resolution."""
        horizontal = float(self.fixed_isotropic_voxel_size_m)
        vertical = float(self.fixed_vertical_voxel_size_m)
        if horizontal <= 0.0 or vertical <= 0.0:
            return (0.0, 0.0, 0.0)
        return (horizontal, vertical, horizontal)

    @property
    def fine_voxel_size_m(self) -> float:
        """Return the finest persisted local refinement resolution."""
        if not self.fine_tiles:
            if self.fixed_isotropic_voxel_size_m > 0.0:
                return float(self.fixed_isotropic_voxel_size_m)
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

        Current caches use the direct-mesh path for production routing.
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
                if self.surface_overlap_occupied_wins and (
                    occupied or fine_occupied
                ):
                    return False, 0.0
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
            if self.surface_overlap_occupied_wins and (
                occupied or fine_occupied
            ):
                return False, 0.0
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
        fixed_tiles_are_fine = bool(
            not self.fine_tiles
            and self.fixed_isotropic_voxel_size_m > 0.0
        )
        if self.chunk_store is not None and not self.fine_tiles:
            for chunk_id in self.chunk_store.chunk_ids_for_point(
                point,
                fine_only=None if fixed_tiles_are_fine else True,
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
            if fixed_tiles_are_fine:
                tile = self._probe_tiles[tile_index]
            else:
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
        fixed_tiles_are_fine = bool(
            not self.fine_tiles
            and self.fixed_isotropic_voxel_size_m > 0.0
        )
        if self.chunk_store is not None and not self.fine_tiles:
            candidate_ids: set[str] | None = None
            for point in normalized:
                point_ids = set(
                    self.chunk_store.chunk_ids_for_point(
                        point,
                        fine_only=None if fixed_tiles_are_fine else True,
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
        candidate_tiles = self.tiles if fixed_tiles_are_fine else self.fine_tiles
        return tuple(
            tile
            for tile in candidate_tiles
            if all(tile.contains_point(point) for point in normalized)
        )

    def probe_fine_point(
        self,
        point: Sequence[float],
        *,
        include_clearance: bool = True,
    ) -> tuple[bool, float] | None:
        """Query only persisted fine frontier tiles."""
        if not self.fine_tiles and self.fixed_isotropic_voxel_size_m > 0.0:
            if self.chunk_store is not None and not self.tiles:
                fixed_tiles = tuple(
                    tile
                    for chunk_id in self.chunk_store.chunk_ids_for_point(
                        point,
                        fine_only=None,
                    )
                    if (tile := self.chunk_store.get_chunk(chunk_id)) is not None
                )
            else:
                fixed_tiles = self.tiles
            free_clearances: list[float] = []
            occupied = False
            for tile in fixed_tiles:
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
            if occupied:
                return False, 0.0
            if free_clearances:
                return True, max(free_clearances)
            return None
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
            len(tile.surface_cells)
            * float(getattr(tile, "cell_volume_m3", tile.voxel_size_m ** 3))
            for tile in self.tiles
        )
        if self.chunk_store is not None and not self.tiles:
            surface_occupied_volume_m3 = sum(
                descriptor.surface_cell_count
                * float(
                    getattr(
                        descriptor,
                        "cell_volume_m3",
                        descriptor.voxel_size_m ** 3,
                    )
                )
                for descriptor in self.chunk_store.descriptors(
                    fine_only=False,
                )
            )
        return {
            "model_kind": NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
            "cache_quality_profile": "fixed_orthogonal_terminal_route_v1",
            "coverage_scope": self.coverage_scope,
            "tile_count": int(self.tile_count),
            "fine_tile_count": int(self.fine_tile_count),
            "voxel_size_m": float(self.voxel_size_m),
            "vertical_voxel_size_m": float(self.vertical_voxel_size_m),
            "voxel_size_max_m": max(tile_sizes, default=0.0),
            "fine_voxel_size_m": float(self.fine_voxel_size_m),
            "fine_voxel_size_max_m": max(
                fine_tile_sizes,
                default=float(self.fixed_isotropic_voxel_size_m),
            ),
            "fixed_voxel_method": (
                FIXED_ORTHOGONAL_VOXEL_METHOD
                if self.fixed_isotropic_voxel_size_m > 0.0
                else None
            ),
            "fixed_isotropic_voxel_size_m": float(
                self.fixed_isotropic_voxel_size_m
            ),
            "fixed_vertical_voxel_size_m": float(
                self.fixed_vertical_voxel_size_m
            ),
            "fixed_voxel_cell_size_m": [
                float(value) for value in self.fixed_voxel_cell_size_m
            ],
            "cubic_graph_method": (
                CUBIC_VOXEL_GRAPH_METHOD
                if self.fixed_isotropic_voxel_size_m > 0.0
                else None
            ),
            "surface_overlap_policy": (
                "occupied_wins"
                if self.surface_overlap_occupied_wins
                else "legacy_free_preferred"
            ),
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
    mesh_point_has_opposing_support: MeshPointSupportCheck | None = None,
    config: NavigationVoxelCacheConfig | None = None,
) -> NavigationVoxelCacheBuildResult:
    """Build bounded voxel models and volume summaries for cached routes.

    ``navigation_metadata`` is updated in place with small route summaries;
    the returned payload contains the larger compressed models for the
    sidecar file. The route recommendation is changed only when a built model
    exists, and an explicit navigation-start route remains authoritative.
    """
    resolved = (config or NavigationVoxelCacheConfig()).validated()
    imported_ingress_anchor = _imported_navigation_start_anchor(
        navigation_metadata
    )
    navigation_start_ingress = _navigation_start_position(
        navigation_metadata
    )
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
            mesh_point_has_opposing_support=(
                mesh_point_has_opposing_support
            ),
            config=resolved,
            source_ingress_anchor=(
                imported_ingress_anchor
                if (
                    imported_ingress_anchor is not None
                    and route_value.get(
                        "starts_at_navigation_start_anchor"
                    )
                    is True
                )
                else (
                    navigation_start_ingress
                    if route_value.get("starts_at_navigation_start") is True
                    else None
                )
            ),
            source_ingress_is_obj_surface_anchor=(
                imported_ingress_anchor is not None
                and route_value.get(
                    "starts_at_navigation_start_anchor"
                )
                is True
            ),
        )
        certified_component_cells = summary.pop(
            "_certified_component_cells",
            None,
        )
        certified_route_points = summary.pop(
            "_certified_route_points",
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
        if bool(summary.get("built")):
            _publish_certified_complete_route(
                route_value,
                certified_route_points,
                source_point_offset=int(
                    summary["certified_ingress_hint_index"]
                ),
            )
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
    selection_method = NAVIGATION_ROUTE_SELECTION_LONGEST_SAFE_NON_CIRCULAR
    # A stale centerline recommendation must never survive when no complete
    # non-circular route can be certified, regardless of ingress source.
    navigation_metadata.pop("recommended_route_id", None)
    navigation_metadata["route_selection_method"] = selection_method
    if recommended_route_id is not None:
        navigation_metadata["recommended_route_id"] = recommended_route_id
        navigation_metadata["route_selection_method"] = selection_method
        _publish_selected_route_method(
            navigation_metadata,
            route_id=recommended_route_id,
            selection_method=selection_method,
        )
    payload: dict[str, object] = {
        "version": NAVIGATION_VOXEL_CACHE_VERSION,
        "method": NAVIGATION_VOXEL_CACHE_METHOD,
        "voxel_size_m": float(resolved.voxel_size_m),
        "vertical_voxel_size_m": float(resolved.vertical_voxel_size_m),
        "voxel_cell_size_m": [
            float(resolved.voxel_size_m),
            float(resolved.vertical_voxel_size_m),
            float(resolved.voxel_size_m),
        ],
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
        "fine_vertical_voxel_size_m": float(
            resolved.fine_vertical_voxel_size_m
        ),
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
        "cache_quality_profile": "fixed_orthogonal_terminal_route_v1",
        "coverage_scope": "certified_terminal_route",
        "fixed_voxel_method": FIXED_ORTHOGONAL_VOXEL_METHOD,
        "fixed_isotropic_voxel_size_m": float(resolved.voxel_size_m),
        "fixed_vertical_voxel_size_m": float(
            resolved.vertical_voxel_size_m
        ),
        "fixed_voxel_cell_size_m": [
            float(resolved.voxel_size_m),
            float(resolved.vertical_voxel_size_m),
            float(resolved.voxel_size_m),
        ],
        "route_corridor_radius_m": float(resolved.route_corridor_radius_m),
        "cubic_component_max_voxels": int(
            resolved.cubic_component_max_voxels
        ),
        "cubic_graph_method": CUBIC_VOXEL_GRAPH_METHOD,
        "surface_overlap_policy": "occupied_wins",
        "sampling_complete_required": True,
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
            "coverage_scope": "certified_terminal_route",
            "fixed_voxel_method": FIXED_ORTHOGONAL_VOXEL_METHOD,
            "fixed_isotropic_voxel_size_m": float(resolved.voxel_size_m),
            "fixed_vertical_voxel_size_m": float(
                resolved.vertical_voxel_size_m
            ),
            "fixed_voxel_cell_size_m": [
                float(resolved.voxel_size_m),
                float(resolved.vertical_voxel_size_m),
                float(resolved.voxel_size_m),
            ],
            "route_corridor_radius_m": float(resolved.route_corridor_radius_m),
            "cubic_component_max_voxels": int(
                resolved.cubic_component_max_voxels
            ),
            "cubic_graph_method": CUBIC_VOXEL_GRAPH_METHOD,
            "surface_overlap_policy": "occupied_wins",
            "sampling_complete_required": True,
            "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "tile_size_m": float(resolved.tile_size_m),
            "max_tiles": int(resolved.max_tiles),
            "coverage_repair_sample_budget": int(
                resolved.coverage_repair_sample_budget
            ),
            "fine_voxel_size_m": float(resolved.fine_voxel_size_m),
            "fine_vertical_voxel_size_m": float(
                resolved.fine_vertical_voxel_size_m
            ),
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
            "cache_quality_profile": "fixed_orthogonal_terminal_route_v1",
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
    fixed_size_m: float | None = None
    fixed_vertical_size_m: float | None = None
    if (
        model.get("version") == NAVIGATION_VOXEL_CACHE_VERSION
        and model.get("method") == NAVIGATION_VOXEL_ATLAS_MODEL_METHOD
    ):
        fixed_cell_size = _point(
            model.get("fixed_voxel_cell_size_m"),
            "fixed navigation voxel cell size",
        )
        fixed_size_m = float(fixed_cell_size[0])
        fixed_vertical_size_m = float(fixed_cell_size[1])
        try:
            declared_fixed_vertical_size_m = float(
                model["fixed_vertical_voxel_size_m"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "cached fixed navigation vertical voxel size is malformed"
            ) from exc
        if (
            not math.isclose(
                fixed_cell_size[0],
                fixed_cell_size[2],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or fixed_size_m > MAX_CACHE_FIXED_VOXEL_SIZE_M + 1e-9
            or fixed_vertical_size_m
            > MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9
            or not math.isfinite(declared_fixed_vertical_size_m)
            or declared_fixed_vertical_size_m <= 0.0
            or declared_fixed_vertical_size_m
            > MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9
            or not math.isclose(
                declared_fixed_vertical_size_m,
                fixed_vertical_size_m,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or model.get("sampling_complete") is not True
            or model.get("surface_overlap_policy") != "occupied_wins"
        ):
            raise ValueError("cached fixed navigation chunk policy is invalid")
        if any(
            not all(
                math.isclose(
                    descriptor.cell_size_m[axis],
                    fixed_cell_size[axis],
                    rel_tol=0.0,
                    abs_tol=1e-7,
                )
                for axis in range(3)
            )
            for descriptor in descriptors
        ):
            raise ValueError("cached fixed navigation chunk size is inconsistent")
    try:
        declared_chunk_count = int(
            raw_store.get("chunk_count", len(descriptors))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("cached navigation voxel chunk count is malformed") from exc
    for descriptor in descriptors:
        relative_path = descriptor.relative_path
        path_parts = (
            navigation_voxel_chunk_relative_path_parts(relative_path)
            if relative_path is not None
            else None
        )
        if (
            path_parts is None
            or len(path_parts) < 2
            or path_parts[0] != "navigation_voxel_chunks"
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
    def decode_chunk(payload: Mapping[str, object]) -> LocalVoxelVolume:
        volume = deserialize_local_voxel_volume(payload)
        if not isinstance(volume, LocalVoxelVolume):
            raise ValueError("cached navigation voxel chunk is not local")
        if fixed_size_m is not None and fixed_vertical_size_m is not None and (
            volume.sampling_truncated
            or not math.isclose(
                float(volume.voxel_size_m),
                fixed_size_m,
                rel_tol=0.0,
                abs_tol=1e-7,
            )
            or not math.isclose(
                float(
                    getattr(volume, "vertical_voxel_size_m", volume.voxel_size_m)
                ),
                fixed_vertical_size_m,
                rel_tol=0.0,
                abs_tol=1e-7,
            )
        ):
            raise ValueError("cached fixed navigation chunk is incomplete")
        return volume

    return DiskNavigationVoxelChunkStore(
        cache_dir,
        descriptors,
        decoder=decode_chunk,
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
    vertical_size = float(
        getattr(volume, "vertical_voxel_size_m", volume.voxel_size_m)
    )
    return {
        "version": 2,
        "method": "sparse_surface_voxels_zlib_int32_v2",
        "voxel_size_m": float(volume.voxel_size_m),
        "cell_size_m": [
            float(volume.voxel_size_m),
            vertical_size,
            float(volume.voxel_size_m),
        ],
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
        all_tiles = tuple(volume.tiles) + tuple(volume.fine_tiles)
        fixed_size_m = float(volume.fixed_isotropic_voxel_size_m)
        fixed_vertical_size_m = float(volume.fixed_vertical_voxel_size_m)
        if fixed_size_m <= 0.0:
            sizes = {float(tile.voxel_size_m) for tile in all_tiles}
            if len(sizes) != 1:
                raise ValueError(
                    "V12 navigation atlas requires one horizontal voxel size"
                )
            fixed_size_m = sizes.pop()
        if fixed_vertical_size_m <= 0.0:
            vertical_sizes = {
                float(
                    getattr(tile, "vertical_voxel_size_m", tile.voxel_size_m)
                )
                for tile in all_tiles
            }
            if len(vertical_sizes) != 1:
                raise ValueError(
                    "V12 navigation atlas requires one vertical voxel size"
                )
            fixed_vertical_size_m = vertical_sizes.pop()
        if (
            not math.isfinite(fixed_size_m)
            or fixed_size_m <= 0.0
            or fixed_size_m > MAX_CACHE_FIXED_VOXEL_SIZE_M + 1e-9
        ):
            raise ValueError("V12 horizontal navigation voxels are too coarse")
        if (
            not math.isfinite(fixed_vertical_size_m)
            or fixed_vertical_size_m <= 0.0
            or fixed_vertical_size_m
            > MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9
        ):
            raise ValueError("V12 vertical navigation voxels are too coarse")
        if any(
            tile.sampling_truncated
            or not math.isclose(
                float(tile.voxel_size_m),
                fixed_size_m,
                rel_tol=0.0,
                abs_tol=1e-7,
            )
            or not math.isclose(
                float(
                    getattr(tile, "vertical_voxel_size_m", tile.voxel_size_m)
                ),
                fixed_vertical_size_m,
                rel_tol=0.0,
                abs_tol=1e-7,
            )
            for tile in all_tiles
        ):
            raise ValueError("V12 navigation atlas tiles are incomplete")
        if not volume.surface_overlap_occupied_wins:
            raise ValueError("V12 navigation atlas requires occupied-wins overlap")
        if volume.coverage_scope != "certified_terminal_route":
            raise ValueError("V12 navigation atlas coverage scope is invalid")
        mesh_graph = volume.prepared_mesh_graph
        if (
            mesh_graph is None
            or mesh_graph.method != MESH_NAVIGATION_GRAPH_METHOD
            or not mesh_graph.nodes
            or mesh_graph.terminal_count <= 0
            or mesh_graph.unknown_boundary_count > 0
            or float(mesh_graph.grid_size_m[1])
            > MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9
        ):
            raise ValueError("V12 navigation atlas has no certified terminal path")
        return {
            "version": NAVIGATION_VOXEL_CACHE_VERSION,
            "method": NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
            "cache_quality_profile": "fixed_orthogonal_terminal_route_v1",
            "coverage_scope": volume.coverage_scope,
            "fixed_voxel_method": FIXED_ORTHOGONAL_VOXEL_METHOD,
            "fixed_isotropic_voxel_size_m": float(
                fixed_size_m
            ),
            "fixed_vertical_voxel_size_m": float(fixed_vertical_size_m),
            "fixed_voxel_cell_size_m": [
                float(fixed_size_m),
                float(fixed_vertical_size_m),
                float(fixed_size_m),
            ],
            "sampling_complete": True,
            "surface_overlap_policy": "occupied_wins",
            "cubic_graph_method": CUBIC_VOXEL_GRAPH_METHOD,
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
        _V10_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _V9_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _OLDER_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _ANCIENT_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _HISTORIC_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _LEGACY_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
    }:
        expected_versions = {
            NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: NAVIGATION_VOXEL_CACHE_VERSION,
            _PREVIOUS_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: (
                _PREVIOUS_NAVIGATION_VOXEL_CACHE_VERSION
            ),
            _V10_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: (
                _V10_NAVIGATION_VOXEL_CACHE_VERSION
            ),
            _V9_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: (
                _V9_NAVIGATION_VOXEL_CACHE_VERSION
            ),
            _OLDER_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: (
                _OLDER_NAVIGATION_VOXEL_CACHE_VERSION
            ),
            _ANCIENT_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: (
                _ANCIENT_NAVIGATION_VOXEL_CACHE_VERSION
            ),
            _HISTORIC_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: (
                _HISTORIC_NAVIGATION_VOXEL_CACHE_VERSION
            ),
            _LEGACY_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD: (
                _LEGACY_PREPARED_NAVIGATION_VOXEL_CACHE_VERSION
            ),
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
            _V10_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
            _V9_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        }:
            graph_3d_payload = payload.get("prepared_3d_graph")
            if graph_3d_payload is not None:
                prepared_3d_graph = deserialize_navigation_voxel_3d_graph(
                    graph_3d_payload,
                    max_nodes=DEFAULT_3D_GRAPH_MAX_NODES,
                    max_edges=DEFAULT_3D_GRAPH_MAX_EDGES,
                )
        if atlas_method in {
            NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
            _PREVIOUS_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
            _V10_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        }:
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
            _V10_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
            _V9_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
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
        if atlas_method == NAVIGATION_VOXEL_ATLAS_MODEL_METHOD:
            if payload.get("fixed_voxel_method") != FIXED_ORTHOGONAL_VOXEL_METHOD:
                raise ValueError("cached fixed navigation voxel method is invalid")
            fixed_cell_size = _point(
                payload.get("fixed_voxel_cell_size_m"),
                "fixed navigation voxel cell size",
            )
            fixed_size_m = float(fixed_cell_size[0])
            fixed_vertical_size_m = float(fixed_cell_size[1])
            try:
                declared_fixed_vertical_size_m = float(
                    payload["fixed_vertical_voxel_size_m"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "cached fixed navigation vertical voxel size is malformed"
                ) from exc
            if (
                not math.isclose(
                    fixed_cell_size[0],
                    fixed_cell_size[2],
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                or fixed_size_m > MAX_CACHE_FIXED_VOXEL_SIZE_M + 1e-9
                or fixed_vertical_size_m
                > MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9
                or not math.isfinite(declared_fixed_vertical_size_m)
                or declared_fixed_vertical_size_m <= 0.0
                or declared_fixed_vertical_size_m
                > MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9
                or not math.isclose(
                    declared_fixed_vertical_size_m,
                    fixed_vertical_size_m,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            ):
                raise ValueError("cached fixed navigation voxels are too coarse")
            if payload.get("sampling_complete") is not True:
                raise ValueError("cached fixed navigation sampling is incomplete")
            if payload.get("surface_overlap_policy") != "occupied_wins":
                raise ValueError("cached fixed navigation overlap policy is invalid")
            if payload.get("cubic_graph_method") != CUBIC_VOXEL_GRAPH_METHOD:
                raise ValueError(
                    "cached fixed navigation cubic graph method is invalid"
                )
            if payload.get("coverage_scope") != "certified_terminal_route":
                raise ValueError("cached fixed navigation coverage scope is invalid")
            if any(
                tile.sampling_truncated
                or not math.isclose(
                    float(tile.voxel_size_m),
                    fixed_size_m,
                    rel_tol=0.0,
                    abs_tol=1e-7,
                )
                or not math.isclose(
                    float(
                        getattr(
                            tile,
                            "vertical_voxel_size_m",
                            tile.voxel_size_m,
                        )
                    ),
                    fixed_vertical_size_m,
                    rel_tol=0.0,
                    abs_tol=1e-7,
                )
                for tile in tuple(tiles) + tuple(fine_tiles)
            ):
                raise ValueError("cached fixed navigation tiles are inconsistent")
            if chunk_store is not None and any(
                not all(
                    math.isclose(
                        descriptor.cell_size_m[axis],
                        fixed_cell_size[axis],
                        rel_tol=0.0,
                        abs_tol=1e-7,
                    )
                    for axis in range(3)
                )
                for descriptor in chunk_store.descriptors()
            ):
                raise ValueError("cached fixed navigation chunk index is inconsistent")
            if (
                prepared_mesh_graph is None
                or prepared_mesh_graph.method != MESH_NAVIGATION_GRAPH_METHOD
                or not prepared_mesh_graph.nodes
                or prepared_mesh_graph.terminal_count <= 0
                or prepared_mesh_graph.unknown_boundary_count > 0
                or float(prepared_mesh_graph.grid_size_m[1])
                > MAX_CACHE_FIXED_VERTICAL_VOXEL_SIZE_M + 1e-9
            ):
                raise ValueError(
                    "cached fixed navigation terminal path is invalid"
                )
            occupied_wins = True
        else:
            fixed_size_m = 0.0
            fixed_vertical_size_m = 0.0
            occupied_wins = False
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
            fixed_isotropic_voxel_size_m=fixed_size_m,
            fixed_vertical_voxel_size_m=fixed_vertical_size_m,
            surface_overlap_occupied_wins=occupied_wins,
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
        _V10_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _V9_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _OLDER_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _ANCIENT_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
        _HISTORIC_NAVIGATION_VOXEL_ATLAS_MODEL_METHOD,
    }:
        return deserialize_navigation_voxel_volume(payload)
    local_identity = (payload.get("version"), payload.get("method"))
    if local_identity not in {
        (1, "sparse_surface_voxels_zlib_int32_v1"),
        (2, "sparse_surface_voxels_zlib_int32_v2"),
    }:
        raise ValueError("unsupported navigation voxel model method")
    size = _positive_float(payload.get("voxel_size_m"), "voxel size")
    if local_identity[0] == 2:
        cell_size = _point(payload.get("cell_size_m"), "voxel cell size")
        if (
            not math.isclose(cell_size[0], size, rel_tol=0.0, abs_tol=1e-9)
            or not math.isclose(cell_size[2], size, rel_tol=0.0, abs_tol=1e-9)
            or cell_size[1] <= 0.0
        ):
            raise ValueError("cached navigation voxel cell size is invalid")
        vertical_size = float(cell_size[1])
    else:
        vertical_size = float(size)
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
        vertical_voxel_size_m=vertical_size,
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
    mesh_point_has_opposing_support: MeshPointSupportCheck | None = None,
    config: NavigationVoxelCacheConfig,
    source_ingress_anchor: Point | None = None,
    source_ingress_is_obj_surface_anchor: bool = False,
) -> dict[str, object]:
    common: dict[str, object] = {
        "version": NAVIGATION_VOXEL_CACHE_VERSION,
        "method": NAVIGATION_VOXEL_CACHE_METHOD,
        "curvature_method": CURVATURE_PROFILE_METHOD,
        "voxel_size_m": float(config.voxel_size_m),
        "vertical_voxel_size_m": float(config.vertical_voxel_size_m),
        "voxel_cell_size_m": [
            float(config.voxel_size_m),
            float(config.vertical_voxel_size_m),
            float(config.voxel_size_m),
        ],
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
        "fine_vertical_voxel_size_m": float(
            config.fine_vertical_voxel_size_m
        ),
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
        "cache_quality_profile": "fixed_orthogonal_terminal_route_v1",
        "coverage_scope": "certified_terminal_route",
        "fixed_voxel_method": FIXED_ORTHOGONAL_VOXEL_METHOD,
        "fixed_isotropic_voxel_size_m": float(config.voxel_size_m),
        "fixed_vertical_voxel_size_m": float(config.vertical_voxel_size_m),
        "fixed_voxel_cell_size_m": [
            float(config.voxel_size_m),
            float(config.vertical_voxel_size_m),
            float(config.voxel_size_m),
        ],
        "route_corridor_radius_m": float(config.route_corridor_radius_m),
        "cubic_component_max_voxels": int(
            config.cubic_component_max_voxels
        ),
        "cubic_graph_method": CUBIC_VOXEL_GRAPH_METHOD,
        "surface_overlap_policy": "occupied_wins",
        "sampling_complete_required": True,
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
        attempt_route: Mapping[str, object] = route
        attempt_points = points
        attempt_source_offset = 0
        terminal_route_attempts: list[dict[str, object]] = []
        atlas: NavigationVoxelAtlas | None = None
        metrics: dict[str, float | int | bool] = {}
        atlas_details: dict[str, object] = {}
        for _attempt_index in range(
            DEFAULT_MESH_TERMINAL_ROUTE_ATTEMPT_LIMIT
        ):
            atlas, metrics, atlas_details = _build_route_voxel_atlas(
                manifest,
                attempt_route,
                attempt_points,
                triangle_provider=triangle_provider,
                mesh_edge_is_clear=mesh_edge_is_clear,
                mesh_point_has_opposing_support=(
                    mesh_point_has_opposing_support
                ),
                config=config,
                source_ingress_anchor=source_ingress_anchor,
                source_ingress_is_obj_surface_anchor=(
                    source_ingress_is_obj_surface_anchor
                ),
            )
            cubic_component = atlas_details.get("cubic_component")
            cubic_details = (
                cubic_component
                if isinstance(cubic_component, Mapping)
                else {}
            )
            mesh_graph = atlas_details.get("prepared_mesh_graph")
            mesh_details = (
                mesh_graph if isinstance(mesh_graph, Mapping) else {}
            )
            terminal_route_attempts.append(
                {
                    "source_hint_start_index": int(attempt_source_offset),
                    "source_hint_end_index": int(
                        attempt_source_offset + len(attempt_points) - 1
                    ),
                    "source_hint_point_count": len(attempt_points),
                    "built": bool(
                        atlas is not None
                        and atlas.has_prepared_mesh_graph
                    ),
                    "reason": str(mesh_details.get("reason", "")),
                    "ingress_hint_index": cubic_details.get(
                        "ingress_hint_index"
                    ),
                    "terminal_hint_index": cubic_details.get(
                        "terminal_hint_index"
                    ),
                    "contiguous_route_length_m": float(
                        cubic_details.get("contiguous_route_length_m", 0.0)
                    ),
                    "selected_component_voxel_count": int(
                        cubic_details.get(
                            "selected_component_voxel_count",
                            0,
                        )
                    ),
                }
            )
            if atlas is not None and atlas.has_prepared_mesh_graph:
                break
            # Guided Dive is one complete route. A connected later range is
            # useful diagnostic evidence, but rebuilding it as an executable
            # suffix would recreate the historical short-dive failure.
            break
        if terminal_route_attempts:
            atlas_details["mesh_terminal_route_attempt_count"] = len(
                terminal_route_attempts
            )
            atlas_details["mesh_terminal_route_attempts"] = list(
                terminal_route_attempts
            )
            atlas_details["selected_source_route_point_count"] = len(
                attempt_points
            )
            atlas_details["source_route_point_count"] = len(points)
            for field_name in (
                "certified_ingress_hint_index",
                "certified_terminal_hint_index",
            ):
                value = atlas_details.get(field_name)
                if isinstance(value, int):
                    atlas_details[field_name] = (
                        int(value) + attempt_source_offset
                    )
            certified_start = atlas_details.get(
                "certified_ingress_hint_index"
            )
            certified_end = atlas_details.get(
                "certified_terminal_hint_index"
            )
            atlas_details["selected_source_hint_start_index"] = int(
                certified_start
                if isinstance(certified_start, int)
                else attempt_source_offset
            )
            atlas_details["selected_source_hint_end_index"] = int(
                certified_end
                if isinstance(certified_end, int)
                else attempt_source_offset + len(attempt_points) - 1
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

    try:
        full_ingress_route = bool(
            attempt_source_offset == 0
            and int(atlas_details["certified_ingress_hint_index"]) == 0
            and int(atlas_details["certified_terminal_hint_index"])
            == len(points) - 1
            and int(atlas_details["selected_source_hint_start_index"])
            == 0
            and int(atlas_details["selected_source_hint_end_index"])
            == len(points) - 1
        )
    except (KeyError, TypeError, ValueError):
        full_ingress_route = False
    authoritative_route_built = bool(
        atlas is not None
        and atlas.has_prepared_mesh_graph
        and full_ingress_route
    )
    route_evidence_built = bool(
        atlas is not None
        or int(atlas_details.get("surface_sample_count", 0)) > 0
        or isinstance(atlas_details.get("cubic_graph"), Mapping)
    )
    common.update(
        {
            "outcome": (
                "built"
                if authoritative_route_built
                else "known_terminal_unreachable"
                if route_evidence_built
                else "no_surface_samples"
            ),
            "built": authoritative_route_built,
            "complete_ingress_route": bool(full_ingress_route),
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
    if atlas is None or not authoritative_route_built:
        return common

    try:
        route_length = float(atlas_details["certified_route_length_m"])
    except (KeyError, TypeError, ValueError):
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
    base_vertical_voxel_size: float | None = None,
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
    vertical = max(
        1e-6,
        float(
            base
            if base_vertical_voxel_size is None
            else base_vertical_voxel_size
        ),
    )
    target_nodes = max(1_024, int(max_nodes) // 4)
    horizontal_factor = 1
    while (
        int(filled_cell_count) > target_nodes * horizontal_factor * horizontal_factor
        and horizontal_factor < 64
    ):
        horizontal_factor *= 2
    horizontal_size = base * horizontal_factor
    return horizontal_size, vertical, horizontal_size


def _build_route_voxel_atlas(
    manifest: Mapping[str, object],
    route: Mapping[str, object],
    points: tuple[Point, ...],
    *,
    triangle_provider: TriangleProvider,
    mesh_edge_is_clear: MeshEdgeSafetyCheck | None,
    mesh_point_has_opposing_support: MeshPointSupportCheck | None = None,
    config: NavigationVoxelCacheConfig,
    source_ingress_anchor: Point | None = None,
    source_ingress_is_obj_surface_anchor: bool = False,
) -> tuple[
    NavigationVoxelAtlas | None,
    dict[str, float | int | bool],
    dict[str, object],
]:
    """Build fixed voxel evidence and one exact terminal path."""
    component_cells = _flat_cells(route.get("component_cells"))
    coverage_scope = "certified_terminal_route"
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
    route_cells = _consecutive_route_cells(_flat_cells(route.get("cells")))
    if len(set(route_cells)) != len(route_cells):
        return None, {}, {
            "coverage_cell_count": 0,
            "tile_count": 0,
            "coverage_scope": coverage_scope,
            "coverage_includes_preceding_curvature": False,
            "triangle_count": 0,
            "surface_sample_count": 0,
            "sampling_truncated": False,
            "reason": "non_circular_route_revisits_footprint_cell",
        }
    route_cell_horizontal_guides = _route_cell_horizontal_guide_points(
        route_cells,
        cell_size=cell_size,
    )
    horizontal_guides = route_cell_horizontal_guides or points
    corridor_points = (
        tuple(dict.fromkeys((source_ingress_anchor, *horizontal_guides)))
        if source_ingress_anchor is not None
        else horizontal_guides
    )
    corridor_cells = tuple(route_cells or component_cells)
    if source_ingress_anchor is not None:
        source_cell = (
            math.floor(source_ingress_anchor[0] / cell_size),
            math.floor(source_ingress_anchor[2] / cell_size),
        )
        corridor_cells = tuple(dict.fromkeys((source_cell, *corridor_cells)))
    sampling_cells = _fixed_route_corridor_cells(
        corridor_cells,
        component_cells,
        cell_size=cell_size,
        radius_m=config.route_corridor_radius_m,
    )
    component_cell_set = set(component_cells)
    requested_component_cell_set = set(component_cell_set)
    y_ranges = _route_y_ranges(route.get("component_y_ranges"), component_cells)
    component_vertical_gap_seeds = _component_vertical_gap_seed_points(
        route.get("component_vertical_gap_seeds"),
        component_cells=component_cell_set,
        cell_size=cell_size,
    )
    component_vertical_gap_intervals = _component_vertical_gap_intervals(
        route.get("component_vertical_gap_intervals"),
        component_cells=component_cell_set,
    )
    required_route_vertical_gap_intervals = component_vertical_gap_intervals
    selected_layer_y_ranges: dict[
        FootprintCell,
        tuple[float, float],
    ] = {}
    if source_ingress_anchor is not None:
        (
            component_vertical_gap_seeds,
            selected_layer_y_ranges,
        ) = _source_connected_vertical_gap_layer(
            component_vertical_gap_intervals,
            route_cells=route_cells,
            eligible_cells=set(sampling_cells),
            source_ingress_anchor=source_ingress_anchor,
            cell_size=cell_size,
            max_attachment_distance_m=min(
                float(config.mesh_graph_entry_anchor_radius_m),
                MAX_OBJ_SOURCE_INGRESS_SNAP_DISTANCE_M,
            ),
            # One footprint cell spans many execution voxels. Permit a
            # proposal to climb through a steep shaft, but only between the
            # one entrance-connected pair of bounded intervals chosen for
            # consecutive cells. Global voxel connectivity still has to
            # prove the climb rather than treating this rank as authority.
            max_vertical_transition_m=max(
                float(MAX_SURFACE_GAP_VERTICAL_TRANSITION_M),
                float(cell_size) * math.sqrt(2.0),
            ),
        )
        if not component_vertical_gap_seeds or not selected_layer_y_ranges:
            return None, {}, {
                "coverage_cell_count": 0,
                "sampling_support_cell_count": len(sampling_cells),
                "tile_count": 0,
                "coverage_scope": coverage_scope,
                "coverage_includes_preceding_curvature": False,
                "triangle_count": 0,
                "surface_sample_count": 0,
                "sampling_truncated": False,
                "reason": "source_connected_vertical_gap_layer_missing",
                "surface_gap_interval_count": sum(
                    len(value)
                    for value in component_vertical_gap_intervals.values()
                ),
            }
        required_route_vertical_gap_intervals = {
            cell: (selected_layer_y_ranges[cell],)
            for cell in route_cells
            if cell in selected_layer_y_ranges
        }
        if len(required_route_vertical_gap_intervals) != len(
            route_cells
        ):
            return None, {}, {
                "coverage_cell_count": 0,
                "sampling_support_cell_count": len(sampling_cells),
                "tile_count": 0,
                "coverage_scope": coverage_scope,
                "coverage_includes_preceding_curvature": False,
                "triangle_count": 0,
                "surface_sample_count": 0,
                "sampling_truncated": False,
                "reason": "surface_gap_route_interval_chain_missing",
            }
        transition_y_ranges = _route_transition_sampling_y_ranges(
            selected_layer_y_ranges,
            route_cells=route_cells,
        )
        if not transition_y_ranges:
            return None, {}, {
                "coverage_cell_count": 0,
                "sampling_support_cell_count": len(sampling_cells),
                "tile_count": 0,
                "coverage_scope": coverage_scope,
                "coverage_includes_preceding_curvature": False,
                "triangle_count": 0,
                "surface_sample_count": 0,
                "sampling_truncated": False,
                "reason": "surface_gap_transition_envelope_missing",
            }
        y_ranges = transition_y_ranges
        component_cell_set = set(selected_layer_y_ranges)
        component_cells = tuple(sorted(component_cell_set))
        sampling_cells = tuple(
            cell for cell in sampling_cells if cell in component_cell_set
        )
    fallback_y_range = _fallback_y_range(manifest)
    tile_size = max(float(config.tile_size_m), float(config.voxel_size_m))
    groups = _component_tile_groups(
        sampling_cells,
        cell_size=cell_size,
        tile_size=tile_size,
    )
    padding = max(config.voxel_size_m * 2.0, cell_size * 0.25)
    tiles: list[LocalVoxelVolume] = []
    tile_seed_points: list[tuple[Point, ...]] = []
    true_3d_base_voxel_size = max(
        1e-6,
        min(
            float(config.voxel_size_m),
            float(config.fine_voxel_size_m),
        ),
    )
    true_3d_base_vertical_voxel_size = max(
        1e-6,
        min(
            float(config.vertical_voxel_size_m),
            float(config.fine_vertical_voxel_size_m),
        ),
    )
    total_samples = 0
    total_triangles = 0
    sampling_truncated = False
    skipped_tiles = 0

    def retain_tile(
        tile: LocalVoxelVolume,
        tile_points: Sequence[Point],
    ) -> bool:
        """Retain one tile and merge its bounded footprint metrics."""
        nonlocal total_samples
        nonlocal total_triangles
        nonlocal sampling_truncated
        nonlocal skipped_tiles

        total_triangles += int(tile.triangle_count)
        total_samples += int(tile.surface_sample_count)
        sampling_truncated = sampling_truncated or bool(tile.sampling_truncated)
        if tile.triangle_count <= 0 and not tile_points:
            skipped_tiles += 1
            return False
        tiles.append(tile)
        tile_seed_points.append(tuple(tile_points))
        return True

    fixed_regions: list[FixedVoxelRegion] = []
    for cells in groups:
        target_cells = tuple(
            cell for cell in cells if cell in component_cell_set
        )
        if not target_cells:
            continue
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
            vertical_gap_seeds=component_vertical_gap_seeds,
        )
        fixed_regions.append(
            FixedVoxelRegion(
                bounds_min=bounds_min,
                bounds_max=bounds_max,
                # OBJ vertex zero is immutable surface evidence, not an
                # executable/free-space seed. Only the selected bounded
                # surface-gap points may initiate fixed-volume flood fill.
                seed_points=tuple(dict.fromkeys(tile_points)),
            )
        )
    fixed_build = build_fixed_orthogonal_voxel_tiles(
        tuple(fixed_regions),
        triangle_provider=triangle_provider,
        voxel_size_m=config.voxel_size_m,
        vertical_voxel_size_m=config.vertical_voxel_size_m,
        chunk_edge_m=tile_size + 2.0 * padding,
        max_chunks=config.max_tiles,
        max_voxels_per_chunk=min(
            config.max_cells,
            DEFAULT_CACHE_VOXEL_MAX_TILE_CELLS,
        ),
        max_surface_samples_per_chunk=config.max_surface_samples,
        surface_inflation_cells=0,
    )
    for tile, tile_points in zip(
        fixed_build.tiles,
        fixed_build.tile_seed_points,
        strict=True,
    ):
        retain_tile(tile, tile_points)

    if not tiles:
        return None, {}, {
            "coverage_cell_count": 0,
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
            "fixed_voxel_build": dict(fixed_build.details),
            "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "navigation_cell_count": 0,
        }

    cubic_build: CubicVoxelGraphBuildResult | None = None
    cubic_corridor_attempts: list[dict[str, object]] = []
    selected_corridor_radius_m = float(config.route_corridor_radius_m)
    for candidate_radius_m in _cubic_corridor_radius_candidates(
        config.route_corridor_radius_m,
        voxel_size_m=config.voxel_size_m,
        minimum_radius_m=(
            float(cell_size) * math.sqrt(2.0) * 0.5
            if source_ingress_is_obj_surface_anchor
            else None
        ),
    ):
        candidate_corridor_cells = set(
            _fixed_route_corridor_cells(
                corridor_cells,
                component_cells,
                cell_size=cell_size,
                radius_m=candidate_radius_m,
            )
        )
        route_tube_contains = _horizontal_route_tube_point_filter(
            corridor_points,
            radius_m=candidate_radius_m,
            voxel_size_m=config.voxel_size_m,
        )

        def point_in_candidate_corridor(
            point: Point,
            *,
            allowed_cells: set[FootprintCell] = candidate_corridor_cells,
        ) -> bool:
            cell = (
                math.floor(float(point[0]) / cell_size),
                math.floor(float(point[2]) / cell_size),
            )
            low_y, high_y = _cell_y_range(
                cell,
                y_ranges,
                fallback_y_range,
            )
            vertical_quantization_margin_m = (
                float(config.vertical_voxel_size_m) * 0.5 + 1e-9
            )
            return bool(
                cell in allowed_cells
                and low_y - vertical_quantization_margin_m
                <= float(point[1])
                <= high_y + vertical_quantization_margin_m
                and route_tube_contains(point)
            )

        try:
            candidate_build = build_cubic_graph_from_local_volumes(
                tuple(zip(tiles, tile_seed_points, strict=True)),
                voxel_size_m=config.voxel_size_m,
                vertical_voxel_size_m=config.vertical_voxel_size_m,
                minimum_clearance_m=config.mesh_graph_minimum_clearance_m,
                point_filter=point_in_candidate_corridor,
                # Local seed fills are tile-construction diagnostics, not
                # global connectivity authority.  Merge every bounded
                # non-surface cell first so an imperfect seed or tile seam
                # cannot erase a real passage; occupied observations still
                # win globally, and the single source-to-terminal component
                # plus exact mesh checks remain the execution proof.
                include_all_filtered_free_cells=True,
                max_free_voxels=config.cubic_component_max_voxels,
            )
        except CubicVoxelLimitExceededError:
            cubic_corridor_attempts.append(
                {
                    "radius_m": float(candidate_radius_m),
                    "outcome": "free_voxel_limit_exceeded",
                    "max_free_voxels": int(
                        config.cubic_component_max_voxels
                    ),
                }
            )
            continue
        selected_corridor_radius_m = float(candidate_radius_m)
        cubic_corridor_attempts.append(
            {
                "radius_m": float(candidate_radius_m),
                "outcome": "built",
                "free_voxel_count": int(
                    candidate_build.graph.free_voxel_count
                ),
            }
        )
        cubic_build = CubicVoxelGraphBuildResult(
            graph=candidate_build.graph,
            details={
                **candidate_build.details,
                "point_filter_method": "horizontal_polyline_envelope_v1",
                "selected_route_corridor_radius_m": float(
                    selected_corridor_radius_m
                ),
                "route_corridor_attempts": list(cubic_corridor_attempts),
            },
        )
        break
    if cubic_build is None:
        raise CubicVoxelLimitExceededError(
            config.cubic_component_max_voxels
        )
    cubic_graph_details = dict(cubic_build.details)
    selected_cubic_graph, cubic_component_details = (
        _select_terminal_cubic_component(
            cubic_build.graph,
            points,
            terminal_snap_distance_m=float(
                config.mesh_graph_max_edge_distance_m
            ),
            ingress_snap_distance_m=float(
                config.mesh_graph_max_edge_distance_m
            ),
            max_component_voxels=config.cubic_component_max_voxels,
            require_original_ingress=bool(
                route.get("starts_at_navigation_start")
                or source_ingress_anchor is not None
            ),
            source_ingress_point=source_ingress_anchor,
            source_ingress_snap_distance_m=min(
                float(config.mesh_graph_entry_anchor_radius_m),
                MAX_OBJ_SOURCE_INGRESS_SNAP_DISTANCE_M,
            ),
            source_ingress_gap_y_ranges=(
                selected_layer_y_ranges
                if source_ingress_anchor is not None
                else None
            ),
            source_ingress_footprint_cell_size_m=(
                cell_size if source_ingress_anchor is not None else None
            ),
            required_route_cells=route_cells,
            required_vertical_gap_intervals=(
                required_route_vertical_gap_intervals
            ),
            required_footprint_cell_size_m=cell_size,
        )
    )
    # The selected component owns its packed-key set. Drop the much larger
    # preselection graph before the later metrics/path passes so a long cave
    # does not retain both resident sets for the rest of cache construction.
    cubic_build = None
    candidate_build = None
    if selected_cubic_graph is None:
        return None, {}, {
            "coverage_cell_count": 0,
            "sampling_support_cell_count": len(sampling_cells),
            "tile_count": len(tiles),
            "coverage_scope": coverage_scope,
            "coverage_includes_preceding_curvature": False,
            "tile_size_m": float(tile_size),
            "tiles_skipped": int(skipped_tiles),
            "triangle_count": int(total_triangles),
            "surface_sample_count": int(total_samples),
            "sampling_truncated": bool(sampling_truncated),
            "fixed_voxel_build": dict(fixed_build.details),
            "cubic_graph": dict(cubic_graph_details),
            "cubic_component": cubic_component_details,
            "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "navigation_cell_count": 0,
        }

    selected_surface_gap_route_key_groups = cubic_component_details.pop(
        "_surface_gap_route_key_groups",
        None,
    )
    if (
        not isinstance(selected_surface_gap_route_key_groups, Sequence)
        or isinstance(
            selected_surface_gap_route_key_groups,
            (str, bytes),
        )
        or len(selected_surface_gap_route_key_groups)
        != len(route_cells)
    ):
        return None, {}, {
            "coverage_cell_count": 0,
            "sampling_support_cell_count": len(sampling_cells),
            "tile_count": len(tiles),
            "coverage_scope": coverage_scope,
            "coverage_includes_preceding_curvature": False,
            "tile_size_m": float(tile_size),
            "tiles_skipped": int(skipped_tiles),
            "triangle_count": int(total_triangles),
            "surface_sample_count": int(total_samples),
            "sampling_truncated": bool(sampling_truncated),
            "fixed_voxel_build": dict(fixed_build.details),
            "cubic_graph": dict(cubic_graph_details),
            "cubic_component": cubic_component_details,
            "reason": "surface_gap_route_component_evidence_missing",
            "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "navigation_cell_count": 0,
        }
    selected_surface_gap_route_key_groups = tuple(
        tuple(
            (int(key[0]), int(key[1]), int(key[2]))
            for key in group
        )
        for group in selected_surface_gap_route_key_groups
    )
    selected_route_gap_intervals: list[tuple[float, float]] = []
    for cell, group in zip(
        route_cells,
        selected_surface_gap_route_key_groups,
        strict=True,
    ):
        intervals = tuple(
            required_route_vertical_gap_intervals.get(cell, ())
        )
        if not intervals or not group:
            return None, {}, {
                "coverage_cell_count": 0,
                "sampling_support_cell_count": len(sampling_cells),
                "tile_count": len(tiles),
                "coverage_scope": coverage_scope,
                "coverage_includes_preceding_curvature": False,
                "tile_size_m": float(tile_size),
                "tiles_skipped": int(skipped_tiles),
                "triangle_count": int(total_triangles),
                "surface_sample_count": int(total_samples),
                "sampling_truncated": bool(sampling_truncated),
                "fixed_voxel_build": dict(fixed_build.details),
                "cubic_graph": dict(cubic_graph_details),
                "cubic_component": cubic_component_details,
                "reason": "surface_gap_selected_interval_evidence_missing",
                "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
                "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
                "navigation_cell_count": 0,
            }
        key_center_y = float(
            selected_cubic_graph.voxel_center(group[0])[1]
        )
        selected_route_gap_intervals.append(
            min(
                (
                    (float(interval[0]), float(interval[1]))
                    for interval in intervals
                ),
                key=lambda interval: (
                    0.0
                    if interval[0] <= key_center_y <= interval[1]
                    else min(
                        abs(key_center_y - interval[0]),
                        abs(key_center_y - interval[1]),
                    ),
                    interval,
                ),
            )
        )
    cubic_component_details.update(
        {
            "surface_gap_route_cells": [
                int(value) for cell in route_cells for value in cell
            ],
            "surface_gap_selected_route_intervals": [
                float(value)
                for interval in selected_route_gap_intervals
                for value in interval
            ],
            "surface_gap_selected_route_interval_count": len(
                selected_route_gap_intervals
            ),
        }
    )

    raw_ingress_hint_index = cubic_component_details.get(
        "ingress_hint_index"
    )
    raw_terminal_hint_index = cubic_component_details.get(
        "terminal_hint_index"
    )
    if (
        type(raw_ingress_hint_index) is not int
        or type(raw_terminal_hint_index) is not int
        or raw_ingress_hint_index != 0
        or raw_terminal_hint_index != len(points) - 1
    ):
        return None, {}, {
            "coverage_cell_count": 0,
            "sampling_support_cell_count": len(sampling_cells),
            "tile_count": len(tiles),
            "coverage_scope": coverage_scope,
            "coverage_includes_preceding_curvature": False,
            "tile_size_m": float(tile_size),
            "tiles_skipped": int(skipped_tiles),
            "triangle_count": int(total_triangles),
            "surface_sample_count": int(total_samples),
            "sampling_truncated": bool(sampling_truncated),
            "fixed_voxel_build": dict(fixed_build.details),
            "cubic_graph": dict(cubic_graph_details),
            "cubic_component": cubic_component_details,
            "reason": "cubic_component_full_route_hint_span_missing",
            "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
            "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
            "navigation_cell_count": 0,
        }
    certified_ingress_hint_index = raw_ingress_hint_index
    certified_terminal_hint_index = raw_terminal_hint_index
    terminal_graph_key_payload = cubic_component_details.get(
        "terminal_graph_key"
    )
    if (
        not isinstance(terminal_graph_key_payload, Sequence)
        or isinstance(terminal_graph_key_payload, (str, bytes))
        or len(terminal_graph_key_payload) != 3
    ):
        raise ValueError("selected cubic terminal key is malformed")
    terminal_graph_key = tuple(
        int(value) for value in terminal_graph_key_payload
    )
    certified_terminal_point = selected_cubic_graph.voxel_center(
        terminal_graph_key  # type: ignore[arg-type]
    )
    certified_terminal_candidate_points: list[Point] = [
        certified_terminal_point
    ]
    terminal_candidate_limit_m = min(
        float(config.mesh_graph_max_edge_distance_m),
        max(
            float(selected_cubic_graph.cell_diagonal_m),
            float(
                cubic_component_details.get(
                    "terminal_snap_limit_m",
                    config.mesh_graph_max_edge_distance_m,
                )
            ),
        ),
    )
    raw_terminal_candidate_keys = cubic_component_details.get(
        "terminal_graph_key_candidates"
    )
    if (
        isinstance(raw_terminal_candidate_keys, Sequence)
        and not isinstance(raw_terminal_candidate_keys, (str, bytes))
    ):
        for raw_candidate_key in raw_terminal_candidate_keys:
            if (
                not isinstance(raw_candidate_key, Sequence)
                or isinstance(raw_candidate_key, (str, bytes))
                or len(raw_candidate_key) != 3
            ):
                continue
            try:
                candidate_key = tuple(
                    int(value) for value in raw_candidate_key
                )
            except (TypeError, ValueError):
                continue
            if not selected_cubic_graph.contains_key(
                candidate_key  # type: ignore[arg-type]
            ):
                continue
            candidate_point = selected_cubic_graph.voxel_center(
                candidate_key  # type: ignore[arg-type]
            )
            if (
                _horizontal_point_distance_m(
                    points[certified_terminal_hint_index],
                    candidate_point,
                )
                > terminal_candidate_limit_m + 1e-9
            ):
                continue
            if candidate_point not in certified_terminal_candidate_points:
                certified_terminal_candidate_points.append(candidate_point)
    ingress_graph_key_payload = cubic_component_details.get(
        "ingress_graph_key"
    )
    if (
        not isinstance(ingress_graph_key_payload, Sequence)
        or isinstance(ingress_graph_key_payload, (str, bytes))
        or len(ingress_graph_key_payload) != 3
    ):
        raise ValueError("selected cubic ingress key is malformed")
    ingress_graph_key = tuple(int(value) for value in ingress_graph_key_payload)
    certified_ingress_point = selected_cubic_graph.voxel_center(
        ingress_graph_key  # type: ignore[arg-type]
    )
    certified_ingress_candidate_points: list[Point] = [
        certified_ingress_point
    ]
    raw_ingress_candidate_keys = cubic_component_details.get(
        "ingress_graph_key_candidates"
    )
    if (
        isinstance(raw_ingress_candidate_keys, Sequence)
        and not isinstance(raw_ingress_candidate_keys, (str, bytes))
    ):
        source_attachment_limit_m = min(
            float(config.mesh_graph_entry_anchor_radius_m),
            MAX_OBJ_SOURCE_INGRESS_SNAP_DISTANCE_M,
        )
        for raw_candidate_key in raw_ingress_candidate_keys:
            if (
                not isinstance(raw_candidate_key, Sequence)
                or isinstance(raw_candidate_key, (str, bytes))
                or len(raw_candidate_key) != 3
            ):
                continue
            try:
                candidate_key = tuple(
                    int(value) for value in raw_candidate_key
                )
            except (TypeError, ValueError):
                continue
            if not selected_cubic_graph.contains_key(
                candidate_key  # type: ignore[arg-type]
            ):
                continue
            candidate_point = selected_cubic_graph.voxel_center(
                candidate_key  # type: ignore[arg-type]
            )
            if (
                source_ingress_anchor is not None
                and math.dist(source_ingress_anchor, candidate_point)
                > source_attachment_limit_m + 1e-9
            ):
                continue
            if candidate_point not in certified_ingress_candidate_points:
                certified_ingress_candidate_points.append(candidate_point)

    terminal_candidate_count_before_mesh_support = len(
        certified_terminal_candidate_points
    )
    ingress_candidate_count_before_mesh_support = len(
        certified_ingress_candidate_points
    )
    certified_terminal_candidate_points = list(
        _mesh_supported_candidate_points(
            certified_terminal_candidate_points,
            mesh_point_has_opposing_support=(
                mesh_point_has_opposing_support
            ),
            max_distance_m=DEFAULT_MESH_OPPOSING_SUPPORT_DISTANCE_M,
            minimum_clearance_m=float(
                config.mesh_graph_minimum_clearance_m
            ),
        )
    )
    certified_ingress_candidate_points = list(
        _mesh_supported_candidate_points(
            certified_ingress_candidate_points,
            mesh_point_has_opposing_support=(
                mesh_point_has_opposing_support
            ),
            max_distance_m=DEFAULT_MESH_OPPOSING_SUPPORT_DISTANCE_M,
            minimum_clearance_m=float(
                config.mesh_graph_minimum_clearance_m
            ),
        )
    )
    if certified_terminal_candidate_points:
        certified_terminal_point = certified_terminal_candidate_points[0]
        terminal_graph_key = selected_cubic_graph.world_key(
            certified_terminal_point
        )
        cubic_component_details["terminal_graph_key"] = [
            int(value) for value in terminal_graph_key
        ]
    if certified_ingress_candidate_points:
        certified_ingress_point = certified_ingress_candidate_points[0]
        ingress_graph_key = selected_cubic_graph.world_key(
            certified_ingress_point
        )
        cubic_component_details["ingress_graph_key"] = [
            int(value) for value in ingress_graph_key
        ]
    certified_route_points = (
        certified_ingress_point,
        *points[
            certified_ingress_hint_index + 1 : certified_terminal_hint_index
        ],
        certified_terminal_point,
    )
    cubic_component_details.update(
        {
            "requested_terminal_point": _point_payload(
                points[certified_terminal_hint_index]
            ),
            "certified_terminal_point": _point_payload(
                certified_terminal_point
            ),
            "certified_terminal_candidate_count": len(
                certified_terminal_candidate_points
            ),
            "certified_terminal_candidate_count_before_mesh_support": (
                terminal_candidate_count_before_mesh_support
            ),
            "certified_terminal_candidate_policy": (
                "bounded_endpoint_opposing_mesh_support_v1"
                if mesh_point_has_opposing_support is not None
                else "bounded_endpoint_free_voxel_candidates_v2"
            ),
            "certified_ingress_candidate_count": len(
                certified_ingress_candidate_points
            ),
            "certified_ingress_candidate_count_before_mesh_support": (
                ingress_candidate_count_before_mesh_support
            ),
            "opposing_mesh_support_required": bool(
                mesh_point_has_opposing_support is not None
            ),
            "opposing_mesh_support_distance_m": (
                float(DEFAULT_MESH_OPPOSING_SUPPORT_DISTANCE_M)
                if mesh_point_has_opposing_support is not None
                else None
            ),
            "certified_ingress_candidate_policy": (
                "bounded_obj_attachment_opposing_mesh_support_v1"
                if (
                    source_ingress_is_obj_surface_anchor
                    and mesh_point_has_opposing_support is not None
                )
                else (
                    "bounded_obj_attachment_free_voxels_v1"
                    if source_ingress_is_obj_surface_anchor
                    else (
                        "bounded_navigation_start_surface_interval_voxels_v2"
                        if source_ingress_anchor is not None
                        else "selected_component_ingress_v1"
                    )
                )
            ),
            "certified_ingress_point": _point_payload(
                certified_ingress_point
            ),
            "terminal_selection": (
                "ranked_contiguous_route_component_v2"
                if cubic_component_details.get("ingress_selection")
                == "ranked_contiguous_route_component_v2"
                else "bounded_true_3d_free_voxel_snap_v1"
            ),
        }
    )

    def selected_component_probe(
        point: Point,
    ) -> tuple[bool, float] | None:
        try:
            key = selected_cubic_graph.world_key(point)
        except (TypeError, ValueError):
            return None
        if not selected_cubic_graph.contains_key(key):
            return None
        # The packed graph contains only globally occupied-wins free keys that
        # already met this clearance threshold during the complete tile merge.
        # Returning the conservative threshold avoids recomputing an expensive
        # local surface-distance search for every cache-time A* sample. Exact
        # cached-mesh checks still gate every edge, and the certificate probes
        # the persisted voxel chunks again before runtime authorization.
        return (
            True,
            float(config.mesh_graph_minimum_clearance_m),
        )

    component_cell_set: set[FootprintCell] = set()
    for key in selected_cubic_graph.iter_keys():
        center = selected_cubic_graph.voxel_center(key)
        component_cell_set.add(
            (
                math.floor(center[0] / cell_size),
                math.floor(center[2] / cell_size),
            )
        )
    component_cell_set.intersection_update(requested_component_cell_set)
    component_cells = tuple(sorted(component_cell_set))
    excluded_component_cell_set = (
        requested_component_cell_set - component_cell_set
    )
    missing_cells_after_repair = tuple(sorted(excluded_component_cell_set))
    repair_attempted = 0
    repair_built = 0
    progress_distances = _component_progress_distances(
        component_cell_set,
        route,
        cell_size=cell_size,
    )
    cell_accumulators: dict[FootprintCell, list[float]] = {}
    true_3d_accumulator: dict[VoxelGraphKey, list[float]] = {}
    true_3d_base_grid_size = _cache_graph_base_grid_size(
        selected_cubic_graph.free_voxel_count,
        base_voxel_size=true_3d_base_voxel_size,
        base_vertical_voxel_size=true_3d_base_vertical_voxel_size,
        max_nodes=config.graph_max_nodes,
    )
    clearance_sum = 0.0
    clearance_min = math.inf
    clearance_count = 0
    for key in selected_cubic_graph.iter_keys():
        center = selected_cubic_graph.voxel_center(key)
        cell = (
            math.floor(center[0] / cell_size),
            math.floor(center[2] / cell_size),
        )
        if cell not in component_cell_set:
            continue
        probe = selected_component_probe(center)
        if probe is None or not bool(probe[0]):
            continue
        clearance_m = max(0.0, float(probe[1]))
        clearance_sum += clearance_m
        clearance_min = min(clearance_min, clearance_m)
        clearance_count += 1
        accumulator = cell_accumulators.setdefault(
            cell,
            [0.0, 0.0, float("inf"), 0.0, 0.0],
        )
        accumulator[0] += 1.0
        accumulator[1] += clearance_m
        accumulator[2] = min(accumulator[2], clearance_m)
        voxel_volume_m3 = float(
            config.voxel_size_m
            * config.vertical_voxel_size_m
            * config.voxel_size_m
        )
        accumulator[3] += voxel_volume_m3
        accumulator[4] += float(center[1])
        accumulate_navigation_voxel_3d_sample(
            true_3d_accumulator,
            center,
            grid_size_m=true_3d_base_grid_size,
            clearance_m=clearance_m,
            volume_m3=voxel_volume_m3,
            progress_m=float(progress_distances.get(cell, 0.0)),
        )

    cell_metrics = {
        cell: NavigationVoxelCellMetric(
            available_volume_m3=float(accumulator[3]),
            free_cell_count=int(accumulator[0]),
            min_clearance_m=float(accumulator[2]),
            mean_clearance_m=float(accumulator[1] / max(1.0, accumulator[0])),
            progress_m=float(progress_distances.get(cell, 0.0)),
            center_y_m=float(accumulator[4] / max(1.0, accumulator[0])),
        )
        for cell, accumulator in cell_accumulators.items()
        if accumulator[0] > 0.0
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
    # V12 persists this explicit graph for compatibility diagnostics only;
    # production motion follows ``prepared_mesh_graph`` below.  Restrict the
    # compatibility graph to immediate cubic neighbors so cache generation
    # does not spend quadratic-ish work discovering longer LOS shortcuts that
    # can never become routing authority.
    compatibility_graph_edge_distance_cells = 1
    prepared_3d_graph = build_navigation_voxel_3d_graph(
        true_3d_metrics,
        grid_size_m=true_3d_grid_size,
        max_edge_distance_cells=compatibility_graph_edge_distance_cells,
        max_edges_per_node=config.graph_max_edges_per_node,
        max_total_edges=config.graph_max_edges,
        unknown_boundary=true_3d_unknown_boundary,
    )
    if bool(config.mesh_graph_enabled) and mesh_edge_is_clear is not None:
        mesh_config = config.mesh_navigation_graph_config()
        direct_entry_keys = tuple(
            dict.fromkeys(
                selected_cubic_graph.world_key(point)
                for point in certified_ingress_candidate_points
                if selected_cubic_graph.contains_key(
                    selected_cubic_graph.world_key(point)
                )
            )
        )
        connector_required = bool(
            source_ingress_anchor is not None
            and route.get("starts_at_navigation_start") is True
        )
        if connector_required:
            connector_safe_entry_keys: list[CubicVoxelKey] = []
            for entry_key in direct_entry_keys:
                entry_point = selected_cubic_graph.voxel_center(entry_key)
                if (
                    source_ingress_anchor is None
                    or math.dist(source_ingress_anchor, entry_point)
                    > min(
                        float(config.mesh_graph_entry_anchor_radius_m),
                        MAX_OBJ_SOURCE_INGRESS_SNAP_DISTANCE_M,
                    )
                    + 1e-9
                ):
                    continue
                try:
                    connector_clear = bool(
                        mesh_edge_is_clear(source_ingress_anchor, entry_point)
                    )
                except Exception:
                    connector_clear = False
                if connector_clear:
                    connector_safe_entry_keys.append(entry_key)
            direct_entry_keys = tuple(connector_safe_entry_keys)
        direct_terminal_keys = tuple(
            dict.fromkeys(
                selected_cubic_graph.world_key(point)
                for point in certified_terminal_candidate_points
                if selected_cubic_graph.contains_key(
                    selected_cubic_graph.world_key(point)
                )
            )
        )
        selected_route_cells = route_cells
        selected_route_gap_key_groups = (
            selected_surface_gap_route_key_groups
        )
        expected_intermediate_gate_count = max(
            0,
            len(selected_route_cells) - 2,
        )
        direct_waypoint_groups = tuple(
            tuple(group) for group in selected_route_gap_key_groups[1:-1]
        )
        direct_waypoint_point_groups = tuple(
            tuple(
                selected_cubic_graph.voxel_center(key)
                for key in group
            )
            for group in direct_waypoint_groups
        )
        ordered_route_guide_points = (
            (
                certified_ingress_point,
                *(
                    selected_cubic_graph.voxel_center(group[0])
                    for group in direct_waypoint_groups
                    if group
                ),
                certified_terminal_point,
            )
            if len(selected_route_cells) >= 2
            else (certified_ingress_point, certified_terminal_point)
        )
        opposing_support_failure = None
        if (
            mesh_point_has_opposing_support is not None
            and not certified_ingress_candidate_points
        ):
            opposing_support_failure = (
                "exact_cubic_spine_ingress_opposing_mesh_support_missing"
            )
        elif (
            mesh_point_has_opposing_support is not None
            and not certified_terminal_candidate_points
        ):
            opposing_support_failure = (
                "exact_cubic_spine_terminal_opposing_mesh_support_missing"
            )
        if opposing_support_failure is not None:
            direct_waypoint_point_groups = None
            direct_mesh_build = MeshNavigationGraphBuildResult(
                graph=None,
                details={
                    "method": MESH_NAVIGATION_GRAPH_METHOD,
                    "reason": opposing_support_failure,
                    "known_terminal_reached": False,
                    "node_limit_reached": False,
                    "opposing_mesh_support_required": True,
                    "opposing_mesh_support_distance_m": float(
                        DEFAULT_MESH_OPPOSING_SUPPORT_DISTANCE_M
                    ),
                },
            )
        elif (
            len(selected_route_gap_key_groups) != len(selected_route_cells)
            or any(not group for group in selected_route_gap_key_groups)
            or len(direct_waypoint_groups)
            != expected_intermediate_gate_count
            or len(ordered_route_guide_points)
            != expected_intermediate_gate_count + 2
        ):
            direct_mesh_build = MeshNavigationGraphBuildResult(
                graph=None,
                details={
                    "method": MESH_NAVIGATION_GRAPH_METHOD,
                    "reason": (
                        "exact_cubic_spine_surface_gap_component_gates_missing"
                    ),
                    "known_terminal_reached": False,
                    "node_limit_reached": False,
                    "surface_gap_waypoints_required": True,
                    "surface_gap_gate_source": (
                        "bounded_surface_intervals_v1"
                    ),
                },
            )
        else:
            direct_mesh_build = build_exact_cubic_spine_navigation_path_graph(
                selected_cubic_graph,
                ordered_route_guide_points,
                start_keys=direct_entry_keys,
                terminal_keys=direct_terminal_keys,
                waypoint_key_groups=direct_waypoint_groups,
                footprint_cell_size_m=cell_size,
                point_probe=selected_component_probe,
                edge_is_clear=mesh_edge_is_clear,
                horizontal_gate_radius_m=min(
                    float(selected_corridor_radius_m),
                    float(MIN_CACHE_VOXEL_ROUTE_CORRIDOR_RADIUS_M),
                ),
                require_waypoint_key_groups=True,
                config=mesh_config,
            )
        mesh_build = direct_mesh_build
        direct_reason = str(direct_mesh_build.details.get("reason", ""))
        direct_capacity_limited = bool(
            direct_mesh_build.details.get("node_limit_reached", False)
            or "limit_reached" in direct_reason
            or "capacity" in direct_reason
        )
        if (
            direct_mesh_build.graph is None
            and not direct_capacity_limited
            and direct_waypoint_point_groups is not None
        ):
            direct_used_nodes = max(
                0,
                int(
                    direct_mesh_build.details.get(
                        "expanded_node_count",
                        direct_mesh_build.details.get(
                            "expanded_voxel_count",
                            0,
                        ),
                    )
                ),
            )
            remaining_fine_nodes = max(
                0,
                int(mesh_config.max_nodes) - direct_used_nodes,
            )
            fine_config = MeshNavigationGraphConfig(
                horizontal_sample_spacing_m=min(
                    float(config.fine_voxel_size_m),
                    float(DEFAULT_MESH_GRAPH_FINE_RETRY_SPACING_M),
                ),
                vertical_sample_spacing_m=min(
                    float(config.fine_vertical_voxel_size_m),
                    0.25,
                ),
                minimum_clearance_m=mesh_config.minimum_clearance_m,
                max_nodes=max(2, remaining_fine_nodes),
                max_edges_per_node=mesh_config.max_edges_per_node,
                max_edge_candidates_per_node=(
                    mesh_config.max_edge_candidates_per_node
                ),
                max_edge_candidates_per_direction=(
                    mesh_config.max_edge_candidates_per_direction
                ),
                max_edge_distance_m=mesh_config.max_edge_distance_m,
                max_vertical_edge_distance_m=(
                    mesh_config.max_vertical_edge_distance_m
                ),
                max_interval_points_per_column=(
                    mesh_config.max_interval_points_per_column
                ),
                ray_merge_epsilon_m=mesh_config.ray_merge_epsilon_m,
            ).validated()
            fine_attempts: list[dict[str, object]] = []
            fine_build: MeshNavigationGraphBuildResult | None = None
            fine_radii = tuple(
                dict.fromkeys(
                    (
                        float(MIN_CACHE_VOXEL_ROUTE_CORRIDOR_RADIUS_M),
                        float(MAX_MESH_GRAPH_FINE_RETRY_TUBE_RADIUS_M),
                    )
                )
            )
            for fine_radius_m in fine_radii:
                if remaining_fine_nodes < 2:
                    break
                fine_build = _build_route_ordered_fine_mesh_navigation_path(
                    ordered_route_guide_points,
                    waypoint_point_groups=direct_waypoint_point_groups,
                    entry_candidate_points=tuple(
                        selected_cubic_graph.voxel_center(key)
                        for key in direct_entry_keys
                    ),
                    terminal_candidate_points=tuple(
                        selected_cubic_graph.voxel_center(key)
                        for key in direct_terminal_keys
                    ),
                    footprint_cell_size_m=cell_size,
                    component_cells=component_cell_set,
                    point_probe=selected_component_probe,
                    edge_is_clear=mesh_edge_is_clear,
                    config=fine_config,
                    route_tube_radius_m=fine_radius_m,
                    max_total_nodes=remaining_fine_nodes,
                )
                remaining_fine_nodes = max(
                    0,
                    int(
                        fine_build.details.get(
                            "remaining_search_nodes",
                            remaining_fine_nodes,
                        )
                    ),
                )
                fine_attempts.append(
                    {
                        "route_tube_radius_m": float(fine_radius_m),
                        "built": fine_build.graph is not None,
                        "reason": str(fine_build.details.get("reason", "")),
                        "node_limit_reached": bool(
                            fine_build.details.get("node_limit_reached", False)
                        ),
                        "remaining_search_nodes": int(
                            remaining_fine_nodes
                        ),
                    }
                )
                if fine_build.graph is not None or bool(
                    fine_build.details.get("node_limit_reached", False)
                ):
                    break
            pending_fine_radius = len(fine_attempts) < len(fine_radii)
            if (
                remaining_fine_nodes < 2
                and pending_fine_radius
                and (
                    fine_build is None
                    or fine_build.graph is None
                    and fine_build.details.get("node_limit_reached") is not True
                )
            ):
                fine_build = MeshNavigationGraphBuildResult(
                    graph=None,
                    details={
                        **(
                            {}
                            if fine_build is None
                            else dict(fine_build.details)
                        ),
                        "method": MESH_NAVIGATION_GRAPH_METHOD,
                        "reason": "route_ordered_fine_mesh_node_limit_reached",
                        "known_terminal_reached": False,
                        "node_limit_reached": True,
                        "remaining_search_nodes": int(
                            remaining_fine_nodes
                        ),
                    },
                )
                fine_attempts.append(
                    {
                        "route_tube_radius_m": float(
                            fine_radii[len(fine_attempts)]
                        ),
                        "built": False,
                        "reason": (
                            "route_ordered_fine_mesh_node_limit_reached"
                        ),
                        "node_limit_reached": True,
                        "remaining_search_nodes": int(
                            remaining_fine_nodes
                        ),
                        "attempted": False,
                    }
                )
            if fine_build is not None:
                mesh_build = MeshNavigationGraphBuildResult(
                    graph=fine_build.graph,
                    details={
                        **dict(fine_build.details),
                        "exact_cubic_spine_attempt": dict(
                            direct_mesh_build.details
                        ),
                        "route_ordered_fine_attempts": list(fine_attempts),
                    },
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
    mesh_graph_details.update(
        {
            "surface_gap_waypoints_required": True,
            "surface_gap_gate_source": cubic_component_details.get(
                "surface_gap_gate_source"
            ),
            "surface_gap_route_cell_count": len(route_cells),
            "surface_gap_route_cells": list(
                cubic_component_details.get(
                    "surface_gap_route_cells",
                    (),
                )
            ),
            "surface_gap_selected_route_intervals": list(
                cubic_component_details.get(
                    "surface_gap_selected_route_intervals",
                    (),
                )
            ),
            "surface_gap_transition_fallback_indices": list(
                cubic_component_details.get(
                    "surface_gap_transition_fallback_indices",
                    (),
                )
            ),
            "requested_terminal_point": _point_payload(points[-1]),
            "terminal_snap_limit_m": float(
                cubic_component_details.get(
                    "terminal_snap_limit_m",
                    config.mesh_graph_max_edge_distance_m,
                )
            ),
        }
    )
    if source_ingress_anchor is not None:
        source_snap_limit_m = min(
            float(config.mesh_graph_entry_anchor_radius_m),
            MAX_OBJ_SOURCE_INGRESS_SNAP_DISTANCE_M,
        )
        connector_required = bool(
            route.get("starts_at_navigation_start") is True
        )
        mesh_graph_details.update(
            {
                "source_ingress_required": True,
                "source_ingress_point": _point_payload(
                    source_ingress_anchor
                ),
                "source_ingress_coordinate_space": "xyz",
                "source_ingress_snap_limit_m": source_snap_limit_m,
                "source_ingress_connector_required": connector_required,
                "source_ingress_attachment_mode": (
                    "executable_authored_start_connector"
                    if connector_required
                    else "non_executable_obj_surface_anchor_snap"
                ),
            }
        )
        strict_route_points = _prepared_mesh_graph_route_points(
            prepared_mesh_graph,
            mesh_graph_details,
        )
        if strict_route_points:
            actual_distance_m = math.dist(
                source_ingress_anchor,
                strict_route_points[0],
            )
            if connector_required:
                try:
                    connector_mesh_clear = bool(
                        mesh_edge_is_clear is not None
                        and mesh_edge_is_clear(
                            source_ingress_anchor,
                            strict_route_points[0],
                        )
                    )
                except Exception:
                    connector_mesh_clear = False
            else:
                connector_mesh_clear = None
            mesh_graph_details.update(
                {
                    "source_ingress_attachment_point": _point_payload(
                        strict_route_points[0]
                    ),
                    "source_ingress_attachment_distance_m": float(
                        actual_distance_m
                    ),
                    "source_ingress_connector_mesh_clear": (
                        None
                        if connector_mesh_clear is None
                        else bool(connector_mesh_clear)
                    ),
                }
            )
            if actual_distance_m > source_snap_limit_m + 1e-9:
                prepared_mesh_graph = None
                mesh_graph_details.update(
                    {
                        "reason": "source_ingress_attachment_too_far",
                        "known_terminal_reached": False,
                    }
                )
            elif connector_required and connector_mesh_clear is not True:
                prepared_mesh_graph = None
                mesh_graph_details.update(
                    {
                        "reason": "source_ingress_connector_mesh_blocked",
                        "known_terminal_reached": False,
                    }
                )
    prepared_mesh_route_points = _prepared_mesh_graph_route_points(
        prepared_mesh_graph,
        mesh_graph_details,
    )
    published_route_points = (
        prepared_mesh_route_points or certified_route_points
    )
    fine_tiles: tuple[LocalVoxelVolume, ...] = ()
    fine_seed_points: tuple[Point, ...] = ()
    fine_seed_details = {
        "fine_route_seed_method": "v12_fixed_orthogonal_tiles",
        "fine_graph_spine_available": prepared_mesh_graph is not None,
        "fine_graph_spine_coverage_complete": prepared_mesh_graph is not None,
    }
    fine_tile_coverage = {
        "fine_built_tile_seed_coverage_complete": True,
        "fine_built_tile_uncovered_seed_count": 0,
        "fine_built_tile_uncovered_seed_examples": [],
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
        fixed_isotropic_voxel_size_m=float(config.voxel_size_m),
        fixed_vertical_voxel_size_m=float(config.vertical_voxel_size_m),
        surface_overlap_occupied_wins=True,
        fine_tiles=fine_tiles,
    )
    metrics: dict[str, float | int | bool] = {
        "seed_count": sum(
            1
            for point in certified_route_points
            if selected_cubic_graph.contains_key(
                selected_cubic_graph.world_key(point)
            )
        ),
        "free_cell_count": int(clearance_count),
        "available_volume_m3": float(
            clearance_count
            * config.voxel_size_m
            * config.vertical_voxel_size_m
            * config.voxel_size_m
        ),
        "surface_fraction": float(
            sum(len(tile.surface_cells) for tile in tiles)
            / max(1, sum(tile.voxel_count for tile in tiles))
        ),
        "min_clearance_m": float(
            0.0 if clearance_count <= 0 else clearance_min
        ),
        "mean_clearance_m": float(
            clearance_sum / max(1, clearance_count)
        ),
        "clearance_sample_count": int(clearance_count),
        "flood_fill_truncated": False,
    }
    details = {
        "bounds_min": _point_payload(atlas.bounds_min),
        "bounds_max": _point_payload(atlas.bounds_max),
        "tile_size_m": float(tile_size),
        "tile_count": len(tiles),
        "fine_tile_count": len(fine_tiles),
        "fixed_voxel_build": dict(fixed_build.details),
        "fixed_voxel_method": FIXED_ORTHOGONAL_VOXEL_METHOD,
        "fixed_isotropic_voxel_size_m": float(config.voxel_size_m),
        "fixed_vertical_voxel_size_m": float(config.vertical_voxel_size_m),
        "fixed_voxel_cell_size_m": [
            float(config.voxel_size_m),
            float(config.vertical_voxel_size_m),
            float(config.voxel_size_m),
        ],
        "selected_route_corridor_radius_m": float(
            selected_corridor_radius_m
        ),
        "surface_overlap_policy": "occupied_wins",
        "sampling_complete": True,
        "cubic_graph": dict(cubic_graph_details),
        "cubic_component": cubic_component_details,
        "cubic_component_probe_method": (
            "packed_free_key_minimum_clearance_v1"
        ),
        "cubic_component_max_voxels": int(
            config.cubic_component_max_voxels
        ),
        "cubic_graph_method": CUBIC_VOXEL_GRAPH_METHOD,
        "cubic_component_voxel_count": int(
            selected_cubic_graph.free_voxel_count
        ),
        "source_route_point_count": len(points),
        "source_route_cell_count": len(route_cells),
        "source_route_cells": [
            int(value) for cell in route_cells for value in cell
        ],
        "source_route_points": [
            float(value) for point in points for value in point
        ],
        "source_route_start_point": _point_payload(points[0]),
        "source_route_terminal_point": _point_payload(points[-1]),
        "source_route_footprint_cell_size_m": float(cell_size),
        "point_count": len(published_route_points),
        "certified_ingress_hint_index": int(
            certified_ingress_hint_index
        ),
        "certified_terminal_hint_index": int(
            certified_terminal_hint_index
        ),
        "certified_route_length_m": float(
            sum(
                math.dist(first, second)
                for first, second in zip(
                    published_route_points,
                    published_route_points[1:],
                    strict=False,
                )
            )
        ),
        "certified_route_source": (
            "prepared_mesh_graph_path_v1"
            if prepared_mesh_route_points
            else "terminal_component_metadata_suffix_v1"
        ),
        "_certified_route_points": published_route_points,
        **fine_seed_details,
        **fine_tile_coverage,
        "fine_route_seed_count": len(fine_seed_points),
        "fine_route_seed_spacing_m": 0.0,
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
        "prepared_3d_graph_role": "compatibility_immediate_neighbors_only",
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
        "fine_sampling_truncated": False,
    }
    return atlas, metrics, details


def _build_route_ordered_fine_mesh_navigation_path(
    route_points: Sequence[Point],
    *,
    waypoint_point_groups: Sequence[Sequence[Point]],
    entry_candidate_points: Sequence[Point],
    terminal_candidate_points: Sequence[Point],
    footprint_cell_size_m: float,
    component_cells: set[FootprintCell],
    point_probe: Callable[[Point], tuple[bool, float] | None],
    edge_is_clear: MeshEdgeSafetyCheck,
    config: MeshNavigationGraphConfig,
    route_tube_radius_m: float,
    max_total_nodes: int,
) -> MeshNavigationGraphBuildResult:
    """Build a finer exact path through every ordered surface-gap gate."""
    points = tuple(route_points)
    entries = tuple(dict.fromkeys(entry_candidate_points))
    raw_terminals = tuple(dict.fromkeys(terminal_candidate_points))
    raw_groups = tuple(
        tuple(dict.fromkeys(group)) for group in waypoint_point_groups
    )
    expected_intermediate_gate_count = max(0, len(points) - 2)
    base_details: dict[str, object] = {
        "method": MESH_NAVIGATION_GRAPH_METHOD,
        "search_method": "route_ordered_segmented_fine_mesh_v1",
        "route_guide_point_count": len(points),
        "intermediate_gate_count": len(raw_groups),
        "expected_intermediate_gate_count": int(
            expected_intermediate_gate_count
        ),
        "intermediate_gate_source": "surface_gap_free_voxel_candidates",
        "raw_route_y_used": False,
        "route_tube_radius_m": float(route_tube_radius_m),
        "search_node_limit": max(1, int(max_total_nodes)),
    }
    if (
        len(points) < 2
        or not entries
        or not raw_terminals
        or len(raw_groups) != expected_intermediate_gate_count
        or any(not group for group in raw_groups)
    ):
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                **base_details,
                "reason": "route_ordered_fine_mesh_inputs_missing",
                "known_terminal_reached": False,
                "node_limit_reached": False,
            },
        )
    spacing = (
        float(config.horizontal_sample_spacing_m),
        float(config.vertical_sample_spacing_m),
        float(config.horizontal_sample_spacing_m),
    )

    def lattice_key(point: Sequence[float]) -> VoxelGraphKey:
        return tuple(  # type: ignore[return-value]
            int(math.floor(float(point[axis]) / spacing[axis]))
            for axis in range(3)
        )

    def lattice_center(key: Sequence[int]) -> Point:
        return tuple(  # type: ignore[return-value]
            (float(key[axis]) + 0.5) * spacing[axis]
            for axis in range(3)
        )

    route_tube_contains = _horizontal_route_tube_point_filter(
        points,
        radius_m=float(route_tube_radius_m),
        voxel_size_m=float(config.horizontal_sample_spacing_m),
    )

    def exact_lattice_targets(values: Sequence[Point]) -> tuple[Point, ...]:
        targets: list[Point] = []
        for value in values:
            try:
                target = lattice_center(lattice_key(value))
            except (IndexError, TypeError, ValueError, OverflowError):
                continue
            footprint_cell = (
                int(math.floor(target[0] / footprint_cell_size_m)),
                int(math.floor(target[2] / footprint_cell_size_m)),
            )
            if (
                footprint_cell not in component_cells
                or not route_tube_contains(target)
            ):
                continue
            if target not in targets:
                targets.append(target)
        return tuple(targets)

    groups = tuple(exact_lattice_targets(group) for group in raw_groups)
    terminals = exact_lattice_targets(raw_terminals)
    if not terminals or any(not group for group in groups):
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                **base_details,
                "reason": "route_ordered_fine_mesh_inputs_missing",
                "known_terminal_reached": False,
                "node_limit_reached": False,
                "exact_lattice_gate_count": sum(bool(group) for group in groups),
            },
        )
    remaining_nodes = max(1, int(max_total_nodes))
    retained_points: list[Point] = []
    retained_keys: set[VoxelGraphKey] = set()
    reached_gate_count = 0
    leg_details: list[dict[str, object]] = []
    current_entries = entries
    target_groups = (*groups, terminals)
    final_leg_details: Mapping[str, object] = {}
    for gate_index, target_group in enumerate(target_groups, start=1):
        if remaining_nodes < 2:
            return MeshNavigationGraphBuildResult(
                graph=None,
                details={
                    **base_details,
                    "reason": "route_ordered_fine_mesh_node_limit_reached",
                    "known_terminal_reached": False,
                    "node_limit_reached": True,
                    "reached_intermediate_gate_count": int(reached_gate_count),
                    "remaining_search_nodes": int(remaining_nodes),
                    "leg_attempts": list(leg_details),
                },
            )
        current_key = (
            lattice_key(current_entries[0])
            if len(current_entries) == 1
            else None
        )
        blocked_keys = (
            retained_keys - ({current_key} if current_key is not None else set())
        )

        def gated_point_probe(
            point: Point,
            *,
            blocked: set[VoxelGraphKey] = blocked_keys,
        ) -> tuple[bool, float] | None:
            if not route_tube_contains(point):
                return None
            if lattice_key(point) in blocked:
                return None
            return point_probe(point)

        leg_config = MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=config.horizontal_sample_spacing_m,
            vertical_sample_spacing_m=config.vertical_sample_spacing_m,
            minimum_clearance_m=config.minimum_clearance_m,
            max_nodes=remaining_nodes,
            max_edges_per_node=config.max_edges_per_node,
            max_edge_candidates_per_node=config.max_edge_candidates_per_node,
            max_edge_candidates_per_direction=(
                config.max_edge_candidates_per_direction
            ),
            max_edge_distance_m=config.max_edge_distance_m,
            max_vertical_edge_distance_m=(
                config.max_vertical_edge_distance_m
            ),
            max_interval_points_per_column=(
                config.max_interval_points_per_column
            ),
            ray_merge_epsilon_m=config.ray_merge_epsilon_m,
        ).validated()
        leg = build_goal_directed_seeded_mesh_navigation_path_graph(
            current_entries,
            footprint_cell_size_m=footprint_cell_size_m,
            component_cells=component_cells,
            point_probe=gated_point_probe,
            edge_is_clear=edge_is_clear,
            terminal_point=target_group[0],
            terminal_candidate_points=target_group[1:],
            route_guide_points=(current_entries[0], target_group[0]),
            require_exact_terminal_point=True,
            config=leg_config,
        )
        final_leg_details = dict(leg.details)
        used_nodes = max(
            1,
            int(leg.details.get("discovered_node_count", 0)),
            int(leg.details.get("expanded_node_count", 0)),
        )
        remaining_nodes = max(0, remaining_nodes - used_nodes)
        leg_details.append(
            {
                "gate_index": int(gate_index),
                "built": leg.graph is not None,
                "reason": str(leg.details.get("reason", "")),
                "used_node_budget": int(used_nodes),
                "remaining_search_nodes": int(remaining_nodes),
                "node_limit_reached": bool(
                    leg.details.get("node_limit_reached", False)
                ),
            }
        )
        if leg.graph is None:
            return MeshNavigationGraphBuildResult(
                graph=None,
                details={
                    **base_details,
                    "reason": (
                        "route_ordered_fine_mesh_node_limit_reached"
                        if leg.details.get("node_limit_reached") is True
                        else "route_ordered_fine_mesh_gate_unreachable"
                    ),
                    "known_terminal_reached": False,
                    "node_limit_reached": bool(
                        leg.details.get("node_limit_reached", False)
                    ),
                    "failed_gate_index": int(gate_index),
                    "reached_intermediate_gate_count": int(reached_gate_count),
                    "remaining_search_nodes": int(remaining_nodes),
                    "leg_attempts": list(leg_details),
                },
            )
        leg_points = _prepared_mesh_graph_route_points(
            leg.graph,
            leg.details,
        )
        if len(leg_points) < 2:
            return MeshNavigationGraphBuildResult(
                graph=None,
                details={
                    **base_details,
                    "reason": "route_ordered_fine_mesh_leg_path_missing",
                    "known_terminal_reached": False,
                    "node_limit_reached": False,
                    "failed_gate_index": int(gate_index),
                    "remaining_search_nodes": int(remaining_nodes),
                    "leg_attempts": list(leg_details),
                },
            )
        append_points = leg_points if not retained_points else leg_points[1:]
        for point in append_points:
            key = lattice_key(point)
            if key in retained_keys:
                return MeshNavigationGraphBuildResult(
                    graph=None,
                    details={
                        **base_details,
                        "reason": "route_ordered_fine_mesh_revisit_detected",
                        "known_terminal_reached": False,
                        "node_limit_reached": False,
                        "failed_gate_index": int(gate_index),
                        "remaining_search_nodes": int(remaining_nodes),
                        "leg_attempts": list(leg_details),
                    },
                )
            retained_keys.add(key)
            retained_points.append(point)
        current_entries = (retained_points[-1],)
        if gate_index <= len(groups):
            reached_gate_count += 1
    validated = build_validated_mesh_path_graph(
        retained_points,
        lattice_spacing_m=spacing,
        footprint_cell_size_m=footprint_cell_size_m,
        point_probe=point_probe,
        edge_is_clear=edge_is_clear,
        minimum_clearance_m=config.minimum_clearance_m,
    )
    if validated.graph is None:
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                **base_details,
                "reason": str(validated.details.get("reason", "")),
                "known_terminal_reached": False,
                "node_limit_reached": False,
                "reached_intermediate_gate_count": int(reached_gate_count),
                "remaining_search_nodes": int(remaining_nodes),
                "leg_attempts": list(leg_details),
            },
        )
    return MeshNavigationGraphBuildResult(
        graph=validated.graph,
        details={
            **dict(validated.details),
            **base_details,
            "reason": "route_ordered_fine_mesh_terminal_path_built",
            "known_terminal_reached": True,
            "node_limit_reached": False,
            "reached_intermediate_gate_count": int(reached_gate_count),
            "exact_lattice_gate_count": len(groups),
            "remaining_search_nodes": int(remaining_nodes),
            "selected_terminal_hint_index": int(
                final_leg_details.get("selected_terminal_hint_index", 0)
            ),
            "selected_terminal_hint_point": final_leg_details.get(
                "selected_terminal_hint_point"
            ),
            "terminal_hint_count": len(terminals),
            "leg_attempts": list(leg_details),
        },
    )


def _build_adaptive_seeded_mesh_navigation_path(
    route_points: Sequence[Point],
    *,
    footprint_cell_size_m: float,
    component_cells: set[FootprintCell],
    point_probe: Callable[[Point], tuple[bool, float] | None],
    edge_is_clear: MeshEdgeSafetyCheck,
    coarse_config: MeshNavigationGraphConfig,
    fine_spacing_m: float,
    terminal_candidate_points: Sequence[Point] = (),
    entry_candidate_points: Sequence[Point] = (),
    strict_ingress: bool = False,
) -> MeshNavigationGraphBuildResult:
    """Build one fixed path, refining only at a genuinely finer spacing.

    Intermediate metadata points are breadcrumbs, not known cave terminals.
    A coarse goal-directed search is accepted only when it reaches the final
    route hint. Otherwise a fine search gets a bounded 4 m horizontal route
    envelope, widening
    once to 8 m only when the first search exhausts without reaching its node
    cap. Failure publishes no mesh graph instead of mislabeling a short
    disconnected prefix as a successful terminal route.
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
    entry_seed_points = (
        tuple(dict.fromkeys(entry_candidate_points))
        if strict_ingress and entry_candidate_points
        else points[
            : (
                1
                if strict_ingress
                else min(
                    DEFAULT_MESH_GRAPH_ENTRY_SEED_POINTS,
                    len(points) - 1,
                )
            )
        ]
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
        entry_seed_points,
        footprint_cell_size_m=footprint_cell_size_m,
        component_cells=corridor_cells,
        point_probe=point_probe,
        edge_is_clear=edge_is_clear,
        terminal_point=points[-1],
        terminal_candidate_points=terminal_candidate_points,
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
                "adaptive_coarse_vertical_spacing_m": float(
                    coarse_goal_config.vertical_sample_spacing_m
                ),
            },
        )

    fine_spacing = min(
        float(coarse_config.horizontal_sample_spacing_m),
        max(0.25, float(fine_spacing_m)),
    )
    fine_vertical_spacing = min(
        float(coarse_config.vertical_sample_spacing_m),
        0.25,
    )
    if (
        math.isclose(
            fine_spacing,
            float(coarse_config.horizontal_sample_spacing_m),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        and math.isclose(
            fine_vertical_spacing,
            float(coarse_config.vertical_sample_spacing_m),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ):
        return MeshNavigationGraphBuildResult(
            graph=None,
            details={
                **coarse_details,
                "reason": "adaptive_mesh_known_terminal_unreachable",
                "adaptive_reason": str(coarse_details.get("reason", "")),
                "adaptive_retry_used": False,
                "known_terminal_reached": False,
                "adaptive_corridor_cell_count": len(corridor_cells),
                "adaptive_coarse_spacing_m": float(
                    coarse_goal_config.horizontal_sample_spacing_m
                ),
                "adaptive_coarse_vertical_spacing_m": float(
                    coarse_goal_config.vertical_sample_spacing_m
                ),
                "adaptive_fine_spacing_m": float(fine_spacing),
                "adaptive_fine_vertical_spacing_m": float(
                    fine_vertical_spacing
                ),
            },
        )
    fine_config = MeshNavigationGraphConfig(
        horizontal_sample_spacing_m=fine_spacing,
        vertical_sample_spacing_m=fine_vertical_spacing,
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
    minimum_fine_tube_radius_m = float(
        MIN_CACHE_VOXEL_ROUTE_CORRIDOR_RADIUS_M
    )
    maximum_fine_tube_radius_m = max(
        minimum_fine_tube_radius_m,
        float(MAX_MESH_GRAPH_FINE_RETRY_TUBE_RADIUS_M),
    )
    fine_tube_radii = tuple(
        dict.fromkeys(
            (
                minimum_fine_tube_radius_m,
                maximum_fine_tube_radius_m,
            )
        )
    )
    fine_attempts: list[dict[str, object]] = []
    fine: MeshNavigationGraphBuildResult | None = None
    fine_route_tube_radius_m = minimum_fine_tube_radius_m
    for candidate_tube_radius_m in fine_tube_radii:
        fine_route_tube_radius_m = float(candidate_tube_radius_m)
        fine_route_tube_contains = _horizontal_route_tube_point_filter(
            points,
            radius_m=fine_route_tube_radius_m,
            voxel_size_m=float(
                coarse_goal_config.horizontal_sample_spacing_m
            ),
        )

        def fine_point_probe(
            point: Point,
            *,
            contains: Callable[[Point], bool] = fine_route_tube_contains,
        ) -> tuple[bool, float] | None:
            if not contains(point):
                return None
            return point_probe(point)

        fine = build_goal_directed_seeded_mesh_navigation_path_graph(
            entry_seed_points,
            footprint_cell_size_m=footprint_cell_size_m,
            component_cells=corridor_cells,
            point_probe=fine_point_probe,
            edge_is_clear=edge_is_clear,
            terminal_point=points[-1],
            terminal_candidate_points=terminal_candidate_points,
            route_guide_points=points,
            config=fine_config,
        )
        fine_attempts.append(
            {
                "route_tube_radius_m": float(fine_route_tube_radius_m),
                "reason": str(fine.details.get("reason", "")),
                "built": fine.graph is not None,
                "discovered_node_count": int(
                    fine.details.get("discovered_node_count", 0)
                ),
                "expanded_node_count": int(
                    fine.details.get("expanded_node_count", 0)
                ),
                "maximum_route_guide_index_seen": int(
                    fine.details.get("maximum_route_guide_index_seen", -1)
                ),
                "node_limit_reached": bool(
                    fine.details.get("node_limit_reached", False)
                ),
            }
        )
        if fine.graph is not None or bool(
            fine.details.get("node_limit_reached", False)
        ):
            break
    assert fine is not None
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
        "adaptive_coarse_vertical_spacing_m": float(
            coarse_goal_config.vertical_sample_spacing_m
        ),
        "adaptive_fine_spacing_m": float(fine_spacing),
        "adaptive_fine_vertical_spacing_m": float(
            fine_vertical_spacing
        ),
        "adaptive_fine_route_tube_radius_m": float(
            fine_route_tube_radius_m
        ),
        "adaptive_fine_route_tube_attempts": list(fine_attempts),
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


def _prepared_mesh_graph_route_points(
    graph: NavigationVoxel3DGraph | None,
    details: Mapping[str, object],
) -> tuple[Point, ...]:
    """Return the exact persisted mesh path from its entry to its terminal."""
    if graph is None or not graph.nodes:
        return ()

    def parsed_key(value: object) -> VoxelGraphKey | None:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return None
        if len(value) != 3:
            return None
        try:
            key = tuple(int(coordinate) for coordinate in value)
        except (TypeError, ValueError):
            return None
        return key if key in graph.nodes else None  # type: ignore[return-value]

    start_key = parsed_key(details.get("seed_graph_key"))
    terminal_key = parsed_key(details.get("terminal_graph_key"))
    if start_key is None or terminal_key is None:
        return ()
    path_keys, _path_details = shortest_navigation_voxel_3d_graph_path(
        graph,
        start_key=start_key,
        terminal_key=terminal_key,
    )
    if path_keys is None or len(path_keys) < 2:
        return ()
    return tuple(
        tuple(float(value) for value in graph.nodes[key].center)
        for key in path_keys
    )  # type: ignore[return-value]


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
    vertical_gap_seeds: Mapping[FootprintCell, Sequence[Point]] | None = None,
) -> tuple[Point, ...]:
    """Return surface-gap flood-fill seeds without trusting route heights."""
    points: list[Point] = []
    for cell in cells:
        gap_points = (
            ()
            if vertical_gap_seeds is None
            else vertical_gap_seeds.get(cell, ())
        )
        if gap_points:
            points.extend(gap_points)
            continue
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
                    vertical_voxel_size_m=float(
                        config.fine_vertical_voxel_size_m
                    ),
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


@dataclass(frozen=True)
class _RouteTubeSegmentProbe:
    """Precomputed exact-distance inputs for one indexed route segment."""

    first: Point
    delta: Point
    inverse_length_squared: float
    bounds_min: Point
    bounds_max: Point


@dataclass(frozen=True)
class _HorizontalRouteTubeSegmentProbe:
    """Precomputed X/Z distance inputs for one route-envelope segment."""

    first_x: float
    first_z: float
    delta_x: float
    delta_z: float
    inverse_length_squared: float
    bounds_min_x: float
    bounds_max_x: float
    bounds_min_z: float
    bounds_max_z: float


def _horizontal_route_tube_point_filter(
    route_points: Sequence[Point],
    *,
    radius_m: float,
    voxel_size_m: float,
) -> Callable[[Point], bool]:
    """Index an X/Z route envelope without trusting imported route heights.

    OBJ-derived footprint centerlines preserve useful passage ordering in the
    horizontal plane, but their interpolated Y samples can jump between the
    floor, ceiling, or a stacked passage.  Those samples therefore bound only
    X/Z work.  The 0.25 m vertical occupied-wins field and exact cached mesh
    checks remain the sole authority for which cave layer is connected.
    """
    points = tuple(route_points)
    if not points:
        return lambda _point: False
    radius = max(float(voxel_size_m), float(radius_m))
    effective_radius = radius + (
        math.sqrt(2.0) * float(voxel_size_m) * 0.5
    )
    bucket_size = radius
    sample_spacing = max(float(voxel_size_m), bucket_size * 0.5)
    bucket_padding = max(
        1,
        int(math.ceil(effective_radius / bucket_size + 0.25)),
    )
    segments = (
        tuple(zip(points[:-1], points[1:], strict=True))
        if len(points) >= 2
        else ((points[0], points[0]),)
    )
    probes: list[_HorizontalRouteTubeSegmentProbe] = []
    mutable_buckets: dict[tuple[int, int], set[int]] = {}
    for segment_index, (first, second) in enumerate(segments):
        first_x = float(first[0])
        first_z = float(first[2])
        delta_x = float(second[0]) - first_x
        delta_z = float(second[2]) - first_z
        length_squared = delta_x * delta_x + delta_z * delta_z
        probes.append(
            _HorizontalRouteTubeSegmentProbe(
                first_x=first_x,
                first_z=first_z,
                delta_x=delta_x,
                delta_z=delta_z,
                inverse_length_squared=(
                    0.0 if length_squared <= 1e-12 else 1.0 / length_squared
                ),
                bounds_min_x=min(first_x, float(second[0])) - effective_radius,
                bounds_max_x=max(first_x, float(second[0])) + effective_radius,
                bounds_min_z=min(first_z, float(second[2])) - effective_radius,
                bounds_max_z=max(first_z, float(second[2])) + effective_radius,
            )
        )
        length_m = math.sqrt(length_squared)
        sample_count = max(1, int(math.ceil(length_m / sample_spacing)))
        for sample_index in range(sample_count + 1):
            fraction = float(sample_index) / float(sample_count)
            sample_x = first_x + delta_x * fraction
            sample_z = first_z + delta_z * fraction
            base = (
                math.floor(sample_x / bucket_size),
                math.floor(sample_z / bucket_size),
            )
            for delta_bucket_x in range(-bucket_padding, bucket_padding + 1):
                for delta_bucket_z in range(
                    -bucket_padding,
                    bucket_padding + 1,
                ):
                    mutable_buckets.setdefault(
                        (
                            base[0] + delta_bucket_x,
                            base[1] + delta_bucket_z,
                        ),
                        set(),
                    ).add(segment_index)
    buckets = {
        key: tuple(sorted(values)) for key, values in mutable_buckets.items()
    }
    distance_limit_squared = effective_radius * effective_radius

    def contains(point: Point) -> bool:
        point_x = float(point[0])
        point_z = float(point[2])
        bucket = (
            math.floor(point_x / bucket_size),
            math.floor(point_z / bucket_size),
        )
        for segment_index in buckets.get(bucket, ()):
            probe = probes[segment_index]
            if (
                point_x < probe.bounds_min_x - 1e-9
                or point_x > probe.bounds_max_x + 1e-9
                or point_z < probe.bounds_min_z - 1e-9
                or point_z > probe.bounds_max_z + 1e-9
            ):
                continue
            relative_x = point_x - probe.first_x
            relative_z = point_z - probe.first_z
            fraction = max(
                0.0,
                min(
                    1.0,
                    (
                        relative_x * probe.delta_x
                        + relative_z * probe.delta_z
                    )
                    * probe.inverse_length_squared,
                ),
            )
            distance_x = relative_x - probe.delta_x * fraction
            distance_z = relative_z - probe.delta_z * fraction
            if (
                distance_x * distance_x + distance_z * distance_z
                <= distance_limit_squared + 1e-9
            ):
                return True
        return False

    return contains


def _horizontal_point_distance_m(
    first: Sequence[float],
    second: Sequence[float],
) -> float:
    """Return X/Z distance while deliberately ignoring route-derived Y."""
    return math.hypot(
        float(second[0]) - float(first[0]),
        float(second[2]) - float(first[2]),
    )


def _mesh_supported_candidate_points(
    points: Sequence[Point],
    *,
    mesh_point_has_opposing_support: MeshPointSupportCheck | None,
    max_distance_m: float,
    minimum_clearance_m: float,
) -> tuple[Point, ...]:
    """Keep candidate points bracketed by exact mesh on an opposing axis."""
    unique_points = tuple(dict.fromkeys(points))
    if mesh_point_has_opposing_support is None:
        return unique_points
    admitted: list[Point] = []
    for point in unique_points:
        try:
            supported = bool(
                mesh_point_has_opposing_support(
                    point,
                    float(max_distance_m),
                    float(minimum_clearance_m),
                )
            )
        except Exception:
            supported = False
        if supported:
            admitted.append(point)
    return tuple(admitted)


def _horizontal_cubic_voxel_candidates(
    graph: SparseCubicVoxelGraph,
    point: Sequence[float],
    *,
    max_distance_m: float,
    limit: int | None = None,
) -> tuple[tuple[CubicVoxelKey, float], ...]:
    """Return endpoint candidates from every Y layer near one X/Z hint.

    Centerline endpoints are footprint ordering hints, so their Y coordinate
    must not choose or reject a cave layer. A bounded result remains vertically
    diverse; the later source-connected exact roadmap decides which candidate
    is executable.
    """
    try:
        if len(point) != 3:
            return ()
        target_x = float(point[0])
        target_z = float(point[2])
        maximum = float(max_distance_m)
    except (TypeError, ValueError):
        return ()
    if (
        not math.isfinite(target_x)
        or not math.isfinite(target_z)
        or not math.isfinite(maximum)
        or maximum < 0.0
    ):
        return ()
    candidates: list[tuple[CubicVoxelKey, float]] = []
    for key in graph.iter_keys():
        center = graph.voxel_center(key)
        distance_m = math.hypot(center[0] - target_x, center[2] - target_z)
        if distance_m <= maximum + 1e-9:
            candidates.append((key, float(distance_m)))
    ordered = tuple(sorted(candidates, key=lambda item: (item[1], item[0])))
    if limit is None or len(ordered) <= max(1, int(limit)):
        return ordered

    result_limit = max(1, int(limit))
    vertical_bucket_size_m = max(
        1.0,
        float(graph.vertical_voxel_size_m) * 4.0,
    )
    best_by_vertical_bucket: dict[int, tuple[CubicVoxelKey, float]] = {}
    for candidate in ordered:
        center_y = graph.voxel_center(candidate[0])[1]
        bucket = math.floor(center_y / vertical_bucket_size_m)
        best_by_vertical_bucket.setdefault(bucket, candidate)
    bucket_candidates = [
        best_by_vertical_bucket[bucket]
        for bucket in sorted(best_by_vertical_bucket)
    ]
    if len(bucket_candidates) > result_limit:
        if result_limit == 1:
            bucket_candidates = [
                bucket_candidates[len(bucket_candidates) // 2]
            ]
        else:
            selected_indices = tuple(
                round(
                    float(index)
                    * float(len(bucket_candidates) - 1)
                    / float(result_limit - 1)
                )
                for index in range(result_limit)
            )
            bucket_candidates = [
                bucket_candidates[index] for index in selected_indices
            ]
    selected_keys = {candidate[0] for candidate in bucket_candidates}
    selected = list(bucket_candidates)
    for candidate in ordered:
        if len(selected) >= result_limit:
            break
        if candidate[0] in selected_keys:
            continue
        selected.append(candidate)
        selected_keys.add(candidate[0])
    return tuple(sorted(selected, key=lambda item: (item[1], item[0])))


def _route_tube_point_filter(
    route_points: Sequence[Point],
    *,
    radius_m: float,
    voxel_size_m: float,
) -> Callable[[Point], bool]:
    """Index a true-3D polyline tube for bounded cubic candidate checks."""
    points = tuple(route_points)
    if not points:
        return lambda _point: False
    radius = max(float(voxel_size_m), float(radius_m))
    # Retain a cube when its volume intersects the requested physical tube,
    # not only when its center lies inside it.
    effective_radius = radius + (math.sqrt(3.0) * float(voxel_size_m) * 0.5)
    bucket_size = radius
    sample_spacing = max(float(voxel_size_m), bucket_size * 0.5)
    bucket_padding = max(
        1,
        int(math.ceil(effective_radius / bucket_size + 0.25)),
    )
    segments = (
        tuple(zip(points[:-1], points[1:], strict=True))
        if len(points) >= 2
        else ((points[0], points[0]),)
    )
    segment_buckets: dict[tuple[int, int, int], set[int]] = {}
    for segment_index, (first, second) in enumerate(segments):
        length_m = math.dist(first, second)
        sample_count = max(1, int(math.ceil(length_m / sample_spacing)))
        for sample_index in range(sample_count + 1):
            fraction = float(sample_index) / float(sample_count)
            sample = tuple(
                float(first[axis])
                + (float(second[axis]) - float(first[axis])) * fraction
                for axis in range(3)
            )
            base = tuple(
                math.floor(sample[axis] / bucket_size)
                for axis in range(3)
            )
            for delta_x in range(-bucket_padding, bucket_padding + 1):
                for delta_y in range(-bucket_padding, bucket_padding + 1):
                    for delta_z in range(-bucket_padding, bucket_padding + 1):
                        segment_buckets.setdefault(
                            (
                                base[0] + delta_x,
                                base[1] + delta_y,
                                base[2] + delta_z,
                            ),
                            set(),
                        ).add(segment_index)
    frozen_buckets = {
        key: tuple(sorted(values)) for key, values in segment_buckets.items()
    }
    distance_limit_squared = effective_radius * effective_radius
    segment_probes = tuple(
        _route_tube_segment_probe(
            first,
            second,
            effective_radius=effective_radius,
        )
        for first, second in segments
    )

    def contains(point: Point) -> bool:
        bucket = tuple(
            math.floor(float(point[axis]) / bucket_size)
            for axis in range(3)
        )
        point_x = float(point[0])
        point_y = float(point[1])
        point_z = float(point[2])
        for segment_index in frozen_buckets.get(bucket, ()):
            probe = segment_probes[segment_index]
            lower = probe.bounds_min
            upper = probe.bounds_max
            if (
                point_x < lower[0] - 1e-9
                or point_x > upper[0] + 1e-9
                or point_y < lower[1] - 1e-9
                or point_y > upper[1] + 1e-9
                or point_z < lower[2] - 1e-9
                or point_z > upper[2] + 1e-9
            ):
                continue
            first = probe.first
            delta = probe.delta
            relative_x = point_x - first[0]
            relative_y = point_y - first[1]
            relative_z = point_z - first[2]
            fraction = max(
                0.0,
                min(
                    1.0,
                    (
                        relative_x * delta[0]
                        + relative_y * delta[1]
                        + relative_z * delta[2]
                    )
                    * probe.inverse_length_squared,
                ),
            )
            distance_x = relative_x - delta[0] * fraction
            distance_y = relative_y - delta[1] * fraction
            distance_z = relative_z - delta[2] * fraction
            if (
                distance_x * distance_x
                + distance_y * distance_y
                + distance_z * distance_z
                <= distance_limit_squared + 1e-9
            ):
                return True
        return False

    return contains


def _route_tube_segment_probe(
    first: Point,
    second: Point,
    *,
    effective_radius: float,
) -> _RouteTubeSegmentProbe:
    first_point = tuple(float(value) for value in first)
    second_point = tuple(float(value) for value in second)
    delta = tuple(
        second_point[axis] - first_point[axis]
        for axis in range(3)
    )
    length_squared = sum(value * value for value in delta)
    radius = max(0.0, float(effective_radius))
    return _RouteTubeSegmentProbe(
        first=first_point,  # type: ignore[arg-type]
        delta=delta,  # type: ignore[arg-type]
        inverse_length_squared=(
            0.0 if length_squared <= 1e-12 else 1.0 / length_squared
        ),
        bounds_min=tuple(
            min(first_point[axis], second_point[axis]) - radius
            for axis in range(3)
        ),  # type: ignore[arg-type]
        bounds_max=tuple(
            max(first_point[axis], second_point[axis]) + radius
            for axis in range(3)
        ),  # type: ignore[arg-type]
    )


def _point_segment_distance_squared(
    point: Point,
    first: Point,
    second: Point,
) -> float:
    delta = tuple(
        float(second[axis]) - float(first[axis])
        for axis in range(3)
    )
    relative = tuple(
        float(point[axis]) - float(first[axis])
        for axis in range(3)
    )
    denominator = sum(value * value for value in delta)
    fraction = (
        0.0
        if denominator <= 1e-12
        else max(
            0.0,
            min(
                1.0,
                sum(
                    relative[axis] * delta[axis]
                    for axis in range(3)
                )
                / denominator,
            ),
        )
    )
    return sum(
        (
            float(point[axis])
            - (
                float(first[axis])
                + delta[axis] * fraction
            )
        )
        ** 2
        for axis in range(3)
    )


def _cubic_corridor_radius_candidates(
    maximum_radius_m: float,
    *,
    voxel_size_m: float,
    minimum_radius_m: float | None = None,
) -> tuple[float, ...]:
    """Return deterministic V12 radii without clipping source uncertainty."""
    maximum = max(float(voxel_size_m), float(maximum_radius_m))
    evidence_minimum = (
        0.0
        if minimum_radius_m is None
        else max(0.0, float(minimum_radius_m))
    )
    minimum = min(
        maximum,
        max(
            float(MIN_CACHE_VOXEL_ROUTE_CORRIDOR_RADIUS_M),
            float(voxel_size_m) * 4.0,
            evidence_minimum,
        ),
    )
    candidates = [maximum]
    while candidates[-1] > minimum + 1e-9:
        next_radius = max(minimum, candidates[-1] * 0.5)
        if math.isclose(
            next_radius,
            candidates[-1],
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            break
        candidates.append(next_radius)
    return tuple(float(value) for value in candidates)


def _consecutive_route_cells(
    cells: Sequence[FootprintCell],
) -> tuple[FootprintCell, ...]:
    """Remove only adjacent duplicate footprint samples, preserving order."""
    ordered: list[FootprintCell] = []
    for raw_cell in cells:
        cell = (int(raw_cell[0]), int(raw_cell[1]))
        if not ordered or cell != ordered[-1]:
            ordered.append(cell)
    return tuple(ordered)


def _route_cell_horizontal_guide_points(
    cells: Sequence[FootprintCell],
    *,
    cell_size: float,
) -> tuple[Point, ...]:
    """Return route-ordered X/Z guides without importing route-derived Y."""
    return tuple(
        (
            float(footprint_world_center(cell, cell_size)[0]),
            0.0,
            float(footprint_world_center(cell, cell_size)[1]),
        )
        for cell in cells
    )


def _fixed_route_corridor_cells(
    route_cells: Sequence[FootprintCell],
    component_cells: Sequence[FootprintCell],
    *,
    cell_size: float,
    radius_m: float,
) -> tuple[FootprintCell, ...]:
    """Return a map-independent bounded corridor around one terminal route."""
    component = set(component_cells)
    anchors = tuple(dict.fromkeys(route_cells))
    if not anchors:
        return tuple(sorted(component))
    radius_cells = max(1, int(math.ceil(float(radius_m) / cell_size)))
    selected: set[FootprintCell] = set()
    for cell in anchors:
        for delta_x in range(-radius_cells, radius_cells + 1):
            for delta_z in range(-radius_cells, radius_cells + 1):
                candidate = (cell[0] + delta_x, cell[1] + delta_z)
                if not component or candidate in component:
                    selected.add(candidate)
    selected.update(cell for cell in anchors if not component or cell in component)
    return tuple(sorted(selected))


def _select_terminal_cubic_component(
    graph: SparseCubicVoxelGraph,
    route_points: Sequence[Point],
    *,
    terminal_snap_distance_m: float,
    ingress_snap_distance_m: float,
    max_component_voxels: int,
    require_original_ingress: bool = False,
    source_ingress_point: Point | None = None,
    source_ingress_snap_distance_m: float | None = None,
    source_ingress_gap_y_ranges: Mapping[
        FootprintCell,
        tuple[float, float],
    ]
    | None = None,
    source_ingress_footprint_cell_size_m: float | None = None,
    required_route_cells: Sequence[FootprintCell] = (),
    required_vertical_gap_intervals: Mapping[
        FootprintCell,
        Sequence[tuple[float, float]],
    ]
    | None = None,
    required_footprint_cell_size_m: float | None = None,
) -> tuple[SparseCubicVoxelGraph | None, dict[str, object]]:
    """Select the best endpoint component joining route evidence to the goal."""
    component_diagnostics_complete = graph.free_voxel_count <= 131_072
    component_sizes = (
        graph.component_sizes() if component_diagnostics_complete else ()
    )
    details: dict[str, object] = {
        "method": "terminal_anchored_cardinal_component_v1",
        "candidate_free_voxel_count": int(graph.free_voxel_count),
        "candidate_component_count": (
            len(component_sizes) if component_diagnostics_complete else None
        ),
        "candidate_component_diagnostics_complete": bool(
            component_diagnostics_complete
        ),
        "largest_candidate_component_sizes": [
            int(value) for value in component_sizes[:8]
        ],
        "selected_component_voxel_count": 0,
        "known_terminal_reached": False,
        "ingress_reached": False,
    }
    if len(route_points) < 2 or graph.free_voxel_count <= 0:
        details["reason"] = "cubic_component_route_or_voxels_missing"
        return None, details
    terminal_limit = max(
        float(terminal_snap_distance_m),
        float(graph.cell_diagonal_m),
    )
    minimum_meaningful_route_length_m = max(
        float(MIN_CACHE_VOXEL_MEANINGFUL_ROUTE_LENGTH_M),
        float(terminal_limit) * 2.0,
    )
    details["minimum_meaningful_route_length_m"] = float(
        minimum_meaningful_route_length_m
    )
    ordered_required_cells: list[FootprintCell] = []
    for raw_cell in required_route_cells:
        cell = (int(raw_cell[0]), int(raw_cell[1]))
        if not ordered_required_cells or cell != ordered_required_cells[-1]:
            ordered_required_cells.append(cell)
    interval_route_required = bool(ordered_required_cells)
    interval_terminal_keys: tuple[CubicVoxelKey, ...] = ()
    if interval_route_required:
        try:
            required_cell_size = float(required_footprint_cell_size_m)
        except (TypeError, ValueError):
            required_cell_size = 0.0
        if required_vertical_gap_intervals is not None:
            interval_terminal_keys = _surface_gap_interval_terminal_keys(
                graph,
                terminal_cell=ordered_required_cells[-1],
                vertical_gap_intervals=required_vertical_gap_intervals,
                footprint_cell_size_m=required_cell_size,
                terminal_point=route_points[-1],
                max_horizontal_distance_m=terminal_limit,
            )
        terminal_candidates = tuple(
            (
                key,
                _horizontal_point_distance_m(
                    route_points[-1],
                    graph.voxel_center(key),
                ),
            )
            for key in interval_terminal_keys
        )
        details.update(
            {
                "terminal_candidate_source": (
                    "final_cell_bounded_surface_intervals_v1"
                ),
                "terminal_interval_candidate_count": len(
                    interval_terminal_keys
                ),
                "surface_gap_route_cell_count": len(
                    ordered_required_cells
                ),
            }
        )
    else:
        terminal_candidates = _horizontal_cubic_voxel_candidates(
            graph,
            route_points[-1],
            max_distance_m=terminal_limit,
        )
        details["terminal_candidate_source"] = "horizontal_endpoint_hint_v1"
    ingress_limit = max(
        0.0,
        float(
            source_ingress_snap_distance_m
            if source_ingress_point is not None
            and source_ingress_snap_distance_m is not None
            else ingress_snap_distance_m
        ),
    )
    if source_ingress_point is not None:
        ingress_limit = min(
            ingress_limit,
            MAX_OBJ_SOURCE_INGRESS_SNAP_DISTANCE_M,
        )
        details.update(
            {
                "source_ingress_required": True,
                "source_ingress_point": _point_payload(
                    source_ingress_point
                ),
                "source_ingress_coordinate_space": "xyz",
                "source_ingress_snap_limit_m": float(ingress_limit),
            }
        )
    if not terminal_candidates and (
        require_original_ingress or interval_route_required
    ):
        details.update(
            {
                "reason": "cubic_component_terminal_voxel_missing",
                "terminal_snap_limit_m": float(terminal_limit),
            }
        )
        return None, details
    pending_candidates = list(terminal_candidates)
    viable: list[
        tuple[
            tuple[object, ...],
            SparseCubicVoxelGraph,
            dict[str, object],
        ]
    ] = []
    failed_candidates: list[dict[str, object]] = []
    candidate_summaries: list[dict[str, object]] = []
    while pending_candidates:
        seed_key, _seed_distance = pending_candidates[0]
        component = graph.connected_component(
            seed_key,
            max_voxels=max_component_voxels,
        )
        component_terminal_candidates = tuple(
            candidate
            for candidate in pending_candidates
            if component.contains_key(candidate[0])
        )
        pending_candidates = [
            candidate
            for candidate in pending_candidates
            if not component.contains_key(candidate[0])
        ]
        terminal_key, terminal_rank_distance = min(
            component_terminal_candidates,
            key=lambda item: (item[1], item[0]),
        )
        component_gate_details: dict[str, object] = {}
        component_route_key_groups: (
            tuple[tuple[CubicVoxelKey, ...], ...] | None
        ) = None
        if interval_route_required:
            assert required_vertical_gap_intervals is not None
            component_route_key_groups = (
                _surface_gap_interval_route_key_groups(
                    component,
                    route_cells=ordered_required_cells,
                    vertical_gap_intervals=required_vertical_gap_intervals,
                    footprint_cell_size_m=float(
                        required_footprint_cell_size_m
                    ),
                    source_point=source_ingress_point,
                    source_max_distance_m=ingress_limit,
                    terminal_point=route_points[-1],
                    terminal_max_horizontal_distance_m=terminal_limit,
                    diagnostics=component_gate_details,
                )
            )
            if component_route_key_groups is not None:
                # Component discovery must inspect every interval-backed key
                # in the endpoint cell. Only after selecting one component
                # may the terminal proposals be bounded; the route-key helper
                # has already applied that component-local cap while retaining
                # interval diversity. Intersecting with a globally capped list
                # can hide the only source-connected endpoint pocket.
                local_terminal_candidates = component_route_key_groups[-1]
                if local_terminal_candidates:
                    terminal_key = local_terminal_candidates[0]
                else:
                    component_route_key_groups = None
            else:
                local_terminal_candidates = ()
        else:
            local_terminal_candidates = _cubic_terminal_neighbor_candidates(
                component,
                terminal_key,
            )
        terminal_distance = (
            _horizontal_point_distance_m(
                route_points[-1],
                component.voxel_center(terminal_key),
            )
            if interval_route_required
            else float(terminal_rank_distance)
        )
        attachment, attachment_details = _cubic_component_route_attachment(
            component,
            route_points,
            ingress_snap_distance_m=ingress_limit,
            require_original_ingress=require_original_ingress,
            source_ingress_point=source_ingress_point,
            source_ingress_gap_y_ranges=source_ingress_gap_y_ranges,
            source_ingress_footprint_cell_size_m=(
                source_ingress_footprint_cell_size_m
            ),
            source_ingress_candidate_keys=(
                component_route_key_groups[0]
                if component_route_key_groups is not None
                else None
            ),
        )
        candidate_details = {
            "terminal_graph_key": [int(value) for value in terminal_key],
            "terminal_snap_distance_m": float(terminal_distance),
            "terminal_snap_limit_m": float(terminal_limit),
            "terminal_hint_index": len(route_points) - 1,
            "terminal_graph_key_candidate_count": len(
                local_terminal_candidates
            ),
            "terminal_graph_key_candidates": [
                [int(value) for value in candidate_key]
                for candidate_key in local_terminal_candidates
            ],
            "selected_component_voxel_count": int(
                component.free_voxel_count
            ),
            "known_terminal_reached": True,
            **component_gate_details,
            **attachment_details,
        }
        if component_route_key_groups is not None:
            candidate_details["_surface_gap_route_key_groups"] = (
                component_route_key_groups
            )
        summary = {
            "terminal_graph_key": [int(value) for value in terminal_key],
            "terminal_snap_distance_m": float(terminal_distance),
            "component_voxel_count": int(component.free_voxel_count),
            "ingress_reached": attachment is not None,
            "ingress_hint_index": candidate_details.get(
                "ingress_hint_index"
            ),
            "contiguous_route_length_m": candidate_details.get(
                "contiguous_route_length_m",
                0.0,
            ),
            "source_ingress_attachment_distance_m": candidate_details.get(
                "source_ingress_attachment_distance_m"
            ),
            "surface_gap_gate_reason": candidate_details.get(
                "surface_gap_gate_reason"
            ),
            "missing_surface_gap_gate_indices": candidate_details.get(
                "missing_surface_gap_gate_indices",
                [],
            ),
            "missing_surface_gap_gate_cells": candidate_details.get(
                "missing_surface_gap_gate_cells",
                [],
            ),
        }
        candidate_summaries.append(summary)
        if component_route_key_groups is None and interval_route_required:
            failed_candidates.append(candidate_details)
            continue
        if attachment is None:
            failed_candidates.append(candidate_details)
            continue
        ingress_index, _ingress_distance, _ingress_key = attachment
        original_ingress = candidate_details.get("ingress_selection") in {
            "original_route_ingress_v1",
            "strict_obj_source_ingress_v1",
            "strict_navigation_source_ingress_v2",
        }
        rank = _cubic_terminal_route_rank(
            candidate_details,
            original_ingress=original_ingress,
            minimum_meaningful_route_length_m=(
                minimum_meaningful_route_length_m
            ),
            component_voxel_count=component.free_voxel_count,
            terminal_distance_m=terminal_distance,
            ingress_index=ingress_index,
            terminal_key=terminal_key,
        )
        viable.append((rank, component, candidate_details))

    if not require_original_ingress and not interval_route_required:
        route_component_candidates = _contiguous_route_component_candidates(
            graph,
            route_points,
            snap_distance_m=ingress_limit,
            max_component_voxels=max_component_voxels,
        )
        ranked_route_components: list[
            tuple[
                tuple[object, ...],
                SparseCubicVoxelGraph,
                dict[str, object],
            ]
        ] = []
        seen_route_ranges: set[tuple[int, int]] = set()
        for (
            _longest_rank,
            route_component,
            route_component_details,
        ) in route_component_candidates:
            route_range = (
                int(route_component_details["ingress_hint_index"]),
                int(route_component_details["terminal_hint_index"]),
            )
            if route_range in seen_route_ranges:
                continue
            seen_route_ranges.add(route_range)
            route_component_rank = _cubic_terminal_route_rank(
                route_component_details,
                original_ingress=False,
                minimum_meaningful_route_length_m=(
                    minimum_meaningful_route_length_m
                ),
                component_voxel_count=route_component.free_voxel_count,
                terminal_distance_m=float(
                    route_component_details["terminal_snap_distance_m"]
                ),
                ingress_index=int(
                    route_component_details["ingress_hint_index"]
                ),
                terminal_key=tuple(
                    route_component_details["terminal_graph_key"]
                ),
            )
            ranked_route_components.append(
                (
                    route_component_rank,
                    route_component,
                    route_component_details,
                )
            )
        ranked_route_components.sort(key=lambda item: item[0])
        viable.extend(ranked_route_components)
        route_component_fallbacks = [
            {
                "ingress_hint_index": component_details[
                    "ingress_hint_index"
                ],
                "terminal_hint_index": component_details[
                    "terminal_hint_index"
                ],
                "contiguous_route_length_m": component_details[
                    "contiguous_route_length_m"
                ],
                "component_voxel_count": int(component.free_voxel_count),
            }
            for _rank, component, component_details in (
                ranked_route_components[:16]
            )
        ]
        details["route_component_fallbacks"] = route_component_fallbacks
        if route_component_fallbacks:
            details["route_component_fallback"] = dict(
                route_component_fallbacks[0]
            )

    details["terminal_component_candidate_count"] = len(
        candidate_summaries
    )
    details["terminal_component_candidates"] = candidate_summaries[:8]
    if not viable:
        if failed_candidates:
            details.update(
                min(
                    failed_candidates,
                    key=lambda candidate: (
                        len(
                            candidate.get(
                                "missing_surface_gap_gate_indices",
                                (),
                            )
                        ),
                        float(candidate["terminal_snap_distance_m"]),
                        -int(candidate["selected_component_voxel_count"]),
                    ),
                )
            )
        details.update(
            {
                "reason": (
                    "cubic_component_terminal_voxel_missing"
                    if not terminal_candidates
                    else (
                        "cubic_component_surface_gap_route_missing"
                        if interval_route_required
                        and any(
                            candidate.get("surface_gap_gate_reason")
                            != "complete"
                            for candidate in failed_candidates
                        )
                        else "cubic_component_ingress_voxel_missing"
                    )
                ),
                "terminal_snap_limit_m": float(terminal_limit),
                "original_ingress_required": bool(
                    require_original_ingress
                ),
            }
        )
        return None, details

    _rank, selected_component, selected_details = min(
        viable,
        key=lambda item: item[0],
    )
    details.update(selected_details)
    details.update(
        {
            "reason": "cubic_terminal_component_selected",
            "ingress_reached": True,
        }
    )
    return selected_component, details


def _cubic_terminal_route_rank(
    candidate_details: Mapping[str, object],
    *,
    original_ingress: bool,
    minimum_meaningful_route_length_m: float,
    component_voxel_count: int,
    terminal_distance_m: float,
    ingress_index: int,
    terminal_key: Sequence[int],
) -> tuple[object, ...]:
    """Rank authored ingress by longest safe reach, then unauthored ease.

    An authored entrance owns the start, so retain its longest connected
    terminal candidate. Without one, a tiny disconnected pocket must not win
    merely because it is short; choose the easiest meaningful component and
    retain the longest run only when none reaches that universal floor.
    """
    route_length_m = max(
        0.0,
        float(candidate_details.get("contiguous_route_length_m", 0.0)),
    )
    meaningful = (
        route_length_m + 1e-9
        >= float(minimum_meaningful_route_length_m)
    )
    try:
        ingress_attachment_distance_m = float(
            candidate_details.get(
                "source_ingress_attachment_distance_m",
                candidate_details.get("ingress_snap_distance_m", math.inf),
            )
        )
    except (TypeError, ValueError):
        ingress_attachment_distance_m = math.inf
    if not math.isfinite(ingress_attachment_distance_m):
        ingress_attachment_distance_m = math.inf
    return (
        0 if original_ingress else 1,
        0 if meaningful else 1,
        (
            -route_length_m
            if original_ingress or not meaningful
            else route_length_m
        ),
        (
            ingress_attachment_distance_m
            if original_ingress
            else 0.0
        ),
        -int(candidate_details.get("reachable_route_hint_count", 0)),
        -int(component_voxel_count),
        float(terminal_distance_m),
        int(ingress_index),
        tuple(int(value) for value in terminal_key),
    )


def _cubic_terminal_neighbor_candidates(
    component: SparseCubicVoxelGraph,
    terminal_key: CubicVoxelKey,
) -> tuple[CubicVoxelKey, ...]:
    """Return the selected endpoint cube and its bounded local free shell."""
    center = component.voxel_center(terminal_key)
    nearby = component.keys_within_distance(
        center,
        max_distance_m=(
            float(component.voxel_size_m)
            * DEFAULT_CUBIC_TERMINAL_NEIGHBOR_RADIUS_VOXELS
            + 1e-9
        ),
    )
    ordered = tuple(key for key, _distance_m in nearby)
    if terminal_key not in ordered:
        ordered = (terminal_key, *ordered)
    return ordered[:DEFAULT_CUBIC_TERMINAL_CANDIDATE_LIMIT]


def _contiguous_route_component_candidates(
    graph: SparseCubicVoxelGraph,
    route_points: Sequence[Point],
    *,
    snap_distance_m: float,
    max_component_voxels: int,
) -> tuple[
    tuple[
        tuple[object, ...],
        SparseCubicVoxelGraph,
        dict[str, object],
    ],
    ...,
]:
    """Return every distinct component-backed consecutive route run."""
    snap_limit = max(float(graph.voxel_size_m), float(snap_distance_m))
    pending: list[tuple[int, float, CubicVoxelKey]] = []
    for index, point in enumerate(route_points):
        key, distance_m = graph.nearest_key(
            point,
            max_distance_m=snap_limit,
        )
        if key is not None:
            pending.append((index, float(distance_m), key))
    candidates: list[tuple[
        tuple[object, ...],
        SparseCubicVoxelGraph,
        dict[str, object],
    ]] = []
    while pending:
        component = graph.connected_component(
            pending[0][2],
            max_voxels=max_component_voxels,
        )
        component_hints = tuple(
            value for value in pending if component.contains_key(value[2])
        )
        pending = [
            value for value in pending if not component.contains_key(value[2])
        ]
        hint_by_index = {value[0]: value for value in component_hints}
        indices = sorted(hint_by_index)
        run_start = 0
        for cursor in range(1, len(indices) + 1):
            run_continues = (
                cursor < len(indices)
                and indices[cursor] == indices[cursor - 1] + 1
            )
            if run_continues:
                continue
            run = indices[run_start:cursor]
            run_start = cursor
            if len(run) < 2:
                continue
            first_index = int(run[0])
            terminal_index = int(run[-1])
            contiguous_length_m = sum(
                math.dist(first, second)
                for first, second in zip(
                    route_points[first_index:terminal_index],
                    route_points[first_index + 1 : terminal_index + 1],
                    strict=True,
                )
            )
            ingress = hint_by_index[first_index]
            terminal = hint_by_index[terminal_index]
            terminal_key_candidates = _cubic_terminal_neighbor_candidates(
                component,
                terminal[2],
            )
            candidate_details: dict[str, object] = {
                "terminal_graph_key": [
                    int(value) for value in terminal[2]
                ],
                "terminal_snap_distance_m": float(terminal[1]),
                "terminal_snap_limit_m": float(snap_limit),
                "terminal_hint_index": int(terminal_index),
                "terminal_graph_key_candidate_count": len(
                    terminal_key_candidates
                ),
                "terminal_graph_key_candidates": [
                    [int(value) for value in candidate_key]
                    for candidate_key in terminal_key_candidates
                ],
                "selected_component_voxel_count": int(
                    component.free_voxel_count
                ),
                "known_terminal_reached": True,
                "ingress_hint_index": int(first_index),
                "ingress_graph_key": [int(value) for value in ingress[2]],
                "ingress_snap_distance_m": float(ingress[1]),
                "ingress_snap_limit_m": float(snap_limit),
                "ingress_candidate_count": 1,
                "ingress_selection": (
                    "ranked_contiguous_route_component_v2"
                ),
                "reachable_route_hint_count": len(component_hints),
                "reachable_route_hint_first_index": int(indices[0]),
                "reachable_route_hint_last_index": int(indices[-1]),
                "original_ingress_required": False,
                "contiguous_route_length_m": float(contiguous_length_m),
            }
            rank: tuple[object, ...] = (
                -float(contiguous_length_m),
                -len(run),
                -int(component.free_voxel_count),
                int(first_index),
                int(terminal_index),
                terminal[2],
            )
            candidates.append((rank, component, candidate_details))
    return tuple(sorted(candidates, key=lambda item: item[0]))


def _longest_contiguous_route_component(
    graph: SparseCubicVoxelGraph,
    route_points: Sequence[Point],
    *,
    snap_distance_m: float,
    max_component_voxels: int,
) -> tuple[SparseCubicVoxelGraph, dict[str, object]] | None:
    """Return the component supporting the longest consecutive route run."""
    candidates = _contiguous_route_component_candidates(
        graph,
        route_points,
        snap_distance_m=snap_distance_m,
        max_component_voxels=max_component_voxels,
    )
    if not candidates:
        return None
    _rank, component, details = candidates[0]
    return component, details


def _cubic_component_route_attachment(
    component: SparseCubicVoxelGraph,
    route_points: Sequence[Point],
    *,
    ingress_snap_distance_m: float,
    require_original_ingress: bool,
    source_ingress_point: Point | None = None,
    source_ingress_gap_y_ranges: Mapping[
        FootprintCell,
        tuple[float, float],
    ]
    | None = None,
    source_ingress_footprint_cell_size_m: float | None = None,
    source_ingress_candidate_keys: Sequence[CubicVoxelKey] | None = None,
) -> tuple[
    tuple[int, float, CubicVoxelKey] | None,
    dict[str, object],
]:
    """Find an authored ingress or the longest contiguous endpoint suffix."""
    ingress_limit = max(0.0, float(ingress_snap_distance_m))
    if source_ingress_point is not None:
        if source_ingress_candidate_keys is not None:
            bounded_ingress_candidates: list[
                tuple[CubicVoxelKey, float]
            ] = []
            for key in dict.fromkeys(source_ingress_candidate_keys):
                if not component.contains_key(key):
                    continue
                center = component.voxel_center(key)
                distance_m = math.dist(source_ingress_point, center)
                if distance_m <= ingress_limit + 1e-9:
                    bounded_ingress_candidates.append((key, distance_m))
            ingress_candidates = tuple(
                sorted(
                    bounded_ingress_candidates,
                    key=lambda item: (item[1], item[0]),
                )
            )
        else:
            ingress_candidates = component.keys_within_distance(
                source_ingress_point,
                max_distance_m=ingress_limit,
            )
        gap_envelope_required = source_ingress_gap_y_ranges is not None
        if gap_envelope_required:
            try:
                footprint_cell_size = float(
                    source_ingress_footprint_cell_size_m
                )
            except (TypeError, ValueError):
                ingress_candidates = ()
            else:
                if not math.isfinite(footprint_cell_size) or footprint_cell_size <= 0.0:
                    ingress_candidates = ()
                else:
                    vertical_margin_m = (
                        float(component.vertical_voxel_size_m) * 0.5 + 1e-9
                    )
                    admitted: list[tuple[CubicVoxelKey, float]] = []
                    for candidate_key, candidate_distance_m in ingress_candidates:
                        center = component.voxel_center(candidate_key)
                        cell = (
                            math.floor(center[0] / footprint_cell_size),
                            math.floor(center[2] / footprint_cell_size),
                        )
                        candidate_range = source_ingress_gap_y_ranges.get(cell)
                        if candidate_range is None:
                            continue
                        low_y, high_y = sorted(
                            (
                                float(candidate_range[0]),
                                float(candidate_range[1]),
                            )
                        )
                        if (
                            low_y - vertical_margin_m
                            <= center[1]
                            <= high_y + vertical_margin_m
                        ):
                            admitted.append(
                                (candidate_key, float(candidate_distance_m))
                            )
                    ingress_candidates = tuple(admitted)
        if not ingress_candidates:
            return None, {
                "source_ingress_required": True,
                "source_ingress_point": _point_payload(
                    source_ingress_point
                ),
                "source_ingress_coordinate_space": "xyz",
                "source_ingress_snap_limit_m": float(ingress_limit),
                "source_ingress_gap_envelope_required": bool(
                    gap_envelope_required
                ),
                "original_ingress_required": True,
            }
        ingress_key, distance_m = ingress_candidates[0]
        attachment_point = component.voxel_center(ingress_key)
        return (0, float(distance_m), ingress_key), {
            "ingress_hint_index": 0,
            "ingress_graph_key": [int(value) for value in ingress_key],
            "ingress_snap_distance_m": float(distance_m),
            "ingress_snap_limit_m": float(ingress_limit),
            "ingress_candidate_count": len(ingress_candidates),
            "ingress_graph_key_candidates": [
                [int(value) for value in candidate_key]
                for candidate_key, _candidate_distance_m in ingress_candidates[
                    :DEFAULT_CUBIC_INGRESS_CANDIDATE_LIMIT
                ]
            ],
            "ingress_selection": "strict_navigation_source_ingress_v2",
            "original_ingress_required": True,
            "source_ingress_required": True,
            "source_ingress_point": _point_payload(source_ingress_point),
            "source_ingress_attachment_point": _point_payload(
                attachment_point
            ),
            "source_ingress_attachment_distance_m": float(distance_m),
            "source_ingress_coordinate_space": "xyz",
            "source_ingress_snap_limit_m": float(ingress_limit),
            "source_ingress_gap_envelope_required": bool(
                gap_envelope_required
            ),
            "contiguous_route_length_m": float(
                sum(
                    math.dist(first, second)
                    for first, second in zip(
                        route_points[:-1],
                        route_points[1:],
                        strict=True,
                    )
                )
            ),
        }
    first_point_count = min(
        DEFAULT_MESH_GRAPH_ENTRY_SEED_POINTS,
        len(route_points) - 1,
    )
    original_candidates: list[tuple[int, float, CubicVoxelKey]] = []
    for index, point in enumerate(route_points[:first_point_count]):
        key, distance_m = component.nearest_key(
            point,
            max_distance_m=ingress_limit,
        )
        if key is not None:
            original_candidates.append((index, float(distance_m), key))
    if original_candidates:
        selected = min(original_candidates)
        return selected, {
            "ingress_hint_index": int(selected[0]),
            "ingress_graph_key": [int(value) for value in selected[2]],
            "ingress_snap_distance_m": float(selected[1]),
            "ingress_snap_limit_m": float(ingress_limit),
            "ingress_candidate_count": len(original_candidates),
            "ingress_selection": "original_route_ingress_v1",
            "original_ingress_required": bool(require_original_ingress),
            "contiguous_route_length_m": float(
                sum(
                    math.dist(first, second)
                    for first, second in zip(
                        route_points[selected[0] : -1],
                        route_points[selected[0] + 1 :],
                        strict=True,
                    )
                )
            ),
        }

    reachable: list[tuple[int, float, CubicVoxelKey]] = []
    for index, point in enumerate(route_points):
        key, distance_m = component.nearest_key(
            point,
            max_distance_m=ingress_limit,
        )
        if key is not None:
            reachable.append((index, float(distance_m), key))
    reachable_indices = [value[0] for value in reachable]
    reachable_set = set(reachable_indices)
    suffix_start = len(route_points) - 1
    while suffix_start - 1 in reachable_set:
        suffix_start -= 1
    shared_details: dict[str, object] = {
        "ingress_snap_limit_m": float(ingress_limit),
        "reachable_route_hint_count": len(reachable_indices),
        "reachable_route_hint_first_index": (
            int(reachable_indices[0]) if reachable_indices else None
        ),
        "reachable_route_hint_last_index": (
            int(reachable_indices[-1]) if reachable_indices else None
        ),
        "original_ingress_required": bool(require_original_ingress),
    }
    if require_original_ingress or suffix_start >= len(route_points) - 1:
        return None, shared_details
    suffix_candidates = [
        value for value in reachable if value[0] == suffix_start
    ]
    if not suffix_candidates:
        return None, shared_details
    selected = min(suffix_candidates)
    contiguous_route_length_m = sum(
        math.dist(first, second)
        for first, second in zip(
            route_points[suffix_start:-1],
            route_points[suffix_start + 1 :],
            strict=True,
        )
    )
    return selected, {
        **shared_details,
        "ingress_hint_index": int(selected[0]),
        "ingress_graph_key": [int(value) for value in selected[2]],
        "ingress_snap_distance_m": float(selected[1]),
        "ingress_candidate_count": 1,
        "ingress_selection": (
            "earliest_contiguous_terminal_component_hint_v1"
        ),
        "contiguous_route_length_m": float(contiguous_route_length_m),
    }


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


def _component_vertical_gap_seed_points(
    value: object,
    *,
    component_cells: set[FootprintCell],
    cell_size: float,
) -> dict[FootprintCell, tuple[Point, ...]]:
    """Parse surface-derived vertical gap seeds persisted with a route.

    Each compact triple contains a footprint cell and one bounded surface-gap
    midpoint. Imported/interpolated centerline Y is intentionally absent from
    this schema.
    """
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return {}
    if len(value) % 3 != 0:
        return {}
    parsed: dict[FootprintCell, list[Point]] = {}
    for index in range(0, len(value), 3):
        raw_x, raw_z, raw_y = value[index : index + 3]
        if (
            type(raw_x) is not int
            or type(raw_z) is not int
            or type(raw_y) not in (int, float)
        ):
            return {}
        cell = (int(raw_x), int(raw_z))
        y = float(raw_y)
        if cell not in component_cells or not math.isfinite(y):
            return {}
        x, z = footprint_world_center(cell, cell_size)
        parsed.setdefault(cell, []).append((float(x), y, float(z)))
    return {
        cell: tuple(dict.fromkeys(points))
        for cell, points in parsed.items()
        if points
    }


def _surface_gap_cubic_waypoint_key_groups(
    graph: SparseCubicVoxelGraph,
    *,
    route_cells: Sequence[FootprintCell],
    vertical_gap_seeds: Mapping[FootprintCell, Sequence[Point]],
    max_candidates_per_gate: int = DEFAULT_CUBIC_TERMINAL_CANDIDATE_LIMIT,
) -> tuple[tuple[CubicVoxelKey, ...], ...] | None:
    """Map ordered surface-gap evidence to bounded component waypoints.

    Surface-gap Y is paired-mesh evidence, unlike imported centerline Y. A
    seed may land on a quantization boundary, so a single local cell-diagonal
    snap is allowed; the returned key must still belong to the already
    selected global component. Every ordered intermediate breadcrumb is
    mandatory: missing evidence returns ``None`` so no later planner can
    silently skip a portion of the source route. Entrance and final cells are
    excluded because their separately bounded keys are mandatory.
    """
    point_groups = _surface_gap_waypoint_point_groups(
        route_cells=route_cells,
        vertical_gap_seeds=vertical_gap_seeds,
        max_candidates_per_gate=max_candidates_per_gate,
    )
    if point_groups is None:
        return None
    groups: list[tuple[CubicVoxelKey, ...]] = []
    for point_group in point_groups:
        keys: list[CubicVoxelKey] = []
        for seed in point_group:
            try:
                key = graph.world_key(seed)
            except (TypeError, ValueError):
                continue
            if not graph.contains_key(key):
                key, _distance_m = graph.nearest_key(
                    seed,
                    max_distance_m=graph.cell_diagonal_m + 1e-9,
                )
                if key is None:
                    continue
            if key not in keys:
                keys.append(key)
        if not keys:
            return None
        groups.append(tuple(keys))
    return tuple(groups)


def _surface_gap_waypoint_point_groups(
    *,
    route_cells: Sequence[FootprintCell],
    vertical_gap_seeds: Mapping[FootprintCell, Sequence[Point]],
    max_candidates_per_gate: int = DEFAULT_CUBIC_TERMINAL_CANDIDATE_LIMIT,
) -> tuple[tuple[Point, ...], ...] | None:
    """Return complete ordered surface-gap candidates for intermediate cells."""
    ordered_cells: list[FootprintCell] = []
    for cell in route_cells:
        normalized = (int(cell[0]), int(cell[1]))
        if not ordered_cells or normalized != ordered_cells[-1]:
            ordered_cells.append(normalized)
    if len(ordered_cells) < 2:
        return None
    if len(ordered_cells) == 2:
        return ()
    if not vertical_gap_seeds:
        return None
    candidate_limit = max(1, int(max_candidates_per_gate))
    groups: list[tuple[Point, ...]] = []
    for cell in ordered_cells[1:-1]:
        candidates = tuple(
            dict.fromkeys(vertical_gap_seeds.get(cell, ()))
        )[:candidate_limit]
        if not candidates:
            return None
        groups.append(candidates)
    return tuple(groups)


def _surface_gap_interval_route_key_groups(
    graph: SparseCubicVoxelGraph,
    *,
    route_cells: Sequence[FootprintCell],
    vertical_gap_intervals: Mapping[
        FootprintCell,
        Sequence[tuple[float, float]],
    ],
    footprint_cell_size_m: float,
    max_candidates_per_gate: int = DEFAULT_CUBIC_TERMINAL_CANDIDATE_LIMIT,
    max_vertical_transition_m: float = (
        MAX_SURFACE_GAP_VERTICAL_TRANSITION_M
    ),
    source_point: Point | None = None,
    source_max_distance_m: float | None = None,
    terminal_point: Point | None = None,
    terminal_max_horizontal_distance_m: float | None = None,
    diagnostics: dict[str, object] | None = None,
) -> tuple[tuple[CubicVoxelKey, ...], ...] | None:
    """Map every ordered footprint cell to interval-backed free voxels.

    A persisted vertical interval is surface evidence; its midpoint is only a
    proposal.  The executable candidate is a free key in the already selected
    cubic component whose center remains in the exact footprint cell and in a
    bounded interval.  Keeping candidates from each interval preserves stacked
    passages without allowing raw route Y to choose a layer.
    """
    details = diagnostics if diagnostics is not None else {}
    ordered_cells: list[FootprintCell] = []
    for raw_cell in route_cells:
        cell = (int(raw_cell[0]), int(raw_cell[1]))
        if not ordered_cells or cell != ordered_cells[-1]:
            ordered_cells.append(cell)
    try:
        footprint_cell_size = float(footprint_cell_size_m)
    except (TypeError, ValueError):
        footprint_cell_size = 0.0
    if (
        not ordered_cells
        or not math.isfinite(footprint_cell_size)
        or footprint_cell_size <= 0.0
    ):
        details.update(
            {
                "surface_gap_gate_source": "bounded_surface_intervals_v1",
                "surface_gap_gate_reason": "route_or_cell_size_missing",
                "surface_gap_gate_count": 0,
            }
        )
        return None

    requested_source = (
        _point_tuple(source_point) if source_point is not None else None
    )
    requested_terminal = (
        _point_tuple(terminal_point) if terminal_point is not None else None
    )
    if (
        (source_point is not None and requested_source is None)
        or (terminal_point is not None and requested_terminal is None)
    ):
        details.update(
            {
                "surface_gap_gate_source": "bounded_surface_intervals_v1",
                "surface_gap_gate_reason": "endpoint_evidence_malformed",
                "surface_gap_gate_count": len(ordered_cells),
            }
        )
        return None

    def optional_distance_limit(value: float | None) -> float:
        if value is None:
            return math.inf
        try:
            result = float(value)
        except (TypeError, ValueError):
            return -1.0
        return result if math.isfinite(result) and result >= 0.0 else -1.0

    source_limit_m = optional_distance_limit(source_max_distance_m)
    terminal_limit_m = optional_distance_limit(
        terminal_max_horizontal_distance_m
    )
    if source_limit_m < 0.0 or terminal_limit_m < 0.0:
        details.update(
            {
                "surface_gap_gate_source": "bounded_surface_intervals_v1",
                "surface_gap_gate_reason": "endpoint_limit_malformed",
                "surface_gap_gate_count": len(ordered_cells),
            }
        )
        return None

    normalized_intervals: dict[
        FootprintCell,
        tuple[tuple[float, float], ...],
    ] = {}
    missing_interval_indices: list[int] = []
    missing_interval_cells: list[FootprintCell] = []
    for index, cell in enumerate(ordered_cells):
        intervals: list[tuple[float, float]] = []
        for raw_interval in vertical_gap_intervals.get(cell, ()):
            if len(raw_interval) != 2:
                continue
            try:
                low_y, high_y = sorted(
                    (float(raw_interval[0]), float(raw_interval[1]))
                )
            except (TypeError, ValueError):
                continue
            if (
                math.isfinite(low_y)
                and math.isfinite(high_y)
                and high_y > low_y + 1e-9
            ):
                intervals.append((low_y, high_y))
        normalized = tuple(sorted(dict.fromkeys(intervals)))
        if not normalized:
            missing_interval_indices.append(index)
            missing_interval_cells.append(cell)
            continue
        normalized_intervals[cell] = normalized
    if missing_interval_indices:
        details.update(
            {
                "surface_gap_gate_source": "bounded_surface_intervals_v1",
                "surface_gap_gate_reason": "bounded_intervals_missing",
                "surface_gap_gate_count": len(ordered_cells),
                "missing_surface_gap_gate_indices": missing_interval_indices,
                "missing_surface_gap_gate_cells": [
                    [int(value) for value in cell]
                    for cell in missing_interval_cells
                ],
            }
        )
        return None

    candidate_limit = max(1, int(max_candidates_per_gate))
    # Each interval retains a bounded local shortlist.  The final per-cell
    # merge first takes one candidate per interval, then fills the remaining
    # allowance by safety rank.  Memory is therefore independent of component
    # voxel count while vertically distinct passages remain represented.
    interval_heaps: dict[
        FootprintCell,
        list[
            list[
                tuple[
                    tuple[float, float, int, int, int],
                    tuple[float, float, CubicVoxelKey],
                    CubicVoxelKey,
                ]
            ]
        ],
    ] = {
        cell: [[] for _interval in intervals]
        for cell, intervals in normalized_intervals.items()
    }
    try:
        transition_limit_m = float(max_vertical_transition_m)
    except (TypeError, ValueError):
        transition_limit_m = 0.0
    if not math.isfinite(transition_limit_m) or transition_limit_m < 0.0:
        transition_limit_m = 0.0

    def interval_gap_m(
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> float:
        if first[1] < second[0]:
            return float(second[0] - first[1])
        if second[1] < first[0]:
            return float(first[0] - second[1])
        return 0.0

    # A transition candidate is tied to one interval in the current cell and
    # one continuity-compatible interval in one adjacent route cell.  Never
    # collapse every stacked interval in three cells into one min/max slab:
    # that can silently jump to an unrelated cave level.
    transition_envelopes: dict[
        FootprintCell,
        tuple[tuple[float, float, float, float], ...],
    ] = {}
    for index, cell in enumerate(ordered_cells):
        envelopes: set[tuple[float, float, float, float]] = set()
        for neighbor_index in (index - 1, index + 1):
            if not 0 <= neighbor_index < len(ordered_cells):
                continue
            neighbor_cell = ordered_cells[neighbor_index]
            step_limit_m = transition_limit_m * max(
                1.0,
                footprint_cell_distance(cell, neighbor_cell),
            )
            for own_interval in normalized_intervals[cell]:
                for neighbor_interval in normalized_intervals[neighbor_cell]:
                    if (
                        interval_gap_m(own_interval, neighbor_interval)
                        > step_limit_m + 1e-9
                    ):
                        continue
                    envelopes.add(
                        (
                            min(own_interval[0], neighbor_interval[0]),
                            max(own_interval[1], neighbor_interval[1]),
                            own_interval[0],
                            own_interval[1],
                        )
                    )
        transition_envelopes[cell] = tuple(sorted(envelopes))
    fallback_heaps: dict[
        FootprintCell,
        list[
            tuple[
                tuple[float, float, int, int, int],
                tuple[float, float, CubicVoxelKey],
                CubicVoxelKey,
            ]
        ],
    ] = {cell: [] for cell in ordered_cells}
    admitted_counts = {cell: 0 for cell in ordered_cells}
    transition_counts = {cell: 0 for cell in ordered_cells}
    vertical_margin_m = float(graph.vertical_voxel_size_m) * 0.5 + 1e-9
    route_index_by_cell = {
        cell: index for index, cell in enumerate(ordered_cells)
    }
    for key in graph.iter_keys():
        center = graph.voxel_center(key)
        cell = (
            math.floor(center[0] / footprint_cell_size),
            math.floor(center[2] / footprint_cell_size),
        )
        intervals = normalized_intervals.get(cell)
        if intervals is None:
            continue
        matched = False
        horizontal_center = footprint_world_center(cell, footprint_cell_size)
        horizontal_distance_squared = (
            (float(center[0]) - float(horizontal_center[0])) ** 2
            + (float(center[2]) - float(horizontal_center[1])) ** 2
        )
        for interval_index, (low_y, high_y) in enumerate(intervals):
            if not (
                low_y - vertical_margin_m
                <= float(center[1])
                <= high_y + vertical_margin_m
            ):
                continue
            matched = True
            interior_margin_m = min(
                float(center[1]) - low_y,
                high_y - float(center[1]),
            )
            route_index = route_index_by_cell[cell]
            if route_index == 0 and requested_source is not None:
                source_distance_m = math.dist(requested_source, center)
                if source_distance_m > source_limit_m + 1e-9:
                    continue
                rank = (
                    float(source_distance_m),
                    -float(interior_margin_m),
                    key,
                )
            elif (
                route_index == len(ordered_cells) - 1
                and requested_terminal is not None
            ):
                terminal_distance_m = _horizontal_point_distance_m(
                    requested_terminal,
                    center,
                )
                if terminal_distance_m > terminal_limit_m + 1e-9:
                    continue
                rank = (
                    float(terminal_distance_m),
                    -float(interior_margin_m),
                    key,
                )
            else:
                rank = (
                    -float(interior_margin_m),
                    float(horizontal_distance_squared),
                    key,
                )
            # The inverse token makes heap[0] the worst retained rank.
            inverse = (
                -float(rank[0]),
                -float(rank[1]),
                -int(key[0]),
                -int(key[1]),
                -int(key[2]),
            )
            entry = (inverse, rank, key)
            heap = interval_heaps[cell][interval_index]
            if len(heap) < candidate_limit:
                heapq.heappush(heap, entry)
            elif rank < heap[0][1]:
                heapq.heapreplace(heap, entry)
        if matched:
            admitted_counts[cell] += 1
        matching_envelopes = tuple(
            envelope
            for envelope in transition_envelopes[cell]
            if (
                envelope[0] - vertical_margin_m
                <= float(center[1])
                <= envelope[1] + vertical_margin_m
            )
        )
        if matching_envelopes:
            vertical_evidence_distance_m = min(
                0.0
                if own_low_y - vertical_margin_m
                <= float(center[1])
                <= own_high_y + vertical_margin_m
                else min(
                    abs(float(center[1]) - own_low_y),
                    abs(float(center[1]) - own_high_y),
                )
                for (
                    _transition_low_y,
                    _transition_high_y,
                    own_low_y,
                    own_high_y,
                ) in matching_envelopes
            )
            fallback_rank = (
                float(vertical_evidence_distance_m),
                float(horizontal_distance_squared),
                key,
            )
            fallback_inverse = (
                -float(vertical_evidence_distance_m),
                -float(horizontal_distance_squared),
                -int(key[0]),
                -int(key[1]),
                -int(key[2]),
            )
            fallback_entry = (fallback_inverse, fallback_rank, key)
            fallback_heap = fallback_heaps[cell]
            if len(fallback_heap) < candidate_limit:
                heapq.heappush(fallback_heap, fallback_entry)
            elif fallback_rank < fallback_heap[0][1]:
                heapq.heapreplace(fallback_heap, fallback_entry)
            transition_counts[cell] += 1

    groups: list[tuple[CubicVoxelKey, ...]] = []
    missing_key_indices: list[int] = []
    missing_key_cells: list[FootprintCell] = []
    transition_fallback_indices: list[int] = []
    transition_fallback_cells: list[FootprintCell] = []
    selected_counts: list[int] = []
    for index, cell in enumerate(ordered_cells):
        per_interval = [
            sorted(
                ((entry[1], entry[2]) for entry in heap),
                key=lambda item: item[0],
            )
            for heap in interval_heaps[cell]
            if heap
        ]
        if not per_interval:
            # Start and terminal must remain directly interval-backed.  An
            # intermediate coarse footprint cell may instead use selected
            # component voxels inside a pairwise, continuity-compatible
            # surface transition envelope. This handles a steep passage or
            # sparse OBJ column without opening the whole map Y range,
            # combining stacked layers, or consulting route-derived height.
            if 0 < index < len(ordered_cells) - 1 and fallback_heaps[cell]:
                fallback_values = sorted(
                    (
                        (entry[1], entry[2])
                        for entry in fallback_heaps[cell]
                    ),
                    key=lambda item: item[0],
                )
                group = tuple(key for _rank, key in fallback_values)
                groups.append(group)
                selected_counts.append(len(group))
                transition_fallback_indices.append(index)
                transition_fallback_cells.append(cell)
                continue
            missing_key_indices.append(index)
            missing_key_cells.append(cell)
            groups.append(())
            selected_counts.append(0)
            continue
        selected: list[CubicVoxelKey] = []
        selected_set: set[CubicVoxelKey] = set()
        # Preserve at least one candidate from every represented interval when
        # the configured gate allowance permits it.
        first_candidates = sorted(
            (values[0] for values in per_interval),
            key=lambda item: item[0],
        )
        for _rank, key in first_candidates:
            if key in selected_set:
                continue
            selected.append(key)
            selected_set.add(key)
            if len(selected) >= candidate_limit:
                break
        if len(selected) < candidate_limit:
            remaining = sorted(
                (
                    item
                    for values in per_interval
                    for item in values[1:]
                    if item[1] not in selected_set
                ),
                key=lambda item: item[0],
            )
            for _rank, key in remaining:
                if key in selected_set:
                    continue
                selected.append(key)
                selected_set.add(key)
                if len(selected) >= candidate_limit:
                    break
        group = tuple(selected)
        groups.append(group)
        selected_counts.append(len(group))

    details.update(
        {
            "surface_gap_gate_source": (
                "source_layer_pairwise_surface_intervals_v3"
            ),
            "surface_gap_gate_reason": (
                "component_candidates_missing"
                if missing_key_indices
                else "complete_with_pairwise_transition_bridge"
                if transition_fallback_indices
                else "complete"
            ),
            "surface_gap_gate_count": len(ordered_cells),
            "surface_gap_gate_candidate_limit": int(candidate_limit),
            "surface_gap_gate_candidate_count_min": min(
                selected_counts,
                default=0,
            ),
            "surface_gap_gate_candidate_count_max": max(
                selected_counts,
                default=0,
            ),
            "surface_gap_gate_truncated_count": sum(
                max(
                    int(admitted_counts[cell]),
                    int(transition_counts[cell]),
                )
                > int(selected_counts[index])
                for index, cell in enumerate(ordered_cells)
            ),
            "surface_gap_transition_fallback_indices": (
                transition_fallback_indices
            ),
            "surface_gap_transition_fallback_cells": [
                [int(value) for value in cell]
                for cell in transition_fallback_cells
            ],
            "surface_gap_max_vertical_transition_m": float(
                transition_limit_m
            ),
            "missing_surface_gap_gate_indices": missing_key_indices,
            "missing_surface_gap_gate_cells": [
                [int(value) for value in cell]
                for cell in missing_key_cells
            ],
        }
    )
    if missing_key_indices:
        return None
    return tuple(groups)


def _surface_gap_interval_terminal_keys(
    graph: SparseCubicVoxelGraph,
    *,
    terminal_cell: FootprintCell,
    vertical_gap_intervals: Mapping[
        FootprintCell,
        Sequence[tuple[float, float]],
    ],
    footprint_cell_size_m: float,
    terminal_point: Point | None = None,
    max_horizontal_distance_m: float | None = None,
) -> tuple[CubicVoxelKey, ...]:
    """Return every bounded interval-backed endpoint-component seed.

    This list is component-discovery evidence and must not be truncated by
    proximity before connectivity is known. Candidate bounding belongs to
    ``_surface_gap_interval_route_key_groups`` after one source-connected
    component has been selected.
    """
    try:
        footprint_cell_size = float(footprint_cell_size_m)
    except (TypeError, ValueError):
        return ()
    intervals = tuple(vertical_gap_intervals.get(terminal_cell, ()))
    if (
        not intervals
        or not math.isfinite(footprint_cell_size)
        or footprint_cell_size <= 0.0
    ):
        return ()
    normalized_intervals = tuple(
        sorted(
            {
                tuple(sorted((float(interval[0]), float(interval[1]))))
                for interval in intervals
                if len(interval) == 2
                and all(math.isfinite(float(value)) for value in interval)
                and abs(float(interval[1]) - float(interval[0])) > 1e-9
            }
        )
    )
    if not normalized_intervals:
        return ()
    requested_terminal = (
        _point_tuple(terminal_point) if terminal_point is not None else None
    )
    if terminal_point is not None and requested_terminal is None:
        return ()
    if max_horizontal_distance_m is None:
        horizontal_limit_m = math.inf
    else:
        try:
            horizontal_limit_m = float(max_horizontal_distance_m)
        except (TypeError, ValueError):
            return ()
        if not math.isfinite(horizontal_limit_m) or horizontal_limit_m < 0.0:
            return ()
    cell_center = footprint_world_center(terminal_cell, footprint_cell_size)
    vertical_margin_m = float(graph.vertical_voxel_size_m) * 0.5 + 1e-9
    ranked: list[
        tuple[
            tuple[float, float, float, CubicVoxelKey],
            CubicVoxelKey,
        ]
    ] = []
    for key in graph.iter_keys():
        center = graph.voxel_center(key)
        cell = (
            math.floor(center[0] / footprint_cell_size),
            math.floor(center[2] / footprint_cell_size),
        )
        if cell != terminal_cell:
            continue
        margins = tuple(
            min(float(center[1]) - low_y, high_y - float(center[1]))
            for low_y, high_y in normalized_intervals
            if (
                low_y - vertical_margin_m
                <= float(center[1])
                <= high_y + vertical_margin_m
            )
        )
        if not margins:
            continue
        endpoint_distance_m = (
            _horizontal_point_distance_m(requested_terminal, center)
            if requested_terminal is not None
            else 0.0
        )
        if endpoint_distance_m > horizontal_limit_m + 1e-9:
            continue
        horizontal_distance_squared = (
            (float(center[0]) - float(cell_center[0])) ** 2
            + (float(center[2]) - float(cell_center[1])) ** 2
        )
        ranked.append(
            (
                (
                    float(endpoint_distance_m),
                    -float(max(margins)),
                    float(horizontal_distance_squared),
                    key,
                ),
                key,
            )
        )
    return tuple(
        key
        for _rank, key in sorted(ranked, key=lambda item: item[0])
    )


def _component_vertical_gap_intervals(
    value: object,
    *,
    component_cells: set[FootprintCell],
) -> dict[FootprintCell, tuple[tuple[float, float], ...]]:
    """Parse bounded, surface-derived vertical-gap intervals.

    Each compact quadruple contains one footprint cell followed by the lower
    and upper Y boundaries of a bounded interval.  Any malformed entry makes
    the complete payload unusable so strict OBJ ingress cannot silently fall
    back to route-derived height evidence.
    """
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return {}
    if len(value) % 4 != 0:
        return {}
    parsed: dict[FootprintCell, list[tuple[float, float]]] = {}
    for index in range(0, len(value), 4):
        raw_x, raw_z, raw_low_y, raw_high_y = value[index : index + 4]
        if (
            type(raw_x) is not int
            or type(raw_z) is not int
            or type(raw_low_y) not in (int, float)
            or type(raw_high_y) not in (int, float)
        ):
            return {}
        cell = (int(raw_x), int(raw_z))
        low_y, high_y = sorted((float(raw_low_y), float(raw_high_y)))
        if (
            cell not in component_cells
            or not math.isfinite(low_y)
            or not math.isfinite(high_y)
            or high_y <= low_y + 1e-9
        ):
            return {}
        parsed.setdefault(cell, []).append((low_y, high_y))
    return {
        cell: tuple(sorted(dict.fromkeys(intervals)))
        for cell, intervals in parsed.items()
        if intervals
    }


def _source_connected_vertical_gap_layer(
    intervals_by_cell: Mapping[
        FootprintCell,
        Sequence[tuple[float, float]],
    ],
    *,
    route_cells: Sequence[FootprintCell],
    eligible_cells: set[FootprintCell],
    source_ingress_anchor: Point,
    cell_size: float,
    max_attachment_distance_m: float,
    max_vertical_transition_m: float,
) -> tuple[
    dict[FootprintCell, tuple[Point, ...]],
    dict[FootprintCell, tuple[float, float]],
]:
    """Select a complete surface-gap chain attached to OBJ vertex zero.

    Imported route points contribute only their ordered X/Z footprint cells;
    their Y coordinates are not an input.  Dynamic programming first chooses
    one bounded interval for every ordered route cell, starting with an
    interval whose closest point is within the immutable OBJ attachment cap.
    A multi-source interval-graph traversal then extends that proven layer to
    neighboring corridor cells. Missing or discontinuous evidence is omitted,
    never replaced by a route/global midpoint. Exact fixed-voxel and cached-
    mesh connectivity remains the final execution authority.
    """
    allowed = set(eligible_cells) & set(intervals_by_cell)
    ordered_route_cells = tuple(route_cells)
    if not allowed or not ordered_route_cells:
        return {}, {}
    if any(cell not in eligible_cells for cell in ordered_route_cells):
        return {}, {}
    if any(cell not in intervals_by_cell for cell in ordered_route_cells):
        return {}, {}

    source_cell = (
        math.floor(float(source_ingress_anchor[0]) / float(cell_size)),
        math.floor(float(source_ingress_anchor[2]) / float(cell_size)),
    )
    if (
        source_cell in allowed
        and source_cell != ordered_route_cells[0]
    ):
        ordered_route_cells = (source_cell, *ordered_route_cells)

    intervals = {
        cell: tuple(
            sorted(
                {
                    tuple(sorted((float(value[0]), float(value[1]))))
                    for value in intervals_by_cell[cell]
                    if len(value) == 2
                    and all(math.isfinite(float(item)) for item in value)
                    and abs(float(value[1]) - float(value[0])) > 1e-9
                }
            )
        )
        for cell in allowed
    }
    if any(not intervals.get(cell) for cell in ordered_route_cells):
        return {}, {}

    attachment_limit = max(0.0, float(max_attachment_distance_m))
    transition_limit = max(0.0, float(max_vertical_transition_m))

    def interval_gap(
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> float:
        if first[1] < second[0]:
            return float(second[0] - first[1])
        if second[1] < first[0]:
            return float(first[0] - second[1])
        return 0.0

    def interval_midpoint(value: tuple[float, float]) -> float:
        return (float(value[0]) + float(value[1])) * 0.5

    first_cell = ordered_route_cells[0]
    first_x, first_z = footprint_world_center(first_cell, cell_size)
    states: dict[
        int,
        tuple[tuple[float, float, float, float], tuple[int, ...]],
    ] = {}
    for candidate_index, candidate in enumerate(intervals[first_cell]):
        closest_y = min(
            max(float(source_ingress_anchor[1]), candidate[0]),
            candidate[1],
        )
        attachment_distance = math.dist(
            source_ingress_anchor,
            (float(first_x), float(closest_y), float(first_z)),
        )
        if attachment_distance > attachment_limit + 1e-9:
            continue
        states[candidate_index] = (
            (float(attachment_distance), 0.0, 0.0, 0.0),
            (candidate_index,),
        )
    if not states:
        return {}, {}

    previous_cell = first_cell
    for cell in ordered_route_cells[1:]:
        next_states: dict[
            int,
            tuple[tuple[float, float, float, float], tuple[int, ...]],
        ] = {}
        step_scale = max(
            1.0,
            footprint_cell_distance(previous_cell, cell),
        )
        maximum_gap = transition_limit * step_scale
        for candidate_index, candidate in enumerate(intervals[cell]):
            best: tuple[
                tuple[float, float, float, float],
                tuple[int, ...],
            ] | None = None
            for previous_index, (rank, path) in states.items():
                previous_interval = intervals[previous_cell][previous_index]
                gap = interval_gap(previous_interval, candidate)
                if gap > maximum_gap + 1e-9:
                    continue
                midpoint_change = abs(
                    interval_midpoint(candidate)
                    - interval_midpoint(previous_interval)
                )
                candidate_rank = (
                    rank[0],
                    max(rank[1], float(gap)),
                    rank[2] + float(gap),
                    rank[3] + float(midpoint_change),
                )
                candidate_value = (
                    candidate_rank,
                    (*path, candidate_index),
                )
                if best is None or candidate_value < best:
                    best = candidate_value
            if best is not None:
                next_states[candidate_index] = best
        if not next_states:
            return {}, {}
        states = next_states
        previous_cell = cell

    _rank, selected_path = min(states.values())
    route_selected: dict[
        FootprintCell,
        set[tuple[float, float]],
    ] = {}
    for cell, candidate_index in zip(
        ordered_route_cells,
        selected_path,
        strict=True,
    ):
        route_selected.setdefault(cell, set()).add(
            intervals[cell][candidate_index]
        )

    # Extend only through candidate intervals connected to the chosen route
    # chain. This prevents an independent midpoint in every component cell
    # from seeding a stacked passage or rock layer.
    distances: dict[
        tuple[FootprintCell, int],
        tuple[float, float],
    ] = {}
    frontier: list[
        tuple[float, float, FootprintCell, int]
    ] = []
    for cell, selected_intervals in route_selected.items():
        for selected_interval in selected_intervals:
            candidate_index = intervals[cell].index(selected_interval)
            key = (cell, candidate_index)
            distances[key] = (0.0, 0.0)
            heapq.heappush(frontier, (0.0, 0.0, cell, candidate_index))

    while frontier:
        distance_m, accumulated_gap, cell, candidate_index = heapq.heappop(
            frontier
        )
        key = (cell, candidate_index)
        if (distance_m, accumulated_gap) != distances.get(key):
            continue
        candidate = intervals[cell][candidate_index]
        for neighbor in navigable_footprint_neighbors(cell, allowed):
            horizontal_step = (
                footprint_cell_distance(cell, neighbor) * float(cell_size)
            )
            maximum_gap = transition_limit * max(
                1.0,
                footprint_cell_distance(cell, neighbor),
            )
            for neighbor_index, neighbor_interval in enumerate(
                intervals[neighbor]
            ):
                gap = interval_gap(candidate, neighbor_interval)
                if gap > maximum_gap + 1e-9:
                    continue
                next_rank = (
                    distance_m + horizontal_step + float(gap),
                    accumulated_gap + float(gap),
                )
                neighbor_key = (neighbor, neighbor_index)
                if next_rank >= distances.get(
                    neighbor_key,
                    (math.inf, math.inf),
                ):
                    continue
                distances[neighbor_key] = next_rank
                heapq.heappush(
                    frontier,
                    (*next_rank, neighbor, neighbor_index),
                )

    selected_intervals_by_cell: dict[
        FootprintCell,
        tuple[tuple[float, float], ...],
    ] = {}
    for cell in sorted(allowed):
        forced = route_selected.get(cell)
        if forced:
            selected_intervals_by_cell[cell] = tuple(sorted(forced))
            continue
        candidates = tuple(
            (distances[(cell, index)], index)
            for index in range(len(intervals[cell]))
            if (cell, index) in distances
        )
        if not candidates:
            continue
        _candidate_rank, candidate_index = min(candidates)
        selected_intervals_by_cell[cell] = (
            intervals[cell][candidate_index],
        )

    seed_points: dict[FootprintCell, tuple[Point, ...]] = {}
    y_ranges: dict[FootprintCell, tuple[float, float]] = {}
    for cell, selected_intervals in selected_intervals_by_cell.items():
        x, z = footprint_world_center(cell, cell_size)
        seed_points[cell] = tuple(
            (float(x), interval_midpoint(value), float(z))
            for value in selected_intervals
        )
        y_ranges[cell] = (
            min(value[0] for value in selected_intervals),
            max(value[1] for value in selected_intervals),
        )
    return seed_points, y_ranges


def _route_transition_sampling_y_ranges(
    selected_ranges: Mapping[FootprintCell, tuple[float, float]],
    *,
    route_cells: Sequence[FootprintCell],
) -> dict[FootprintCell, tuple[float, float]]:
    """Add bounded surface-derived support for steep route transitions.

    ``selected_ranges`` remains the immutable gap-layer proposal used for
    entrance attachment.  A coarse footprint step can nevertheless climb
    farther than one 0.25 m execution voxel.  For rasterization only, widen
    both non-entrance cells touched by the specific interval pair. A diagonal
    footprint step also widens every available cardinal support cell so the
    later six-connected voxel proof has physical intermediate cells.

    No imported route height participates.  Surface occupancy, one global
    source-to-terminal component, and exact cached-mesh checks still decide
    whether any proposed transition is executable.
    """
    original = {
        cell: tuple(sorted((float(value[0]), float(value[1]))))
        for cell, value in selected_ranges.items()
        if len(value) == 2
        and all(math.isfinite(float(item)) for item in value)
        and abs(float(value[1]) - float(value[0])) > 1e-9
    }
    ordered = tuple(route_cells)
    if not original or not ordered or any(cell not in original for cell in ordered):
        return {}
    expanded = dict(original)
    source_cell = ordered[0]

    def widen(cell: FootprintCell, low_y: float, high_y: float) -> None:
        if cell == source_cell:
            return
        existing = expanded[cell]
        expanded[cell] = (
            min(float(existing[0]), float(low_y)),
            max(float(existing[1]), float(high_y)),
        )

    for previous, current in zip(ordered[:-1], ordered[1:], strict=True):
        delta_x = int(current[0]) - int(previous[0])
        delta_z = int(current[1]) - int(previous[1])
        if delta_x == 0 and delta_z == 0:
            continue
        if max(abs(delta_x), abs(delta_z)) != 1:
            return {}
        previous_range = original[previous]
        current_range = original[current]
        bridge_low = min(previous_range[0], current_range[0])
        bridge_high = max(previous_range[1], current_range[1])
        widen(previous, bridge_low, bridge_high)
        widen(current, bridge_low, bridge_high)
        if delta_x == 0 or delta_z == 0:
            continue
        supports = tuple(
            cell
            for cell in (
                (previous[0], current[1]),
                (current[0], previous[1]),
            )
            if cell in original
        )
        if not supports:
            return {}
        for support in supports:
            widen(support, bridge_low, bridge_high)
    # A later crossing must never broaden the immutable entrance evidence.
    expanded[source_cell] = original[source_cell]
    return expanded


def _fallback_y_range(
    manifest: Mapping[str, object],
) -> tuple[float, float]:
    """Return mesh chunk Y bounds without consulting imported route Y."""
    values: list[float] = []
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
    if _uses_imported_navigation_start_anchor(navigation_metadata):
        source_anchor = _imported_navigation_start_anchor(
            navigation_metadata
        )
        assert source_anchor is not None
        certified_non_circular = [
            item
            for item in built
            if _is_exact_source_anchor_route(
                route_by_id.get(item[0]),
                item[1],
                source_anchor=source_anchor,
            )
        ]
        if not certified_non_circular:
            return None
        selected = min(
            certified_non_circular,
            key=_longest_safe_route_rank,
        )[0]
        return (
            None
            if _longer_unresolved_route_reason(
                navigation_metadata,
                summaries,
                selected_route_id=selected,
                require_navigation_start=False,
                require_source_anchor=True,
            ) is not None
            else selected
        )
    if _uses_navigation_start(navigation_metadata):
        navigation_start = _navigation_start_position(navigation_metadata)
        assert navigation_start is not None
        certified_non_circular = [
            item
            for item in built
            if _is_exact_navigation_start_route(
                route_by_id.get(item[0]),
                item[1],
                source_start=navigation_start,
            )
        ]
        if not certified_non_circular:
            return None
        selected = min(
            certified_non_circular,
            key=_longest_safe_route_rank,
        )[0]
        return (
            None
            if _longer_unresolved_route_reason(
                navigation_metadata,
                summaries,
                selected_route_id=selected,
                require_navigation_start=True,
                require_source_anchor=False,
            ) is not None
            else selected
        )
    certified_non_circular = [
        item
        for item in built
        if route_by_id.get(item[0], {}).get("closed_loop") is False
    ]
    if not certified_non_circular:
        return None
    selected = min(certified_non_circular, key=_longest_safe_route_rank)[0]
    return (
        None
        if _longer_unresolved_route_reason(
            navigation_metadata,
            summaries,
            selected_route_id=selected,
            require_navigation_start=False,
            require_source_anchor=False,
        ) is not None
        else selected
    )


def _longer_unresolved_route_reason(
    navigation_metadata: Mapping[str, object],
    summaries: Mapping[str, Mapping[str, object]],
    *,
    selected_route_id: str,
    require_navigation_start: bool,
    require_source_anchor: bool,
) -> str | None:
    """Explain why a longer candidate prevents a short recommendation.

    Exhausting a bounded exact-search budget is not evidence that a route is
    unsafe. Treating it as such recreated the historical short-dive failure:
    a 143 m route displaced a source-connected multi-kilometre candidate whose
    roadmap merely hit its node cap. Missing ordered surface-gap evidence is
    likewise an unresolved cache proof. A conclusively rejected mesh route
    may still yield to the longest route that did certify.
    """
    routes = navigation_metadata.get("routes")
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        return None
    route_by_id = {
        str(route.get("id")): route
        for route in routes
        if isinstance(route, Mapping) and route.get("id") is not None
    }
    selected_route = route_by_id.get(str(selected_route_id))
    selected_summary = summaries.get(str(selected_route_id), {})
    selected_length_m = _route_candidate_length_m(
        selected_route,
        selected_summary,
    )
    for route_id, summary in summaries.items():
        if str(route_id) == str(selected_route_id):
            continue
        route = route_by_id.get(str(route_id))
        if route is None or route.get("closed_loop") is not False:
            continue
        if (
            require_navigation_start
            and route.get("starts_at_navigation_start") is not True
        ):
            continue
        if (
            require_source_anchor
            and route.get("starts_at_navigation_start_anchor") is not True
        ):
            continue
        candidate_length_m = _route_candidate_length_m(route, summary)
        if candidate_length_m <= selected_length_m + 1e-6:
            continue
        if _route_exact_search_capacity_limited(summary):
            return "longer_route_search_capacity_limited"
        if _route_exact_search_evidence_missing(summary):
            return "longer_route_ordering_evidence_missing"
    return None


def _route_candidate_length_m(
    route: Mapping[str, object] | None,
    summary: Mapping[str, object],
) -> float:
    """Return a finite source-span estimate for unresolved-route ordering."""
    candidates: list[float] = []
    for source, field_name in (
        (route, "length_m"),
        (summary, "certified_route_length_m"),
        (summary, "route_length_m"),
    ):
        if source is None:
            continue
        try:
            value = float(source.get(field_name, 0.0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0.0:
            candidates.append(value)
    return max(candidates, default=0.0)


def _route_exact_search_capacity_limited(
    summary: Mapping[str, object],
) -> bool:
    """Return whether the authoritative exact route search was unresolved."""
    mesh_details = summary.get("prepared_mesh_graph")
    if not isinstance(mesh_details, Mapping):
        return False
    if mesh_details.get("node_limit_reached") is True:
        return True
    reason = str(mesh_details.get("reason", "")).lower()
    if "limit_reached" in reason or "capacity" in reason:
        return True
    direct_attempt = mesh_details.get("exact_cubic_spine_attempt")
    if isinstance(direct_attempt, Mapping):
        return _route_exact_search_capacity_limited(
            {"prepared_mesh_graph": direct_attempt}
        )
    return False


def _route_exact_search_evidence_missing(
    summary: Mapping[str, object],
) -> bool:
    """Return whether a route failed because its ordered proof was absent."""
    mesh_details = summary.get("prepared_mesh_graph")
    if not isinstance(mesh_details, Mapping):
        return True
    reason = str(mesh_details.get("reason", "")).lower()
    if any(
        token in reason
        for token in (
            "surface_gap_waypoints_missing",
            "waypoint_evidence_missing",
            "spine_inputs_missing",
        )
    ):
        return True
    direct_attempt = mesh_details.get("exact_cubic_spine_attempt")
    if isinstance(direct_attempt, Mapping):
        return _route_exact_search_evidence_missing(
            {"prepared_mesh_graph": direct_attempt}
        )
    return False


def first_manifest_chunk_center_for_route_contract(
    manifest_chunks: object,
) -> Point | None:
    """Return the minimum spatial chunk center without importing the builder."""
    if not isinstance(manifest_chunks, Mapping) or not manifest_chunks:
        return None
    numeric_chunks: list[tuple[tuple[int, int, int], str, object]] = []
    for raw_key, info in manifest_chunks.items():
        parts = str(raw_key).replace(",", "_").split("_")
        if len(parts) != 3:
            continue
        try:
            cell = tuple(int(part) for part in parts)
        except ValueError:
            continue
        numeric_chunks.append((cell, str(raw_key), info))
    if numeric_chunks:
        _cell, _key, info = min(
            numeric_chunks,
            key=lambda item: (item[0], item[1]),
        )
    else:
        info = next(iter(manifest_chunks.values()))
    if not isinstance(info, Mapping):
        return None
    minimum = _point_tuple(info.get("bounds_min"))
    maximum = _point_tuple(info.get("bounds_max"))
    if (
        minimum is None
        or maximum is None
        or any(maximum[axis] < minimum[axis] for axis in range(3))
    ):
        return None
    return tuple(
        (minimum[axis] + maximum[axis]) * 0.5
        for axis in range(3)
    )  # type: ignore[return-value]


def navigation_route_contract_rebuild_reason(
    navigation_metadata: Mapping[str, object],
    *,
    manifest_chunks: object = None,
) -> str | None:
    """Return a cheap manifest-aware reason that route metadata is stale.

    This does not replace full artifact certification. It prevents the viewer
    from executing known-invalid V12 states before a user explicitly runs the
    verifier: an unbound/mismatched entrance, malformed OBJ-order provenance,
    a missing recommendation, or a short recommendation chosen while a longer
    source-connected route's exact search was capacity-limited.
    """
    raw_start_present = "navigation_start" in navigation_metadata
    declared_start = _navigation_start_position(navigation_metadata)
    raw_anchor = navigation_metadata.get("navigation_start_anchor")
    has_anchor = raw_anchor is not None
    if raw_start_present and declared_start is None:
        return "navigation_start_malformed"
    if has_anchor and declared_start is not None:
        return "navigation_start_policy_ambiguous"
    inferred_start = first_manifest_chunk_center_for_route_contract(
        manifest_chunks
    )
    if has_anchor:
        try:
            parsed_anchor = _imported_navigation_start_anchor(
                navigation_metadata
            )
        except (TypeError, ValueError):
            parsed_anchor = None
        if parsed_anchor is None:
            return "navigation_start_anchor_malformed"
    elif declared_start is None:
        return "navigation_start_missing"
    else:
        raw_start = navigation_metadata.get("navigation_start")
        start_source = (
            raw_start.get("source")
            if isinstance(raw_start, Mapping)
            else None
        )
        if start_source == "first_manifest_chunk_center_v1":
            if inferred_start is None:
                return "navigation_inferred_start_unavailable"
            if any(
                abs(declared_start[axis] - inferred_start[axis]) > 1e-6
                for axis in range(3)
            ):
                return "navigation_inferred_start_mismatch"

    if (
        navigation_metadata.get("route_selection_method")
        != NAVIGATION_ROUTE_SELECTION_LONGEST_SAFE_NON_CIRCULAR
    ):
        return "navigation_route_selection_method_invalid"
    selected_route_id = navigation_metadata.get("recommended_route_id")
    if not isinstance(selected_route_id, str) or not selected_route_id:
        return "recommended_route_missing"
    routes = navigation_metadata.get("routes")
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        return "navigation_routes_missing"
    summaries = {
        str(route.get("id")): route["voxel_corridor"]
        for route in routes
        if isinstance(route, Mapping)
        and route.get("id") is not None
        and isinstance(route.get("voxel_corridor"), Mapping)
    }
    if selected_route_id not in summaries:
        return "recommended_route_invalid"
    unresolved_reason = _longer_unresolved_route_reason(
        navigation_metadata,
        summaries,
        selected_route_id=selected_route_id,
        require_navigation_start=_uses_navigation_start(navigation_metadata),
        require_source_anchor=_uses_imported_navigation_start_anchor(
            navigation_metadata
        ),
    )
    if unresolved_reason is not None:
        return unresolved_reason
    return None


def _uses_imported_navigation_start_anchor(
    navigation_metadata: Mapping[str, object],
) -> bool:
    """Return whether strict OBJ-order route selection is requested."""
    return _imported_navigation_start_anchor(navigation_metadata) is not None


def _uses_navigation_start(
    navigation_metadata: Mapping[str, object],
) -> bool:
    """Return whether a finite executable map start is requested."""
    return _navigation_start_position(navigation_metadata) is not None


def _is_exact_navigation_start_route(
    route: Mapping[str, object] | None,
    summary: Mapping[str, object],
    *,
    source_start: Point,
) -> bool:
    """Require an exact-safe non-circular route from the map start."""
    return bool(
        route is not None
        and route.get("starts_at_navigation_start") is True
        and isinstance(summary.get("prepared_mesh_graph"), Mapping)
        and summary["prepared_mesh_graph"].get(
            "source_ingress_connector_mesh_clear"
        )
        is True
        and summary["prepared_mesh_graph"].get(
            "source_ingress_connector_required"
        )
        is True
        and summary["prepared_mesh_graph"].get(
            "source_ingress_attachment_mode"
        )
        == "executable_authored_start_connector"
        and _exact_ingress_attachment_matches(
            route,
            summary["prepared_mesh_graph"],
            source_point=source_start,
        )
        and _is_exact_ingress_route(route, summary)
    )


def _is_exact_source_anchor_route(
    route: Mapping[str, object] | None,
    summary: Mapping[str, object],
    *,
    source_anchor: Point,
) -> bool:
    """Require a bounded exact ingress attachment and a non-circular path."""
    if route is None:
        return False
    graph_details = summary.get("prepared_mesh_graph")
    return bool(
        isinstance(graph_details, Mapping)
        and route.get("starts_at_navigation_start_anchor") is True
        and graph_details.get("source_ingress_connector_required") is False
        and graph_details.get("source_ingress_attachment_mode")
        == "non_executable_obj_surface_anchor_snap"
        and _exact_ingress_attachment_matches(
            route,
            graph_details,
            source_point=source_anchor,
        )
        and _is_exact_ingress_route(route, summary)
    )


def _exact_ingress_attachment_matches(
    route: Mapping[str, object],
    graph_details: Mapping[str, object],
    *,
    source_point: Point,
) -> bool:
    """Match certified route start to its bounded source attachment proof."""
    recorded_source = _point_tuple(graph_details.get("source_ingress_point"))
    attachment = _point_tuple(
        graph_details.get("source_ingress_attachment_point")
    )
    certified_start = _point_tuple(route.get("certified_start_position"))
    try:
        recorded_distance_m = float(
            graph_details["source_ingress_attachment_distance_m"]
        )
        snap_limit_m = float(graph_details["source_ingress_snap_limit_m"])
    except (KeyError, TypeError, ValueError):
        return False
    if (
        graph_details.get("source_ingress_coordinate_space") != "xyz"
        or recorded_source is None
        or attachment is None
        or certified_start is None
    ):
        return False
    actual_distance_m = math.dist(source_point, attachment)
    return bool(
        np.allclose(recorded_source, source_point, rtol=0.0, atol=1e-6)
        and np.allclose(attachment, certified_start, rtol=0.0, atol=1e-6)
        and math.isfinite(recorded_distance_m)
        and recorded_distance_m >= 0.0
        and math.isfinite(snap_limit_m)
        and snap_limit_m > 0.0
        and snap_limit_m <= MAX_OBJ_SOURCE_INGRESS_SNAP_DISTANCE_M + 1e-9
        and actual_distance_m <= snap_limit_m + 1e-9
        and abs(actual_distance_m - recorded_distance_m) <= 1e-6
    )


def _is_exact_ingress_route(
    route: Mapping[str, object],
    summary: Mapping[str, object],
) -> bool:
    """Return whether one open route has a bounded certified ingress."""
    if route.get("closed_loop") is not False:
        return False
    graph_details = summary.get("prepared_mesh_graph")
    if not isinstance(graph_details, Mapping):
        return False
    if graph_details.get("source_ingress_required") is not True:
        return False
    if _point_tuple(graph_details.get("source_ingress_attachment_point")) is None:
        return False
    try:
        attachment_distance_m = float(
            graph_details["source_ingress_attachment_distance_m"]
        )
        attachment_limit_m = float(
            graph_details["source_ingress_snap_limit_m"]
        )
        route_length_m = float(summary["route_length_m"])
        terminal_graph_distance_m = float(
            graph_details["terminal_graph_distance_m"]
        )
        source_route_point_count = int(summary["source_route_point_count"])
        certified_ingress_hint_index = int(
            summary["certified_ingress_hint_index"]
        )
        certified_terminal_hint_index = int(
            summary["certified_terminal_hint_index"]
        )
        selected_source_hint_start_index = int(
            summary["selected_source_hint_start_index"]
        )
        selected_source_hint_end_index = int(
            summary["selected_source_hint_end_index"]
        )
    except (KeyError, TypeError, ValueError):
        return False
    return (
        math.isfinite(attachment_distance_m)
        and attachment_distance_m >= 0.0
        and math.isfinite(attachment_limit_m)
        and attachment_limit_m > 0.0
        and attachment_limit_m
        <= MAX_OBJ_SOURCE_INGRESS_SNAP_DISTANCE_M + 1e-9
        and attachment_distance_m <= attachment_limit_m + 1e-9
        and math.isfinite(route_length_m)
        and route_length_m > 0.0
        and math.isfinite(terminal_graph_distance_m)
        and terminal_graph_distance_m > 0.0
        and source_route_point_count >= 2
        and certified_ingress_hint_index == 0
        and certified_terminal_hint_index == source_route_point_count - 1
        and selected_source_hint_start_index == 0
        and selected_source_hint_end_index == source_route_point_count - 1
        and summary.get("complete_ingress_route") is True
    )


def _longest_safe_route_rank(
    item: tuple[str, Mapping[str, object]],
) -> tuple[float, float, float, float, float, str]:
    """Rank exact-safe routes by length, then conservative comfort evidence."""
    route_id, summary = item

    def finite_metric(field_name: str) -> float:
        try:
            value = float(summary.get(field_name, 0.0))
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, value) if math.isfinite(value) else 0.0

    return (
        -finite_metric("route_length_m"),
        -finite_metric("min_clearance_m"),
        -finite_metric("mean_clearance_m"),
        -finite_metric("volume_per_route_m"),
        -finite_metric("available_volume_m3"),
        route_id,
    )


def _publish_selected_route_method(
    navigation_metadata: Mapping[str, object],
    *,
    route_id: str,
    selection_method: str,
) -> None:
    """Label only the route selected after exact certification."""
    routes = navigation_metadata.get("routes")
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        return
    for route in routes:
        if isinstance(route, dict) and str(route.get("id")) == route_id:
            route["selection_method"] = selection_method
            return


def _supported_cache_identity(version: object, method: object) -> bool:
    """Accept current prepared graphs and readable older sidecars."""
    return (version, method) in {
        (NAVIGATION_VOXEL_CACHE_VERSION, NAVIGATION_VOXEL_CACHE_METHOD),
        (
            _PREVIOUS_NAVIGATION_VOXEL_CACHE_VERSION,
            _PREVIOUS_NAVIGATION_VOXEL_CACHE_METHOD,
        ),
        (
            _V10_NAVIGATION_VOXEL_CACHE_VERSION,
            _V10_NAVIGATION_VOXEL_CACHE_METHOD,
        ),
        (
            _V9_NAVIGATION_VOXEL_CACHE_VERSION,
            _V9_NAVIGATION_VOXEL_CACHE_METHOD,
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
        "vertical_voxel_size_m": float(config.vertical_voxel_size_m),
        "voxel_cell_size_m": [
            float(config.voxel_size_m),
            float(config.vertical_voxel_size_m),
            float(config.voxel_size_m),
        ],
        "curvature_method": CURVATURE_PROFILE_METHOD,
        "tile_size_m": float(config.tile_size_m),
        "max_tiles": int(config.max_tiles),
        "fine_voxel_size_m": float(config.fine_voxel_size_m),
        "fine_vertical_voxel_size_m": float(
            config.fine_vertical_voxel_size_m
        ),
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
        "route_corridor_radius_m": float(config.route_corridor_radius_m),
        "cubic_component_max_voxels": int(
            config.cubic_component_max_voxels
        ),
        "mesh_navigation_graph_method": MESH_NAVIGATION_GRAPH_METHOD,
        "graph_routing_authority": "prepared_mesh_free_space_graph",
        "cache_quality_profile": "fixed_orthogonal_terminal_route_v1",
        "coverage_scope": "certified_terminal_route",
        "fixed_voxel_method": FIXED_ORTHOGONAL_VOXEL_METHOD,
        "fixed_isotropic_voxel_size_m": float(config.voxel_size_m),
        "fixed_vertical_voxel_size_m": float(config.vertical_voxel_size_m),
        "fixed_voxel_cell_size_m": [
            float(config.voxel_size_m),
            float(config.vertical_voxel_size_m),
            float(config.voxel_size_m),
        ],
        "surface_overlap_policy": "occupied_wins",
        "sampling_complete_required": True,
        "navigation_graph_method": NAVIGATION_VOXEL_GRAPH_METHOD,
        "branch_lookahead_method": NAVIGATION_VOXEL_BRANCH_LOOKAHEAD_METHOD,
        "routes": {},
    }


def _route_id(route: Mapping[str, object], index: int) -> str:
    value = route.get("id")
    return str(value) if value is not None else f"centerline-{index}"


def _imported_navigation_start_anchor(
    navigation_metadata: Mapping[str, object],
) -> Point | None:
    """Return the non-executable OBJ ingress anchor for prototype caches."""
    field_name = "navigation_start_anchor"
    if field_name not in navigation_metadata:
        return None
    value = navigation_metadata[field_name]
    if not isinstance(value, Mapping):
        raise ValueError("OBJ navigation start anchor must be an object")
    expected_fields = {
        "position",
        "kind",
        "source",
        "source_vertex_index",
        "source_order",
        "executable",
        "attachment_required",
        "attachment_coordinate_space",
    }
    if set(value) != expected_fields:
        raise ValueError("OBJ navigation start anchor schema is malformed")
    expected_strings = {
        "kind": "obj_surface_vertex",
        "source_order": "obj_declaration_order",
        "attachment_coordinate_space": "xyz",
    }
    string_policy_valid = all(
        type(value[field]) is str and value[field] == expected
        for field, expected in expected_strings.items()
    )
    if (
        not string_policy_valid
        or value["executable"] is not False
        or value["attachment_required"] is not True
    ):
        raise ValueError("OBJ navigation start anchor policy is malformed")
    source = value["source"]
    source_vertex_index = value["source_vertex_index"]
    if (
        type(source) is not str
        or not source.strip()
        or type(source_vertex_index) is not int
    ):
        raise ValueError("OBJ navigation start anchor provenance is malformed")
    if source_vertex_index != 0:
        raise ValueError("OBJ navigation start anchor is not the first vertex")
    position = value["position"]
    if (
        type(position) is not list
        or len(position) != 3
        or any(type(coordinate) not in (int, float) for coordinate in position)
    ):
        raise ValueError("OBJ navigation start anchor position is malformed")
    point = tuple(float(coordinate) for coordinate in position)
    if not all(math.isfinite(coordinate) for coordinate in point):
        raise ValueError("OBJ navigation start anchor position is malformed")
    return point  # type: ignore[return-value]


def _navigation_start_position(
    navigation_metadata: Mapping[str, object],
) -> Point | None:
    """Return a finite regular navigation-start position, if present."""
    value = navigation_metadata.get("navigation_start")
    if isinstance(value, Mapping):
        value = value.get("position")
    return _point_tuple(value)


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


def _publish_certified_complete_route(
    route: dict[str, object],
    value: object,
    *,
    source_point_offset: int,
) -> None:
    """Publish only an exact route that begins at source hint zero."""
    if int(source_point_offset) != 0:
        return
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return
    points: list[Point] = []
    for item in value:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)):
            return
        if len(item) != 3:
            return
        try:
            point = tuple(float(coordinate) for coordinate in item)
        except (TypeError, ValueError):
            return
        if not all(math.isfinite(coordinate) for coordinate in point):
            return
        points.append(point)  # type: ignore[arg-type]
    if len(points) < 2:
        return

    original_points = _route_points(route)
    offset = max(0, int(source_point_offset))
    if not original_points or offset >= len(original_points):
        return
    cell_size = _positive_float(
        route.get("footprint_cell_size"),
        "route footprint cell size",
    )
    certified_cells = tuple(
        (
            math.floor(point[0] / cell_size),
            math.floor(point[2] / cell_size),
        )
        for point in points
    )

    route["points"] = [coordinate for point in points for coordinate in point]
    route["cells"] = [coordinate for cell in certified_cells for coordinate in cell]
    route["length_m"] = float(
        sum(
            math.dist(first, second)
            for first, second in zip(points, points[1:], strict=False)
        )
    )
    route["certified_ingress_hint_index"] = int(offset)
    route["source_route_point_count"] = len(original_points)
    route["point_source"] = "prepared_mesh_graph_path_v1"
    route["certified_start_position"] = [
        float(coordinate) for coordinate in points[0]
    ]

    route.pop("y_ranges", None)
    route.pop("clearance_margins", None)


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
