"""Focused adapter for exposing verified update packages without executing them."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol

from caveviewer.core.capabilities import UpdatePackageRevealRoute

from .base import SplashPlatformAdapter


class UpdatePackageRevealAdapter(Protocol):
    """Narrow native action boundary used after an update download is verified."""

    def reveal_route(self) -> UpdatePackageRevealRoute | None:
        """Return the process-stable route, or ``None`` when unsupported."""

    def reveal_action_label(self) -> str:
        """Return the concise label for a user-invoked reveal action."""

    def reveal_verified_package(self, payload_path: str) -> None:
        """Expose a verified package without opening, executing, or installing it."""


@dataclass(frozen=True, slots=True)
class PlatformUpdatePackageRevealAdapter:
    """Compatibility facade over the existing broad platform adapter.

    Package-specific behavior remains in the established adapters for now,
    including macOS's read-only DMG mount path. Consumers depend only on this
    narrow façade, so later extraction can move those implementations without
    changing the update workflow again.
    """

    platform_adapter: SplashPlatformAdapter
    selected_route: UpdatePackageRevealRoute | None

    def reveal_route(self) -> UpdatePackageRevealRoute | None:
        """Return the route selected at process composition time."""
        return self.selected_route

    def reveal_action_label(self) -> str:
        """Delegate only the user-visible reveal label to the compatibility API."""
        return self.platform_adapter.download_reveal_action_label()

    def reveal_verified_package(self, payload_path: str) -> None:
        """Delegate the non-executing reveal to the established native behavior."""
        self.platform_adapter.reveal_downloaded_payload(payload_path)


def create_update_package_reveal_adapter(
    platform_adapter: SplashPlatformAdapter,
    *,
    platform_name: str | None = None,
) -> PlatformUpdatePackageRevealAdapter:
    """Compose the focused adapter for a selected operating-system target."""
    return PlatformUpdatePackageRevealAdapter(
        platform_adapter=platform_adapter,
        selected_route=_route_for_platform(platform_name or sys.platform),
    )


def create_legacy_update_package_reveal_adapter(
    platform_adapter: SplashPlatformAdapter,
) -> PlatformUpdatePackageRevealAdapter:
    """Preserve direct legacy manager callers without pretending route certainty."""
    return PlatformUpdatePackageRevealAdapter(
        platform_adapter=platform_adapter,
        selected_route=UpdatePackageRevealRoute.LEGACY_ADAPTER,
    )


def _route_for_platform(platform_name: str) -> UpdatePackageRevealRoute | None:
    normalized = str(platform_name).strip().lower()
    if normalized == "darwin":
        return UpdatePackageRevealRoute.FINDER
    if normalized.startswith("win"):
        return UpdatePackageRevealRoute.EXPLORER
    if normalized.startswith("linux"):
        return UpdatePackageRevealRoute.DESKTOP_SERVICE
    return None
