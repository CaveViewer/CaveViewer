"""Tests for bounded curvature-guided local voxel analysis."""

from __future__ import annotations

import numpy as np

from caveviewer.core.navigation.voxel_volume import (
    LocalVoxelVolume,
    VOXEL_ANALYSIS_OUTCOME_NO_CURVATURE_REGION,
    VOXEL_ANALYSIS_OUTCOME_NO_TRIANGLES,
    VoxelVolumeConfig,
    analyze_curvature_guided_voxel_volume,
    build_curvature_guided_voxel_volume,
    build_surface_voxel_volume,
)


def test_fine_local_route_preserves_downward_and_lateral_connectivity():
    origin = (-8.0, -8.0, -8.0)
    shape = (32, 24, 32)
    blocked: set[tuple[int, int, int]] = set()
    for x in range(shape[0]):
        for z in range(shape[2]):
            blocked.update(((x, 1, z), (x, 14, z)))
    for x in range(shape[0]):
        for y in range(shape[1]):
            blocked.update(((x, y, 1), (x, y, 14)))
    for y in range(2, 14):
        for z in range(2, 14):
            if not (3 <= y <= 5 and 10 <= z <= 12):
                blocked.add((12, y, z))

    volume = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=origin,
        shape=shape,
        surface_cells=frozenset(blocked),
        triangle_count=1,
        surface_sample_count=len(blocked),
        sampling_truncated=False,
        max_clearance_search_cells=16,
    )

    route = volume.find_forward_route(
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        max_distance_m=16.0,
        max_nodes=4096,
        min_target_distance_m=4.0,
    )

    assert route is not None
    assert route.forward_progress_m > 0.0
    assert any(point[1] < -1.0 for point in route.points)
    assert any(point[2] > 0.0 for point in route.points)
    assert route.target_connectivity > 0


def test_fine_local_route_reports_a_bounded_partial_search():
    volume = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(-8.0, -8.0, -8.0),
        shape=(32, 24, 32),
        surface_cells=frozenset(),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=16,
    )

    route = volume.find_forward_route(
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        max_distance_m=16.0,
        max_nodes=64,
        min_target_distance_m=4.0,
    )

    assert route is not None
    assert route.search_truncated is True
    assert len(route.points) >= 2


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
