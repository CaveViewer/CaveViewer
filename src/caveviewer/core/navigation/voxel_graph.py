"""Prepared forward-constrained graph data for cached cave navigation.

The cache builder uses this module to turn filled navigation cells into a
bounded visibility graph. Runtime Guided Dive planning can then search the
prepared graph with a heading-aware state instead of rebuilding topology or
repeatedly inspecting raw mesh geometry.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math

from caveviewer.core.navigation.centerline import (
    FootprintCell,
    Point,
    navigable_footprint_neighbors,
)


NAVIGATION_VOXEL_GRAPH_METHOD = "heading_aware_forward_any_angle_v1"
NAVIGATION_VOXEL_GRAPH_VERSION = 1
DEFAULT_GRAPH_MAX_EDGE_DISTANCE_CELLS = 12
DEFAULT_GRAPH_MAX_EDGES_PER_NODE = 32
# Keep the serialized any-angle graph bounded on consumer hardware. The
# cache's coarse metric limit is 16,384 nodes, so this still permits sixteen
# directional edges per node in the largest normal atlas.
DEFAULT_GRAPH_MAX_EDGES = 262_144
DEFAULT_GRAPH_DIRECTION_BINS = 16


@dataclass(frozen=True)
class NavigationVoxelGraphNode:
    """Prepared topology metadata for one navigable footprint voxel."""

    cell: FootprintCell
    component_id: int
    center_y_m: float
    connectivity_score: float
    local_degree: int
    dead_end: bool
    terminal: bool
    unknown_boundary: bool
    preferred_neighbors: tuple[FootprintCell, ...] = ()

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "cell": [int(self.cell[0]), int(self.cell[1])],
            "component_id": int(self.component_id),
            "center_y_m": float(self.center_y_m),
            "connectivity_score": float(self.connectivity_score),
            "local_degree": int(self.local_degree),
            "dead_end": bool(self.dead_end),
            "terminal": bool(self.terminal),
            "unknown_boundary": bool(self.unknown_boundary),
            "preferred_neighbor_count": len(self.preferred_neighbors),
        }


@dataclass(frozen=True)
class NavigationVoxelGraphEdge:
    """One directed any-angle, line-of-sight route edge."""

    source: FootprintCell
    target: FootprintCell
    distance_m: float
    direction: Point
    min_clearance_m: float
    line_of_sight: bool = True

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "source": [int(self.source[0]), int(self.source[1])],
            "target": [int(self.target[0]), int(self.target[1])],
            "distance_m": float(self.distance_m),
            "direction": [float(value) for value in self.direction],
            "min_clearance_m": float(self.min_clearance_m),
            "line_of_sight": bool(self.line_of_sight),
        }


@dataclass(frozen=True)
class NavigationVoxelGraph:
    """Bounded prepared graph persisted with one voxel atlas."""

    nodes: Mapping[FootprintCell, NavigationVoxelGraphNode]
    edges: Mapping[FootprintCell, tuple[NavigationVoxelGraphEdge, ...]]
    component_count: int
    max_edge_distance_cells: int
    max_edges_per_node: int
    method: str = NAVIGATION_VOXEL_GRAPH_METHOD

    @property
    def edge_count(self) -> int:
        return sum(len(edges) for edges in self.edges.values())

    @property
    def terminal_count(self) -> int:
        return sum(1 for node in self.nodes.values() if node.terminal)

    @property
    def dead_end_count(self) -> int:
        return sum(1 for node in self.nodes.values() if node.dead_end)

    @property
    def unknown_boundary_count(self) -> int:
        return sum(1 for node in self.nodes.values() if node.unknown_boundary)

    def outgoing(self, cell: FootprintCell) -> tuple[NavigationVoxelGraphEdge, ...]:
        return self.edges.get(cell, ())

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "method": str(self.method),
            "version": NAVIGATION_VOXEL_GRAPH_VERSION,
            "node_count": len(self.nodes),
            "edge_count": int(self.edge_count),
            "component_count": int(self.component_count),
            "terminal_count": int(self.terminal_count),
            "dead_end_count": int(self.dead_end_count),
            "unknown_boundary_count": int(self.unknown_boundary_count),
            "max_edge_distance_cells": int(self.max_edge_distance_cells),
            "max_edges_per_node": int(self.max_edges_per_node),
        }


def build_navigation_voxel_graph(
    component_cells: Sequence[FootprintCell] | set[FootprintCell],
    metrics: Mapping[FootprintCell, object],
    *,
    cell_size_m: float,
    max_edge_distance_cells: int = DEFAULT_GRAPH_MAX_EDGE_DISTANCE_CELLS,
    max_edges_per_node: int = DEFAULT_GRAPH_MAX_EDGES_PER_NODE,
    max_total_edges: int = DEFAULT_GRAPH_MAX_EDGES,
) -> NavigationVoxelGraph:
    """Build a bounded 2.5D any-angle graph from filled navigation cells.

    The free-space flood fill is already performed while building each voxel
    tile. This graph keeps the runtime representation compact by treating each
    footprint cell with filled volume as one navigational voxel node, while
    its edge direction includes the cached representative height. Local
    eight-neighbor topology supplies dead-end labels; longer edges are added
    only when the footprint line between the cells remains covered.
    """
    cell_size = max(1e-6, float(cell_size_m))
    all_cells = {
        (int(cell[0]), int(cell[1]))
        for cell in component_cells
        if len(cell) == 2
    }
    graph_cells = {
        (int(cell[0]), int(cell[1]))
        for cell, metric in metrics.items()
        if len(cell) == 2 and int(getattr(metric, "free_cell_count", 0)) > 0
    }
    if not graph_cells:
        return NavigationVoxelGraph(
            nodes={},
            edges={},
            component_count=0,
            max_edge_distance_cells=max(1, int(max_edge_distance_cells)),
            max_edges_per_node=max(1, int(max_edges_per_node)),
        )

    local_neighbors = {
        cell: tuple(
            sorted(
                neighbor
                for neighbor in navigable_footprint_neighbors(cell, graph_cells)
                if neighbor in graph_cells
            )
        )
        for cell in graph_cells
    }
    component_ids, component_count = _component_ids(graph_cells, local_neighbors)
    unknown_boundary = {
        cell: any(
            neighbor in all_cells and neighbor not in graph_cells
            for neighbor in navigable_footprint_neighbors(cell, all_cells)
        )
        for cell in graph_cells
    }
    terminal = {
        cell: not unknown_boundary[cell] and len(local_neighbors[cell]) <= 1
        for cell in graph_cells
    }
    dead_end = _dead_end_nodes(
        graph_cells,
        local_neighbors,
        terminal=terminal,
        unknown_boundary=unknown_boundary,
        component_ids=component_ids,
    )

    edge_map: dict[FootprintCell, tuple[NavigationVoxelGraphEdge, ...]] = {}
    edge_distance_limit = max(1, int(max_edge_distance_cells))
    requested_edge_limit = max(4, int(max_edges_per_node))
    edge_limit = min(
        requested_edge_limit,
        max(
            4,
            int(max_total_edges) // max(1, len(graph_cells)),
        ),
    )
    for source in sorted(graph_cells):
        candidates: list[tuple[float, FootprintCell, bool]] = []
        for target in _nearby_cells(
            source,
            graph_cells,
            radius_cells=edge_distance_limit,
        ):
            if target == source:
                continue
            is_local = target in local_neighbors[source]
            if not is_local and not _footprint_line_of_sight(
                source,
                target,
                graph_cells,
            ):
                continue
            distance_cells = math.hypot(
                float(target[0] - source[0]),
                float(target[1] - source[1]),
            )
            candidates.append((distance_cells, target, is_local))
        selected_targets = _select_directionally_diverse_targets(
            source,
            candidates,
            local_neighbors=local_neighbors[source],
            max_targets=edge_limit,
        )
        source_metric = metrics[source]
        source_y = _metric_center_y(source_metric)
        source_clearance = _metric_clearance(source_metric)
        edges: list[NavigationVoxelGraphEdge] = []
        for distance_cells, target, _is_local in selected_targets:
            target_metric = metrics[target]
            target_y = _metric_center_y(target_metric)
            dx = float(target[0] - source[0]) * cell_size
            dz = float(target[1] - source[1]) * cell_size
            dy = float(target_y - source_y)
            distance_m = math.sqrt(dx * dx + dy * dy + dz * dz)
            if distance_m <= 1e-6:
                continue
            edges.append(
                NavigationVoxelGraphEdge(
                    source=source,
                    target=target,
                    distance_m=float(distance_m),
                    direction=(
                        dx / distance_m,
                        dy / distance_m,
                        dz / distance_m,
                    ),
                    min_clearance_m=min(
                        source_clearance,
                        _metric_clearance(target_metric),
                    ),
                    line_of_sight=True,
                )
            )
        edge_map[source] = tuple(edges)

    nodes: dict[FootprintCell, NavigationVoxelGraphNode] = {}
    for cell in sorted(graph_cells):
        metric = metrics[cell]
        outgoing = edge_map.get(cell, ())
        preferred = tuple(
            edge.target
            for edge in sorted(
                outgoing,
                key=lambda edge: _preferred_edge_key(
                    edge,
                    source=cell,
                    metrics=metrics,
                    dead_end=dead_end,
                ),
                reverse=True,
            )
            if _edge_is_forward_progress(edge, source=cell, metrics=metrics)
        )
        nodes[cell] = NavigationVoxelGraphNode(
            cell=cell,
            component_id=int(component_ids[cell]),
            center_y_m=float(_metric_center_y(metric)),
            connectivity_score=float(
                len(outgoing) + 2 * len(local_neighbors[cell])
            ),
            local_degree=len(local_neighbors[cell]),
            dead_end=bool(dead_end[cell]),
            terminal=bool(terminal[cell]),
            unknown_boundary=bool(unknown_boundary[cell]),
            preferred_neighbors=preferred,
        )

    return NavigationVoxelGraph(
        nodes=nodes,
        edges=edge_map,
        component_count=int(component_count),
        max_edge_distance_cells=edge_distance_limit,
        max_edges_per_node=edge_limit,
    )


def serialize_navigation_voxel_graph(
    graph: NavigationVoxelGraph,
) -> dict[str, object]:
    """Return a compact index-based graph payload."""
    cells = tuple(sorted(graph.nodes))
    indices = {cell: index for index, cell in enumerate(cells)}
    nodes = []
    for cell in cells:
        node = graph.nodes[cell]
        nodes.append(
            [
                int(cell[0]),
                int(cell[1]),
                int(node.component_id),
                float(node.center_y_m),
                float(node.connectivity_score),
                int(node.local_degree),
                bool(node.dead_end),
                bool(node.terminal),
                bool(node.unknown_boundary),
                [
                    int(indices[target])
                    for target in node.preferred_neighbors
                    if target in indices
                ],
            ]
        )
    edges = []
    for source in cells:
        for edge in graph.outgoing(source):
            if edge.target not in indices:
                continue
            edges.append(
                [
                    int(indices[source]),
                    int(indices[edge.target]),
                    float(edge.distance_m),
                    float(edge.direction[0]),
                    float(edge.direction[1]),
                    float(edge.direction[2]),
                    float(edge.min_clearance_m),
                    bool(edge.line_of_sight),
                ]
            )
    return {
        "version": NAVIGATION_VOXEL_GRAPH_VERSION,
        "method": str(graph.method),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "component_count": int(graph.component_count),
        "max_edge_distance_cells": int(graph.max_edge_distance_cells),
        "max_edges_per_node": int(graph.max_edges_per_node),
        "nodes": nodes,
        "edges": edges,
    }


def deserialize_navigation_voxel_graph(
    payload: object,
    *,
    max_nodes: int = 16_384,
    max_edges: int = DEFAULT_GRAPH_MAX_EDGES,
) -> NavigationVoxelGraph:
    """Validate and restore a prepared graph from an optional cache field."""
    if not isinstance(payload, Mapping):
        raise ValueError("cached navigation voxel graph is missing")
    if payload.get("version") != NAVIGATION_VOXEL_GRAPH_VERSION:
        raise ValueError("unsupported navigation voxel graph version")
    if payload.get("method") != NAVIGATION_VOXEL_GRAPH_METHOD:
        raise ValueError("unsupported navigation voxel graph method")
    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges")
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        raise ValueError("cached navigation voxel graph nodes are malformed")
    if not isinstance(raw_edges, Sequence) or isinstance(raw_edges, (str, bytes)):
        raise ValueError("cached navigation voxel graph edges are malformed")
    if len(raw_nodes) > max(1, int(max_nodes)):
        raise ValueError("cached navigation voxel graph has too many nodes")
    if len(raw_edges) > max(1, int(max_edges)):
        raise ValueError("cached navigation voxel graph has too many edges")
    cells: list[FootprintCell] = []
    node_values: list[
        tuple[int, float, float, int, bool, bool, bool, tuple[int, ...]]
    ] = []
    for raw in raw_nodes:
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes))
            or len(raw) != 10
        ):
            raise ValueError("cached navigation voxel graph node is malformed")
        try:
            cell = (int(raw[0]), int(raw[1]))
            component_id = int(raw[2])
            center_y = float(raw[3])
            connectivity = float(raw[4])
            local_degree = int(raw[5])
            dead_end = bool(raw[6])
            terminal = bool(raw[7])
            unknown_boundary = bool(raw[8])
        except (TypeError, ValueError) as exc:
            raise ValueError("cached navigation voxel graph node is malformed") from exc
        preferred_raw = raw[9]
        if not isinstance(preferred_raw, Sequence) or isinstance(
            preferred_raw,
            (str, bytes),
        ):
            raise ValueError("cached navigation voxel graph preferences are malformed")
        try:
            preferred = tuple(int(index) for index in preferred_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "cached navigation voxel graph preferences are malformed"
            ) from exc
        if (
            cell in cells
            or not all(math.isfinite(value) for value in (center_y, connectivity))
            or component_id < 0
            or local_degree < 0
            or any(index < 0 or index >= len(raw_nodes) for index in preferred)
        ):
            raise ValueError("cached navigation voxel graph node is invalid")
        cells.append(cell)
        node_values.append(
            (
                component_id,
                center_y,
                connectivity,
                local_degree,
                dead_end,
                terminal,
                unknown_boundary,
                preferred,
            )
        )
    edge_map: dict[FootprintCell, list[NavigationVoxelGraphEdge]] = {
        cell: [] for cell in cells
    }
    for raw in raw_edges:
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes))
            or len(raw) != 8
        ):
            raise ValueError("cached navigation voxel graph edge is malformed")
        try:
            source_index = int(raw[0])
            target_index = int(raw[1])
            distance_m = float(raw[2])
            direction = (float(raw[3]), float(raw[4]), float(raw[5]))
            clearance = float(raw[6])
            line_of_sight = bool(raw[7])
        except (TypeError, ValueError) as exc:
            raise ValueError("cached navigation voxel graph edge is malformed") from exc
        if (
            not 0 <= source_index < len(cells)
            or not 0 <= target_index < len(cells)
            or source_index == target_index
            or not math.isfinite(distance_m)
            or distance_m <= 0.0
            or not all(math.isfinite(value) for value in direction)
            or not math.isfinite(clearance)
            or clearance < 0.0
            or not line_of_sight
        ):
            raise ValueError("cached navigation voxel graph edge is invalid")
        source = cells[source_index]
        target = cells[target_index]
        edge_map[source].append(
            NavigationVoxelGraphEdge(
                source=source,
                target=target,
                distance_m=distance_m,
                direction=direction,
                min_clearance_m=clearance,
                line_of_sight=line_of_sight,
            )
        )
    nodes = {
        cell: NavigationVoxelGraphNode(
            cell=cell,
            component_id=values[0],
            center_y_m=values[1],
            connectivity_score=values[2],
            local_degree=values[3],
            dead_end=values[4],
            terminal=values[5],
            unknown_boundary=values[6],
            preferred_neighbors=tuple(cells[index] for index in values[7]),
        )
        for cell, values in zip(cells, node_values, strict=True)
    }
    return NavigationVoxelGraph(
        nodes=nodes,
        edges={cell: tuple(values) for cell, values in edge_map.items()},
        component_count=max(
            0,
            int(payload.get("component_count", 0)),
        ),
        max_edge_distance_cells=max(
            1,
            int(payload.get("max_edge_distance_cells", 1)),
        ),
        max_edges_per_node=max(
            4,
            int(payload.get("max_edges_per_node", 4)),
        ),
        method=str(payload.get("method")),
    )


def _component_ids(
    cells: set[FootprintCell],
    neighbors: Mapping[FootprintCell, Sequence[FootprintCell]],
) -> tuple[dict[FootprintCell, int], int]:
    component_ids: dict[FootprintCell, int] = {}
    component_count = 0
    for start in sorted(cells):
        if start in component_ids:
            continue
        queue = deque([start])
        component_ids[start] = component_count
        while queue:
            cell = queue.popleft()
            for neighbor in neighbors.get(cell, ()):
                if neighbor in component_ids:
                    continue
                component_ids[neighbor] = component_count
                queue.append(neighbor)
        component_count += 1
    return component_ids, component_count


def _dead_end_nodes(
    cells: set[FootprintCell],
    neighbors: Mapping[FootprintCell, Sequence[FootprintCell]],
    *,
    terminal: Mapping[FootprintCell, bool],
    unknown_boundary: Mapping[FootprintCell, bool],
    component_ids: Mapping[FootprintCell, int],
) -> dict[FootprintCell, bool]:
    """Mark leaf-to-junction chains as dead ends without degree-only errors."""
    dead_end = {cell: False for cell in cells}
    for leaf in sorted(cell for cell in cells if terminal[cell]):
        chain: list[FootprintCell] = [leaf]
        previous: FootprintCell | None = leaf
        current = (
            neighbors.get(leaf, ())[0]
            if neighbors.get(leaf, ())
            else None
        )
        reaches_junction = False
        reaches_unknown = unknown_boundary[leaf]
        while current is not None:
            chain.append(current)
            reaches_unknown = reaches_unknown or unknown_boundary[current]
            degree = len(neighbors.get(current, ()))
            if degree != 2:
                reaches_junction = degree >= 3
                break
            candidates = [
                neighbor
                for neighbor in neighbors.get(current, ())
                if neighbor != previous
            ]
            next_cell = candidates[0] if candidates else None
            if next_cell is None or next_cell in chain:
                break
            previous, current = current, next_cell
            if len(chain) > len(cells):
                break
        if reaches_junction and not reaches_unknown:
            for cell in chain[:-1]:
                if component_ids.get(cell) == component_ids.get(leaf):
                    dead_end[cell] = True
    return dead_end


def _nearby_cells(
    source: FootprintCell,
    cells: set[FootprintCell],
    *,
    radius_cells: int,
) -> tuple[FootprintCell, ...]:
    radius = max(1, int(radius_cells))
    return tuple(
        (x, z)
        for x in range(source[0] - radius, source[0] + radius + 1)
        for z in range(source[1] - radius, source[1] + radius + 1)
        if (x, z) != source
        and (x, z) in cells
    )


def _select_directionally_diverse_targets(
    source: FootprintCell,
    candidates: Sequence[tuple[float, FootprintCell, bool]],
    *,
    local_neighbors: Sequence[FootprintCell],
    max_targets: int,
) -> tuple[tuple[float, FootprintCell, bool], ...]:
    """Keep local topology plus nearest visibility targets per direction."""
    selected: list[tuple[float, FootprintCell, bool]] = []
    selected_cells: set[FootprintCell] = set()
    by_target = {target: item for item in candidates for target in (item[1],)}
    for target in sorted(local_neighbors):
        item = by_target.get(target)
        if item is not None:
            selected.append(item)
            selected_cells.add(target)

    bins: dict[int, tuple[float, FootprintCell, bool]] = {}
    for item in sorted(candidates):
        distance, target, _is_local = item
        if target in selected_cells:
            continue
        angle = math.atan2(
            float(target[1] - source[1]),
            float(target[0] - source[0]),
        )
        bin_index = int(
            math.floor(
                ((angle + math.pi) / (2.0 * math.pi))
                * DEFAULT_GRAPH_DIRECTION_BINS
            )
        ) % DEFAULT_GRAPH_DIRECTION_BINS
        bins.setdefault(bin_index, item)
    for item in bins.values():
        if len(selected) >= max_targets:
            break
        if item[1] in selected_cells:
            continue
        selected.append(item)
        selected_cells.add(item[1])
    for item in sorted(candidates):
        if len(selected) >= max_targets:
            break
        if item[1] in selected_cells:
            continue
        selected.append(item)
        selected_cells.add(item[1])
    return tuple(selected[:max_targets])


def _footprint_line_of_sight(
    source: FootprintCell,
    target: FootprintCell,
    graph_cells: set[FootprintCell],
) -> bool:
    """Return whether a super-sampled centerline remains in free cells."""
    dx = target[0] - source[0]
    dz = target[1] - source[1]
    steps = max(1, max(abs(dx), abs(dz)) * 4)
    for index in range(1, steps):
        fraction = float(index) / float(steps)
        x = float(source[0]) + 0.5 + float(dx) * fraction
        z = float(source[1]) + 0.5 + float(dz) * fraction
        epsilon = 1e-6
        sampled = {
            (
                math.floor(x + offset_x),
                math.floor(z + offset_z),
            )
            for offset_x in (-epsilon, 0.0, epsilon)
            for offset_z in (-epsilon, 0.0, epsilon)
        }
        if not sampled.issubset(graph_cells):
            return False
    return True


def _metric_center_y(metric: object) -> float:
    value = float(getattr(metric, "center_y_m", 0.0))
    return value if math.isfinite(value) else 0.0


def _metric_clearance(metric: object) -> float:
    value = float(
        getattr(
            metric,
            "min_clearance_m",
            getattr(metric, "mean_clearance_m", 0.0),
        )
    )
    return max(0.0, value) if math.isfinite(value) else 0.0


def _edge_is_forward_progress(
    edge: NavigationVoxelGraphEdge,
    *,
    source: FootprintCell,
    metrics: Mapping[FootprintCell, object],
) -> bool:
    source_progress = float(getattr(metrics[source], "progress_m", 0.0))
    target_progress = float(getattr(metrics[edge.target], "progress_m", 0.0))
    return target_progress >= source_progress


def _preferred_edge_key(
    edge: NavigationVoxelGraphEdge,
    *,
    source: FootprintCell,
    metrics: Mapping[FootprintCell, object],
    dead_end: Mapping[FootprintCell, bool],
) -> tuple[object, ...]:
    source_progress = float(getattr(metrics[source], "progress_m", 0.0))
    target_progress = float(getattr(metrics[edge.target], "progress_m", 0.0))
    return (
        not dead_end.get(edge.target, False),
        target_progress - source_progress,
        -float(edge.distance_m),
        -int(edge.target[0]),
        -int(edge.target[1]),
    )
