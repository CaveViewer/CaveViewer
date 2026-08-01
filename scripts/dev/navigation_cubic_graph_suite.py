#!/usr/bin/env python3
"""Run the isotropic cubic-graph diagnostic across every local map cache."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time


STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"
STATUS_INCOMPATIBLE = "INCOMPATIBLE_RESOLUTION"
STATUS_MISSING = "MISSING_ARTIFACT"

_MISSING_REASONS = frozenset(
    {
        "cache_manifest_missing",
        "navigation_atlas_missing",
        "navigation_route_missing",
        "navigation_route_points_missing",
        "mesh_collision_guard_missing",
    }
)
_RESOLUTION_ERROR_FRAGMENT = (
    "coarse atlas contains tiles that differ from the requested resolution"
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    maps_root = args.maps_root.expanduser().resolve()
    caches = _discover_map_caches(maps_root)
    if args.map_name:
        selected_names = {name.casefold() for name in args.map_name}
        caches = tuple(
            item for item in caches if item[0].casefold() in selected_names
        )
    if not caches:
        payload = {
            "passed": False,
            "reason": "map_caches_missing",
            "maps_root": str(maps_root),
            "map_count": 0,
            "results": [],
        }
        _print_suite(payload, json_output=bool(args.json))
        return 2

    experiment_script = Path(__file__).with_name(
        "navigation_cubic_graph_experiment.py"
    )
    results = []
    suite_started = time.perf_counter()
    for map_name, cache_dir in caches:
        result = _run_experiment(
            map_name=map_name,
            cache_dir=cache_dir,
            experiment_script=experiment_script,
            voxel_size_m=float(args.voxel_size),
            minimum_clearance_m=float(args.minimum_clearance),
            allow_diagonal=bool(args.allow_diagonal),
            max_expansions=int(args.max_expansions),
            max_mesh_replans=int(args.max_mesh_replans),
            timeout_s=float(args.timeout_s),
        )
        results.append(result)
        if not args.json:
            _print_map_result(result)
    payload = _suite_payload(
        maps_root=maps_root,
        voxel_size_m=float(args.voxel_size),
        minimum_clearance_m=float(args.minimum_clearance),
        allow_diagonal=bool(args.allow_diagonal),
        duration_s=time.perf_counter() - suite_started,
        results=results,
    )
    if args.json:
        _print_suite(payload, json_output=True)
    else:
        _print_human_summary(payload)
    return 0 if bool(payload["passed"]) else 1


def _discover_map_caches(maps_root: Path) -> tuple[tuple[str, Path], ...]:
    if not maps_root.is_dir():
        return ()
    discovered = []
    for directory, child_names, _file_names in os.walk(maps_root):
        child_names.sort(key=str.casefold)
        if "_cache" not in child_names:
            continue
        map_dir = Path(directory)
        cache_dir = map_dir / "_cache"
        relative_map = map_dir.relative_to(maps_root)
        map_name = (
            map_dir.name
            if relative_map == Path(".")
            else str(relative_map)
        )
        discovered.append((map_name, cache_dir.resolve()))
        child_names.remove("_cache")
    return tuple(sorted(discovered, key=lambda item: item[0].casefold()))


def _run_experiment(
    *,
    map_name: str,
    cache_dir: Path,
    experiment_script: Path,
    voxel_size_m: float,
    minimum_clearance_m: float,
    allow_diagonal: bool,
    max_expansions: int,
    max_mesh_replans: int,
    timeout_s: float,
) -> dict[str, object]:
    command = _experiment_command(
        cache_dir=cache_dir,
        experiment_script=experiment_script,
        voxel_size_m=voxel_size_m,
        minimum_clearance_m=minimum_clearance_m,
        allow_diagonal=allow_diagonal,
        max_expansions=max_expansions,
        max_mesh_replans=max_mesh_replans,
    )
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=max(1.0, float(timeout_s)),
        )
    except subprocess.TimeoutExpired as exc:
        diagnostic = {
            "passed": False,
            "reason": "map_test_timeout",
            "timeout_s": float(timeout_s),
            "stdout": _text_or_empty(exc.stdout),
            "stderr": _text_or_empty(exc.stderr),
        }
        return _map_result(
            map_name=map_name,
            cache_dir=cache_dir,
            duration_s=time.perf_counter() - started,
            return_code=None,
            diagnostic=diagnostic,
        )
    except OSError as exc:
        diagnostic = {
            "passed": False,
            "reason": "experiment_process_error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        return _map_result(
            map_name=map_name,
            cache_dir=cache_dir,
            duration_s=time.perf_counter() - started,
            return_code=None,
            diagnostic=diagnostic,
        )
    diagnostic = _parse_diagnostic(
        completed.stdout,
        stderr=completed.stderr,
        return_code=completed.returncode,
    )
    return _map_result(
        map_name=map_name,
        cache_dir=cache_dir,
        duration_s=time.perf_counter() - started,
        return_code=completed.returncode,
        diagnostic=diagnostic,
    )


def _experiment_command(
    *,
    cache_dir: Path,
    experiment_script: Path,
    voxel_size_m: float,
    minimum_clearance_m: float,
    allow_diagonal: bool,
    max_expansions: int,
    max_mesh_replans: int,
) -> list[str]:
    command = [
        sys.executable,
        str(experiment_script),
        "--cache-dir",
        str(cache_dir),
        "--mode",
        "atlas",
        "--voxel-size",
        str(float(voxel_size_m)),
        "--minimum-clearance",
        str(float(minimum_clearance_m)),
        "--max-expansions",
        str(max(1, int(max_expansions))),
        "--max-mesh-replans",
        str(max(0, int(max_mesh_replans))),
        "--json",
    ]
    if not allow_diagonal:
        command.append("--cardinal-only")
    return command


def _parse_diagnostic(
    stdout: str,
    *,
    stderr: str,
    return_code: int,
) -> dict[str, object]:
    try:
        payload = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return {
            "passed": False,
            "reason": "experiment_output_invalid",
            "return_code": int(return_code),
            "stdout": _text_or_empty(stdout),
            "stderr": _text_or_empty(stderr),
        }
    if not isinstance(payload, dict):
        return {
            "passed": False,
            "reason": "experiment_output_invalid",
            "return_code": int(return_code),
            "stdout": _text_or_empty(stdout),
            "stderr": _text_or_empty(stderr),
        }
    if stderr.strip():
        payload["process_stderr"] = stderr.strip()
    payload["process_return_code"] = int(return_code)
    return payload


def _map_result(
    *,
    map_name: str,
    cache_dir: Path,
    duration_s: float,
    return_code: int | None,
    diagnostic: dict[str, object],
) -> dict[str, object]:
    status = _classify_diagnostic(diagnostic)
    return {
        "map_name": str(map_name),
        "cache_dir": str(cache_dir),
        "status": status,
        "passed": status == STATUS_PASSED,
        "duration_s": float(duration_s),
        "process_return_code": return_code,
        "reason": str(diagnostic.get("reason", "")),
        "diagnostic": diagnostic,
    }


def _classify_diagnostic(diagnostic: dict[str, object]) -> str:
    if diagnostic.get("passed") is True:
        return STATUS_PASSED
    reason = str(diagnostic.get("reason", ""))
    if reason in _MISSING_REASONS:
        return STATUS_MISSING
    error = str(diagnostic.get("error", ""))
    if _RESOLUTION_ERROR_FRAGMENT in error:
        return STATUS_INCOMPATIBLE
    return STATUS_FAILED


def _suite_payload(
    *,
    maps_root: Path,
    voxel_size_m: float,
    minimum_clearance_m: float,
    allow_diagonal: bool,
    duration_s: float,
    results: Sequence[dict[str, object]],
) -> dict[str, object]:
    status_counts = {
        status: sum(result.get("status") == status for result in results)
        for status in (
            STATUS_PASSED,
            STATUS_FAILED,
            STATUS_INCOMPATIBLE,
            STATUS_MISSING,
        )
    }
    return {
        "passed": bool(results) and status_counts[STATUS_PASSED] == len(results),
        "reason": "" if status_counts[STATUS_PASSED] == len(results) else (
            "not_all_maps_proved_at_requested_resolution"
        ),
        "maps_root": str(maps_root),
        "voxel_size_m": float(voxel_size_m),
        "minimum_clearance_m": float(minimum_clearance_m),
        "cardinal_only": not bool(allow_diagonal),
        "sequential": True,
        "map_count": len(results),
        "status_counts": status_counts,
        "duration_s": float(duration_s),
        "results": list(results),
    }


def _print_map_result(result: dict[str, object]) -> None:
    diagnostic = result.get("diagnostic")
    details = diagnostic if isinstance(diagnostic, dict) else {}
    path = details.get("path")
    path_details = path if isinstance(path, dict) else {}
    route_m = path_details.get("distance_m")
    route_text = (
        ""
        if not isinstance(route_m, (int, float)) or not math.isfinite(route_m)
        else f" route={float(route_m):.1f}m"
    )
    reason = str(result.get("reason", ""))
    if result.get("status") == STATUS_INCOMPATIBLE:
        reason = "V10 coarse atlas is not uniformly 1 m"
    reason_text = f" reason={reason}" if reason else ""
    print(
        f"{result['status']:<23} {result['map_name']}"
        f"{route_text}{reason_text}",
        flush=True,
    )


def _print_human_summary(payload: dict[str, object]) -> None:
    counts = payload["status_counts"]
    print(
        "Summary: "
        f"{counts[STATUS_PASSED]} passed, "
        f"{counts[STATUS_FAILED]} failed, "
        f"{counts[STATUS_INCOMPATIBLE]} incompatible resolution, "
        f"{counts[STATUS_MISSING]} missing artifacts"
    )


def _print_suite(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("FAIL", payload.get("reason", ""))


def _text_or_empty(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the read-only isotropic cubic graph diagnostic sequentially "
            "for every map cache below one directory."
        )
    )
    parser.add_argument(
        "--maps-root",
        type=Path,
        default=Path.home() / "Downloads" / "Maps",
    )
    parser.add_argument(
        "--map-name",
        action="append",
        help="Test only this relative map path; may be repeated.",
    )
    parser.add_argument("--voxel-size", type=float, default=1.0)
    parser.add_argument("--minimum-clearance", type=float, default=0.25)
    parser.add_argument("--allow-diagonal", action="store_true")
    parser.add_argument("--max-expansions", type=int, default=2_000_000)
    parser.add_argument("--max-mesh-replans", type=int, default=64)
    parser.add_argument("--timeout-s", type=float, default=900.0)
    parser.add_argument("--json", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
