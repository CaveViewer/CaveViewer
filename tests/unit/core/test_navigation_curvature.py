"""Tests for reusable navigation curvature profiles."""

from __future__ import annotations

import math

import pytest

from caveviewer.core.navigation.curvature import (
    analyze_polyline_curvature,
    select_curvature_regions,
)


def test_curvature_profile_leaves_straight_polyline_unranked():
    points = tuple((float(index), 0.0, 0.0) for index in range(8))

    profile = analyze_polyline_curvature(points, window_points=1)

    assert profile.point_count == len(points)
    assert all(sample.rank_0_100 == 0 for sample in profile.samples)
    assert profile.regions == ()
    assert select_curvature_regions(profile) == ()


def test_curvature_profile_ranks_and_merges_an_l_bend():
    points = (
        (0.0, 0.0, 0.0),
        (4.0, 0.0, 0.0),
        (8.0, 0.0, 0.0),
        (8.0, 0.0, 4.0),
        (8.0, 0.0, 8.0),
    )

    profile = analyze_polyline_curvature(points, window_points=1)
    regions = select_curvature_regions(
        profile,
        minimum_rank=65,
        max_regions=1,
    )

    assert len(regions) == 1
    assert regions[0].start_index == 0
    assert regions[0].end_index == len(points) - 1
    assert regions[0].max_rank_0_100 == 100
    assert regions[0].total_curvature_rad == pytest.approx(0.5 * math.pi)
