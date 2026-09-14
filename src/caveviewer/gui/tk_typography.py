"""Shared semantic typography roles for CaveViewer's Tk interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caveviewer.gui.platform.presentation import PresentationProfile


@dataclass(frozen=True)
class TkTypography:
    """Runtime-scaled logical-pixel fonts grouped by visual role."""

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
    display_scale: float


def resolve_tk_text_scale(root, profile: PresentationProfile) -> float:
    """Return the app text multiplier derived from the current Tk defaults."""
    default_font_points = 12.0
    if profile.uses_tk_default_font_scale:
        try:
            import tkinter.font as tkfont

            default_font = tkfont.nametofont("TkDefaultFont", root=root)
            default_font_points = abs(
                float(default_font.actual("size") or default_font_points)
            )
        except Exception:
            pass
    return profile.tk_text_scale(default_font_points)


def create_tk_typography(
    font_family: str,
    *,
    medium_family: str | None = None,
    medium_styles: tuple[str, ...] = (),
    semibold_family: str | None = None,
    semibold_styles: tuple[str, ...] = ("bold",),
    text_scale: float = 1.0,
    display_scale: float = 1.0,
) -> TkTypography:
    """Build CaveViewer's Tk type scale in display-scaled logical pixels."""
    try:
        normalized_text_scale = max(0.01, float(text_scale))
    except (TypeError, ValueError):
        normalized_text_scale = 1.0
    try:
        normalized_display_scale = max(0.01, float(display_scale))
    except (TypeError, ValueError):
        normalized_display_scale = 1.0

    def font(logical_pixels: float, *styles: str) -> tuple:
        size = max(
            1,
            int(
                round(
                    float(logical_pixels)
                    * normalized_text_scale
                    * normalized_display_scale
                )
            ),
        )
        # Tk treats negative font sizes as pixels. Positive sizes are points
        # and would reintroduce platform-dependent point conversion here.
        return font_family, -size, *styles

    return TkTypography(
        display=font(24, "bold"),
        heading=font(19, "bold"),
        body_strong=font(13, "bold"),
        body=font(13),
        supporting=font(12),
        section=font(12, "bold"),
        medium_family=medium_family or font_family,
        medium_styles=tuple(medium_styles),
        semibold_family=semibold_family or font_family,
        semibold_styles=tuple(semibold_styles),
        text_scale=normalized_text_scale,
        display_scale=normalized_display_scale,
    )
