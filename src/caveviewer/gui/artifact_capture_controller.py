"""Shared render-thread presentation state for saved user artifacts.

Video recording and Guided Dive tracing have independent capture and writer
implementations.  This controller deliberately owns only the common user
experience once a capture is being saved: persistent progress feedback, a
fixed success confirmation, and a delayed best-effort native file reveal.
"""

from __future__ import annotations

from dataclasses import dataclass, field


SAVED_ARTIFACT_CONFIRMATION_SECONDS = 3.0


@dataclass(frozen=True)
class ArtifactCaptureStatus:
    """One user-facing capture status ready for the viewer overlay."""

    message: str
    detail: str | None
    kind: str
    duration: float | None


@dataclass(frozen=True)
class SavedArtifactRevealRequest:
    """A completed artifact whose native reveal may now be performed."""

    output_path: str
    artifact_name: str


@dataclass
class ArtifactCapturePresentationController:
    """Create consistent post-capture feedback without owning file writers."""

    confirmation_seconds: float = SAVED_ARTIFACT_CONFIRMATION_SECONDS
    _pending_reveals: list[tuple[float, SavedArtifactRevealRequest]] = field(
        default_factory=list
    )

    @property
    def has_pending_reveals(self) -> bool:
        """Return whether checking the clock can result in a native reveal."""
        return bool(self._pending_reveals)

    def saving_status(self, artifact_name: str) -> ArtifactCaptureStatus:
        """Return a persistent status while a writer finalizes an artifact."""
        return ArtifactCaptureStatus(
            message=f"Saving {artifact_name.lower()}…",
            detail="Finishing the file. Keep CaveViewer open.",
            kind="info",
            duration=None,
        )

    def exit_saving_status(
        self,
        artifact_names: tuple[str, ...],
    ) -> ArtifactCaptureStatus:
        """Return the persistent status shown while CaveViewer exits safely."""
        names = tuple(dict.fromkeys(name for name in artifact_names if name))
        if names == ("Video",):
            message = "Finishing video"
            detail = "Saving the last frames. CaveViewer will close automatically."
        elif names == ("Dive trace",):
            message = "Finishing dive trace"
            detail = "Saving the final trace. CaveViewer will close automatically."
        elif names == ("Slice",):
            message = "Finishing slice"
            detail = "Saving the final slice. CaveViewer will close automatically."
        else:
            message = "Finishing captures"
            detail = (
                f"Saving {_join_capture_names(names)}. "
                "CaveViewer will close automatically."
            )
        return ArtifactCaptureStatus(
            message=message,
            detail=detail,
            kind="info",
            duration=None,
        )

    def canceled_status(self, artifact_name: str) -> ArtifactCaptureStatus:
        """Return the shared countdown-cancellation confirmation."""
        return ArtifactCaptureStatus(
            message=f"{artifact_name} canceled",
            detail=None,
            kind="cancel",
            duration=self.confirmation_seconds,
        )

    def saved_status(
        self,
        artifact_name: str,
        output_path: str | None,
        *,
        now: float,
        reveal: bool = True,
    ) -> ArtifactCaptureStatus:
        """Confirm publication and defer native reveal until it has been seen."""
        if output_path and reveal:
            request = SavedArtifactRevealRequest(
                output_path=output_path,
                artifact_name=artifact_name,
            )
            self._pending_reveals.append((now + self.confirmation_seconds, request))
            detail = "Opening its location…"
        else:
            detail = None
        return ArtifactCaptureStatus(
            message=f"{artifact_name} saved",
            detail=detail,
            kind="success",
            duration=self.confirmation_seconds,
        )

    def failed_status(
        self,
        artifact_name: str,
        detail: str | None,
    ) -> ArtifactCaptureStatus:
        """Return the shared failure confirmation and never schedule a reveal."""
        return ArtifactCaptureStatus(
            message=f"Could not save {artifact_name.lower()}",
            detail=detail,
            kind="error",
            duration=self.confirmation_seconds,
        )

    def take_due_reveals(self, *, now: float) -> tuple[SavedArtifactRevealRequest, ...]:
        """Return artifacts whose visible confirmation period has elapsed."""
        due: list[SavedArtifactRevealRequest] = []
        remaining: list[tuple[float, SavedArtifactRevealRequest]] = []
        for reveal_at, request in self._pending_reveals:
            if now >= reveal_at:
                due.append(request)
            else:
                remaining.append((reveal_at, request))
        self._pending_reveals = remaining
        return tuple(due)

    def discard_pending_reveals(self) -> None:
        """Prevent delayed file-browser launches while the application exits."""
        self._pending_reveals.clear()


def _join_capture_names(names: tuple[str, ...]) -> str:
    """Return the lowercase, natural-language capture list used on exit."""
    lowered = tuple(name.lower() for name in names)
    if not lowered:
        return "the capture files"
    if len(lowered) == 1:
        return f"the {lowered[0]}"
    if len(lowered) == 2:
        return f"the {lowered[0]} and {lowered[1]}"
    return f"the {', '.join(lowered[:-1])}, and {lowered[-1]}"
