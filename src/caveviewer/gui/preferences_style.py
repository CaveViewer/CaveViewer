"""Semantic visual tokens for the CaveViewer Preferences surface."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Callable

from caveviewer.gui.tk_theme import DARK_THEME, TkTheme


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
class PreferencesTypographyRoles:
    """Names of the shared Tk typography roles used by Preferences."""

    active_tab: str = "body_strong"
    inactive_tab: str = "body"
    section_heading: str = "heading"
    section_description: str = "supporting"
    field_label: str = "body_strong"
    field_description: str = "supporting"
    field_value: str = "body"
    field_unit: str = "body"
    action: str = "body_strong"
    feedback: str = "supporting"


def preferences_visual_palette(
    theme: TkTheme = DARK_THEME,
) -> PreferencesVisualPalette:
    """Resolve the Preferences contract through the active semantic Tk theme."""
    return PreferencesVisualPalette(
        surface_background=theme.background,
        section_background=theme.panel,
        heading_text=theme.body_text,
        field_label_text=theme.body_text,
        supporting_text=theme.secondary_text,
        control_background=theme.entry_background,
        control_border=theme.entry_border,
        control_focus_border=theme.entry_focus_border,
        control_invalid_border=theme.invalid_border,
        control_text=theme.body_text,
        control_placeholder_text=theme.placeholder_text,
        tab_active_text=theme.primary_button,
        tab_inactive_text=theme.secondary_text,
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
PREFERENCES_TYPOGRAPHY_ROLES = PreferencesTypographyRoles()
PREFERENCES_VISUAL_PALETTE = preferences_visual_palette()
