"""Tests for bounded 3D Guided Dive recovery probe geometry."""

from __future__ import annotations

import math

import numpy as np
import pytest

from caveviewer.core.navigation.recovery_scan import (
    forward_hemisphere_directions,
    iter_hemisphere_probes,
)


def test_forward_hemisphere_directions_cover_a_full_forward_half_sphere():
    forward = (1.0, 0.0, 0.0)
    directions = forward_hemisphere_directions(forward, count=32)

    assert len(directions) == 32
    alignments = [float(np.dot(direction, forward)) for direction in directions]
    assert min(alignments) >= 0.0
    assert max(alignments) > 0.95
    assert min(alignments) < 0.1
    assert min(direction[1] for direction in directions) < -0.5
    assert max(direction[1] for direction in directions) > 0.5
    assert min(direction[2] for direction in directions) < -0.5
    assert max(direction[2] for direction in directions) > 0.5
    assert all(
        math.sqrt(sum(float(value) ** 2 for value in direction))
        == pytest.approx(1.0)
        for direction in directions
    )


def test_hemisphere_probes_include_roll_and_lateral_vertical_origins():
    probes = tuple(
        iter_hemisphere_probes(
            (0.0, 0.0, 0.0),
            forward=(1.0, 0.0, 0.0),
            distance_m=20.0,
            cell_size_m=10.0,
            voxel_size_m=2.0,
            direction_count=8,
            roll_count=4,
        )
    )

    assert len(probes) == 8 * 4 * 9
    assert {round(probe.roll_deg) for probe in probes} == {
        -180,
        -90,
        0,
        90,
    }
    assert {probe.offset_label for probe in probes} >= {
        "center",
        "right",
        "left",
        "up",
        "down",
    }
    assert any(abs(probe.origin_offset[1]) > 1e-6 for probe in probes)
    assert any(abs(probe.origin_offset[2]) > 1e-6 for probe in probes)
    assert all(probe.forward_alignment >= 0.0 for probe in probes)
