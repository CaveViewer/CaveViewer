"""Public factories and types for platform-specific desktop integration."""

from .desktop_services import (
    DesktopServiceError,
    DesktopServices,
    DirectorySelection,
    get_desktop_services,
)
from .factory import get_platform_adapter, get_splash_platform_adapter
from .linux import LinuxSplashPlatformAdapter
from .macos import MacOSSplashPlatformAdapter
from .windows import WindowsSplashPlatformAdapter

__all__ = [
    "get_platform_adapter",
    "get_splash_platform_adapter",
    "get_desktop_services",
    "DesktopServiceError",
    "DesktopServices",
    "DirectorySelection",
    "MacOSSplashPlatformAdapter",
    "WindowsSplashPlatformAdapter",
    "LinuxSplashPlatformAdapter",
]
