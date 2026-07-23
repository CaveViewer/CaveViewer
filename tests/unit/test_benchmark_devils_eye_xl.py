"""Unit tests for the machine-local Devil's Eye XL benchmark wrapper."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPOSITORY_ROOT / "scripts" / "benchmark" / "run_devils_eye_xl_benchmark.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "run_devils_eye_xl_benchmark_for_tests",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_devils_eye_xl_dry_run_plans_copy_compile_and_local_history(
    tmp_path,
    capsys,
):
    module = _load_script_module()
    source_map_dir = tmp_path / "Downloads" / "Maps" / "Devil's Eye XL"
    local_map_dir = tmp_path / "repo" / ".benchmark-data" / "maps" / "devils-eye-xl"
    results_dir = tmp_path / "repo" / ".benchmark-data" / "results" / "devils-eye-xl"
    source_map_dir.mkdir(parents=True)
    (source_map_dir / "gold.obj").write_text("o gold\n", encoding="utf-8")

    exit_code = module.main(
        [
            "--source-map-dir",
            str(source_map_dir),
            "--local-map-dir",
            str(local_map_dir),
            "--results-dir",
            str(results_dir),
            "--label",
            "candidate stack",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Devil's Eye XL local benchmark plan:" in output
    assert "copy_map: yes" in output
    assert "compile_cache: yes" in output
    assert str(local_map_dir / "_cache") in output
    assert str(results_dir / "history.jsonl") in output
    assert "run_local_benchmark.py" in output
    assert "candidate-stack" in output


def test_devils_eye_xl_run_compares_with_previous_record_and_writes_summary(
    tmp_path,
    monkeypatch,
):
    module = _load_script_module()
    local_map_dir = tmp_path / "map"
    results_dir = tmp_path / "results"
    run_dir = results_dir / "runs" / "candidate"
    history_path = results_dir / "history.jsonl"
    local_map_dir.mkdir()
    (local_map_dir / "gold.obj").write_text("o gold\n", encoding="utf-8")
    (local_map_dir / "_cache").mkdir()
    (local_map_dir / "_cache" / "manifest.json").write_text("{}", encoding="utf-8")
    history_path.parent.mkdir(parents=True)
    history_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema_version": 1,
                        "timestamp_utc": "2026-07-22T10:00:00Z",
                        "label": "older-release",
                        "summary": _summary(median_fps=80.0),
                    },
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "schema_version": 1,
                        "timestamp_utc": "2026-07-23T10:00:00Z",
                        "label": "previous-main",
                        "summary": _summary(median_fps=100.0),
                    },
                    sort_keys=True,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    commands = []

    def fake_run(command, **_kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="abcdef1234567890\n")
        raise AssertionError(f"unexpected command: {command!r}")

    def fake_logged_subprocess(command, _plan, **_kwargs):
        commands.append(command)
        assert any(str(part).endswith("run_local_benchmark.py") for part in command)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "summary.json").write_text(
            json.dumps(_summary(median_fps=90.0), sort_keys=True),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_run_logged_subprocess", fake_logged_subprocess)

    exit_code = module.main(
        [
            "--local-map-dir",
            str(local_map_dir),
            "--results-dir",
            str(results_dir),
            "--label",
            "candidate",
        ]
    )

    assert exit_code == 1
    assert (run_dir / "comparison.json").is_file()
    latest_text = (results_dir / "latest-summary.txt").read_text(encoding="utf-8")
    assert "Status: FAIL" in latest_text
    assert "Gate baseline: previous-main median_fps=100.00" in latest_text
    assert "FAIL median_fps" in latest_text
    assert "Previous local runs compared to current" in latest_text
    assert "previous-main @ 2026-07-23T10:00:00Z" in latest_text
    assert "median_fps=100.00 (-10.00%)" in latest_text
    assert "older-release @ 2026-07-22T10:00:00Z" in latest_text
    assert "median_fps=80.00 (+12.50%)" in latest_text
    orchestration_log = (run_dir / "orchestration.log").read_text(encoding="utf-8")
    assert "Devil's Eye XL local benchmark plan:" in orchestration_log
    assert "Status: FAIL" in orchestration_log
    history_lines = history_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(history_lines) == 3
    assert json.loads(history_lines[-1])["comparison_passed"] is False
    benchmark_command = next(
        command
        for command in commands
        if any(str(part).endswith("run_local_benchmark.py") for part in command)
    )
    assert str(local_map_dir / "_cache") in benchmark_command
    assert str(local_map_dir) in benchmark_command


def _summary(*, median_fps: float) -> dict:
    return {
        "schema_version": 1,
        "scenario": {"name": "gold", "fingerprint": "scenario-a"},
        "reason": "completed",
        "measured_frames": 120,
        "metrics": {
            "mean_fps": median_fps,
            "median_fps": median_fps,
            "one_percent_low_fps": 80.0,
            "p95_frame_ms": 20.0,
            "stutter_counts": {"over_50ms": 0},
        },
        "environment": {"cache_manifest_sha256": "map-a"},
    }
