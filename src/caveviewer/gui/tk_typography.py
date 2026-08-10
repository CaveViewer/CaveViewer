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


def create_tk_typography(
    font_family: str,
    *,
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
        display=font(20, "bold"),
        heading=font(16, "bold"),
        body_strong=font(12, "bold"),
        body=font(12),
        supporting=font(10),
        section=font(10, "bold"),
    )
