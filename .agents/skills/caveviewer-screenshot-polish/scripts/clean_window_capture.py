"""Clean captured window edges while preserving screenshot content."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageColor


CornerSelection = Literal["none", "top", "bottom", "all"]
RgbColor = tuple[int, int, int]


@dataclass(frozen=True)
class EdgeInsets:
    """Pixel insets removed from a captured image."""

    top: int = 0
    right: int = 0
    bottom: int = 0
    left: int = 0

    def validate(self) -> None:
        """Reject negative crop values."""
        if min(self.top, self.right, self.bottom, self.left) < 0:
            raise ValueError("trim values must be non-negative")


@dataclass(frozen=True)
class CleanupReport:
    """Machine-readable summary of one screenshot cleanup."""

    input_size: tuple[int, int]
    output_size: tuple[int, int]
    trim: EdgeInsets
    corner_radius: int
    corners: CornerSelection
    transparent_corner_pixels: int
    partial_corner_pixels: int
    opaque_corner_pixels: int

    def to_dict(self) -> dict[str, object]:
        """Return values suitable for JSON output."""
        result = asdict(self)
        result["trim"] = asdict(self.trim)
        return result


def _selected_corners(selection: CornerSelection) -> tuple[str, ...]:
    if selection == "none":
        return ()
    if selection == "top":
        return ("top-left", "top-right")
    if selection == "bottom":
        return ("bottom-left", "bottom-right")
    if selection == "all":
        return ("top-left", "top-right", "bottom-left", "bottom-right")
    raise ValueError(f"unsupported corner selection: {selection}")


def _parse_rgb(value: str) -> RgbColor:
    try:
        color = ImageColor.getrgb(value)
    except ValueError as exc:
        raise ValueError(f"invalid edge color: {value}") from exc
    if len(color) == 4:
        return color[:3]
    return color


def _corner_geometry(
    corner: str,
    width: int,
    height: int,
    radius: int,
) -> tuple[range, range, float, float, tuple[int, int]]:
    if corner == "top-left":
        return (
            range(0, radius),
            range(0, radius),
            float(radius),
            float(radius),
            (min(width - 1, radius + 1), 0),
        )
    if corner == "top-right":
        return (
            range(width - radius, width),
            range(0, radius),
            float(width - radius),
            float(radius),
            (max(0, width - radius - 2), 0),
        )
    if corner == "bottom-left":
        return (
            range(0, radius),
            range(height - radius, height),
            float(radius),
            float(height - radius),
            (min(width - 1, radius + 1), height - 1),
        )
    if corner == "bottom-right":
        return (
            range(width - radius, width),
            range(height - radius, height),
            float(width - radius),
            float(height - radius),
            (max(0, width - radius - 2), height - 1),
        )
    raise ValueError(f"unsupported corner: {corner}")


def _pixel_coverage(
    x: int,
    y: int,
    center_x: float,
    center_y: float,
    radius: int,
    supersample: int,
) -> float:
    inside = 0
    radius_squared = radius * radius
    for sample_y in range(supersample):
        point_y = y + ((sample_y + 0.5) / supersample)
        delta_y = point_y - center_y
        for sample_x in range(supersample):
            point_x = x + ((sample_x + 0.5) / supersample)
            delta_x = point_x - center_x
            if (delta_x * delta_x) + (delta_y * delta_y) <= radius_squared:
                inside += 1
    return inside / (supersample * supersample)


def clean_image(
    source: Image.Image,
    *,
    trim: EdgeInsets = EdgeInsets(),
    corner_radius: int = 0,
    corners: CornerSelection = "none",
    edge_color: RgbColor | None = None,
    supersample: int = 8,
) -> tuple[Image.Image, CleanupReport]:
    """Return a cropped RGBA copy and a report of its corner transparency."""
    trim.validate()
    if supersample < 1 or supersample > 16:
        raise ValueError("supersample must be between 1 and 16")

    input_size = source.size
    cropped_width = source.width - trim.left - trim.right
    cropped_height = source.height - trim.top - trim.bottom
    if cropped_width < 1 or cropped_height < 1:
        raise ValueError("trim values remove the entire image")

    selected_corners = _selected_corners(corners)
    if selected_corners and corner_radius < 1:
        raise ValueError("corner radius must be positive when corners are selected")
    if not selected_corners and corner_radius != 0:
        raise ValueError("corner radius requires a non-none corner selection")
    if corner_radius * 2 > min(cropped_width, cropped_height):
        raise ValueError("corner radius is too large for the cropped image")

    with source.convert("RGBA") as rgba_source:
        cleaned = rgba_source.crop(
            (
                trim.left,
                trim.top,
                source.width - trim.right,
                source.height - trim.bottom,
            )
        )

    transparent = 0
    partial = 0
    opaque = 0
    pixels = cleaned.load()
    for corner in selected_corners:
        x_range, y_range, center_x, center_y, sample_point = _corner_geometry(
            corner,
            cleaned.width,
            cleaned.height,
            corner_radius,
        )
        corner_color = edge_color or pixels[sample_point[0], sample_point[1]][:3]
        for y in y_range:
            for x in x_range:
                coverage = _pixel_coverage(
                    x,
                    y,
                    center_x,
                    center_y,
                    corner_radius,
                    supersample,
                )
                previous_alpha = pixels[x, y][3]
                alpha = round(previous_alpha * coverage)
                pixels[x, y] = (*corner_color, alpha)
                if alpha == 0:
                    transparent += 1
                elif alpha == 255:
                    opaque += 1
                else:
                    partial += 1

    report = CleanupReport(
        input_size=input_size,
        output_size=cleaned.size,
        trim=trim,
        corner_radius=corner_radius,
        corners=corners,
        transparent_corner_pixels=transparent,
        partial_corner_pixels=partial,
        opaque_corner_pixels=opaque,
    )
    return cleaned, report


def clean_capture(
    input_path: Path,
    output_path: Path,
    *,
    trim: EdgeInsets = EdgeInsets(),
    corner_radius: int = 0,
    corners: CornerSelection = "none",
    edge_color: RgbColor | None = None,
    supersample: int = 8,
    replace: bool = False,
) -> CleanupReport:
    """Clean one capture and publish the PNG atomically."""
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    if input_path == output_path:
        raise ValueError("input and output paths must differ")
    if output_path.suffix.lower() != ".png":
        raise ValueError("output must be a PNG file")
    if output_path.exists() and not replace:
        raise FileExistsError(f"output already exists: {output_path}")

    with Image.open(input_path) as source:
        source.load()
        cleaned, report = clean_image(
            source,
            trim=trim,
            corner_radius=corner_radius,
            corners=corners,
            edge_color=edge_color,
            supersample=supersample,
        )
        save_options: dict[str, object] = {}
        if source.info.get("icc_profile") is not None:
            save_options["icc_profile"] = source.info["icc_profile"]
        if source.info.get("dpi") is not None:
            save_options["dpi"] = source.info["dpi"]

    temporary_path: Path | None = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=output_path.parent,
            prefix=f".{output_path.stem}-",
            suffix=".png",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        cleaned.save(temporary_path, format="PNG", **save_options)
        os.replace(temporary_path, output_path)
    finally:
        cleaned.close()
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return report


def _non_negative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Trim captured window fringes and reconstruct transparent rounded "
            "corners without resampling screenshot content."
        )
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--trim",
        type=_non_negative,
        default=0,
        help="default trim in pixels for all four edges",
    )
    for edge in ("top", "right", "bottom", "left"):
        parser.add_argument(
            f"--trim-{edge}",
            type=_non_negative,
            help=f"override the default trim for the {edge} edge",
        )
    parser.add_argument("--corner-radius", type=_non_negative, default=0)
    parser.add_argument(
        "--corners",
        choices=("none", "top", "bottom", "all"),
        default="none",
    )
    parser.add_argument(
        "--edge-color",
        help="RGB color used to rebuild empty corner boxes, such as #0A0A0D",
    )
    parser.add_argument("--supersample", type=_positive, default=8)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing output file; input is still never overwritten",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the screenshot cleanup command."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.supersample > 16:
        parser.error("--supersample must not exceed 16")
    if arguments.corners == "none" and arguments.corner_radius != 0:
        parser.error("--corner-radius requires --corners")
    if arguments.corners != "none" and arguments.corner_radius == 0:
        parser.error("--corners requires a positive --corner-radius")
    if arguments.edge_color is not None and arguments.corners == "none":
        parser.error("--edge-color requires --corners")

    trim = EdgeInsets(
        top=arguments.trim if arguments.trim_top is None else arguments.trim_top,
        right=(
            arguments.trim if arguments.trim_right is None else arguments.trim_right
        ),
        bottom=(
            arguments.trim
            if arguments.trim_bottom is None
            else arguments.trim_bottom
        ),
        left=arguments.trim if arguments.trim_left is None else arguments.trim_left,
    )
    try:
        edge_color = (
            None if arguments.edge_color is None else _parse_rgb(arguments.edge_color)
        )
        report = clean_capture(
            arguments.input,
            arguments.output,
            trim=trim,
            corner_radius=arguments.corner_radius,
            corners=arguments.corners,
            edge_color=edge_color,
            supersample=arguments.supersample,
            replace=arguments.replace,
        )
    except (FileNotFoundError, FileExistsError, OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")

    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
