"""Linux GUI platform adapter implementation."""

from __future__ import annotations

from .desktop_services import DesktopServices, get_desktop_services
from .default import DefaultSplashPlatformAdapter


class LinuxSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    """Linux GUI integration and saved-file reveal behavior."""

    def __init__(self, *, desktop_services: DesktopServices | None = None) -> None:
        self._desktop_services = desktop_services or get_desktop_services()

    def reveal_file(self, path: str) -> None:
        """Reveal a saved user file through portal-backed desktop services."""
        self._desktop_services.reveal_path(path)
