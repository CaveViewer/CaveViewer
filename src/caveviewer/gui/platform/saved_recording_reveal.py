"""Focused adapter for revealing a saved recording without opening it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .base import SplashPlatformAdapter


class SavedRecordingRevealAdapter(Protocol):
    """Narrow best-effort action boundary used after a recording is saved."""

    def reveal_saved_recording(self, output_path: str) -> None:
        """Reveal an already-saved recording without opening or executing it."""


@dataclass(frozen=True, slots=True)
class PlatformSavedRecordingRevealAdapter:
    """Compatibility facade over established platform-specific file reveal.

    The broad adapter retains its existing Finder, Explorer, and Linux desktop
    service behavior for now. Recording completion depends only on this narrow
    facade, so the native implementations can later move here without changing
    recording lifecycle or success-state handling.
    """

    platform_adapter: SplashPlatformAdapter

    def reveal_saved_recording(self, output_path: str) -> None:
        """Delegate non-executing reveal to existing native behavior."""
        self.platform_adapter.reveal_file(output_path)


def create_saved_recording_reveal_adapter(
    platform_adapter: SplashPlatformAdapter,
) -> PlatformSavedRecordingRevealAdapter:
    """Compose the focused recording-reveal action for a platform adapter."""
    return PlatformSavedRecordingRevealAdapter(platform_adapter=platform_adapter)
