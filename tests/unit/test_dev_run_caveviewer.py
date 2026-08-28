"""Tests for the IDE dependency bootstrap and CaveViewer launch ordering."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "dev" / "run_caveviewer.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("run_caveviewer", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ensure_runtime_dependencies_uses_selected_interpreter(monkeypatch):
    script = _load_script()
    run = Mock()
    monkeypatch.setattr(script.subprocess, "run", run)

    script.ensure_runtime_dependencies(PROJECT_ROOT)

    run.assert_called_once_with(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--requirement",
            str(PROJECT_ROOT / "requirements.txt"),
        ],
        check=True,
    )


def test_require_supported_python_rejects_wrong_series(monkeypatch):
    script = _load_script()
    monkeypatch.setattr(script.sys, "version_info", (3, 14, 7))
    monkeypatch.setattr(script.sys, "executable", "/example/python")

    with pytest.raises(
        RuntimeError,
        match=(
            r"CaveViewer requires Python 3\.12, but the selected interpreter "
            r"is Python 3\.14\.7: /example/python"
        ),
    ):
        script.require_supported_python()


def test_main_installs_before_launching_caveviewer(monkeypatch):
    script = _load_script()
    events = []
    monkeypatch.setattr(
        script,
        "require_supported_python",
        lambda: events.append(("validate",)),
    )
    monkeypatch.setattr(
        script,
        "ensure_runtime_dependencies",
        lambda project_root: events.append(("install", project_root)),
    )
    monkeypatch.setattr(
        script.runpy,
        "run_module",
        lambda *args, **kwargs: events.append(("run", args, kwargs)),
    )

    script.main()

    assert events == [
        ("validate",),
        ("install", PROJECT_ROOT),
        ("run", ("caveviewer",), {"run_name": "__main__", "alter_sys": True}),
    ]
