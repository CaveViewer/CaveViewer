"""Protect the scale-independent Help visual and content contract."""

from dataclasses import fields

import pytest

from caveviewer.gui.help_style import (
    HELP_SECTION_CONTENT,
    HELP_VISUAL_METRICS,
    HELP_VISUAL_PALETTE,
    HelpVisualMetrics,
    ScaledHelpVisualMetrics,
    create_help_typography,
    help_section_content,
)
from caveviewer.gui.preferences_style import (
    PREFERENCES_PRIMARY_TEXT,
    PREFERENCES_SUPPORTING_TEXT,
    PREFERENCES_TAB_INDICATOR,
    PREFERENCES_VISUAL_METRICS,
)
from caveviewer.gui.tk_theme import DARK_THEME
from caveviewer.gui.tk_typography import create_tk_typography


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
    assert palette.heading_text == PREFERENCES_PRIMARY_TEXT
    assert palette.supporting_text == PREFERENCES_SUPPORTING_TEXT
    assert palette.tab_active_text == PREFERENCES_PRIMARY_TEXT
    assert palette.tab_inactive_text == PREFERENCES_SUPPORTING_TEXT
    assert palette.tab_indicator == PREFERENCES_TAB_INDICATOR
    assert palette.control_focus_border == DARK_THEME.entry_focus_border
    assert palette.error_text == DARK_THEME.error_text


def test_help_typography_matches_preferences():
    shared = create_tk_typography(
        "Inter",
        medium_family="Inter Medium",
        semibold_family="Inter SemiBold",
        semibold_styles=(),
    )

    typography = create_help_typography(shared, px=round)

    assert typography.active_tab == ("Inter SemiBold", -14)
    assert typography.inactive_tab == ("Inter", -14)
    assert typography.section_title == ("Inter", -16, "bold")
    assert typography.section_summary == ("Inter", -13)
    assert typography.keycap == ("Inter Medium", -14)
    assert typography.action == ("Inter SemiBold", -14)
    assert typography.action_strong == ("Inter SemiBold", -14)
    assert typography.detail == typography.error == ("Inter", -12)


def test_help_typography_applies_display_scaling_once():
    shared = create_tk_typography(
        "Inter",
        medium_family="Inter Medium",
        semibold_family="Inter SemiBold",
        semibold_styles=(),
    )

    typography = create_help_typography(
        shared,
        px=lambda value: round(value * 1.5),
    )

    assert typography.active_tab == ("Inter SemiBold", -21)
    assert typography.section_title == ("Inter", -24, "bold")
    assert typography.section_summary == ("Inter", -20)
    assert typography.keycap == ("Inter Medium", -21)
    assert typography.detail == ("Inter", -18)


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
