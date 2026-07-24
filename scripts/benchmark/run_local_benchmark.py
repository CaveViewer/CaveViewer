#!/usr/bin/env python3
"""Run a local CaveViewer benchmark and optionally compare with a baseline.

This wrapper is for pre-PR validation from a source checkout. It deliberately
does not switch git branches. To compare `main` with a candidate stack, run it
from two separate worktrees/checkouts and pass the baseline `summary.json` to
the candidate run.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Sequence

_SCRIPT_PATH = Path(__file__).resolve()
_REPOSITORY_ROOT = _SCRIPT_PATH.parents[2]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from caveviewer.core.chunking import builder as chunker
from caveviewer.gui.benchmark import (
    BenchmarkConfigurationError,
    BenchmarkScenario,
    BenchmarkThresholds,
)


def _repo_root() -> Path:
    return _REPOSITORY_ROOT


def _parser() -> argparse.ArgumentParser:
    root = _repo_root()
    parser = argparse.ArgumentParser(
        description=(
            "Run CaveViewer's benchmark from this checkout and optionally "
            "compare it with a baseline summary."
        )
    )
    parser.add_argument(
        "--cache-dir",
        required=True,
        help="Precompiled benchmark map cache containing manifest.json.",
    )
    parser.add_argument(
        "--textures-dir",
        help="Texture root for the cache. Defaults to --cache-dir.",
    )
    parser.add_argument(
        "--scenario",
        default=str(root / "benchmarks" / "viewer-benchmark-scenario.v1.json"),
        help="Benchmark scenario JSON.",
    )
    parser.add_argument(
        "--thresholds",
        default=str(root / "benchmarks" / "viewer-thresholds.v1.json"),
        help="Benchmark threshold config JSON.",
    )
    parser.add_argument(
        "--baseline-summary",
        help="Optional baseline summary.json to compare against.",
    )
    parser.add_argument(
        "--output-root",
        default=str(root / "benchmark-results" / "local"),
        help="Root directory for local benchmark artifacts.",
    )
    parser.add_argument(
        "--label",
        help="Run label used as the artifact subdirectory name.",
    )
    parser.add_argument(
        "--log-level",
        default="DEBUG",
        help="Benchmark log level. Defaults to DEBUG for pre-PR diagnostics.",
    )
    parser.add_argument(
        "--vsync",
        choices=("off", "on", "unchanged"),
        default="off",
        help="Forwarded caveviewer-benchmark vsync policy.",
    )
    parser.add_argument(
        "--xvfb",
        action="store_true",
        help="Prefix the benchmark with xvfb-run -a on Linux/headless systems.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print commands without launching the viewer.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = _build_plan(args)
    except BenchmarkConfigurationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print("Benchmark validation plan:")
    print(f"  cache_dir: {plan['cache_dir']}")
    print(f"  textures_dir: {plan['textures_dir']}")
    print(f"  scenario: {plan['scenario_path']}")
    print(f"  scenario_fingerprint: {plan['scenario_fingerprint']}")
    print(f"  thresholds: {plan['threshold_path']}")
    print(f"  output_dir: {plan['output_dir']}")
    if plan["baseline_summary"]:
        print(f"  baseline_summary: {plan['baseline_summary']}")
    print("  benchmark_command:")
    print(f"    {_format_command(plan['benchmark_command'])}")
    if plan["compare_command"]:
        print("  compare_command:")
        print(f"    {_format_command(plan['compare_command'])}")

    if args.dry_run:
        return 0

    output_dir = Path(plan["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = _pythonpath_with_src(env)
    benchmark_result = subprocess.run(
        plan["benchmark_command"],
        cwd=_repo_root(),
        env=env,
        check=False,
    )
    if benchmark_result.returncode != 0:
        return benchmark_result.returncode

    if plan["compare_command"] is None:
        return 0
    compare_result = subprocess.run(
        plan["compare_command"],
        cwd=_repo_root(),
        env=env,
        check=False,
    )
    return compare_result.returncode


def _build_plan(args) -> dict[str, object]:
    root = _repo_root()
    cache_dir = _existing_dir(args.cache_dir, "cache-dir")
    textures_dir = _existing_dir(args.textures_dir or args.cache_dir, "textures-dir")
    manifest_path = cache_dir / chunker.MANIFEST_NAME
    if not manifest_path.is_file():
        raise BenchmarkConfigurationError(
            f"cache-dir must contain {chunker.MANIFEST_NAME}: {cache_dir}"
        )

    scenario_path = _existing_file(args.scenario, "scenario")
    scenario = BenchmarkScenario.load(scenario_path)
    threshold_path = _existing_file(args.thresholds, "thresholds")
    BenchmarkThresholds.load(threshold_path)

    baseline_summary = None
    if args.baseline_summary:
        baseline_summary = _existing_file(args.baseline_summary, "baseline-summary")
        _validate_summary_json(baseline_summary)

    label = _safe_label(args.label or _default_label(root))
    output_dir = Path(args.output_root).expanduser().resolve() / label
    summary_path = output_dir / "summary.json"
    benchmark_command = [
        sys.executable,
        "-m",
        "caveviewer.benchmark",
        "--cache-dir",
        str(cache_dir),
        "--textures-dir",
        str(textures_dir),
        "--scenario",
        str(scenario_path),
        "--output-dir",
        str(output_dir),
        "--log-file",
        str(output_dir / "benchmark.log"),
        "--log-level",
        args.log_level,
        "--vsync",
        args.vsync,
    ]
    if args.xvfb:
        benchmark_command = ["xvfb-run", "-a", *benchmark_command]

    compare_command = None
    if baseline_summary is not None:
        compare_command = [
            sys.executable,
            str(root / "scripts" / "benchmark" / "compare_benchmark_results.py"),
            "--baseline",
            str(baseline_summary),
            "--candidate",
            str(summary_path),
            "--output",
            str(output_dir / "comparison.json"),
            "--thresholds",
            str(threshold_path),
        ]

    return {
        "cache_dir": cache_dir,
        "textures_dir": textures_dir,
        "scenario_path": scenario_path,
        "scenario_fingerprint": scenario.fingerprint,
        "threshold_path": threshold_path,
        "baseline_summary": baseline_summary,
        "output_dir": output_dir,
        "benchmark_command": benchmark_command,
        "compare_command": compare_command,
    }


def _existing_dir(path: str, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise BenchmarkConfigurationError(f"{label} does not exist: {resolved}")
    return resolved


def _existing_file(path: str | os.PathLike[str], label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise BenchmarkConfigurationError(f"{label} does not exist: {resolved}")
    return resolved


def _validate_summary_json(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkConfigurationError(
            f"baseline-summary is not readable benchmark JSON: {path}"
        ) from exc
    if not isinstance(payload, dict) or "metrics" not in payload:
        raise BenchmarkConfigurationError(
            f"baseline-summary does not look like benchmark summary JSON: {path}"
        )


def _default_label(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        candidate = completed.stdout.strip()
        if candidate:
            return candidate
    return "current"


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


def _pythonpath_with_src(env: dict[str, str]) -> str:
    src_path = str(_repo_root() / "src")
    existing = env.get("PYTHONPATH", "")
    return src_path if not existing else os.pathsep.join((src_path, existing))


def _format_command(command: Sequence[object]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


if __name__ == "__main__":
    sys.exit(main())
