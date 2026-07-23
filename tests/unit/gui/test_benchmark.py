"""Tests for viewer benchmark scenarios, metrics, and comparisons."""

from __future__ import annotations

import json
import logging
import math
from types import SimpleNamespace

import pytest

from caveviewer.gui.benchmark import (
    BenchmarkConfigurationError,
    BenchmarkController,
    BenchmarkScenario,
    BenchmarkThresholds,
    apply_pose_to_camera,
    compare_summaries,
    summarize_samples,
)


def test_scenario_parses_and_interpolates_shortest_angle_route():
    scenario = BenchmarkScenario.from_mapping(
        {
            "version": 1,
            "name": "turn",
            "position_mode": "first_chunk_center_offset",
            "warmup_seconds": 1.0,
            "measurement_seconds": 2.0,
            "route": [
                {
                    "time_s": 0.0,
                    "position": [0.0, 0.0, 0.0],
                    "yaw_deg": 350.0,
                    "pitch_deg": 0.0,
                },
                {
                    "time_s": 10.0,
                    "position": [10.0, 0.0, -20.0],
                    "yaw_deg": 10.0,
                    "pitch_deg": -10.0,
                },
            ],
            "stutter_thresholds_ms": [100.0, 33.3],
        }
    )

    pose = scenario.pose_at(5.0)

    assert scenario.position_mode == "first_chunk_center_offset"
    assert scenario.stutter_thresholds_ms == (33.3, 100.0)
    assert len(scenario.fingerprint) == 64
    assert scenario.fingerprint == BenchmarkScenario.from_mapping(
        scenario.identity_payload
    ).fingerprint
    assert pose.position == (5.0, 0.0, -10.0)
    assert math.isclose(pose.yaw_deg, 360.0)
    assert math.isclose(pose.pitch_deg, -5.0)


def test_scenario_rejects_invalid_route_order():
    with pytest.raises(
        BenchmarkConfigurationError,
        match="strictly increasing",
    ):
        BenchmarkScenario.from_mapping(
            {
                "name": "bad",
                "route": [
                    {"time_s": 1.0, "position": [0, 0, 0]},
                    {"time_s": 1.0, "position": [1, 0, 0]},
                ],
            }
        )


def test_scenario_reports_invalid_numeric_policy_fields():
    with pytest.raises(BenchmarkConfigurationError, match="render_distance"):
        BenchmarkScenario.from_mapping(
            {
                "name": "bad-number",
                "render_distance": "far",
                "route": [{"time_s": 0.0, "position": [0, 0, 0]}],
            }
        )


def test_apply_pose_supports_map_relative_position_origin():
    camera = SimpleNamespace()

    apply_pose_to_camera(
        camera,
        BenchmarkScenario.from_mapping(
            {
                "name": "pose",
                "route": [
                    {
                        "time_s": 0.0,
                        "position": [1.0, 2.0, 3.0],
                        "yaw_deg": 45.0,
                        "pitch_deg": -10.0,
                    }
                ],
            }
        ).route[0],
        position_origin=(10.0, 20.0, 30.0),
    )

    assert camera.position.tolist() == [11.0, 22.0, 33.0]
    assert math.isclose(camera.yaw, math.radians(45.0))
    assert math.isclose(camera.pitch, math.radians(-10.0))


