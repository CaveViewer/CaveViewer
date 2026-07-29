"""Tests for the offline Guided Dive navigation certificate."""

from __future__ import annotations

from dataclasses import replace

import pytest

import caveviewer.core.navigation.certificate as certificate
from caveviewer.core.navigation.autodive import (
    AUTO_DIVE_PREFLIGHT_READY,
    AutoDivePlan,
    AutoDivePreflightResult,
)
from caveviewer.core.navigation.voxel_cache import (
    NavigationVoxelAtlas,
    NavigationVoxelCellMetric,
)
from caveviewer.core.navigation.voxel_graph_3d import (
    NavigationVoxel3DMetric,
    build_navigation_voxel_3d_graph,
)
from caveviewer.core.navigation.voxel_volume import LocalVoxelVolume


class _NoCollisionGuard:
    def segment_collision(self, _first, _second):
        return None


def _graph_and_atlas():
    metrics = {
        (index, 0, 0): NavigationVoxel3DMetric(
            center=(float(index) + 0.5, 0.5, 0.5),
            footprint_cell=(index, 0),
            available_volume_m3=4.0,
            free_voxel_count=4,
            min_clearance_m=2.0,
            mean_clearance_m=2.0,
            progress_m=float(index),
        )
        for index in range(3)
    }
    graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        unknown_boundary=(),
    )
    tile = LocalVoxelVolume(
        voxel_size_m=1.0,
        origin=(0.0, 0.0, 0.0),
        shape=(3, 2, 1),
        surface_cells=frozenset(),
        triangle_count=0,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=2,
    )
    atlas = NavigationVoxelAtlas(
        tiles=(tile,),
        coverage_scope="entire_cave_component",
        cell_metrics={
            (index, 0): NavigationVoxelCellMetric(
                available_volume_m3=4.0,
                free_cell_count=4,
                min_clearance_m=2.0,
                mean_clearance_m=2.0,
                progress_m=float(index),
            )
            for index in range(3)
        },
        prepared_3d_graph=graph,
    )
    return graph, atlas


def _manifest():
    return {
        "chunks": {"0_0_0": {}},
        "navigation": {
            "routes": [
                {
                    "id": "main",
                    "length_m": 2.0,
                    "component_cells": [0, 0, 1, 0, 2, 0],
                }
            ],
            "recommended_route_id": "main",
            "voxel_cache": {
                "path": "navigation_voxels.json",
                "version": 8,
                "method": "whole_cave_voxel_atlas_v8",
            },
        },
    }


def _plan(graph, atlas):
    points = (
        (0.5, 0.5, 0.5),
        (1.5, 0.5, 0.5),
        (2.5, 0.5, 0.5),
    )
    keys = ((0, 0, 0), (1, 0, 0), (2, 0, 0))
    return AutoDivePlan(
        route=None,  # type: ignore[arg-type]
        centerline_path=None,
        route_points=points,
        route_cells=((0, 0), (1, 0), (2, 0)),
        circular_arc=False,
        route_length_m=2.0,
        duration_s=1.0,
        render_distance_cells=1,
        navigation_route_id="main",
        preflight_validated=True,
        navigation_atlas=atlas,
        navigation_graph=graph,
        navigation_graph_keys=keys,
        terminal_reached=True,
    )


def test_graph_geometry_gate_rejects_centers_outside_declared_grid():
    graph, _atlas = _graph_and_atlas()

    malformed = replace(graph, grid_size_m=(2.0, 2.0, 2.0))
    result = certificate._verify_graph_geometry(malformed)

    assert result["passed"] is False
    assert result["coordinate_mismatch_count"] == 2
    assert result["reason"] == "graph_node_center_grid_mismatch"


def test_certificate_passes_only_after_artifact_route_and_replan_gates(
    monkeypatch,
    tmp_path,
):
    graph, atlas = _graph_and_atlas()
    plan = _plan(graph, atlas)
    preflight = AutoDivePreflightResult(
        status=AUTO_DIVE_PREFLIGHT_READY,
        reason="validated_farthest_graph_terminal_route",
        plan=plan,
        navigation_route_id="main",
        terminal_graph_key=(2, 0, 0),
        start_graph_key=(0, 0, 0),
        route_point_count=len(plan.route_points),
        coverage_incomplete=False,
    )

    monkeypatch.setattr(certificate, "cache_dir_is_valid", lambda *_args: True)
    monkeypatch.setattr(
        certificate,
        "load_chunk_file",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        certificate,
        "load_cached_navigation_voxel_volume",
        lambda *_args, **_kwargs: atlas,
    )
    monkeypatch.setattr(
        certificate.CachedChunkMeshCollisionGuard,
        "from_manifest",
        classmethod(lambda cls, *_args, **_kwargs: _NoCollisionGuard()),
    )
    monkeypatch.setattr(
        certificate,
        "build_auto_dive_preflight_plan",
        lambda *_args, **_kwargs: preflight,
    )
    monkeypatch.setattr(
        certificate,
        "build_voxel_graph_auto_dive_plan",
        lambda *_args, **_kwargs: plan,
    )

    result = certificate.certify_navigation_cache(
        _manifest(),
        cache_dir=tmp_path,
        start_position=plan.route_points[0],
        checkpoint_spacing_m=4.0,
    )

    assert result.passed is True
    assert {check.name for check in result.checks} == {
        "input",
        "cache_artifacts",
        "render_chunk_decoding",
        "navigation_route",
        "navigation_artifact",
        "navigation_chunk_decoding",
        "graph_geometry",
        "graph_coverage",
        "mesh_collision_artifact",
        "route_preflight",
        "route_safety",
        "runtime_replanning",
    }
    assert all(check.passed for check in result.checks)


def test_full_cave_profile_rejects_unknown_frontier(monkeypatch, tmp_path):
    graph, atlas = _graph_and_atlas()
    graph = replace(
        graph,
        nodes={
            key: replace(node, unknown_boundary=(key == (2, 0, 0)))
            for key, node in graph.nodes.items()
        },
    )
    atlas = replace(atlas, prepared_3d_graph=graph)
    monkeypatch.setattr(certificate, "cache_dir_is_valid", lambda *_args: True)
    monkeypatch.setattr(certificate, "load_chunk_file", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        certificate,
        "load_cached_navigation_voxel_volume",
        lambda *_args, **_kwargs: atlas,
    )

    result = certificate.certify_navigation_cache(
        _manifest(),
        cache_dir=tmp_path,
        start_position=(0.5, 0.5, 0.5),
    )

    coverage = next(check for check in result.checks if check.name == "graph_coverage")
    assert coverage.passed is False
    assert coverage.reason == "unknown_graph_boundary"


@pytest.mark.parametrize("value", [(0.0, 1.0), (float("nan"), 1.0)])
def test_route_checkpoints_reject_invalid_limits(value):
    with pytest.raises(ValueError):
        certificate._route_checkpoints(
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
            spacing_m=value[0],
            max_checkpoints=8,
        )
