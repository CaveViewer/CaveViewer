"""Pinned provenance and metadata contracts for bundled UI fonts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import freetype

from caveviewer.resources import (
    inter_font_license_path,
    inter_font_manifest_path,
    inter_font_path,
    inter_font_paths,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INTER_ROOT = REPOSITORY_ROOT / "src/caveviewer/resources/fonts/inter"


def test_inter_manifest_pins_release_files_and_license() -> None:
    manifest = json.loads((INTER_ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["family"] == "Inter"
    assert manifest["version"] == "4.0"
    assert manifest["upstream"]["release"].endswith("/releases/tag/v4.0")
    assert manifest["upstream"]["archive"].endswith("/v4.0/Inter-4.0.zip")
    assert manifest["upstream"]["archive_sha256"] == (
        "ff970a5d4561a04f102a7cb781adbd6ac4e9b6c460914c7a101f15acb7f7d1a4"
    )
    assert set(manifest["files"]) == {
        "Inter-Regular.ttf",
        "Inter-Medium.ttf",
        "Inter-SemiBold.ttf",
        "Inter-Bold.ttf",
        "LICENSE.txt",
    }

    for filename, metadata in manifest["files"].items():
        data = (INTER_ROOT / filename).read_bytes()
        assert hashlib.sha256(data).hexdigest() == metadata["sha256"]

    license_text = (INTER_ROOT / "LICENSE.txt").read_text(encoding="utf-8")
    assert license_text.startswith("Copyright (c) 2016 The Inter Project Authors")
    assert "SIL OPEN FONT LICENSE Version 1.1" in license_text


def test_inter_static_faces_retain_expected_family_and_styles() -> None:
    expected_styles = {
        "Inter-Regular.ttf": "Regular",
        "Inter-Medium.ttf": "Medium",
        "Inter-SemiBold.ttf": "SemiBold",
        "Inter-Bold.ttf": "Bold",
    }

    for filename, expected_style in expected_styles.items():
        face = freetype.Face(str(INTER_ROOT / filename))
        assert face.family_name == b"Inter"
        assert face.style_name == expected_style.encode("ascii")
        assert face.num_glyphs == 2926


def test_inter_resource_helpers_resolve_the_pinned_files() -> None:
    assert inter_font_path() == INTER_ROOT / "Inter-Regular.ttf"
    assert inter_font_path("medium") == INTER_ROOT / "Inter-Medium.ttf"
    assert inter_font_path("semibold") == INTER_ROOT / "Inter-SemiBold.ttf"
    assert inter_font_path("bold") == INTER_ROOT / "Inter-Bold.ttf"
    assert inter_font_paths() == (
        INTER_ROOT / "Inter-Regular.ttf",
        INTER_ROOT / "Inter-Medium.ttf",
        INTER_ROOT / "Inter-SemiBold.ttf",
        INTER_ROOT / "Inter-Bold.ttf",
    )
    assert inter_font_license_path() == INTER_ROOT / "LICENSE.txt"
    assert inter_font_manifest_path() == INTER_ROOT / "manifest.json"


def test_unknown_inter_style_is_rejected() -> None:
    try:
        inter_font_path("italic")
    except ValueError as exc:
        assert "unknown Inter style 'italic'" in str(exc)
    else:
        raise AssertionError("unknown Inter style was accepted")


def test_inter_assets_are_declared_for_source_and_frozen_packages() -> None:
    pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pyinstaller_spec = (
        REPOSITORY_ROOT / "packaging/pyinstaller/CaveViewer.spec"
    ).read_text(encoding="utf-8")

    assert '"fonts/inter/*.json"' in pyproject
    assert '"fonts/inter/*.ttf"' in pyproject
    assert '"fonts/inter/*.txt"' in pyproject
    assert "resources_root / 'fonts'" in pyinstaller_spec
    assert "'caveviewer/resources/fonts'" in pyinstaller_spec


def test_linux_package_uses_bundled_inter_instead_of_copying_a_system_font() -> None:
    linux_builder = (
        REPOSITORY_ROOT / "scripts/linux/common/build.sh"
    ).read_text(encoding="utf-8")
    linux_container = (
        REPOSITORY_ROOT / "scripts/linux/Dockerfile.linux-build"
    ).read_text(encoding="utf-8")
    linux_packager = (
        REPOSITORY_ROOT / "scripts/linux/common/package.sh"
    ).read_text(encoding="utf-8")

    assert "fonts-noto-core" not in linux_builder
    assert "fonts-noto-core" not in linux_container
    assert 'resources/fonts:caveviewer/resources/fonts"' in linux_builder
    assert "CaveViewerUI-Regular.ttf" not in linux_packager
    assert "bundled_ui_font" not in linux_packager
