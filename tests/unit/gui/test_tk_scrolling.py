"""Verify consistent Tk mouse-wheel normalization across platforms."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caveviewer.gui.tk_scrolling import vertical_scroll_units


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (SimpleNamespace(delta=120), -1),
        (SimpleNamespace(delta=-120), 1),
        (SimpleNamespace(delta=240), -2),
        (SimpleNamespace(delta=-240), 2),
        (SimpleNamespace(delta=1), -1),
        (SimpleNamespace(delta=-1), 1),
        (SimpleNamespace(delta=0, num=4), -1),
        (SimpleNamespace(delta=0, num=5), 1),
        (SimpleNamespace(delta="invalid", num=4), -1),
        (SimpleNamespace(delta=float("nan")), None),
    ],
)
def test_vertical_scroll_units_preserves_direction_for_every_tk_wheel_style(
    event,
    expected,
):
    assert vertical_scroll_units(event) == expected
