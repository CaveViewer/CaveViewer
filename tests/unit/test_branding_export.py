"""Exercise deterministic cross-platform branding artifact export."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
import pytest

from caveviewer.branding import REQUIRED_ROLES, resolve_branding_profile
from caveviewer.branding_export import (
    LINUX_APPLICATION_ID,
    LINUX_ICON_SIZES,
    MACOS_ICONSET_OUTPUTS,
    PREVIEW_ICON_SIZES,
    RUNTIME_ROLE_SIZES,
    WINDOWS_ICON_SIZES,
    BrandingExportError,
    _render_asset,
    export_branding_profile,
    run,
)


def test_export_produces_every_runtime_and_platform_artifact(tmp_path):
    destination = tmp_path / "brand"
    summary_path = export_branding_profile(
        resolve_branding_profile(environ={}), destination
    )

    for role, size in RUNTIME_ROLE_SIZES.items():
        _assert_rgba_png(destination / "runtime" / f"{role}.png", size)
    with Image.open(destination / "runtime" / "loading_mark.png") as loading_mark:
        mark_bounds = loading_mark.getchannel("A").getbbox()
        assert mark_bounds is not None
        assert mark_bounds[2] - mark_bounds[0] <= 170
        assert mark_bounds[3] - mark_bounds[1] <= 170
    with Image.open(
        destination / "runtime" / "loading_progress_mask.png"
    ) as loading_mask:
        mask_bounds = loading_mask.getchannel("A").getbbox()
        assert mask_bounds is not None
        assert mark_bounds[0] <= mask_bounds[0] < mask_bounds[2] <= mark_bounds[2]
        assert mark_bounds[1] <= mask_bounds[1] < mask_bounds[3] <= mark_bounds[3]
    for filename, size in MACOS_ICONSET_OUTPUTS:
        _assert_rgba_png(destination / "macos" / "CaveViewer.iconset" / filename, size)
    for size in LINUX_ICON_SIZES:
        _assert_rgba_png(
            destination
            / "linux"
            / "hicolor"
            / f"{size}x{size}"
            / "apps"
            / f"{LINUX_APPLICATION_ID}.png",
            size,
        )
    scalable_icon = (
        destination
        / "linux"
        / "hicolor"
        / "scalable"
        / "apps"
        / f"{LINUX_APPLICATION_ID}.svg"
    )
    symbolic_icon = (
        destination
        / "linux"
        / "hicolor"
        / "symbolic"
        / "apps"
        / f"{LINUX_APPLICATION_ID}-symbolic.svg"
    )
    assert scalable_icon.read_text(encoding="utf-8").startswith("<?xml")
    assert symbolic_icon.read_text(encoding="utf-8").startswith("<?xml")
    _assert_ico_sizes(destination / "windows" / "caveviewer.ico")
    assert (destination / "linux" / ".DirIcon").is_file()
    assert (destination / "previews" / "contact-sheet.png").is_file()
    assert (destination / "previews" / "macos-contact-sheet.png").is_file()
    assert (destination / "previews" / "linux-contact-sheet.png").is_file()
    assert summary_path == destination / "export-summary.v1.json"

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["schema_version"] == 1
    assert summary["profile"]["id"] == "default"
    assert set(summary["roles"]) == REQUIRED_ROLES
    assert all("sha256" in output for output in summary["outputs"])
    assert all(not Path(output["path"]).is_absolute() for output in summary["outputs"])


def test_export_is_byte_reproducible(tmp_path):
    profile = resolve_branding_profile(environ={})
    first = tmp_path / "first"
    second = tmp_path / "second"

    export_branding_profile(profile, first)
    export_branding_profile(profile, second)

    assert _file_payloads(first) == _file_payloads(second)


def test_export_refuses_existing_destination_and_cleans_failed_staging(tmp_path):
    profile = resolve_branding_profile(environ={})
    destination = tmp_path / "brand"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(BrandingExportError, match="already exists"):
        export_branding_profile(profile, destination)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob(".brand-*"))


def test_replace_rebuilds_existing_export(tmp_path):
    profile = resolve_branding_profile(environ={})
    destination = tmp_path / "brand"
    export_branding_profile(profile, destination)
    stale = destination / "stale.txt"
    stale.write_text("stale", encoding="utf-8")

    export_branding_profile(profile, destination, replace=True)

    assert not stale.exists()
    assert (destination / "export-summary.v1.json").is_file()


def test_render_cache_reuses_derivative_without_sharing_mutable_image():
    asset = resolve_branding_profile(environ={}).asset_for("windows_app_icon")
    cache = {}

    first = _render_asset(asset, 32, cache=cache)
    second = _render_asset(asset, 32, cache=cache)
    original_pixel = second.getpixel((0, 0))
    first.putpixel((0, 0), (1, 2, 3, 4))

    assert len(cache) == 1
    assert first is not second
    assert second.getpixel((0, 0)) == original_pixel


def test_cli_validates_exports_and_writes_contact_sheet(tmp_path, capsys):
    assert run(["validate"]) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation == {"profile_id": "default", "valid": True}

    export_dir = tmp_path / "export"
    assert run(["export", "--output", str(export_dir)]) == 0
    assert (export_dir / "export-summary.v1.json").is_file()

    contact_sheet = tmp_path / "sheet.png"
    assert run(["contact-sheet", "--output", str(contact_sheet)]) == 0
    with Image.open(contact_sheet) as image:
        assert image.mode == "RGBA"
        assert image.width > max(PREVIEW_ICON_SIZES)
        assert image.height >= (max(PREVIEW_ICON_SIZES) * 4 + 40) * 2


def _assert_rgba_png(path: Path, size: int) -> None:
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.mode == "RGBA"
        assert image.size == (size, size)


def _assert_ico_sizes(path: Path) -> None:
    with Image.open(path) as image:
        sizes = set(image.info["sizes"])
    assert sizes == {(size, size) for size in WINDOWS_ICON_SIZES}


def _file_payloads(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
