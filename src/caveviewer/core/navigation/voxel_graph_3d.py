"""Bounded true-3D connectivity data for cached cave navigation.

The surface atlas stores sparse occupied voxels and performs a bounded flood
fill from route seeds. This module turns the resulting free-space samples into
coarser navigation voxels with independent X, Y, and Z coordinates. The
runtime can therefore distinguish stacked passages that share one footprint
cell without carrying the full raw voxel volume into route search.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math

from caveviewer.core.navigation.centerline import FootprintCell, Point


VoxelGraphKey = tuple[int, int, int]
GridSize = float | Sequence[float]

NAVIGATION_VOXEL_3D_GRAPH_METHOD = "heading_aware_true_3d_voxel_graph_v4"
NAVIGATION_VOXEL_3D_GRAPH_VERSION = 4
# Guided Dive now keeps a substantially denser graph in the cache. The graph
# is sparse (only filled navigable voxels become nodes), so this is a cap on
# useful navigation evidence rather than a dense world-volume allocation.
DEFAULT_3D_GRAPH_MAX_NODES = 262_144
DEFAULT_3D_GRAPH_MAX_EDGES = 1_048_576
# Keep any-angle shortcuts local enough that branch scoring still exposes
# meaningful alternatives at the 1 m cache resolution. Longer passages are
# represented by a sequence of prepared graph edges rather than one large
# leap, which also keeps replanning granularity predictable.
DEFAULT_3D_GRAPH_MAX_EDGE_DISTANCE_CELLS = 4
DEFAULT_3D_GRAPH_MAX_EDGES_PER_NODE = 24
DEFAULT_3D_GRAPH_DIRECTION_BINS = 32
DEFAULT_3D_GRAPH_MAX_VERTICAL_GRID_SIZE_M = 4.0
DEFAULT_3D_GRAPH_MAX_VERTICAL_EDGE_DISTANCE_M = 4.0
DEFAULT_3D_GRAPH_MAX_EDGE_DISTANCE_M = 24.0
DEFAULT_3D_GRAPH_MAX_VERTICAL_COARSENING_FACTOR = 4

_CARDINAL_OFFSETS = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
    (0, 0, 1),
    (0, 0, -1),
)

_NEIGHBOR_OFFSETS_26 = tuple(
    (dx, dy, dz)
    for dx in (-1, 0, 1)
    for dy in (-1, 0, 1)
    for dz in (-1, 0, 1)
    if (dx, dy, dz) != (0, 0, 0)
)


@dataclass(frozen=True)
class NavigationVoxel3DMetric:
    """Aggregated free-space evidence for one 3D navigation voxel."""

    center: Point
    footprint_cell: FootprintCell
    available_volume_m3: float
    free_voxel_count: int
    min_clearance_m: float
    mean_clearance_m: float
    progress_m: float


@dataclass(frozen=True)
class NavigationVoxel3DNode:
    """Prepared topology and route evidence for one true-3D node."""

    key: VoxelGraphKey
    center: Point
    footprint_cell: FootprintCell
    component_id: int
    progress_m: float
    connectivity_score: float
    local_degree: int
    dead_end: bool
    terminal: bool
    unknown_boundary: bool
    available_volume_m3: float
    min_clearance_m: float
    mean_clearance_m: float
    preferred_neighbors: tuple[VoxelGraphKey, ...] = ()

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "key": [int(value) for value in self.key],
            "center": [float(value) for value in self.center],
            "footprint_cell": [
                int(self.footprint_cell[0]),
                int(self.footprint_cell[1]),
            ],
            "component_id": int(self.component_id),
            "progress_m": float(self.progress_m),
            "connectivity_score": float(self.connectivity_score),
            "local_degree": int(self.local_degree),
            "dead_end": bool(self.dead_end),
            "terminal": bool(self.terminal),
            "unknown_boundary": bool(self.unknown_boundary),
            "available_volume_m3": float(self.available_volume_m3),
            "min_clearance_m": float(self.min_clearance_m),
            "mean_clearance_m": float(self.mean_clearance_m),
            "preferred_neighbor_count": len(self.preferred_neighbors),
        }


@dataclass(frozen=True)
class NavigationVoxel3DEdge:
    """One directed line-of-sight edge in the prepared 3D graph."""

    source: VoxelGraphKey
    target: VoxelGraphKey
    distance_m: float
    direction: Point
    min_clearance_m: float
    line_of_sight: bool = True

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "source": [int(value) for value in self.source],
            "target": [int(value) for value in self.target],
            "distance_m": float(self.distance_m),
            "direction": [float(value) for value in self.direction],
            "min_clearance_m": float(self.min_clearance_m),
            "line_of_sight": bool(self.line_of_sight),
        }


@dataclass(frozen=True)
class NavigationVoxel3DGraph:
    """Bounded prepared graph over free 3D navigation voxels."""

    nodes: Mapping[VoxelGraphKey, NavigationVoxel3DNode]
    edges: Mapping[VoxelGraphKey, tuple[NavigationVoxel3DEdge, ...]]
    component_count: int
    grid_size_m: tuple[float, float, float]
    max_edge_distance_cells: int
    max_edges_per_node: int
    max_edge_distance_m: float = DEFAULT_3D_GRAPH_MAX_EDGE_DISTANCE_M
    max_vertical_edge_distance_m: float = (
        DEFAULT_3D_GRAPH_MAX_VERTICAL_EDGE_DISTANCE_M
    )
    method: str = NAVIGATION_VOXEL_3D_GRAPH_METHOD

    @property
    def edge_count(self) -> int:
        return sum(len(edges) for edges in self.edges.values())

    @property
    def routable_node_count(self) -> int:
        """Return nodes with at least one valid in-component edge."""
        return sum(
            1
            for key in self.nodes
            if any(
                edge.line_of_sight
                and edge.source == key
                and edge.target in self.nodes
                and self.nodes[edge.target].component_id
                == self.nodes[key].component_id
                for edge in self.outgoing(key)
            )
        )

    @property
    def edge_integrity_safe(self) -> bool:
        """Return whether persisted edges match the graph topology."""
        if not self.nodes:
            return False
        declared_components = {
            int(node.component_id) for node in self.nodes.values()
        }
        if not declared_components or self.component_count < len(
            declared_components
        ):
            return False
        for source, edges in self.edges.items():
            if source not in self.nodes:
                return False
            source_node = self.nodes[source]
            for edge in edges:
                target_node = self.nodes.get(edge.target)
                if (
                    not edge.line_of_sight
                    or edge.source != source
                    or target_node is None
                    or target_node.component_id != source_node.component_id
                    or not math.isfinite(float(edge.distance_m))
                    or float(edge.distance_m) <= 0.0
                    or not all(
                        math.isfinite(float(value)) for value in edge.direction
                    )
                ):
                    return False
        return True

    @property
    def terminal_count(self) -> int:
        return sum(1 for node in self.nodes.values() if node.terminal)

    @property
    def dead_end_count(self) -> int:
        return sum(1 for node in self.nodes.values() if node.dead_end)

    @property
    def unknown_boundary_count(self) -> int:
        return sum(1 for node in self.nodes.values() if node.unknown_boundary)

    @property
    def motion_geometry_safe(self) -> bool:
        """Return whether graph centers are safe to use as camera waypoints."""
        if not all(
            math.isfinite(float(value)) and float(value) > 0.0
            for value in self.grid_size_m
        ):
            return False
        if (
            float(self.grid_size_m[1])
            > DEFAULT_3D_GRAPH_MAX_VERTICAL_GRID_SIZE_M
        ):
            return False
        max_edge_distance = max(1e-6, float(self.max_edge_distance_m))
        max_vertical_distance = max(
            1e-6,
            float(self.max_vertical_edge_distance_m),
        )
        for edges in self.edges.values():
            for edge in edges:
                if float(edge.distance_m) > max_edge_distance + 1e-6:
                    return False
                if (
                    abs(float(edge.direction[1]) * float(edge.distance_m))
                    > max_vertical_distance + 1e-6
                ):
                    return False
        return True

    def outgoing(
        self,
        key: VoxelGraphKey,
    ) -> tuple[NavigationVoxel3DEdge, ...]:
        return self.edges.get(key, ())

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "method": str(self.method),
            "version": NAVIGATION_VOXEL_3D_GRAPH_VERSION,
            "true_3d": True,
            "node_count": len(self.nodes),
            "edge_count": int(self.edge_count),
            "routable_node_count": int(self.routable_node_count),
            "component_count": int(self.component_count),
            "terminal_count": int(self.terminal_count),
            "dead_end_count": int(self.dead_end_count),
            "unknown_boundary_count": int(self.unknown_boundary_count),
            "grid_size_m": [float(value) for value in self.grid_size_m],
            "max_edge_distance_cells": int(self.max_edge_distance_cells),
            "max_edges_per_node": int(self.max_edges_per_node),
            "max_edge_distance_m": float(self.max_edge_distance_m),
            "max_vertical_edge_distance_m": float(
                self.max_vertical_edge_distance_m
            ),
            "native_coordinate_frame": "graph_xyz",
            "edge_integrity_safe": bool(self.edge_integrity_safe),
            "motion_geometry_safe": bool(self.motion_geometry_safe),
        }


def _normalise_grid_size(grid_size_m: GridSize) -> tuple[float, float, float]:
    """Return positive X/Y/Z bucket sizes from scalar or anisotropic input."""
    if isinstance(grid_size_m, Sequence) and not isinstance(
        grid_size_m,
        (str, bytes),
    ):
        if len(grid_size_m) != 3:
            raise ValueError("3-D graph grid size must have three values")
        values = tuple(float(value) for value in grid_size_m)
    else:
        size = float(grid_size_m)
        values = (size, size, size)
    if not all(math.isfinite(value) and value > 0.0 for value in values):
        raise ValueError("3-D graph grid sizes must be positive and finite")
    return values  # type: ignore[return-value]


def accumulate_navigation_voxel_3d_sample(
    accumulator: dict[VoxelGraphKey, list[float]],
    center: Point,
    *,
    grid_size_m: GridSize,
    clearance_m: float,
    volume_m3: float,
    progress_m: float,
) -> None:
    """Accumulate one filled raw voxel into a bounded-grid bucket."""
    sizes = _normalise_grid_size(grid_size_m)
    key = tuple(
        math.floor(float(center[axis]) / sizes[axis])
        for axis in range(3)
    )  # type: ignore[assignment]
    record = accumulator.get(key)
    if record is None:
        accumulator[key] = [
            1.0,
            float(center[0]),
            float(center[1]),
            float(center[2]),
            float(clearance_m),
            float(clearance_m),
            float(volume_m3),
            float(progress_m),
        ]
        return
    if len(record) < 8:
        record.extend([0.0] * (8 - len(record)))
    record[0] += 1.0
    record[1] += float(center[0])
    record[2] += float(center[1])
    record[3] += float(center[2])
    record[4] = min(record[4], float(clearance_m))
    record[5] += float(clearance_m)
    record[6] += float(volume_m3)
    record[7] += float(progress_m)


def finalize_navigation_voxel_3d_metrics(
    accumulator: Mapping[VoxelGraphKey, Sequence[float]],
    *,
    grid_size_m: GridSize,
    footprint_cell_size_m: float | None = None,
    max_nodes: int = DEFAULT_3D_GRAPH_MAX_NODES,
    max_vertical_factor: int = DEFAULT_3D_GRAPH_MAX_VERTICAL_COARSENING_FACTOR,
) -> tuple[dict[VoxelGraphKey, NavigationVoxel3DMetric], tuple[float, float, float]]:
    """Finalize bounded metrics while preserving useful vertical resolution."""
    base_sizes = _normalise_grid_size(grid_size_m)
    footprint_size = (
        base_sizes[0]
        if footprint_cell_size_m is None
        else max(1e-6, float(footprint_cell_size_m))
    )
    metrics = _metrics_from_accumulator(
        accumulator,
        base_sizes,
        footprint_cell_size_m=footprint_size,
    )
    if len(metrics) <= max(1, int(max_nodes)):
        return metrics, base_sizes

    vertical_factor = 1
    horizontal_factor = 1
    vertical_limit = max(1, int(max_vertical_factor))
    iterations = 0
    while len(metrics) > max(1, int(max_nodes)) and iterations < 32:
        iterations += 1
        # Preserve Y resolution first. Horizontal coarsening is much less
        # destructive for cave routing because it keeps stacked passages and
        # up/down openings independently traversable.
        step_horizontal_factor = 1
        step_vertical_factor = 1
        if horizontal_factor < 64:
            horizontal_factor *= 2
            step_horizontal_factor = 2
        elif vertical_factor < vertical_limit:
            vertical_factor *= 2
            step_vertical_factor = 2
        else:
            horizontal_factor *= 2
            step_horizontal_factor = 2
        # ``metrics`` already contains the result of the preceding pass.  The
        # factors passed to _coarsen_metrics must therefore describe only this
        # pass; passing the cumulative factors again collapses the keys by
        # 2, 4, 8, ... repeatedly while the persisted grid reports only the
        # final cumulative factor.
        metrics = _coarsen_metrics(
            metrics,
            horizontal_factor=step_horizontal_factor,
            vertical_factor=step_vertical_factor,
            base_size=base_sizes,
            footprint_cell_size_m=footprint_size,
        )
    return metrics, (
        base_sizes[0] * horizontal_factor,
        base_sizes[1] * vertical_factor,
        base_sizes[2] * horizontal_factor,
    )


def build_navigation_voxel_3d_graph(
    metrics: Mapping[VoxelGraphKey, NavigationVoxel3DMetric],
    *,
    grid_size_m: tuple[float, float, float],
    max_edge_distance_cells: int = DEFAULT_3D_GRAPH_MAX_EDGE_DISTANCE_CELLS,
    max_edges_per_node: int = DEFAULT_3D_GRAPH_MAX_EDGES_PER_NODE,
    max_total_edges: int = DEFAULT_3D_GRAPH_MAX_EDGES,
    unknown_boundary: Sequence[VoxelGraphKey] | set[VoxelGraphKey] = (),
    max_edge_distance_m: float | None = DEFAULT_3D_GRAPH_MAX_EDGE_DISTANCE_M,
    max_vertical_edge_distance_m: float | None = (
        DEFAULT_3D_GRAPH_MAX_VERTICAL_EDGE_DISTANCE_M
    ),
) -> NavigationVoxel3DGraph:
    """Build topology, dead-end labels, and bounded 3D LOS edges."""
    keys = set(metrics)
    if not keys:
        return NavigationVoxel3DGraph(
            nodes={},
            edges={},
            component_count=0,
            grid_size_m=tuple(float(value) for value in grid_size_m),
            max_edge_distance_cells=max(1, int(max_edge_distance_cells)),
            max_edges_per_node=max(1, int(max_edges_per_node)),
        )
    unknown = set(unknown_boundary)
    edge_distance_m_limit = (
        None
        if max_edge_distance_m is None
        else max(1e-6, float(max_edge_distance_m))
    )
    vertical_edge_distance_m_limit = (
        None
        if max_vertical_edge_distance_m is None
        else max(1e-6, float(max_vertical_edge_distance_m))
    )
    local_neighbors = {
        key: tuple(
            sorted(
                candidate
                for candidate in _neighbor_keys(key)
                if candidate in keys and _neighbor_is_clear(key, candidate, keys)
            )
        )
        for key in keys
    }
    component_ids, component_count = _component_ids(keys, local_neighbors)
    # ``progress_m`` describes the cached route seed, not altitude or a
    # monotonic depth coordinate. A cave can legitimately descend toward a
    # shallower region while still moving forward. Terminal topology must
    # therefore come from connectivity, never from the absence of a higher
    # progress neighbor.
    terminal = {
        key: key not in unknown and len(local_neighbors[key]) <= 1
        for key in keys
    }
    dead_end = _dead_end_nodes(
        keys,
        local_neighbors,
        terminal=terminal,
        unknown_boundary=unknown,
        component_ids=component_ids,
    )

    requested_edge_limit = max(6, int(max_edges_per_node))
    edge_limit = min(
        requested_edge_limit,
        max(1, int(max_total_edges) // max(1, len(keys))),
    )
    edge_distance_limit = max(1, int(max_edge_distance_cells))
    edge_map: dict[VoxelGraphKey, tuple[NavigationVoxel3DEdge, ...]] = {}
    remaining_edge_budget = max(0, int(max_total_edges))
    for source in sorted(keys):
        source_metric = metrics[source]
        candidates: list[tuple[float, VoxelGraphKey, bool]] = []
        for target in _nearby_keys(source, keys, edge_distance_limit):
            if target == source:
                continue
            # LOS shortcuts must not bridge two local 26-connected
            # components. Keeping the edge set consistent with component_id
            # makes the runtime topology filter a real invariant instead of
            # a best-effort diagnostic.
            if component_ids.get(target) != component_ids.get(source):
                continue
            target_metric = metrics[target]
            physical_delta = tuple(
                float(target_metric.center[axis] - source_metric.center[axis])
                for axis in range(3)
            )
            physical_distance = math.sqrt(
                sum(value * value for value in physical_delta)
            )
            if (
                edge_distance_m_limit is not None
                and physical_distance > edge_distance_m_limit + 1e-6
            ):
                continue
            if (
                vertical_edge_distance_m_limit is not None
                and abs(physical_delta[1])
                > vertical_edge_distance_m_limit + 1e-6
            ):
                continue
            is_local = target in local_neighbors[source]
            if not is_local and not _grid_line_of_sight(source, target, keys):
                continue
            distance_cells = _grid_distance(source, target)
            candidates.append((distance_cells, target, is_local))
        selected = _select_directionally_diverse_targets(
            source,
            candidates,
            local_neighbors=local_neighbors[source],
            max_targets=min(edge_limit, remaining_edge_budget),
        )
        edges: list[NavigationVoxel3DEdge] = []
        for _distance_cells, target, _is_local in selected:
            target_metric = metrics[target]
            delta = tuple(
                float(target_metric.center[axis] - source_metric.center[axis])
                for axis in range(3)
            )
            distance_m = math.sqrt(sum(value * value for value in delta))
            if distance_m <= 1e-6:
                continue
            edges.append(
                NavigationVoxel3DEdge(
                    source=source,
                    target=target,
                    distance_m=float(distance_m),
                    direction=tuple(
                        value / distance_m for value in delta
                    ),  # type: ignore[arg-type]
                    min_clearance_m=min(
                        float(source_metric.min_clearance_m),
                        float(target_metric.min_clearance_m),
                    ),
                    line_of_sight=True,
                )
            )
        edge_map[source] = tuple(edges)
        remaining_edge_budget -= len(edges)

    nodes: dict[VoxelGraphKey, NavigationVoxel3DNode] = {}
    for key in sorted(keys):
        metric = metrics[key]
        outgoing = edge_map.get(key, ())
        preferred = tuple(
            edge.target
            for edge in sorted(
                outgoing,
                key=lambda edge: _preferred_edge_key(
                    edge,
                    source=key,
                    metrics=metrics,
                    dead_end=dead_end,
                ),
                reverse=True,
            )
            if not dead_end.get(edge.target, False)
        )
        nodes[key] = NavigationVoxel3DNode(
            key=key,
            center=metric.center,
            footprint_cell=metric.footprint_cell,
            component_id=int(component_ids[key]),
            progress_m=float(metric.progress_m),
            connectivity_score=float(
                len(outgoing) + len(local_neighbors[key])
            ),
            local_degree=len(local_neighbors[key]),
            dead_end=bool(dead_end.get(key, False)),
            terminal=bool(terminal[key]),
            unknown_boundary=key in unknown,
            available_volume_m3=float(metric.available_volume_m3),
            min_clearance_m=float(metric.min_clearance_m),
            mean_clearance_m=float(metric.mean_clearance_m),
            preferred_neighbors=preferred,
        )
    return NavigationVoxel3DGraph(
        nodes=nodes,
        edges=edge_map,
        component_count=int(component_count),
        grid_size_m=tuple(float(value) for value in grid_size_m),
        max_edge_distance_cells=edge_distance_limit,
        max_edges_per_node=edge_limit,
        max_edge_distance_m=(
            DEFAULT_3D_GRAPH_MAX_EDGE_DISTANCE_M
            if edge_distance_m_limit is None
            else edge_distance_m_limit
        ),
        max_vertical_edge_distance_m=(
            DEFAULT_3D_GRAPH_MAX_VERTICAL_EDGE_DISTANCE_M
            if vertical_edge_distance_m_limit is None
            else vertical_edge_distance_m_limit
        ),
    )


def serialize_navigation_voxel_3d_graph(
    graph: NavigationVoxel3DGraph,
) -> dict[str, object]:
    """Serialize the graph with compact integer node references."""
    keys = tuple(sorted(graph.nodes))
    indices = {key: index for index, key in enumerate(keys)}
    nodes = []
    for key in keys:
        node = graph.nodes[key]
        nodes.append(
            [
                int(key[0]),
                int(key[1]),
                int(key[2]),
                *[float(value) for value in node.center],
                int(node.footprint_cell[0]),
                int(node.footprint_cell[1]),
                int(node.component_id),
                float(node.progress_m),
                float(node.connectivity_score),
                int(node.local_degree),
                bool(node.dead_end),
                bool(node.terminal),
                bool(node.unknown_boundary),
                float(node.available_volume_m3),
                float(node.min_clearance_m),
                float(node.mean_clearance_m),
                [
                    int(indices[target])
                    for target in node.preferred_neighbors
                    if target in indices
                ],
            ]
        )
    edges = []
    for source in keys:
        for edge in graph.outgoing(source):
            if edge.target not in indices:
                continue
            edges.append(
                [
                    int(indices[source]),
                    int(indices[edge.target]),
                    float(edge.distance_m),
                    *[float(value) for value in edge.direction],
                    float(edge.min_clearance_m),
                    bool(edge.line_of_sight),
                ]
            )
    return {
        "version": NAVIGATION_VOXEL_3D_GRAPH_VERSION,
        "method": str(graph.method),
        "true_3d": True,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "component_count": int(graph.component_count),
        "grid_size_m": [float(value) for value in graph.grid_size_m],
        "max_edge_distance_cells": int(graph.max_edge_distance_cells),
        "max_edges_per_node": int(graph.max_edges_per_node),
        "max_edge_distance_m": float(graph.max_edge_distance_m),
        "max_vertical_edge_distance_m": float(
            graph.max_vertical_edge_distance_m
        ),
        "nodes": nodes,
        "edges": edges,
    }


def deserialize_navigation_voxel_3d_graph(
    payload: object,
    *,
    max_nodes: int = DEFAULT_3D_GRAPH_MAX_NODES,
    max_edges: int = DEFAULT_3D_GRAPH_MAX_EDGES,
) -> NavigationVoxel3DGraph:
    """Validate and restore a serialized true-3D graph."""
    if not isinstance(payload, Mapping):
        raise ValueError("cached true-3D voxel graph is missing")
    if payload.get("version") != NAVIGATION_VOXEL_3D_GRAPH_VERSION:
        raise ValueError("unsupported true-3D voxel graph version")
    if payload.get("method") != NAVIGATION_VOXEL_3D_GRAPH_METHOD:
        raise ValueError("unsupported true-3D voxel graph method")
    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges")
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        raise ValueError("cached true-3D voxel graph nodes are malformed")
    if not isinstance(raw_edges, Sequence) or isinstance(raw_edges, (str, bytes)):
        raise ValueError("cached true-3D voxel graph edges are malformed")
    if len(raw_nodes) > max(1, int(max_nodes)):
        raise ValueError("cached true-3D voxel graph has too many nodes")
    if len(raw_edges) > max(1, int(max_edges)):
        raise ValueError("cached true-3D voxel graph has too many edges")
    grid_size = _finite_float_sequence(payload.get("grid_size_m"), 3)
    keys: list[VoxelGraphKey] = []
    values: list[
        tuple[
            Point,
            FootprintCell,
            int,
            float,
            float,
            int,
            bool,
            bool,
            bool,
            float,
            float,
            float,
            tuple[int, ...],
        ]
    ] = []
    for raw in raw_nodes:
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes))
            or len(raw) != 19
        ):
            raise ValueError("cached true-3D voxel graph node is malformed")
        try:
            key = (int(raw[0]), int(raw[1]), int(raw[2]))
            center = (float(raw[3]), float(raw[4]), float(raw[5]))
            footprint = (int(raw[6]), int(raw[7]))
            component = int(raw[8])
            progress = float(raw[9])
            connectivity = float(raw[10])
            degree = int(raw[11])
            dead_end = bool(raw[12])
            terminal = bool(raw[13])
            unknown = bool(raw[14])
            volume = float(raw[15])
            minimum = float(raw[16])
            mean = float(raw[17])
        except (TypeError, ValueError) as exc:
            raise ValueError("cached true-3D voxel graph node is malformed") from exc
        preferred_raw = raw[18]
        if not isinstance(preferred_raw, Sequence) or isinstance(preferred_raw, (str, bytes)):
            raise ValueError("cached true-3D voxel graph preferences are malformed")
        try:
            preferred = tuple(int(index) for index in preferred_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("cached true-3D voxel graph preferences are malformed") from exc
        if (
            key in keys
            or not all(
                math.isfinite(value)
                for value in (
                    *center,
                    progress,
                    connectivity,
                    volume,
                    minimum,
                    mean,
                )
            )
            or component < 0
            or degree < 0
            or progress < 0.0
            or volume < 0.0
            or minimum < 0.0
            or mean < 0.0
            or any(index < 0 or index >= len(raw_nodes) for index in preferred)
        ):
            raise ValueError("cached true-3D voxel graph node is invalid")
        keys.append(key)
        values.append(
            (
                center,
                footprint,
                component,
                progress,
                connectivity,
                degree,
                dead_end,
                terminal,
                unknown,
                volume,
                minimum,
                mean,
                preferred,
            )
        )
    edge_map: dict[VoxelGraphKey, list[NavigationVoxel3DEdge]] = {
        key: [] for key in keys
    }
    for raw in raw_edges:
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes))
            or len(raw) != 8
        ):
            raise ValueError("cached true-3D voxel graph edge is malformed")
        try:
            source_index = int(raw[0])
            target_index = int(raw[1])
            distance = float(raw[2])
            direction = (float(raw[3]), float(raw[4]), float(raw[5]))
            clearance = float(raw[6])
            line_of_sight = bool(raw[7])
        except (TypeError, ValueError) as exc:
            raise ValueError("cached true-3D voxel graph edge is malformed") from exc
        if (
            not 0 <= source_index < len(keys)
            or not 0 <= target_index < len(keys)
            or source_index == target_index
            or not math.isfinite(distance)
            or distance <= 0.0
            or not all(math.isfinite(value) for value in direction)
            or not math.isfinite(clearance)
            or clearance < 0.0
            or not line_of_sight
        ):
            raise ValueError("cached true-3D voxel graph edge is invalid")
        source, target = keys[source_index], keys[target_index]
        edge_map[source].append(
            NavigationVoxel3DEdge(
                source=source,
                target=target,
                distance_m=distance,
                direction=direction,
                min_clearance_m=clearance,
                line_of_sight=line_of_sight,
            )
        )
    nodes = {
        key: NavigationVoxel3DNode(
            key=key,
            center=values[index][0],
            footprint_cell=values[index][1],
            component_id=values[index][2],
            progress_m=values[index][3],
            connectivity_score=values[index][4],
            local_degree=values[index][5],
            dead_end=values[index][6],
            terminal=values[index][7],
            unknown_boundary=values[index][8],
            available_volume_m3=values[index][9],
            min_clearance_m=values[index][10],
            mean_clearance_m=values[index][11],
            preferred_neighbors=tuple(keys[index] for index in values[index][12]),
        )
        for index, key in enumerate(keys)
    }
    component_count = max(0, int(payload.get("component_count", 0)))
    return NavigationVoxel3DGraph(
        nodes=nodes,
        edges={key: tuple(value) for key, value in edge_map.items()},
        component_count=component_count,
        grid_size_m=grid_size,
        max_edge_distance_cells=max(1, int(payload.get("max_edge_distance_cells", 1))),
        max_edges_per_node=max(1, int(payload.get("max_edges_per_node", 1))),
        max_edge_distance_m=max(
            1e-6,
            float(
                payload.get(
                    "max_edge_distance_m",
                    DEFAULT_3D_GRAPH_MAX_EDGE_DISTANCE_M,
                )
            ),
        ),
        max_vertical_edge_distance_m=max(
            1e-6,
            float(
                payload.get(
                    "max_vertical_edge_distance_m",
                    DEFAULT_3D_GRAPH_MAX_VERTICAL_EDGE_DISTANCE_M,
                )
            ),
        ),
        method=str(payload.get("method")),
    )


def _metrics_from_accumulator(
    accumulator: Mapping[VoxelGraphKey, Sequence[float]],
    grid_size_m: GridSize,
    *,
    footprint_cell_size_m: float | None = None,
) -> dict[VoxelGraphKey, NavigationVoxel3DMetric]:
    metrics: dict[VoxelGraphKey, NavigationVoxel3DMetric] = {}
    grid_sizes = _normalise_grid_size(grid_size_m)
    footprint_size = (
        grid_sizes[0]
        if footprint_cell_size_m is None
        else max(1e-6, float(footprint_cell_size_m))
    )
    for key, raw in accumulator.items():
        if len(raw) < 8 or raw[0] <= 0.0:
            continue
        count = float(raw[0])
        center = (
            float(raw[1] / count),
            float(raw[2] / count),
            float(raw[3] / count),
        )
        footprint = (
            math.floor(center[0] / footprint_size),
            math.floor(center[2] / footprint_size),
        )
        progress_sum = float(raw[7])
        metrics[key] = NavigationVoxel3DMetric(
            center=center,
            footprint_cell=footprint,
            available_volume_m3=max(0.0, float(raw[6])),
            free_voxel_count=max(0, int(raw[0])),
            min_clearance_m=max(0.0, float(raw[4])),
            mean_clearance_m=max(0.0, float(raw[5] / count)),
            progress_m=max(0.0, progress_sum / count),
        )
    return metrics


def _coarsen_metrics(
    metrics: Mapping[VoxelGraphKey, NavigationVoxel3DMetric],
    *,
    horizontal_factor: int,
    vertical_factor: int,
    base_size: GridSize,
    footprint_cell_size_m: float | None = None,
) -> dict[VoxelGraphKey, NavigationVoxel3DMetric]:
    accumulator: dict[VoxelGraphKey, list[float]] = {}
    for key, metric in metrics.items():
        coarse_key = (
            math.floor(key[0] / horizontal_factor),
            math.floor(key[1] / vertical_factor),
            math.floor(key[2] / horizontal_factor),
        )
        record = accumulator.setdefault(coarse_key, [0.0] * 8)
        weight = max(1.0, float(metric.free_voxel_count))
        record[0] += weight
        record[1] += metric.center[0] * weight
        record[2] += metric.center[1] * weight
        record[3] += metric.center[2] * weight
        record[4] = min(
            metric.min_clearance_m
            if record[0] <= weight
            else record[4],
            metric.min_clearance_m,
        )
        record[5] += metric.mean_clearance_m * weight
        record[6] += metric.available_volume_m3
        record[7] += metric.progress_m * weight
    return _metrics_from_accumulator(
        accumulator,
        base_size,
        footprint_cell_size_m=footprint_cell_size_m,
    )


def _neighbor_keys(key: VoxelGraphKey) -> tuple[VoxelGraphKey, ...]:
    return tuple(
        (key[0] + dx, key[1] + dy, key[2] + dz)
        for dx, dy, dz in _NEIGHBOR_OFFSETS_26
    )


def _neighbor_is_clear(
    source: VoxelGraphKey,
    target: VoxelGraphKey,
    keys: set[VoxelGraphKey],
) -> bool:
    delta = tuple(target[index] - source[index] for index in range(3))
    nonzero_axes = [index for index, value in enumerate(delta) if value]
    if len(nonzero_axes) <= 1:
        return True
    for mask in range(1, 1 << len(nonzero_axes)):
        candidate = list(source)
        for bit, axis in enumerate(nonzero_axes):
            if mask & (1 << bit):
                candidate[axis] += 1 if delta[axis] > 0 else -1
        if tuple(candidate) not in keys:
            return False
    return True


def _component_ids(
    keys: set[VoxelGraphKey],
    neighbors: Mapping[VoxelGraphKey, Sequence[VoxelGraphKey]],
) -> tuple[dict[VoxelGraphKey, int], int]:
    component_ids: dict[VoxelGraphKey, int] = {}
    component_count = 0
    for start in sorted(keys):
        if start in component_ids:
            continue
        component_ids[start] = component_count
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for neighbor in neighbors.get(current, ()):
                if neighbor in component_ids:
                    continue
                component_ids[neighbor] = component_count
                queue.append(neighbor)
        component_count += 1
    return component_ids, component_count


def _dead_end_nodes(
    keys: set[VoxelGraphKey],
    neighbors: Mapping[VoxelGraphKey, Sequence[VoxelGraphKey]],
    *,
    terminal: Mapping[VoxelGraphKey, bool],
    unknown_boundary: set[VoxelGraphKey],
    component_ids: Mapping[VoxelGraphKey, int],
) -> dict[VoxelGraphKey, bool]:
    dead_end = {key: False for key in keys}
    for leaf in sorted(key for key in keys if terminal[key]):
        chain = [leaf]
        previous: VoxelGraphKey | None = None
        current: VoxelGraphKey | None = leaf
        reaches_junction = False
        reaches_unknown = leaf in unknown_boundary
        while current is not None:
            candidates = [
                neighbor
                for neighbor in neighbors.get(current, ())
                if neighbor != previous
            ]
            next_cell = candidates[0] if len(candidates) == 1 else None
            if next_cell is None:
                reaches_junction = len(neighbors.get(current, ())) >= 3
                break
            previous, current = current, next_cell
            chain.append(current)
            reaches_unknown = reaches_unknown or current in unknown_boundary
            if len(chain) > len(keys):
                break
        if reaches_junction and not reaches_unknown:
            for key in chain[:-1]:
                if component_ids.get(key) == component_ids.get(leaf):
                    dead_end[key] = True
    return dead_end


def _nearby_keys(
    source: VoxelGraphKey,
    keys: set[VoxelGraphKey],
    radius: int,
) -> tuple[VoxelGraphKey, ...]:
    radius = max(1, int(radius))
    return tuple(
        (x, y, z)
        for x in range(source[0] - radius, source[0] + radius + 1)
        for y in range(source[1] - radius, source[1] + radius + 1)
        for z in range(source[2] - radius, source[2] + radius + 1)
        if (x, y, z) != source
        and (x, y, z) in keys
        and max(abs(x - source[0]), abs(y - source[1]), abs(z - source[2])) <= radius
    )


def _grid_distance(first: VoxelGraphKey, second: VoxelGraphKey) -> float:
    return math.sqrt(
        sum((float(second[index] - first[index])) ** 2 for index in range(3))
    )


def _grid_line_of_sight(
    source: VoxelGraphKey,
    target: VoxelGraphKey,
    keys: set[VoxelGraphKey],
) -> bool:
    steps = max(
        1,
        max(abs(target[index] - source[index]) for index in range(3)) * 4,
    )
    for index in range(1, steps):
        fraction = float(index) / float(steps)
        sampled = tuple(
            int(math.floor(
                float(source[axis])
                + (float(target[axis] - source[axis]) * fraction)
                + 0.5
            ))
            for axis in range(3)
        )
        if sampled not in keys:
            return False
    return True


def _select_directionally_diverse_targets(
    source: VoxelGraphKey,
    candidates: Sequence[tuple[float, VoxelGraphKey, bool]],
    *,
    local_neighbors: Sequence[VoxelGraphKey],
    max_targets: int,
) -> tuple[tuple[float, VoxelGraphKey, bool], ...]:
    """Keep movement coverage when a node's edge budget is smaller than 26.

    Local neighbors are already known to be traversable, but sorting their
    integer keys before truncating biases the persisted graph toward negative
    X/Y/Z directions. That can erase the only edge representing a visible
    right-hand, upward, or forward passage. Reserve the six cardinal axes
    first, then retain one representative for each signed direction and each
    angular bin before filling any remaining slots by distance.
    """
    selected: list[tuple[float, VoxelGraphKey, bool]] = []
    selected_keys: set[VoxelGraphKey] = set()
    by_target = {item[1]: item for item in candidates}

    limit = max(0, int(max_targets))

    def append(item: tuple[float, VoxelGraphKey, bool] | None) -> None:
        if item is None or len(selected) >= limit:
            return
        target = item[1]
        if target in selected_keys:
            return
        selected.append(item)
        selected_keys.add(target)

    # A six-axis set is the preferred representation needed to permit forward,
    # reverse, lateral, and vertical camera moves without depending on key
    # ordering. A very large graph may receive a smaller global edge budget;
    # in that case append whichever cardinal candidates actually exist before
    # considering longer shortcuts.
    for offset in _CARDINAL_OFFSETS:
        target = tuple(source[index] + offset[index] for index in range(3))
        append(by_target.get(target))

    def candidate_key(
        item: tuple[float, VoxelGraphKey, bool],
    ) -> tuple[object, ...]:
        distance, target, is_local = item
        return (not is_local, float(distance), target)

    signed_bins: dict[
        tuple[int, int, int], list[tuple[float, VoxelGraphKey, bool]]
    ] = {}
    angular_bins: dict[
        tuple[int, int], list[tuple[float, VoxelGraphKey, bool]]
    ] = {}
    for item in candidates:
        _distance, target, _is_local = item
        if target in selected_keys:
            continue
        delta = tuple(target[index] - source[index] for index in range(3))
        signed = tuple(
            0 if value == 0 else (1 if value > 0 else -1)
            for value in delta
        )
        signed_bins.setdefault(signed, []).append(item)

        length = math.sqrt(sum(float(value * value) for value in delta))
        if length <= 1e-9:
            continue
        azimuth = math.atan2(float(delta[2]), float(delta[0]))
        elevation = math.asin(
            max(-1.0, min(1.0, float(delta[1]) / length))
        )
        azimuth_bin_count = max(8, DEFAULT_3D_GRAPH_DIRECTION_BINS // 4)
        elevation_bin_count = max(4, DEFAULT_3D_GRAPH_DIRECTION_BINS // 8)
        azimuth_bin = int(
            math.floor(
                ((azimuth + math.pi) / (2.0 * math.pi))
                * azimuth_bin_count
            )
        ) % azimuth_bin_count
        elevation_bin = int(
            math.floor(
                ((elevation + math.pi / 2.0) / math.pi)
                * elevation_bin_count
            )
        ) % elevation_bin_count
        angular_bins.setdefault((azimuth_bin, elevation_bin), []).append(item)

    for bucket in sorted(signed_bins):
        append(min(signed_bins[bucket], key=candidate_key))

    for bucket in sorted(angular_bins):
        append(min(angular_bins[bucket], key=candidate_key))

    for item in sorted(candidates):
        if len(selected) >= limit:
            break
        if item[1] in selected_keys:
            continue
        selected.append(item)
        selected_keys.add(item[1])
    return tuple(selected[:limit])


def _preferred_edge_key(
    edge: NavigationVoxel3DEdge,
    *,
    source: VoxelGraphKey,
    metrics: Mapping[VoxelGraphKey, NavigationVoxel3DMetric],
    dead_end: Mapping[VoxelGraphKey, bool],
) -> tuple[object, ...]:
    source_metric = metrics[source]
    target_metric = metrics[edge.target]
    return (
        not dead_end.get(edge.target, False),
        target_metric.progress_m - source_metric.progress_m,
        target_metric.mean_clearance_m,
        -float(edge.distance_m),
        edge.target,
    )


def _finite_float_sequence(value: object, length: int) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("cached true-3D voxel graph grid size is malformed")
    if len(value) != length:
        raise ValueError("cached true-3D voxel graph grid size is malformed")
    try:
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError("cached true-3D voxel graph grid size is malformed") from exc
    if not all(math.isfinite(item) and item > 0.0 for item in values):
        raise ValueError("cached true-3D voxel graph grid size is invalid")
    return values
