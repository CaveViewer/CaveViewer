"""Tests for free-fly camera state and movement math."""

from __future__ import annotations

import pytest

from caveviewer.gui.camera import FlyCamera


def test_adjust_speed_uses_multiplicative_steps_and_respects_bounds():
    camera = FlyCamera(move_speed=4.0)

    camera.adjust_speed(1)
    assert camera.move_speed == pytest.approx(4.4)

    camera.adjust_speed(-1)
    assert camera.move_speed == pytest.approx(4.0)

    camera.move_speed = 0.1
    camera.adjust_speed(-1)
    assert camera.move_speed == 0.1

    camera.move_speed = 200.0
    camera.adjust_speed(1)
    assert camera.move_speed == 200.0
