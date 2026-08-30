"""Cross-platform contract tying runtime branding to package exports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from caveviewer.branding import resolve_branding_assets, resolve_branding_profile
from caveviewer.branding_export import export_branding_profile


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_runtime_and_every_package_pipeline_select_the_same_profile_contract():
    runtime_assets = resolve_branding_assets(environ={})
    profile = resolve_branding_profile(environ={})
    scripts = {
        "windows": REPOSITORY_ROOT / "scripts/windows/build.sh",
        "macos": REPOSITORY_ROOT / "scripts/macos/build.sh",
        "linux": REPOSITORY_ROOT / "scripts/linux/common/build.sh",
    }

    assert runtime_assets.profile_id == profile.profile_id
    for platform_name, path in scripts.items():
        source = path.read_text(encoding="utf-8")
        assert "CAVEVIEWER_BRAND_PROFILE" in source, platform_name
        assert "caveviewer.branding_export" in source, platform_name
        assert "export-summary.v1.json" in source, platform_name
        assert "branding/default" in source, platform_name


def test_export_summary_hashes_every_output_and_small_previews_are_rgba(tmp_path):
    destination = tmp_path / "brand"
    summary_path = export_branding_profile(
        resolve_branding_profile(environ={}), destination
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    for output in summary["outputs"]:
        path = destination / output["path"]
        assert path.stat().st_size == output["bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == output["sha256"]

    with Image.open(destination / "previews/contact-sheet.png") as sheet:
        assert sheet.mode == "RGBA"
    for size in (16, 24, 32):
        with Image.open(destination / "windows/caveviewer.ico") as icon:
            icon.size = (size, size)
            assert icon.convert("RGBA").getbbox() is not None


def test_branding_package_data_and_native_smoke_contracts_are_tracked():
    pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    macos_smoke = (
        REPOSITORY_ROOT / "scripts/macos/smoke_dmg.sh"
    ).read_text(encoding="utf-8")
    linux_package = (
        REPOSITORY_ROOT / "scripts/linux/common/package.sh"
    ).read_text(encoding="utf-8")
    windows_package = (
        REPOSITORY_ROOT / "scripts/windows/package.sh"
    ).read_text(encoding="utf-8")

    assert '"branding/**/*.json"' in pyproject
    assert '"branding/**/*.png"' in pyproject
    assert "CFBundleIconFile" in macos_smoke
    assert "hicolor" in linux_package and ".DirIcon" in linux_package
    assert "SetupIconFile" in windows_package
