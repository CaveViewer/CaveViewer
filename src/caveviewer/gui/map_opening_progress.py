"""Pure presentation state for one continuous map-opening operation.

Import/cache construction and initial scene streaming remain separate runtime
phases.  This module composes their already-measured progress into one
user-facing operation without taking ownership of either lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MapOpeningProgressFrame:
    """One render-ready state in a map-opening presentation session."""

    session_id: int
    map_name: str
    stage: str
    fraction: float | None
    title: str
    note: str


class MapOpeningProgressSession:
    """Compose import and initial-streaming milestones for one map open.

    The bar intentionally has no numeric percentage label.  A source import
    occupies the first visual milestone and leaves a bounded final band for
    the existing, independently measured initial-view readiness.  This avoids
    presenting elapsed time as a percentage while still making the phase
    transition continuous and monotonic.
    """

    IMPORT_VISUAL_MILESTONE = 0.90
    STREAMING_STAGE = "preparing cave"

    def __init__(self) -> None:
        self._next_session_id = 0
        self._session_id = 0
        self._map_name = ""
        self._phase = "idle"
        self._display_fraction = 0.0
        self._includes_import = False
        self._supporting_note = ""

    @property
    def session_id(self) -> int:
        """Return the current render-session identity."""
        return self._session_id

    def begin_import(
        self,
        map_name: str,
        *,
        new_operation: bool = False,
        note: str = "",
    ) -> MapOpeningProgressFrame:
        """Begin or resume the import portion of a selected map opening."""
        if (
            new_operation
            or self._phase != "import"
            or self._map_name != self._normalize_map_name(map_name)
        ):
            self._begin(
                map_name,
                phase="import",
                includes_import=True,
                supporting_note=note,
            )
        return self._frame("starting import", self._display_fraction)

    def begin_cached(
        self,
        map_name: str,
        *,
        new_operation: bool = False,
    ) -> MapOpeningProgressFrame:
        """Begin the cache-only form of a selected map opening."""
        if (
            new_operation
            or self._phase != "cached"
            or self._map_name != self._normalize_map_name(map_name)
        ):
            self._begin(
                map_name,
                phase="cached",
                includes_import=False,
                supporting_note="",
            )
        return self._frame(self.STREAMING_STAGE, None)

    def observe_import(
        self,
        map_name: str,
        stage: str,
        fraction: float,
        *,
        note: str,
        supporting_note_override: str | None = None,
    ) -> MapOpeningProgressFrame:
        """Present measured import progress within its visual milestone."""
        if (
            self._phase != "import"
            or self._map_name != self._normalize_map_name(map_name)
        ):
            self.begin_import(map_name, note=note)
        target = self._clamp_fraction(fraction) * self.IMPORT_VISUAL_MILESTONE
        self._display_fraction = max(self._display_fraction, target)
        return self._frame(
            stage,
            self._display_fraction,
            supporting_note=supporting_note_override,
        )

    def begin_streaming(self, map_name: str) -> MapOpeningProgressFrame:
        """Transition the current opening operation to initial-view streaming."""
        normalized_name = self._normalize_map_name(map_name)
        if self._phase not in {"import", "cached", "streaming"} or (
            self._map_name != normalized_name
        ):
            self._begin(
                map_name,
                phase="streaming",
                includes_import=False,
                supporting_note="",
            )
        else:
            self._phase = "streaming"
        if self._includes_import:
            self._display_fraction = max(
                self._display_fraction,
                self.IMPORT_VISUAL_MILESTONE,
            )
        return self._frame(self.STREAMING_STAGE, self._display_fraction)

    def observe_streaming(
        self,
        map_name: str,
        fraction: float,
    ) -> MapOpeningProgressFrame:
        """Present measured initial-view readiness without restarting the bar."""
        self.begin_streaming(map_name)
        raw_fraction = self._clamp_fraction(fraction)
        target = (
            self.IMPORT_VISUAL_MILESTONE
            + raw_fraction * (1.0 - self.IMPORT_VISUAL_MILESTONE)
            if self._includes_import
            else raw_fraction
        )
        self._display_fraction = max(self._display_fraction, target)
        return self._frame(self.STREAMING_STAGE, self._display_fraction)

    def complete(self, map_name: str) -> MapOpeningProgressFrame:
        """Render the existing completion hold without ending its session early."""
        self.begin_streaming(map_name)
        self._display_fraction = 1.0
        return self._frame(self.STREAMING_STAGE, self._display_fraction)

    def finish(self) -> None:
        """Mark the current operation complete after its visual hold ends."""
        if self._phase == "streaming":
            self._phase = "complete"

    def abandon(self) -> None:
        """End an unsuccessful operation so a later open starts a fresh session."""
        if self._phase in {"import", "cached", "streaming"}:
            self._phase = "abandoned"

    def _begin(
        self,
        map_name: str,
        *,
        phase: str,
        includes_import: bool,
        supporting_note: str,
    ) -> None:
        self._next_session_id += 1
        self._session_id = self._next_session_id
        self._map_name = self._normalize_map_name(map_name)
        self._phase = phase
        self._display_fraction = 0.0
        self._includes_import = includes_import
        self._supporting_note = str(supporting_note or "")

    def _frame(
        self,
        stage: str,
        fraction: float | None,
        *,
        supporting_note: str | None = None,
    ) -> MapOpeningProgressFrame:
        return MapOpeningProgressFrame(
            session_id=self._session_id,
            map_name=self._map_name or "map",
            stage=stage,
            fraction=fraction,
            # Keep the established stage → bar → note hierarchy.  This
            # session is continuous across phases, not an added visual title.
            title="",
            note=(
                self._supporting_note
                if supporting_note is None
                else str(supporting_note)
            ),
        )

    @staticmethod
    def _normalize_map_name(map_name: str) -> str:
        return " ".join(str(map_name or "map").split()) or "map"

    @staticmethod
    def _clamp_fraction(fraction: float) -> float:
        return max(0.0, min(1.0, float(fraction)))
