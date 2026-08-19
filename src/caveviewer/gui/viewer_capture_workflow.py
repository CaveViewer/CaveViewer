"""Non-OpenGL capture workflow decisions for a viewer session.

The viewer window owns render-thread framebuffer resources and presentation.
This controller owns the cross-capture state that decides when shutdown may
finish and which post-scene overlay has priority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class CaptureOverlayMode(Enum):
    """The user-visible capture state that owns the post-scene overlay."""

    RECORDING = auto()
    MANUAL_DIVE_TRACE_COUNTDOWN = auto()
    SLICE_COUNTDOWN = auto()
    HUD = auto()


@dataclass(frozen=True)
class CaptureOverlayState:
    """Capture facts used to choose a non-GL overlay mode."""

    recording_armed: bool
    manual_dive_trace_countdown_active: bool
    slice_countdown_active: bool


@dataclass
class ViewerCaptureWorkflow:
    """Coordinate exit-time capture finalization without owning writers."""

    exit_status_minimum_seconds: float = 0.75
    exit_finalization_requested: bool = False
    exit_status_presented_at: float | None = None

    @property
    def exit_finalization_active(self) -> bool:
        """Return whether viewer shutdown is waiting for capture publication."""
        return self.exit_finalization_requested

    def begin_exit_finalization(self) -> None:
        """Start an exit workflow and require a visible status before closing."""
        self.exit_finalization_requested = True
        self.exit_status_presented_at = None

    def complete_exit_finalization(self) -> None:
        """Clear the completed exit workflow before the window is released."""
        self.exit_finalization_requested = False
        self.exit_status_presented_at = None

    def mark_exit_status_presented(self, *, now: float) -> None:
        """Record the first frame that visibly presented exit progress."""
        if self.exit_finalization_active and self.exit_status_presented_at is None:
            self.exit_status_presented_at = now

    def can_complete_exit_finalization(
        self,
        *,
        artifacts_pending: bool,
        now: float,
        allow_unpresented_status: bool = False,
    ) -> bool:
        """Return whether the window may close without hiding exit feedback."""
        if not self.exit_finalization_active or artifacts_pending:
            return False
        if allow_unpresented_status:
            return True
        if self.exit_status_presented_at is None:
            return False
        return (
            now - self.exit_status_presented_at
            >= self.exit_status_minimum_seconds
        )

    @staticmethod
    def overlay_mode_for(state: CaptureOverlayState) -> CaptureOverlayMode:
        """Choose the capture overlay with the established action priority."""
        if state.recording_armed:
            return CaptureOverlayMode.RECORDING
        if state.manual_dive_trace_countdown_active:
            return CaptureOverlayMode.MANUAL_DIVE_TRACE_COUNTDOWN
        if state.slice_countdown_active:
            return CaptureOverlayMode.SLICE_COUNTDOWN
        return CaptureOverlayMode.HUD