def test_controller_writes_frame_artifacts_and_summary(tmp_path):
    scenario = _scenario(measurement_seconds=0.02)
    controller = BenchmarkController(
        scenario=scenario,
        output_dir=tmp_path,
        logger=logging.getLogger("benchmark-test"),
        perf_counter=lambda: 0.0,
        environment={"runner": "unit"},
    )
    camera = SimpleNamespace()

    controller.set_position_origin((100.0, 0.0, 0.0))
    controller.update_camera(camera, now=0.0)
    complete = _record_frame(controller, now=0.03, frame_ms=20.0)
    summary = controller.finish(reason="completed")

    assert complete is True
    assert controller.summary_path == tmp_path / "summary.json"
    assert (tmp_path / "environment.json").exists()
    assert (tmp_path / "frames.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert summary["measured_frames"] == 1
    assert summary["metrics"]["median_fps"] == 50.0
    assert summary["metrics"]["wall_clock_fps"] == 50.0
    assert summary["metrics"]["wall_clock_seconds"] == 0.02
    assert summary["scenario"]["fingerprint"] == scenario.fingerprint
    assert summary["environment"]["runner"] == "unit"


def test_controller_update_environment_refreshes_artifact(tmp_path):
    controller = BenchmarkController(
        scenario=_scenario(),
        output_dir=tmp_path,
        logger=logging.getLogger("benchmark-test"),
        perf_counter=lambda: 0.0,
        environment={"runner": "unit"},
    )

    controller.prepare_output()
    controller.update_environment({"streaming_worker_target": 4})

    environment = json.loads(
        (tmp_path / "environment.json").read_text(encoding="utf-8")
    )
    assert environment["runner"] == "unit"
    assert environment["streaming_worker_target"] == 4


def test_controller_completion_does_not_wait_for_next_sample_bucket(tmp_path):
    scenario = _scenario(measurement_seconds=0.02, sample_every_n_frames=2)
    controller = BenchmarkController(
        scenario=scenario,
        output_dir=tmp_path,
        logger=logging.getLogger("benchmark-test"),
        perf_counter=lambda: 0.0,
    )

    controller.update_camera(SimpleNamespace(), now=0.0)

    assert _record_frame(controller, now=0.03, frame_ms=20.0) is True
    assert controller.finish(reason="completed")["measured_frames"] == 0


def test_controller_max_runtime_includes_startup_before_measurement(tmp_path):
    scenario = BenchmarkScenario.from_mapping(
        {
            "name": "startup-timeout",
            "warmup_seconds": 0.0,
            "measurement_seconds": 0.1,
            "max_runtime_seconds": 0.5,
            "route": [{"time_s": 0.0, "position": [0.0, 0.0, 0.0]}],
        }
    )
    controller = BenchmarkController(
        scenario=scenario,
        output_dir=tmp_path,
        logger=logging.getLogger("benchmark-test"),
        perf_counter=lambda: 10.0,
    )

    controller.prepare_output()

    assert controller.exceeded_max_runtime(10.4) is False
    assert controller.exceeded_max_runtime(10.5) is True


def test_summarize_samples_reports_stutter_counts():
    scenario = _scenario()
    samples = [
        _sample(frame_index=1, frame_ms=10.0),
        _sample(frame_index=2, frame_ms=40.0),
        _sample(frame_index=3, frame_ms=60.0),
    ]

    summary = summarize_samples(
        samples,
        scenario=scenario,
        environment={"gpu": "unit"},
        reason="completed",
    )

    assert summary["metrics"]["median_fps"] == 25.0
    assert summary["metrics"]["wall_clock_fps"] == 3.0
    assert summary["metrics"]["median_frame_interval_ms"] == 16.666667
    assert summary["metrics"]["median_drawn_chunks"] == 4.0
    assert summary["metrics"]["max_wanted_chunks"] == 6.0
    assert summary["metrics"]["total_chunks_uploaded"] == 3
    assert summary["metrics"]["total_bytes_uploaded"] == 12288
    assert summary["metrics"]["stutter_counts"] == {
        "over_33_3ms": 2,
        "over_50ms": 1,
        "over_100ms": 0,
    }
    assert summary["scenario"]["position_mode"] == "absolute"
    assert summary["scenario"]["fingerprint"] == scenario.fingerprint


def test_thresholds_load_from_versioned_config_with_overrides(tmp_path):
    threshold_path = tmp_path / "thresholds.json"
    threshold_path.write_text(
        """
        {
          "version": 1,
          "thresholds": {
            "max_wall_clock_fps_drop_pct": 1.5,
            "max_median_fps_drop_pct": 2.0,
            "max_one_percent_low_fps_drop_pct": 4.0,
            "max_p95_frame_ms_increase_pct": 6.0,
            "max_stutter_frame_increase_pct": 8.0
          }
        }
        """,
        encoding="utf-8",
    )

    thresholds = BenchmarkThresholds.load(threshold_path).with_overrides(
        max_p95_frame_ms_increase_pct=7.5,
        max_wall_clock_fps_drop_pct=2.5,
    )

    assert thresholds.max_wall_clock_fps_drop_pct == 2.5
    assert thresholds.max_median_fps_drop_pct == 2.0
    assert thresholds.max_one_percent_low_fps_drop_pct == 4.0
    assert thresholds.max_p95_frame_ms_increase_pct == 7.5
    assert thresholds.max_stutter_frame_increase_pct == 8.0


def test_compare_summaries_flags_fps_and_frame_time_regressions():
    comparison = compare_summaries(
        {
            "scenario": {"name": "gold", "fingerprint": "scenario-a"},
            "environment": {"cache_manifest_sha256": "map-a"},
            "metrics": {
                "wall_clock_fps": 100.0,
                "median_fps": 100.0,
                "one_percent_low_fps": 80.0,
                "p95_frame_ms": 20.0,
                "stutter_counts": {"over_50ms": 2},
            },
        },
        {
            "scenario": {"name": "gold", "fingerprint": "scenario-a"},
            "environment": {"cache_manifest_sha256": "map-a"},
            "metrics": {
                "wall_clock_fps": 94.0,
                "median_fps": 94.0,
                "one_percent_low_fps": 70.0,
                "p95_frame_ms": 24.0,
                "stutter_counts": {"over_50ms": 3},
            },
        },
        BenchmarkThresholds(
            max_wall_clock_fps_drop_pct=5.0,
            max_median_fps_drop_pct=5.0,
            max_one_percent_low_fps_drop_pct=10.0,
            max_p95_frame_ms_increase_pct=15.0,
            max_stutter_frame_increase_pct=20.0,
        ),
    )

    failed_metrics = {
        check["metric"] for check in comparison["checks"] if not check["passed"]
    }
    assert comparison["passed"] is False
    assert failed_metrics == {
        "median_fps",
        "wall_clock_fps",
        "one_percent_low_fps",
        "p95_frame_ms",
        "stutter_counts.over_50ms",
    }


def test_compare_summaries_skips_wall_clock_check_for_legacy_summaries():
    comparison = compare_summaries(
        {
            "scenario": {"name": "gold", "fingerprint": "scenario-a"},
            "environment": {"cache_manifest_sha256": "map-a"},
            "metrics": {
                "median_fps": 100.0,
                "one_percent_low_fps": 80.0,
                "p95_frame_ms": 20.0,
                "stutter_counts": {"over_50ms": 0},
            },
        },
        {
            "scenario": {"name": "gold", "fingerprint": "scenario-a"},
            "environment": {"cache_manifest_sha256": "map-a"},
            "metrics": {
                "median_fps": 100.0,
                "one_percent_low_fps": 80.0,
                "p95_frame_ms": 20.0,
                "stutter_counts": {"over_50ms": 0},
            },
        },
    )

    wall_clock_check = next(
        check for check in comparison["checks"] if check["metric"] == "wall_clock_fps"
    )
    assert wall_clock_check["passed"] is True
    assert wall_clock_check["skipped"] is True


def test_compare_summaries_fails_on_scenario_or_map_mismatch():
    comparison = compare_summaries(
        {
            "scenario": {"name": "gold", "fingerprint": "scenario-a"},
            "environment": {"cache_manifest_sha256": "map-a"},
            "metrics": {
                "median_fps": 100.0,
                "one_percent_low_fps": 80.0,
                "p95_frame_ms": 20.0,
                "stutter_counts": {"over_50ms": 0},
            },
        },
        {
            "scenario": {"name": "gold", "fingerprint": "scenario-b"},
            "environment": {"cache_manifest_sha256": "map-b"},
            "metrics": {
                "median_fps": 100.0,
                "one_percent_low_fps": 80.0,
                "p95_frame_ms": 20.0,
                "stutter_counts": {"over_50ms": 0},
            },
        },
    )

    failed_metrics = {
        check["metric"] for check in comparison["checks"] if not check["passed"]
    }
    assert comparison["passed"] is False
    assert "scenario.fingerprint" in failed_metrics
    assert "environment.cache_manifest_sha256" in failed_metrics


def test_compare_summaries_fails_on_actual_window_mismatch():
    comparison = compare_summaries(
        {
            "scenario": {"name": "gold", "fingerprint": "scenario-a"},
            "environment": {
                "cache_manifest_sha256": "map-a",
                "actual_window_size": [1600, 1000],
                "actual_framebuffer_size": [1600, 1000],
            },
            "metrics": {
                "median_fps": 100.0,
                "one_percent_low_fps": 80.0,
                "p95_frame_ms": 20.0,
                "stutter_counts": {"over_50ms": 0},
            },
        },
        {
            "scenario": {"name": "gold", "fingerprint": "scenario-a"},
            "environment": {
                "cache_manifest_sha256": "map-a",
                "actual_window_size": [2048, 1280],
                "actual_framebuffer_size": [4096, 2560],
            },
            "metrics": {
                "median_fps": 100.0,
                "one_percent_low_fps": 80.0,
                "p95_frame_ms": 20.0,
                "stutter_counts": {"over_50ms": 0},
            },
        },
    )

    failed_metrics = {
        check["metric"] for check in comparison["checks"] if not check["passed"]
    }
    assert comparison["passed"] is False
    assert "environment.actual_window_size" in failed_metrics
    assert "environment.actual_framebuffer_size" in failed_metrics


def test_compare_summaries_fails_on_streaming_settings_mismatch():
    comparison = compare_summaries(
        {
            "scenario": {"name": "gold", "fingerprint": "scenario-a"},
            "environment": {
                "cache_manifest_sha256": "map-a",
                "streaming_settings_fingerprint": "streaming-a",
            },
            "metrics": {
                "median_fps": 100.0,
                "one_percent_low_fps": 80.0,
                "p95_frame_ms": 20.0,
                "stutter_counts": {"over_50ms": 0},
            },
        },
        {
            "scenario": {"name": "gold", "fingerprint": "scenario-a"},
            "environment": {
                "cache_manifest_sha256": "map-a",
                "streaming_settings_fingerprint": "streaming-b",
            },
            "metrics": {
                "median_fps": 100.0,
                "one_percent_low_fps": 80.0,
                "p95_frame_ms": 20.0,
                "stutter_counts": {"over_50ms": 0},
            },
        },
    )

    failed_metrics = {
        check["metric"] for check in comparison["checks"] if not check["passed"]
    }
    assert comparison["passed"] is False
    assert "environment.streaming_settings_fingerprint" in failed_metrics


def test_compare_summaries_allows_absent_actual_framebuffer_when_both_missing():
    comparison = compare_summaries(
        {
            "scenario": {"name": "gold", "fingerprint": "scenario-a"},
            "environment": {
                "cache_manifest_sha256": "map-a",
                "actual_framebuffer_size": None,
            },
            "metrics": {
                "median_fps": 100.0,
                "one_percent_low_fps": 80.0,
                "p95_frame_ms": 20.0,
                "stutter_counts": {"over_50ms": 0},
            },
        },
        {
            "scenario": {"name": "gold", "fingerprint": "scenario-a"},
            "environment": {
                "cache_manifest_sha256": "map-a",
                "actual_framebuffer_size": None,
            },
            "metrics": {
                "median_fps": 100.0,
                "one_percent_low_fps": 80.0,
                "p95_frame_ms": 20.0,
                "stutter_counts": {"over_50ms": 0},
            },
        },
    )

    metrics = {check["metric"] for check in comparison["checks"]}
    assert "environment.actual_framebuffer_size" not in metrics
    assert comparison["passed"] is True


def _scenario(
    *,
    measurement_seconds: float = 1.0,
    sample_every_n_frames: int = 1,
) -> BenchmarkScenario:
    return BenchmarkScenario.from_mapping(
        {
            "name": "unit",
            "warmup_seconds": 0.0,
            "measurement_seconds": measurement_seconds,
            "sample_every_n_frames": sample_every_n_frames,
            "route": [
                {
                    "time_s": 0.0,
                    "position": [0.0, 0.0, 0.0],
                    "yaw_deg": 0.0,
                    "pitch_deg": 0.0,
                }
            ],
        }
    )


def _record_frame(
    controller: BenchmarkController,
    *,
    now: float,
    frame_ms: float,
) -> bool:
    return controller.record_frame(
        now=now,
        frame_ms=frame_ms,
        streaming_ms=1.0,
        scene_setup_ms=2.0,
        mesh_draw_ms=3.0,
        mesh_cull_ms=0.5,
        mesh_submit_ms=2.5,
        overlay_ms=0.25,
        other_ms=0.75,
        drawn_chunks=4,
        resident_chunks=5,
        world_stats={
            "loaded": 5,
            "pending": 1,
            "ready": 2,
            "unload_pending": 0,
            "wanted": 6,
        },
        streaming_timing={
            "chunks_uploaded": 1,
            "chunks_unloaded": 0,
            "bytes_uploaded": 4096,
            "upload_stalls": 0,
        },
    )


def _sample(*, frame_index: int, frame_ms: float):
    return SimpleNamespace(
        frame_index=frame_index,
        elapsed_s=frame_index / 60.0,
        measured_elapsed_s=frame_index / 60.0,
        frame_ms=frame_ms,
        fps=1000.0 / frame_ms,
        streaming_ms=1.0,
        scene_setup_ms=2.0,
        mesh_draw_ms=3.0,
        mesh_cull_ms=0.5,
        mesh_submit_ms=2.5,
        overlay_ms=0.25,
        other_ms=0.75,
        drawn_chunks=4,
        resident_chunks=5,
        loaded_chunks=5,
        pending_chunks=1,
        ready_chunks=2,
        unload_pending_chunks=0,
        wanted_chunks=6,
        chunks_uploaded=1,
        chunks_unloaded=0,
        bytes_uploaded=4096,
        upload_stalls=0,
    )
