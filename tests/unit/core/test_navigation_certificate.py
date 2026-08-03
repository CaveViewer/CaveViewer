"""Tests for the offline Guided Dive navigation certificate."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import caveviewer.core.navigation.certificate as certificate
from caveviewer.core.navigation.autodive import (
    AUTO_DIVE_PREFLIGHT_READY,
    AutoDivePlan,
    AutoDivePreflightResult,
    AutoDiveRouteSegment,
    AutoDiveSettings,
)
from caveviewer.core.navigation.cubic_graph import CUBIC_VOXEL_GRAPH_METHOD
from caveviewer.core.navigation.fixed_voxels import FIXED_ORTHOGONAL_VOXEL_METHOD
from caveviewer.core.navigation.voxel_cache import (
    NAVIGATION_VOXEL_CACHE_METHOD,
    NAVIGATION_VOXEL_CACHE_VERSION,
    NavigationVoxelAtlas,
    NavigationVoxelCellMetric,
)
from caveviewer.core.navigation.voxel_graph_3d import (
    NAVIGATION_MESH_3D_GRAPH_METHOD,
    NavigationVoxel3DMetric,
    build_navigation_voxel_3d_graph,
)
from caveviewer.core.navigation.voxel_volume import LocalVoxelVolume
from caveviewer.core.navigation.voxel_store import (
    NAVIGATION_VOXEL_CHUNK_STORAGE_METHOD,
)


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
    voxel_graph = build_navigation_voxel_3d_graph(
        metrics,
        grid_size_m=(1.0, 1.0, 1.0),
        unknown_boundary=(),
    )
    mesh_metrics = {
        key: replace(metric, center=(metric.center[0], 0.125, metric.center[2]))
        for key, metric in metrics.items()
    }
    mesh_graph = replace(
        build_navigation_voxel_3d_graph(
            mesh_metrics,
            grid_size_m=(1.0, 0.25, 1.0),
            unknown_boundary=(),
        ),
        method=NAVIGATION_MESH_3D_GRAPH_METHOD,
    )
    mesh_graph = replace(
        mesh_graph,
        nodes={
            key: replace(
                node,
                terminal=key == (2, 0, 0),
                dead_end=key == (2, 0, 0),
                preferred_neighbors=tuple(
                    neighbor
                    for neighbor in ((key[0] - 1, 0, 0), (key[0] + 1, 0, 0))
                    if neighbor in mesh_graph.nodes
                ),
                local_degree=sum(
                    neighbor in mesh_graph.nodes
                    for neighbor in (
                        (key[0] - 1, 0, 0),
                        (key[0] + 1, 0, 0),
                    )
                ),
            )
            for key, node in mesh_graph.nodes.items()
        },
        edges={
            key: tuple(
                edge
                for edge in edges
                if abs(edge.target[0] - key[0]) == 1
            )
            for key, edges in mesh_graph.edges.items()
        },
    )
    tile = LocalVoxelVolume(
        voxel_size_m=1.0,
        vertical_voxel_size_m=0.25,
        origin=(0.0, 0.0, 0.0),
        shape=(3, 4, 1),
        surface_cells=frozenset(),
        triangle_count=0,
        surface_sample_count=1,
        sampling_truncated=False,
        max_clearance_search_cells=2,
    )
    atlas = NavigationVoxelAtlas(
        tiles=(tile,),
        coverage_scope="certified_terminal_route",
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
        prepared_3d_graph=voxel_graph,
        prepared_mesh_graph=mesh_graph,
        fixed_isotropic_voxel_size_m=1.0,
        fixed_vertical_voxel_size_m=0.25,
        surface_overlap_occupied_wins=True,
    )
    assert atlas.prepared_mesh_graph is not None
    return atlas.prepared_mesh_graph, atlas


def _manifest():
    return {
        "chunks": {
            "0_0_0": {
                "bounds_min": [0.0, 0.0, 0.0],
                "bounds_max": [1.0, 0.25, 1.0],
            }
        },
        "navigation": {
            "navigation_start": {
                "position": [0.5, 0.125, 0.5],
                "label": "Cave start",
                "source": "first_manifest_chunk_center_v1",
            },
            "routes": [
                {
                    "id": "main",
                    "selection_method": (
                        "longest_safe_non_circular_certified_route_v1"
                    ),
                    "closed_loop": False,
                    "length_m": 2.0,
                    "points": [
                        0.5,
                        0.125,
                        0.5,
                        1.5,
                        0.125,
                        0.5,
                        2.5,
                        0.125,
                        0.5,
                    ],
                    "footprint_cell_size": 1.0,
                    "cells": [0, 0, 1, 0, 2, 0],
                    "component_cells": [0, 0, 1, 0, 2, 0],
                    "component_vertical_gap_intervals": [
                        0,
                        0,
                        0.0,
                        0.25,
                        1,
                        0,
                        0.0,
                        0.25,
                        2,
                        0,
                        0.0,
                        0.25,
                    ],
                    "starts_at_navigation_start": True,
                    "certified_start_position": [0.5, 0.125, 0.5],
                    "voxel_corridor": {
                        "built": True,
                        "source_route_point_count": 3,
                        "source_route_cell_count": 3,
                        "source_route_cells": [0, 0, 1, 0, 2, 0],
                        "source_route_points": [
                            0.5,
                            0.125,
                            0.5,
                            1.5,
                            0.125,
                            0.5,
                            2.5,
                            0.125,
                            0.5,
                        ],
                        "source_route_start_point": [0.5, 0.125, 0.5],
                        "source_route_terminal_point": [2.5, 0.125, 0.5],
                        "source_route_footprint_cell_size_m": 1.0,
                        "fixed_vertical_voxel_size_m": 0.25,
                        "certified_ingress_hint_index": 0,
                        "certified_terminal_hint_index": 2,
                        "selected_source_hint_start_index": 0,
                        "selected_source_hint_end_index": 2,
                        "complete_ingress_route": True,
                        "route_length_m": 2.0,
                        "prepared_mesh_graph": {
                            "known_terminal_reached": True,
                            "terminal_count": 1,
                            "unknown_boundary_count": 0,
                            "seed_graph_key": [0, 0, 0],
                            "terminal_graph_key": [2, 0, 0],
                            "persisted_path_node_count": 3,
                            "persisted_path_edge_count": 2,
                            "terminal_graph_distance_m": 2.0,
                            "selected_terminal_hint_index": 0,
                            "selected_terminal_hint_point": [
                                2.5,
                                0.125,
                                0.5,
                            ],
                            "terminal_hint_count": 1,
                            "requested_terminal_point": [
                                2.5,
                                0.125,
                                0.5,
                            ],
                            "terminal_snap_limit_m": 24.0,
                            "surface_gap_waypoints_required": True,
                            "surface_gap_gate_source": (
                                "source_layer_pairwise_surface_intervals_v3"
                            ),
                            "surface_gap_route_cell_count": 3,
                            "surface_gap_route_cells": [
                                0,
                                0,
                                1,
                                0,
                                2,
                                0,
                            ],
                            "surface_gap_selected_route_intervals": [
                                0.0,
                                0.25,
                                0.0,
                                0.25,
                                0.0,
                                0.25,
                            ],
                            "surface_gap_transition_fallback_indices": [],
                            "source_ingress_required": True,
                            "source_ingress_connector_required": True,
                            "source_ingress_connector_mesh_clear": True,
                            "source_ingress_attachment_mode": (
                                "executable_authored_start_connector"
                            ),
                            "source_ingress_coordinate_space": "xyz",
                            "source_ingress_point": [0.5, 0.125, 0.5],
                            "source_ingress_attachment_point": [0.5, 0.125, 0.5],
                            "source_ingress_attachment_distance_m": 0.0,
                            "source_ingress_snap_limit_m": 24.0,
                        },
                    },
                }
            ],
            "recommended_route_id": "main",
            "route_selection_method": (
                "longest_safe_non_circular_certified_route_v1"
            ),
            "voxel_cache": {
                "path": "navigation_voxels.json",
                "version": NAVIGATION_VOXEL_CACHE_VERSION,
                "method": NAVIGATION_VOXEL_CACHE_METHOD,
            },
        },
    }


def test_route_contract_accepts_obj_vertex_zero_anchor_with_chunks():
    manifest = _manifest()
    navigation = manifest["navigation"]
    route = navigation["routes"][0]
    navigation.pop("navigation_start")
    navigation["navigation_start_anchor"] = {
        "position": [0.5, 0.125, 0.5],
        "kind": "obj_surface_vertex",
        "source": "map.obj",
        "source_vertex_index": 0,
        "source_order": "obj_declaration_order",
        "executable": False,
        "attachment_required": True,
        "attachment_coordinate_space": "xyz",
    }
    route["starts_at_navigation_start_anchor"] = True
    graph = route["voxel_corridor"]["prepared_mesh_graph"]
    graph["source_ingress_connector_required"] = False
    graph["source_ingress_attachment_mode"] = (
        "non_executable_obj_surface_anchor_snap"
    )

    result = certificate._verify_navigation_route_contract(manifest, "main")

    assert result["passed"] is True
    assert result["start_policy"] == (
        "obj_declaration_order_vertex_zero_anchor"
    )


def test_route_contract_rejects_inferred_start_that_is_not_first_chunk():
    manifest = _manifest()
    navigation = manifest["navigation"]
    route = navigation["routes"][0]
    navigation["navigation_start"]["position"] = [987.0, -16.0, -816.0]
    graph = route["voxel_corridor"]["prepared_mesh_graph"]
    graph["source_ingress_point"] = [987.0, -16.0, -816.0]
    graph["source_ingress_attachment_distance_m"] = (
        ((987.0 - 0.5) ** 2 + (-16.0 - 0.125) ** 2 + (-816.0 - 0.5) ** 2)
        ** 0.5
    )

    result = certificate._verify_navigation_route_contract(manifest, "main")

    assert result["passed"] is False
    assert result["reason"] == "navigation_route_contract_stale"
    assert result["rebuild_reason"] == "navigation_inferred_start_mismatch"


def test_route_contract_rejects_a_safe_but_partial_source_span():
    manifest = _manifest()
    corridor = manifest["navigation"]["routes"][0]["voxel_corridor"]
    corridor["source_route_point_count"] = 30
    corridor["certified_ingress_hint_index"] = 10
    corridor["certified_terminal_hint_index"] = 29
    corridor["selected_source_hint_start_index"] = 10
    corridor["selected_source_hint_end_index"] = 29

    result = certificate._verify_navigation_route_contract(manifest, "main")

    assert result["passed"] is False
    assert result["reason"] == "navigation_source_route_incomplete"
    assert result["selected_source_hint_start_index"] == 10


def test_route_contract_rejects_short_selection_while_long_route_hit_capacity():
    manifest = _manifest()
    navigation = manifest["navigation"]
    navigation["routes"][0]["length_m"] = 20.0
    navigation["routes"].append(
        {
            "id": "long-unresolved",
            "closed_loop": False,
            "length_m": 200.0,
            "starts_at_navigation_start": True,
            "voxel_corridor": {
                "built": False,
                "prepared_mesh_graph": {
                    "reason": "exact_cubic_spine_search_limit_reached",
                    "node_limit_reached": True,
                },
            },
        }
    )

    result = certificate._verify_navigation_route_contract(manifest, "main")

    assert result["passed"] is False
    assert result["reason"] == "navigation_route_selection_unresolved"
    assert result["rebuild_reason"] == (
        "longer_route_search_capacity_limited"
    )


def test_route_contract_accepts_an_exact_authored_start_attachment():
    manifest = _manifest()
    navigation = manifest["navigation"]
    route = navigation["routes"][0]
    navigation["navigation_start"] = {
        "position": [10.0, 0.125, 0.5],
        "source": "map.navigation.json",
    }
    graph = route["voxel_corridor"]["prepared_mesh_graph"]
    graph["source_ingress_point"] = [10.0, 0.125, 0.5]
    graph["source_ingress_attachment_distance_m"] = 9.5

    result = certificate._verify_navigation_route_contract(manifest, "main")

    assert result["passed"] is True
    assert result["start_policy"] == "authored_navigation_start"
    assert result["full_source_span"] is True


def test_route_selection_prefers_certified_manifest_recommendation():
    manifest = _manifest()
    manifest["navigation"]["routes"].insert(
        0,
        {"id": "longer-unsafe", "length_m": 100.0},
    )

    assert certificate._select_route_id(manifest, None) == "main"
    assert certificate._select_route_id(manifest, "main") == "main"
    assert certificate._select_route_id(manifest, "longer-unsafe") is None


def _plan(graph, atlas):
    points = (
        (0.5, 0.125, 0.5),
        (1.5, 0.125, 0.5),
        (2.5, 0.125, 0.5),
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
        fixed_route=True,
    )


def test_graph_geometry_gate_rejects_centers_outside_declared_grid():
    graph, _atlas = _graph_and_atlas()

    malformed = replace(graph, grid_size_m=(2.0, 2.0, 2.0))
    result = certificate._verify_graph_geometry(malformed)

    assert result["passed"] is False
    assert result["coordinate_mismatch_count"] == 2
    assert result["reason"] == "graph_node_center_grid_mismatch"


def test_graph_geometry_v12_gate_rejects_coarse_authoritative_mesh_y():
    graph, _atlas = _graph_and_atlas()

    coarse = replace(graph, grid_size_m=(1.0, 0.5, 1.0))
    result = certificate._verify_graph_geometry(
        coarse,
        max_vertical_grid_size_m=0.25,
    )

    assert result["passed"] is False
    assert result["coordinate_mismatch_count"] == 0
    assert result["vertical_resolution_valid"] is False
    assert result["reason"] == "graph_vertical_resolution_too_coarse"


def test_prepared_route_binding_rejects_terminal_key_from_another_graph():
    graph, _atlas = _graph_and_atlas()
    manifest = _manifest()
    proof = manifest["navigation"]["routes"][0]["voxel_corridor"][
        "prepared_mesh_graph"
    ]
    proof["terminal_graph_key"] = [1, 0, 0]

    result = certificate._verify_prepared_route_binding(
        manifest,
        "main",
        graph,
    )

    assert result["passed"] is False
    assert result["reason"] == "prepared_terminal_key_mismatch"


def test_prepared_route_binding_requires_one_real_known_terminal():
    graph, _atlas = _graph_and_atlas()
    graph = replace(
        graph,
        nodes={
            key: replace(node, terminal=key in {(0, 0, 0), (2, 0, 0)})
            for key, node in graph.nodes.items()
        },
    )

    result = certificate._verify_prepared_route_binding(
        _manifest(),
        "main",
        graph,
    )

    assert result["passed"] is False
    assert result["reason"] == "prepared_graph_terminal_not_unique"


def test_prepared_route_binding_rejects_terminal_outside_final_surface_gap():
    graph, _atlas = _graph_and_atlas()
    manifest = _manifest()
    route = manifest["navigation"]["routes"][0]
    route["component_vertical_gap_intervals"][-2:] = [10.0, 11.0]

    result = certificate._verify_prepared_route_binding(
        manifest,
        "main",
        graph,
    )

    assert result["passed"] is False
    assert result["terminal_interval_bound"] is False
    assert result["reason"] == "prepared_terminal_outside_surface_gap"


@pytest.mark.parametrize("invalid_count", [3.0, "3", True])
def test_prepared_route_binding_rejects_coerced_source_counts(invalid_count):
    graph, _atlas = _graph_and_atlas()
    manifest = _manifest()
    corridor = manifest["navigation"]["routes"][0]["voxel_corridor"]
    corridor["source_route_point_count"] = invalid_count

    result = certificate._verify_prepared_route_binding(
        manifest,
        "main",
        graph,
    )

    assert result["passed"] is False
    assert result["reason"] == "prepared_route_binding_schema_invalid"


def test_prepared_route_binding_rejects_malformed_late_source_coordinate():
    graph, _atlas = _graph_and_atlas()
    manifest = _manifest()
    corridor = manifest["navigation"]["routes"][0]["voxel_corridor"]
    corridor["source_route_points"][-1] = "0.5"

    result = certificate._verify_prepared_route_binding(
        manifest,
        "main",
        graph,
    )

    assert result["passed"] is False
    assert result["reason"] == "prepared_route_binding_schema_invalid"


def test_prepared_route_binding_rejects_a_different_graph_start():
    graph, _atlas = _graph_and_atlas()
    manifest = _manifest()
    proof = manifest["navigation"]["routes"][0]["voxel_corridor"][
        "prepared_mesh_graph"
    ]
    proof["seed_graph_key"] = [1, 0, 0]

    result = certificate._verify_prepared_route_binding(
        manifest,
        "main",
        graph,
    )

    assert result["passed"] is False
    assert result["reason"] == "prepared_graph_start_binding_invalid"


def test_prepared_route_binding_rejects_published_path_that_skips_a_gate():
    graph, _atlas = _graph_and_atlas()
    manifest = _manifest()
    route = manifest["navigation"]["routes"][0]
    route["points"] = route["points"][:3] + route["points"][-3:]
    route["cells"] = route["cells"][:2] + route["cells"][-2:]

    result = certificate._verify_prepared_route_binding(
        manifest,
        "main",
        graph,
    )

    assert result["passed"] is False
    assert result["reason"] == "prepared_published_graph_path_mismatch"


def test_prepared_route_binding_replays_every_selected_surface_gap():
    graph, _atlas = _graph_and_atlas()
    manifest = _manifest()
    route = manifest["navigation"]["routes"][0]
    proof = route["voxel_corridor"]["prepared_mesh_graph"]
    route["component_vertical_gap_intervals"][6:8] = [10.0, 11.0]
    proof["surface_gap_selected_route_intervals"][2:4] = [10.0, 11.0]

    result = certificate._verify_prepared_route_binding(
        manifest,
        "main",
        graph,
    )

    assert result["passed"] is False
    assert result["gate_replay_valid"] is False
    assert result["reason"] == "prepared_route_gate_replay_failed"


def test_prepared_route_binding_allows_only_persisted_pairwise_fallback_hull():
    graph, _atlas = _graph_and_atlas()
    manifest = _manifest()
    route = manifest["navigation"]["routes"][0]
    proof = route["voxel_corridor"]["prepared_mesh_graph"]
    route["component_vertical_gap_intervals"][6:8] = [1.0, 1.25]
    proof["surface_gap_selected_route_intervals"][2:4] = [1.0, 1.25]
    proof["surface_gap_transition_fallback_indices"] = [1]

    result = certificate._verify_prepared_route_binding(
        manifest,
        "main",
        graph,
    )

    assert result["passed"] is True
    assert result["gate_replay_valid"] is True


def test_prepared_route_binding_compares_actual_graph_boundary_counts():
    graph, _atlas = _graph_and_atlas()
    graph = replace(
        graph,
        nodes={
            key: replace(node, unknown_boundary=key == (0, 0, 0))
            for key, node in graph.nodes.items()
        },
    )

    result = certificate._verify_prepared_route_binding(
        _manifest(),
        "main",
        graph,
    )

    assert result["passed"] is False
    assert result["reason"] == "prepared_graph_terminal_counts_invalid"


def test_artifact_index_requires_the_v12_fixed_voxel_contract(tmp_path):
    assert NAVIGATION_VOXEL_CACHE_VERSION == 12
    assert NAVIGATION_VOXEL_CACHE_METHOD == "fixed_orthogonal_route_atlas_v12"
    assert FIXED_ORTHOGONAL_VOXEL_METHOD == "fixed_orthogonal_voxel_chunks_v2"
    (tmp_path / "navigation_voxels.json").write_text("{}", encoding="utf-8")
    chunk_dir = tmp_path / "navigation_voxel_chunks"
    chunk_dir.mkdir()
    (chunk_dir / "chunk.json").write_text("{}", encoding="utf-8")
    descriptor = {
        "path": "navigation_voxels.json",
        "version": NAVIGATION_VOXEL_CACHE_VERSION,
        "method": NAVIGATION_VOXEL_CACHE_METHOD,
        "storage_method": NAVIGATION_VOXEL_CHUNK_STORAGE_METHOD,
        "chunk_directory": "navigation_voxel_chunks",
        "chunk_count": 1,
        "fixed_voxel_method": FIXED_ORTHOGONAL_VOXEL_METHOD,
        "fixed_isotropic_voxel_size_m": 1.0,
        "fixed_vertical_voxel_size_m": 0.25,
        "fixed_voxel_cell_size_m": [1.0, 0.25, 1.0],
        "cubic_graph_method": CUBIC_VOXEL_GRAPH_METHOD,
        "surface_overlap_policy": "occupied_wins",
        "sampling_complete_required": True,
    }
    manifest = {"navigation": {"voxel_cache": descriptor}}

    valid = certificate._verify_navigation_artifact_index(
        str(tmp_path),
        manifest,
        "main",
    )
    descriptor["fixed_vertical_voxel_size_m"] = 0.5
    coarse_vertical = certificate._verify_navigation_artifact_index(
        str(tmp_path),
        manifest,
        "main",
    )
    descriptor["fixed_vertical_voxel_size_m"] = 0.125
    mismatched_vertical = certificate._verify_navigation_artifact_index(
        str(tmp_path),
        manifest,
        "main",
    )
    descriptor["fixed_vertical_voxel_size_m"] = 0.25
    descriptor["version"] = 10
    stale = certificate._verify_navigation_artifact_index(
        str(tmp_path),
        manifest,
        "main",
    )

    assert valid["passed"] is True
    assert valid["fixed_isotropic_voxel_size_m"] == 1.0
    assert valid["fixed_vertical_voxel_size_m"] == 0.25
    assert coarse_vertical["passed"] is False
    assert (
        coarse_vertical["reason"]
        == "navigation_fixed_vertical_voxel_size_invalid"
    )
    assert mismatched_vertical["passed"] is False
    assert (
        mismatched_vertical["reason"]
        == "navigation_fixed_voxel_cell_size_invalid"
    )
    assert stale["passed"] is False
    assert stale["reason"] == "navigation_cache_rebuild_required"


def test_certificate_passes_only_after_artifact_route_and_replan_gates(
    monkeypatch,
    tmp_path,
):
    graph, atlas = _graph_and_atlas()
    plan = _plan(graph, atlas)
    plan = replace(
        plan,
        route_segments=(
            AutoDiveRouteSegment(
                route_points=plan.route_points,
                route_cells=plan.route_cells,
                source="prepared_global_graph",
                graph_keys=plan.navigation_graph_keys,
                details={"kind": "prepared_global_route"},
            ),
        ),
    )
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
        certificate,
        "_verify_navigation_artifact_index",
        lambda *_args, **_kwargs: {"passed": True},
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
        lambda *_args, **_kwargs: pytest.fail(
            "fixed-route certification must not invoke runtime replanning"
        ),
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
        "navigation_artifact_index",
        "navigation_route_contract",
        "navigation_artifact",
        "navigation_chunk_decoding",
        "graph_geometry",
        "graph_route_binding",
        "graph_coverage",
        "mesh_collision_artifact",
        "route_preflight",
        "route_safety",
        "runtime_replanning",
    }
    assert all(check.passed for check in result.checks)
    route_safety = next(check for check in result.checks if check.name == "route_safety")
    assert route_safety.details["route_geometry_source"] == "fixed_route_ledger"
    simulation = next(
        check for check in result.checks if check.name == "runtime_replanning"
    )
    assert simulation.details["execution_mode"] == "fixed_route_no_replan"
    assert simulation.details["replan_request_count"] == 0


def test_fixed_route_execution_honors_the_exact_mesh_only_start_connector():
    graph, atlas = _graph_and_atlas()
    occupied_start_tile = replace(
        atlas.tiles[0],
        surface_cells=frozenset({(0, 2, 0)}),
    )
    atlas = replace(atlas, tiles=(occupied_start_tile,))
    plan = replace(
        _plan(graph, atlas),
        route_points=(
            (0.25, 0.5, 0.5),
            (1.5, 0.125, 0.5),
            (2.5, 0.125, 0.5),
        ),
        route_cells=((0, 0), (1, 0), (2, 0)),
        route_length_m=2.25,
        navigation_graph_keys=((1, 0, 0), (2, 0, 0)),
        mesh_only_start_connector=True,
    )

    passed, details = certificate._simulate_fixed_route_execution(
        plan=plan,
        atlas=atlas,
        graph=graph,
        mesh_guard=_NoCollisionGuard(),
        settings=AutoDiveSettings(minimum_graph_clearance_m=0.0),
        checkpoint_spacing_m=1.0,
        max_checkpoints=16,
    )

    assert passed is True
    assert details["failure_count"] == 0
    assert details["simulated_checkpoint_count"] == 3


def test_fixed_route_execution_rejects_a_mesh_hit_on_the_start_connector():
    graph, atlas = _graph_and_atlas()
    plan = replace(
        _plan(graph, atlas),
        route_points=(
            (0.25, 0.5, 0.5),
            (1.5, 0.125, 0.5),
            (2.5, 0.125, 0.5),
        ),
        route_length_m=2.25,
        navigation_graph_keys=((1, 0, 0), (2, 0, 0)),
        mesh_only_start_connector=True,
    )

    class BlockedConnectorGuard:
        def segment_collision(self, first, _second):
            if tuple(first) == (0.25, 0.5, 0.5):
                return SimpleNamespace(point=(0.5, 0.5, 0.5))
            return None

    passed, details = certificate._simulate_fixed_route_execution(
        plan=plan,
        atlas=atlas,
        graph=graph,
        mesh_guard=BlockedConnectorGuard(),
        settings=AutoDiveSettings(minimum_graph_clearance_m=0.0),
        checkpoint_spacing_m=1.0,
        max_checkpoints=16,
    )

    assert passed is False
    assert details["reason"] == "mesh_intersection"


def test_full_cave_profile_rejects_unknown_frontier(monkeypatch, tmp_path):
    graph, atlas = _graph_and_atlas()
    graph = replace(
        graph,
        nodes={
            key: replace(node, unknown_boundary=(key == (2, 0, 0)))
            for key, node in graph.nodes.items()
        },
    )
    atlas = replace(atlas, prepared_mesh_graph=graph)
    monkeypatch.setattr(certificate, "cache_dir_is_valid", lambda *_args: True)
    monkeypatch.setattr(certificate, "load_chunk_file", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        certificate,
        "_verify_navigation_artifact_index",
        lambda *_args, **_kwargs: {"passed": True},
    )
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


def test_artifact_phase_does_not_deserialize_navigation_graph(monkeypatch, tmp_path):
    monkeypatch.setattr(certificate, "cache_dir_is_valid", lambda *_args: True)
    monkeypatch.setattr(
        certificate,
        "load_chunk_file",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        certificate,
        "_verify_navigation_artifact_index",
        lambda *_args, **_kwargs: {"passed": True},
    )

    def fail_if_graph_loads(*_args, **_kwargs):
        pytest.fail("artifact certification must not load the navigation graph")

    monkeypatch.setattr(
        certificate,
        "load_cached_navigation_voxel_volume",
        fail_if_graph_loads,
    )

    result = certificate.certify_navigation_cache(
        _manifest(),
        cache_dir=tmp_path,
        phase=certificate.PHASE_ARTIFACTS,
    )

    assert result.passed is True
    assert result.phase == certificate.PHASE_ARTIFACTS
    assert "navigation_artifact" not in {
        check.name for check in result.checks
    }


def test_graph_phase_stops_before_route_preflight(monkeypatch, tmp_path):
    graph, atlas = _graph_and_atlas()
    monkeypatch.setattr(certificate, "cache_dir_is_valid", lambda *_args: True)
    monkeypatch.setattr(certificate, "load_chunk_file", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        certificate,
        "_verify_navigation_artifact_index",
        lambda *_args, **_kwargs: {"passed": True},
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

    def fail_if_route_runs(*_args, **_kwargs):
        pytest.fail("graph certification must stop before route preflight")

    monkeypatch.setattr(
        certificate,
        "build_auto_dive_preflight_plan",
        fail_if_route_runs,
    )

    result = certificate.certify_navigation_cache(
        _manifest(),
        cache_dir=tmp_path,
        phase=certificate.PHASE_GRAPH,
    )

    assert result.passed is True
    assert result.phase == certificate.PHASE_GRAPH
    assert "route_preflight" not in {
        check.name for check in result.checks
    }


@pytest.mark.parametrize("value", [(0.0, 1.0), (float("nan"), 1.0)])
def test_route_checkpoints_reject_invalid_limits(value):
    with pytest.raises(ValueError):
        certificate._route_checkpoints(
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
            spacing_m=value[0],
            max_checkpoints=8,
        )
