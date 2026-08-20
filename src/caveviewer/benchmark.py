"""Command-line entry point for automated CaveViewer FPS benchmarks.

The benchmark runner intentionally opens an existing chunk cache instead of
building/importing a source model. That keeps frame-rate comparisons focused
on viewer streaming/rendering behavior and lets CI reuse a separately managed
gold-standard map artifact.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
from typing import Sequence

from caveviewer.app import _route_moderngl_window_logging
from caveviewer.core.diagnostics.logging import configure_logging, get_logger
from caveviewer.core.preferences.runtime_settings import (
    RuntimeSettings,
    current_runtime_platform_facts,
    resolve_runtime_settings,
)
from caveviewer.benchmarking.results import (
    BenchmarkConfigurationError,
    BenchmarkScenario,
)
from caveviewer.gui.preferences import load_saved_preference_values
from caveviewer.version import APP_NAME, APP_VERSION


_LOG = get_logger("Benchmark")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run CaveViewer's deterministic viewer FPS benchmark against a "
            "precompiled chunk cache."
        )
    )
    parser.add_argument(
        "--cache-dir",
        required=True,
        help="Precompiled chunk-cache directory containing manifest.json.",
    )
    parser.add_argument(
        "--textures-dir",
        help="Texture root for the cache. Defaults to --cache-dir.",
    )
    parser.add_argument(
        "--scenario",
        required=True,
        help="Benchmark scenario JSON file.",
    )
    parser.add_argument(
        "--output-dir",
        default="benchmark-results",
        help="Directory for summary.json, frames.jsonl, environment.json, and logs.",
    )
    parser.add_argument(
        "--log-file",
        help="File log path. Defaults to <output-dir>/benchmark.log.",
    )
    parser.add_argument(
        "--log-level",
        help="Console/file logging level. Overrides CAVEVIEWER_LOG_LEVEL.",
    )
    parser.add_argument(
        "--vsync",
        choices=("off", "on", "unchanged"),
        default="off",
        help="Benchmark vsync policy. Defaults to off for regression sensitivity.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the final summary JSON to stdout after the viewer exits.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_dir = Path(args.output_dir)
    log_file = Path(args.log_file) if args.log_file else output_dir / "benchmark.log"
    runtime_settings = _resolve_benchmark_runtime_settings(args)
    _configure_benchmark_logging(str(runtime_settings["log_level"]), log_file)
    _route_moderngl_window_logging()

    try:
        scenario = BenchmarkScenario.load(args.scenario)
        cache_dir = Path(args.cache_dir)
        textures_dir = Path(args.textures_dir) if args.textures_dir else cache_dir
        _LOG.info("=" * 60)
        _LOG.info("  %s %s benchmark", APP_NAME, APP_VERSION)
        _LOG.info("=" * 60)
        _LOG.info("Scenario: %s", args.scenario)
        _LOG.info("Cache directory: %s", cache_dir)
        _LOG.info("Textures directory: %s", textures_dir)
        _LOG.info("Output directory: %s", output_dir)
        _LOG.info("Log file: %s", log_file)

        from caveviewer.gui.viewer_window import run_viewer_benchmark

        summary_path = Path(
            run_viewer_benchmark(
                str(cache_dir),
                str(textures_dir),
                scenario,
                str(output_dir),
                runtime_settings=runtime_settings,
            )
        )
        if not summary_path.exists():
            raise BenchmarkConfigurationError(
                f"benchmark ended without writing expected summary: {summary_path}"
            )
        if args.json:
            print(summary_path.read_text(encoding="utf-8").rstrip())
        return 0
    except BenchmarkConfigurationError as exc:
        _LOG.error("Benchmark configuration failed: %s", exc)
        return 2
    except KeyboardInterrupt:
        _LOG.info("Benchmark interrupted by user.")
        return 130
    except Exception:
        _LOG.exception("Benchmark failed.")
        return 1


def _configure_benchmark_logging(level: str, log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    configure_logging(level, force=True)
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "[%(component)s] %(levelname)s: %(message)s",
            defaults={"component": "caveviewer"},
        )
    )
    logging.getLogger().addHandler(file_handler)


def _resolve_benchmark_runtime_settings(args) -> RuntimeSettings:
    """Compose the benchmark's immutable settings without mutating ``environ``."""

    cli_overrides: dict[str, object] = {}
    if args.log_level:
        cli_overrides["log_level"] = args.log_level
    if args.vsync != "unchanged":
        cli_overrides["vsync"] = args.vsync == "on"
    runtime_settings = resolve_runtime_settings(
        preferences=load_saved_preference_values(),
        environ=os.environ,
        cli_overrides=cli_overrides,
        platform=current_runtime_platform_facts(),
    )
    _LOG.info("Resolved saved CaveViewer preferences for benchmark runtime settings.")
    return runtime_settings


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
