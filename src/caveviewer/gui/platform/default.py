"""Default GUI platform behavior for unsupported or generic desktops."""

from __future__ import annotations

import os
import shutil

from .base import (
    DialogLayoutPolicy,
    PreferencesDialogLayoutPolicy,
    SplashLayoutPolicy,
    SplashPlatformAdapter,
)


class DefaultSplashPlatformAdapter(SplashPlatformAdapter):
    """Conservative defaults for non-macOS platforms."""

    def ui_font_family(self) -> str:
        return "Segoe UI"

    def install_about_handler(self, root, program_name: str, version: str) -> None:
        # No platform-specific About menu integration outside macOS.
        return None

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

    def download_reveal_action_label(self) -> str:
        return "Open Download Folder"

    def reveal_downloaded_payload(self, payload_path: str) -> None:
        raise RuntimeError(
            f"Revealing downloaded packages is unsupported on this platform: "
            f"{payload_path}"
        )

    def reveal_file(self, path: str) -> None:
        raise RuntimeError(
            f"Revealing files is unsupported on this platform: {path}"
        )

    def bookmark_save_modifier(self) -> str:
        """Return the modifier key name for saving bookmarks (default: 'control' for non-macOS)."""
        return "control"

    def primary_shortcut_modifier_label(self) -> str:
        """Return the primary shortcut label shown in controls/help UI."""
        return "Ctrl"

    def mouse_look_button_name(self) -> str:
        """Return the primary mouse button name for camera look (Windows/Linux use left-click)."""
        return "left"

    def compact_manual_controls_layout(self) -> bool:
        """Use the denser manual controls layout on non-macOS platforms."""
        return True

    def font_candidates(self) -> list[str]:
        """Return fallback font file paths (mostly Linux fonts)."""
        return [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]

    def splash_layout_policy(self) -> SplashLayoutPolicy:
        """Return generic splash layout values for non-specialized platforms."""
        return SplashLayoutPolicy(
            app_icon_resource_name="app_icon_macos.png",
            reuse_existing_root=False,
            destroy_root_on_close=True,
            windows_layout=False,
            linux_layout=False,
            window_width=940,
            min_height=680,
            extra_bottom_slack=0,
            secondary_link_row_bottom_gap=36,
            footer_credits_bottom_pad=36,
            title_to_action_gap=28,
            browse_button_bottom_gap=16,
            instruction_bottom_gap=0,
            secondary_link_row_top_gap=16,
        )

    def preferences_dialog_layout_policy(self) -> PreferencesDialogLayoutPolicy:
        """Return generic Preferences layout values."""
        return PreferencesDialogLayoutPolicy(
            windows_layout=False,
            macos_layout=False,
            linux_layout=False,
            wrap_length=460,
            text_entry_width=36,
            body_pad_x=24,
            min_width=760,
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

    def dialog_layout_policy(self) -> DialogLayoutPolicy:
        """Return generic shared dialog layout values."""
        return DialogLayoutPolicy(body_pad_x=24, use_label_action_buttons=False)

    def tk_primary_modifier_name(self) -> str:
        """Return the Tk event modifier for primary shortcuts."""
        return "Control"

    def default_text_antialiasing_mode(self) -> str:
        """Return the default FreeType anti-aliasing mode."""
        return "normal"

    def viewer_overlay_text_scale(self, base_scale: float) -> float:
        """Return the default OpenGL overlay text scale."""
        return float(base_scale)

    def tk_text_scale(self, default_font_points: float) -> float:
        """Return the runtime Tk text scale for fixed-size splash tokens."""
        try:
            return max(1.0, float(default_font_points) / 12.0)
        except (TypeError, ValueError):
            return 1.0

    def supports_tk_display_scaling(self) -> bool:
        """Return whether Tk scaling should be adjusted on this platform."""
        return False

    def configure_process_dpi_awareness(self) -> None:
        """Configure process DPI awareness when the platform requires it."""
        return None

    def load_system_certificates(self, context) -> None:
        """Load any platform certificate stores needed by urllib SSL contexts."""
        return None

    def recording_subprocess_startup_kwargs(self) -> dict:
        """Return subprocess kwargs for GUI-launched recording encoders."""
        return {}

    def suppress_forced_startup_focus(
        self, *, is_frozen: bool, force_requested: bool
    ) -> bool:
        """Return whether startup focus forcing should be suppressed."""
        return False

    def command_modifier_uses_control_fallback(self) -> bool:
        """Return whether Command checks should also inspect Control flags."""
        return False

    def shift_digit_bookmark_save_fallback(self) -> bool:
        """Return whether Shift+digit can save camera bookmarks."""
        return False

    def option_left_mouse_look_enabled(self) -> bool:
        """Return whether Option+left-click/motion enables mouse look."""
        return False

    def focus_viewer_window(self, window) -> None:
        """Best-effort foreground activation for generic window backends."""
        for target in (window, getattr(window, "_window", None)):
            if target is None:
                continue
            try:
                if hasattr(target, "switch_to"):
                    target.switch_to()
            except Exception:
                pass
            try:
                if hasattr(target, "activate"):
                    target.activate()
            except Exception:
                pass

    def viewer_uses_glfw_native_initial_size(self) -> bool:
        """Return whether viewer sizing is deferred to GLFW backend selection."""
        return False
