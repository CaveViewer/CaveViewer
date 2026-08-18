"""Atomically promote verified update packages into user-visible Downloads."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class UpdatePackageStorageAdapter(Protocol):
    """Narrow native action boundary used after an update package is verified."""

    def persist_verified_package(
        self,
        temporary_payload_path: str,
        download_url: str | None,
    ) -> str:
        """Atomically publish a verified temporary package and return its path."""


@dataclass(frozen=True, slots=True)
class DefaultUpdatePackageStorageAdapter:
    """Store verified packages in Downloads with generic package naming."""

    downloads_dir: Path | str | None = None

    def persist_verified_package(
        self,
        temporary_payload_path: str,
        download_url: str | None,
    ) -> str:
        """Promote the payload through a hidden sibling in Downloads."""
        return _promote_verified_package(
            temporary_payload_path=Path(temporary_payload_path),
            downloads_dir=_resolve_downloads_dir(self.downloads_dir),
            filename=_generic_package_filename(download_url),
            make_executable=False,
        )


@dataclass(frozen=True, slots=True)
class MacOSUpdatePackageStorageAdapter:
    """Store verified macOS packages using the established DMG naming rules."""

    downloads_dir: Path | str | None = None

    def persist_verified_package(
        self,
        temporary_payload_path: str,
        download_url: str | None,
    ) -> str:
        """Promote the payload through a hidden sibling in Downloads."""
        return _promote_verified_package(
            temporary_payload_path=Path(temporary_payload_path),
            downloads_dir=_resolve_downloads_dir(self.downloads_dir),
            filename=_macos_package_filename(download_url),
            make_executable=False,
        )


@dataclass(frozen=True, slots=True)
class LinuxUpdatePackageStorageAdapter:
    """Store verified Linux packages and mark AppImages executable before publish."""

    downloads_dir: Path | str | None = None

    def persist_verified_package(
        self,
        temporary_payload_path: str,
        download_url: str | None,
    ) -> str:
        """Promote the payload through a hidden sibling in Downloads."""
        filename = _generic_package_filename(download_url)
        return _promote_verified_package(
            temporary_payload_path=Path(temporary_payload_path),
            downloads_dir=_resolve_downloads_dir(self.downloads_dir),
            filename=filename,
            make_executable=filename.lower().endswith(".appimage"),
        )


def create_update_package_storage_adapter(
    *,
    platform_name: str | None = None,
) -> UpdatePackageStorageAdapter:
    """Compose direct storage behavior for the selected operating-system target."""
    normalized_platform = str(platform_name or sys.platform).strip().lower()
    if normalized_platform == "darwin":
        return MacOSUpdatePackageStorageAdapter()
    if normalized_platform.startswith("linux"):
        return LinuxUpdatePackageStorageAdapter()
    return DefaultUpdatePackageStorageAdapter()


def _resolve_downloads_dir(configured_dir: Path | str | None) -> Path:
    """Return the configured test destination or the user's Downloads directory."""
    if configured_dir is not None:
        return Path(configured_dir)
    return Path(os.path.expanduser("~")) / "Downloads"


def _generic_package_filename(download_url: str | None) -> str:
    """Preserve the existing generic URL-basename and fallback naming behavior."""
    if download_url:
        basename = os.path.basename(download_url.split("?", 1)[0]).strip()
        if basename:
            return basename
    return "CaveViewer-update.bin"


def _macos_package_filename(download_url: str | None) -> str:
    """Preserve macOS's DMG-only filename and fallback behavior."""
    filename = _generic_package_filename(download_url)
    if filename.lower().endswith(".dmg"):
        return filename
    return "CaveViewer-latest.dmg"


def _promote_verified_package(
    *,
    temporary_payload_path: Path,
    downloads_dir: Path,
    filename: str,
    make_executable: bool,
) -> str:
    """Copy a verified payload through a hidden sibling before atomically publishing it."""
    downloads_dir.mkdir(parents=True, exist_ok=True)
    final_path = _non_conflicting_path(downloads_dir, filename)
    hidden_path: Path | None = None

    try:
        descriptor, hidden_name = tempfile.mkstemp(
            prefix=f".{final_path.name}.",
            suffix=".tmp",
            dir=downloads_dir,
        )
        hidden_path = Path(hidden_name)
        with temporary_payload_path.open("rb") as source, os.fdopen(
            descriptor, "wb"
        ) as destination:
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())

        # A cross-filesystem shutil.move() used copy2(), which preserved mode
        # bits. Retain that behavior before applying the Linux AppImage bit.
        shutil.copymode(temporary_payload_path, hidden_path)
        if make_executable:
            os.chmod(hidden_path, hidden_path.stat().st_mode | 0o111)

        os.replace(hidden_path, final_path)
        hidden_path = None
        return str(final_path)
    finally:
        if hidden_path is not None:
            try:
                hidden_path.unlink(missing_ok=True)
            except OSError:
                pass


def _non_conflicting_path(downloads_dir: Path, filename: str) -> Path:
    """Return the existing Downloads collision suffix convention for ``filename``."""
    final_path = downloads_dir / filename
    if not final_path.exists():
        return final_path

    basename, extension = os.path.splitext(filename)
    suffix = 1
    while True:
        candidate = downloads_dir / f"{basename}-{suffix}{extension}"
        if not candidate.exists():
            return candidate
        suffix += 1
