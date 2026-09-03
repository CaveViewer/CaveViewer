"""Raster visual primitives owned by the Tk splash and library presentation.

The Tk presentation modules stay responsible for widget creation and mutation.
This module prepares progress-ring and vector-icon images with Pillow so
Canvas presentations can remain crisp on high-density displays without each
caller implementing its own pixel geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cache
from typing import Literal, TypeAlias

from PIL import Image, ImageColor, ImageDraw

from caveviewer.resources import ui_icon_path


_PROGRESS_RING_MAX_SUPERSAMPLE = 4
_PROGRESS_RING_MAX_RASTER_SIZE = 2048

Color: TypeAlias = str | tuple[int, int, int] | tuple[int, int, int, int]
Point: TypeAlias = tuple[float, float]
ProgressCenterGlyph: TypeAlias = Literal["pause", "stop"]


@dataclass(frozen=True)
class VectorPath:
    """One rounded vector path expressed in target Canvas pixels."""

    points: tuple[Point, ...]
    color: Color
    width: float
    closed: bool = False


@dataclass(frozen=True)
class VectorArc:
    """One rounded partial circular path expressed in target Canvas pixels."""

    center: Point
    radius: float
    start_degrees: float
    extent_degrees: float
    color: Color
    width: float


@dataclass(frozen=True)
class VectorPolygon:
    """One filled and/or outlined polygon expressed in target Canvas pixels."""

    points: tuple[Point, ...]
    fill_color: Color | None = None
    outline_color: Color | None = None
    outline_width: float = 1.0


@dataclass(frozen=True)
class VectorEllipse:
    """One filled and/or outlined ellipse expressed in target Canvas pixels."""

    bounds: tuple[float, float, float, float]
    fill_color: Color | None = None
    outline_color: Color | None = None
    outline_width: float = 1.0


@dataclass(frozen=True)
class VectorRectangle:
    """One filled rectangle expressed in target Canvas pixels."""

    bounds: tuple[float, float, float, float]
    fill_color: Color


def progress_ring_supersample_factor(image_size: int | float) -> int:
    """Return a bounded scale that keeps ring edges smooth on dense displays."""
    target_size = max(1, int(round(float(image_size))))
    max_safe_factor = max(1, _PROGRESS_RING_MAX_RASTER_SIZE // target_size)
    return min(_PROGRESS_RING_MAX_SUPERSAMPLE, max_safe_factor)


def _rgba(color: Color) -> tuple[int, int, int, int]:
    """Normalize a Tk/Pillow color into a Pillow RGBA tuple."""
    if isinstance(color, tuple):
        if len(color) == 4:
            return color
        if len(color) == 3:
            return (*color, 255)
    red, green, blue = ImageColor.getrgb(str(color))
    return red, green, blue, 255


def _arc_points(
    *,
    center: Point,
    radius: float,
    start_degrees: float,
    extent_degrees: float,
) -> list[tuple[float, float]]:
    """Return Canvas-compatible arc points around the supplied x/y center."""
    center_x, center_y = center
    steps = max(2, int(math.ceil(abs(extent_degrees) * max(1.0, radius) / 12)))
    points: list[tuple[float, float]] = []
    for index in range(steps + 1):
        angle = math.radians(start_degrees + extent_degrees * index / steps)
        points.append(
            (
                center_x + radius * math.cos(angle),
                center_y - radius * math.sin(angle),
            )
        )
    return points


def _render_progress_ring_raster(
    *,
    image_size: int | float,
    ring_diameter: int | float,
    stroke_width: int | float,
    track_color: str | tuple[int, int, int] | tuple[int, int, int, int],
    fill_color: str | tuple[int, int, int] | tuple[int, int, int, int],
    start_degrees: float,
    extent_degrees: float,
) -> tuple[Image.Image, int, int]:
    """Draw a progress ring in its supersampled raster coordinate system."""
    target_size = max(1, int(round(float(image_size))))
    supersample = progress_ring_supersample_factor(target_size)
    raster_size = target_size * supersample
    ring_diameter = min(
        float(target_size),
        max(1.0, float(ring_diameter)),
    ) * supersample
    stroke = max(1, int(round(float(stroke_width) * supersample)))
    center = raster_size / 2.0
    radius = max(0.5, ring_diameter / 2.0 - stroke / 2.0)
    bounds = (
        int(round(center - radius)),
        int(round(center - radius)),
        int(round(center + radius)),
        int(round(center + radius)),
    )
    image = Image.new("RGBA", (raster_size, raster_size))
    drawer = ImageDraw.Draw(image)
    drawer.ellipse(bounds, outline=_rgba(track_color), width=stroke)

    extent = float(extent_degrees)
    if abs(extent) >= 359.5:
        drawer.ellipse(bounds, outline=_rgba(fill_color), width=stroke)
    elif abs(extent) > 0.0:
        points = _arc_points(
            center=(center, center),
            radius=radius,
            start_degrees=float(start_degrees),
            extent_degrees=extent,
        )
        drawer.line(points, fill=_rgba(fill_color), width=stroke, joint="curve")
        cap_radius = stroke / 2.0
        for point_x, point_y in (points[0], points[-1]):
            drawer.ellipse(
                (
                    int(round(point_x - cap_radius)),
                    int(round(point_y - cap_radius)),
                    int(round(point_x + cap_radius)),
                    int(round(point_y + cap_radius)),
                ),
                fill=_rgba(fill_color),
            )

    return image, target_size, supersample


def _downsample_progress_raster(
    image: Image.Image,
    *,
    target_size: int,
    supersample: int,
) -> Image.Image:
    """Return a progress-control raster at its target Tk-pixel dimensions."""
    if supersample == 1:
        return image
    return image.resize((target_size, target_size), Image.Resampling.LANCZOS)


def render_progress_ring(
    *,
    image_size: int | float,
    ring_diameter: int | float,
    stroke_width: int | float,
    track_color: str | tuple[int, int, int] | tuple[int, int, int, int],
    fill_color: str | tuple[int, int, int] | tuple[int, int, int, int],
    start_degrees: float,
    extent_degrees: float,
) -> Image.Image:
    """Render one transparent, anti-aliased circular progress indicator.

    ``image_size`` is already the target Tk pixel size after display scaling.
    The ring is drawn at a bounded larger resolution and then LANCZOS
    downsampled so Canvas's non-antialiased oval/arc primitives are avoided.
    """
    image, target_size, supersample = _render_progress_ring_raster(
        image_size=image_size,
        ring_diameter=ring_diameter,
        stroke_width=stroke_width,
        track_color=track_color,
        fill_color=fill_color,
        start_degrees=start_degrees,
        extent_degrees=extent_degrees,
    )
    return _downsample_progress_raster(
        image,
        target_size=target_size,
        supersample=supersample,
    )


def _draw_progress_center_glyph(
    drawer: ImageDraw.ImageDraw,
    *,
    raster_size: int,
    supersample: int,
    glyph: ProgressCenterGlyph,
    glyph_size: int | float,
    color: Color,
) -> None:
    """Draw a stop square or pause bars with bounds centered in one raster."""
    glyph_pixels = min(
        raster_size,
        max(supersample, int(round(float(glyph_size) * supersample))),
    )
    top = (raster_size - glyph_pixels) // 2
    color_rgba = _rgba(color)

    if glyph == "stop":
        left = (raster_size - glyph_pixels) // 2
        drawer.rectangle(
            (left, top, left + glyph_pixels - 1, top + glyph_pixels - 1),
            fill=color_rgba,
        )
        return

    if glyph != "pause":
        raise ValueError(f"Unsupported progress center glyph: {glyph!r}")

    # Keep the two pause bars symmetric about the same raster center as the
    # ring. Drawing both before one shared downsample avoids Canvas half-pixel
    # placement differences on fractional display scales.
    bar_width = max(1, int(round(glyph_pixels / 3)))
    gap_width = max(1, int(round(glyph_pixels / 5)))
    total_width = bar_width * 2 + gap_width
    left = (raster_size - total_width) // 2
    right = left + bar_width + gap_width
    for bar_left in (left, right):
        drawer.rectangle(
            (
                bar_left,
                top,
                bar_left + bar_width - 1,
                top + glyph_pixels - 1,
            ),
            fill=color_rgba,
        )


def render_progress_control(
    *,
    image_size: int | float,
    ring_diameter: int | float,
    stroke_width: int | float,
    track_color: Color,
    fill_color: Color,
    start_degrees: float,
    extent_degrees: float,
    center_glyph: ProgressCenterGlyph,
    center_glyph_size: int | float,
    center_glyph_color: Color,
) -> Image.Image:
    """Render an anti-aliased progress ring and centered stop/pause affordance.

    The central glyph is drawn in the ring's supersampled raster, then both
    are downsampled together. This keeps their visual centers aligned when Tk
    display scaling turns logical dimensions into different physical pixels.
    """
    image, target_size, supersample = _render_progress_ring_raster(
        image_size=image_size,
        ring_diameter=ring_diameter,
        stroke_width=stroke_width,
        track_color=track_color,
        fill_color=fill_color,
        start_degrees=start_degrees,
        extent_degrees=extent_degrees,
    )
    _draw_progress_center_glyph(
        ImageDraw.Draw(image),
        raster_size=image.width,
        supersample=supersample,
        glyph=center_glyph,
        glyph_size=center_glyph_size,
        color=center_glyph_color,
    )
    return _downsample_progress_raster(
        image,
        target_size=target_size,
        supersample=supersample,
    )


def _target_icon_size(
    image_size: tuple[int | float, int | float],
) -> tuple[int, int]:
    """Normalize a target Canvas image size without allowing zero dimensions."""
    return (
        max(1, int(round(float(image_size[0])))),
        max(1, int(round(float(image_size[1])))),
    )


@cache
def _retry_icon_alpha() -> Image.Image:
    """Load the alpha channel of the bundled Font Awesome retry asset once."""
    with Image.open(ui_icon_path("retry.png")) as icon:
        return icon.getchannel("A").copy()


def render_retry_icon(
    *,
    image_size: tuple[int | float, int | float],
    glyph_diameter: int | float,
    color: Color,
) -> Image.Image:
    """Render the licensed Font Awesome retry glyph at its optical size.

    The source artwork's circular arc fills its 512-pixel view box. Scaling it
    to ``glyph_diameter`` keeps the denser circular arrow optically balanced
    with neighboring action icons instead of filling the larger button target.
    """
    target_width, target_height = _target_icon_size(image_size)
    content_diameter = min(
        max(1, int(round(float(glyph_diameter)))),
        target_width,
        target_height,
    )
    supersample = progress_ring_supersample_factor(
        max(target_width, target_height)
    )
    raster_width = target_width * supersample
    raster_height = target_height * supersample
    raster_diameter = content_diameter * supersample
    alpha = Image.new("L", (raster_width, raster_height))
    source_alpha = _retry_icon_alpha().resize(
        (raster_diameter, raster_diameter),
        Image.Resampling.LANCZOS,
    )
    alpha.paste(
        source_alpha,
        (
            (raster_width - raster_diameter) // 2,
            (raster_height - raster_diameter) // 2,
        ),
    )
    if supersample > 1:
        alpha = alpha.resize(
            (target_width, target_height),
            Image.Resampling.LANCZOS,
        )
    image = Image.new("RGBA", (target_width, target_height), _rgba(color))
    image.putalpha(alpha)
    return image


def _draw_rounded_path(
    drawer: ImageDraw.ImageDraw,
    points: tuple[Point, ...],
    *,
    color: Color,
    width: int,
    closed: bool = False,
    round_vertices: bool = True,
) -> None:
    """Draw a high-resolution path with Canvas-like caps and joins.

    Straight icon paths use circles at every vertex to emulate rounded joins.
    Curves represented by a dense sampled polyline disable that behavior so
    only their start and end receive round caps.
    """
    if not points:
        return
    paint = _rgba(color)
    cap_radius = width / 2.0
    path = (*points, points[0]) if closed and len(points) > 1 else points
    if len(path) > 1:
        drawer.line(path, fill=paint, width=width, joint="curve")
    if round_vertices or len(path) == 1:
        cap_points = points
    elif closed:
        cap_points = ()
    else:
        cap_points = (path[0], path[-1])
    for point_x, point_y in cap_points:
        drawer.ellipse(
            (
                int(round(point_x - cap_radius)),
                int(round(point_y - cap_radius)),
                int(round(point_x + cap_radius)),
                int(round(point_y + cap_radius)),
            ),
            fill=paint,
        )


def render_vector_icon(
    *,
    image_size: tuple[int | float, int | float],
    paths: tuple[VectorPath, ...] = (),
    arcs: tuple[VectorArc, ...] = (),
    polygons: tuple[VectorPolygon, ...] = (),
    ellipses: tuple[VectorEllipse, ...] = (),
    rectangles: tuple[VectorRectangle, ...] = (),
) -> Image.Image:
    """Rasterize target-pixel Canvas vector art with bounded supersampling.

    Inputs preserve their Canvas coordinates and colors. The icon is drawn at a
    bounded larger raster size, then LANCZOS downsampled, which gives diagonal,
    curved, and rounded icon edges consistent anti-aliasing on dense displays.
    """
    target_width, target_height = _target_icon_size(image_size)
    supersample = progress_ring_supersample_factor(
        max(target_width, target_height)
    )
    raster_size = (target_width * supersample, target_height * supersample)
    image = Image.new("RGBA", raster_size)
    drawer = ImageDraw.Draw(image)

    def scale_point(point: Point) -> Point:
        return point[0] * supersample, point[1] * supersample

    def scale_bounds(bounds: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
        return tuple(int(round(value * supersample)) for value in bounds)

    def scale_width(width: float) -> int:
        return max(1, int(round(width * supersample)))

    for rectangle in rectangles:
        drawer.rectangle(
            scale_bounds(rectangle.bounds),
            fill=_rgba(rectangle.fill_color),
        )
    for polygon in polygons:
        points = tuple(scale_point(point) for point in polygon.points)
        if polygon.fill_color is not None and points:
            drawer.polygon(points, fill=_rgba(polygon.fill_color))
        if polygon.outline_color is not None:
            _draw_rounded_path(
                drawer,
                points,
                color=polygon.outline_color,
                width=scale_width(polygon.outline_width),
                closed=True,
            )
    for ellipse in ellipses:
        drawer.ellipse(
            scale_bounds(ellipse.bounds),
            fill=(
                _rgba(ellipse.fill_color)
                if ellipse.fill_color is not None
                else None
            ),
            outline=(
                _rgba(ellipse.outline_color)
                if ellipse.outline_color is not None
                else None
            ),
            width=scale_width(ellipse.outline_width),
        )
    for path in paths:
        _draw_rounded_path(
            drawer,
            tuple(scale_point(point) for point in path.points),
            color=path.color,
            width=scale_width(path.width),
            closed=path.closed,
        )
    for arc in arcs:
        center_x, center_y = scale_point(arc.center)
        _draw_rounded_path(
            drawer,
            tuple(
                _arc_points(
                    center=(center_x, center_y),
                    radius=max(0.5, arc.radius * supersample),
                    start_degrees=arc.start_degrees,
                    extent_degrees=arc.extent_degrees,
                )
            ),
            color=arc.color,
            width=scale_width(arc.width),
            round_vertices=False,
        )

    if supersample == 1:
        return image
    return image.resize((target_width, target_height), Image.Resampling.LANCZOS)


def progress_ring_photo(
    widget: object,
    *,
    image_size: int | float,
    ring_diameter: int | float,
    stroke_width: int | float,
    track_color: str | tuple[int, int, int] | tuple[int, int, int, int],
    fill_color: str | tuple[int, int, int] | tuple[int, int, int, int],
    start_degrees: float,
    extent_degrees: float,
) -> object:
    """Create a root-owned Tk photo for one splash progress ring on the UI thread."""
    from PIL import ImageTk

    return ImageTk.PhotoImage(
        render_progress_ring(
            image_size=image_size,
            ring_diameter=ring_diameter,
            stroke_width=stroke_width,
            track_color=track_color,
            fill_color=fill_color,
            start_degrees=start_degrees,
            extent_degrees=extent_degrees,
        ),
        master=widget.winfo_toplevel(),
    )


def progress_control_photo(
    widget: object,
    *,
    image_size: int | float,
    ring_diameter: int | float,
    stroke_width: int | float,
    track_color: Color,
    fill_color: Color,
    start_degrees: float,
    extent_degrees: float,
    center_glyph: ProgressCenterGlyph,
    center_glyph_size: int | float,
    center_glyph_color: Color,
) -> object:
    """Create one Tk photo for a shared progress ring and center affordance."""
    from PIL import ImageTk

    return ImageTk.PhotoImage(
        render_progress_control(
            image_size=image_size,
            ring_diameter=ring_diameter,
            stroke_width=stroke_width,
            track_color=track_color,
            fill_color=fill_color,
            start_degrees=start_degrees,
            extent_degrees=extent_degrees,
            center_glyph=center_glyph,
            center_glyph_size=center_glyph_size,
            center_glyph_color=center_glyph_color,
        ),
        master=widget.winfo_toplevel(),
    )


def vector_icon_photo(
    widget: object,
    *,
    image_size: tuple[int | float, int | float],
    paths: tuple[VectorPath, ...] = (),
    arcs: tuple[VectorArc, ...] = (),
    polygons: tuple[VectorPolygon, ...] = (),
    ellipses: tuple[VectorEllipse, ...] = (),
    rectangles: tuple[VectorRectangle, ...] = (),
) -> object:
    """Create one root-owned Tk photo for anti-aliased Canvas vector art."""
    from PIL import ImageTk

    return ImageTk.PhotoImage(
        render_vector_icon(
            image_size=image_size,
            paths=paths,
            arcs=arcs,
            polygons=polygons,
            ellipses=ellipses,
            rectangles=rectangles,
        ),
        master=widget.winfo_toplevel(),
    )


def retry_icon_photo(
    widget: object,
    *,
    image_size: tuple[int | float, int | float],
    glyph_diameter: int | float,
    color: Color,
) -> object:
    """Create one root-owned photo for the bundled Font Awesome retry glyph."""
    from PIL import ImageTk

    return ImageTk.PhotoImage(
        render_retry_icon(
            image_size=image_size,
            glyph_diameter=glyph_diameter,
            color=color,
        ),
        master=widget.winfo_toplevel(),
    )
