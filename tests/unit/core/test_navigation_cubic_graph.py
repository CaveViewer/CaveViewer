"""Tests for implicit isotropic cubic navigation connectivity."""

from __future__ import annotations

import math

import pytest

from caveviewer.core.navigation.cubic_graph import (
    SparseCubicVoxelGraph,
    build_cubic_graph_from_local_volumes,
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


def test_stacked_passages_remain_separate_without_vertical_evidence():
    graph = SparseCubicVoxelGraph.from_keys(
        tuple((x, 0, 0) for x in range(4))
        + tuple((x, 2, 0) for x in range(4))
    )

    assert graph.component_sizes() == (4, 4)
    assert graph.shortest_path((0, 0, 0), (3, 2, 0)) is None


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


def test_builder_rejects_non_cubic_or_misaligned_source_volume():
    coarse = _volume(
        origin=(0.0, 0.0, 0.0),
        shape=(1, 1, 1),
        voxel_size_m=2.0,
    )
    shifted = _volume(
        origin=(0.5, 0.0, 0.0),
        shape=(1, 1, 1),
    )

    with pytest.raises(ValueError, match="requested cubic resolution"):
        build_cubic_graph_from_local_volumes(
            ((coarse, ((1.0, 1.0, 1.0),)),),
            voxel_size_m=1.0,
        )
    with pytest.raises(ValueError, match="global cubic grid"):
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


def _volume(
    *,
    origin,
    shape,
    voxel_size_m=1.0,
    surface_cells=frozenset(),
    sampling_truncated=False,
):
    return LocalVoxelVolume(
        voxel_size_m=float(voxel_size_m),
        origin=tuple(float(value) for value in origin),
        shape=tuple(int(value) for value in shape),
        surface_cells=surface_cells,
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=bool(sampling_truncated),
        max_clearance_search_cells=4,
    )
