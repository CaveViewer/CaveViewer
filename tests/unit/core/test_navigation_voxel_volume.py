"""Tests for bounded curvature-guided local voxel analysis."""

from __future__ import annotations

import numpy as np

from caveviewer.core.navigation.voxel_volume import (
    VOXEL_ANALYSIS_OUTCOME_NO_CURVATURE_REGION,
    VOXEL_ANALYSIS_OUTCOME_NO_TRIANGLES,
    VoxelVolumeConfig,
    analyze_curvature_guided_voxel_volume,
    build_curvature_guided_voxel_volume,
    build_surface_voxel_volume,
)


def _floor_triangle_mesh() -> np.ndarray:
    return np.asarray(
        [[
            [0.0, 0.0, 0.0],
            [20.0, 0.0, 0.0],
            [0.0, 0.0, 20.0],
        ]],
        dtype=np.float64,
    )


def test_surface_voxel_volume_is_bounded_and_reports_surface_clearance():
    volume = build_surface_voxel_volume(
        [_floor_triangle_mesh()],
        bounds_min=(-2.0, -2.0, -2.0),
        bounds_max=(22.0, 6.0, 22.0),
        config=VoxelVolumeConfig(
            voxel_size_m=1.0,
            max_voxels=512,
            surface_inflation_cells=0,
        ),
    )

    assert volume.voxel_count <= 512
    assert volume.triangle_count == 1
    assert volume.surface_sample_count > 0
    assert volume.surface_cells
    assert volume.surface_clearance_m(next(iter(volume.surface_cells))) == 0.0
    assert volume.diagnostic_payload()["voxel_count"] == volume.voxel_count


def test_curvature_guidance_skips_mesh_provider_for_straight_route():
    calls: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []

    def provider(bounds_min, bounds_max):
        calls.append((bounds_min, bounds_max))
        return (_floor_triangle_mesh(),)

    points = tuple((float(index * 4), 2.0, 0.0) for index in range(8))
    profile, volume = build_curvature_guided_voxel_volume(
        points,
        triangle_provider=provider,
    )

    assert profile.regions == ()
    assert volume is None
    assert calls == []


def test_curvature_guidance_reports_when_no_region_is_inside_analysis_horizon():
    points = tuple((float(index * 4), 2.0, 0.0) for index in range(8))

    analysis = analyze_curvature_guided_voxel_volume(
        points,
        triangle_provider=lambda _bounds_min, _bounds_max: (_floor_triangle_mesh(),),
        max_distance_m=1.0,
    )

    assert analysis.outcome == VOXEL_ANALYSIS_OUTCOME_NO_CURVATURE_REGION
    assert analysis.volume is None
    assert analysis.selected_regions == ()
    assert analysis.diagnostic_payload()["triangle_count"] == 0


def test_curvature_guidance_reports_when_selected_region_has_no_triangles():
    points = (
        (0.0, 2.0, 0.0),
        (4.0, 2.0, 0.0),
        (8.0, 2.0, 0.0),
        (8.0, 2.0, 4.0),
        (8.0, 2.0, 8.0),
    )

    analysis = analyze_curvature_guided_voxel_volume(
        points,
        triangle_provider=lambda _bounds_min, _bounds_max: (),
        voxel_size_m=1.0,
        max_regions=1,
        max_voxels=1024,
    )

    assert analysis.outcome == VOXEL_ANALYSIS_OUTCOME_NO_TRIANGLES
    assert analysis.volume is None
    assert len(analysis.selected_regions) == 1
    assert analysis.region_point_count > 0
    assert analysis.bounds_min is not None
    assert analysis.bounds_max is not None
    assert analysis.triangle_count == 0


def test_curvature_guidance_builds_only_a_local_volume_for_a_bend():
    calls: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []

    def provider(bounds_min, bounds_max):
        calls.append((bounds_min, bounds_max))
        return (_floor_triangle_mesh(),)

    points = (
        (0.0, 2.0, 0.0),
        (4.0, 2.0, 0.0),
        (8.0, 2.0, 0.0),
        (8.0, 2.0, 4.0),
        (8.0, 2.0, 8.0),
    )
    profile, volume = build_curvature_guided_voxel_volume(
        points,
        triangle_provider=provider,
        voxel_size_m=1.0,
        max_regions=1,
        max_voxels=1024,
    )

    assert profile.regions
    assert volume is not None
    assert len(calls) == 1
    assert volume.voxel_count <= 1024
    assert volume.bounds_min[0] >= -8.0
    assert volume.bounds_max[0] <= 16.0
