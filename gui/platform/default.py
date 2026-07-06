from __future__ import annotations

import os
import shutil

from .base import ManualInstallResult, SplashPlatformAdapter


class DefaultSplashPlatformAdapter(SplashPlatformAdapter):
    """Conservative defaults for non-macOS platforms."""

    def ui_font_family(self) -> str:
        return "Segoe UI"

    def install_about_handler(self, root, program_name: str, version: str) -> None:
        # No platform-specific About menu integration outside macOS.
        return None

    def install_channel(self) -> str:
        return "unsupported"

    def persist_downloaded_payload(self, temp_payload_path: str, download_url: str | None) -> str:
        downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        os.makedirs(downloads_dir, exist_ok=True)

        url_basename = ""
        if download_url:
            url_basename = os.path.basename(download_url.split("?", 1)[0]).strip()
        if not url_basename:
            url_basename = "CaveViewer-update.bin"

        final_path = os.path.join(downloads_dir, url_basename)
        if os.path.exists(final_path):
            base, ext = os.path.splitext(final_path)
            suffix = 1
            candidate = f"{base}-{suffix}{ext}"
            while os.path.exists(candidate):
                suffix += 1
                candidate = f"{base}-{suffix}{ext}"
            final_path = candidate

        shutil.move(temp_payload_path, final_path)
        return final_path

    def prepare_manual_install(self, payload_path: str) -> ManualInstallResult:
        raise RuntimeError(
            "Automatic installer handoff is not implemented for this platform yet. "
            "Open the downloaded payload manually."
        )

    def default_update_repo(self) -> str:
        return "KernalPanic/CaveViewer"

    def default_update_manifest_url(self, repo: str, branch: str) -> str:
        return f"https://raw.githubusercontent.com/{repo}/{branch}/updates/macos/stable.json"

    def update_check_user_agent(self) -> str:
        return "CaveViewer-UpdateChecker"

    def supports_install_channel(self, channel: str) -> bool:
        return False

    def unsupported_install_channel_message(self, channel: str) -> str:
        return f"Unsupported install channel '{channel}'."

    def channel_download_url_keys(self, channel: str) -> tuple[str, ...]:
        return ("download_url",)

    def channel_download_size_keys(self, channel: str) -> tuple[str, ...]:
        return ("download_size_bytes",)

    def channel_sha256_keys(self, channel: str) -> tuple[str, ...]:
        return ("sha256",)

    def missing_download_url_message(self, channel: str) -> str:
        return "Update manifest is missing required field: download_url."

    def detect_package_kind(self, download_url: str, channel: str) -> str:
        url = (download_url or "").strip().lower()
        if not url:
            return "unknown"
        if url.endswith(".tar.gz"):
            return "tar.gz"
        if url.endswith(".appimage"):
            return "appimage"
        if url.endswith(".msi"):
            return "msi"
        if url.endswith(".exe"):
            return "exe"
        if url.endswith(".deb"):
            return "deb"
        if url.endswith(".rpm"):
            return "rpm"
        if url.endswith(".dmg"):
            return "dmg"
        if url.endswith(".pkg"):
            return "pkg"
        if url.endswith(".zip"):
            return "zip"
        return "unknown"

    def updater_supported_modes(self) -> set[str]:
        return set()

    def launch_payload_for_mode(self, mode: str, payload_path: str, log_func) -> None:
        raise RuntimeError(
            f"Unsupported update mode '{mode}' for this platform."
        )

    def bookmark_save_modifier(self) -> str:
        """Return the modifier key name for saving bookmarks (default: 'control' for non-macOS)."""
        return "control"

    def mouse_look_button_name(self) -> str:
        """Return the primary mouse button name for camera look (Windows/Linux use left-click)."""
        return "left"

    def font_candidates(self) -> list[str]:
        """Return fallback font file paths (mostly Linux fonts)."""
        return [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
