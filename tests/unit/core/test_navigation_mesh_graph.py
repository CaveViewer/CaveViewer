"""Regression tests for the conservative mesh-derived navigation roadmap."""

from __future__ import annotations

from collections.abc import Iterable
import math

import numpy as np

from caveviewer.core.navigation.mesh_graph import (
    MESH_NAVIGATION_GRAPH_METHOD,
    MeshNavigationGraphAnchor,
    MeshNavigationGraphConfig,
    build_goal_directed_seeded_mesh_navigation_path_graph,
    build_mesh_anchored_navigation_graph,
    build_mesh_navigation_graph,
    build_seeded_mesh_navigation_path_graph,
)


def test_mesh_graph_uses_paired_mesh_intervals_not_a_dense_voxel_field():
    triangles = _box_triangles((0.0, 0.0, 0.0), (8.0, 8.0, 8.0))
    result = build_mesh_navigation_graph(
        _manifest(),
        _route(),
        triangle_provider=_provider(triangles),
        edge_is_clear=lambda _first, _second: True,
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=2.0,
            vertical_sample_spacing_m=2.0,
            minimum_clearance_m=0.25,
            max_nodes=512,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
    )

    assert result.graph is not None
    assert result.graph.method == MESH_NAVIGATION_GRAPH_METHOD
    assert result.graph.edge_integrity_safe is True
    assert result.graph.motion_geometry_safe is True
    assert result.details["inside_evidence"] == "paired_vertical_mesh_intervals"
    assert result.details["paired_column_count"] == 16
    assert result.details["candidate_node_count"] == len(result.graph.nodes)
    assert all(
        0.25 < node.center[1] < 7.75
        for node in result.graph.nodes.values()
    )
    assert result.graph.edge_count > 0


def test_mesh_graph_rejects_open_vertical_mesh_evidence():
    triangles = _box_triangles(
        (0.0, 0.0, 0.0),
        (8.0, 8.0, 8.0),
        include_top=False,
    )
    result = build_mesh_navigation_graph(
        _manifest(),
        _route(),
        triangle_provider=_provider(triangles),
        edge_is_clear=lambda _first, _second: True,
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=2.0,
            max_nodes=512,
        ),
    )

    assert result.graph is None
    assert result.details["reason"] == "mesh_free_space_candidates_missing"
    assert result.details["unpaired_column_count"] == 16


def test_mesh_graph_keeps_mesh_rejected_handoff_in_separate_components():
    triangles = np.concatenate(
        (
            _box_triangles((0.0, 0.0, 0.0), (8.0, 8.0, 8.0)),
            _plane_x_triangles(4.0, 0.0, 8.0, 0.0, 8.0),
        ),
        axis=0,
    )

    def edge_is_clear(first: tuple[float, float, float], second: tuple[float, float, float]) -> bool:
        return not (
            min(first[0], second[0]) < 4.0 < max(first[0], second[0])
        )

    result = build_mesh_navigation_graph(
        _manifest(),
        _route(),
        triangle_provider=_provider(triangles),
        edge_is_clear=edge_is_clear,
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=2.0,
            vertical_sample_spacing_m=2.0,
            max_nodes=512,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
    )

    assert result.graph is not None
    assert result.graph.component_count == 2
    assert result.details["mesh_rejected_edge_count"] > 0


def test_mesh_graph_keeps_a_second_directional_candidate_after_a_mesh_rejection():
    """A sliver on the nearest edge must not disconnect a valid passage."""
    first = (0.0, 1.0, 0.0)
    blocked = (1.0, 1.0, 0.0)
    alternate = (2.0, 1.0, 0.0)
    anchors = tuple(
        MeshNavigationGraphAnchor(
            point=point,
            footprint_cell=(0, 0),
            clearance_m=1.0,
        )
        for point in (first, blocked, alternate)
    )

    result = build_mesh_anchored_navigation_graph(
        anchors,
        footprint_cell_size_m=8.0,
        triangle_provider=lambda _lower, _upper: (),
        edge_is_clear=lambda source, target: {
            tuple(source),
            tuple(target),
        }
        != {first, blocked},
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=1.0,
            vertical_sample_spacing_m=1.0,
            max_nodes=32,
            max_edges_per_node=4,
            max_edge_candidates_per_node=4,
            max_edge_candidates_per_direction=2,
            max_edge_distance_m=2.1,
            max_vertical_edge_distance_m=1.0,
        ),
    )

    assert result.graph is not None
    assert result.graph.component_count == 1
    assert result.details["mesh_rejected_edge_count"] >= 1


