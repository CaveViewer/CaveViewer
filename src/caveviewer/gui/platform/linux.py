"""Linux GUI platform adapter implementation."""

from __future__ import annotations

import os
import platform
import subprocess

from .base import PreferencesDialogLayoutPolicy, SplashLayoutPolicy
from .desktop_services import DesktopServices, get_desktop_services
from .default import DefaultSplashPlatformAdapter


class LinuxSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    """Linux update metadata and manual package-reveal integration."""

    def __init__(self, *, desktop_services: DesktopServices | None = None) -> None:
        self._desktop_services = desktop_services or get_desktop_services()

    def ui_font_family(self) -> str:
        return "sans-serif"

    def default_update_manifest_url(self, repo: str, branch: str) -> str:
        update_arch = self._update_arch_slug()
        if update_arch is None:
            return ""
        return f"https://raw.githubusercontent.com/{repo}/{branch}/updates/linux/{update_arch}/stable.json"

    def _update_arch_slug(self) -> str | None:
        machine = platform.machine().strip().lower()
        if machine in {"x86_64", "amd64"}:
            return "x86_64"
        return None

    def install_channel(self) -> str:
        return "linux_app"

    def supports_install_channel(self, channel: str) -> bool:
        return channel == "linux_app" and self._update_arch_slug() == "x86_64"

    def unsupported_install_channel_message(self, channel: str) -> str:
        if channel == "linux_app" and self._update_arch_slug() is None:
            machine = platform.machine().strip() or "unknown"
            return (
                "Linux automatic updates are available only for x86_64 builds. "
                f"This machine reports architecture '{machine}', so automatic "
                "updates are disabled."
            )
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

    def persist_downloaded_payload(self, temp_payload_path: str, download_url: str | None) -> str:
        final_path = super().persist_downloaded_payload(temp_payload_path, download_url)
        if final_path.lower().endswith(".appimage"):
            current_mode = os.stat(final_path).st_mode
            os.chmod(final_path, current_mode | 0o111)
        return final_path

    def download_reveal_action_label(self) -> str:
        return "Open Download Folder"

    def reveal_downloaded_payload(self, payload_path: str) -> None:
        self._desktop_services.reveal_path(payload_path)

    def reveal_file(self, path: str) -> None:
        """Reveal a saved user file through portal-backed desktop services."""
        self._desktop_services.reveal_path(path)

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

    def splash_layout_policy(self) -> SplashLayoutPolicy:
        """Return Linux splash layout values."""
        return SplashLayoutPolicy(
            app_icon_resource_name="app_icon_macos.png",
            reuse_existing_root=False,
            destroy_root_on_close=True,
            windows_layout=False,
            linux_layout=True,
            window_width=940,
            min_height=680,
            extra_bottom_slack=0,
            secondary_link_row_bottom_gap=36,
            footer_credits_bottom_pad=36,
            title_to_action_gap=72,
            browse_button_bottom_gap=42,
            instruction_bottom_gap=30,
            secondary_link_row_top_gap=40,
        )

    def preferences_dialog_layout_policy(self) -> PreferencesDialogLayoutPolicy:
        """Return Linux Preferences layout values."""
        return PreferencesDialogLayoutPolicy(
            windows_layout=False,
            macos_layout=False,
            linux_layout=True,
            wrap_length=460,
            text_entry_width=36,
            body_pad_x=32,
            min_width=860,
            row_pad_x=18,
            row_pad_y=12,
            control_row_top_pad_y=14,
            tab_pad_x=14,
            tab_pad_y=7,
            tab_bottom_pad_y=18,
            button_row_top_pad_y=18,
            tab_highlight_thickness=1,
            notice_wrap_length=720,
            resizable_vertical=False,
        )

    def default_text_antialiasing_mode(self) -> str:
        """Return the Linux default FreeType anti-aliasing mode."""
        return "light"

    def supports_tk_display_scaling(self) -> bool:
        """Linux Tk dialogs use display DPI or AppImage-provided Tk scaling."""
        return True

    def viewer_uses_glfw_native_initial_size(self) -> bool:
        """Linux viewer sizing is resolved after GLFW backend selection."""
        return True
