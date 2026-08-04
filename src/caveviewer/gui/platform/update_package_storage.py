"""Focused adapter for retaining verified update packages in user-visible storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .base import SplashPlatformAdapter


class UpdatePackageStorageAdapter(Protocol):
    """Narrow native action boundary used after an update package is verified."""

    def persist_verified_package(
        self,
        temporary_payload_path: str,
        download_url: str | None,
    ) -> str:
        """Move a verified temporary package into user-visible storage and return it."""


@dataclass(frozen=True, slots=True)
class PlatformUpdatePackageStorageAdapter:
    """Compatibility facade over established platform-specific package storage.

    The existing broad adapter retains its filename, collision, and platform
    handling for now, including macOS DMG naming and Linux AppImage permissions.
    Update consumers depend only on this focused facade, so native storage
    implementations can later move here without changing the update workflow.
    """

    platform_adapter: SplashPlatformAdapter

    def persist_verified_package(
        self,
        temporary_payload_path: str,
        download_url: str | None,
    ) -> str:
        """Delegate promotion of the verified payload to existing native behavior."""
        return self.platform_adapter.persist_downloaded_payload(
            temporary_payload_path,
            download_url,
        )


def create_update_package_storage_adapter(
    platform_adapter: SplashPlatformAdapter,
) -> PlatformUpdatePackageStorageAdapter:
    """Compose the focused storage action for the selected platform adapter."""
    return PlatformUpdatePackageStorageAdapter(platform_adapter=platform_adapter)
