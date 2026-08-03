"""Tests for implicit orthogonal navigation connectivity."""

from __future__ import annotations

import math

import pytest

from caveviewer.core.navigation.cubic_graph import (
    CubicVoxelLimitExceededError,
    SparseCubicVoxelGraph,
    build_cubic_graph_from_local_volumes,
    build_route_ordered_cubic_path,
    pack_cubic_voxel_key,
    unpack_cubic_voxel_key,
)
from caveviewer.core.navigation.voxel_volume import LocalVoxelVolume


def test_packed_cubic_voxel_key_round_trips_signed_coordinates():
    key = (-1_000_000, 0, 1_000_000)

    assert unpack_cubic_voxel_key(pack_cubic_voxel_key(key)) == key


def test_implicit_graph_connects_aligned_one_metre_tiles():
    first = _volume(origin=(0.0, 0.0, 0.0), shape=(2, 1, 1))
    second = _volume(origin=(2.0, 0.0, 0.0), shape=(2, 1, 1))

    result = build_cubic_graph_from_local_volumes(
        (
            (first, ((0.5, 0.5, 0.5),)),
            (second, ((2.5, 0.5, 0.5),)),
        ),
        voxel_size_m=1.0,
    )
    path = result.graph.shortest_path((0, 0, 0), (3, 0, 0))

    assert result.graph.free_voxel_count == 4
    assert result.graph.component_sizes() == (4,)
    assert path is not None
    assert path.keys == ((0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0))
    assert path.distance_m == 3.0
    assert result.details["explicit_edge_count"] == 0


def test_diagonal_step_cannot_cut_an_unsupported_corner():
    disconnected = SparseCubicVoxelGraph.from_keys(
        ((0, 0, 0), (1, 1, 0)),
    )
    supported = SparseCubicVoxelGraph.from_keys(
        ((0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)),
    )

    assert disconnected.shortest_path((0, 0, 0), (1, 1, 0)) is None
    path = supported.shortest_path((0, 0, 0), (1, 1, 0))

    assert path is not None
    assert path.keys == ((0, 0, 0), (1, 1, 0))
    assert path.distance_m == pytest.approx(math.sqrt(2.0))


def test_shortest_path_routes_around_a_rejected_exact_edge():
    graph = SparseCubicVoxelGraph.from_keys(
        (
            (0, 0, 0),
            (1, 0, 0),
            (2, 0, 0),
            (0, 1, 0),
            (1, 1, 0),
            (2, 1, 0),
        )
    )

    path = graph.shortest_path(
        (0, 0, 0),
        (2, 0, 0),
        allow_diagonal=False,
        blocked_edges=(((1, 0, 0), (2, 0, 0)),),
    )

    assert path is not None
    assert path.keys == (
        (0, 0, 0),
        (1, 0, 0),
        (1, 1, 0),
        (2, 1, 0),
        (2, 0, 0),
    )


def test_horizontal_gate_ignores_untrusted_route_y_and_keeps_start_layer():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((x, 0, 0) for x in range(5))
        + tuple((x, 4, 0) for x in range(5))
    )

    result = graph.find_path_to_horizontal_gate(
        (0, 0, 0),
        (4.5, 1000.0, 0.5),
        max_horizontal_distance_m=0.51,
        allow_diagonal=False,
        max_expansions=16,
    )

    assert result.path is not None
    assert result.path.keys[-1] == (4, 0, 0)
    assert all(key[1] == 0 for key in result.path.keys)
    assert result.node_limit_reached is False


def test_route_ordered_cubic_path_shares_one_expansion_budget():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((x, 0, 0) for x in range(21))
        + tuple((10, y, 0) for y in range(1, 8))
    )

    result = build_route_ordered_cubic_path(
        graph,
        (
            (0.5, 99.0, 0.5),
            (10.5, -99.0, 0.5),
            (20.5, 99.0, 0.5),
        ),
        start_key=(0, 0, 0),
        terminal_keys=((20, 0, 0),),
        horizontal_gate_radius_m=0.51,
        max_expansions=32,
        waypoint_key_groups=(((10, 0, 0),),),
    )

    assert result.path is not None
    assert result.path.keys[0] == (0, 0, 0)
    assert result.path.keys[-1] == (20, 0, 0)
    assert result.details["expanded_voxel_count"] <= 32
    assert result.details["raw_route_y_used"] is False
    assert result.details["known_terminal_reached"] is True


def test_route_ordered_cubic_path_fails_whole_route_at_shared_budget():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((x, 0, 0) for x in range(12))
    )

    result = build_route_ordered_cubic_path(
        graph,
        ((0.5, 0.5, 0.5), (5.5, 0.5, 0.5), (11.5, 0.5, 0.5)),
        start_key=(0, 0, 0),
        terminal_keys=((11, 0, 0),),
        horizontal_gate_radius_m=0.51,
        max_expansions=4,
        waypoint_key_groups=(((5, 0, 0),),),
    )

    assert result.path is None
    assert result.details["reason"] == (
        "route_ordered_cubic_spine_expansion_limit_reached"
    )
    assert result.details["node_limit_reached"] is True
    assert result.details["known_terminal_reached"] is False


