"""Cover reusable camera-route interpolation and following."""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from caveviewer.core.navigation.route import (
    CameraRoute,
    RouteFollower,
    RouteKeyframe,
    route_keyframes_for_points,
)


def test_route_follower_applies_offset_route_to_camera():
    route = CameraRoute.from_keyframes(
        (
            RouteKeyframe(0.0, (0.0, 0.0, 0.0), 0.0, 0.0),
            RouteKeyframe(10.0, (10.0, 0.0, 0.0), 90.0, -10.0),
        ),
        position_mode="first_chunk_center_offset",
    )
    follower = RouteFollower(route, perf_counter=lambda: 0.0)
    follower.set_position_origin((100.0, 20.0, -50.0))
    camera = SimpleNamespace(
        position=np.zeros(3, dtype=np.float64),
        yaw=0.0,
        pitch=0.0,
        roll=0.0,
    )

    follower.update_camera(camera, now=5.0)
    follower.update_camera(camera, now=10.0)

    assert camera.position.tolist() == pytest.approx([105.0, 20.0, -50.0])
    assert camera.yaw == pytest.approx(math.radians(45.0))
    assert camera.pitch == pytest.approx(math.radians(-5.0))


def test_route_keyframes_for_points_can_hold_start_until_travel():
    keyframes = route_keyframes_for_points(
        ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)),
        duration_s=20.0,
        start_time_s=5.0,
        hold_start=True,
    )

    assert [frame["time_s"] for frame in keyframes] == [0.0, 5.0, 25.0]
    assert keyframes[0]["position"] == [0.0, 0.0, 0.0]
    assert keyframes[1]["position"] == [0.0, 0.0, 0.0]
    assert keyframes[-1]["position"] == [10.0, 0.0, 0.0]


def test_route_keyframes_can_steer_toward_lookahead_point():
    keyframes = route_keyframes_for_points(
        (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (1.0, 0.0, 1.0),
        ),
        duration_s=2.0,
        lookahead_distance_m=2.0,
    )

    assert keyframes[0]["yaw_deg"] == pytest.approx(45.0)
