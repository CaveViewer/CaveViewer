"""Architecture guardrails for benchmark package boundaries."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARKING_ROOT = REPO_ROOT / "src" / "caveviewer" / "benchmarking"
FORBIDDEN_MODULES = (
    "caveviewer.gui",
    "moderngl",
    "moderngl_window",
    "tkinter",
    "glfw",
)


@dataclass(frozen=True)
class Violation:
    path: Path
    lineno: int
    detail: str


def test_benchmarking_modules_do_not_import_gui_or_rendering_layers():
    violations: list[Violation] = []

    for path in sorted(BENCHMARKING_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_forbidden_module(alias.name):
                        violations.append(
                            Violation(path, node.lineno, f"imports {alias.name}")
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if _is_forbidden_module(module):
                    violations.append(
                        Violation(path, node.lineno, f"imports from {module}")
                    )

    assert not violations, _format_violations(violations)


def test_map_runner_does_not_depend_on_script_entry_points():
    runner_path = BENCHMARKING_ROOT / "map_runner.py"
    runner_source = runner_path.read_text(encoding="utf-8")

    assert "scripts/benchmark" not in runner_source
    assert "run_local_benchmark.py" not in runner_source


def _is_forbidden_module(module: str) -> bool:
    return any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for forbidden in FORBIDDEN_MODULES
    )


def _format_violations(violations: list[Violation]) -> str:
    return "\n".join(
        f"{violation.path.relative_to(REPO_ROOT)}:{violation.lineno}: "
        f"{violation.detail}"
        for violation in violations
    )
