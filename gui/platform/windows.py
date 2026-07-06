from __future__ import annotations

import os
import subprocess

from .base import ManualInstallResult
from .default import DefaultSplashPlatformAdapter


class WindowsSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    """Windows platform adapter for update metadata and manual installer handoff."""

    def default_update_repo(self) -> str:
        return super().default_update_repo()

    def default_update_manifest_url(self, repo: str, branch: str) -> str:
        return f"https://raw.githubusercontent.com/{repo}/{branch}/updates/windows/stable.json"

    def install_channel(self) -> str:
        return "windows_app"

    def supports_install_channel(self, channel: str) -> bool:
        return channel == "windows_app"

    def unsupported_install_channel_message(self, channel: str) -> str:
        return (
            f"Unsupported install channel '{channel}' on Windows. "
            "Expected channel: windows_app."
        )

    def channel_download_url_keys(self, channel: str) -> tuple[str, ...]:
        if channel == "windows_app":
            return (
                "download_url_windows_msi",
                "download_url_windows_exe",
                "download_url_windows_zip",
                "download_url_windows",
            )
        return super().channel_download_url_keys(channel)

    def channel_download_size_keys(self, channel: str) -> tuple[str, ...]:
        if channel == "windows_app":
            return (
                "download_size_bytes_windows_msi",
                "download_size_bytes_windows_exe",
                "download_size_bytes_windows_zip",
                "download_size_bytes_windows",
            )
        return super().channel_download_size_keys(channel)

    def channel_sha256_keys(self, channel: str) -> tuple[str, ...]:
        if channel == "windows_app":
            return (
                "sha256_windows_msi",
                "sha256_windows_exe",
                "sha256_windows_zip",
                "sha256_windows",
            )
        return super().channel_sha256_keys(channel)

    def missing_download_url_message(self, channel: str) -> str:
        if channel == "windows_app":
            return "Update manifest is missing a Windows download URL."
        return super().missing_download_url_message(channel)

    def updater_supported_modes(self) -> set[str]:
        return {"windows_app"}

    def prepare_manual_install(self, payload_path: str) -> ManualInstallResult:
        self._open_payload(payload_path)
        return ManualInstallResult(mounted_payload_path=None, mounted_app_path=None)

    def launch_payload_for_mode(self, mode: str, payload_path: str, log_func) -> None:
        if mode != "windows_app":
            return super().launch_payload_for_mode(mode, payload_path, log_func)
        self._open_payload(payload_path)
        log_func(f"Opened payload for manual install: {payload_path}")

    def _open_payload(self, payload_path: str) -> None:
        if hasattr(os, "startfile"):
            os.startfile(payload_path)  # type: ignore[attr-defined]
            return
        subprocess.Popen(["explorer", payload_path])

    def font_candidates(self) -> list[str]:
        """Return Windows-specific font file paths in priority order."""
        return [
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/tahoma.ttf",
            "C:/Windows/Fonts/verdana.ttf",
            "C:/Windows/Fonts/consola.ttf",
        ]
