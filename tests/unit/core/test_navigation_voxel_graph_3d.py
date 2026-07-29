"""Tests for the bounded true-3D navigation graph."""

from __future__ import annotations

from caveviewer.core.navigation.voxel_graph_3d import (
    NAVIGATION_VOXEL_3D_GRAPH_METHOD,
    NavigationVoxel3DMetric,
    accumulate_navigation_voxel_3d_sample,
    build_navigation_voxel_3d_graph,
    deserialize_navigation_voxel_3d_graph,
    finalize_navigation_voxel_3d_metrics,
    serialize_navigation_voxel_3d_graph,
)


def _metric(
    key: tuple[int, int, int],
    *,
    progress: float,
) -> NavigationVoxel3DMetric:
    return NavigationVoxel3DMetric(
        center=(key[0] + 0.5, key[1] + 0.5, key[2] + 0.5),
        footprint_cell=(key[0], key[2]),
        available_volume_m3=2.0,
        free_voxel_count=2,
        min_clearance_m=1.0,
        mean_clearance_m=1.5,
        progress_m=progress,
    )


def test_true_3d_graph_keeps_stacked_passages_separate():
    metrics = {
        (0, 0, 0): _metric((0, 0, 0), progress=0.0),
        (1, 0, 0): _metric((1, 0, 0), progress=1.0),
        (0, 2, 0): _metric((0, 2, 0), progress=0.0),
        (1, 2, 0): _metric((1, 2, 0), progress=1.0),
    }

    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )

    assert graph.method == NAVIGATION_VOXEL_3D_GRAPH_METHOD
    assert graph.component_count == 2
    assert graph.nodes[(0, 0, 0)].component_id != graph.nodes[(0, 2, 0)].component_id


def test_true_3d_graph_preserves_all_cardinal_moves_when_edges_are_capped():
    metrics = {
        (x, y, z): _metric((x, y, z), progress=float(x))
        for x in range(-1, 2)
        for y in range(-1, 2)
        for z in range(-1, 2)
    }

    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        max_edges_per_node=6,
        max_total_edges=10_000,
    )

    targets = {edge.target for edge in graph.outgoing((0, 0, 0))}
    assert targets == {
        (1, 0, 0),
        (-1, 0, 0),
        (0, 1, 0),
        (0, -1, 0),
        (0, 0, 1),
        (0, 0, -1),
    }


def test_true_3d_graph_never_exceeds_global_edge_budget():
    metrics = {
        (x, y, z): _metric((x, y, z), progress=float(x))
        for x in range(4)
        for y in range(2)
        for z in range(4)
    }

    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        max_total_edges=10,
    )

    assert graph.edge_count <= 10


def test_true_3d_graph_labels_terminal_and_dead_end_topology():
    keys = ((0, 0, 0), (1, 0, 0), (2, 0, 0), (1, 0, 1))
    metrics = {
        key: _metric(key, progress=float(index))
        for index, key in enumerate(keys[:3])
    }
    metrics[keys[3]] = _metric(keys[3], progress=1.0)

    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )

    assert graph.nodes[(2, 0, 0)].terminal is True
    assert graph.nodes[(1, 0, 1)].dead_end is True
    assert graph.nodes[(1, 0, 1)].terminal is True


def test_true_3d_graph_round_trip_preserves_nodes_and_edges():
    metrics = {
        key: _metric(key, progress=float(key[0]))
        for key in ((0, 0, 0), (1, 0, 0), (1, 1, 0))
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
    )

    restored = deserialize_navigation_voxel_3d_graph(
        serialize_navigation_voxel_3d_graph(graph)
    )

    assert restored.method == graph.method
    assert restored.nodes == graph.nodes
    assert restored.edge_count == graph.edge_count


def test_true_3d_metrics_coarsen_to_consumer_hardware_bound():
    accumulator = {}
    for x in range(6):
        for y in range(4):
            for z in range(6):
                accumulate_navigation_voxel_3d_sample(
                    accumulator,
                    (x + 0.5, y + 0.5, z + 0.5),
                    grid_size_m=1.0,
                    clearance_m=1.0,
                    volume_m3=1.0,
                    progress_m=float(x),
                )

    metrics, grid_size = finalize_navigation_voxel_3d_metrics(
        accumulator,
        grid_size_m=1.0,
        max_nodes=12,
    )

    assert len(metrics) <= 12
    assert grid_size[1] >= 1.0
    for key, metric in metrics.items():
        for axis in range(3):
            lower = key[axis] * grid_size[axis]
            upper = (key[axis] + 1) * grid_size[axis]
            assert lower <= metric.center[axis] < upper


def test_true_3d_metrics_accept_anisotropic_horizontal_buckets():
    accumulator = {}
    accumulate_navigation_voxel_3d_sample(
        accumulator,
        (3.5, 2.5, 3.5),
        grid_size_m=(4.0, 1.0, 4.0),
        clearance_m=1.0,
        volume_m3=1.0,
        progress_m=2.0,
    )

    metrics, grid_size = finalize_navigation_voxel_3d_metrics(
        accumulator,
        grid_size_m=(4.0, 1.0, 4.0),
        max_nodes=8,
    )

    assert tuple(metrics) == ((0, 2, 0),)
    assert grid_size == (4.0, 1.0, 4.0)


def test_true_3d_metrics_preserve_vertical_resolution_when_coarsening():
    accumulator = {}
    for x in range(12):
        for y in range(8):
            for z in range(12):
                accumulate_navigation_voxel_3d_sample(
                    accumulator,
                    (x + 0.5, y + 0.5, z + 0.5),
                    grid_size_m=1.0,
                    clearance_m=1.0,
                    volume_m3=1.0,
                    progress_m=float(x),
                )

    _metrics, grid_size = finalize_navigation_voxel_3d_metrics(
        accumulator,
        grid_size_m=1.0,
        max_nodes=64,
    )

    assert grid_size[1] <= 4.0
    assert grid_size[0] >= grid_size[1]


def test_true_3d_graph_reports_coarse_vertical_geometry_as_unsafe():
    metrics = {
        (0, 0, 0): _metric((0, 0, 0), progress=0.0),
        (1, 0, 0): _metric((1, 0, 0), progress=1.0),
    }

    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 32.0, 1.0),
    )

    assert graph.motion_geometry_safe is False
    assert graph.diagnostic_payload()["motion_geometry_safe"] is False
