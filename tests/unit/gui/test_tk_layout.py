"""Protect the shared four-pixel Tk shell geometry."""

from caveviewer.gui import tk_layout


def test_reference_shell_resolves_the_svg_content_column():
    assert tk_layout.SHELL_SIDEBAR_WIDTH == 220
    assert tk_layout.SHELL_SIDEBAR_GUTTER == 16
    assert tk_layout.SHELL_MAIN_GUTTER == 32
    assert tk_layout.SHELL_SCROLLBAR_RAIL_WIDTH == 16
    assert tk_layout.SHELL_CONTENT_WIDTH_AT_REFERENCE == 796
    assert (
        tk_layout.SHELL_SIDEBAR_WIDTH + tk_layout.SHELL_MAIN_GUTTER
    ) == 252


def test_shell_spacing_and_control_tiers_follow_the_four_pixel_grid():
    grid_values = (
        tk_layout.SHELL_SIDEBAR_GUTTER,
        tk_layout.SHELL_MAIN_GUTTER,
        tk_layout.SHELL_TOP_INSET,
        tk_layout.PRIMARY_ROW_HEIGHT,
        tk_layout.TABBED_CONTENT_GAP,
        tk_layout.CARD_PADDING,
        tk_layout.CARD_GAP,
        tk_layout.COMPACT_CONTROL_HEIGHT,
        tk_layout.COMPACT_NUMERIC_WIDTH,
        tk_layout.REGULAR_CONTROL_HEIGHT,
        tk_layout.PROMINENT_CONTROL_HEIGHT,
        tk_layout.PROMINENT_ACTION_WIDTH,
        tk_layout.ACTION_GAP,
        tk_layout.FOOTER_GAP,
    )

    assert all(value % tk_layout.TK_GRID_UNIT == 0 for value in grid_values)
    assert tk_layout.CARD_RADIUS == 10
    assert tk_layout.NAV_ICON_SIZE == 18
    assert tk_layout.COMPACT_NUMERIC_WIDTH == 84
