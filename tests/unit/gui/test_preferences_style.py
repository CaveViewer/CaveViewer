"""Protect the scale-independent Preferences visual contract."""

from dataclasses import fields

from caveviewer.gui.preferences_style import (
    PREFERENCES_VISUAL_METRICS,
    PREFERENCES_VISUAL_PALETTE,
    PREFERENCES_PRIMARY_TEXT,
    PREFERENCES_SUPPORTING_TEXT,
    PreferencesVisualMetrics,
    ScaledPreferencesVisualMetrics,
    create_preferences_typography,
)
from caveviewer.gui.tk_theme import DARK_THEME
from caveviewer.gui.tk_typography import create_tk_typography


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
    assert palette.heading_text == PREFERENCES_PRIMARY_TEXT == "#EFF1F5"
    assert palette.field_label_text == PREFERENCES_PRIMARY_TEXT
    assert palette.control_text == PREFERENCES_PRIMARY_TEXT
    assert palette.tab_active_text == PREFERENCES_PRIMARY_TEXT
    assert palette.supporting_text == PREFERENCES_SUPPORTING_TEXT == "#A9AFBC"
    assert palette.tab_inactive_text == PREFERENCES_SUPPORTING_TEXT
    assert palette.disabled_action_text == DARK_THEME.placeholder_text


def test_preferences_typography_matches_the_streaming_reference():
    shared = create_tk_typography(
        "Inter",
        medium_family="Inter Medium",
        medium_styles=(),
        semibold_family="Inter SemiBold",
        semibold_styles=(),
    )

    typography = create_preferences_typography(shared, px=round)

    assert typography.active_tab == ("Inter SemiBold", -14)
    assert typography.inactive_tab == ("Inter", -14)
    assert typography.section_title == ("Inter", -16, "bold")
    assert typography.section_summary == ("Inter", -13)
    assert typography.field_label == ("Inter SemiBold", -14)
    assert typography.field_description == ("Inter", -12)
    assert typography.field_value == ("Inter Medium", -14)
    assert typography.field_unit == ("Inter", -14)


def test_preferences_typography_applies_display_scaling_once():
    shared = create_tk_typography(
        "Inter",
        medium_family="Inter Medium",
        semibold_family="Inter SemiBold",
        semibold_styles=(),
    )

    typography = create_preferences_typography(
        shared,
        px=lambda value: round(value * 1.5),
    )

    assert typography.active_tab == ("Inter SemiBold", -21)
    assert typography.section_title == ("Inter", -24, "bold")
    assert typography.section_summary == ("Inter", -20)
    assert typography.field_description == ("Inter", -18)
    assert typography.field_value == ("Inter Medium", -21)


def test_preferences_typography_preserves_the_runtime_text_scale():
    shared = create_tk_typography(
        "Inter",
        medium_family="Inter Medium",
        semibold_family="Inter SemiBold",
        semibold_styles=(),
        text_scale=1.25,
    )

    typography = create_preferences_typography(shared, px=round)

    assert typography.active_tab == ("Inter SemiBold", -18)
    assert typography.section_title == ("Inter", -20, "bold")
    assert typography.field_description == ("Inter", -15)
    assert typography.field_value == ("Inter Medium", -18)
