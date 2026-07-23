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
    assert "route_mode: auto-centerline" in output
    assert "centerline_route: keyframes=24" in output
    assert "render_distance: 6" in output
    assert "measurement_seconds: 120" in output
    assert "texture_resident_cache_mb: 768" in output
    assert "target_length=auto(48 chunks, streaming exercise" in output
    assert "selection=max_visible_chunk_texture_complexity" in output
    assert "movement=after_warmup" in output
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
    (local_map_dir / "_cache" / "manifest.json").write_text(
        json.dumps(_manifest()),
        encoding="utf-8",
    )
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
        assert _kwargs["env"]["CAVEVIEWER_TEXTURE_RESIDENT_CACHE_MB"] == "768"
        scenario_path = Path(command[command.index("--scenario") + 1])
        generated_scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        assert generated_scenario["metadata"]["route_mode"] == "auto_centerline_v1"
        assert generated_scenario["metadata"]["target_route_length_source"] == (
            "devils_eye_streaming_default_chunks"
        )
        assert generated_scenario["metadata"]["target_route_length_chunks"] == 48.0
        assert generated_scenario["measurement_seconds"] == 120.0
        assert generated_scenario["max_runtime_seconds"] == 215.0
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "summary.json").write_text(
            json.dumps(
                _summary(
                    median_fps=90.0,
                    wall_clock_fps=90.0,
                    scenario=generated_scenario,
                ),
                sort_keys=True,
            ),
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
    assert (run_dir / "auto-centerline-route-v1.json").is_file()
    latest_text = (results_dir / "latest-summary.txt").read_text(encoding="utf-8")
    assert "Status: FAIL" in latest_text
    assert "wall_clock_fps=90.00" in latest_text
    assert "Runtime load:" in latest_text
    assert "median_drawn_chunks=40.00" in latest_text
    assert "Runtime texture:" in latest_text
    assert "evictions=3.00" in latest_text
    assert "Startup readiness:" in latest_text
    assert "visible_chunks=40" in latest_text
    assert "textures=40/40 resident" in latest_text
    assert "missing_textures=0" in latest_text
    assert "coverage=40/40 chunks" in latest_text
    assert "missing_chunks=0" in latest_text
    assert "startup_radius=9 chunks" in latest_text
    assert "Streaming request:" in latest_text
    assert "distance=6 chunks" in latest_text
    assert "Streaming effective:" in latest_text
    assert "Texture residency:" in latest_text
    assert "requested_cache_cap=768 MB" in latest_text
    assert "Route: auto_centerline" in latest_text
    assert "selection=max_visible_chunk_texture_complexity_v1" in latest_text
    assert "target_speed_m_per_s=4.00" in latest_text
    assert "Gate baseline: previous-main wall_clock_fps=<missing> median_render_fps=100.00" in latest_text
    assert "FAIL median_fps" in latest_text
    assert "Previous local run compared to current" in latest_text
    assert "Run: previous-main" in latest_text
    assert "Gate: FAIL" in latest_text
    assert (
        "median render FPS: current=90.00 fps, previous=100.00 fps, delta=-10.00%"
        in latest_text
    )
    assert "Run: older-release" not in latest_text
    assert "Time:" not in latest_text
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
    assert benchmark_command[benchmark_command.index("--cache-dir") + 1] == str(
        local_map_dir / "_cache"
    )
    assert benchmark_command[benchmark_command.index("--textures-dir") + 1] == str(
        local_map_dir / "_cache"
    )
    assert str(run_dir / "auto-centerline-route-v1.json") in benchmark_command


def test_devils_eye_xl_uses_only_compatible_history_as_gate_baseline():
    module = _load_script_module()
    current_summary = _summary(median_fps=90.0, wall_clock_fps=90.0)
    incompatible_summary = _summary(median_fps=100.0, wall_clock_fps=100.0)
    incompatible_summary["scenario"]["fingerprint"] = "old-route"
    compatible_summary = _summary(median_fps=95.0, wall_clock_fps=95.0)

    assert module._latest_compatible_record(
        [
            {"label": "old-route", "summary": incompatible_summary},
            {"label": "same-route", "summary": compatible_summary},
        ],
        current_summary,
    )["label"] == "same-route"
    assert module._latest_compatible_record(
        [{"label": "old-route", "summary": incompatible_summary}],
        current_summary,
    ) is None

    history_text = "\n".join(
        module._history_comparison_lines(
            [
                {
                    "label": "old-route",
                    "timestamp_utc": "2026-07-23T10:00:00Z",
                    "summary": incompatible_summary,
                }
            ],
            current_summary=current_summary,
            thresholds=module.BenchmarkThresholds(),
            limit=5,
        )
    )
    assert "Run: old-route" in history_text
    assert "Gate: INCOMPATIBLE (route changed; not used as gate baseline)" in history_text
    assert (
        "wall clock FPS: current=90.00 fps, previous=100.00 fps, delta=-10.00%"
        in history_text
    )


def test_devils_eye_xl_treats_actual_window_size_changes_as_incompatible():
    module = _load_script_module()
    current_summary = _summary(median_fps=90.0, wall_clock_fps=90.0)
    current_summary["environment"]["actual_window_size"] = [2048, 1280]
    current_summary["environment"]["actual_framebuffer_size"] = [4096, 2560]
    previous_summary = _summary(median_fps=100.0, wall_clock_fps=100.0)
    previous_summary["environment"]["actual_window_size"] = [1600, 1000]
    previous_summary["environment"]["actual_framebuffer_size"] = [1600, 1000]

    assert module._latest_compatible_record(
        [{"label": "old-size", "summary": previous_summary}],
        current_summary,
    ) is None

    history_text = "\n".join(
        module._history_comparison_lines(
            [{"label": "old-size", "summary": previous_summary}],
            current_summary=current_summary,
            thresholds=module.BenchmarkThresholds(),
            limit=1,
        )
    )

    assert "Gate: INCOMPATIBLE" in history_text
    assert "window size changed" in history_text
    assert "framebuffer size changed" in history_text


def test_devils_eye_xl_treats_streaming_settings_changes_as_incompatible():
    module = _load_script_module()
    current_summary = _summary(median_fps=90.0, wall_clock_fps=90.0)
    current_summary["environment"]["streaming_settings_fingerprint"] = "streaming-b"
    previous_summary = _summary(median_fps=100.0, wall_clock_fps=100.0)
    previous_summary["environment"]["streaming_settings_fingerprint"] = "streaming-a"

    assert module._latest_compatible_record(
        [{"label": "old-streaming", "summary": previous_summary}],
        current_summary,
    ) is None

    history_text = "\n".join(
        module._history_comparison_lines(
            [{"label": "old-streaming", "summary": previous_summary}],
            current_summary=current_summary,
            thresholds=module.BenchmarkThresholds(),
            limit=1,
        )
    )

    assert "Gate: INCOMPATIBLE" in history_text
    assert "streaming settings changed" in history_text


def _summary(
    *,
    median_fps: float,
    wall_clock_fps: float | None = None,
    scenario: dict | None = None,
) -> dict:
    scenario_payload = (
        {"name": "gold", "fingerprint": "scenario-a"}
        if scenario is None
        else {
            "name": "gold",
            "fingerprint": "scenario-a",
            "metadata": scenario.get("metadata", {}),
            "render_distance": scenario.get("render_distance", 3),
        }
    )
    render_distance = int(scenario_payload.get("render_distance", 3))
    metrics = {
        "mean_fps": median_fps,
        "median_fps": median_fps,
        "one_percent_low_fps": 80.0,
        "p95_frame_ms": 20.0,
        "median_drawn_chunks": 40.0,
        "max_drawn_chunks": 45.0,
        "median_wanted_chunks": 70.0,
        "max_pending_chunks": 3.0,
        "total_chunks_uploaded": 20,
        "total_bytes_uploaded": 1048576,
        "total_upload_stalls": 0,
        "max_ready_chunks": 2.0,
        "frames_with_pending_chunks": 4,
        "frames_with_ready_chunks": 3,
        "frames_with_chunk_uploads": 5,
        "frames_with_chunk_unloads": 2,
        "frames_with_texture_uploads": 2,
        "total_texture_upload_ms": 11.5,
        "total_texture_decode_ms": 4.25,
        "total_texture_mipmap_ms": 2.5,
        "total_texture_bytes_uploaded": 64 * 1024 * 1024,
        "total_texture_material_cache_hits": 12,
        "total_texture_file_cache_hits": 4,
        "total_texture_decoded_cache_hits": 5,
        "total_texture_sync_decodes": 1,
        "total_texture_placeholders": 0,
        "total_texture_evictions": 3,
        "total_texture_evicted_bytes": 48 * 1024 * 1024,
        "stutter_counts": {"over_50ms": 0},
    }
    if wall_clock_fps is not None:
        metrics["wall_clock_fps"] = wall_clock_fps
    return {
        "schema_version": 1,
        "scenario": scenario_payload,
        "reason": "completed",
        "measured_frames": 120,
        "metrics": metrics,
        "environment": {
            "cache_manifest_sha256": "map-a",
            "streaming_settings_fingerprint": "streaming-a",
            "streaming_settings": {
                "render_distance_chunks": render_distance,
                "system_ram_target_percent": "8",
                "gpu_memory_target_percent": "70",
                "gpu_memory_override_gb": "",
                "texture_resident_cache_mb": "768",
                "io_workers": "2",
                "io_reserved_cpus": "3",
                "upload_chunks_per_frame": "1",
                "upload_groups_per_frame": "1",
                "upload_time_budget_ms": "3.0",
            },
            "effective_render_distance_chunks": render_distance,
            "streaming_chunk_size_m": 50.0,
            "streaming_max_loaded_chunks": 488,
            "streaming_ready_backlog_capacity": 16,
            "streaming_worker_target": 2,
            "startup_upload_chunks_per_frame": 4,
            "startup_upload_groups_per_frame": 8,
            "startup_upload_time_budget_ms": 12.0,
            "catchup_upload_chunks_per_frame": 2,
            "catchup_upload_groups_per_frame": 8,
            "catchup_upload_time_budget_ms": 8.0,
            "initial_visual_ready_seconds": 12.5,
            "initial_visual_ready_frames": 3,
            "initial_visual_ready_visible_chunks": 40,
            "initial_visual_ready_required_textures": 40,
            "initial_visual_ready_resident_textures": 40,
            "initial_visual_ready_visible_textures": 12,
            "initial_visual_ready_missing_textures": 0,
            "initial_visual_ready_expected_chunks": 40,
            "initial_visual_ready_covered_chunks": 40,
            "initial_visual_ready_missing_chunks": 0,
            "initial_visual_ready_coverage_pct": 100.0,
            "initial_visual_ready_load_radius_chunks": min(render_distance + 3, 10),
            "texture_max_dimension": 2048,
            "texture_resident_budget_bytes": 1720 * 1024 * 1024,
            "texture_decoded_cache_budget_bytes": 304 * 1024 * 1024,
        },
    }


def _manifest() -> dict:
    chunks = {}
    for x in range(4):
        for y in range(2):
            for z in range(4):
                chunks[f"{x}_{y}_{z}"] = _chunk(x, y, z)
    return {
        "chunk_size": 10.0,
        "mtl_materials": {"rock": "rock.jpg"},
        "chunks": chunks,
    }


def _chunk(x: int, y: int, z: int) -> dict:
    return {
        "bounds_min": [x * 10.0, y * 10.0, z * 10.0],
        "bounds_max": [x * 10.0 + 10.0, y * 10.0 + 10.0, z * 10.0 + 10.0],
        "materials": ["rock"],
    }
