"""Public factories and types for platform-specific desktop integration."""

from .app_identity import LINUX_WINDOW_INSTANCE_NAME, tk_root_options
from .desktop_services import (
    DesktopServiceError,
    DesktopInhibitor,
    DesktopServices,
    DirectorySelection,
    FileSelection,
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
    "LINUX_WINDOW_INSTANCE_NAME",
    "tk_root_options",
    "DesktopServiceError",
    "DesktopInhibitor",
    "DesktopServices",
    "DirectorySelection",
    "FileSelection",
    "MacOSSplashPlatformAdapter",
    "WindowsSplashPlatformAdapter",
    "LinuxSplashPlatformAdapter",
]
