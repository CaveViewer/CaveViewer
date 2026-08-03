"""Factory for selecting the active GUI platform adapter.

All GUI code that needs OS-specific behavior should enter through this module
or ``caveviewer.gui.platform`` exports instead of checking ``sys.platform``
directly.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from .base import SplashPlatformAdapter
from .default import DefaultSplashPlatformAdapter
from .linux import LinuxSplashPlatformAdapter
from .macos import MacOSSplashPlatformAdapter
from .windows import WindowsSplashPlatformAdapter

if TYPE_CHECKING:
    from .desktop_services import DesktopServices


def get_platform_adapter(
    *,
    desktop_services: "DesktopServices | None" = None,
    platform_name: str | None = None,
) -> SplashPlatformAdapter:
    """Create the adapter for a selected platform without retaining globals.

    Existing callers may continue to omit both arguments.  A composition root
    can inject one ``DesktopServices`` instance so Linux package reveal and
    other desktop actions use the same portal-first service.
    """
    resolved_platform_name = platform_name or sys.platform
    if resolved_platform_name == "darwin":
        return MacOSSplashPlatformAdapter()
    if resolved_platform_name.startswith("win"):
        return WindowsSplashPlatformAdapter()
    if resolved_platform_name.startswith("linux"):
        return LinuxSplashPlatformAdapter(desktop_services=desktop_services)
    return DefaultSplashPlatformAdapter()


def get_splash_platform_adapter(
    *,
    desktop_services: "DesktopServices | None" = None,
    platform_name: str | None = None,
) -> SplashPlatformAdapter:
    """Compatibility name for callers that still request the broad adapter."""
    return get_platform_adapter(
        desktop_services=desktop_services,
        platform_name=platform_name,
    )
