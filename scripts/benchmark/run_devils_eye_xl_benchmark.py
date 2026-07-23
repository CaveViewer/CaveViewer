#!/usr/bin/env python3
"""Run the machine-local Devil's Eye XL streaming FPS benchmark.

The gold map is intentionally local-only. A real run copies
``~/Downloads/Maps/Devil's Eye XL`` into the ignored repository-local
``.benchmark-data`` tree when needed, compiles a map-local ``_cache`` when the
manifest is missing, runs the existing benchmark wrapper, compares with the
latest local history record, and writes a human-readable summary.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Any, Mapping, Sequence


_SCRIPT_PATH = Path(__file__).resolve()
_REPOSITORY_ROOT = _SCRIPT_PATH.parents[2]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from caveviewer.gui.benchmark import (
    BenchmarkConfigurationError,
    BenchmarkScenario,
    BenchmarkThresholds,
    compare_summaries,
    load_json_file,
)
from caveviewer.gui.benchmark_routes import (
    CENTERLINE_ROUTE_VERTICAL_POSITION_FRACTION,
    DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS,
    DEFAULT_DENSE_ROUTE_KEYFRAMES,
    DEFAULT_DENSE_ROUTE_PERCENTILE,
    generate_centerline_route_scenario,
    generate_dense_chunk_route_scenario,
)


LOCAL_BENCHMARK_ID = "devils-eye-xl"
DEFAULT_SOURCE_MAP_DIR = Path.home() / "Downloads" / "Maps" / "Devil's Eye XL"
DEFAULT_LOCAL_MAP_DIR = (
    _REPOSITORY_ROOT / ".benchmark-data" / "maps" / LOCAL_BENCHMARK_ID
)
DEFAULT_RESULTS_DIR = (
    _REPOSITORY_ROOT / ".benchmark-data" / "results" / LOCAL_BENCHMARK_ID
)
DEFAULT_SCENARIO_PATH = _REPOSITORY_ROOT / "benchmarks" / "gold-route-v1.json"
DEFAULT_THRESHOLDS_PATH = (
    _REPOSITORY_ROOT / "benchmarks" / "viewer-thresholds.v1.json"
)
CACHE_DIRNAME = "_cache"
MANIFEST_NAME = "manifest.json"
MAP_SOURCE_SUFFIXES = {".glb", ".gltf", ".obj"}
LOCAL_HISTORY_SCHEMA_VERSION = 1
DEFAULT_DEVILS_EYE_RENDER_DISTANCE = 6
DEFAULT_DEVILS_EYE_MEASUREMENT_SECONDS = 120.0
DEFAULT_DEVILS_EYE_MAX_RUNTIME_MARGIN_SECONDS = 90.0
DEFAULT_DEVILS_EYE_CENTERLINE_ROUTE_KEYFRAMES = 24
DEFAULT_DEVILS_EYE_CENTERLINE_ROUTE_TARGET_CHUNKS = 48.0
DEFAULT_DEVILS_EYE_TEXTURE_RESIDENT_CACHE_MB = 768.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the local-only Devil's Eye XL FPS benchmark, compare with the "
            "previous local record, and write a text summary."
        )
    )
    parser.add_argument(
        "--source-map-dir",
        default=str(DEFAULT_SOURCE_MAP_DIR),
        help=(
            "Original gold map directory. Defaults to "
            "~/Downloads/Maps/Devil's Eye XL."
        ),
    )
    parser.add_argument(
        "--local-map-dir",
        default=str(DEFAULT_LOCAL_MAP_DIR),
        help=(
            "Ignored repository-local copy of the gold map. Defaults to "
            ".benchmark-data/maps/devils-eye-xl."
        ),
    )
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help=(
            "Ignored repository-local benchmark result/history directory. "
            "Defaults to .benchmark-data/results/devils-eye-xl."
        ),
    )
    parser.add_argument(
        "--scenario",
        default=str(DEFAULT_SCENARIO_PATH),
        help=(
            "Benchmark scenario JSON. In generated route modes this is used as "
            "the timing/control template. Defaults to benchmarks/gold-route-v1.json."
        ),
    )
    parser.add_argument(
        "--route-mode",
        choices=("auto-centerline", "auto-dense", "static"),
        default="auto-centerline",
        help=(
            "Use a vertex-footprint centerline route, use a manifest-derived "
            "dense route, or run the scenario file exactly as written. Defaults "
            "to auto-centerline."
        ),
    )
    parser.add_argument(
        "--render-distance",
        type=int,
        default=DEFAULT_DEVILS_EYE_RENDER_DISTANCE,
        help=(
            "Render-distance chunk radius for the local gold benchmark. "
            f"Defaults to {DEFAULT_DEVILS_EYE_RENDER_DISTANCE} to exercise "
            "a visually fuller Devil's Eye XL load."
        ),
    )
    parser.add_argument(
        "--centerline-route-keyframes",
        type=int,
        default=DEFAULT_DEVILS_EYE_CENTERLINE_ROUTE_KEYFRAMES,
        help="Target number of keyframes for the generated centerline route.",
    )
    parser.add_argument(
        "--centerline-route-target-length-m",
        type=float,
        help=(
            "Target centerline segment length in map units. Defaults to a "
            "Devil's Eye XL streaming exercise route of "
            f"{DEFAULT_DEVILS_EYE_CENTERLINE_ROUTE_TARGET_CHUNKS:g} chunks "
            "so the measured window crosses chunk neighborhoods."
        ),
    )
    parser.add_argument(
        "--centerline-route-y-search-radius-cells",
        type=int,
        default=DEFAULT_CENTERLINE_ROUTE_Y_SEARCH_RADIUS_CELLS,
        help=(
            "Neighboring chunk-column radius used to estimate centerline Y. "
            "Defaults to 1."
        ),
    )
    parser.add_argument(
        "--dense-route-keyframes",
        type=int,
        default=DEFAULT_DENSE_ROUTE_KEYFRAMES,
        help="Target number of keyframes for the generated dense route.",
    )
    parser.add_argument(
        "--dense-route-percentile",
        type=float,
        default=DEFAULT_DENSE_ROUTE_PERCENTILE,
        help="Chunk-density percentile used to select dense route cells.",
    )
    parser.add_argument(
        "--thresholds",
        default=str(DEFAULT_THRESHOLDS_PATH),
        help=(
            "Benchmark threshold JSON. Defaults to "
            "benchmarks/viewer-thresholds.v1.json."
        ),
    )
    parser.add_argument(
        "--measurement-seconds",
        type=float,
        default=DEFAULT_DEVILS_EYE_MEASUREMENT_SECONDS,
        help=(
            "Measured route duration. Defaults to "
            f"{DEFAULT_DEVILS_EYE_MEASUREMENT_SECONDS:g}s for the local "
            "streaming stress benchmark."
        ),
    )
    parser.add_argument(
        "--max-runtime-seconds",
        type=float,
        help=(
            "Maximum viewer benchmark runtime after output preparation. "
            "Defaults to warmup + measurement + "
            f"{DEFAULT_DEVILS_EYE_MAX_RUNTIME_MARGIN_SECONDS:g}s."
        ),
    )
    parser.add_argument(
        "--texture-resident-cache-mb",
        type=float,
        default=DEFAULT_DEVILS_EYE_TEXTURE_RESIDENT_CACHE_MB,
        help=(
            "Benchmark-only resident texture cache cap in MB. Defaults to "
            f"{DEFAULT_DEVILS_EYE_TEXTURE_RESIDENT_CACHE_MB:g} MB so the "
            "route exercises texture eviction/reload."
        ),
    )
    parser.add_argument(
        "--label",
        help="Run label used as the artifact subdirectory name.",
    )
    parser.add_argument(
        "--log-level",
        default="DEBUG",
        help="Benchmark log level. Defaults to DEBUG for inspectable local output.",
    )
    parser.add_argument(
        "--vsync",
        choices=("off", "on", "unchanged"),
        default="unchanged",
        help=(
            "Forwarded caveviewer-benchmark vsync policy. Defaults to unchanged "
            "so the local gold benchmark starts like the regular app."
        ),
    )
    parser.add_argument(
        "--xvfb",
        action="store_true",
        help="Forward the benchmark through xvfb-run -a on Linux/headless systems.",
    )
    parser.add_argument(
        "--force-compile",
        action="store_true",
        help="Rebuild the gold map _cache even when manifest.json already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print planned work without copying or running.",
    )
    parser.add_argument(
        "--history-limit",
        type=int,
        default=1,
        help="Number of previous local runs to include in the text summary.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = _build_plan(args)
    except BenchmarkConfigurationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    _print_plan(plan)
    if args.dry_run:
        return 0

    try:
        _prepare_orchestration_log(plan)
        _ensure_local_map_copy(plan)
        _ensure_cache(plan)
        _prepare_scenario(plan)
        benchmark_returncode = _run_benchmark(plan)
        if benchmark_returncode != 0:
            return benchmark_returncode

        summary = load_json_file(plan["summary_path"])
        previous_records = _history_records(plan["history_path"])
        previous_record = _latest_compatible_record(previous_records, summary)
        previous_summary = _summary_from_record(previous_record)
        thresholds = BenchmarkThresholds.load(plan["thresholds_path"])
        comparison = (
            compare_summaries(previous_summary, summary, thresholds)
            if previous_summary is not None
            else None
        )
        comparison_path = _write_comparison(plan["comparison_path"], comparison)
        record = _record_for_run(
            plan,
            summary=summary,
            comparison=comparison,
            comparison_path=comparison_path,
        )
        _append_history(plan["history_path"], record)
        text_summary = _human_summary(
            plan,
            summary=summary,
            previous_record=previous_record,
            previous_records=previous_records,
            comparison=comparison,
            comparison_path=comparison_path,
            thresholds=thresholds,
        )
        plan["latest_summary_path"].parent.mkdir(parents=True, exist_ok=True)
        plan["latest_summary_path"].write_text(text_summary, encoding="utf-8")
        _append_orchestration_log(plan, text_summary)
        print(text_summary, end="")
        return 0 if comparison is None or comparison["passed"] else 1
    except BenchmarkConfigurationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _build_plan(args: argparse.Namespace) -> dict[str, Any]:
    source_map_dir = Path(args.source_map_dir).expanduser().resolve()
    local_map_dir = Path(args.local_map_dir).expanduser().resolve()
    results_dir = Path(args.results_dir).expanduser().resolve()
    scenario_template_path = _existing_file(args.scenario, "scenario")
    threshold_path = _existing_file(args.thresholds, "thresholds")
    base_scenario_template = BenchmarkScenario.load(scenario_template_path)
    scenario_template = _scenario_with_benchmark_policy(
        base_scenario_template,
        render_distance=int(args.render_distance),
        measurement_seconds=float(args.measurement_seconds),
        max_runtime_seconds=(
            None
            if args.max_runtime_seconds is None
            else float(args.max_runtime_seconds)
        ),
    )
    BenchmarkThresholds.load(threshold_path)

    source_copy_needed = not _has_map_source(local_map_dir)
    if source_copy_needed:
        _validate_source_map_dir(source_map_dir)
        if local_map_dir.exists():
            raise BenchmarkConfigurationError(
                "local-map-dir exists but does not contain a supported map "
                f"source file: {local_map_dir}"
            )

    cache_dir = local_map_dir / CACHE_DIRNAME
    manifest_path = cache_dir / MANIFEST_NAME
    compile_needed = bool(args.force_compile or not manifest_path.is_file())
    label = _safe_label(args.label or _default_label())
    run_dir = results_dir / "runs" / label
    route_mode = str(args.route_mode)
    generated_scenario_paths = {
        "auto-centerline": run_dir / "auto-centerline-route-v1.json",
        "auto-dense": run_dir / "auto-dense-route-v1.json",
        "static": run_dir / "static-route-v1.json",
    }
    scenario_path = generated_scenario_paths.get(route_mode, scenario_template_path)
    scenario_fingerprint = (
        "<generated after cache validation>"
        if route_mode != "static" and route_mode in generated_scenario_paths
        else scenario_template.fingerprint
    )

    compile_command = [
        sys.executable,
        "-m",
        "caveviewer.chunker",
        "--source",
        str(local_map_dir),
    ]
    if args.force_compile:
        compile_command.append("--force")

    benchmark_command = [
        sys.executable,
        str(_REPOSITORY_ROOT / "scripts" / "benchmark" / "run_local_benchmark.py"),
        "--cache-dir",
        str(cache_dir),
        "--textures-dir",
        str(cache_dir),
        "--scenario",
        str(scenario_path),
        "--thresholds",
        str(threshold_path),
        "--output-root",
        str(results_dir / "runs"),
        "--label",
        label,
        "--log-level",
        args.log_level,
        "--vsync",
        args.vsync,
    ]
    if args.xvfb:
        benchmark_command.append("--xvfb")

    return {
        "source_map_dir": source_map_dir,
        "local_map_dir": local_map_dir,
        "cache_dir": cache_dir,
        "manifest_path": manifest_path,
        "results_dir": results_dir,
        "history_path": results_dir / "history.jsonl",
        "latest_summary_path": results_dir / "latest-summary.txt",
        "scenario_path": scenario_path,
        "scenario_template_path": scenario_template_path,
        "scenario_template": scenario_template,
        "scenario_fingerprint": scenario_fingerprint,
        "render_distance": scenario_template.render_distance,
        "measurement_seconds": scenario_template.measurement_seconds,
        "max_runtime_seconds": scenario_template.max_runtime_seconds,
        "texture_resident_cache_mb": float(args.texture_resident_cache_mb),
        "route_mode": route_mode,
        "centerline_route_keyframes": max(1, int(args.centerline_route_keyframes)),
        "centerline_route_target_length_m": (
            None
            if args.centerline_route_target_length_m is None
            else float(args.centerline_route_target_length_m)
        ),
        "centerline_route_target_length_source": (
            "devils_eye_streaming_default_chunks"
            if args.centerline_route_target_length_m is None
            else "cli_meters"
        ),
        "centerline_route_y_search_radius_cells": max(
            0,
            int(args.centerline_route_y_search_radius_cells),
        ),
        "dense_route_keyframes": max(1, int(args.dense_route_keyframes)),
        "dense_route_percentile": float(args.dense_route_percentile),
        "thresholds_path": threshold_path,
        "label": label,
        "run_dir": run_dir,
        "summary_path": run_dir / "summary.json",
        "comparison_path": run_dir / "comparison.json",
        "orchestration_log_path": run_dir / "orchestration.log",
        "source_copy_needed": source_copy_needed,
        "compile_needed": compile_needed,
        "force_compile": bool(args.force_compile),
        "history_limit": max(0, int(args.history_limit)),
        "compile_command": compile_command,
        "benchmark_command": benchmark_command,
    }


def _print_plan(plan: Mapping[str, Any]) -> None:
    print(_plan_text(plan), end="")


def _plan_text(plan: Mapping[str, Any]) -> str:
    lines = [
        "Devil's Eye XL local benchmark plan:",
        f"  source_map_dir: {plan['source_map_dir']}",
        f"  local_map_dir: {plan['local_map_dir']}",
        f"  cache_dir: {plan['cache_dir']}",
        f"  route_mode: {plan['route_mode']}",
        f"  render_distance: {plan['render_distance']}",
        f"  measurement_seconds: {plan['measurement_seconds']:g}",
        f"  max_runtime_seconds: {plan['max_runtime_seconds']:g}",
        f"  texture_resident_cache_mb: {plan['texture_resident_cache_mb']:g}",
        f"  scenario_template: {plan['scenario_template_path']}",
        f"  scenario: {plan['scenario_path']}",
        f"  scenario_fingerprint: {plan['scenario_fingerprint']}",
        *_route_generation_plan_lines(plan),
        f"  thresholds: {plan['thresholds_path']}",
        f"  history: {plan['history_path']}",
        f"  output_dir: {plan['run_dir']}",
        f"  orchestration_log: {plan['orchestration_log_path']}",
        f"  copy_map: {_yes_no(plan['source_copy_needed'])}",
        f"  compile_cache: {_yes_no(plan['compile_needed'])}",
    ]
    if plan["compile_needed"]:
        lines.extend(
            [
                "  compile_command:",
                f"    {_format_command(plan['compile_command'])}",
            ]
        )
    lines.extend(
        [
            "  benchmark_command:",
            f"    {_format_command(plan['benchmark_command'])}",
        ]
    )
    return "\n".join(lines) + "\n"


def _route_generation_plan_lines(plan: Mapping[str, Any]) -> list[str]:
    if plan["route_mode"] == "auto-centerline":
        target_length = plan["centerline_route_target_length_m"]
        target_text = (
            (
                "auto("
                f"{DEFAULT_DEVILS_EYE_CENTERLINE_ROUTE_TARGET_CHUNKS:g} chunks, "
                "streaming exercise; meters computed after cache validation)"
            )
            if target_length is None
            else f"{target_length:g}m"
        )
        return [
            "  centerline_route: "
            f"keyframes={plan['centerline_route_keyframes']} "
            f"target_length={target_text} "
            "selection=max_visible_chunk_texture_complexity "
            "movement=after_warmup "
            f"vertical_fraction={CENTERLINE_ROUTE_VERTICAL_POSITION_FRACTION:g} "
            f"y_search_radius_cells={plan['centerline_route_y_search_radius_cells']}",
            "  dense_route: disabled",
        ]
    if plan["route_mode"] == "auto-dense":
        return [
            "  centerline_route: disabled",
            "  dense_route: "
            f"keyframes={plan['dense_route_keyframes']} "
            f"percentile={plan['dense_route_percentile']:g}",
        ]
    return [
        "  centerline_route: disabled",
        "  dense_route: disabled",
    ]


def _scenario_with_benchmark_policy(
    scenario: BenchmarkScenario,
    *,
    render_distance: int,
    measurement_seconds: float,
    max_runtime_seconds: float | None,
) -> BenchmarkScenario:
    measurement = float(measurement_seconds)
    if measurement <= 0.0:
        raise BenchmarkConfigurationError("measurement-seconds must be positive")
    max_runtime = (
        float(max_runtime_seconds)
        if max_runtime_seconds is not None
        else (
            float(scenario.warmup_seconds)
            + measurement
            + DEFAULT_DEVILS_EYE_MAX_RUNTIME_MARGIN_SECONDS
        )
    )
    if max_runtime < scenario.warmup_seconds + measurement:
        raise BenchmarkConfigurationError(
            "max-runtime-seconds must be at least warmup + measurement"
        )

    payload = scenario.identity_payload
    payload["metadata"] = dict(scenario.metadata)
    payload["render_distance"] = max(1, int(render_distance))
    payload["measurement_seconds"] = measurement
    payload["max_runtime_seconds"] = max_runtime
    return BenchmarkScenario.from_mapping(payload)


def _prepare_orchestration_log(plan: Mapping[str, Any]) -> None:
    Path(plan["run_dir"]).mkdir(parents=True, exist_ok=True)
    log_path = Path(plan["orchestration_log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(_plan_text(plan) + "\n", encoding="utf-8")


def _ensure_local_map_copy(plan: Mapping[str, Any]) -> None:
    if not plan["source_copy_needed"]:
        return
    source_map_dir = plan["source_map_dir"]
    local_map_dir = plan["local_map_dir"]
    assert isinstance(source_map_dir, Path)
    assert isinstance(local_map_dir, Path)
    _log_message(
        plan,
        f"Copying gold map into ignored local benchmark data: {local_map_dir}",
    )
    local_map_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_map_dir, local_map_dir, symlinks=True)


def _ensure_cache(plan: Mapping[str, Any]) -> None:
    manifest_path = plan["manifest_path"]
    assert isinstance(manifest_path, Path)
    if manifest_path.is_file() and not plan["force_compile"]:
        return
    _log_message(plan, "Compiling Devil's Eye XL map cache with caveviewer.chunker.")
    returncode = _run_logged_subprocess(
        plan["compile_command"],
        plan,
        cwd=_REPOSITORY_ROOT,
        env=_subprocess_env_for_local_cache(),
    )
    if returncode != 0:
        raise BenchmarkConfigurationError(
            f"chunker failed with exit code {returncode}"
        )
    if not manifest_path.is_file():
        raise BenchmarkConfigurationError(
            f"chunker completed but did not create {manifest_path}"
        )


def _prepare_scenario(plan: dict[str, Any]) -> None:
    if plan["route_mode"] == "static":
        scenario_path = Path(plan["scenario_path"])
        scenario_path.parent.mkdir(parents=True, exist_ok=True)
        scenario = plan["scenario_template"]
        assert isinstance(scenario, BenchmarkScenario)
        payload = scenario.identity_payload
        payload["metadata"] = dict(scenario.metadata)
        scenario_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return
    manifest = load_json_file(plan["manifest_path"])
    if plan["route_mode"] == "auto-centerline":
        target_length_m = plan["centerline_route_target_length_m"]
        if target_length_m is None:
            target_length_m = _devils_eye_centerline_route_target_length_m(
                manifest
            )
        route_result = generate_centerline_route_scenario(
            manifest,
            plan["scenario_template"],
            keyframe_count=int(plan["centerline_route_keyframes"]),
            target_length_m=target_length_m,
            y_search_radius_cells=int(
                plan["centerline_route_y_search_radius_cells"]
            ),
        )
        if plan["centerline_route_target_length_source"] != "cli_meters":
            _mark_devils_eye_default_centerline_metadata(
                route_result.scenario_payload,
            )
        summary_text = _centerline_route_summary
        plan_key = "centerline_route"
    else:
        route_result = generate_dense_chunk_route_scenario(
            manifest,
            plan["scenario_template"],
            dense_percentile=float(plan["dense_route_percentile"]),
            keyframe_count=int(plan["dense_route_keyframes"]),
        )
        summary_text = _dense_route_summary
        plan_key = "dense_route"
    scenario_path = Path(plan["scenario_path"])
    scenario_path.parent.mkdir(parents=True, exist_ok=True)
    scenario_path.write_text(
        json.dumps(route_result.scenario_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    scenario = BenchmarkScenario.from_mapping(route_result.scenario_payload)
    plan["scenario_fingerprint"] = scenario.fingerprint
    plan[plan_key] = route_result
    _log_message(plan, summary_text(route_result, scenario_path))


def _devils_eye_centerline_route_target_length_m(
    manifest: Mapping[str, Any],
) -> float:
    return (
        _manifest_chunk_size_m(manifest)
        * DEFAULT_DEVILS_EYE_CENTERLINE_ROUTE_TARGET_CHUNKS
    )


def _manifest_chunk_size_m(manifest: Mapping[str, Any]) -> float:
    try:
        chunk_size = float(manifest["chunk_size"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BenchmarkConfigurationError(
            "manifest chunk_size must be a positive number"
        ) from exc
    if chunk_size <= 0.0:
        raise BenchmarkConfigurationError(
            "manifest chunk_size must be a positive number"
        )
    return chunk_size


def _mark_devils_eye_default_centerline_metadata(
    scenario_payload: dict[str, Any],
) -> None:
    metadata = scenario_payload.get("metadata", {})
    if not isinstance(metadata, dict):
        return
    metadata["target_route_length_source"] = (
        "devils_eye_streaming_default_chunks"
    )
    metadata["target_route_speed_source"] = (
        "devils_eye_streaming_default_chunks"
    )
    metadata["target_route_length_chunks"] = (
        DEFAULT_DEVILS_EYE_CENTERLINE_ROUTE_TARGET_CHUNKS
    )
    metadata["target_route_minimum_length_chunks"] = (
        DEFAULT_DEVILS_EYE_CENTERLINE_ROUTE_TARGET_CHUNKS
    )
    metadata["streaming_exercise_definition"] = (
        "route length is expressed in chunk widths so the measured window "
        "crosses streaming neighborhoods and exercises runtime chunk uploads"
    )


def _dense_route_summary(dense_route, scenario_path: Path) -> str:
    metadata = dense_route.scenario_payload["metadata"]
    return (
        "Generated dense chunk route: "
        f"scenario={scenario_path} "
        f"route_length_m={metadata['route_length_m']:.1f} "
        f"keyframes={metadata['route_keyframe_count']} "
        f"path_cells={metadata['path_cell_count']} "
        f"dense_component_size={metadata['dense_component_size']} "
        f"max_neighborhood_chunks={metadata['max_neighborhood_chunks']} "
        f"mean_route_neighborhood_chunks={metadata['mean_route_neighborhood_chunks']:.1f}"
    )


def _centerline_route_summary(centerline_route, scenario_path: Path) -> str:
    metadata = centerline_route.scenario_payload["metadata"]
    return (
        "Generated centerline route: "
        f"scenario={scenario_path} "
        f"route_length_m={metadata['route_length_m']:.1f} "
        f"target_route_length_m={metadata['target_route_length_m']:.1f} "
        f"target_speed_m_per_s={metadata['target_route_speed_m_per_second']:.2f} "
        f"actual_speed_m_per_s={metadata['actual_route_speed_m_per_second']:.2f} "
        f"travel={metadata['route_travel_start_s']:.1f}s.."
        f"{metadata['route_travel_start_s'] + metadata['route_travel_duration_s']:.1f}s "
        f"keyframes={metadata['route_keyframe_count']} "
        f"path_cells={metadata['path_cell_count']} "
        f"full_path_cells={metadata['full_path_cell_count']} "
        f"footprint_component_size={metadata['footprint_component_size']} "
        f"selection={metadata['route_selection_strategy']} "
        f"max_visible_chunks={metadata['max_route_visible_chunks']} "
        f"max_route_textures={metadata['max_route_unique_textures']} "
        f"route_source={metadata['route_source']} "
        f"y_strategy={metadata['y_strategy']} "
        f"vertical_fraction={metadata['vertical_position_fraction']:.2f} "
        f"route_y={metadata['min_route_y']:.1f}..{metadata['max_route_y']:.1f} "
        f"max_clearance_m={metadata['max_clearance_m']:.1f} "
        f"mean_route_clearance_m={metadata['mean_route_clearance_m']:.1f}"
    )


def _run_benchmark(plan: Mapping[str, Any]) -> int:
    _log_message(plan, "Running Devil's Eye XL FPS benchmark.")
    return _run_logged_subprocess(
        plan["benchmark_command"],
        plan,
        cwd=_REPOSITORY_ROOT,
        env=_subprocess_env_for_local_cache(plan),
    )


def _run_logged_subprocess(
    command: Sequence[object],
    plan: Mapping[str, Any],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> int:
    command_text = _format_command(command)
    _append_orchestration_log(plan, f"$ {command_text}\n")
    process = subprocess.Popen(
        [str(part) for part in command],
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    with open(plan["orchestration_log_path"], "a", encoding="utf-8") as log:
        for line in process.stdout:
            print(line, end="")
            log.write(line)
            log.flush()
    return int(process.wait())


def _validate_source_map_dir(source_map_dir: Path) -> None:
    if not source_map_dir.is_dir():
        raise BenchmarkConfigurationError(
            f"source-map-dir does not exist: {source_map_dir}"
        )
    if not _has_map_source(source_map_dir):
        raise BenchmarkConfigurationError(
            "source-map-dir does not contain a supported map source file "
            f"({', '.join(sorted(MAP_SOURCE_SUFFIXES))}): {source_map_dir}"
        )


def _has_map_source(map_dir: Path) -> bool:
    if not map_dir.is_dir():
        return False
    for child in map_dir.iterdir():
        if child.is_file() and child.suffix.lower() in MAP_SOURCE_SUFFIXES:
            return True
    return False


def _history_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.is_file():
        return records
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def _summary_from_record(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    summary = record.get("summary")
    if isinstance(summary, dict):
        return dict(summary)
    summary_path = record.get("summary_path")
    if not summary_path:
        return None
    path = Path(str(summary_path)).expanduser()
    if not path.is_file():
        return None
    return load_json_file(path)


def _write_comparison(
    path: Path,
    comparison: Mapping[str, Any] | None,
) -> Path | None:
    if comparison is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _append_history(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _record_for_run(
    plan: Mapping[str, Any],
    *,
    summary: Mapping[str, Any],
    comparison: Mapping[str, Any] | None,
    comparison_path: Path | None,
) -> dict[str, Any]:
    return {
        "schema_version": LOCAL_HISTORY_SCHEMA_VERSION,
        "timestamp_utc": _utc_timestamp(),
        "benchmark_id": LOCAL_BENCHMARK_ID,
        "label": plan["label"],
        "git_sha": _git_sha(),
        "local_map_dir": str(plan["local_map_dir"]),
        "cache_dir": str(plan["cache_dir"]),
        "summary_path": str(plan["summary_path"]),
        "comparison_path": None if comparison_path is None else str(comparison_path),
        "comparison_passed": None if comparison is None else bool(comparison["passed"]),
        "summary": _compact_summary(summary),
    }


def _compact_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": summary.get("schema_version"),
        "scenario": dict(summary.get("scenario", {})),
        "reason": summary.get("reason"),
        "measured_frames": summary.get("measured_frames"),
        "metrics": dict(summary.get("metrics", {})),
        "environment": dict(summary.get("environment", {})),
    }


def _human_summary(
    plan: Mapping[str, Any],
    *,
    summary: Mapping[str, Any],
    previous_record: Mapping[str, Any] | None,
    previous_records: Sequence[Mapping[str, Any]],
    comparison: Mapping[str, Any] | None,
    comparison_path: Path | None,
    thresholds: BenchmarkThresholds,
) -> str:
    metrics = summary.get("metrics", {})
    environment = summary.get("environment", {})
    lines = [
        "CaveViewer Devil's Eye XL local benchmark",
        f"Status: {_status_text(comparison)}",
        f"Run: {plan['label']}",
        f"Git SHA: {_git_sha()}",
        (
            "Current: "
            f"wall_clock_fps={_format_metric(metrics.get('wall_clock_fps'))}, "
            f"median_render_fps={_format_metric(metrics.get('median_fps'))}, "
            f"one_percent_low_render_fps={_format_metric(metrics.get('one_percent_low_fps'))}, "
            f"p95_frame_ms={_format_metric(metrics.get('p95_frame_ms'))}"
        ),
        (
            "Runtime load: "
            f"median_drawn_chunks={_format_metric(metrics.get('median_drawn_chunks'))}, "
            f"max_drawn_chunks={_format_metric(metrics.get('max_drawn_chunks'))}, "
            f"median_wanted_chunks={_format_metric(metrics.get('median_wanted_chunks'))}, "
            f"max_pending_chunks={_format_metric(metrics.get('max_pending_chunks'))}, "
            f"max_ready_chunks={_format_metric(metrics.get('max_ready_chunks'))}, "
            f"chunks_uploaded={_format_metric(metrics.get('total_chunks_uploaded'))}, "
            f"chunks_unloaded={_format_metric(metrics.get('total_chunks_unloaded'))}, "
            f"bytes_uploaded={_format_metric(metrics.get('total_bytes_uploaded'))}, "
            f"upload_stalls={_format_metric(metrics.get('total_upload_stalls'))}, "
            f"frames_with_pending={_format_metric(metrics.get('frames_with_pending_chunks'))}, "
            f"frames_with_uploads={_format_metric(metrics.get('frames_with_chunk_uploads'))}"
        ),
        (
            "Runtime texture: "
            f"bytes_uploaded={_format_bytes_mb(metrics.get('total_texture_bytes_uploaded'))}, "
            f"upload_ms={_format_metric(metrics.get('total_texture_upload_ms'))}, "
            f"decode_ms={_format_metric(metrics.get('total_texture_decode_ms'))}, "
            f"evictions={_format_metric(metrics.get('total_texture_evictions'))}, "
            f"evicted={_format_bytes_mb(metrics.get('total_texture_evicted_bytes'))}, "
            f"decoded_cache_hits={_format_metric(metrics.get('total_texture_decoded_cache_hits'))}, "
            f"file_cache_hits={_format_metric(metrics.get('total_texture_file_cache_hits'))}, "
            f"sync_decodes={_format_metric(metrics.get('total_texture_sync_decodes'))}, "
            f"placeholders={_format_metric(metrics.get('total_texture_placeholders'))}, "
            f"frames_with_texture_uploads={_format_metric(metrics.get('frames_with_texture_uploads'))}"
        ),
        (
            "Display: "
            f"window={_format_size(environment.get('actual_window_size'))}, "
            f"framebuffer={_format_size(environment.get('actual_framebuffer_size'))}, "
            f"ui_surface={_format_size(environment.get('actual_ui_surface_size'))}, "
            f"backend={environment.get('window_backend') or '<missing>'}"
        ),
        *_startup_readiness_summary_lines(environment),
        *_streaming_summary_lines(environment),
    ]
    route_line = _route_summary_for_summary(summary)
    if route_line:
        lines.append(route_line)
    if previous_record is None:
        if previous_records:
            lines.append(
                "Gate baseline: none compatible; this run seeds a new comparable "
                "local benchmark history."
            )
        else:
            lines.append("Gate baseline: none; this run seeds the local benchmark history.")
    else:
        previous_summary = _summary_from_record(previous_record) or {}
        previous_metrics = previous_summary.get("metrics", {})
        lines.append(
            "Gate baseline: "
            f"{previous_record.get('label', '<unknown>')} "
            f"wall_clock_fps={_format_metric(previous_metrics.get('wall_clock_fps'))} "
            f"median_render_fps={_format_metric(previous_metrics.get('median_fps'))}"
        )
    if comparison is not None:
        lines.append("Comparison checks:")
        for check in comparison.get("checks", []):
            lines.append(f"  - {_format_check(check)}")
    lines.extend(_history_comparison_lines(
        previous_records,
        current_summary=summary,
        thresholds=thresholds,
        limit=int(plan["history_limit"]),
    ))
    lines.extend(
        [
            f"Artifacts: {plan['run_dir']}",
            f"Summary JSON: {plan['summary_path']}",
            f"Frame log: {Path(plan['run_dir']) / 'frames.jsonl'}",
            f"Benchmark log: {Path(plan['run_dir']) / 'benchmark.log'}",
            f"Orchestration log: {plan['orchestration_log_path']}",
            f"History: {plan['history_path']}",
            f"Latest text summary: {plan['latest_summary_path']}",
        ]
    )
    if comparison_path is not None:
        lines.append(f"Comparison JSON: {comparison_path}")
    return "\n".join(lines) + "\n"


def _startup_readiness_summary_lines(environment: Mapping[str, Any]) -> list[str]:
    if "initial_visual_ready_seconds" not in environment:
        return []
    return [
        (
            "Startup readiness: "
            f"visual_ready_after={_format_setting(environment.get('initial_visual_ready_seconds'), suffix=' s')}, "
            f"settle_frames={_format_setting(environment.get('initial_visual_ready_frames'))}, "
            f"visible_chunks={_format_setting(environment.get('initial_visual_ready_visible_chunks'))}, "
            "textures="
            f"{_format_setting(environment.get('initial_visual_ready_resident_textures'))}/"
            f"{_format_setting(environment.get('initial_visual_ready_required_textures'))} resident, "
            f"visible_textures={_format_setting(environment.get('initial_visual_ready_visible_textures'))}"
        )
    ]


def _streaming_summary_lines(environment: Mapping[str, Any]) -> list[str]:
    settings = environment.get("streaming_settings")
    if not isinstance(settings, Mapping):
        settings = {}
    has_effective_values = any(
        key in environment
        for key in (
            "streaming_chunk_size_m",
            "streaming_max_loaded_chunks",
            "streaming_ready_backlog_capacity",
            "texture_max_dimension",
        )
    )
    if not settings and not has_effective_values:
        return []

    return [
        (
            "Streaming request: "
            f"distance={_format_setting(settings.get('render_distance_chunks'))} chunks, "
            f"RAM target={_format_percent_setting(settings.get('system_ram_target_percent'))}, "
            f"GPU target={_format_percent_setting(settings.get('gpu_memory_target_percent'))}, "
            f"GPU override={_format_setting(settings.get('gpu_memory_override_gb'), suffix=' GB')}, "
            f"workers={_format_setting(settings.get('io_workers'))}, "
            f"reserved_cpus={_format_setting(settings.get('io_reserved_cpus'))}, "
            "upload="
            f"{_format_setting(settings.get('upload_chunks_per_frame'))} chunks/frame, "
            f"{_format_setting(settings.get('upload_groups_per_frame'))} groups/chunk, "
            f"{_format_setting(settings.get('upload_time_budget_ms'))} ms/frame"
        ),
        (
            "Streaming effective: "
            f"distance={_format_setting(environment.get('effective_render_distance_chunks'))} chunks, "
            f"chunk_size={_format_setting(environment.get('streaming_chunk_size_m'), suffix=' m')}, "
            f"max_loaded_chunks={_format_setting(environment.get('streaming_max_loaded_chunks'))}, "
            f"ready_backlog={_format_setting(environment.get('streaming_ready_backlog_capacity'))}, "
            f"worker_target={_format_setting(environment.get('streaming_worker_target'))}, "
            f"startup_upload={_format_upload_policy(environment, 'startup')}, "
            f"catchup_upload={_format_upload_policy(environment, 'catchup')}"
        ),
        (
            "Texture residency: "
            f"max_dimension={_format_setting(environment.get('texture_max_dimension'), suffix=' px')}, "
            f"requested_cache_cap={_format_setting(settings.get('texture_resident_cache_mb'), suffix=' MB')}, "
            f"resident_budget={_format_bytes_mb(environment.get('texture_resident_budget_bytes'))}, "
            f"decoded_cache={_format_bytes_mb(environment.get('texture_decoded_cache_budget_bytes'))}"
        ),
    ]


def _format_upload_policy(
    environment: Mapping[str, Any],
    prefix: str,
) -> str:
    return (
        f"{_format_setting(environment.get(f'{prefix}_upload_chunks_per_frame'))} "
        "chunks/frame, "
        f"{_format_setting(environment.get(f'{prefix}_upload_groups_per_frame'))} "
        "groups/chunk, "
        f"{_format_setting(environment.get(f'{prefix}_upload_time_budget_ms'))} ms/frame"
    )


def _history_comparison_lines(
    previous_records: Sequence[Mapping[str, Any]],
    *,
    current_summary: Mapping[str, Any],
    thresholds: BenchmarkThresholds,
    limit: int,
) -> list[str]:
    if not previous_records:
        return ["Previous local runs: none"]
    if limit <= 0:
        return []

    if limit == 1:
        lines = ["Previous local run compared to current:"]
    else:
        lines = [f"Previous local runs compared to current (most recent {limit}):"]
    for record in reversed(previous_records[-limit:]):
        previous_summary = _summary_from_record(record)
        if previous_summary is None:
            lines.append(
                f"  - {record.get('label', '<unknown>')}: metrics unavailable"
            )
            continue
        gate = _comparison_status(previous_summary, current_summary, thresholds)
        lines.extend(
            [
                f"  - Run: {record.get('label', '<unknown>')}",
                f"    Gate: {gate}{_compatibility_note(previous_summary, current_summary)}",
                "    FPS (higher is better, current vs previous):",
                _metric_change_line(
                    "wall clock FPS",
                    "wall_clock_fps",
                    current_summary=current_summary,
                    previous_summary=previous_summary,
                    unit="fps",
                ),
                _metric_change_line(
                    "median render FPS",
                    "median_fps",
                    current_summary=current_summary,
                    previous_summary=previous_summary,
                    unit="fps",
                ),
                _metric_change_line(
                    "1% low render FPS",
                    "one_percent_low_fps",
                    current_summary=current_summary,
                    previous_summary=previous_summary,
                    unit="fps",
                ),
                "    Frame time (lower is better, current vs previous):",
                _metric_change_line(
                    "p95 frame time",
                    "p95_frame_ms",
                    current_summary=current_summary,
                    previous_summary=previous_summary,
                    unit="ms",
                ),
            ]
        )
    return lines


def _metric_change_line(
    label: str,
    metric_name: str,
    *,
    current_summary: Mapping[str, Any],
    previous_summary: Mapping[str, Any],
    unit: str,
) -> str:
    current_value = _metric_value(current_summary, metric_name)
    previous_value = _metric_value(previous_summary, metric_name)
    if current_value is None or previous_value is None:
        return (
            f"      {label}: current={_format_metric_with_unit(current_value, unit)}, "
            f"previous={_format_metric_with_unit(previous_value, unit)} "
            "(delta unavailable)"
        )
    return (
        f"      {label}: current={_format_metric_with_unit(current_value, unit)}, "
        f"previous={_format_metric_with_unit(previous_value, unit)}, "
        f"delta={_percent_delta_text(previous_value, current_value)}"
    )


def _format_metric_with_unit(value: float | None, unit: str) -> str:
    if value is None:
        return "<missing>"
    return f"{value:.2f} {unit}"


def _metric_value(summary: Mapping[str, Any], metric_name: str) -> float | None:
    value = summary.get("metrics", {}).get(metric_name)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _percent_delta_text(baseline: float, candidate: float) -> str:
    if baseline == 0:
        delta = 0.0 if candidate == 0 else 100.0
    else:
        delta = ((candidate - baseline) / abs(baseline)) * 100.0
    return f"{delta:+.2f}%"


def _compatibility_note(
    previous_summary: Mapping[str, Any],
    current_summary: Mapping[str, Any],
) -> str:
    reasons = _incompatibility_reasons(previous_summary, current_summary)
    if not reasons:
        return ""
    return f" ({'; '.join(reasons)}; not used as gate baseline)"


def _incompatibility_reasons(
    previous_summary: Mapping[str, Any],
    current_summary: Mapping[str, Any],
) -> list[str]:
    previous_scenario = previous_summary.get("scenario", {})
    current_scenario = current_summary.get("scenario", {})
    previous_environment = previous_summary.get("environment", {})
    current_environment = current_summary.get("environment", {})

    reasons: list[str] = []
    previous_name = previous_scenario.get("name")
    current_name = current_scenario.get("name")
    if not previous_name or not current_name:
        reasons.append("scenario name missing")
    elif previous_name != current_name:
        reasons.append("scenario name changed")

    previous_fingerprint = previous_scenario.get("fingerprint")
    current_fingerprint = current_scenario.get("fingerprint")
    if not previous_fingerprint or not current_fingerprint:
        reasons.append("scenario fingerprint missing")
    elif previous_fingerprint != current_fingerprint:
        reasons.append("route changed")

    previous_map = previous_environment.get("cache_manifest_sha256")
    current_map = current_environment.get("cache_manifest_sha256")
    if not previous_map or not current_map:
        reasons.append("map manifest hash missing")
    elif previous_map != current_map:
        reasons.append("map manifest changed")

    for key, reason in (
        ("actual_window_size", "window size changed"),
        ("actual_framebuffer_size", "framebuffer size changed"),
        ("streaming_settings_fingerprint", "streaming settings changed"),
    ):
        previous_value = previous_environment.get(key)
        current_value = current_environment.get(key)
        if previous_value is not None or current_value is not None:
            if previous_value != current_value:
                reasons.append(reason)

    return reasons


def _comparison_status(
    previous_summary: Mapping[str, Any],
    current_summary: Mapping[str, Any],
    thresholds: BenchmarkThresholds,
) -> str:
    if not _summaries_are_comparable(previous_summary, current_summary):
        return "INCOMPATIBLE"
    return (
        "PASS"
        if compare_summaries(previous_summary, current_summary, thresholds)["passed"]
        else "FAIL"
    )


def _latest_compatible_record(
    records: Sequence[Mapping[str, Any]],
    current_summary: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    for record in reversed(records):
        previous_summary = _summary_from_record(record)
        if previous_summary is not None and _summaries_are_comparable(
            previous_summary,
            current_summary,
        ):
            return record
    return None


def _summaries_are_comparable(
    previous_summary: Mapping[str, Any],
    current_summary: Mapping[str, Any],
) -> bool:
    previous_scenario = previous_summary.get("scenario", {})
    current_scenario = current_summary.get("scenario", {})
    previous_environment = previous_summary.get("environment", {})
    current_environment = current_summary.get("environment", {})
    comparable = (
        bool(previous_scenario.get("name"))
        and previous_scenario.get("name") == current_scenario.get("name")
        and bool(previous_scenario.get("fingerprint"))
        and previous_scenario.get("fingerprint") == current_scenario.get("fingerprint")
        and bool(previous_environment.get("cache_manifest_sha256"))
        and previous_environment.get("cache_manifest_sha256")
        == current_environment.get("cache_manifest_sha256")
    )
    if not comparable:
        return False
    for key in ("actual_window_size", "actual_framebuffer_size"):
        previous_value = previous_environment.get(key)
        current_value = current_environment.get(key)
        if (
            (previous_value is not None or current_value is not None)
            and previous_value != current_value
        ):
            return False
    previous_streaming = previous_environment.get("streaming_settings_fingerprint")
    current_streaming = current_environment.get("streaming_settings_fingerprint")
    if (
        (previous_streaming is not None or current_streaming is not None)
        and previous_streaming != current_streaming
    ):
        return False
    return True


def _route_summary_for_summary(summary: Mapping[str, Any]) -> str | None:
    metadata = summary.get("scenario", {}).get("metadata", {})
    if not isinstance(metadata, Mapping):
        return None
    route_mode = metadata.get("route_mode")
    if route_mode == "auto_centerline_v1":
        return (
            "Route: auto_centerline "
            f"length_m={_format_metric(metadata.get('route_length_m'))}, "
            f"target_length_m={_format_metric(metadata.get('target_route_length_m'))}, "
            f"target_speed_m_per_s={_format_metric(metadata.get('target_route_speed_m_per_second'))}, "
            f"actual_speed_m_per_s={_format_metric(metadata.get('actual_route_speed_m_per_second'))}, "
            f"travel_start_s={_format_metric(metadata.get('route_travel_start_s'))}, "
            f"travel_duration_s={_format_metric(metadata.get('route_travel_duration_s'))}, "
            f"keyframes={metadata.get('route_keyframe_count', '<missing>')}, "
            f"path_cells={metadata.get('path_cell_count', '<missing>')}, "
            f"selection={metadata.get('route_selection_strategy', '<missing>')}, "
            f"max_visible_chunks={metadata.get('max_route_visible_chunks', '<missing>')}, "
            f"mean_visible_chunks={_format_metric(metadata.get('mean_route_visible_chunks'))}, "
            f"max_route_textures={metadata.get('max_route_unique_textures', '<missing>')}, "
            f"mean_route_textures={_format_metric(metadata.get('mean_route_unique_textures'))}, "
            f"route_source={metadata.get('route_source', '<missing>')}, "
            f"y_strategy={metadata.get('y_strategy', '<missing>')}, "
            f"vertical_fraction={_format_metric(metadata.get('vertical_position_fraction'))}, "
            f"route_y={_format_metric(metadata.get('min_route_y'))}..{_format_metric(metadata.get('max_route_y'))}, "
            f"max_clearance_m={_format_metric(metadata.get('max_clearance_m'))}, "
            f"mean_route_clearance_m={_format_metric(metadata.get('mean_route_clearance_m'))}"
        )
    if route_mode == "auto_dense_chunks_v1":
        return (
            "Route: auto_dense_chunks "
            f"length_m={_format_metric(metadata.get('route_length_m'))}, "
            f"keyframes={metadata.get('route_keyframe_count', '<missing>')}, "
            f"path_cells={metadata.get('path_cell_count', '<missing>')}, "
            f"max_neighborhood_chunks={metadata.get('max_neighborhood_chunks', '<missing>')}, "
            f"mean_route_neighborhood_chunks={_format_metric(metadata.get('mean_route_neighborhood_chunks'))}"
        )
    return None


def _status_text(comparison: Mapping[str, Any] | None) -> str:
    if comparison is None:
        return "BASELINE RECORDED"
    return "PASS" if comparison["passed"] else "FAIL"


def _format_check(check: Mapping[str, Any]) -> str:
    status = "PASS" if check.get("passed") else "FAIL"
    metric = check.get("metric", "<unknown>")
    baseline = _format_metric(check.get("baseline"))
    candidate = _format_metric(check.get("candidate"))
    delta = _format_metric(check.get("delta_pct"))
    if check.get("skipped"):
        return (
            f"{status} {metric}: skipped "
            f"(baseline={baseline}, candidate={candidate}; {check.get('reason')})"
        )
    if check.get("kind") == "compatibility":
        return f"{status} {metric}: baseline={baseline}, candidate={candidate}"
    if "allowed_drop_pct" in check:
        allowed = _format_metric(check.get("allowed_drop_pct"))
        return (
            f"{status} {metric}: baseline={baseline}, candidate={candidate}, "
            f"delta={delta}%, allowed_drop={allowed}%"
        )
    allowed = _format_metric(check.get("allowed_increase_pct"))
    return (
        f"{status} {metric}: baseline={baseline}, candidate={candidate}, "
        f"delta={delta}%, allowed_increase={allowed}%"
    )


def _existing_file(path: str | os.PathLike[str], label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise BenchmarkConfigurationError(f"{label} does not exist: {resolved}")
    return resolved


def _default_label() -> str:
    return f"{_utc_timestamp().replace(':', '').replace('-', '')}-{_git_short_sha()}"


def _git_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        value = completed.stdout.strip()
        if value:
            return value
    return "unknown"


def _git_short_sha() -> str:
    sha = _git_sha()
    return sha[:12] if sha != "unknown" else "unknown"


def _safe_label(label: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in str(label).strip()
    ).strip(".-")
    if not cleaned:
        raise BenchmarkConfigurationError(
            "label must contain at least one safe character"
        )
    return cleaned


def _subprocess_env_for_local_cache(
    plan: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = _pythonpath_with_src(env)
    env.pop("CAVEVIEWER_MAP_CACHE_DIR", None)
    if plan is not None:
        texture_cache_mb = float(plan["texture_resident_cache_mb"])
        if texture_cache_mb > 0.0:
            env["CAVEVIEWER_TEXTURE_RESIDENT_CACHE_MB"] = (
                f"{texture_cache_mb:g}"
            )
    return env


def _log_message(plan: Mapping[str, Any], message: str) -> None:
    print(message)
    _append_orchestration_log(plan, message + "\n")


def _append_orchestration_log(plan: Mapping[str, Any], message: str) -> None:
    log_path = Path(plan["orchestration_log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(message)


def _pythonpath_with_src(env: Mapping[str, str]) -> str:
    src_path = str(_SOURCE_ROOT)
    existing = env.get("PYTHONPATH", "")
    return src_path if not existing else os.pathsep.join((src_path, existing))


def _format_command(command: Sequence[object]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def _format_metric(value: object) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    return str(value if value is not None else "<missing>")


def _format_setting(value: object, *, suffix: str = "") -> str:
    if value is None or value == "":
        return "<missing>"
    if isinstance(value, bool):
        text = str(value)
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        text = str(int(value)) if value.is_integer() else f"{value:.2f}"
    else:
        text = str(value)
    if text == "<missing>" or not suffix:
        return text
    return f"{text}{suffix}"


def _format_percent_setting(value: object) -> str:
    if value is None or value == "":
        return "<missing>"
    text = str(value).strip()
    if not text:
        return "<missing>"
    return text if text.endswith("%") else f"{text}%"


def _format_bytes_mb(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "<missing>"
    return f"{float(value) / (1024 ** 2):.1f} MB"


def _format_size(value: object) -> str:
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) == 2
    ):
        try:
            width = int(value[0])
            height = int(value[1])
        except (TypeError, ValueError):
            return "<missing>"
        return f"{width}x{height}"
    return "<missing>"


def _utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


if __name__ == "__main__":
    sys.exit(main())
