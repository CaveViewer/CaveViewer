"""Render-thread interaction state for manual Guided Dive tracing.

The controller owns the countdown shown before tracing starts and the delayed
native-file-reveal schedule after a completed trace is confirmed.  The viewer
owns the OpenGL presentation and platform reveal side effect, while
``ManualDiveTraceRecorder`` owns the background JSONL writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math


@dataclass(frozen=True)
class ManualDiveTraceCountdownSnapshot:
    """One countdown value ready for the viewer overlay."""

    number: int
    progress: float


@dataclass
class ManualDiveTraceStateController:
    """Track the user-visible countdown and deferred trace-file reveals."""

    countdown_started_at: float | None = None
    countdown_until: float | None = None
    _pending_reveals: list[tuple[float, str]] = field(default_factory=list)

    @property
    def countdown_active(self) -> bool:
        """Return whether a trace has been armed but has not started yet."""
        return self.countdown_until is not None

    def start_countdown(self, *, now: float, start_number: int) -> None:
        """Arm a visible numeric countdown before trace capture starts."""
        number = max(0, int(start_number))
        self.countdown_started_at = now
        self.countdown_until = now + float(number) + 1.0

    def clear_countdown(self) -> None:
        """Cancel or finish any pending trace countdown."""
        self.countdown_started_at = None
        self.countdown_until = None

    def countdown_ready(self, *, now: float) -> bool:
        """Return whether the armed trace may begin."""
        return self.countdown_until is not None and now >= self.countdown_until

    def countdown_display(
        self,
        *,
        now: float,
        start_number: int,
    ) -> ManualDiveTraceCountdownSnapshot:
        """Return the current numeric countdown and normalized progress."""
        number_limit = max(0, int(start_number))
        if self.countdown_until is None:
            return ManualDiveTraceCountdownSnapshot(number=0, progress=1.0)

        duration = float(number_limit) + 1.0
        started_at = self.countdown_started_at
        if started_at is None:
            started_at = self.countdown_until - duration
        elapsed = max(0.0, now - started_at)
        remaining = max(0.0, self.countdown_until - now)
        number = max(0, min(number_limit, int(math.ceil(remaining)) - 1))
        progress = max(0.0, min(1.0, elapsed / duration))
        return ManualDiveTraceCountdownSnapshot(number=number, progress=progress)

    def defer_reveal(
        self,
        output_path: str,
        *,
        now: float,
        delay_s: float,
    ) -> None:
        """Hold a completed file reveal until its success confirmation is visible."""
        self._pending_reveals.append((now + max(0.0, delay_s), output_path))

    def take_due_reveals(self, *, now: float) -> tuple[str, ...]:
        """Return completed trace paths whose confirmation time has elapsed."""
        due_paths: list[str] = []
        remaining: list[tuple[float, str]] = []
        for reveal_at, output_path in self._pending_reveals:
            if now >= reveal_at:
                due_paths.append(output_path)
            else:
                remaining.append((reveal_at, output_path))
        self._pending_reveals = remaining
        return tuple(due_paths)
