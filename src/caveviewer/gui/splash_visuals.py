"""Raster visual primitives owned by the Tk splash and library presentation.

The Tk presentation modules stay responsible for widget creation and mutation.
This module prepares background, progress-ring, and vector-icon images with
Pillow so Canvas presentations can remain crisp on high-density displays
without each caller implementing its own pixel geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TypeAlias

from PIL import Image, ImageColor, ImageDraw, ImageOps


_PROGRESS_RING_MAX_SUPERSAMPLE = 4
_PROGRESS_RING_MAX_RASTER_SIZE = 2048

Color: TypeAlias = str | tuple[int, int, int] | tuple[int, int, int, int]
Point: TypeAlias = tuple[float, float]


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


def fit_splash_background(
    source: Image.Image,
    *,
    size: tuple[int, int],
) -> Image.Image:
    """Cover one splash target with a centered, high-quality image crop."""
    width = max(1, int(size[0]))
    height = max(1, int(size[1]))
    return ImageOps.fit(
        source.convert("RGB"),
        (width, height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


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
    center: float,
    radius: float,
    start_degrees: float,
    extent_degrees: float,
) -> list[tuple[float, float]]:
    """Return Canvas-compatible clockwise/counter-clockwise arc points."""
    steps = max(2, int(math.ceil(abs(extent_degrees) * max(1.0, radius) / 12)))
    points: list[tuple[float, float]] = []
    for index in range(steps + 1):
        angle = math.radians(start_degrees + extent_degrees * index / steps)
        points.append(
            (
                center + radius * math.cos(angle),
                center - radius * math.sin(angle),
            )
        )
    return points


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
            center=center,
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

    if supersample == 1:
        return image
    return image.resize((target_size, target_size), Image.Resampling.LANCZOS)


def _target_icon_size(
    image_size: tuple[int | float, int | float],
) -> tuple[int, int]:
    """Normalize a target Canvas image size without allowing zero dimensions."""
    return (
        max(1, int(round(float(image_size[0])))),
        max(1, int(round(float(image_size[1])))),
    )


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
                    center=center_x,
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
