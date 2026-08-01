"""Tests for the all-map isotropic cubic graph diagnostic runner."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = (
    REPOSITORY_ROOT / "scripts" / "dev" / "navigation_cubic_graph_suite.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "navigation_cubic_graph_suite_for_tests",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discovers_map_caches_recursively_in_stable_order(tmp_path):
    module = _load_script_module()
    (tmp_path / "alpha cave" / "_cache").mkdir(parents=True)
    (tmp_path / "collection" / "Zulu" / "_cache").mkdir(parents=True)
    (tmp_path / "north" / "Repeated" / "_cache").mkdir(parents=True)
    (tmp_path / "south" / "Repeated" / "_cache").mkdir(parents=True)
    (tmp_path / "alpha cave" / "_cache" / "nested" / "_cache").mkdir(
        parents=True
    )
    (tmp_path / "not-a-map").mkdir()

    discovered = module._discover_map_caches(tmp_path)

    assert tuple(name for name, _cache_dir in discovered) == (
        "alpha cave",
        str(Path("collection") / "Zulu"),
        str(Path("north") / "Repeated"),
        str(Path("south") / "Repeated"),
    )


def test_classifies_pass_missing_resolution_and_real_failure():
    module = _load_script_module()

    assert module._classify_diagnostic({"passed": True}) == "PASSED"
    assert module._classify_diagnostic(
        {"passed": False, "reason": "navigation_atlas_missing"}
    ) == "MISSING_ARTIFACT"
    assert module._classify_diagnostic(
        {
            "passed": False,
            "reason": "cubic_graph_experiment_error",
            "error": (
                "coarse atlas contains tiles that differ from the requested "
                "resolution; use region mode or rebuild"
            ),
        }
    ) == "INCOMPATIBLE_RESOLUTION"
    assert module._classify_diagnostic(
        {"passed": False, "reason": "cubic_voxel_path_missing"}
    ) == "FAILED"


def test_suite_passes_only_when_every_discovered_map_passes(tmp_path):
    module = _load_script_module()
    passed = {"status": "PASSED"}
    missing = {"status": "MISSING_ARTIFACT"}

    complete = module._suite_payload(
        maps_root=tmp_path,
        voxel_size_m=1.0,
        minimum_clearance_m=0.25,
        allow_diagonal=False,
        duration_s=1.0,
        results=(passed, passed),
    )
    incomplete = module._suite_payload(
        maps_root=tmp_path,
        voxel_size_m=1.0,
        minimum_clearance_m=0.25,
        allow_diagonal=False,
        duration_s=1.0,
        results=(passed, missing),
    )

    assert complete["passed"] is True
    assert incomplete["passed"] is False
    assert incomplete["status_counts"] == {
        "PASSED": 1,
        "FAILED": 0,
        "INCOMPATIBLE_RESOLUTION": 0,
        "MISSING_ARTIFACT": 1,
    }


def test_main_runs_every_map_sequentially_and_prints_json(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_script_module()
    for name in ("First", "Second"):
        (tmp_path / name / "_cache").mkdir(parents=True)
    calls = []

    def fake_run_experiment(**kwargs):
        calls.append(kwargs["map_name"])
        return {
            "map_name": kwargs["map_name"],
            "cache_dir": str(kwargs["cache_dir"]),
            "status": "PASSED",
            "passed": True,
            "duration_s": 0.1,
            "process_return_code": 0,
            "reason": "",
            "diagnostic": {"passed": True},
        }

    monkeypatch.setattr(module, "_run_experiment", fake_run_experiment)

    exit_code = module.main(
        ["--maps-root", str(tmp_path), "--voxel-size", "1", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert calls == ["First", "Second"]
    assert payload["passed"] is True
    assert payload["voxel_size_m"] == 1.0
    assert payload["sequential"] is True
