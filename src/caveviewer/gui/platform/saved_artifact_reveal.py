"""Direct native actions for revealing saved user artifacts.

The composed adapter owns only the non-executing reveal side effect. Callers
invoke it on their existing GUI thread after persistence has completed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .desktop_services import DesktopServices, get_desktop_services
from .windows_explorer import explorer_select_command


class SavedArtifactRevealAdapter(Protocol):
    """Narrow best-effort action boundary used after an artifact is saved."""

    def reveal_saved_artifact(self, output_path: str) -> None:
        """Reveal an already-saved artifact without opening or executing it."""


class UnsupportedSavedArtifactRevealAdapter:
    """Fail explicitly when the host has no safe native reveal route."""

    def reveal_saved_artifact(self, output_path: str) -> None:
        raise RuntimeError(
            f"Revealing files is unsupported on this platform: {output_path}"
        )


class WindowsSavedArtifactRevealAdapter:
    """Select a saved artifact in Explorer without opening it."""

    def reveal_saved_artifact(self, output_path: str) -> None:
        subprocess.Popen(explorer_select_command(os.fspath(Path(output_path))))


class MacOSSavedArtifactRevealAdapter:
    """Select a saved artifact in Finder without opening it."""

    def reveal_saved_artifact(self, output_path: str) -> None:
        path = Path(output_path).expanduser().absolute()
        subprocess.Popen(["open", "-R", os.fspath(path)])


@dataclass(frozen=True, slots=True)
class LinuxSavedArtifactRevealAdapter:
    """Reveal a saved artifact through the composed desktop service."""

    desktop_services: DesktopServices

    def reveal_saved_artifact(self, output_path: str) -> None:
        self.desktop_services.reveal_path(os.fspath(Path(output_path)))


def create_saved_artifact_reveal_adapter(
    *,
    platform_name: str | None = None,
    desktop_services: DesktopServices | None = None,
) -> SavedArtifactRevealAdapter:
    """Compose direct reveal behavior from stable platform facts."""
    normalized_platform = str(platform_name or sys.platform).strip().lower()
    if normalized_platform == "darwin":
        return MacOSSavedArtifactRevealAdapter()
    if normalized_platform.startswith("linux"):
        return LinuxSavedArtifactRevealAdapter(
            desktop_services=desktop_services
            or get_desktop_services(platform_name=normalized_platform)
        )
    if normalized_platform.startswith("win"):
        return WindowsSavedArtifactRevealAdapter()
    return UnsupportedSavedArtifactRevealAdapter()