def test_mesh_graph_uses_only_the_verified_target_hint_as_a_terminal():
    triangles = _box_triangles((0.0, 0.0, 0.0), (8.0, 8.0, 8.0))
    result = build_mesh_navigation_graph(
        _manifest(),
        _route(),
        triangle_provider=_provider(triangles),
        edge_is_clear=lambda _first, _second: True,
        terminal_hint_points=((7.0, 4.0, 4.0),),
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=2.0,
            vertical_sample_spacing_m=2.0,
            max_nodes=512,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
    )

    assert result.graph is not None
    terminal_keys = [
        key for key, node in result.graph.nodes.items() if node.terminal
    ]
    assert len(terminal_keys) == 1
    terminal = result.graph.nodes[terminal_keys[0]]
    assert terminal.center[0] > 5.0
    assert result.details["terminal_hint_count"] == 1
    assert result.details["terminal_hint_node_count"] == 1


def test_seeded_mesh_graph_persists_only_one_exact_connected_path():
    free_keys = {(x, 0, 0) for x in range(5)}

    result = build_seeded_mesh_navigation_path_graph(
        ((1.0, 1.0, 1.0),),
        footprint_cell_size_m=32.0,
        component_cells=((0, 0),),
        point_probe=_grid_probe(free_keys),
        edge_is_clear=lambda _first, _second: True,
        terminal_hint_points=(
            (5.0, 1.0, 1.0),
            (9.0, 1.0, 1.0),
        ),
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=2.0,
            vertical_sample_spacing_m=2.0,
            minimum_clearance_m=0.25,
            max_nodes=64,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
    )

    assert result.graph is not None
    assert result.details["component_complete"] is True
    assert result.details["reachable_node_count"] == 5
    assert result.details["persisted_path_node_count"] == 5
    assert result.details["terminal_hint_index"] == 1
    assert len(result.graph.nodes) == 5
    assert result.graph.component_count == 1
    terminal_keys = [
        key for key, node in result.graph.nodes.items() if node.terminal
    ]
    assert terminal_keys == [(4, 0, 0)]


def test_seeded_mesh_graph_skips_a_hint_behind_a_rejected_mesh_edge():
    free_keys = {(x, 0, 0) for x in range(5)}

    def edge_is_clear(first, second):
        return max(first[0], second[0]) <= 5.0

    result = build_seeded_mesh_navigation_path_graph(
        ((1.0, 1.0, 1.0),),
        footprint_cell_size_m=32.0,
        component_cells=((0, 0),),
        point_probe=_grid_probe(free_keys),
        edge_is_clear=edge_is_clear,
        terminal_hint_points=(
            (5.0, 1.0, 1.0),
            (9.0, 1.0, 1.0),
        ),
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=2.0,
            vertical_sample_spacing_m=2.0,
            max_nodes=64,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
    )

    assert result.graph is not None
    assert result.details["mesh_rejected_edge_count"] >= 1
    assert result.details["terminal_hint_index"] == 0
    assert (4, 0, 0) not in result.graph.nodes
    assert result.graph.nodes[(2, 0, 0)].terminal is True


def test_seeded_mesh_graph_fails_closed_when_component_hits_node_limit():
    free_keys = {(x, 0, 0) for x in range(10)}

    result = build_seeded_mesh_navigation_path_graph(
        ((1.0, 1.0, 1.0),),
        footprint_cell_size_m=32.0,
        component_cells=((0, 0),),
        point_probe=_grid_probe(free_keys),
        edge_is_clear=lambda _first, _second: True,
        terminal_hint_points=((19.0, 1.0, 1.0),),
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=2.0,
            vertical_sample_spacing_m=2.0,
            max_nodes=3,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
    )

    assert result.graph is None
    assert result.details["node_limit_reached"] is True
    assert result.details["reason"] == "seeded_mesh_graph_node_limit_reached"


