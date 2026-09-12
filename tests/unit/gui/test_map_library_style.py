"""Tests for the Map Library's semantic visual contract."""

from caveviewer.gui.map_library_style import (
    MAP_LIBRARY_CATALOG_CARD_BORDER,
    MAP_LIBRARY_DESCRIPTION_FG,
    MAP_LIBRARY_MAIN_LEFT_GAP,
    MAP_LIBRARY_MUTED_FG,
    MAP_LIBRARY_PANEL_FILL,
    MAP_LIBRARY_PRIMARY_FG,
    MAP_LIBRARY_RECENT_CARD_BORDER,
    MAP_LIBRARY_RIGHT_MARGIN,
    MAP_LIBRARY_SCROLLBAR_RAIL_WIDTH,
    MAP_LIBRARY_TITLE_FG,
    MAP_LIBRARY_TYPOGRAPHY_ROLES,
    MAP_LIBRARY_VISUAL_METRICS,
    MAP_LIBRARY_WINDOW_FILL,
    OPEN_ANOTHER_LOCAL_MAP_TITLE,
    OPEN_LOCAL_MAP_DESCRIPTION,
    OPEN_LOCAL_MAP_TITLE,
    SECTION_HEADING_FONT_SCALE,
    create_map_library_panel_style,
    map_library_visual_palette,
)
from caveviewer.gui.tk_theme import DARK_THEME
from caveviewer.gui.tk_typography import create_tk_typography


def test_map_library_metrics_scale_every_logical_value_once():
    calls = []

    def px(value):
        calls.append(value)
        return int(value * 2)

    scaled = MAP_LIBRARY_VISUAL_METRICS.scaled(px)

    assert len(calls) == len(MAP_LIBRARY_VISUAL_METRICS.__dataclass_fields__)
    assert scaled.surface_top_pad_y == 48
    assert scaled.section_corner_radius == 20
    assert scaled.section_padding_x == 48
    assert scaled.section_padding_y == 40
    assert scaled.section_padding_bottom_y == 50
    assert scaled.section_gap_y == 48
    assert scaled.recent_card_min_height == 276
    assert scaled.catalog_card_min_height == 968
    assert scaled.local_action_icon_width == 64
    assert scaled.compact_local_action_height == 56
    assert scaled.compact_local_action_icon_width == 40
    assert scaled.recent_rows_to_local_action_y == 24
    assert scaled.action_button_size == 56
    assert scaled.progress_height == 6


def test_map_library_reference_frame_resolves_exact_card_coordinates():
    window_width = 1080
    sidebar_width = 220
    main_x = sidebar_width + MAP_LIBRARY_MAIN_LEFT_GAP
    card_width = (
        window_width
        - main_x
        - MAP_LIBRARY_RIGHT_MARGIN
    )
    catalog_y = (
        MAP_LIBRARY_VISUAL_METRICS.surface_top_pad_y
        + MAP_LIBRARY_VISUAL_METRICS.recent_card_min_height
        + MAP_LIBRARY_VISUAL_METRICS.section_gap_y
    )

    assert MAP_LIBRARY_SCROLLBAR_RAIL_WIDTH == 14
    assert main_x == 246
    assert card_width == 806
    assert (
        MAP_LIBRARY_VISUAL_METRICS.surface_top_pad_y
        == MAP_LIBRARY_VISUAL_METRICS.section_gap_y
        == 24
    )
    assert catalog_y == 186
    assert catalog_y + MAP_LIBRARY_VISUAL_METRICS.catalog_card_min_height == 670


def test_map_library_palette_uses_semantic_theme_roles():
    palette = map_library_visual_palette()

    assert palette.surface_background == MAP_LIBRARY_WINDOW_FILL == "#0D0F13"
    assert MAP_LIBRARY_PANEL_FILL == "#15171C"
    assert palette.section_background == MAP_LIBRARY_PANEL_FILL
    assert palette.action_background == MAP_LIBRARY_PANEL_FILL
    assert palette.disabled_action_background == MAP_LIBRARY_PANEL_FILL
    assert palette.section_border == MAP_LIBRARY_RECENT_CARD_BORDER == "#30343D"
    assert (
        palette.catalog_section_border
        == MAP_LIBRARY_CATALOG_CARD_BORDER
        == "#313337"
    )
    assert palette.section_heading_text == DARK_THEME.body_text
    assert palette.map_title_text == MAP_LIBRARY_TITLE_FG == "#EFF1F5"
    assert palette.local_action_text == MAP_LIBRARY_PRIMARY_FG == "#F5C451"
    assert palette.disclosure_text == MAP_LIBRARY_PRIMARY_FG
    assert palette.local_action_supporting_text == MAP_LIBRARY_MUTED_FG
    assert palette.supporting_text == MAP_LIBRARY_DESCRIPTION_FG == "#B6BCC8"
    assert palette.file_size_text == MAP_LIBRARY_MUTED_FG == "#A9AFBC"
    assert palette.overflow_foreground == MAP_LIBRARY_MUTED_FG
    assert palette.error_text == DARK_THEME.error_text


def test_map_library_panel_style_resolves_metrics_fonts_and_progress_colors():
    typography = create_tk_typography("CaveViewer Test")

    style = create_map_library_panel_style(
        px=lambda value: int(round(value * 1.5)),
        typography=typography,
        progress_track_color="#111111",
        progress_fill_color="#ffaa00",
    )

    assert style.metrics.section_padding_y == 30
    assert style.metrics.section_padding_bottom_y == 38
    assert style.metrics.local_action_height == 75
    assert style.metrics.compact_local_action_height == 42
    assert SECTION_HEADING_FONT_SCALE == 0.8
    assert style.section_font == (
        typography.heading[0],
        round(typography.heading[1] * SECTION_HEADING_FONT_SCALE),
        *typography.heading[2:],
    )
    assert style.section_font[1] < typography.heading[1]
    assert style.title_font == (typography.body_strong[0], -21, "bold")
    assert style.local_action_font == (typography.body[0], -21)
    assert style.supporting_font == (typography.supporting[0], -18)
    assert style.local_action_supporting_color == "#A9AFBC"
    assert style.file_size_color == "#A9AFBC"
    assert style.progress_track_color == "#111111"
    assert style.progress_fill_color == "#ffaa00"


def test_map_library_windows_titles_use_the_semibold_face_at_14_pixels():
    typography = create_tk_typography(
        "Segoe UI",
        semibold_family="Segoe UI Semibold",
        semibold_styles=(),
    )

    style = create_map_library_panel_style(
        px=lambda value: int(round(value * 1.5)),
        typography=typography,
        progress_track_color="#111111",
        progress_fill_color="#ffaa00",
    )

    assert style.title_font == ("Segoe UI Semibold", -21)
    assert style.body_font == ("Segoe UI", -18)
    assert style.supporting_font == ("Segoe UI", -18)


def test_map_library_copy_and_typography_match_the_prototype():
    assert OPEN_LOCAL_MAP_TITLE == "Open a local map"
    assert OPEN_ANOTHER_LOCAL_MAP_TITLE == "Open another local map"
    assert OPEN_LOCAL_MAP_DESCRIPTION == "Browse a cave map folder on your computer."
    assert MAP_LIBRARY_TYPOGRAPHY_ROLES.section_heading == "heading"
    assert MAP_LIBRARY_TYPOGRAPHY_ROLES.map_title == "body_strong"
    assert MAP_LIBRARY_TYPOGRAPHY_ROLES.compact_local_action == "body"
