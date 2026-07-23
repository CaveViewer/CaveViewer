#!/usr/bin/env python3
"""Compare CaveViewer benchmark summary JSON files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from caveviewer.gui.benchmark import (
    BenchmarkThresholds,
    compare_summaries,
    load_json_file,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare candidate CaveViewer benchmark results with a baseline.",
    )
    parser.add_argument("--baseline", required=True, help="Baseline summary.json path.")
    parser.add_argument("--candidate", required=True, help="Candidate summary.json path.")
    parser.add_argument("--output", required=True, help="Comparison JSON output path.")
    parser.add_argument(
        "--thresholds",
        help="Optional threshold config JSON. Defaults to built-in thresholds.",
    )
    parser.add_argument("--max-median-fps-drop-pct", type=float)
    parser.add_argument("--max-one-percent-low-fps-drop-pct", type=float)
    parser.add_argument("--max-p95-frame-ms-increase-pct", type=float)
    parser.add_argument("--max-stutter-frame-increase-pct", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    thresholds = (
        BenchmarkThresholds.load(args.thresholds)
        if args.thresholds
        else BenchmarkThresholds()
    ).with_overrides(
        max_median_fps_drop_pct=args.max_median_fps_drop_pct,
        max_one_percent_low_fps_drop_pct=args.max_one_percent_low_fps_drop_pct,
        max_p95_frame_ms_increase_pct=args.max_p95_frame_ms_increase_pct,
        max_stutter_frame_increase_pct=args.max_stutter_frame_increase_pct,
    )
    comparison = compare_summaries(
        load_json_file(args.baseline),
        load_json_file(args.candidate),
        thresholds,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(comparison, indent=2, sort_keys=True))
    return 0 if comparison["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
