"""Cover chunk-size recommendation heuristics."""

from __future__ import annotations

import numpy as np
import pytest

from caveviewer.core.map import chunk_size_advisor
from caveviewer.core.map.chunk_size_advisor import (
    recommend_chunk_size_for_glb,
    recommend_chunk_size_from_faces,
)
from caveviewer.core.mesh.obj import RawMesh


def test_curved_geometry_prefers_no_larger_chunks_than_straight_geometry():
    straight = np.asarray(
        [[float(index), 0.0, 0.0] for index in range(120)],
        dtype=np.float32,
    )
    zigzag = np.asarray(
        [[float(index % 30), float(index // 30) * 30.0, 0.0] for index in range(120)],
        dtype=np.float32,
    )

    straight_result = recommend_chunk_size_from_faces(
        straight,
        candidate_sizes=(10, 25, 50),
    )
    zigzag_result = recommend_chunk_size_from_faces(
        zigzag,
        candidate_sizes=(10, 25, 50),
    )

    assert straight_result.recommended_size >= zigzag_result.recommended_size
    assert zigzag_result.recommended_size == 10.0


def test_recommendation_dict_includes_candidate_diagnostics():
    centroids = np.asarray(
        [[float(index), 0.0, 0.0] for index in range(30)],
        dtype=np.float32,
    )

    result = recommend_chunk_size_from_faces(
        centroids,
        material_names=["rock"] * len(centroids),
        candidate_sizes=(10, 30),
    )

    payload = result.as_dict()
    assert payload["advisor_version"] == 1
    assert payload["recommended_size"] in {10.0, 30.0}
    assert len(payload["candidates"]) == 2
    first_candidate = payload["candidates"][0]
    assert first_candidate["chunk_size"] == 10.0
    assert first_candidate["chunk_count"] == 3
    assert first_candidate["p95_chunk_bytes_estimate"] > 0
    assert first_candidate["warnings"] == []


def test_recommendation_reports_progress_for_precomputed_faces():
    centroids = np.asarray(
        [[float(index), 0.0, 0.0] for index in range(30)],
        dtype=np.float32,
    )
    progress = []

    recommend_chunk_size_from_faces(
        centroids,
        candidate_sizes=(10, 30),
        progress_cb=lambda *item: progress.append(item),
    )

    assert progress[0] == ("scoring candidates", 0.0)
    assert progress[-1] == ("done", 1.0)


def test_parallel_workers_match_sequential_recommendation():
    centroids = np.asarray(
        [[float(index % 40), float(index // 40), float(index % 7)] for index in range(400)],
        dtype=np.float32,
    )
    material_names = ["rock" if index % 2 else "silt" for index in range(len(centroids))]

    sequential = recommend_chunk_size_from_faces(
        centroids,
        material_names=material_names,
        candidate_sizes=(10, 25, 50),
        worker_count=1,
    )
    parallel = recommend_chunk_size_from_faces(
        centroids,
        material_names=material_names,
        candidate_sizes=(10, 25, 50),
        worker_count=2,
    )

    assert parallel.as_dict() == sequential.as_dict()


def test_recommendation_requires_faces():
    with pytest.raises(ValueError, match="without faces"):
        recommend_chunk_size_from_faces(
            np.empty((0, 3), dtype=np.float32),
            candidate_sizes=(10, 30),
        )


def test_glb_recommendation_preflights_source_and_expanded_arrays(monkeypatch):
    import caveviewer.core.mesh.glb as glb_parser

    calls = []

    def source_preflight(path: str) -> None:
        calls.append(("source", path))

    def import_preflight(
        vertex_count: int,
        uv_count: int,
        normal_count: int,
        face_count: int,
        *,
        source_path: str,
    ) -> None:
        calls.append(
            (
                "expanded",
                vertex_count,
                uv_count,
                normal_count,
                face_count,
                source_path,
            )
        )

    def parse_glb(path: str, *, progress_cb=None, preflight_cb=None):
        calls.append(("parse", path))
        if preflight_cb is not None:
            preflight_cb(3, 3, 3, 1)
        positions = np.asarray(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            dtype=np.float32,
        )
        face_indices = np.asarray([[0, 1, 2]], dtype=np.int32)
        return (
            RawMesh(
                positions=positions,
                uvs=np.zeros((3, 2), dtype=np.float32),
                normals=np.zeros((3, 3), dtype=np.float32),
                face_pos_idx=face_indices,
                face_uv_idx=face_indices.copy(),
                face_nrm_idx=face_indices.copy(),
            ),
            {},
        )

    monkeypatch.setattr(
        chunk_size_advisor,
        "ensure_sufficient_source_file_read_memory",
        source_preflight,
    )
    monkeypatch.setattr(
        chunk_size_advisor,
        "ensure_sufficient_import_memory",
        import_preflight,
    )
    monkeypatch.setattr(glb_parser, "parse_glb", parse_glb)

    result = recommend_chunk_size_for_glb(
        "/maps/cave.glb",
        candidate_sizes=(10, 30),
    )

    assert result.recommended_size in {10.0, 30.0}
    assert calls[:3] == [
        ("source", "/maps/cave.glb"),
        ("parse", "/maps/cave.glb"),
        ("expanded", 3, 3, 3, 1, "/maps/cave.glb"),
    ]
