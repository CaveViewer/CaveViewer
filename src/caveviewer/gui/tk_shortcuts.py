"""Platform-aware Tk shortcut binding helpers."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any


def primary_modifier_event_sequence(key: str, *, platform: str | None = None) -> str:
    """Return the Tk event sequence for the platform's primary shortcut key."""
    active_platform = sys.platform if platform is None else platform
    modifier = "Command" if active_platform == "darwin" else "Control"
    normalized_key = str(key).strip("<>")
    return f"<{modifier}-{normalized_key}>"


def bind_primary_shortcut(
    widget: Any,
    key: str,
    callback: Callable[[Any], object],
    *,
    add: str | bool | None = None,
) -> str:
    """Bind a primary-modifier shortcut and return the Tk event sequence used."""
    sequence = primary_modifier_event_sequence(key)
    if add is None:
        widget.bind(sequence, callback)
    else:
        widget.bind(sequence, callback, add=add)
    return sequence
