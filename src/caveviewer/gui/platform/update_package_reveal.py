"""Reveal verified update packages through focused non-executing adapters."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from typing import Protocol

from caveviewer.core.capabilities import UpdatePackageRevealRoute

from .desktop_services import DesktopServices, get_desktop_services
from .windows_explorer import explorer_select_command


class UpdatePackageRevealAdapter(Protocol):
    """Narrow native action boundary used after an update download is verified."""

    def reveal_route(self) -> UpdatePackageRevealRoute | None:
        """Return the process-stable route, or ``None`` when unsupported."""

    def reveal_action_label(self) -> str:
        """Return the concise label for a user-invoked reveal action."""

    def reveal_verified_package(self, payload_path: str) -> None:
        """Expose a verified package without opening, executing, or installing it."""


class MacOSUpdatePackageRevealAdapter:
    """Reveal macOS update packages through Finder without executing them."""

    def __init__(self) -> None:
        # Reuse an existing mount for repeated reveal actions instead of
        # attaching the same DMG once per click.
        self._mounted_payloads: dict[str, tuple[str, str]] = {}

    def reveal_route(self) -> UpdatePackageRevealRoute:
        """Declare the static Finder reveal route without native work."""
        return UpdatePackageRevealRoute.FINDER

    def reveal_action_label(self) -> str:
        """Return the platform-native update-package action label."""
        return "Show in Finder"

    def reveal_verified_package(self, payload_path: str) -> None:
        """Mount a DMG read-only or select another verified package in Finder."""
        payload_path = os.path.abspath(payload_path)
        if not payload_path.lower().endswith(".dmg"):
            subprocess.Popen(["open", "-R", payload_path])
            return

        cached = self._mounted_payloads.get(payload_path)
        if cached is not None:
            mountpoint, reveal_path = cached
            if os.path.exists(mountpoint) and os.path.exists(reveal_path):
                self._reveal_in_finder(mountpoint, reveal_path)
                return
            self._mounted_payloads.pop(payload_path, None)

        completed = subprocess.run(
            [
                "hdiutil",
                "attach",
                payload_path,
                "-nobrowse",
                "-readonly",
                "-plist",
            ],
            check=True,
            capture_output=True,
        )
        attach_result = plistlib.loads(completed.stdout)
        mountpoint = next(
            (
                entity.get("mount-point")
                for entity in attach_result.get("system-entities", ())
                if entity.get("mount-point")
            ),
            None,
        )
        if not mountpoint:
            raise RuntimeError(f"Mounted DMG did not report a mount point: {payload_path}")

        app_path = None
        for root_dir, dir_names, _ in os.walk(mountpoint):
            for dir_name in dir_names:
                if dir_name.endswith(".app"):
                    app_path = os.path.join(root_dir, dir_name)
                    break
            if app_path:
                break

        reveal_path = app_path or mountpoint
        self._mounted_payloads[payload_path] = (mountpoint, reveal_path)
        self._reveal_in_finder(mountpoint, reveal_path)

    @staticmethod
    def _reveal_in_finder(mountpoint: str, reveal_path: str) -> None:
        if reveal_path != mountpoint:
            subprocess.Popen(["open", "-R", reveal_path])
        else:
            subprocess.Popen(["open", mountpoint])


class WindowsUpdatePackageRevealAdapter:
    """Select verified update packages in Explorer without opening them."""

    def reveal_route(self) -> UpdatePackageRevealRoute:
        """Declare the static Explorer reveal route without native work."""
        return UpdatePackageRevealRoute.EXPLORER

    def reveal_action_label(self) -> str:
        """Return the platform-native update-package action label."""
        return "Show in Explorer"

    def reveal_verified_package(self, payload_path: str) -> None:
        """Select the verified package in Explorer without executing it."""
        subprocess.Popen(explorer_select_command(payload_path))


class LinuxUpdatePackageRevealAdapter:
    """Reveal verified packages through the injected Linux desktop service."""

    def __init__(self, *, desktop_services: DesktopServices) -> None:
        self._desktop_services = desktop_services

    def reveal_route(self) -> UpdatePackageRevealRoute:
        """Declare the static desktop-service route without contacting it."""
        return UpdatePackageRevealRoute.DESKTOP_SERVICE

    def reveal_action_label(self) -> str:
        """Return the platform-native update-package action label."""
        return "Open Download Folder"

    def reveal_verified_package(self, payload_path: str) -> None:
        """Ask the desktop service to reveal the verified package's folder."""
        self._desktop_services.reveal_path(payload_path)


class UnsupportedUpdatePackageRevealAdapter:
    """Safely reject package reveal on platforms without a declared route."""

    def reveal_route(self) -> None:
        """Declare that this platform cannot reveal verified update packages."""
        return None

    def reveal_action_label(self) -> str:
        """Retain the generic label even though the unavailable route is gated off."""
        return "Open Download Folder"

    def reveal_verified_package(self, payload_path: str) -> None:
        """Fail closed if a caller bypasses the unavailable reveal gate."""
        raise RuntimeError(
            f"Revealing downloaded packages is unsupported on this platform: "
            f"{payload_path}"
        )


def create_update_package_reveal_adapter(
    *,
    platform_name: str | None = None,
    desktop_services: DesktopServices | None = None,
) -> UpdatePackageRevealAdapter:
    """Compose direct package-reveal behavior for the selected platform."""
    normalized_platform = str(platform_name or sys.platform).strip().lower()
    if normalized_platform == "darwin":
        return MacOSUpdatePackageRevealAdapter()
    if normalized_platform.startswith("win"):
        return WindowsUpdatePackageRevealAdapter()
    if normalized_platform.startswith("linux"):
        return LinuxUpdatePackageRevealAdapter(
            desktop_services=desktop_services
            or get_desktop_services(platform_name=normalized_platform)
        )
    return UnsupportedUpdatePackageRevealAdapter()
