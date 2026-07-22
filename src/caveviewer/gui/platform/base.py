"""Protocol and value objects for GUI platform adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SplashLayoutPolicy:
    """Platform-specific splash-window layout decisions."""

    app_icon_resource_name: str
    reuse_existing_root: bool
    destroy_root_on_close: bool
    windows_layout: bool
    linux_layout: bool
    min_height: int
    extra_bottom_slack: int
    secondary_link_row_bottom_gap: int
    footer_credits_bottom_pad: int
    title_to_action_gap: int
    browse_button_bottom_gap: int
    instruction_bottom_gap: int
    secondary_link_row_top_gap: int


@dataclass(frozen=True)
class PreferencesDialogLayoutPolicy:
    """Platform-specific Preferences dialog layout decisions."""

    windows_layout: bool
    macos_layout: bool
    linux_layout: bool
    wrap_length: int
    text_entry_width: int
    body_pad_x: int
    min_width: int
    row_pad_x: int
    row_pad_y: int
    control_row_top_pad_y: int
    tab_pad_x: int
    tab_pad_y: int
    tab_bottom_pad_y: int
    button_row_top_pad_y: int
    tab_highlight_thickness: int
    notice_wrap_length: int
    resizable_vertical: bool


@dataclass(frozen=True)
class DialogLayoutPolicy:
    """Platform-specific shared Tk dialog presentation decisions."""

    body_pad_x: int
    use_label_action_buttons: bool


class SplashPlatformAdapter(Protocol):
    """Platform-specific hooks used by the splash and update manager."""

    def ui_font_family(self) -> str:
        ...

    def install_about_handler(self, root: Any, program_name: str, version: str) -> None:
        ...

    def install_channel(self) -> str:
        ...

    def persist_downloaded_payload(self, temp_payload_path: str, download_url: str | None) -> str:
        ...

    def download_reveal_action_label(self) -> str:
        ...

    def reveal_downloaded_payload(self, payload_path: str) -> None:
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

    def primary_shortcut_modifier_label(self) -> str:
        """Return the display label for primary app shortcuts, such as 'Cmd' or 'Ctrl'."""
        ...

    def mouse_look_button_name(self) -> str:
        """Return the primary mouse button name for camera look ('left' or 'right'). macOS uses 'right' and Option+left; Windows/Linux use 'left'."""
        ...

    def compact_manual_controls_layout(self) -> bool:
        """Return whether the manual controls overlay should use the compact layout."""
        ...

    def channel_sha256_keys(self, channel: str) -> tuple[str, ...]:
        ...

    def missing_download_url_message(self, channel: str) -> str:
        ...

    def detect_package_kind(self, download_url: str, channel: str) -> str:
        ...

    def font_candidates(self) -> list[str]:
        """Return platform-specific font file paths to try for UI rendering.
        
        Returns a list of font paths in priority order. Fonts are checked for existence
        and the first one found is used. Environment variable CAVEVIEWER_UI_FONT is
        checked first and takes precedence over these platform-specific candidates.
        """
        ...

    def splash_layout_policy(self) -> SplashLayoutPolicy:
        ...

    def preferences_dialog_layout_policy(self) -> PreferencesDialogLayoutPolicy:
        ...

    def dialog_layout_policy(self) -> DialogLayoutPolicy:
        ...

    def tk_primary_modifier_name(self) -> str:
        ...

    def default_text_antialiasing_mode(self) -> str:
        ...

    def supports_tk_display_scaling(self) -> bool:
        ...

    def configure_process_dpi_awareness(self) -> None:
        ...

    def load_system_certificates(self, context: Any) -> None:
        ...

    def recording_subprocess_startup_kwargs(self) -> dict[str, Any]:
        ...

    def suppress_forced_startup_focus(self, *, is_frozen: bool, force_requested: bool) -> bool:
        ...

    def command_modifier_uses_control_fallback(self) -> bool:
        ...

    def shift_digit_bookmark_save_fallback(self) -> bool:
        ...

    def option_left_mouse_look_enabled(self) -> bool:
        ...

    def focus_viewer_window(self, window: Any) -> None:
        ...

    def viewer_uses_glfw_native_initial_size(self) -> bool:
        ...
