"""Focused adapter for platform-specific recording encoder process startup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .base import SplashPlatformAdapter


class RecordingProcessAdapter(Protocol):
    """Narrow boundary for platform-owned ffmpeg ``Popen`` configuration."""

    def encoder_popen_kwargs(self) -> dict[str, Any]:
        """Return non-command ``Popen`` options for a recording encoder."""


@dataclass(frozen=True, slots=True)
class PlatformRecordingProcessAdapter:
    """Compatibility facade over established platform-specific launch options.

    The broad adapter keeps its current behavior for now, including Windows
    console suppression through ``STARTUPINFO`` and ``CREATE_NO_WINDOW``.
    Recording workflow code depends only on this focused facade, so native
    process startup can later move here without changing capability policy,
    ffmpeg command construction, or session ownership.
    """

    platform_adapter: SplashPlatformAdapter

    def encoder_popen_kwargs(self) -> dict[str, Any]:
        """Delegate recording-process options to existing native behavior."""
        return self.platform_adapter.recording_subprocess_startup_kwargs()


def create_recording_process_adapter(
    platform_adapter: SplashPlatformAdapter,
) -> PlatformRecordingProcessAdapter:
    """Compose the focused encoder-process action for a platform adapter."""
    return PlatformRecordingProcessAdapter(platform_adapter=platform_adapter)
