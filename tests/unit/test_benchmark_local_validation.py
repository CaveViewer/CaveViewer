"""Unit tests for the local benchmark validation harness."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "benchmark" / "run_local_benchmark.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "run_local_benchmark_for_tests",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_benchmark_harness_dry_run_validates_and_prints_plan(
    tmp_path,
    capsys,
):
    module = _load_script_module()
    cache_dir = tmp_path / "cache"
    output_root = tmp_path / "out"
    baseline_summary = tmp_path / "baseline-summary.json"
    cache_dir.mkdir()
    (cache_dir / "manifest.json").write_text(
        json.dumps({"chunks": {}, "mtl_materials": {}}),
        encoding="utf-8",
    )
    baseline_summary.write_text(
        json.dumps({"metrics": {"median_fps": 60.0}}),
        encoding="utf-8",
    )

    exit_code = module.main(
        [
            "--cache-dir",
            str(cache_dir),
            "--baseline-summary",
            str(baseline_summary),
            "--output-root",
            str(output_root),
            "--label",
            "candidate stack",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Benchmark validation plan:" in output
    assert "scenario_fingerprint:" in output
    assert "caveviewer.benchmark" in output
    assert "compare_benchmark_results.py" in output
    assert str(output_root / "candidate-stack") in output


def test_local_benchmark_harness_rejects_cache_without_manifest(tmp_path, capsys):
    module = _load_script_module()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    exit_code = module.main(
        [
            "--cache-dir",
            str(cache_dir),
            "--dry-run",
        ]
    )

    assert exit_code == 2
    assert "manifest.json" in capsys.readouterr().err
