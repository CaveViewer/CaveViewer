"""Windows GUI platform adapter implementation."""

from __future__ import annotations

import ctypes
import os
import ssl
import subprocess

from .base import DialogLayoutPolicy, PreferencesDialogLayoutPolicy, SplashLayoutPolicy
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

    def reveal_file(self, path: str) -> None:
        """Reveal a saved user file in Explorer without opening the file."""
        normalized_path = os.path.normpath(os.path.abspath(path))
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

    def splash_layout_policy(self) -> SplashLayoutPolicy:
        """Return Windows splash layout values."""
        return SplashLayoutPolicy(
            app_icon_resource_name="app_icon_windows.png",
            reuse_existing_root=False,
            destroy_root_on_close=True,
            windows_layout=True,
            linux_layout=False,
            window_width=940,
            min_height=680,
            extra_bottom_slack=0,
            secondary_link_row_bottom_gap=36,
            footer_credits_bottom_pad=36,
            title_to_action_gap=58,
            browse_button_bottom_gap=32,
            instruction_bottom_gap=20,
            secondary_link_row_top_gap=30,
        )

    def preferences_dialog_layout_policy(self) -> PreferencesDialogLayoutPolicy:
        """Return Windows Preferences layout values."""
        return PreferencesDialogLayoutPolicy(
            windows_layout=True,
            macos_layout=False,
            linux_layout=False,
            wrap_length=520,
            text_entry_width=42,
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
            resizable_vertical=True,
        )

    def dialog_layout_policy(self) -> DialogLayoutPolicy:
        """Return Windows shared dialog layout values."""
        return DialogLayoutPolicy(body_pad_x=32, use_label_action_buttons=False)

    def supports_tk_display_scaling(self) -> bool:
        """Windows Tk dialogs need process/display DPI scaling configuration."""
        return True

    def configure_process_dpi_awareness(self) -> None:
        """Best-effort Windows process DPI awareness setup."""
        try:
            # Windows 10+: per-monitor DPI awareness v2.
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except Exception:
            pass

        try:
            # Windows 8.1+: per-monitor DPI awareness.
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass

        try:
            # Vista fallback: system DPI awareness.
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    def load_system_certificates(self, context) -> None:
        """Trust Windows certificate stores in addition to Python's bundle."""
        for store_name in ("CA", "ROOT"):
            try:
                for cert, enc, _trust in ssl.enum_certificates(store_name):
                    if enc == "x509_asn":
                        try:
                            context.load_verify_locations(cadata=cert)
                        except ssl.SSLError:
                            pass
            except (AttributeError, OSError):
                pass

    def recording_subprocess_startup_kwargs(self) -> dict:
        """Hide console windows for GUI-launched ffmpeg recording."""
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        return {
            "startupinfo": startupinfo,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
