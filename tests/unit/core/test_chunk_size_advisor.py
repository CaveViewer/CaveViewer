"""Cover chunk-size recommendation heuristics."""

from __future__ import annotations

import numpy as np
import pytest

from caveviewer.core.chunk_size_advisor import recommend_chunk_size_from_faces


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
