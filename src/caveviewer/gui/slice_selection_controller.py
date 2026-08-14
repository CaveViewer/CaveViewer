"""Testable render-thread state for the keyboard-driven cave slice workflow."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

from caveviewer.gui.manual_dive_trace_controller import ManualDiveTraceStateController


class SliceSelectionState(str, Enum):
    """The selection half of a slice export lifecycle."""

    IDLE = "idle"
    COUNTDOWN = "countdown"
    ACTIVE = "active"
    SAVING = "saving"


@dataclass(frozen=True, slots=True)
class SliceAnchors:
    """Start and end camera positions captured for one slice."""

    start: tuple[float, float, float]
    end: tuple[float, float, float]


@dataclass
class SliceSelectionController:
    """Own countdown and anchor transitions without importing OpenGL or I/O."""

    state: SliceSelectionState = SliceSelectionState.IDLE
    start_anchor: tuple[float, float, float] | None = None
    _countdown: ManualDiveTraceStateController | None = None

    @property
    def countdown_active(self) -> bool:
        return self.state is SliceSelectionState.COUNTDOWN

    @property
    def selection_active(self) -> bool:
        return self.state is SliceSelectionState.ACTIVE

    @property
    def saving(self) -> bool:
        return self.state is SliceSelectionState.SAVING

    @property
    def countdown_started_at(self) -> float | None:
        return self._ensure_countdown().countdown_started_at

    @property
    def countdown_until(self) -> float | None:
        return self._ensure_countdown().countdown_until

    def start_countdown(self, *, now: float, start_number: int) -> bool:
        """Arm the shared capture countdown from an idle selection state."""
        if self.state is not SliceSelectionState.IDLE:
            return False
        self._ensure_countdown().start_countdown(now=now, start_number=start_number)
        self.state = SliceSelectionState.COUNTDOWN
        return True

    def countdown_ready(self, *, now: float) -> bool:
        return self.countdown_active and self._ensure_countdown().countdown_ready(now=now)

    def countdown_display(self, *, now: float, start_number: int):
        return self._ensure_countdown().countdown_display(
            now=now,
            start_number=start_number,
        )

    def begin_selection(
        self,
        position: tuple[float, float, float],
    ) -> bool:
        """Transition from a completed countdown into active slicing."""
        if self.state is not SliceSelectionState.COUNTDOWN:
            return False
        self._ensure_countdown().clear_countdown()
        self.start_anchor = _finite_position(position)
        self.state = SliceSelectionState.ACTIVE
        return True

    def cancel_countdown(self) -> bool:
        if not self.countdown_active:
            return False
        self._ensure_countdown().clear_countdown()
        self.state = SliceSelectionState.IDLE
        return True

    def cancel_selection(self) -> bool:
        """Discard an unfinished active selection, never a saving export."""
        if not self.selection_active:
            return False
        self.start_anchor = None
        self.state = SliceSelectionState.IDLE
        return True

    def finish_selection(
        self,
        end_position: tuple[float, float, float],
    ) -> SliceAnchors | None:
        """Capture the end anchor and reserve this controller for finalization."""
        if not self.selection_active or self.start_anchor is None:
            return None
        anchors = SliceAnchors(
            start=self.start_anchor,
            end=_finite_position(end_position),
        )
        self.start_anchor = None
        self.state = SliceSelectionState.SAVING
        return anchors

    def complete_export(self) -> None:
        """Return to idle after a terminal export state."""
        self._ensure_countdown().clear_countdown()
        self.start_anchor = None
        self.state = SliceSelectionState.IDLE

    def _ensure_countdown(self) -> ManualDiveTraceStateController:
        controller = self._countdown
        if controller is None:
            controller = ManualDiveTraceStateController()
            self._countdown = controller
        return controller


def _finite_position(value: tuple[float, float, float]) -> tuple[float, float, float]:
    try:
        position = tuple(float(component) for component in value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Slice anchors must have three finite coordinates") from exc
    if len(position) != 3 or not all(math.isfinite(component) for component in position):
        raise ValueError("Slice anchors must have three finite coordinates")
    return position  # type: ignore[return-value]
