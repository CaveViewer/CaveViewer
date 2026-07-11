from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class ManualInstallResult:
    mounted_payload_path: str | None
    mounted_app_path: str | None


class SplashPlatformAdapter(Protocol):
    """Platform-specific hooks used by caveviewer.gui.splash_screen."""

    def ui_font_family(self) -> str:
        ...

    def install_about_handler(self, root: Any, program_name: str, version: str) -> None:
        ...

    def install_channel(self) -> str:
        ...

    def persist_downloaded_payload(self, temp_payload_path: str, download_url: str | None) -> str:
        ...

    def prepare_manual_install(self, payload_path: str) -> ManualInstallResult:
        ...

    def default_update_repo(self) -> str:
        ...

    def default_update_manifest_url(self, repo: str, branch: str) -> str:
        ...

    def update_check_user_agent(self) -> str:
        ...

    def supports_install_channel(self, channel: str) -> bool:
        ...

    def unsupported_install_channel_message(self, channel: str) -> str:
        ...

    def channel_download_url_keys(self, channel: str) -> tuple[str, ...]:
        ...

    def channel_download_size_keys(self, channel: str) -> tuple[str, ...]:
        ...

    def bookmark_save_modifier(self) -> str:
        """Return the modifier key name for saving bookmarks (e.g., 'command' for macOS, 'control' for Windows/Linux)."""
        ...

    def mouse_look_button_name(self) -> str:
        """Return the primary mouse button name for camera look ('left' or 'right'). macOS uses 'right' and Option+left; Windows/Linux use 'left'."""
        ...

    def channel_sha256_keys(self, channel: str) -> tuple[str, ...]:
        ...

    def missing_download_url_message(self, channel: str) -> str:
        ...

    def detect_package_kind(self, download_url: str, channel: str) -> str:
        ...

    def updater_supported_modes(self) -> set[str]:
        ...

    def launch_payload_for_mode(self, mode: str, payload_path: str, log_func) -> None:
        ...

    def font_candidates(self) -> list[str]:
        """Return platform-specific font file paths to try for UI rendering.
        
        Returns a list of font paths in priority order. Fonts are checked for existence
        and the first one found is used. Environment variable CAVEVIEWER_UI_FONT is
        checked first and takes precedence over these platform-specific candidates.
        """
        ...
