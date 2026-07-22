"""Platform-aware Tk shortcut binding helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from caveviewer.gui.platform import get_platform_adapter


def primary_modifier_event_sequence(key: str, *, platform: str | None = None) -> str:
    """Return the Tk event sequence for the platform's primary shortcut key."""
    modifier = (
        get_platform_adapter().tk_primary_modifier_name()
        if platform is None
        else "Command"
        if platform == "darwin"
        else "Control"
    )
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
