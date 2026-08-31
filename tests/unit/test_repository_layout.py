"""Enforce repository layout, packaging, resource, and test-documentation rules."""

import ast
from pathlib import Path

from caveviewer.resources import (
    cave_metadata_catalog_path,
    image_path,
    map_library_catalog_path,
    release_public_key_path,
    shader_path,
    ui_icon_path,
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
        PACKAGE_ROOT / "core" / "release_metadata.py",
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
        PACKAGE_ROOT / "gui" / "tk_scrolling.py",
        PACKAGE_ROOT / "gui" / "tk_typography.py",
        PACKAGE_ROOT / "gui" / "cave_metadata.py",
        PACKAGE_ROOT / "gui" / "cave_metadata_panel.py",
        PACKAGE_ROOT / "gui" / "viewer_window.py",
        PACKAGE_ROOT / "resources" / "shaders" / "mesh.vert",
        PACKAGE_ROOT / "resources" / "images" / "app_mark_transparent.png",
        PACKAGE_ROOT / "resources" / "release_signing_primary_public_key.pem",
        PACKAGE_ROOT / "resources" / "release_signing_recovery_public_key.pem",
        PACKAGE_ROOT / "resources" / "release_signing_legacy_public_key.pem",
        PACKAGE_ROOT / "resources" / "map_library_catalog.v1.json",
        PACKAGE_ROOT / "resources" / "cave_metadata_catalog.v1.json",
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


def test_branding_contract_is_documented_and_discoverable():
    branding_path = REPOSITORY_ROOT / "docs" / "development" / "branding.md"
    branding = branding_path.read_text(encoding="utf-8")
    development_index = (
        REPOSITORY_ROOT / "docs" / "development" / "README.md"
    ).read_text(encoding="utf-8")
    architecture = (
        REPOSITORY_ROOT / "docs" / "development" / "architecture.md"
    ).read_text(encoding="utf-8")
    repository_layout = (
        REPOSITORY_ROOT / "docs" / "development" / "repository-layout.md"
    ).read_text(encoding="utf-8")

    assert "## Consumer and output matrix" in branding
    assert "## Stable product identity" in branding
    for stable_identity in (
        "com.caveviewer.CaveViewer",
        "io.github.caveviewer.caveviewer",
        "StartupWMClass",
        "update-manifest paths",
        "application-data roots",
    ):
        assert stable_identity in branding
    assert "[Branding](branding.md)" in development_index
    assert "[Branding](branding.md)" in architecture
    assert "[branding.md](branding.md)" in repository_layout
    assert "## Developer workflow" in branding
    assert "caveviewer-branding --profile" in branding
    assert "CAVEVIEWER_BRAND_PROFILE" in branding
    assert "## Native verification and icon caches" in branding
    assert "[Branding](branding.md)" in (
        REPOSITORY_ROOT / "docs/development/source-setup.md"
    ).read_text(encoding="utf-8")
    assert "[branding profile workflow](branding.md)" in (
        REPOSITORY_ROOT / "docs/development/releases.md"
    ).read_text(encoding="utf-8")


def test_ux_guidelines_are_documented_without_owning_branding():
    ux_path = REPOSITORY_ROOT / "docs" / "development" / "ux-guidelines.md"
    ux_guidelines = ux_path.read_text(encoding="utf-8")
    development_index = (
        REPOSITORY_ROOT / "docs" / "development" / "README.md"
    ).read_text(encoding="utf-8")

    for heading in (
        "## Experience principles",
        "## Layout, density, and scaling",
        "## Dialogs and confirmations",
        "## Keyboard and accessibility",
        "## UX validation checklist",
    ):
        assert heading in ux_guidelines
    assert "Brand identity is deliberately out of scope." in ux_guidelines
    assert "[branding.md](branding.md)" in ux_guidelines
    assert "[UX guidelines](ux-guidelines.md)" in development_index


def test_package_resource_service_resolves_runtime_files():
    assert shader_path("mesh.vert").is_file()
    assert image_path("app_mark_transparent.png").is_file()
    for icon_name in (
        "chevron-right.svg",
        "download.svg",
        "folder-open.svg",
        "more-vertical.svg",
        "pause.svg",
        "play.svg",
        "retry.svg",
        "retry.png",
        "stop.svg",
    ):
        assert ui_icon_path(icon_name).is_file()
    assert release_public_key_path().is_file()
    assert release_public_key_path("recovery").is_file()
    assert release_public_key_path("legacy").is_file()
    assert map_library_catalog_path().is_file()
    assert cave_metadata_catalog_path().is_file()
    pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"images/ui/*.png"' in pyproject


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
    assert '"pip",' in windows_setup
    assert '"-e",' in windows_setup
    assert "$ProjectRoot" in windows_setup
    assert "$script:RuntimePython" in windows_setup
    assert '"-m caveviewer"' in windows_setup


def test_development_launcher_has_no_virtual_machine_special_cases():
    install_script = (REPOSITORY_ROOT / "scripts" / "dev" / "install.sh").read_text(
        encoding="utf-8"
    )

    forbidden_markers = (
        "is_virtual_machine",
        "systemd-detect-virt",
        "/sys/class/dmi",
        "Detected virtual machine",
    )
    assert not [marker for marker in forbidden_markers if marker in install_script]


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
    windows_builder = (
        REPOSITORY_ROOT / "scripts" / "windows" / "build.sh"
    ).read_text(encoding="utf-8")
    windows_installer = (
        REPOSITORY_ROOT / "packaging" / "windows" / "CaveViewerSetup.iss"
    ).read_text(encoding="utf-8")
    windows_metadata_writer = (
        REPOSITORY_ROOT / "scripts" / "windows" / "write_package_metadata.py"
    ).read_text(encoding="utf-8")
    windows_installer_smoke = (
        REPOSITORY_ROOT / "scripts" / "windows" / "smoke_installer.ps1"
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
    assert "CaveViewer.spec" in windows_builder
    assert "CaveViewer.exe" in windows_builder
    assert "CaveViewerSetup.iss" in windows_packager
    assert "windows_signed_installer" in windows_metadata_writer
    assert "Assert-InstallerSignature" in windows_installer_smoke
    assert "OutputBaseFilename={#OutputBaseName}" in windows_installer
    assert "package_root / '__main__.py'" in pyinstaller_spec
    assert "caveviewer/resources/shaders" in pyinstaller_spec
    assert "cave_metadata_catalog.v1.json" in pyinstaller_spec
    assert "cave_metadata_catalog.v1.json" in linux_builder
    assert "release_metadata.v1.json" in pyinstaller_spec
    assert "CAVEVIEWER_BRAND_PROFILE_DIR" in pyinstaller_spec
    assert "CAVEVIEWER_BRANDING_EXPORT_SUMMARY" in pyinstaller_spec
    assert "release_metadata.v1.json" in linux_builder


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
    assert "caveviewer-navigation-certify" not in pyproject
    assert "caveviewer-navigation-verify" not in pyproject
    assert 'where = ["src"]' in pyproject
    assert 'pythonpath = ["src"]' in pyproject
    assert '"caveviewer.resources"' in pyproject
    assert "*.egg-info/" in gitignore
    assert ".benchmark-data/" in gitignore
