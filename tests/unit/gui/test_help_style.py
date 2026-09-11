"""Protect the scale-independent Help visual and content contract."""

from dataclasses import fields

import pytest

from caveviewer.gui.help_style import (
    HELP_SECTION_CONTENT,
    HELP_TYPOGRAPHY_ROLES,
    HELP_VISUAL_METRICS,
    HELP_VISUAL_PALETTE,
    HelpVisualMetrics,
    ScaledHelpVisualMetrics,
    help_section_content,
)
from caveviewer.gui.preferences_style import PREFERENCES_VISUAL_METRICS
from caveviewer.gui.tk_theme import DARK_THEME


def test_help_metrics_align_with_the_approved_preferences_pattern():
    metrics = HELP_VISUAL_METRICS
    preferences = PREFERENCES_VISUAL_METRICS

    assert metrics.control_corner_radius == preferences.control_corner_radius == 4
    assert metrics.section_corner_radius == preferences.section_corner_radius == 12
    assert metrics.section_padding_x == preferences.section_padding_x == 22
    assert metrics.section_padding_y == preferences.section_padding_y == 22
    assert metrics.section_gap_y == preferences.section_gap_y == 20
    assert metrics.card_item_gap_y == preferences.card_item_gap_y == 20
    assert metrics.section_heading_to_description_y == 8
    assert metrics.section_description_to_content_y == 20


def test_help_metrics_apply_display_scaling_once():
    calls: list[int | float] = []

    def px(value: int | float) -> int:
        calls.append(value)
        return round(value * 1.5)

    scaled = HELP_VISUAL_METRICS.scaled(px)

    assert isinstance(scaled, ScaledHelpVisualMetrics)
    assert len(calls) == len(fields(HelpVisualMetrics))
    assert scaled.control_corner_radius == 6
    assert scaled.section_corner_radius == 18
    assert scaled.section_padding_x == 33
    assert scaled.keycap_lane_min_width == 255
    assert not hasattr(scaled, "scaled")


def test_help_palette_uses_existing_semantic_theme_colors():
    palette = HELP_VISUAL_PALETTE

    assert palette.surface_background == DARK_THEME.background
    assert palette.section_background == DARK_THEME.panel
    assert palette.heading_text == DARK_THEME.body_text
    assert palette.supporting_text == DARK_THEME.secondary_text
    assert palette.tab_active_text == DARK_THEME.primary_button
    assert palette.control_focus_border == DARK_THEME.entry_focus_border
    assert palette.error_text == DARK_THEME.error_text


def test_help_typography_uses_only_shared_semantic_roles():
    roles = HELP_TYPOGRAPHY_ROLES

    assert roles.active_tab == "body_strong"
    assert roles.inactive_tab == "body"
    assert roles.section_heading == "heading"
    assert roles.section_description == "supporting"
    assert roles.keycap == "body_strong"
    assert roles.action == "body"
    assert roles.action_strong == "body_strong"
    assert roles.detail == roles.error == "supporting"


def test_help_section_content_covers_each_declared_card_once():
    keys = [(content.tab_key, content.section_id) for content in HELP_SECTION_CONTENT]

    assert len(keys) == len(set(keys)) == 9
    assert help_section_content("keys", "move").purpose == (
        "Control movement direction and speed."
    )
    assert help_section_content("capture", "cave-slice").purpose == (
        "Save part of a cave as a new pre-compiled map."
    )
    assert help_section_content("troubleshooting", "last-error").title == "Last Error"


def test_help_section_content_rejects_an_unknown_card():
    with pytest.raises(KeyError):
        help_section_content("keys", "missing")
