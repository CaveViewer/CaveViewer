"""Shared semantic typography roles for CaveViewer's Tk interfaces."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TkTypography:
    """Runtime-scaled font tuples grouped by their visual role, not widget."""

    display: tuple
    heading: tuple
    body_strong: tuple
    body: tuple
    supporting: tuple
    section: tuple
    medium_family: str
    medium_styles: tuple[str, ...]
    semibold_family: str
    semibold_styles: tuple[str, ...]
    text_scale: float


def create_tk_typography(
    font_family: str,
    *,
    medium_family: str | None = None,
    medium_styles: tuple[str, ...] = (),
    semibold_family: str | None = None,
    semibold_styles: tuple[str, ...] = ("bold",),
    text_scale: float = 1.0,
) -> TkTypography:
    """Build CaveViewer's Tk type scale, applying accessibility scale once."""
    try:
        normalized_scale = max(0.01, float(text_scale))
    except (TypeError, ValueError):
        normalized_scale = 1.0

    def font(points: float, *styles: str) -> tuple:
        size = max(1, int(round(float(points) * normalized_scale)))
        return (font_family, size, *styles)

    return TkTypography(
        display=font(18, "bold"),
        heading=font(14, "bold"),
        body_strong=font(10, "bold"),
        body=font(10),
        supporting=font(9),
        section=font(9, "bold"),
        medium_family=medium_family or font_family,
        medium_styles=tuple(medium_styles),
        semibold_family=semibold_family or font_family,
        semibold_styles=tuple(semibold_styles),
        text_scale=normalized_scale,
    )
