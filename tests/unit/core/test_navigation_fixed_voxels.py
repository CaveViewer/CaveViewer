"""Tests for fixed-orthogonal V12 navigation voxel chunks."""

from __future__ import annotations

import numpy as np
import pytest

from caveviewer.core.navigation.fixed_voxels import (
    FIXED_ORTHOGONAL_VOXEL_METHOD,
    FixedVoxelRegion,
    build_fixed_isotropic_voxel_tiles,
    build_fixed_orthogonal_voxel_tiles,
    segment_voxel_probe_fractions,
)


def test_segment_probe_samples_every_narrow_diagonal_voxel_interval():
    fractions = segment_voxel_probe_fractions(
        (0.5, 0.5, 0.5),
        (10.5, 0.5, 9.5),
        lattice_spacing_m=(1.0, 1.0, 1.0),
        maximum_sample_spacing_m=0.125,
    )

    x_boundary = 0.05
    z_boundary = 1.0 / 18.0
    assert x_boundary in fractions
    assert z_boundary in fractions
    assert any(
        x_boundary < fraction < z_boundary
        for fraction in fractions
    )


def test_segment_probe_preserves_quarter_metre_vertical_boundaries():
    fractions = segment_voxel_probe_fractions(
        (0.5, 0.125, 0.5),
        (0.5, 1.125, 0.5),
        lattice_spacing_m=(1.0, 0.25, 1.0),
        maximum_sample_spacing_m=0.125,
    )

    assert {0.125, 0.375, 0.625, 0.875}.issubset(fractions)


def test_subdivides_large_region_without_coarsening_voxels():
    result = build_fixed_isotropic_voxel_tiles(
        (
            FixedVoxelRegion(
                bounds_min=(0.0, 0.0, 0.0),
                bounds_max=(4.0, 12.0, 4.0),
                seed_points=(
                    (1.5, 1.5, 1.5),
                    (1.5, 5.5, 1.5),
                    (1.5, 9.5, 1.5),
                ),
            ),
        ),
        triangle_provider=_floor_provider,
        voxel_size_m=1.0,
        chunk_edge_m=32.0,
        max_voxels_per_chunk=125,
        max_surface_samples_per_chunk=10_000,
    )

    assert len(result.tiles) > 1
    assert all(tile.voxel_size_m == 1.0 for tile in result.tiles)
    assert all(tile.voxel_count <= 125 for tile in result.tiles)
    assert all(not tile.sampling_truncated for tile in result.tiles)
    assert result.details["sampling_truncated"] is False
    assert result.details["subdivision_count"] > 0


def test_retains_seeded_empty_chunk_but_discards_unseeded_empty_chunk():
    result = build_fixed_isotropic_voxel_tiles(
        (
            FixedVoxelRegion(
                bounds_min=(0.0, 0.0, 0.0),
                bounds_max=(2.0, 2.0, 2.0),
                seed_points=((0.5, 0.5, 0.5),),
            ),
            FixedVoxelRegion(
                bounds_min=(4.0, 0.0, 0.0),
                bounds_max=(6.0, 2.0, 2.0),
            ),
        ),
        triangle_provider=lambda _lower, _upper: (),
    )

    assert len(result.tiles) == 1
    assert result.tile_seed_points == (((0.5, 0.5, 0.5),),)
    assert result.details["discarded_empty_chunk_count"] == 1


def test_adjacent_fixed_chunks_share_a_one_voxel_seam_shell():
    result = build_fixed_isotropic_voxel_tiles(
        (
            FixedVoxelRegion(
                bounds_min=(0.0, 0.0, 0.0),
                bounds_max=(4.0, 1.0, 1.0),
                seed_points=(
                    (0.5, 0.5, 0.5),
                    (2.5, 0.5, 0.5),
                ),
            ),
        ),
        triangle_provider=lambda _lower, _upper: (),
        voxel_size_m=1.0,
        chunk_edge_m=2.0,
    )

    assert len(result.tiles) == 2
    first, second = sorted(result.tiles, key=lambda tile: tile.origin)
    assert first.bounds_max[0] - second.bounds_min[0] == 1.0
    assert first.voxel_size_m == second.voxel_size_m == 1.0


def test_fails_closed_when_surface_sampling_cannot_complete():
    triangles = np.repeat(
        np.asarray(
            [[[0.0, 0.0, 0.0], [0.9, 0.0, 0.0], [0.0, 0.0, 0.9]]],
            dtype=np.float64,
        ),
        repeats=8,
        axis=0,
    )

    with pytest.raises(ValueError, match="sampling remains truncated"):
        build_fixed_isotropic_voxel_tiles(
            (
                FixedVoxelRegion(
                    bounds_min=(0.0, 0.0, 0.0),
                    bounds_max=(1.0, 1.0, 1.0),
                    seed_points=((0.5, 0.5, 0.5),),
                ),
            ),
            triangle_provider=lambda _lower, _upper: (triangles,),
            max_surface_samples_per_chunk=1,
        )


def test_rejects_chunk_count_overflow_before_rasterization():
    with pytest.raises(ValueError, match="chunk limit exceeded"):
        build_fixed_isotropic_voxel_tiles(
            (
                FixedVoxelRegion(
                    bounds_min=(0.0, 0.0, 0.0),
                    bounds_max=(10.0, 1.0, 1.0),
                ),
            ),
            triangle_provider=lambda _lower, _upper: (),
            chunk_edge_m=2.0,
            max_chunks=2,
        )


def test_quarter_metre_vertical_cells_preserve_half_metre_passage_layer():
    result = build_fixed_orthogonal_voxel_tiles(
        (
            FixedVoxelRegion(
                bounds_min=(0.0, 0.0, 0.0),
                bounds_max=(2.0, 0.5, 2.0),
                seed_points=((0.5, 0.375, 0.5),),
            ),
        ),
        triangle_provider=_half_metre_passage_provider,
        voxel_size_m=1.0,
        vertical_voxel_size_m=0.25,
        surface_inflation_cells=0,
    )

    assert result.details["method"] == FIXED_ORTHOGONAL_VOXEL_METHOD
    assert result.details["cell_size_m"] == [1.0, 0.25, 1.0]
    assert len(result.tiles) == 1
    tile = result.tiles[0]
    assert tile.cell_size_m == (1.0, 0.25, 1.0)
    assert tile.voxel_index((0.5, 0.125, 0.5)) in tile.surface_cells
    assert tile.voxel_index((0.5, 0.375, 0.5)) not in tile.surface_cells
    assert tile.voxel_index((0.5, 0.5, 0.5)) in tile.surface_cells


def _floor_provider(_bounds_min, _bounds_max):
    return (
        np.asarray(
            [
                [[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [4.0, 0.0, 4.0]],
                [[0.0, 0.0, 0.0], [4.0, 0.0, 4.0], [0.0, 0.0, 4.0]],
            ],
            dtype=np.float64,
        ),
    )


def _half_metre_passage_provider(_bounds_min, _bounds_max):
    floor = np.asarray(
        [
            [[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [2.0, 0.0, 2.0]],
            [[0.0, 0.0, 0.0], [2.0, 0.0, 2.0], [0.0, 0.0, 2.0]],
        ],
        dtype=np.float64,
    )
    ceiling = floor.copy()
    ceiling[:, :, 1] = 0.5
    return (np.concatenate((floor, ceiling), axis=0),)
