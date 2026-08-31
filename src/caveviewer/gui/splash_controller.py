"""Widget-free lifecycle control for one splash session."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from caveviewer.gui.splash_session import SplashSession


@dataclass(slots=True)
class StartupReadinessGate:
    """Track monotonic startup progress and the two conditions for reveal."""

    visible_at: float
    minimum_ms: int = 3_000
    progress: float = 0.0
    ready: bool = False

    def advance(self, fraction: float) -> float:
        """Advance measurable startup work without allowing progress reversal."""
        bounded = max(0.0, min(0.99, float(fraction)))
        self.progress = max(self.progress, bounded)
        return self.progress

    def mark_ready(self) -> None:
        """Record that artifacts needed by the first interactive frame exist."""
        self.ready = True
        self.progress = 1.0

    def remaining_delay_ms(self, now: float) -> int:
        """Return the non-blocking delay still required by launch policy."""
        elapsed_ms = max(0.0, (float(now) - float(self.visible_at)) * 1_000.0)
        return max(0, int(math.ceil(max(0, self.minimum_ms) - elapsed_ms)))

    def can_reveal(self, now: float) -> bool:
        """Return whether readiness and minimum visible duration are both met."""
        return self.ready and self.remaining_delay_ms(now) == 0

    def visual_progress(self, now: float) -> float:
        """Blend real milestones with a smooth time-based launch presentation."""
        duration_ms = max(1, self.minimum_ms)
        elapsed_ms = max(0.0, (float(now) - float(self.visible_at)) * 1_000.0)
        time_fraction = min(1.0, elapsed_ms / duration_ms)
        animated = 0.08 + (0.90 * time_fraction)
        if self.ready and time_fraction >= 1.0:
            return 1.0
        # Real work may move the bar ahead, but only the combined time/readiness
        # gate may display completion.
        return min(0.98, max(min(self.progress, 0.94), animated))


@dataclass(frozen=True, slots=True)
class SplashScheduler:
    """Tk scheduling port supplied by the splash composition root."""

    after: Callable[[int, Callable[[], None]], str]
    after_cancel: Callable[[str], None]
    after_idle: Callable[[Callable[[], None]], str] | None = None


class SplashController:
    """Own start, selection, scheduling, and idempotent splash shutdown."""

    def __init__(
        self,
        scheduler: SplashScheduler,
        *,
        session: SplashSession | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._session = session or SplashSession()
        self._started = False

    @property
    def closing(self) -> bool:
        return self._session.closing

    @property
    def selected_folder(self) -> str | None:
        return self._session.selected_folder

    def start(self) -> None:
        """Mark the lifecycle active; starting twice is a programming error."""
        if self._started:
            raise RuntimeError("SplashController has already started")
        if self.closing:
            raise RuntimeError("A closed SplashController cannot be restarted")
        self._started = True

    def select_folder(self, path: str) -> None:
        """Record the selected launch target while the lifecycle is active."""
        if not self._started or self.closing:
            return
        self._session.select_folder(path)

    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> str:
        """Schedule one owned callback through the injected Tk-thread port."""
        if not self._started or self.closing:
            raise RuntimeError("Cannot schedule outside an active splash lifecycle")
        return self._session.schedule_after(self._scheduler, delay_ms, callback)

    def schedule_idle(self, callback: Callable[[], None]) -> str:
        """Schedule one owned idle callback through the composition port."""
        if not self._started or self.closing:
            raise RuntimeError("Cannot schedule outside an active splash lifecycle")
        if self._scheduler.after_idle is None:
            return self.schedule(0, callback)
        return self._session.schedule_idle(self._scheduler, callback)

    def close(self) -> None:
        """Stop callbacks exactly once; late callbacks become harmless."""
        if self.closing:
            return
        self._session.mark_closing()
        self._session.cancel_after_callbacks(self._scheduler)

    def cancel_scheduled_callbacks(self) -> None:
        """Cancel owned callbacks during final composition cleanup."""
        self._session.cancel_after_callbacks(self._scheduler)
