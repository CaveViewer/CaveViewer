from __future__ import annotations

import sys

from .base import SplashPlatformAdapter
from .default import DefaultSplashPlatformAdapter
from .linux import LinuxSplashPlatformAdapter
from .macos import MacOSSplashPlatformAdapter
from .windows import WindowsSplashPlatformAdapter


def get_platform_adapter() -> SplashPlatformAdapter:
    if sys.platform == "darwin":
        return MacOSSplashPlatformAdapter()
    if sys.platform.startswith("win"):
        return WindowsSplashPlatformAdapter()
    if sys.platform.startswith("linux"):
        return LinuxSplashPlatformAdapter()
    return DefaultSplashPlatformAdapter()


def get_splash_platform_adapter() -> SplashPlatformAdapter:
    return get_platform_adapter()