def test_goal_directed_mesh_graph_reaches_known_terminal_without_full_flood():
    free_keys = {(x, 0, 0) for x in range(9)}

    result = build_goal_directed_seeded_mesh_navigation_path_graph(
        ((0.5, 0.5, 0.5),),
        footprint_cell_size_m=32.0,
        component_cells=((0, 0),),
        point_probe=_grid_probe(free_keys, spacing_m=1.0),
        edge_is_clear=lambda _first, _second: True,
        terminal_point=(8.5, 0.5, 0.5),
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=1.0,
            vertical_sample_spacing_m=1.0,
            max_nodes=64,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
    )

    assert result.graph is not None
    assert result.details["reason"] == (
        "goal_directed_mesh_terminal_path_built"
    )
    assert result.details["search_method"] == "weighted_a_star"
    assert result.details["expanded_node_count"] < 16
    assert result.details["terminal_attachment_distance_m"] <= math.sqrt(3.0)
    assert result.graph.terminal_count == 1
    assert max(node.center[0] for node in result.graph.nodes.values()) >= 7.5


def test_goal_directed_mesh_graph_uses_later_entry_when_first_is_isolated():
    free_keys = {(0, 0, 0), *((x, 0, 0) for x in range(3, 9))}
    guides = tuple((float(x) + 0.5, 0.5, 0.5) for x in range(9))

    result = build_goal_directed_seeded_mesh_navigation_path_graph(
        (guides[0], guides[3]),
        footprint_cell_size_m=32.0,
        component_cells=((0, 0),),
        point_probe=_grid_probe(free_keys, spacing_m=1.0),
        edge_is_clear=lambda _first, _second: True,
        terminal_point=guides[-1],
        route_guide_points=guides,
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=1.0,
            vertical_sample_spacing_m=1.0,
            max_nodes=64,
            max_edge_distance_m=4.0,
            max_vertical_edge_distance_m=4.0,
        ),
    )

    assert result.graph is not None
    assert result.details["entry_seed_candidate_count"] == 2
    assert result.details["seed_hint_index"] == 1
    assert min(node.center[0] for node in result.graph.nodes.values()) == 3.5
    assert result.graph.terminal_count == 1


def test_goal_directed_mesh_graph_uses_exact_voxel_sampled_guide_portal():
    def edge_is_clear(first, second):
        return math.dist(first, second) >= 5.0

    result = build_goal_directed_seeded_mesh_navigation_path_graph(
        ((1.0, 1.0, 1.0),),
        footprint_cell_size_m=32.0,
        component_cells=((0, 0),),
        point_probe=lambda _point: (True, 2.0),
        edge_is_clear=edge_is_clear,
        terminal_point=(7.0, 1.0, 1.0),
        route_guide_points=((1.0, 1.0, 1.0), (7.0, 1.0, 1.0)),
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=2.0,
            vertical_sample_spacing_m=2.0,
            max_nodes=64,
            max_edge_distance_m=8.0,
            max_vertical_edge_distance_m=4.0,
        ),
    )

    assert result.graph is not None
    assert result.details["guided_portal_accepted_count"] >= 1
    assert result.details["guided_portal_voxel_probe_count"] >= 1
    assert len(result.graph.nodes) == 2
    assert result.graph.max_edge_distance_m == 6.0
    assert result.graph.motion_geometry_safe is True


def test_goal_directed_mesh_graph_rejects_guide_portal_without_voxel_evidence():
    endpoints = {(1.0, 1.0, 1.0), (7.0, 1.0, 1.0)}

    result = build_goal_directed_seeded_mesh_navigation_path_graph(
        ((1.0, 1.0, 1.0),),
        footprint_cell_size_m=32.0,
        component_cells=((0, 0),),
        point_probe=lambda point: (
            (True, 2.0) if tuple(point) in endpoints else None
        ),
        edge_is_clear=lambda _first, _second: True,
        terminal_point=(7.0, 1.0, 1.0),
        route_guide_points=((1.0, 1.0, 1.0), (7.0, 1.0, 1.0)),
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=2.0,
            vertical_sample_spacing_m=2.0,
            max_nodes=64,
            max_edge_distance_m=8.0,
            max_vertical_edge_distance_m=4.0,
        ),
    )

    assert result.graph is None
    assert result.details["guided_portal_voxel_rejection_count"] >= 1
    assert result.details["reason"] == (
        "goal_directed_mesh_graph_terminal_unreachable"
    )


