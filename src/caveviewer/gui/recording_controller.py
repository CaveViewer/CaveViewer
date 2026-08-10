"""Testable recording workflow state for the OpenGL viewer.

The viewer window owns framebuffer readback and encoder process handles. This
controller owns the small state transitions around countdowns, transient status
messages, capture timing, and dropped-frame accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RecordingCountdownSnapshot:
    """User-facing countdown number and progress for the overlay renderer."""

    number: int
    progress: float


@dataclass(frozen=True)
class RecordingStatusSnapshot:
    """Current recording or capture status message ready for presentation."""

    message: str
    detail: str | None
    kind: str | None
    until: float | None


@dataclass
class RecordingStateController:
    """State machine for recording countdowns, status, and frame scheduling."""

    frame_interval: float = 1.0 / 30.0
    countdown_started_at: float | None = None
    countdown_until: float | None = None
    next_frame_time: float | None = None
    last_stage_ms: float = 0.0
    last_drain_ms: float = 0.0
    dropped_frames: int = 0
    status_message: str | None = None
    status_detail: str | None = None
    status_kind: str | None = "info"
    status_until: float | None = None

    def is_armed(self, *, process_active: bool) -> bool:
        """Return whether recording owns the HUD for countdown or capture."""
        return self.countdown_until is not None or process_active

    def start_countdown(self, *, now: float, start_number: int) -> None:
        """Start the visible recording countdown."""
        duration = float(start_number) + 1.0
        self.countdown_started_at = now
        self.countdown_until = now + duration

    def clear_countdown(self) -> None:
        """Clear any pending countdown."""
        self.countdown_started_at = None
        self.countdown_until = None

    def cancel_countdown(self, *, now: float, status_duration: float = 2.8) -> None:
        """Cancel countdown state and show the cancellation status."""
        self.clear_countdown()
        self.show_status(
            "Recording canceled",
            now=now,
            kind="cancel",
            duration=status_duration,
        )

    def countdown_ready(self, *, now: float) -> bool:
        """Return whether the countdown has elapsed and capture should start."""
        return self.countdown_until is not None and now >= self.countdown_until

    def countdown_display(
        self,
        *,
        now: float,
        start_number: int,
    ) -> RecordingCountdownSnapshot:
        """Return the current countdown overlay value."""
        if self.countdown_until is None:
            return RecordingCountdownSnapshot(number=0, progress=1.0)

        duration = float(start_number) + 1.0
        started_at = self.countdown_started_at
        if started_at is None:
            started_at = self.countdown_until - duration

        elapsed = max(0.0, now - started_at)
        remaining = max(0.0, self.countdown_until - now)
        number = max(0, min(start_number, int(math.ceil(remaining)) - 1))
        progress = max(0.0, min(1.0, elapsed / duration))
        return RecordingCountdownSnapshot(number=number, progress=progress)

    def mark_encoder_started(self, *, now: float) -> None:
        """Transition from countdown into active capture timing."""
        self.clear_countdown()
        self.next_frame_time = now
        self.dropped_frames = 0
        self.reset_frame_timings()

    def reset_frame_timings(self) -> None:
        """Clear per-frame timing diagnostics."""
        self.last_stage_ms = 0.0
        self.last_drain_ms = 0.0

    def reset_after_stop_result(self) -> None:
        """Clear transient recording counters after a finalizer result."""
        self.dropped_frames = 0
        self.next_frame_time = None
        self.reset_frame_timings()

    def drop_frames(self, count: int = 1) -> bool:
        """Record dropped frames and return true for the first drop warning."""
        if count <= 0:
            return False
        should_warn = self.dropped_frames == 0
        self.dropped_frames += count
        return should_warn

    def due_frame_slots(self, *, now: float, next_frame_time: float | None) -> int:
        """Return how many frame intervals are due at the current time."""
        if next_frame_time is None:
            return 1
        late_by = max(0.0, now - next_frame_time)
        return 1 + int(late_by / self.frame_interval)

    def advance_next_frame_time(self, *, now: float, frame_slots: int) -> float:
        """Advance the capture schedule after handling due slots."""
        next_time = (self.next_frame_time or now) + frame_slots * self.frame_interval
        self.next_frame_time = next_time
        return next_time

    def show_status(
        self,
        message: str,
        *,
        now: float,
        detail: str | None = None,
        kind: str | None = "info",
        duration: float | None = 2.8,
    ) -> None:
        """Show a status until it expires, is replaced, or is explicitly cleared."""
        self.status_message = message
        self.status_detail = detail
        self.status_kind = kind
        self.status_until = None if duration is None else now + duration

    def clear_status(self) -> None:
        """Clear the transient recording status message."""
        self.status_message = None
        self.status_detail = None
        self.status_kind = "info"
        self.status_until = None

    def active_status(self, *, now: float) -> RecordingStatusSnapshot | None:
        """Return the current status snapshot, expiring it when needed."""
        if not self.status_message:
            return None
        if self.status_until is not None and now >= self.status_until:
            self.clear_status()
            return None
        return RecordingStatusSnapshot(
            message=self.status_message,
            detail=self.status_detail,
            kind=self.status_kind,
            until=self.status_until,
        )
