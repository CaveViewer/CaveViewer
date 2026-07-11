from __future__ import annotations

import os
import subprocess

from .default import DefaultSplashPlatformAdapter


class WindowsSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    """Windows update metadata and manual package-reveal integration."""

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

    def download_reveal_action_label(self) -> str:
        return "Show in Explorer"

    def reveal_downloaded_payload(self, payload_path: str) -> None:
        # /select reveals the package without opening or executing it.
        normalized_path = os.path.normpath(os.path.abspath(payload_path))
        subprocess.Popen(["explorer", f"/select,{normalized_path}"])

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
