from __future__ import annotations

import os
import subprocess

from .base import ManualInstallResult
from .default import DefaultSplashPlatformAdapter


class LinuxSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    """Linux platform adapter for update metadata and manual installer handoff."""

    def ui_font_family(self) -> str:
        return "sans-serif"

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

    def persist_downloaded_payload(self, temp_payload_path: str, download_url: str | None) -> str:
        final_path = super().persist_downloaded_payload(temp_payload_path, download_url)
        if final_path.lower().endswith(".appimage"):
            current_mode = os.stat(final_path).st_mode
            os.chmod(final_path, current_mode | 0o111)
        return final_path

    def prepare_manual_install(self, payload_path: str) -> ManualInstallResult:
        self._open_payload_location(payload_path)
        return ManualInstallResult(mounted_payload_path=None, mounted_app_path=None)

    def launch_payload_for_mode(self, mode: str, payload_path: str, log_func) -> None:
        if mode != "linux_app":
            return super().launch_payload_for_mode(mode, payload_path, log_func)
        self._open_payload_location(payload_path)
        log_func(f"Opened payload location for manual install: {payload_path}")

    def _open_payload_location(self, payload_path: str) -> None:
        containing_dir = os.path.dirname(os.path.abspath(payload_path)) or os.path.expanduser("~")
        subprocess.Popen(["xdg-open", containing_dir])

    def font_candidates(self) -> list[str]:
        """Return Linux-specific font file paths in priority order."""
        candidates = [
            # Bundled AppImage font and common Noto package locations.
            "/usr/share/caveviewer/fonts/CaveViewerUI-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
            # Debian / Ubuntu
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            # Fedora / RHEL / openSUSE
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
            "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
            "/usr/share/fonts/liberation2/LiberationSans-Regular.ttf",
        ]
        # Dynamic fallback: ask fontconfig for the best available sans-serif font.
        # This covers any distro / custom font installation automatically.
        fc_path = self._fc_match_font()
        if fc_path:
            candidates.append(fc_path)
        return candidates

    def _fc_match_font(self) -> str | None:
        """Return a font path from fontconfig, or None if unavailable."""
        try:
            result = subprocess.run(
                ["fc-match", "--format=%{file}", "sans-serif:style=Regular"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            path = result.stdout.strip()
            if path and path.lower().endswith((".ttf", ".otf", ".ttc")):
                return path
        except Exception:
            pass
        return None
