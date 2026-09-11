"""Protect the scale-independent Preferences visual contract."""

from dataclasses import fields

from caveviewer.gui.preferences_style import (
    PREFERENCES_TYPOGRAPHY_ROLES,
    PREFERENCES_VISUAL_METRICS,
    PREFERENCES_VISUAL_PALETTE,
    PreferencesVisualMetrics,
    ScaledPreferencesVisualMetrics,
)
from caveviewer.gui.tk_theme import DARK_THEME


def test_preferences_visual_metrics_match_the_reference_contract():
    metrics = PREFERENCES_VISUAL_METRICS

    assert metrics.control_corner_radius == 4
    assert metrics.section_corner_radius == 12
    assert metrics.section_padding_x == 22
    assert metrics.section_padding_y == 22
    assert metrics.section_gap_y == 20
    assert metrics.section_heading_to_description_y == 8
    assert metrics.section_description_to_fields_y == 20
    assert metrics.card_item_gap_y == 20
    assert metrics.control_height == 40
    assert metrics.numeric_control_width == 100
    assert metrics.action_min_width == 140
    assert metrics.control_content_pad_x == 12
    assert metrics.footer_action_gap_x == 10


def test_preferences_visual_metrics_apply_display_scaling_once():
    calls: list[int | float] = []

    def px(value: int | float) -> int:
        calls.append(value)
        return round(value * 1.5)

    scaled = PREFERENCES_VISUAL_METRICS.scaled(px)

    assert isinstance(scaled, ScaledPreferencesVisualMetrics)
    assert len(calls) == len(fields(PreferencesVisualMetrics))
    assert scaled.control_corner_radius == 6
    assert scaled.section_corner_radius == 18
    assert scaled.section_padding_x == 33
    assert not hasattr(scaled, "scaled")


def test_preferences_palette_uses_existing_semantic_theme_colors():
    palette = PREFERENCES_VISUAL_PALETTE

    assert palette.surface_background == DARK_THEME.background
    assert palette.section_background == DARK_THEME.panel
    assert palette.control_focus_border == DARK_THEME.entry_focus_border
    assert palette.control_invalid_border == DARK_THEME.invalid_border
    assert palette.tab_active_text == DARK_THEME.primary_button
    assert palette.disabled_action_text == DARK_THEME.placeholder_text


def test_preferences_typography_uses_only_shared_semantic_roles():
    roles = PREFERENCES_TYPOGRAPHY_ROLES

    assert roles.active_tab == "body_strong"
    assert roles.inactive_tab == "body"
    assert roles.section_heading == "heading"
    assert roles.section_description == "supporting"
    assert roles.field_label == "body_strong"
    assert roles.field_description == "supporting"
    assert roles.action == "body_strong"
