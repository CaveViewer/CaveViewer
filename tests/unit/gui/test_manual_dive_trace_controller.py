"""Tests for manual Guided Dive countdown state."""

from __future__ import annotations

import pytest

from caveviewer.gui.manual_dive_trace_controller import (
    ManualDiveTraceStateController,
)


def test_trace_countdown_displays_numbers_and_becomes_ready():
    controller = ManualDiveTraceStateController()

    controller.start_countdown(now=10.0, start_number=3)

    assert controller.countdown_active
    assert controller.countdown_started_at == 10.0
    assert controller.countdown_until == 14.0
    assert controller.countdown_ready(now=13.99) is False
    assert controller.countdown_ready(now=14.0) is True
    assert controller.countdown_display(now=10.1, start_number=3).number == 3
    display = controller.countdown_display(now=12.1, start_number=3)
    assert display.number == 1
    assert display.progress == pytest.approx(2.1 / 4.0)

    controller.clear_countdown()

    assert not controller.countdown_active
    assert controller.countdown_display(now=20.0, start_number=3).number == 0
