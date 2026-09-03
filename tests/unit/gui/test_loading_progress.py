"""Tests for the shared routine-loading progress contract."""

from __future__ import annotations

import pytest

from caveviewer.gui.loading_progress import (
    OPENGL_PROGRESS_LAYOUT_SCALE_MAX,
    ROUTINE_PROGRESS_BAR_HEIGHT,
    ROUTINE_PROGRESS_BAR_WIDTH,
    ROUTINE_PROGRESS_BAR_TO_DESCRIPTION_GAP,
    ROUTINE_PROGRESS_TITLE_TO_BAR_GAP,
    circular_progress_ranges,
    clamp_progress,
    hex_color_rgb,
    monotonic_progress,
    progress_layout_scale,
    progress_segments,
    routine_progress_layout,
)


def test_routine_loading_geometry_is_shared_across_renderers():
    assert ROUTINE_PROGRESS_BAR_WIDTH == 300.0
    assert ROUTINE_PROGRESS_BAR_HEIGHT == 4.0
    assert ROUTINE_PROGRESS_TITLE_TO_BAR_GAP == 40.0
    assert ROUTINE_PROGRESS_BAR_TO_DESCRIPTION_GAP == 30.0


def test_routine_loading_layout_uses_visible_gaps_and_collapses_description():
    with_description = routine_progress_layout(
        center_x=400.0,
        center_y=300.0,
        title_height=10.0,
        description_height=5.0,
        has_description=True,
    )
    without_description = routine_progress_layout(
        center_x=400.0,
        center_y=300.0,
        title_height=10.0,
    )

    assert with_description.bar_top - with_description.title_bottom == 40.0
    assert with_description.description_top is not None
    assert with_description.description_top - with_description.bar_bottom == 30.0
    assert without_description.description_top is None
    assert without_description.description_bottom is None
    assert without_description.bar_top - without_description.title_bottom == 40.0
    assert without_description.bar_left == 250.0
    assert without_description.bar_right == 550.0


@pytest.mark.parametrize(
    ("value", "expected"),
    ((-1.0, 0.0), (0.4, 0.4), (2.0, 1.0), (float("nan"), 0.0)),
)
def test_clamp_progress_bounds_values(value, expected):
    assert clamp_progress(value) == expected


def test_monotonic_progress_does_not_regress():
    assert monotonic_progress(0.7, 0.2) == 0.7
    assert monotonic_progress(0.7, 0.9) == 0.9


def test_progress_segments_cover_determinate_and_indeterminate_geometry():
    assert progress_segments(100.0, 400.0, 0.25) == ((100.0, 175.0),)
    segment = progress_segments(100.0, 400.0, None, phase=0.5)
    assert len(segment) == 1
    assert segment[0][1] - segment[0][0] == pytest.approx(84.0)


def test_circular_progress_ranges_cover_determinate_and_wrapping_states():
    assert circular_progress_ranges(0.0) == ()
    assert circular_progress_ranges(0.25) == ((0.0, 0.25),)
    assert circular_progress_ranges(2.0) == ((0.0, 1.0),)
    assert circular_progress_ranges(None, phase=0.9, segment_fraction=0.28) == (
        (0.9, 1.0),
        (0.0, pytest.approx(0.18)),
    )


def test_hex_color_rgb_converts_brand_tokens():
    assert hex_color_rgb("#FF8000") == (1.0, pytest.approx(128 / 255), 0.0)


def test_opengl_progress_layout_scale_matches_loading_surfaces():
    assert progress_layout_scale((1280, 720)) == 1.0
    assert progress_layout_scale((1920, 1080)) == pytest.approx(1.25)
    assert progress_layout_scale((3840, 2160)) == OPENGL_PROGRESS_LAYOUT_SCALE_MAX