def test_route_ordered_cubic_path_rejects_revisited_waypoints():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((x, 0, 0) for x in range(5))
    )

    result = build_route_ordered_cubic_path(
        graph,
        (
            (0.5, 0.5, 0.5),
            (2.5, 0.5, 0.5),
            (1.5, 0.5, 0.5),
            (4.5, 0.5, 0.5),
        ),
        start_key=(0, 0, 0),
        terminal_keys=((4, 0, 0),),
        horizontal_gate_radius_m=0.51,
        max_expansions=32,
        waypoint_key_groups=(((2, 0, 0),), ((1, 0, 0),)),
    )

    assert result.path is None
    assert result.details["reason"] in {
        "route_ordered_cubic_spine_gate_unreachable",
        "route_ordered_cubic_spine_revisit_detected",
    }
    assert result.details["known_terminal_reached"] is False


def test_route_ordered_cubic_path_requires_complete_surface_gap_gates():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((x, 0, 0) for x in range(4))
    )

    result = build_route_ordered_cubic_path(
        graph,
        tuple(graph.voxel_center((x, 0, 0)) for x in range(4)),
        start_key=(0, 0, 0),
        terminal_keys=((3, 0, 0),),
        horizontal_gate_radius_m=0.51,
        max_expansions=32,
        waypoint_key_groups=(((1, 0, 0),), ()),
        require_waypoint_key_groups=True,
    )

    assert result.path is None
    assert result.details["reason"] == (
        "route_ordered_cubic_spine_waypoint_evidence_missing"
    )


@pytest.mark.parametrize(
    "waypoint_groups",
    (
        (((1, 0, 0),),),
        (
            ((1, 0, 0),),
            ((2, 0, 0),),
            ((2, 0, 0),),
        ),
    ),
)
def test_route_ordered_cubic_path_requires_exact_surface_gap_gate_count(
    waypoint_groups,
):
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((x, 0, 0) for x in range(4))
    )

    result = build_route_ordered_cubic_path(
        graph,
        tuple(graph.voxel_center((x, 0, 0)) for x in range(4)),
        start_key=(0, 0, 0),
        terminal_keys=((3, 0, 0),),
        horizontal_gate_radius_m=0.51,
        max_expansions=32,
        waypoint_key_groups=waypoint_groups,
        require_waypoint_key_groups=True,
    )

    assert result.path is None
    assert result.details["reason"] == (
        "route_ordered_cubic_spine_waypoint_evidence_missing"
    )
    assert result.details["expected_intermediate_gate_count"] == 2


def test_stacked_passages_remain_separate_without_vertical_evidence():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((x, 0, 0) for x in range(4))
        + tuple((x, 2, 0) for x in range(4))
    )

    assert graph.component_sizes() == (4, 4)
    assert graph.shortest_path((0, 0, 0), (3, 2, 0)) is None


def test_connected_component_retains_only_the_terminal_seam_component():
    graph = SparseCubicVoxelGraph.from_keys(
        (
            (0, 0, 0),
            (1, 0, 0),
            (2, 0, 0),
            (8, 0, 0),
            (9, 0, 0),
        )
    )

    component = graph.connected_component((2, 0, 0))

    assert component.keys() == ((0, 0, 0), (1, 0, 0), (2, 0, 0))
    assert component.component_sizes() == (3,)
    assert not component.contains_key((8, 0, 0))


def test_connected_component_and_builder_enforce_voxel_limits():
    graph = SparseCubicVoxelGraph.from_keys(tuple((x, 0, 0) for x in range(4)))

    with pytest.raises(ValueError, match="component limit"):
        graph.connected_component((0, 0, 0), max_voxels=3)
    with pytest.raises(CubicVoxelLimitExceededError, match="free-space limit"):
        build_cubic_graph_from_local_volumes(
            (
                (
                    _volume(origin=(0.0, 0.0, 0.0), shape=(4, 1, 1)),
                    ((0.5, 0.5, 0.5),),
                ),
            ),
            max_free_voxels=3,
        )


def test_overlapping_surface_occupancy_overrides_free_evidence():
    free = _volume(origin=(0.0, 0.0, 0.0), shape=(2, 1, 1))
    blocked = _volume(
        origin=(1.0, 0.0, 0.0),
        shape=(1, 1, 1),
        surface_cells=frozenset({(0, 0, 0)}),
    )

    result = build_cubic_graph_from_local_volumes(
        (
            (free, ((0.5, 0.5, 0.5),)),
            (blocked, ()),
        )
    )

    assert result.graph.contains_key((0, 0, 0))
    assert not result.graph.contains_key((1, 0, 0))
    assert result.details["blocked_free_conflict_count"] == 1


