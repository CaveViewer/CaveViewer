"""Architecture guardrails for GUI package boundaries."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
GUI_ROOT = REPO_ROOT / "src" / "caveviewer" / "gui"
GUI_PLATFORM_ROOT = GUI_ROOT / "platform"
GUI_FEATURES_ROOT = GUI_ROOT / "features"
APP_MODULE = "caveviewer.app"


@dataclass(frozen=True)
class Violation:
    path: Path
    lineno: int
    detail: str


def _gui_python_files() -> list[Path]:
    return sorted(path for path in GUI_ROOT.rglob("*.py") if path.is_file())


def _parse_module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _format_violations(violations: list[Violation]) -> str:
    return "\n".join(
        f"{violation.path.relative_to(REPO_ROOT)}:{violation.lineno}: "
        f"{violation.detail}"
        for violation in violations
    )


def test_gui_modules_do_not_import_app_layer():
    violations: list[Violation] = []

    for path in _gui_python_files():
        for node in ast.walk(_parse_module(path)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == APP_MODULE or alias.name.startswith(
                        f"{APP_MODULE}."
                    ):
                        violations.append(
                            Violation(path, node.lineno, f"imports {alias.name}")
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imports_app_module = module == APP_MODULE or module.startswith(
                    f"{APP_MODULE}."
                )
                imports_app_from_package = module == "caveviewer" and any(
                    alias.name == "app" for alias in node.names
                )
                imports_app_relative = node.level >= 2 and (
                    module == "app"
                    or module.startswith("app.")
                    or any(alias.name == "app" for alias in node.names)
                )
                if (
                    imports_app_module
                    or imports_app_from_package
                    or imports_app_relative
                ):
                    violations.append(
                        Violation(path, node.lineno, "imports caveviewer.app")
                    )

    assert not violations, _format_violations(violations)


def test_platform_checks_stay_inside_gui_platform_adapters():
    violations: list[Violation] = []

    for path in _gui_python_files():
        if path.is_relative_to(GUI_PLATFORM_ROOT):
            continue

        tree = _parse_module(path)
        sys_aliases = set()
        os_aliases = set()
        platform_aliases = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_name = alias.name
                    local_name = alias.asname or imported_name.partition(".")[0]
                    if imported_name == "sys":
                        sys_aliases.add(local_name)
                    elif imported_name == "os":
                        os_aliases.add(local_name)
                    elif imported_name == "platform":
                        platform_aliases.add(local_name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    if module == "sys" and alias.name == "platform":
                        violations.append(
                            Violation(path, node.lineno, "imports sys.platform")
                        )
                    elif module == "os" and alias.name == "name":
                        violations.append(
                            Violation(path, node.lineno, "imports os.name")
                        )
                    elif module == "platform" and alias.name in {
                        "machine",
                        "system",
                    }:
                        violations.append(
                            Violation(
                                path,
                                node.lineno,
                                f"imports platform.{alias.name}",
                            )
                        )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if isinstance(node.value, ast.Name):
                if node.value.id in sys_aliases and node.attr == "platform":
                    violations.append(
                        Violation(path, node.lineno, "uses sys.platform")
                    )
                elif node.value.id in os_aliases and node.attr == "name":
                    violations.append(Violation(path, node.lineno, "uses os.name"))
                elif (
                    node.value.id in platform_aliases
                    and node.attr in {"machine", "system"}
                ):
                    violations.append(
                        Violation(
                            path,
                            node.lineno,
                            f"uses platform.{node.attr}",
                        )
                    )

    assert not violations, _format_violations(violations)


def test_feature_policies_do_not_import_platform_or_side_effect_modules():
    """Keep feature decisions as pure transforms of injected capability facts."""
    forbidden_modules = {"os", "platform", "subprocess", "sys", "tkinter"}
    violations: list[Violation] = []

    for path in sorted(GUI_FEATURES_ROOT.rglob("*.py")):
        for node in ast.walk(_parse_module(path)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_root = alias.name.partition(".")[0]
                    if module_root in forbidden_modules:
                        violations.append(
                            Violation(path, node.lineno, f"imports {alias.name}")
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "caveviewer.gui.platform" or module.startswith(
                    "caveviewer.gui.platform."
                ):
                    violations.append(
                        Violation(path, node.lineno, f"imports {module}")
                    )
                elif module.partition(".")[0] in forbidden_modules:
                    violations.append(
                        Violation(path, node.lineno, f"imports {module}"))

    assert not violations, _format_violations(violations)


def test_viewer_does_not_construct_platform_services_at_module_import():
    """Keep process-owned platform construction out of viewer module import."""
    viewer_module = _parse_module(GUI_ROOT / "viewer_window.py")
    factory_names = {"get_platform_adapter", "get_desktop_services"}
    violations: list[Violation] = []

    for node in viewer_module.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef)):
            continue
        for descendant in ast.walk(node):
            if (
                isinstance(descendant, ast.Call)
                and isinstance(descendant.func, ast.Name)
                and descendant.func.id in factory_names
            ):
                violations.append(
                    Violation(
                        GUI_ROOT / "viewer_window.py",
                        descendant.lineno,
                        f"constructs {descendant.func.id} during module import",
                    )
                )

    assert not violations, _format_violations(violations)


def test_gui_modules_have_ownership_docstrings():
    violations: list[Violation] = []

    for path in _gui_python_files():
        module = _parse_module(path)
        docstring = ast.get_docstring(module)
        if not docstring:
            violations.append(Violation(path, 1, "missing module docstring"))
            continue

        first_line = docstring.strip().splitlines()[0].strip()
        if first_line.startswith("caveviewer.gui."):
            violations.append(
                Violation(path, 1, f"placeholder module docstring: {first_line}")
            )

    assert not violations, _format_violations(violations)
