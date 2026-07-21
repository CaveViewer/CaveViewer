"""Keep package metadata, scripts, and setup docs on Python 3.12."""

from __future__ import annotations

import tomllib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_package_metadata_supports_only_python_312():
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    assert pyproject["project"]["requires-python"] == ">=3.12,<3.13"


def test_development_build_and_release_scripts_validate_python_312():
    helper = _read("scripts/common/python.sh")
    install_script = _read("scripts/dev/install.sh")
    macos_build = _read("scripts/macos/build.sh")
    release_script = _read("scripts/release.sh")
    windows_setup = _read("scripts/windows/setup.ps1")

    assert 'CV_PYTHON_SERIES="3.12"' in helper
    assert "CAVEVIEWER_PYTHON" in helper
    assert "cv_resolve_project_python" in helper
    assert "cv_python_is_supported" in helper

    for script in (install_script, macos_build):
        assert 'source "$script_dir/../common/python.sh"' in script
        assert 'python_bin="$(cv_resolve_project_python)"' in script
        assert 'cv_python_is_supported "$venv_python"' in script

    assert 'cv_python_is_supported "$test_python"' in release_script
    assert "/python/3.12." in windows_setup
    assert "/python-3.12." in windows_setup


def test_developer_docs_require_python_312():
    developer_readme = _read("docs/development/source-setup.md")
    scripts_readme = _read("scripts/README.md")

    assert "- Python 3.12" in developer_readme
    assert "Python 3.10+" not in developer_readme
    assert "py -3.12 -m venv" in developer_readme
    assert "Python 3.12" in scripts_readme
