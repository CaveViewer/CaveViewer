"""Semantic visual tokens for the CaveViewer Preferences surface."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Callable

from caveviewer.gui.tk_theme import DARK_THEME, TkTheme
from caveviewer.gui.tk_typography import TkTypography


PREFERENCES_PRIMARY_TEXT = "#EFF1F5"
PREFERENCES_SUPPORTING_TEXT = "#A9AFBC"
PREFERENCES_TAB_INDICATOR = "#30343D"


@dataclass(frozen=True, slots=True)
class ScaledPreferencesVisualMetrics:
    """Preferences geometry after one logical-to-display conversion."""

    control_corner_radius: int
    section_corner_radius: int
    section_padding_x: int
    section_padding_y: int
    section_gap_y: int
    section_heading_to_fields_y: int
    section_heading_to_description_y: int
    section_description_to_fields_y: int
    field_label_to_description_y: int
    field_description_to_control_y: int
    control_height: int
    numeric_control_width: int
    action_min_width: int
    control_content_pad_x: int
    unit_gap_x: int
    control_seam_thickness: int
    card_item_gap_y: int
    footer_action_gap_x: int
    footer_top_gap_y: int
    control_border_thickness: int
    focus_border_thickness: int


@dataclass(frozen=True, slots=True)
class PreferencesVisualMetrics:
    """Unscaled logical geometry measured from the Preferences reference."""

    control_corner_radius: int = 4
    section_corner_radius: int = 12
    section_padding_x: int = 22
    section_padding_y: int = 22
    section_gap_y: int = 20
    section_heading_to_fields_y: int = 24
    section_heading_to_description_y: int = 8
    section_description_to_fields_y: int = 20
    field_label_to_description_y: int = 6
    field_description_to_control_y: int = 14
    control_height: int = 40
    numeric_control_width: int = 100
    action_min_width: int = 140
    control_content_pad_x: int = 12
    unit_gap_x: int = 10
    control_seam_thickness: int = 1
    card_item_gap_y: int = 20
    footer_action_gap_x: int = 10
    footer_top_gap_y: int = 20
    control_border_thickness: int = 1
    focus_border_thickness: int = 2

    def scaled(
        self,
        px: Callable[[int | float], int],
    ) -> ScaledPreferencesVisualMetrics:
        """Convert every logical metric exactly once for one composed surface."""
        values = {
            field.name: px(getattr(self, field.name))
            for field in fields(self)
        }
        return ScaledPreferencesVisualMetrics(**values)


@dataclass(frozen=True, slots=True)
class PreferencesVisualPalette:
    """Semantic colors used by Preferences cards, fields, tabs, and actions."""

    surface_background: str
    section_background: str
    heading_text: str
    field_label_text: str
    supporting_text: str
    control_background: str
    control_border: str
    control_focus_border: str
    control_invalid_border: str
    control_text: str
    control_placeholder_text: str
    tab_active_text: str
    tab_inactive_text: str
    primary_action_background: str
    primary_action_hover_background: str
    primary_action_pressed_background: str
    primary_action_border: str
    primary_action_text: str
    secondary_action_background: str
    secondary_action_hover_background: str
    secondary_action_pressed_background: str
    secondary_action_border: str
    secondary_action_text: str
    disabled_action_background: str
    disabled_action_border: str
    disabled_action_text: str


@dataclass(frozen=True, slots=True)
class PreferencesTypography:
    """Exact display-scaled font tuples for the Preferences hierarchy."""

    active_tab: tuple
    inactive_tab: tuple
    section_title: tuple
    section_summary: tuple
    field_label: tuple
    field_description: tuple
    field_value: tuple
    field_unit: tuple


def create_preferences_typography(
    typography: TkTypography,
    *,
    px: Callable[[int | float], int],
) -> PreferencesTypography:
    """Build the Preferences type hierarchy from registered Inter faces."""

    regular_family = str(typography.body[0])

    def font(family: str, size: int, *styles: str) -> tuple:
        pixel_size = -max(1, int(px(size * typography.text_scale)))
        return (family, pixel_size, *styles)

    return PreferencesTypography(
        active_tab=font(
            typography.semibold_family,
            14,
            *typography.semibold_styles,
        ),
        inactive_tab=font(regular_family, 14),
        section_title=font(regular_family, 16, "bold"),
        section_summary=font(regular_family, 13),
        field_label=font(
            typography.semibold_family,
            14,
            *typography.semibold_styles,
        ),
        field_description=font(regular_family, 12),
        field_value=font(
            typography.medium_family,
            14,
            *typography.medium_styles,
        ),
        field_unit=font(regular_family, 14),
    )


def preferences_visual_palette(
    theme: TkTheme = DARK_THEME,
) -> PreferencesVisualPalette:
    """Resolve the Preferences contract through the active semantic Tk theme."""
    return PreferencesVisualPalette(
        surface_background=theme.background,
        section_background=theme.panel,
        heading_text=PREFERENCES_PRIMARY_TEXT,
        field_label_text=PREFERENCES_PRIMARY_TEXT,
        supporting_text=PREFERENCES_SUPPORTING_TEXT,
        control_background=theme.entry_background,
        control_border=theme.entry_border,
        control_focus_border=theme.entry_focus_border,
        control_invalid_border=theme.invalid_border,
        control_text=PREFERENCES_PRIMARY_TEXT,
        control_placeholder_text=theme.placeholder_text,
        tab_active_text=PREFERENCES_PRIMARY_TEXT,
        tab_inactive_text=PREFERENCES_SUPPORTING_TEXT,
        primary_action_background=theme.primary_button,
        primary_action_hover_background=theme.primary_button_hover,
        primary_action_pressed_background=theme.primary_button_border,
        primary_action_border=theme.primary_button_border,
        primary_action_text=theme.primary_button_text,
        secondary_action_background=theme.secondary_button,
        secondary_action_hover_background=theme.secondary_button_hover,
        secondary_action_pressed_background=theme.secondary_button_border,
        secondary_action_border=theme.secondary_button_border,
        secondary_action_text=theme.body_text,
        disabled_action_background=theme.secondary_button,
        disabled_action_border=theme.entry_border,
        disabled_action_text=theme.placeholder_text,
    )


PREFERENCES_VISUAL_METRICS = PreferencesVisualMetrics()
PREFERENCES_VISUAL_PALETTE = preferences_visual_palette()
