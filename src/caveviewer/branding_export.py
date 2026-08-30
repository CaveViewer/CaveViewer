"""Deterministically export one CaveViewer branding profile for every platform."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from PIL import Image, ImageDraw

from caveviewer.branding import (
    BRANDING_MANIFEST_FILENAME,
    BrandingAsset,
    BrandingProfile,
    BrandingProfileError,
    default_branding_manifest_path,
    load_branding_profile,
)


EXPORT_SCHEMA_VERSION = 1
WINDOWS_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
LINUX_ICON_SIZES = (48, 64, 128, 256, 512)
PREVIEW_ICON_SIZES = (16, 24, 32)
MACOS_ICONSET_OUTPUTS = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)
RUNTIME_ROLE_SIZES = {
    "application_mark": 512,
    "about_mark": 512,
    "loading_mark": 256,
    "windows_app_icon": 256,
    "macos_app_icon": 1024,
    "linux_app_icon": 512,
}
LINUX_APPLICATION_ID = "io.github.caveviewer.caveviewer"


class BrandingExportError(RuntimeError):
    """Report an export failure without leaving a partial destination."""


def export_branding_profile(
    profile: BrandingProfile,
    output_directory: str | os.PathLike[str],
    *,
    replace: bool = False,
) -> Path:
    """Export all deterministic derivatives and return the summary path."""
    destination = Path(output_directory).resolve()
    if destination.exists() and not replace:
        raise BrandingExportError(
            f"branding export destination already exists: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent)
    )
    try:
        _write_runtime_outputs(profile, staging)
        _write_windows_outputs(profile, staging)
        _write_macos_outputs(profile, staging)
        _write_linux_outputs(profile, staging)
        _write_contact_sheet(profile, staging / "previews" / "contact-sheet.png")
        summary_path = _write_summary(profile, staging)
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise BrandingExportError(
                    "refusing to replace non-directory export destination: "
                    f"{destination}"
                )
            shutil.rmtree(destination)
        staging.replace(destination)
        return destination / summary_path.relative_to(staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def write_contact_sheet(
    profile: BrandingProfile,
    output_path: str | os.PathLike[str],
) -> Path:
    """Write exact-size light/dark previews for interactive brand comparison."""
    destination = Path(output_path).resolve()
    _write_contact_sheet(profile, destination)
    return destination


def _write_runtime_outputs(profile: BrandingProfile, root: Path) -> None:
    for role, size in RUNTIME_ROLE_SIZES.items():
        _save_png(
            _render_asset(profile.asset_for(role), size),
            root / "runtime" / f"{role}.png",
        )


def _write_windows_outputs(profile: BrandingProfile, root: Path) -> None:
    source = _render_asset(profile.asset_for("windows_app_icon"), 256)
    destination = root / "windows" / "caveviewer.ico"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.save(
        destination,
        format="ICO",
        sizes=[(size, size) for size in WINDOWS_ICON_SIZES],
        bitmap_format="png",
    )


def _write_macos_outputs(profile: BrandingProfile, root: Path) -> None:
    asset = profile.asset_for("macos_app_icon")
    iconset = root / "macos" / "CaveViewer.iconset"
    for filename, size in MACOS_ICONSET_OUTPUTS:
        _save_png(_render_asset(asset, size), iconset / filename)


def _write_linux_outputs(profile: BrandingProfile, root: Path) -> None:
    asset = profile.asset_for("linux_app_icon")
    for size in LINUX_ICON_SIZES:
        destination = (
            root
            / "linux"
            / "hicolor"
            / f"{size}x{size}"
            / "apps"
            / f"{LINUX_APPLICATION_ID}.png"
        )
        _save_png(_render_asset(asset, size), destination)
    root_icon = _render_asset(asset, 256)
    _save_png(root_icon, root / "linux" / f"{LINUX_APPLICATION_ID}.png")
    _save_png(root_icon, root / "linux" / ".DirIcon")


def _render_asset(asset: BrandingAsset, size: int) -> Image.Image:
    with Image.open(asset.path) as source_file:
        source = source_file.convert("RGBA")
    content_size = max(1, round(size * (1.0 - (2.0 * asset.safe_area_inset))))
    source.thumbnail((content_size, content_size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    offset = ((size - source.width) // 2, (size - source.height) // 2)
    canvas.alpha_composite(source, offset)
    return canvas


def _save_png(image: Image.Image, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=False, compress_level=9)


def _write_contact_sheet(profile: BrandingProfile, destination: Path) -> None:
    # Keep the largest exact-size preview wholly inside its cell. The previous
    # 5x enlargement clipped 32-pixel artwork and gave a misleading impression
    # of the icon's safe area during brand review.
    scale = 4
    margin = 20
    cell_width = 160
    cell_height = 180
    backgrounds = (("light", (245, 243, 237, 255)), ("dark", (24, 24, 22, 255)))
    sheet = Image.new(
        "RGBA",
        (margin * 2 + cell_width * len(PREVIEW_ICON_SIZES),
         margin * 2 + cell_height * len(backgrounds)),
        (38, 36, 32, 255),
    )
    draw = ImageDraw.Draw(sheet)
    asset = profile.asset_for("application_mark")
    for row, (label, background) in enumerate(backgrounds):
        for column, size in enumerate(PREVIEW_ICON_SIZES):
            left = margin + column * cell_width
            top = margin + row * cell_height
            draw.rounded_rectangle(
                (left, top, left + cell_width - 10, top + cell_height - 10),
                radius=8,
                fill=background,
            )
            exact = _render_asset(asset, size)
            preview = exact.resize(
                (size * scale, size * scale), Image.Resampling.NEAREST
            )
            x = left + (cell_width - 10 - preview.width) // 2
            y = top + 10
            sheet.alpha_composite(preview, (x, y))
            text_color = (
                (32, 30, 26, 255)
                if label == "light"
                else (238, 231, 215, 255)
            )
            draw.text(
                (left + 8, top + cell_height - 32),
                f"{size}px - {label}",
                fill=text_color,
            )
    _save_png(sheet, destination)


def _write_summary(profile: BrandingProfile, root: Path) -> Path:
    outputs = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "export-summary.v1.json":
            continue
        outputs.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
        )
    payload = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "profile": {
            "id": profile.profile_id,
            "display_name": profile.display_name,
            "manifest_sha256": hashlib.sha256(
                profile.manifest_path.read_bytes()
            ).hexdigest(),
        },
        "roles": {
            role: {
                "asset_id": asset_id,
                "source_sha256": profile.assets[asset_id].sha256,
            }
            for role, asset_id in sorted(profile.roles.items())
        },
        "outputs": outputs,
    }
    destination = root / "export-summary.v1.json"
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return destination


def _load_cli_profile(profile_argument: str | None) -> BrandingProfile:
    if profile_argument is None:
        return load_branding_profile(default_branding_manifest_path())
    path = Path(profile_argument)
    if path.is_dir():
        path = path / BRANDING_MANIFEST_FILENAME
    return load_branding_profile(path)


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the side-effect-free branding CLI parser."""
    parser = argparse.ArgumentParser(prog="caveviewer-branding")
    parser.add_argument(
        "--profile",
        help="Profile directory or branding.v1.json; defaults to the bundled profile",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate a profile and print its identity")
    export_parser = subparsers.add_parser("export", help="Export all brand artifacts")
    export_parser.add_argument("--output", required=True, type=Path)
    export_parser.add_argument("--replace", action="store_true")
    sheet_parser = subparsers.add_parser(
        "contact-sheet", help="Export exact-size light/dark previews"
    )
    sheet_parser.add_argument("--output", required=True, type=Path)
    return parser


def run(argv: list[str] | None = None) -> int:
    """Run the branding export CLI."""
    arguments = build_argument_parser().parse_args(argv)
    try:
        profile = _load_cli_profile(arguments.profile)
        if arguments.command == "validate":
            print(json.dumps({"profile_id": profile.profile_id, "valid": True}))
        elif arguments.command == "export":
            summary = export_branding_profile(
                profile, arguments.output, replace=arguments.replace
            )
            print(summary)
        else:
            print(write_contact_sheet(profile, arguments.output))
    except (BrandingProfileError, BrandingExportError, OSError) as exc:
        print(f"Branding error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