def test_goal_directed_mesh_graph_rejects_blocked_local_edge_midpoint():
    free_keys = {
        (0, 0, 0),
        (1, 0, 0),
        (0, 0, 1),
        (1, 0, 1),
        (2, 0, 0),
        (3, 0, 0),
    }
    blocked_midpoint = (1.0, 0.5, 0.5)

    def point_probe(point):
        if tuple(point) == blocked_midpoint:
            return False, 0.0
        key = tuple(int(math.floor(value)) for value in point)
        return (True, 2.0) if key in free_keys else None

    result = build_goal_directed_seeded_mesh_navigation_path_graph(
        ((0.5, 0.5, 0.5),),
        footprint_cell_size_m=32.0,
        component_cells=((0, 0),),
        point_probe=point_probe,
        edge_is_clear=lambda _first, _second: True,
        terminal_point=(3.5, 0.5, 0.5),
        route_guide_points=((0.5, 0.5, 0.5), (3.5, 0.5, 0.5)),
        config=MeshNavigationGraphConfig(
            horizontal_sample_spacing_m=1.0,
            vertical_sample_spacing_m=1.0,
            max_nodes=64,
            max_edge_candidates_per_node=12,
            max_edge_distance_m=1.75,
            max_vertical_edge_distance_m=1.0,
        ),
    )

    assert result.graph is not None
    assert result.details["edge_voxel_probe_count"] > 0
    assert result.details["edge_voxel_rejection_count"] > 0
    route_centers = tuple(node.center for node in result.graph.nodes.values())
    assert any(center[2] > 0.5 for center in route_centers)
    assert all(
        {edge.source, edge.target} != {(0, 0, 0), (1, 0, 0)}
        for edges in result.graph.edges.values()
        for edge in edges
    )


def _manifest() -> dict[str, object]:
    return {
        "footprint_cell_size": 8.0,
        "chunks": {
            "0:0": {
                "bounds_min": [0.0, 0.0, 0.0],
                "bounds_max": [8.0, 8.0, 8.0],
            }
        },
    }


def _grid_probe(
    free_keys: set[tuple[int, int, int]],
    *,
    spacing_m: float = 2.0,
):
    def probe(point: tuple[float, float, float]):
        key = tuple(int(math.floor(value / spacing_m)) for value in point)
        if key not in free_keys:
            return None
        return True, 2.0

    return probe


def _route() -> dict[str, object]:
    return {
        "footprint_cell_size": 8.0,
        "component_cells": [0, 0],
    }


def _provider(triangles: np.ndarray):
    def provider(
        bounds_min: tuple[float, float, float],
        bounds_max: tuple[float, float, float],
    ) -> Iterable[np.ndarray]:
        lower = np.minimum(np.asarray(bounds_min), np.asarray(bounds_max))
        upper = np.maximum(np.asarray(bounds_min), np.asarray(bounds_max))
        triangle_min = triangles.min(axis=1)
        triangle_max = triangles.max(axis=1)
        intersects = np.all(triangle_max >= lower, axis=1) & np.all(
            upper >= triangle_min,
            axis=1,
        )
        if np.any(intersects):
            yield triangles[intersects]

    return provider


def _box_triangles(
    lower: tuple[float, float, float],
    upper: tuple[float, float, float],
    *,
    include_top: bool = True,
) -> np.ndarray:
    x0, y0, z0 = lower
    x1, y1, z1 = upper
    faces = [
        ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)),
        ((x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0)),
        ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)),
        ((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0)),
        ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)),
    ]
    if include_top:
        faces.append(((x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0)))
    return np.asarray(
        [
            (first, second, third)
            for first, second, third, fourth in faces
            for first, second, third in ((first, second, third), (first, third, fourth))
        ],
        dtype=np.float64,
    )


def _plane_x_triangles(
    x: float,
    y0: float,
    y1: float,
    z0: float,
    z1: float,
) -> np.ndarray:
    return np.asarray(
        [
            ((x, y0, z0), (x, y1, z0), (x, y1, z1)),
            ((x, y0, z0), (x, y1, z1), (x, y0, z1)),
        ],
        dtype=np.float64,
    )
