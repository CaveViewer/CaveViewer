"""Normalize Tk mouse-wheel events for CaveViewer's scrollable panels."""

from __future__ import annotations

from math import isfinite
from typing import Any


_WHEEL_NOTCH_DELTA = 120.0


def vertical_scroll_units(event: Any) -> int | None:
    """Return a signed Canvas ``yview`` unit count for one Tk wheel event.

    Traditional Windows wheels use 120-sized deltas, while Aqua Tk and
    trackpads can report much smaller non-zero values.  Never truncate a valid
    small delta to zero: one event must move at least one unit in its direction.
    Larger conventional deltas still retain their approximate notch count.
    """
    try:
        delta = float(getattr(event, "delta", 0))
    except (TypeError, ValueError):
        delta = 0.0

    if isfinite(delta) and delta != 0.0:
        magnitude = max(1, int(round(abs(delta) / _WHEEL_NOTCH_DELTA)))
        return -magnitude if delta > 0 else magnitude

    button_number = getattr(event, "num", None)
    if button_number == 4:
        return -1
    if button_number == 5:
        return 1
    return None
