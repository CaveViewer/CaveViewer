"""Tests for OpenGL-independent viewer streaming policy and readiness."""

from __future__ import annotations

import numpy as np
import pytest

from caveviewer.gui import viewer_streaming_runtime as streaming


def test_upload_budget_policy_selects_startup_catchup_and_normal_limits():
    policy = streaming.UploadBudgetPolicy(
        normal=streaming.UploadLimits(1, 2, 3.0),
        catchup=streaming.UploadLimits(2, 8, 8.0),
        startup=streaming.UploadLimits(4, 8, 12.0),
    )

    assert policy.limits_for(None, startup_active=True) == streaming.UploadLimits(
        4, 8, 12.0
    )
    assert policy.limits_for(
        {"ready": 1, "wanted": 5, "loaded_wanted": 2},
        startup_active=False,
    ) == streaming.UploadLimits(2, 8, 8.0)
    assert policy.limits_for(
        {"ready": 0, "wanted": 5, "loaded_wanted": 2},
        startup_active=False,
    ) == streaming.UploadLimits(1, 2, 3.0)


def test_initial_chunk_progress_weights_pending_ready_and_terminal_cells():
    stats = {
        "wanted": 4,
        "total_available": 10,
        "loaded_wanted": 1,
        "failed_wanted": 1,
        "ready": 1,
        "pending": 1,
    }

    assert streaming.initial_chunk_load_progress(stats, 10) == pytest.approx(0.75)
    assert streaming.initial_chunk_load_is_ready(stats, 10) is False


def test_streaming_cell_priority_prefers_cells_in_front_of_camera():
    priority = streaming.streaming_cell_priority_key(
        camera_position=(0.0, 0.0, 0.0),
        camera_forward=(1.0, 0.0, 0.0),
        chunk_size=1.0,
        window_size=(1600, 1000),
        fov_deg=75.0,
    )

    assert priority((2, 0, 0)) < priority((-3, 0, 0))


def test_route_prefetch_snapshot_counts_failed_cells_as_covered():
    result = streaming.route_prefetch_stats(
        frozenset({(0, 0, 0), (1, 0, 0), (2, 0, 0)}),
        loaded_cells=frozenset({(0, 0, 0)}),
        pending_cells=frozenset({(1, 0, 0)}),
        failed_cells=frozenset({(2, 0, 0)}),
    )

    assert result.ready is False
    assert result.loaded_cells == 1
    assert result.pending_cells == 1
    assert result.failed_cells == 1
    assert result.missing_cells == 1
    assert result.coverage_pct == pytest.approx(200.0 / 3.0)


def test_texture_readiness_uses_exact_resident_sources_when_available():
    result = streaming.texture_readiness(
        wanted_sources={"rock.jpg", "silt.jpg"},
        visible_sources={"rock.jpg"},
        resident_sources={"rock.jpg"},
        exact_sources_known=True,
        manager_stats={"unique_files_resident": 2},
    )

    assert result.textures_ready is False
    assert result.required_textures == 2
    assert result.resident_textures == 2
    assert result.missing_textures == 1


def test_visual_coverage_requires_each_frustum_visible_wanted_cell():
    manifest = {
        "chunks": {
            "0_0_0": {
                "bounds_min": [-0.8, -0.8, -0.8],
                "bounds_max": [-0.2, 0.8, 0.8],
            },
            "1_0_0": {
                "bounds_min": [0.2, -0.8, -0.8],
                "bounds_max": [0.8, 0.8, 0.8],
            },
        }
    }

    result = streaming.startup_visual_coverage(
        wanted_cells=frozenset({(0, 0, 0), (1, 0, 0)}),
        visible_cells=[((0, 0, 0), [])],
        failed_cells=frozenset(),
        manifest=manifest,
        view=np.eye(4),
        projection=np.eye(4),
    )

    assert result.coverage_ready is False
    assert result.expected_chunks == 2
    assert result.covered_chunks == 1
    assert result.coverage_pct == pytest.approx(50.0)


def test_visual_readiness_requires_consecutive_settled_frames_and_resets():
    runtime = streaming.ViewerStreamingRuntime(initial_chunks_loaded=True)
    stats = {
        "wanted": 1,
        "total_available": 1,
        "loaded_wanted": 1,
        "pending": 0,
        "ready": 0,
    }
    kwargs = {
        "visible_chunk_count": 1,
        "max_loaded_chunks": 10,
        "upload_state_count": 0,
        "textures": streaming.TextureReadiness(),
        "coverage": streaming.CoverageStats(),
        "route": streaming.RoutePrefetchStats(),
        "settle_frames": 2,
    }

    first, first_ready = runtime.observe_visual_readiness(stats, **kwargs)
    second, second_ready = runtime.observe_visual_readiness(stats, **kwargs)

    assert first["visual_ready"] is False
    assert first_ready is False
    assert second["visual_ready"] is True
    assert second_ready is True

    runtime.reset()
    assert runtime.initial_chunks_loaded is False
    assert runtime.initial_visual_ready is False
    assert runtime.initial_visual_ready_frames == 0
