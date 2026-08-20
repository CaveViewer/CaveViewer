"""Unit tests for the CaveViewer benchmark CLI wrapper."""

from __future__ import annotations

import json
import os
from pathlib import Path

from caveviewer import benchmark
from caveviewer.gui import viewer_window


def test_benchmark_cli_runs_viewer_benchmark_and_prints_summary(
    tmp_path,
    monkeypatch,
    capsys,
):
    cache_dir = tmp_path / "cache"
    scenario_path = tmp_path / "scenario.json"
    output_dir = tmp_path / "out"
    cache_dir.mkdir()
    scenario_path.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "cli",
                "warmup_seconds": 0.0,
                "measurement_seconds": 1.0,
                "route": [
                    {
                        "time_s": 0.0,
                        "position": [0.0, 0.0, 0.0],
                        "yaw_deg": 0.0,
                        "pitch_deg": 0.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("CAVEVIEWER_VSYNC", raising=False)
    caveviewer_home = tmp_path / "caveviewer-home"
    preferences_dir = caveviewer_home / "config"
    preferences_dir.mkdir(parents=True)
    monkeypatch.setenv("CAVEVIEWER_HOME", str(caveviewer_home))
    (preferences_dir / "advanced_settings.json").write_text(
        json.dumps(
            {
                "io_workers": "4",
                "upload_chunks_per_frame": "5",
                "upload_groups_per_frame": "7",
                "upload_time_budget_ms": "9.5",
            }
        ),
        encoding="utf-8",
    )

    def fake_run_viewer_benchmark(
        cache,
        textures,
        scenario,
        output,
        *,
        runtime_settings,
    ):
        assert cache == str(cache_dir)
        assert textures == str(cache_dir)
        assert scenario.name == "cli"
        assert output == str(output_dir)
        assert runtime_settings.viewer_configuration().vsync is False
        assert runtime_settings["io_workers"] == 4
        assert runtime_settings["upload_chunks_per_frame"] == 5
        assert runtime_settings["upload_groups_per_frame"] == 7
        assert runtime_settings["upload_time_budget_ms"] == 9.5
        assert "CAVEVIEWER_VSYNC" not in os.environ
        assert "CAVEVIEWER_IO_WORKERS" not in os.environ
        summary_path = Path(output) / "summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps({"metrics": {"median_fps": 60.0}}),
            encoding="utf-8",
        )
        return str(summary_path)

    monkeypatch.setattr(
        viewer_window,
        "run_viewer_benchmark",
        fake_run_viewer_benchmark,
    )

    exit_code = benchmark.main(
        [
            "--cache-dir",
            str(cache_dir),
            "--scenario",
            str(scenario_path),
            "--output-dir",
            str(output_dir),
            "--json",
        ]
    )

    assert exit_code == 0
    assert (output_dir / "benchmark.log").exists()
    assert '"median_fps": 60.0' in capsys.readouterr().out


def test_benchmark_cli_reports_configuration_errors(tmp_path):
    exit_code = benchmark.main(
        [
            "--cache-dir",
            str(tmp_path / "missing-cache"),
            "--scenario",
            str(tmp_path / "missing-scenario.json"),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert exit_code == 2