def test_global_merge_can_recover_free_evidence_omitted_by_local_seed():
    divided = _volume(
        origin=(0.0, 0.0, 0.0),
        shape=(3, 1, 1),
        surface_cells=frozenset({(1, 0, 0)}),
    )

    seeded = build_cubic_graph_from_local_volumes(
        ((divided, ((0.5, 0.5, 0.5),)),),
    )
    complete = build_cubic_graph_from_local_volumes(
        ((divided, ((0.5, 0.5, 0.5),)),),
        include_all_filtered_free_cells=True,
    )

    assert seeded.graph.keys() == ((0, 0, 0),)
    assert complete.graph.keys() == ((0, 0, 0), (2, 0, 0))
    assert complete.details["source_free_space_method"] == (
        "all_filtered_non_surface_cells_v1"
    )


def test_builder_rejects_wrong_resolution_or_misaligned_source_volume():
    coarse = _volume(
        origin=(0.0, 0.0, 0.0),
        shape=(1, 1, 1),
        voxel_size_m=2.0,
    )
    shifted = _volume(
        origin=(0.5, 0.0, 0.0),
        shape=(1, 1, 1),
    )

    with pytest.raises(ValueError, match="requested orthogonal resolution"):
        build_cubic_graph_from_local_volumes(
            ((coarse, ((1.0, 1.0, 1.0),)),),
            voxel_size_m=1.0,
        )
    with pytest.raises(ValueError, match="global orthogonal grid"):
        build_cubic_graph_from_local_volumes(
            ((shifted, ((1.0, 0.5, 0.5),)),),
            voxel_size_m=1.0,
        )


def test_builder_rejects_truncated_surface_evidence():
    truncated = _volume(
        origin=(0.0, 0.0, 0.0),
        shape=(1, 1, 1),
        sampling_truncated=True,
    )

    with pytest.raises(ValueError, match="truncated surface evidence"):
        build_cubic_graph_from_local_volumes(
            ((truncated, ((0.5, 0.5, 0.5),)),),
        )

    accepted = build_cubic_graph_from_local_volumes(
        ((truncated, ((0.5, 0.5, 0.5),)),),
        allow_truncated_surface_evidence=True,
    )

    assert accepted.details["truncated_source_volume_count"] == 1


def test_nearest_key_uses_voxel_centers_and_a_bounded_radius():
    graph = SparseCubicVoxelGraph.from_keys(((0, 0, 0), (4, 0, 0)))

    key, distance_m = graph.nearest_key(
        (3.9, 0.5, 0.5),
        max_distance_m=1.0,
    )
    missing, missing_distance = graph.nearest_key(
        (2.5, 0.5, 0.5),
        max_distance_m=1.0,
    )

    assert key == (4, 0, 0)
    assert distance_m == pytest.approx(0.6)
    assert missing is None
    assert math.isinf(missing_distance)


def test_nearest_key_rejects_malformed_input():
    graph = SparseCubicVoxelGraph.from_keys(((0, 0, 0),))

    malformed, malformed_distance = graph.nearest_key(
        ("bad", 0.0, 0.0),
        max_distance_m=1.0,
    )

    assert malformed is None
    assert math.isinf(malformed_distance)


def test_quarter_metre_vertical_graph_uses_physical_centres_and_costs():
    graph = SparseCubicVoxelGraph.from_keys(
        ((0, 0, 0), (0, 1, 0), (0, 2, 0), (1, 2, 0)),
        voxel_size_m=1.0,
        vertical_voxel_size_m=0.25,
    )

    path = graph.shortest_path(
        (0, 0, 0),
        (0, 2, 0),
        allow_diagonal=False,
    )
    nearby = graph.keys_within_distance(
        (0.5, 0.625, 0.5),
        max_distance_m=0.3,
    )

    assert graph.cell_size_m == (1.0, 0.25, 1.0)
    assert graph.cell_volume_m3 == 0.25
    assert graph.voxel_center((0, 1, 0)) == (0.5, 0.375, 0.5)
    assert path is not None
    assert path.distance_m == 0.5
    assert [key for key, _distance in nearby] == [(0, 2, 0), (0, 1, 0)]


def _volume(
    *,
    origin,
    shape,
    voxel_size_m=1.0,
    vertical_voxel_size_m=None,
    surface_cells=frozenset(),
    sampling_truncated=False,
):
    return LocalVoxelVolume(
        voxel_size_m=float(voxel_size_m),
        vertical_voxel_size_m=vertical_voxel_size_m,
        origin=tuple(float(value) for value in origin),
        shape=tuple(int(value) for value in shape),
        surface_cells=surface_cells,
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=bool(sampling_truncated),
        max_clearance_search_cells=4,
    )
