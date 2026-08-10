"""Focused adapter for revealing a saved user artifact without opening it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .base import SplashPlatformAdapter


class SavedArtifactRevealAdapter(Protocol):
    """Narrow best-effort action boundary used after an artifact is saved."""

    def reveal_saved_artifact(self, output_path: str) -> None:
        """Reveal an already-saved artifact without opening or executing it."""


@dataclass(frozen=True, slots=True)
class PlatformSavedArtifactRevealAdapter:
    """Compatibility facade over established platform-specific file reveal.

    The broad adapter retains its existing Finder, Explorer, and Linux desktop
    service behavior for now. Artifact completion depends only on this narrow
    facade, so native implementations can later move here without changing
    video or trace writer lifecycles.
    """

    platform_adapter: SplashPlatformAdapter

    def reveal_saved_artifact(self, output_path: str) -> None:
        """Delegate non-executing reveal to existing native behavior."""
        self.platform_adapter.reveal_file(output_path)


def create_saved_artifact_reveal_adapter(
    platform_adapter: SplashPlatformAdapter,
) -> PlatformSavedArtifactRevealAdapter:
    """Compose the focused artifact-reveal action for a platform adapter."""
    return PlatformSavedArtifactRevealAdapter(platform_adapter=platform_adapter)
