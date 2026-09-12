"""Verify measured Help card geometry and rounded canvas composition."""

from types import SimpleNamespace

from caveviewer.gui.help_canvas import (
    CanvasBounds,
    draw_rounded_canvas_box,
    measure_help_card,
    measure_shortcut_row,
    translated_rounded_rectangle_points,
)
from caveviewer.gui.help_style import HELP_VISUAL_METRICS


def _metrics():
    return HELP_VISUAL_METRICS.scaled(round)


def test_help_card_keeps_equal_padding_and_matches_preferences_content_edges():
    metrics = _metrics()

    card = measure_help_card(
        width=800,
        top=20,
        content_bottom=140,
        left=12,
        metrics=metrics,
    )

    assert card.outer == CanvasBounds(12, 20, 800, 162)
    assert card.content == CanvasBounds(34, 42, 778, 140)
    assert card.content.left - card.outer.left == 22
    assert card.outer.right - card.content.right == 22
    assert card.content.top - card.outer.top == 22
    assert card.outer.bottom - card.content.bottom == 22


def test_help_card_clamps_narrow_width_without_collapsing_its_content():
    metrics = _metrics()

    card = measure_help_card(
        width=20,
        top=-4,
        content_bottom=-1,
        left=12,
        metrics=metrics,
    )

    assert card.outer.left == 12
    assert card.outer.top == 0
    assert card.outer.width == 8
    assert card.content.width == 2
    assert card.outer.height == 44


def test_shortcut_row_keeps_aligned_lanes_at_ordinary_width():
    metrics = _metrics()

    row = measure_shortcut_row(
        content_x=34,
        content_width=732,
        metrics=metrics,
    )

    assert row.stacked is False
    assert row.keycap_x == 34
    assert row.keycap_lane_width == 250
    assert row.action_x == 316
    assert row.action_width == 450


def test_shortcut_row_stacks_before_the_action_lane_can_clip():
    metrics = _metrics()

    row = measure_shortcut_row(
        content_x=34,
        content_width=332,
        metrics=metrics,
    )

    assert row.stacked is True
    assert row.keycap_x == row.action_x == 34
    assert row.keycap_lane_width == 170
    assert row.action_width == 332


def test_rounded_points_translate_the_shared_path_without_changing_its_bounds():
    bounds = CanvasBounds(12, 20, 112, 80)

    points = translated_rounded_rectangle_points(bounds, 12)
    xs = points[0::2]
    ys = points[1::2]

    assert min(xs) == bounds.left
    assert max(xs) == bounds.right
    assert min(ys) == bounds.top
    assert max(ys) == bounds.bottom


def test_rounded_box_is_lowered_below_its_measured_foreground():
    calls = []
    canvas = SimpleNamespace(
        create_polygon=lambda *args, **kwargs: calls.append((args, kwargs)) or 42,
        tag_lower=lambda *args: calls.append(("lower", args)),
    )

    item = draw_rounded_canvas_box(
        canvas,
        CanvasBounds(12, 20, 112, 80),
        radius=12,
        fill="#12121a",
        tags=("help-content", "help-card"),
        below=99,
    )

    assert item == 42
    polygon_options = calls[0][1]
    assert polygon_options["smooth"] is True
    assert polygon_options["splinesteps"] == 24
    assert polygon_options["tags"] == ("help-content", "help-card")
    assert calls[1] == ("lower", (42, 99))
