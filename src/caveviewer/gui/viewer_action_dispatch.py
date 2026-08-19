"""Ordered non-GL keyboard action dispatch for the viewer.

Keyboard handlers in ``CaveViewerWindow`` retain their UI and OpenGL-adjacent
side effects.  This module owns the stable priority order between those
handlers so it can be tested without constructing a window backend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


ViewerKeyAction = Callable[[], bool]


@dataclass(frozen=True)
class ViewerKeyPressActions:
    """Handlers considered, in priority order, for one key press."""

    window_shortcut: ViewerKeyAction
    recorded_dive: ViewerKeyAction
    begin_screen: ViewerKeyAction
    fly_speed: ViewerKeyAction
    bookmark: ViewerKeyAction
    manual_dive_trace: ViewerKeyAction
    slice: ViewerKeyAction
    recording: ViewerKeyAction
    slice_escape: ViewerKeyAction
    reset_view: ViewerKeyAction


class ViewerActionDispatcher:
    """Dispatch viewer keyboard actions without owning window resources."""

    def dispatch_key_press(self, actions: ViewerKeyPressActions) -> bool:
        """Run press actions until one consumes the key event."""
        for action in (
            actions.window_shortcut,
            actions.recorded_dive,
            actions.begin_screen,
            actions.fly_speed,
            actions.bookmark,
            actions.manual_dive_trace,
            actions.slice,
            actions.recording,
            actions.slice_escape,
            actions.reset_view,
        ):
            if action():
                return True
        return False

    @staticmethod
    def dispatch_key_repeat(
        *,
        waiting_for_begin: bool,
        fly_speed: ViewerKeyAction,
    ) -> bool:
        """Apply repeat-only actions unless the introductory overlay owns input."""
        return False if waiting_for_begin else fly_speed()
