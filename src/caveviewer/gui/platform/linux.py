"""Linux GUI platform adapter implementation."""

from __future__ import annotations

import subprocess

from .base import PreferencesDialogLayoutPolicy, SplashLayoutPolicy
from .desktop_services import DesktopServices, get_desktop_services
from .default import DefaultSplashPlatformAdapter


class LinuxSplashPlatformAdapter(DefaultSplashPlatformAdapter):
    """Linux manual package-reveal integration."""

    def __init__(self, *, desktop_services: DesktopServices | None = None) -> None:
        self._desktop_services = desktop_services or get_desktop_services()

    def ui_font_family(self) -> str:
        return "sans-serif"

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
