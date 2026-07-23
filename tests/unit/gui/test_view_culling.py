"""Tests for resident chunk frustum culling and cache reuse."""

from __future__ import annotations

import numpy as np

from caveviewer.gui import view_culling


def _bounds(min_xyz, max_xyz):
    return (
        np.array(min_xyz, dtype=np.float32),
        np.array(max_xyz, dtype=np.float32),
    )


def test_frustum_culling_cache_reuses_static_view_until_generation_changes():
    cache = view_culling.FrustumCullingCache()
    view = np.identity(4, dtype=np.float64)
    projection = np.identity(4, dtype=np.float64)
    chunk_gpu_objects = {
        (0, 0, 0): ["inside"],
        (2, 0, 0): ["outside"],
        (9, 9, 9): ["unknown-bounds"],
    }
    chunk_aabbs = {
        (0, 0, 0): _bounds((-0.5, -0.5, -0.5), (0.5, 0.5, 0.5)),
        (2, 0, 0): _bounds((2.0, -0.5, -0.5), (3.0, 0.5, 0.5)),
    }

    visible = cache.visible_chunks(
        view=view,
        projection=projection,
        chunk_gpu_objects=chunk_gpu_objects,
        chunk_aabbs=chunk_aabbs,
        generation=1,
    )
    visible_again = cache.visible_chunks(
        view=view,
        projection=projection,
        chunk_gpu_objects=chunk_gpu_objects,
        chunk_aabbs=chunk_aabbs,
        generation=1,
    )

    assert [cell for cell, _vao_list in visible] == [(0, 0, 0), (9, 9, 9)]
    assert visible_again is visible
    assert cache.reused_last_result is True

    chunk_gpu_objects[(0, 2, 0)] = ["new-inside"]
    chunk_aabbs[(0, 2, 0)] = _bounds((-0.25, -0.25, -0.25), (0.25, 0.25, 0.25))
    visible_after_generation_change = cache.visible_chunks(
        view=view,
        projection=projection,
        chunk_gpu_objects=chunk_gpu_objects,
        chunk_aabbs=chunk_aabbs,
        generation=2,
    )

    assert visible_after_generation_change is not visible
    assert cache.reused_last_result is False
    assert [cell for cell, _vao_list in visible_after_generation_change] == [
        (0, 0, 0),
        (9, 9, 9),
        (0, 2, 0),
    ]


def test_frustum_culling_cache_recomputes_when_view_changes():
    cache = view_culling.FrustumCullingCache()
    projection = np.identity(4, dtype=np.float64)
    chunk_gpu_objects = {(0, 0, 0): ["chunk"]}
    chunk_aabbs = {(0, 0, 0): _bounds((-0.5, -0.5, -0.5), (0.5, 0.5, 0.5))}

    first_visible = cache.visible_chunks(
        view=np.identity(4, dtype=np.float64),
        projection=projection,
        chunk_gpu_objects=chunk_gpu_objects,
        chunk_aabbs=chunk_aabbs,
        generation=1,
    )
    moved_view = np.identity(4, dtype=np.float64)
    moved_view[0, 3] = 0.1

    second_visible = cache.visible_chunks(
        view=moved_view,
        projection=projection,
        chunk_gpu_objects=chunk_gpu_objects,
        chunk_aabbs=chunk_aabbs,
        generation=1,
    )

    assert second_visible is not first_visible
    assert cache.reused_last_result is False
