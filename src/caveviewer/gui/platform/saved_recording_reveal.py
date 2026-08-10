"""Compatibility imports for the former recording-specific reveal adapter.

New capture features should use :mod:`saved_artifact_reveal`; these aliases
retain the previous import path while callers migrate.
"""

from __future__ import annotations

from typing import Protocol

from .saved_artifact_reveal import (
    PlatformSavedArtifactRevealAdapter,
    create_saved_artifact_reveal_adapter,
)

class SavedRecordingRevealAdapter(Protocol):
    """Former recording-only contract retained for source compatibility."""

    def reveal_saved_recording(self, output_path: str) -> None:
        """Reveal an already-saved recording without opening or executing it."""


PlatformSavedRecordingRevealAdapter = PlatformSavedArtifactRevealAdapter
create_saved_recording_reveal_adapter = create_saved_artifact_reveal_adapter
