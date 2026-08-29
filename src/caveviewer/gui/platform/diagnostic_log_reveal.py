"""Native, non-executing actions for revealing diagnostic log files."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .desktop_services import DesktopServices, get_desktop_services
from .windows_explorer import explorer_select_command


class DiagnosticLogRevealAdapter(Protocol):
    """Focused platform boundary for selecting a diagnostic log."""

    def reveal_diagnostic_log(self, log_path: str) -> None:
        """Select an existing log without opening or executing it."""


class UnsupportedDiagnosticLogRevealAdapter:
    """Fail explicitly when the host has no safe native reveal route."""

    def reveal_diagnostic_log(self, log_path: str) -> None:
        raise RuntimeError(
            f"Revealing diagnostic logs is unsupported on this platform: {log_path}"
        )


class WindowsDiagnosticLogRevealAdapter:
    """Select a diagnostic log in Explorer."""

    def reveal_diagnostic_log(self, log_path: str) -> None:
        subprocess.Popen(explorer_select_command(os.fspath(Path(log_path))))


class MacOSDiagnosticLogRevealAdapter:
    """Select a diagnostic log in Finder."""

    def reveal_diagnostic_log(self, log_path: str) -> None:
        path = Path(log_path).expanduser().absolute()
        subprocess.Popen(["open", "-R", os.fspath(path)])


@dataclass(frozen=True, slots=True)
class LinuxDiagnosticLogRevealAdapter:
    """Reveal a diagnostic log through the composed desktop service."""

    desktop_services: DesktopServices

    def reveal_diagnostic_log(self, log_path: str) -> None:
        self.desktop_services.reveal_path(os.fspath(Path(log_path)))


def create_diagnostic_log_reveal_adapter(
    *,
    platform_name: str | None = None,
    desktop_services: DesktopServices | None = None,
) -> DiagnosticLogRevealAdapter:
    """Compose safe log-reveal behavior from stable platform facts."""

    normalized_platform = str(platform_name or sys.platform).strip().lower()
    if normalized_platform == "darwin":
        return MacOSDiagnosticLogRevealAdapter()
    if normalized_platform.startswith("linux"):
        return LinuxDiagnosticLogRevealAdapter(
            desktop_services=desktop_services
            or get_desktop_services(platform_name=normalized_platform)
        )
    if normalized_platform.startswith("win"):
        return WindowsDiagnosticLogRevealAdapter()
    return UnsupportedDiagnosticLogRevealAdapter()
