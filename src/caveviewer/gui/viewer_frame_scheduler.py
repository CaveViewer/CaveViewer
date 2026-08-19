"""Non-OpenGL frame-phase and throttling policy for the viewer session.

``CaveViewerWindow`` remains responsible for the OpenGL work performed in a
frame.  This module owns only the deterministic decision about which session
phase may run and the timestamp gates for low-value callbacks such as an
iconified window or an import-pause notice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class ViewerFramePhase(Enum):
    """The non-GL session phase selected for one viewer callback."""

    INACTIVE = auto()
    ICONIFIED = auto()
    FINALIZING_CAPTURE = auto()
    IMPORTING = auto()
    STARTUP = auto()
    INTERACTIVE = auto()


@dataclass(frozen=True)
class ViewerFrameState:
    """Facts needed to select a frame phase without accessing a window."""

    setup_complete: bool
    closing_requested: bool
    iconified: bool
    finalizing_capture: bool
    import_active: bool
    map_loaded: bool


@dataclass
class ViewerFrameScheduler:
    """Select frame phases and maintain non-blocking render deadlines."""

    _throttle_due_at: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def phase_for(state: ViewerFrameState) -> ViewerFramePhase:
        """Return the highest-priority session phase for ``state``.

        The order intentionally keeps capture finalization ahead of import and
        map interaction, so an accepted close request never lets a new input or
        streaming action run while a user artifact is still being published.
        """
        if not state.setup_complete or state.closing_requested:
            return ViewerFramePhase.INACTIVE
        if state.iconified:
            return ViewerFramePhase.ICONIFIED
        if state.finalizing_capture:
            return ViewerFramePhase.FINALIZING_CAPTURE
        if state.import_active:
            return ViewerFramePhase.IMPORTING
        if not state.map_loaded:
            return ViewerFramePhase.STARTUP
        return ViewerFramePhase.INTERACTIVE

    def is_due(self, key: str, interval_s: float, *, now: float) -> bool:
        """Return whether a throttled callback should run at ``now``.

        This never sleeps: the render/window callback remains available to its
        backend while lower-priority work waits for its next deadline.
        """
        if now < self._throttle_due_at.get(key, 0.0):
            return False
        self._throttle_due_at[key] = now + max(0.0, interval_s)
        return True

    def reset_throttle(self, *keys: str) -> None:
        """Forget deadlines for states that are no longer active."""
        if not keys:
            self._throttle_due_at.clear()
            return
        for key in keys:
            self._throttle_due_at.pop(key, None)
