"""Narrow action facade for native Tk and viewer presentation work.

The immutable :mod:`presentation` profile owns process-stable UI conventions.
This adapter owns only native side effects that must happen at action time:
process DPI setup, the macOS About handler, and best-effort viewer activation.
The initial implementation delegates to the broad compatibility adapter so
platform behavior remains unchanged while native code moves in later slices.
"""

from __future__ import annotations

from typing import Any, Protocol

from .base import SplashPlatformAdapter


class PresentationActionsAdapter(Protocol):
    """Native presentation actions that do not belong in a static profile."""

    def configure_process_dpi_awareness(self) -> None:
        """Perform best-effort process-wide DPI setup before creating Tk."""

    def install_about_handler(
        self,
        root: Any,
        program_name: str,
        version: str,
    ) -> None:
        """Install the native About action for one already-created Tk root."""

    def focus_viewer_window(self, window: Any) -> None:
        """Best-effort activate an already-created viewer window."""


class PlatformPresentationActionsAdapter:
    """Compatibility facade over native presentation methods on the broad adapter."""

    def __init__(self, platform_adapter: SplashPlatformAdapter) -> None:
        self._platform_adapter = platform_adapter

    def configure_process_dpi_awareness(self) -> None:
        self._platform_adapter.configure_process_dpi_awareness()

    def install_about_handler(
        self,
        root: Any,
        program_name: str,
        version: str,
    ) -> None:
        self._platform_adapter.install_about_handler(root, program_name, version)

    def focus_viewer_window(self, window: Any) -> None:
        self._platform_adapter.focus_viewer_window(window)


def create_presentation_actions_adapter(
    platform_adapter: SplashPlatformAdapter,
) -> PresentationActionsAdapter:
    """Create the presentation-action facade for one process-owned adapter."""
    return PlatformPresentationActionsAdapter(platform_adapter)
