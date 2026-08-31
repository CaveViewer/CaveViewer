"""Contracts for architecture-specific macOS update manifests and scripts."""

import os
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MACOS_UPDATES = REPOSITORY_ROOT / "updates" / "macos"
requires_executable_shell_scripts = pytest.mark.skipif(
    os.name == "nt",
    reason="macOS release shell helpers are exercised on Unix CI",
)


def test_arm64_manifests_match_signed_legacy_compatibility_aliases():
    for channel in ("stable", "preview"):
        legacy_manifest = MACOS_UPDATES / f"{channel}.json"
        legacy_signature = MACOS_UPDATES / f"{channel}.json.sig"
        arm_manifest = MACOS_UPDATES / "arm64" / f"{channel}.json"
        arm_signature = MACOS_UPDATES / "arm64" / f"{channel}.json.sig"

        paths = (legacy_manifest, legacy_signature, arm_manifest, arm_signature)
        if channel == "stable":
            assert all(path.exists() for path in paths)
        else:
            assert all(path.exists() for path in paths) or not any(
                path.exists() for path in paths
            )
        if not arm_manifest.exists():
            continue

        assert arm_manifest.read_bytes() == legacy_manifest.read_bytes()
        assert arm_signature.read_bytes() == legacy_signature.read_bytes()


@requires_executable_shell_scripts
def test_macos_script_architecture_helper_normalizes_supported_names():
    helper = REPOSITORY_ROOT / "scripts" / "macos" / "architecture.sh"
    command = (
        'source "$1"\n'
        "cv_normalize_macos_arch aarch64\n"
        "cv_normalize_macos_arch amd64\n"
        "if cv_normalize_macos_arch unsupported; then exit 9; fi\n"
    )

    completed = subprocess.run(
        ["bash", "-c", command, "bash", str(helper)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.splitlines() == ["arm64", "x86_64"]


def test_macos_release_scripts_use_architecture_specific_contracts():
    packager = (
        REPOSITORY_ROOT / "scripts" / "macos" / "package_macos_dmg.sh"
    ).read_text(encoding="utf-8")
    publisher = (
        REPOSITORY_ROOT / "scripts" / "macos" / "publish.sh"
    ).read_text(encoding="utf-8")
    manifest_writer = (
        REPOSITORY_ROOT / "scripts" / "macos" / "update_manifest.sh"
    ).read_text(encoding="utf-8")

    assert "CaveViewer-${version}-macos-${macos_arch}.dmg" in packager
    assert '"architecture": "$macos_arch"' in packager
    assert "updates/macos/$macos_arch/$manifest_channel.json" in publisher
    assert 'if [ "$macos_arch" = "arm64" ]; then' in publisher
    assert "updates/macos/$macos_arch/$channel.json" in manifest_writer
    assert '"$repo_root/scripts/write_update_manifest.py"' in manifest_writer
    assert '--architecture "$macos_arch"' in manifest_writer


def test_macos_build_uses_shared_branding_export_and_native_icns_container():
    builder = (
        REPOSITORY_ROOT / "scripts" / "macos" / "build.sh"
    ).read_text(encoding="utf-8")
    smoke = (
        REPOSITORY_ROOT / "scripts" / "macos" / "smoke_dmg.sh"
    ).read_text(encoding="utf-8")

    assert "caveviewer.branding_export" in builder
    assert "CAVEVIEWER_BRAND_PROFILE" in builder
    assert "CAVEVIEWER_BRAND_PROFILE_DIR" in builder
    assert "CAVEVIEWER_BRANDING_EXPORT_SUMMARY" in builder
    assert "macos/CaveViewer.iconset" in builder
    assert 'iconutil -c icns "$iconset_dir" -o "$icon_icns"' in builder
    assert "sips" not in builder
    assert "app_icon_macos.png" not in builder
    assert "CFBundleIconFile" in smoke
    assert "branding/export-summary.v1.json" in smoke
