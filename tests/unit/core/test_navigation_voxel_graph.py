"""Tests for cache-time prepared any-angle voxel graph data."""

from __future__ import annotations

from caveviewer.core.navigation.voxel_cache import NavigationVoxelCellMetric
from caveviewer.core.navigation.voxel_graph import (
    NAVIGATION_VOXEL_GRAPH_METHOD,
    build_navigation_voxel_graph,
    deserialize_navigation_voxel_graph,
    serialize_navigation_voxel_graph,
)


def _metrics(cells):
    return {
        cell: NavigationVoxelCellMetric(
            available_volume_m3=100.0,
            free_cell_count=10,
            min_clearance_m=2.0,
            mean_clearance_m=3.0,
            progress_m=float(max(0, cell[0])),
            center_y_m=float(cell[1]),
        )
        for cell in cells
    }


def test_prepared_graph_keeps_local_topology_and_blocks_corner_cutting():
    cells = ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    graph = build_navigation_voxel_graph(
        cells,
        _metrics(cells),
        cell_size_m=1.0,
        max_edge_distance_cells=4,
    )

    assert graph.method == NAVIGATION_VOXEL_GRAPH_METHOD
    assert len(graph.nodes) == len(cells)
    assert graph.edge_count > len(cells)
    assert graph.outgoing((0, 0))
    assert all(edge.line_of_sight for edge in graph.outgoing((0, 0)))
    assert not any(
        edge.target == (2, 2)
        for edge in graph.outgoing((0, 0))
    )


def test_prepared_graph_labels_terminal_branch_and_unknown_boundary():
    graph_cells = (
        (0, 0),
        (1, 0),
        (2, 0),
        (2, 1),
        (2, 2),
        (2, -1),
        (2, -2),
    )
    all_component_cells = graph_cells + ((2, -3),)
    graph = build_navigation_voxel_graph(
        all_component_cells,
        _metrics(graph_cells),
        cell_size_m=1.0,
    )

    assert graph.nodes[(2, 2)].dead_end is True
    assert graph.nodes[(2, -2)].unknown_boundary is True
    assert graph.nodes[(2, -2)].terminal is False
    assert graph.nodes[(2, 0)].connectivity_score > 0.0


def test_prepared_graph_round_trip_preserves_heading_metadata():
    cells = ((0, 0), (1, 0), (2, 0))
    graph = build_navigation_voxel_graph(
        cells,
        _metrics(cells),
        cell_size_m=2.0,
    )
    restored = deserialize_navigation_voxel_graph(
        serialize_navigation_voxel_graph(graph)
    )

    assert restored.nodes == graph.nodes
    assert restored.edges == graph.edges
    assert restored.component_count == graph.component_count
