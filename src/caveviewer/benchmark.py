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
from caveviewer.benchmarking.results import (
    BenchmarkConfigurationError,
    BenchmarkScenario,
)
from caveviewer.gui.preferences import apply_preferences_to_env, load_preferences
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
        default=os.environ.get("CAVEVIEWER_LOG_LEVEL", "INFO") or "INFO",
        help="Console/file logging level. Defaults to CAVEVIEWER_LOG_LEVEL or INFO.",
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
    _configure_benchmark_logging(args.log_level, log_file)
    _route_moderngl_window_logging()
    _apply_saved_preferences_to_env()

    if args.vsync != "unchanged":
        os.environ["CAVEVIEWER_VSYNC"] = "1" if args.vsync == "on" else "0"

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


def _apply_saved_preferences_to_env() -> None:
    preferences = load_preferences()
    apply_preferences_to_env(preferences)
    _LOG.info("Applied saved CaveViewer preferences for benchmark runtime settings.")


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
