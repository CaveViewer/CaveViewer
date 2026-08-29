"""Raster visual primitives owned by the Tk splash presentation.

The splash screen stays responsible for Tk widget creation and mutation. This
module prepares background and progress-ring images with Pillow so Canvas
presentations can remain crisp on high-density displays without each caller
implementing its own pixel geometry.
"""

from __future__ import annotations

import math

from PIL import Image, ImageColor, ImageDraw, ImageOps


_PROGRESS_RING_MAX_SUPERSAMPLE = 4
_PROGRESS_RING_MAX_RASTER_SIZE = 2048


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


def _rgba(
    color: str | tuple[int, int, int] | tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
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
