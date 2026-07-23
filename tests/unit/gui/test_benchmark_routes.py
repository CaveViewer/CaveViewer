"""Tests for manifest-derived benchmark route generation."""

from __future__ import annotations

from caveviewer.gui.benchmark import BenchmarkScenario
from caveviewer.gui.benchmark_routes import (
    generate_centerline_route_scenario,
    generate_dense_chunk_route_scenario,
)


def test_centerline_route_uses_vertex_footprint_middle_passage():
    template = BenchmarkScenario.from_mapping(
        {
            "version": 1,
            "name": "template",
            "warmup_seconds": 2.0,
            "measurement_seconds": 8.0,
            "render_distance": 1,
            "route": [{"time_s": 0.0, "position": [0.0, 0.0, 0.0]}],
        }
    )

    centerline_route = generate_centerline_route_scenario(
        _centerline_manifest(),
        template,
        keyframe_count=4,
    )
    scenario = BenchmarkScenario.from_mapping(centerline_route.scenario_payload)
    metadata = centerline_route.scenario_payload["metadata"]

    assert scenario.position_mode == "absolute"
    assert scenario.total_duration_seconds == 10.0
    assert metadata["route_mode"] == "auto_centerline_v1"
    assert metadata["route_source"] == "vertex_footprint_manifest"
    assert metadata["y_strategy"] == "local_vertical_center_v1"
    assert metadata["y_bias"] == 0.65
    assert metadata["footprint_cell_size_m"] == 2.0
    assert metadata["max_clearance_cells"] >= 3
    assert metadata["route_length_m"] > 0.0
    assert metadata["route_keyframe_count"] >= 2
    assert all(cell[1] in {1, 2, 3} for cell in centerline_route.route_cells)
    assert centerline_route.scenario_payload["route"][0]["time_s"] == 0.0
    assert centerline_route.scenario_payload["route"][-1]["time_s"] == 10.0
    assert all(
        keyframe["position"][1] == 6.5
        for keyframe in centerline_route.scenario_payload["route"]
    )


def test_centerline_route_uses_vertical_center_in_tall_columns():
    template = BenchmarkScenario.from_mapping(
        {
            "version": 1,
            "name": "template",
            "warmup_seconds": 2.0,
            "measurement_seconds": 8.0,
            "render_distance": 1,
            "route": [{"time_s": 0.0, "position": [0.0, 0.0, 0.0]}],
        }
    )

    centerline_route = generate_centerline_route_scenario(
        _centerline_manifest(
            y_ranges=((0.0, 10.0), (100.0, 120.0)),
        ),
        template,
        keyframe_count=4,
    )

    route_y_values = [
        keyframe["position"][1]
        for keyframe in centerline_route.scenario_payload["route"]
    ]
    assert all(value == 78.0 for value in route_y_values)
    assert centerline_route.scenario_payload["metadata"]["min_route_y"] == 78.0
    assert centerline_route.scenario_payload["metadata"]["max_route_y"] == 78.0


def test_dense_chunk_route_uses_connected_high_density_manifest_region():
    template = BenchmarkScenario.from_mapping(
        {
            "version": 1,
            "name": "template",
            "warmup_seconds": 2.0,
            "measurement_seconds": 8.0,
            "render_distance": 1,
            "route": [{"time_s": 0.0, "position": [0.0, 0.0, 0.0]}],
        }
    )

    dense_route = generate_dense_chunk_route_scenario(
        _manifest(),
        template,
        dense_percentile=60.0,
        keyframe_count=5,
    )
    scenario = BenchmarkScenario.from_mapping(dense_route.scenario_payload)
    metadata = dense_route.scenario_payload["metadata"]

    assert scenario.position_mode == "absolute"
    assert scenario.total_duration_seconds == 10.0
    assert metadata["route_mode"] == "auto_dense_chunks_v1"
    assert metadata["route_source"] == "chunk_manifest"
    assert metadata["max_neighborhood_chunks"] >= 10
    assert metadata["route_length_m"] > 0.0
    assert metadata["route_keyframe_count"] >= 2
    assert dense_route.route_cells[0] in dense_route.path_cells
    assert dense_route.route_cells[-1] in dense_route.path_cells
    assert dense_route.scenario_payload["route"][0]["time_s"] == 0.0
    assert dense_route.scenario_payload["route"][-1]["time_s"] == 10.0


def _manifest() -> dict:
    chunks = {}
    for x in range(4):
        for y in range(2):
            for z in range(4):
                chunks[f"{x}_{y}_{z}"] = _chunk(x, y, z)
    for x in range(20, 23):
        chunks[f"{x}_0_0"] = _chunk(x, 0, 0)
    return {"chunks": chunks}


def _chunk(x: int, y: int, z: int) -> dict:
    return {
        "bounds_min": [x * 10.0, y * 10.0, z * 10.0],
        "bounds_max": [x * 10.0 + 10.0, y * 10.0 + 10.0, z * 10.0 + 10.0],
        "materials": ["rock"],
    }


def _centerline_manifest(
    *,
    y_ranges: tuple[tuple[float, float], ...] = ((0.0, 10.0),),
) -> dict:
    chunks = {}
    footprint_cells = []
    for x in range(7):
        for z in range(5):
            for y, (min_y, max_y) in enumerate(y_ranges):
                chunks[f"{x}_{y}_{z}"] = {
                    "bounds_min": [x * 2.0, min_y, z * 2.0],
                    "bounds_max": [x * 2.0 + 2.0, max_y, z * 2.0 + 2.0],
                    "materials": ["rock"],
                }
            footprint_cells.extend((x, z))
    return {
        "chunk_size": 2.0,
        "footprint_cell_size": 2.0,
        "footprint_cells": footprint_cells,
        "chunks": chunks,
    }
