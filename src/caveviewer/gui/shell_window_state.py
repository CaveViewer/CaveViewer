"""Persist and restore the Tk shell's user-selected window size."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import cast

from caveviewer.gui.preference_paths import state_dir, write_text_atomic


_STATE_FILENAME = "shell_window_state.json"
_STATE_FORMAT_VERSION = 1
SHELL_LAYOUT_REVISION = 2
_MAX_LOGICAL_DIMENSION = 100_000.0


@dataclass(frozen=True, slots=True)
class ShellWindowState:
    """Portable normal bounds and show state for the primary Tk shell."""

    normal_width: float
    normal_height: float
    maximized: bool
    layout_revision: int = SHELL_LAYOUT_REVISION


def shell_window_state_path() -> str:
    """Return the platform-appropriate persistent shell-state path."""
    return os.path.join(state_dir(), _STATE_FILENAME)


def load_shell_window_state(
    path: str | os.PathLike[str] | None = None,
    *,
    expected_layout_revision: int = SHELL_LAYOUT_REVISION,
) -> ShellWindowState | None:
    """Load a compatible shell state, ignoring absent or invalid data."""
    try:
        state_path = os.fsdecode(path) if path is not None else shell_window_state_path()
        with open(state_path, "r", encoding="utf-8") as state_file:
            payload = json.load(state_file)
    except (OSError, TypeError, ValueError):
        return None
    return _state_from_payload(
        payload,
        expected_layout_revision=expected_layout_revision,
    )


def save_shell_window_state(
    state: ShellWindowState,
    path: str | os.PathLike[str] | None = None,
) -> bool:
    """Best-effort atomically persist one validated shell state."""
    if not _state_is_valid(
        state,
        expected_layout_revision=SHELL_LAYOUT_REVISION,
    ):
        return False
    payload = {
        "version": _STATE_FORMAT_VERSION,
        "layout_revision": state.layout_revision,
        "normal_width": state.normal_width,
        "normal_height": state.normal_height,
        "maximized": state.maximized,
    }
    try:
        state_path = os.fsdecode(path) if path is not None else shell_window_state_path()
        write_text_atomic(
            state_path,
            json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        )
    except (OSError, TypeError, ValueError):
        return False
    return True


def shell_state_from_native_size(
    *,
    width: int | float,
    height: int | float,
    layout_scale: int | float,
    maximized: bool,
) -> ShellWindowState:
    """Convert settled native pixels to portable logical shell dimensions."""
    scale = _positive_finite(layout_scale, fallback=1.0)
    native_width = max(1.0, _positive_finite(width, fallback=1.0))
    native_height = max(1.0, _positive_finite(height, fallback=1.0))
    return ShellWindowState(
        normal_width=native_width / scale,
        normal_height=native_height / scale,
        maximized=bool(maximized),
    )


def native_size_from_shell_state(
    state: ShellWindowState,
    *,
    layout_scale: int | float,
    minimum_size: tuple[int | float, int | float],
    available_size: tuple[int | float, int | float],
) -> tuple[int, int]:
    """Scale remembered logical dimensions once, then clamp to this display."""
    scale = _positive_finite(layout_scale, fallback=1.0)
    minimum_width = max(
        1,
        int(round(_positive_finite(minimum_size[0], fallback=1))),
    )
    minimum_height = max(
        1,
        int(round(_positive_finite(minimum_size[1], fallback=1))),
    )
    available_width = max(
        1,
        int(round(_positive_finite(available_size[0], fallback=1))),
    )
    available_height = max(
        1,
        int(round(_positive_finite(available_size[1], fallback=1))),
    )
    width = max(minimum_width, int(round(state.normal_width * scale)))
    height = max(minimum_height, int(round(state.normal_height * scale)))
    return min(width, available_width), min(height, available_height)


def _state_from_payload(
    payload: object,
    *,
    expected_layout_revision: int,
) -> ShellWindowState | None:
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    layout_revision = payload.get("layout_revision")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != _STATE_FORMAT_VERSION
        or not isinstance(layout_revision, int)
        or isinstance(layout_revision, bool)
        or layout_revision != expected_layout_revision
    ):
        return None
    maximized = payload.get("maximized")
    if not isinstance(maximized, bool):
        return None
    normal_width = payload.get("normal_width")
    normal_height = payload.get("normal_height")
    if not (
        _logical_dimension_is_valid(normal_width)
        and _logical_dimension_is_valid(normal_height)
    ):
        return None
    state = ShellWindowState(
        normal_width=float(cast(int | float, normal_width)),
        normal_height=float(cast(int | float, normal_height)),
        maximized=maximized,
        layout_revision=cast(int, layout_revision),
    )
    if not _state_is_valid(
        state,
        expected_layout_revision=expected_layout_revision,
    ):
        return None
    return state


def _state_is_valid(
    state: ShellWindowState,
    *,
    expected_layout_revision: int,
) -> bool:
    return bool(
        isinstance(state, ShellWindowState)
        and not isinstance(state.layout_revision, bool)
        and state.layout_revision == expected_layout_revision
        and isinstance(state.maximized, bool)
        and _logical_dimension_is_valid(state.normal_width)
        and _logical_dimension_is_valid(state.normal_height)
    )


def _logical_dimension_is_valid(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return (
        math.isfinite(float(value))
        and 0.0 < float(value) <= _MAX_LOGICAL_DIMENSION
    )


def _positive_finite(value: int | float, *, fallback: float) -> float:
    if isinstance(value, bool):
        return float(fallback)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return numeric if math.isfinite(numeric) and numeric > 0 else float(fallback)
