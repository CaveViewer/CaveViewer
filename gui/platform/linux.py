from __future__ import annotations

import subprocess

from .base import ManualInstallResult
from .default import DefaultSplashPlatformAdapter


class LinuxSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    """Linux platform adapter for update metadata and manual installer handoff."""

    def default_update_manifest_url(self, repo: str) -> str:
        return f"https://raw.githubusercontent.com/{repo}/main/updates/linux/stable.json"

    def install_channel(self) -> str:
        return "linux_app"

    def supports_install_channel(self, channel: str) -> bool:
        return channel == "linux_app"

    def unsupported_install_channel_message(self, channel: str) -> str:
        return (
            f"Unsupported install channel '{channel}' on Linux. "
            "Expected channel: linux_app."
        )

    def channel_download_url_keys(self, channel: str) -> tuple[str, ...]:
        if channel == "linux_app":
            return (
                "download_url_linux_appimage",
                "download_url_linux_deb",
                "download_url_linux_rpm",
                "download_url_linux_tar_gz",
                "download_url_linux",
                "download_url",
            )
        return super().channel_download_url_keys(channel)

    def channel_download_size_keys(self, channel: str) -> tuple[str, ...]:
        if channel == "linux_app":
            return (
                "download_size_bytes_linux_appimage",
                "download_size_bytes_linux_deb",
                "download_size_bytes_linux_rpm",
                "download_size_bytes_linux_tar_gz",
                "download_size_bytes_linux",
                "download_size_bytes",
            )
        return super().channel_download_size_keys(channel)

    def channel_sha256_keys(self, channel: str) -> tuple[str, ...]:
        if channel == "linux_app":
            return (
                "sha256_linux_appimage",
                "sha256_linux_deb",
                "sha256_linux_rpm",
                "sha256_linux_tar_gz",
                "sha256_linux",
                "sha256",
            )
        return super().channel_sha256_keys(channel)

    def missing_download_url_message(self, channel: str) -> str:
        if channel == "linux_app":
            return "Update manifest is missing a Linux download URL."
        return super().missing_download_url_message(channel)

    def updater_supported_modes(self) -> set[str]:
        return {"linux_app"}

    def prepare_manual_install(self, payload_path: str) -> ManualInstallResult:
        self._open_payload(payload_path)
        return ManualInstallResult(mounted_payload_path=None, mounted_app_path=None)

    def launch_payload_for_mode(self, mode: str, payload_path: str, log_func) -> None:
        if mode != "linux_app":
            return super().launch_payload_for_mode(mode, payload_path, log_func)
        self._open_payload(payload_path)
        log_func(f"Opened payload for manual install: {payload_path}")

    def _open_payload(self, payload_path: str) -> None:
        subprocess.Popen(["xdg-open", payload_path])

    def font_candidates(self) -> list[str]:
        """Return Linux-specific font file paths in priority order."""
        return [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]