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


class CaptureOwner(Enum):
    """The single capture lifecycle allowed to own a viewer session."""

    VIDEO = auto()
    DIVE_TRACE = auto()
    SLICE = auto()


@dataclass(frozen=True)
class CaptureOverlayState:
    """Capture facts used to choose a non-GL overlay mode."""

    recording_armed: bool
    manual_dive_trace_countdown_active: bool
    slice_countdown_active: bool


@dataclass(frozen=True)
class CaptureOwnershipState:
    """Cross-capture lifecycle facts, including asynchronous finalization."""

    recording_owned: bool
    manual_dive_trace_owned: bool
    slice_owned: bool


@dataclass(frozen=True)
class CaptureInstruction:
    """Persistent non-video capture guidance rendered above the cave view."""

    title: str
    note: str


@dataclass
class ViewerCaptureWorkflow:
    """Coordinate capture-aware viewer shutdown without owning writers."""

    exit_status_minimum_seconds: float = 0.75
    exit_finalization_requested: bool = False
    exit_status_presented_at: float | None = None
    escape_cancellation_close_requested: bool = False

    @property
    def exit_finalization_active(self) -> bool:
        """Return whether viewer shutdown is waiting for capture publication."""
        return self.exit_finalization_requested

    @property
    def escape_cancellation_active(self) -> bool:
        """Return whether Escape owns shutdown through capture cancellation."""
        return self.escape_cancellation_close_requested

    @property
    def close_pending(self) -> bool:
        """Return whether either capture-aware shutdown workflow owns the viewer."""
        return self.exit_finalization_active or self.escape_cancellation_active

    def begin_exit_finalization(self) -> None:
        """Start an exit workflow and require a visible status before closing."""
        self.exit_finalization_requested = True
        self.exit_status_presented_at = None

    def complete_exit_finalization(self) -> None:
        """Clear the completed exit workflow before the window is released."""
        self.exit_finalization_requested = False
        self.exit_status_presented_at = None

    def begin_escape_cancellation(self) -> None:
        """Keep the viewer alive while Escape discards the active capture."""
        self.escape_cancellation_close_requested = True

    def complete_escape_cancellation(self) -> None:
        """Clear Escape-owned shutdown after cleanup and confirmation."""
        self.escape_cancellation_close_requested = False

    def complete_close_workflows(self) -> None:
        """Clear every capture-aware shutdown mode before releasing the window."""
        self.complete_exit_finalization()
        self.complete_escape_cancellation()

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

    def can_complete_escape_cancellation(
        self,
        *,
        artifacts_pending: bool,
        confirmation_until: float | None,
        now: float,
    ) -> bool:
        """Close only after discard cleanup and its timed result are complete."""
        return bool(
            self.escape_cancellation_active
            and not artifacts_pending
            and confirmation_until is not None
            and now >= confirmation_until
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

    @staticmethod
    def owner_for(state: CaptureOwnershipState) -> CaptureOwner | None:
        """Return the sole capture owner using the established safety priority."""
        if state.recording_owned:
            return CaptureOwner.VIDEO
        if state.manual_dive_trace_owned:
            return CaptureOwner.DIVE_TRACE
        if state.slice_owned:
            return CaptureOwner.SLICE
        return None

    @staticmethod
    def should_ignore_capture_shortcut(
        *,
        active_owner: CaptureOwner | None,
        requested_owner: CaptureOwner,
    ) -> bool:
        """Silently consume a capture shortcut aimed at a different owner."""
        return active_owner is not None and active_owner is not requested_owner

    @staticmethod
    def instruction_for(
        owner: CaptureOwner | None,
        *,
        primary_shortcut_label: str,
    ) -> CaptureInstruction | None:
        """Keep every active capture view banner-free after its countdown."""
        del owner, primary_shortcut_label
        # Each countdown already teaches finish/save and Escape cancellation;
        # repeating that guidance throughout capture obstructs the cave view.
        return None
