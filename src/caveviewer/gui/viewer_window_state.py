"""Persist the user-selected native viewer size between interactive launches."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from caveviewer.gui.preference_paths import state_dir, write_text_atomic


_STATE_FILENAME = "viewer_window_state.json"
_STATE_FORMAT_VERSION = 1
_MAX_DIMENSION = 100_000


@dataclass(frozen=True, slots=True)
class ViewerWindowState:
    """Validated logical dimensions captured from a normal viewer window."""

    width: int
    height: int


def viewer_window_state_path() -> str:
    """Return the platform-appropriate persistent viewer-state path."""
    return os.path.join(state_dir(), _STATE_FILENAME)


def load_viewer_window_state(
    path: str | os.PathLike[str] | None = None,
) -> ViewerWindowState | None:
    """Load saved viewer dimensions, ignoring missing or invalid state."""
    try:
        state_path = (
            os.fsdecode(path) if path is not None else viewer_window_state_path()
        )
        with open(state_path, "r", encoding="utf-8") as state_file:
            payload = json.load(state_file)
    except (OSError, TypeError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _STATE_FORMAT_VERSION
    ):
        return None
    state = ViewerWindowState(
        width=payload.get("width"),
        height=payload.get("height"),
    )
    return state if _state_is_valid(state) else None


def save_viewer_window_state(
    state: ViewerWindowState,
    path: str | os.PathLike[str] | None = None,
) -> bool:
    """Best-effort atomically persist validated viewer dimensions."""
    if not _state_is_valid(state):
        return False
    payload = {
        "version": _STATE_FORMAT_VERSION,
        "width": state.width,
        "height": state.height,
    }
    try:
        state_path = (
            os.fsdecode(path) if path is not None else viewer_window_state_path()
        )
        write_text_atomic(
            state_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )
    except (OSError, TypeError, ValueError):
        return False
    return True


def _state_is_valid(state: ViewerWindowState) -> bool:
    return isinstance(state, ViewerWindowState) and all(
        _dimension_is_valid(value) for value in (state.width, state.height)
    )


def _dimension_is_valid(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= _MAX_DIMENSION
    )
