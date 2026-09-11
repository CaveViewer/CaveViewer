"""Measured rounded-card drawing primitives for the canvas-based Help surface."""

from __future__ import annotations

from dataclasses import dataclass

from caveviewer.gui.help_style import ScaledHelpVisualMetrics
from caveviewer.gui.preferences_controls import rounded_rectangle_points


@dataclass(frozen=True, slots=True)
class CanvasBounds:
    """One normalized canvas rectangle."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)


@dataclass(frozen=True, slots=True)
class HelpCardGeometry:
    """Outer card and inner content bounds measured in display pixels."""

    outer: CanvasBounds
    content: CanvasBounds


@dataclass(frozen=True, slots=True)
class ShortcutRowGeometry:
    """Responsive keycap and action lanes for one shortcut row."""

    keycap_x: int
    keycap_lane_width: int
    action_x: int
    action_width: int
    stacked: bool


def measure_help_card(
    *,
    width: int,
    top: int,
    content_bottom: int,
    left: int,
    metrics: ScaledHelpVisualMetrics,
) -> HelpCardGeometry:
    """Measure one card after its wrapped foreground content has been drawn."""

    outer_left = max(0, int(left))
    outer_top = max(0, int(top))
    outer_right = max(outer_left + 1, int(width) - metrics.content_right_pad_x)
    horizontal_padding = min(
        metrics.section_padding_x,
        max(0, (outer_right - outer_left - 1) // 2),
    )
    content_left = outer_left + horizontal_padding
    content_right = max(content_left + 1, outer_right - horizontal_padding)
    resolved_content_bottom = max(
        outer_top + metrics.section_padding_y,
        int(content_bottom),
    )
    outer_bottom = resolved_content_bottom + metrics.section_padding_y
    return HelpCardGeometry(
        outer=CanvasBounds(outer_left, outer_top, outer_right, outer_bottom),
        content=CanvasBounds(
            content_left,
            outer_top + metrics.section_padding_y,
            content_right,
            resolved_content_bottom,
        ),
    )


def measure_shortcut_row(
    *,
    content_x: int,
    content_width: int,
    metrics: ScaledHelpVisualMetrics,
) -> ShortcutRowGeometry:
    """Keep two lanes when they fit and stack them before either can clip."""

    resolved_width = max(1, int(content_width))
    keycap_lane_width = min(
        metrics.keycap_lane_max_width,
        max(metrics.keycap_lane_min_width, round(resolved_width * 0.37)),
    )
    remaining_action_width = (
        resolved_width - keycap_lane_width - metrics.action_lane_gap_x
    )
    stacked = remaining_action_width < metrics.action_min_width
    return ShortcutRowGeometry(
        keycap_x=content_x,
        keycap_lane_width=keycap_lane_width,
        action_x=(
            content_x
            if stacked
            else content_x + keycap_lane_width + metrics.action_lane_gap_x
        ),
        action_width=(resolved_width if stacked else remaining_action_width),
        stacked=stacked,
    )


def translated_rounded_rectangle_points(
    bounds: CanvasBounds,
    radius: int,
    *,
    inset: int = 0,
) -> tuple[int, ...]:
    """Return shared rounded-rectangle points translated into canvas space."""

    points = rounded_rectangle_points(
        bounds.width,
        bounds.height,
        radius,
        inset=inset,
    )
    return tuple(
        coordinate + (bounds.left if index % 2 == 0 else bounds.top)
        for index, coordinate in enumerate(points)
    )


def draw_rounded_canvas_box(
    canvas,
    bounds: CanvasBounds,
    *,
    radius: int,
    fill: str,
    outline: str = "",
    border_width: int = 0,
    tags: str | tuple[str, ...] = "help-content",
    below: int | str | None = None,
) -> int:
    """Draw one rounded canvas surface and optionally lower it below content."""

    item = canvas.create_polygon(
        *translated_rounded_rectangle_points(
            bounds,
            radius,
            inset=max(0, border_width // 2),
        ),
        fill=fill,
        outline=outline,
        width=border_width,
        smooth=True,
        splinesteps=24,
        tags=tags,
    )
    if below is not None:
        canvas.tag_lower(item, below)
    return item
