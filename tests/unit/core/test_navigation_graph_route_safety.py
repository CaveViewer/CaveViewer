"""Tests for exact graph-route, voxel, and cached-mesh safety checks."""

from types import SimpleNamespace

from caveviewer.core.navigation.graph_route_safety import (
    GraphRouteSafetyPolicy,
    GraphRouteSafetyValidator,
)
from caveviewer.core.navigation.voxel_cache import NavigationVoxelAtlas
from caveviewer.core.navigation.voxel_graph_3d import (
    NavigationVoxel3DMetric,
    build_navigation_voxel_3d_graph,
)
from caveviewer.core.navigation.voxel_volume import LocalVoxelVolume


def _start_connector_validator(*, blocked_mesh: bool):
    volume = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(0.0, 0.0, 0.0),
        shape=(4, 1, 1),
        surface_cells=frozenset({(0, 0, 0)}),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=4,
    )
    metrics = {
        (1, 0, 0): NavigationVoxel3DMetric(
            center=(1.5, 0.5, 0.5),
            footprint_cell=(1, 0),
            available_volume_m3=1.0,
            free_voxel_count=1,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=0.0,
        ),
        (2, 0, 0): NavigationVoxel3DMetric(
            center=(2.5, 0.5, 0.5),
            footprint_cell=(2, 0),
            available_volume_m3=1.0,
            free_voxel_count=1,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=1.0,
        ),
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        max_edge_distance_cells=1,
    )

    class MeshGuard:
        def segment_collision(self, first, second):
            if blocked_mesh and tuple(first) == (0.25, 0.5, 0.5):
                return SimpleNamespace(point=(0.75, 0.5, 0.5))
            return None

    return GraphRouteSafetyValidator(
        NavigationVoxelAtlas(tiles=(volume,), prepared_3d_graph=graph),
        graph,
        mesh_guard=MeshGuard(),
        policy=GraphRouteSafetyPolicy(minimum_clearance_m=0.0),
    )


def test_mesh_only_start_connector_skips_only_the_quantized_camera_voxel():
    validator = _start_connector_validator(blocked_mesh=False)
    points = (
        (0.25, 0.5, 0.5),
        (1.5, 0.5, 0.5),
        (2.5, 0.5, 0.5),
    )
    keys = ((1, 0, 0), (2, 0, 0))

    ordinary_failure = validator.route_clearance_failure(points, keys)
    certified_failure = validator.route_clearance_failure(
        points,
        keys,
        allow_mesh_only_start_connector=True,
    )

    assert ordinary_failure is not None
    assert ordinary_failure.reason == "graph_point_blocked"
    assert certified_failure is None


def test_mesh_only_start_connector_still_rejects_an_exact_mesh_hit():
    validator = _start_connector_validator(blocked_mesh=True)
    failure = validator.route_clearance_failure(
        (
            (0.25, 0.5, 0.5),
            (1.5, 0.5, 0.5),
            (2.5, 0.5, 0.5),
        ),
        ((1, 0, 0), (2, 0, 0)),
        allow_mesh_only_start_connector=True,
    )

    assert failure is not None
    assert failure.kind == "camera_connector"
    assert failure.reason == "mesh_intersection"


def test_segment_validation_cannot_skip_a_narrow_diagonal_voxel_crossing():
    volume = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(0.0, 0.0, 0.0),
        shape=(12, 1, 11),
        surface_cells=frozenset({(1, 0, 0)}),
        triangle_count=1,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=12,
    )
    metrics = {
        (0, 0, 0): NavigationVoxel3DMetric(
            center=(0.5, 0.5, 0.5),
            footprint_cell=(0, 0),
            available_volume_m3=1.0,
            free_voxel_count=1,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=0.0,
        ),
        (10, 0, 9): NavigationVoxel3DMetric(
            center=(10.5, 0.5, 9.5),
            footprint_cell=(10, 9),
            available_volume_m3=1.0,
            free_voxel_count=1,
            min_clearance_m=1.0,
            mean_clearance_m=1.0,
            progress_m=1.0,
        ),
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        max_edge_distance_cells=1,
    )
    validator = GraphRouteSafetyValidator(
        NavigationVoxelAtlas(
            tiles=(volume,),
            prepared_3d_graph=graph,
            fixed_isotropic_voxel_size_m=1.0,
            fixed_vertical_voxel_size_m=1.0,
        ),
        graph,
        mesh_guard=SimpleNamespace(
            segment_collision=lambda _first, _second: None
        ),
        policy=GraphRouteSafetyPolicy(sample_spacing_m=0.125),
    )

    failure = validator.segment_clearance_failure(
        (0.5, 0.5, 0.5),
        (10.5, 0.5, 9.5),
    )

    assert failure is not None
    assert failure.reason == "graph_point_blocked"
    assert failure.point is not None
    assert volume.voxel_index(failure.point) == (1, 0, 0)

    checkpoint = tuple(
        first + (second - first) / 14.0
        for first, second in zip(
            (0.5, 0.5, 0.5),
            (10.5, 0.5, 9.5),
            strict=False,
        )
    )
    split_failure = validator.segment_clearance_failure(
        (0.5, 0.5, 0.5),
        checkpoint,
    )
    assert split_failure is not None
    assert split_failure.reason == failure.reason
    assert split_failure.point is not None
    assert volume.voxel_index(split_failure.point) == (1, 0, 0)

    reverse_failure = validator.segment_clearance_failure(
        (10.5, 0.5, 9.5),
        (0.5, 0.5, 0.5),
    )
    assert reverse_failure is not None
    assert reverse_failure.reason == failure.reason
    assert reverse_failure.point is not None
    assert volume.voxel_index(reverse_failure.point) == (1, 0, 0)

    assert validator.segment_clearance_failure(
        (2.5, 0.5, 0.5),
        (10.5, 0.5, 9.5),
    ) is None
