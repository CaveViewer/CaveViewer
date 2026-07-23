"""Unit tests for the benchmark comparison helper script."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPOSITORY_ROOT / "scripts" / "benchmark" / "compare_benchmark_results.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "compare_benchmark_results_for_tests",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compare_script_uses_threshold_config_and_writes_output(tmp_path):
    module = _load_script_module()
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    thresholds = tmp_path / "thresholds.json"
    output = tmp_path / "comparison.json"
    baseline.write_text(json.dumps(_summary(median_fps=100.0)), encoding="utf-8")
    candidate.write_text(json.dumps(_summary(median_fps=98.0)), encoding="utf-8")
    thresholds.write_text(
        json.dumps(
            {
                "version": 1,
                "thresholds": {
                    "max_median_fps_drop_pct": 3.0,
                    "max_one_percent_low_fps_drop_pct": 3.0,
                    "max_p95_frame_ms_increase_pct": 3.0,
                    "max_stutter_frame_increase_pct": 3.0,
                },
            }
        ),
        encoding="utf-8",
    )

    exit_code = module.main(
        [
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--output",
            str(output),
            "--thresholds",
            str(thresholds),
        ]
    )

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True


def test_compare_script_returns_configuration_error_for_bad_inputs(tmp_path):
    module = _load_script_module()
    output = tmp_path / "comparison.json"

    exit_code = module.main(
        [
            "--baseline",
            str(tmp_path / "missing-baseline.json"),
            "--candidate",
            str(tmp_path / "missing-candidate.json"),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 2
    assert not output.exists()


def _summary(*, median_fps: float) -> dict:
    return {
        "scenario": {"name": "gold", "fingerprint": "scenario-a"},
        "environment": {"cache_manifest_sha256": "map-a"},
        "metrics": {
            "median_fps": median_fps,
            "one_percent_low_fps": 90.0,
            "p95_frame_ms": 20.0,
            "stutter_counts": {"over_50ms": 0},
        },
    }
