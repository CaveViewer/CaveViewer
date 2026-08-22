"""Linux GUI platform adapter implementation."""

from __future__ import annotations

from .desktop_services import DesktopServices, get_desktop_services
from .default import DefaultSplashPlatformAdapter


class LinuxSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    """Frozen Linux compatibility object pending broad-adapter deletion."""

    def __init__(self, *, desktop_services: DesktopServices | None = None) -> None:
        self._desktop_services = desktop_services or get_desktop_services()
