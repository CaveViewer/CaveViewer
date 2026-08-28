"""Tests for the IDE dependency bootstrap and CaveViewer launch ordering."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock, call

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


def test_ensure_uv_installs_pinned_version_when_missing(monkeypatch):
    script = _load_script()
    installed_uv = Path("/tools/uv")
    discoveries = iter((None, installed_uv))
    run = Mock()
    monkeypatch.setattr(script, "find_uv", lambda: next(discoveries))
    monkeypatch.setattr(script.subprocess, "run", run)

    assert script.ensure_uv() == installed_uv
    run.assert_called_once_with(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "uv==0.12.5",
        ],
        check=True,
    )


def test_ensure_managed_runtime_reuses_supported_environment(
    monkeypatch, tmp_path
):
    script = _load_script()
    interpreter = tmp_path / ".venv" / "bin" / "python"
    monkeypatch.setattr(script, "runtime_python", lambda project_root: interpreter)
    monkeypatch.setattr(script, "interpreter_is_supported", lambda candidate: True)
    ensure_uv = Mock()
    monkeypatch.setattr(script, "ensure_uv", ensure_uv)

    assert script.ensure_managed_runtime(tmp_path) == interpreter
    ensure_uv.assert_not_called()


def test_ensure_managed_runtime_creates_python_312_environment(
    monkeypatch, tmp_path
):
    script = _load_script()
    interpreter = tmp_path / ".venv" / "bin" / "python"
    uv = Path("/tools/uv")
    support_checks = iter((False, True))
    run = Mock()
    monkeypatch.setattr(script, "runtime_python", lambda project_root: interpreter)
    monkeypatch.setattr(
        script, "interpreter_is_supported", lambda candidate: next(support_checks)
    )
    monkeypatch.setattr(script, "ensure_uv", lambda: uv)
    monkeypatch.setattr(script.subprocess, "run", run)

    assert script.ensure_managed_runtime(tmp_path) == interpreter
    run.assert_called_once_with(
        [
            str(uv),
            "venv",
            "--python",
            "3.12",
            "--managed-python",
            "--seed",
            "--clear",
            str(tmp_path / ".venv"),
        ],
        check=True,
    )


def test_ensure_managed_runtime_rejects_failed_provisioning(monkeypatch, tmp_path):
    script = _load_script()
    interpreter = tmp_path / ".venv" / "bin" / "python"
    monkeypatch.setattr(script, "runtime_python", lambda project_root: interpreter)
    monkeypatch.setattr(script, "interpreter_is_supported", lambda candidate: False)
    monkeypatch.setattr(script, "ensure_uv", lambda: Path("/tools/uv"))
    monkeypatch.setattr(script.subprocess, "run", Mock())

    with pytest.raises(
        RuntimeError, match="uv did not create a usable Python 3.12 environment"
    ):
        script.ensure_managed_runtime(tmp_path)


def test_ensure_managed_python_installs_and_finds_python_312(monkeypatch):
    script = _load_script()
    uv = Path("/tools/uv")
    interpreter = Path("/managed/python")
    run = Mock(
        side_effect=(
            Mock(returncode=0),
            Mock(stdout=str(interpreter)),
        )
    )
    monkeypatch.setattr(script.subprocess, "run", run)
    monkeypatch.setattr(script, "interpreter_is_supported", lambda candidate: True)

    assert script.ensure_managed_python(uv) == interpreter
    assert run.call_args_list == [
        call([str(uv), "python", "install", "3.12"], check=True),
        call(
            [str(uv), "python", "find", "--managed-python", "3.12"],
            check=True,
            capture_output=True,
            text=True,
        ),
    ]


def test_main_installs_before_launching_caveviewer(monkeypatch):
    script = _load_script()
    events = []
    interpreter = PROJECT_ROOT / ".venv" / "bin" / "python"
    monkeypatch.setattr(
        script,
        "ensure_managed_runtime",
        lambda project_root: interpreter,
    )
    monkeypatch.setattr(script, "runtime_python", lambda project_root: interpreter)
    monkeypatch.setattr(script, "interpreter_is_supported", lambda candidate: True)
    monkeypatch.setattr(script, "running_in", lambda candidate: True)
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
        ("install", PROJECT_ROOT),
        ("run", ("caveviewer",), {"run_name": "__main__", "alter_sys": True}),
    ]


def test_main_relaunches_with_managed_runtime_before_installing(monkeypatch):
    script = _load_script()
    interpreter = PROJECT_ROOT / ".venv" / "bin" / "python"
    relaunch = Mock(side_effect=SystemExit)
    install = Mock()
    monkeypatch.setattr(
        script, "ensure_managed_runtime", lambda project_root: interpreter
    )
    monkeypatch.setattr(script, "runtime_python", lambda project_root: interpreter)
    monkeypatch.setattr(script, "interpreter_is_supported", lambda candidate: True)
    monkeypatch.setattr(script, "running_in", lambda candidate: False)
    monkeypatch.setattr(script, "ensure_runtime_dependencies", install)
    monkeypatch.setattr(script, "relaunch", relaunch)

    with pytest.raises(SystemExit):
        script.main()

    relaunch.assert_called_once_with(interpreter)
    install.assert_not_called()


def test_relaunch_removes_previous_virtual_environment_marker(monkeypatch):
    script = _load_script()
    interpreter = Path("/managed/python")
    execve = Mock(side_effect=SystemExit)
    monkeypatch.setenv("VIRTUAL_ENV", "/old/environment")
    monkeypatch.setattr(script.os, "execve", execve)

    with pytest.raises(SystemExit):
        script.relaunch(interpreter)

    executable, arguments, environment = execve.call_args.args
    assert executable == str(interpreter)
    assert arguments == [str(interpreter), str(SCRIPT_PATH), *sys.argv[1:]]
    assert "VIRTUAL_ENV" not in environment


def test_main_escapes_unsupported_active_project_environment(monkeypatch):
    script = _load_script()
    project_interpreter = PROJECT_ROOT / ".venv" / "bin" / "python"
    managed_interpreter = Path("/managed/python")
    relaunch = Mock(side_effect=SystemExit)
    monkeypatch.setattr(
        script, "runtime_python", lambda project_root: project_interpreter
    )
    monkeypatch.setattr(script, "running_in", lambda candidate: True)
    monkeypatch.setattr(script, "interpreter_is_supported", lambda candidate: False)
    monkeypatch.setattr(script, "ensure_uv", lambda: Path("/tools/uv"))
    monkeypatch.setattr(
        script, "ensure_managed_python", lambda uv: managed_interpreter
    )
    monkeypatch.setattr(script, "relaunch", relaunch)

    with pytest.raises(SystemExit):
        script.main()

    relaunch.assert_called_once_with(managed_interpreter)
