"""Enforce repository layout, packaging, resource, and test-documentation rules."""

import ast
from pathlib import Path

from caveviewer.resources import (
    image_path,
    map_library_catalog_path,
    release_public_key_path,
    shader_path,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPOSITORY_ROOT / "src" / "caveviewer"


def test_python_test_modules_have_descriptive_module_docstrings():
    undocumented = []

    for path in sorted((REPOSITORY_ROOT / "tests").rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not ast.get_docstring(module):
            undocumented.append(str(path.relative_to(REPOSITORY_ROOT)))

    assert not undocumented, f"test modules without docstrings: {undocumented}"


def test_application_uses_src_package_layout():
    expected_paths = (
        PACKAGE_ROOT / "__init__.py",
        PACKAGE_ROOT / "__main__.py",
        PACKAGE_ROOT / "app.py",
        PACKAGE_ROOT / "benchmark.py",
        PACKAGE_ROOT / "version.py",
        PACKAGE_ROOT / "benchmarking" / "__init__.py",
        PACKAGE_ROOT / "benchmarking" / "map_runner.py",
        PACKAGE_ROOT / "benchmarking" / "results.py",
        PACKAGE_ROOT / "benchmarking" / "routes.py",
        PACKAGE_ROOT / "core" / "chunking" / "buckets.py",
        PACKAGE_ROOT / "core" / "chunking" / "builder.py",
        PACKAGE_ROOT / "core" / "chunking" / "capacity.py",
        PACKAGE_ROOT / "core" / "chunking" / "io.py",
        PACKAGE_ROOT / "core" / "chunking" / "metadata.py",
        PACKAGE_ROOT / "core" / "chunking" / "staging.py",
        PACKAGE_ROOT / "core" / "chunking" / "upload.py",
        PACKAGE_ROOT / "core" / "diagnostics" / "logging.py",
        PACKAGE_ROOT / "core" / "mesh" / "obj.py",
        PACKAGE_ROOT / "core" / "mesh" / "glb.py",
        PACKAGE_ROOT / "core" / "navigation" / "__init__.py",
        PACKAGE_ROOT / "core" / "navigation" / "centerline.py",
        PACKAGE_ROOT / "core" / "navigation" / "route.py",
        PACKAGE_ROOT / "core" / "preferences" / "schema.py",
        PACKAGE_ROOT / "core" / "textures" / "decoding.py",
        PACKAGE_ROOT / "core" / "workers" / "allocation.py",
        PACKAGE_ROOT / "gui" / "preferences.py",
        PACKAGE_ROOT / "gui" / "preferences_form.py",
        PACKAGE_ROOT / "gui" / "preferences_dialog.py",
        PACKAGE_ROOT / "gui" / "benchmark.py",
        PACKAGE_ROOT / "gui" / "benchmark_routes.py",
        PACKAGE_ROOT / "gui" / "viewer_window.py",
        PACKAGE_ROOT / "resources" / "shaders" / "mesh.vert",
        PACKAGE_ROOT / "resources" / "images" / "app_mark_transparent.png",
        PACKAGE_ROOT / "resources" / "release_signing_public_key.pem",
        PACKAGE_ROOT / "resources" / "map_library_catalog.v1.json",
        REPOSITORY_ROOT / "benchmarks" / "viewer-benchmark-scenario.v1.json",
        REPOSITORY_ROOT / "benchmarks" / "viewer-thresholds.v1.json",
        REPOSITORY_ROOT
        / "scripts"
        / "benchmark"
        / "compare_benchmark_results.py",
        REPOSITORY_ROOT / "scripts" / "benchmark" / "run_local_benchmark.py",
        REPOSITORY_ROOT
        / "scripts"
        / "benchmark"
        / "hooks"
        / "pre-push-map-benchmark",
        REPOSITORY_ROOT / "packaging" / "pyinstaller" / "CaveViewer.spec",
    )

    missing = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in expected_paths
        if not path.exists()
    ]
    assert not missing, f"missing migrated paths: {missing}"


def test_package_resource_service_resolves_runtime_files():
    assert shader_path("mesh.vert").is_file()
    assert image_path("app_mark_transparent.png").is_file()
    assert release_public_key_path().is_file()
    assert map_library_catalog_path().is_file()


def test_legacy_runtime_paths_are_removed():
    legacy_paths = (
        "caveviewer.py",
        "caveviewer_version.py",
        "core",
        "gui",
        "shaders",
        "security",
        "CaveViewer.spec",
    )

    remaining = [path for path in legacy_paths if (REPOSITORY_ROOT / path).exists()]
    assert not remaining, f"legacy runtime paths remain: {remaining}"


def test_development_launchers_use_the_installed_package():
    install_script = (REPOSITORY_ROOT / "scripts" / "dev" / "install.sh").read_text(
        encoding="utf-8"
    )
    windows_setup = (
        REPOSITORY_ROOT / "scripts" / "windows" / "setup.ps1"
    ).read_text(encoding="utf-8")

    assert "pip install" in install_script
    assert "-e \"$project_root\"" in install_script
    assert 'python" -m caveviewer' in install_script
    assert "pip install" in windows_setup
    assert '-e `"$ProjectRoot`"' in windows_setup
    assert '"-m caveviewer"' in windows_setup


def test_packaging_consumers_reference_migrated_paths():
    source_packager = (
        REPOSITORY_ROOT / "scripts" / "common" / "package_source.sh"
    ).read_text(encoding="utf-8")
    linux_builder = (
        REPOSITORY_ROOT / "scripts" / "linux" / "common" / "build.sh"
    ).read_text(encoding="utf-8")
    macos_builder = (
        REPOSITORY_ROOT / "scripts" / "macos" / "build.sh"
    ).read_text(encoding="utf-8")
    windows_packager = (
        REPOSITORY_ROOT / "scripts" / "windows" / "package.sh"
    ).read_text(encoding="utf-8")
    pyinstaller_spec = (
        REPOSITORY_ROOT / "packaging" / "pyinstaller" / "CaveViewer.spec"
    ).read_text(encoding="utf-8")

    assert "\n  src " in source_packager
    assert "\n  benchmarks " in source_packager
    assert "\n  packaging " in source_packager
    assert 'packaging/pyinstaller/CaveViewer.spec' in linux_builder
    assert 'packaging/pyinstaller/CaveViewer.spec' in macos_builder
    assert "src/caveviewer/resources" in linux_builder
    assert 'src/caveviewer/__main__.py' in linux_builder
    assert '"src",' in windows_packager
    assert '"packaging",' in windows_packager
    assert "package_root / '__main__.py'" in pyinstaller_spec
    assert "caveviewer/resources/shaders" in pyinstaller_spec


def test_pyproject_declares_src_package_and_entry_point():
    pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    gitignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert '[project.scripts]' in pyproject
    assert 'caveviewer = "caveviewer.__main__:run"' in pyproject
    assert 'caveviewer-benchmark = "caveviewer.benchmark:run"' in pyproject
    assert (
        'caveviewer-map-benchmark = "caveviewer.benchmarking.map_runner:run"'
        in pyproject
    )
    assert 'caveviewer-chunker = "caveviewer.chunker:main"' in pyproject
    assert 'where = ["src"]' in pyproject
    assert 'pythonpath = ["src"]' in pyproject
    assert '"caveviewer.resources"' in pyproject
    assert "*.egg-info/" in gitignore
    assert ".benchmark-data/" in gitignore
