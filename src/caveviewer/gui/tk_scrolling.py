"""Normalize Tk mouse-wheel events for CaveViewer's scrollable panels."""

from __future__ import annotations

from math import isfinite
from typing import Any


_WHEEL_NOTCH_DELTA = 120.0
_WHEEL_NOTCH_PIXELS = 40.0


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


def vertical_scroll_pixels(event: Any) -> float | None:
    """Return a signed pixel delta for one Tk wheel event.

    High-resolution trackpads can emit many small ``delta`` values. Preserve
    those deltas as pixels so canvas-backed panels move smoothly instead of
    turning each tiny event into a full canvas unit jump.
    """
    try:
        delta = float(getattr(event, "delta", 0))
    except (TypeError, ValueError):
        delta = 0.0

    if isfinite(delta) and delta != 0.0:
        if abs(delta) >= _WHEEL_NOTCH_DELTA:
            return -(delta / _WHEEL_NOTCH_DELTA) * _WHEEL_NOTCH_PIXELS
        return -delta

    button_number = getattr(event, "num", None)
    if button_number == 4:
        return -_WHEEL_NOTCH_PIXELS
    if button_number == 5:
        return _WHEEL_NOTCH_PIXELS
    return None
