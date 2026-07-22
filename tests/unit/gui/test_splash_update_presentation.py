"""Verify the splash labels and actions for every visible update state."""

from __future__ import annotations

import inspect
import sys

import pytest

from caveviewer.gui import (
    map_history,
    map_library_panel,
    map_library_workflow,
    splash_screen,
)
from caveviewer.gui.update_manager import UpdateSnapshot, UpdateState


@pytest.mark.parametrize(
    ("snapshot", "status", "action_text", "action", "status_action"),
    [
        (
            UpdateSnapshot(
                state=UpdateState.AVAILABLE,
                current_version="1.0.63",
                available_version="v1.0.64",
            ),
            "Update 1.0.64 available",
            "Download",
            splash_screen._UpdateAction.DOWNLOAD,
            None,
        ),
        (
            UpdateSnapshot(
                state=UpdateState.DOWNLOADING,
                current_version="1.0.63",
                available_version="1.0.64",
                downloaded_bytes=42,
                total_bytes=100,
            ),
            "Downloading… 42%",
            "",
            None,
            None,
        ),
        (
            UpdateSnapshot(
                state=UpdateState.VERIFYING,
                current_version="1.0.63",
                available_version="1.0.64",
            ),
            "Verifying…",
            "",
            None,
            None,
        ),
        (
            UpdateSnapshot(
                state=UpdateState.READY,
                current_version="1.0.63",
                available_version="1.0.64",
                payload_path="/downloads/CaveViewer.dmg",
            ),
            "Update ready",
            "Show in Finder",
            splash_screen._UpdateAction.REVEAL,
            None,
        ),
        (
            UpdateSnapshot(
                state=UpdateState.FAILED,
                current_version="1.0.63",
                available_version="1.0.64",
                error="offline",
            ),
            "Download failed",
            "Retry",
            splash_screen._UpdateAction.RETRY,
            None,
        ),
    ],
)
def test_update_state_has_expected_splash_presentation(
    snapshot,
    status,
    action_text,
    action,
    status_action,
):
    presentation = splash_screen._update_presentation(snapshot, "Show in Finder")

    assert presentation.status_text == status
    assert presentation.action_text == action_text
    assert presentation.action == action
    assert presentation.status_action == status_action


@pytest.mark.parametrize(
    "state",
    (
        UpdateState.IDLE,
        UpdateState.CHECKING,
        UpdateState.UP_TO_DATE,
        UpdateState.SHUTDOWN,
    ),
)
def test_non_actionable_update_states_remain_quiet(state):
    presentation = splash_screen._update_presentation(
        UpdateSnapshot(state=state, current_version="1.0.63"),
        "Show in Finder",
    )

    assert presentation == splash_screen._UpdatePresentation()


def test_last_browse_directory_uses_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    state_home = tmp_path / "state"
    map_root = tmp_path / "maps"
    map_root.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    splash_screen._save_last_browse_dir(str(map_root))

    state_file = state_home / "caveviewer" / "last_browse_path"
    assert state_file.read_text(encoding="utf-8") == str(map_root)
    assert splash_screen._load_last_browse_dir() == str(map_root)


def test_library_recent_maps_use_open_history_not_last_browse(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    state_home = tmp_path / "state"
    last_map = tmp_path / "last-map"
    recent_map = tmp_path / "recent-map"
    last_map.mkdir()
    recent_map.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    splash_screen._save_last_browse_dir(str(last_map))
    map_history.remember_recent_map_path(str(recent_map))

    assert splash_screen._load_library_recent_map_paths() == [str(recent_map)]


def test_splash_root_reuses_existing_macos_tk_root(monkeypatch):
    monkeypatch.setattr(splash_screen.sys, "platform", "darwin")
    destroyed_children = []

    class Child:
        def __init__(self, name):
            self.name = name

        def destroy(self):
            destroyed_children.append(self.name)

    class ExistingRoot:
        def winfo_exists(self):
            return True

        def winfo_children(self):
            return [Child("old-logo"), Child("old-button")]

    root = ExistingRoot()
    tk_module = type(
        "FakeTk",
        (),
        {
            "_default_root": root,
            "Tk": lambda **_options: (_ for _ in ()).throw(
                AssertionError("macOS splash must reuse the kept-alive root")
            ),
        },
    )

    assert splash_screen._create_splash_root(tk_module) is root
    assert destroyed_children == ["old-logo", "old-button"]


def test_splash_root_creates_new_tk_when_no_macos_root(monkeypatch):
    monkeypatch.setattr(splash_screen.sys, "platform", "darwin")
    created = []
    root = object()
    tk_module = type(
        "FakeTk",
        (),
        {
            "_default_root": None,
            "Tk": lambda **options: created.append(options) or root,
        },
    )

    assert splash_screen._create_splash_root(tk_module) is root
    assert created == [splash_screen.tk_root_options()]


def test_splash_root_does_not_reuse_default_root_off_macos(monkeypatch):
    monkeypatch.setattr(splash_screen.sys, "platform", "linux")
    existing_root = object()
    created = []
    root = object()
    tk_module = type(
        "FakeTk",
        (),
        {
            "_default_root": existing_root,
            "Tk": lambda **options: created.append(options) or root,
        },
    )

    assert splash_screen._create_splash_root(tk_module) is root
    assert created == [splash_screen.tk_root_options()]


def test_splash_font_selection_prefers_tk_visible_linux_family():
    selected = splash_screen._select_tk_font_family(
        {"noto sans": "Noto Sans"},
        "TkDefaultFont",
        ["Missing Family", *splash_screen._LINUX_TK_SANS_FAMILIES],
        linux_layout=True,
    )

    assert selected == "Noto Sans"


def test_splash_font_selection_avoids_nimbus_sans_linux_fallback():
    selected = splash_screen._select_tk_font_family(
        {},
        "Nimbus Sans L",
        ["Missing Family"],
        linux_layout=True,
    )

    assert selected == "sans-serif"


def test_splash_font_configuration_does_not_wait_on_fontconfig():
    source = inspect.getsource(splash_screen)

    assert "subprocess.run" not in source
    assert "_fontconfig_sans_family" not in source


def test_splash_label_actions_are_keyboard_accessible_without_fallthrough():
    source = inspect.getsource(splash_screen.show_splash_screen)

    assert "highlightthickness=1" in source
    assert "takefocus=True" in source
    assert 'label.bind("<Return>", invoke)' in source
    assert 'label.bind("<space>", invoke)' in source
    assert "def _invoke_and_break(callback):" in source
    assert "def _bind_activation(widget, callback) -> None:" in source
    assert "_bind_activation(browse_button, on_open_map_folder)" in source
    assert "_bind_activation(preferences_link, _on_preferences_click)" in source
    assert "MapLibraryWorkflow(" in source
    assert "start_sample_download_worker(" not in source
    assert "show_sample_maps_dialog(" not in source


def test_splash_map_library_panel_is_scrollable_and_generically_labeled():
    splash_source = inspect.getsource(splash_screen.show_splash_screen)
    style_source = inspect.getsource(splash_screen._map_library_panel_style)
    panel_source = inspect.getsource(map_library_panel.MapLibraryPanel)
    workflow_source = inspect.getsource(map_library_workflow.MapLibraryWorkflow)
    source = splash_source + style_source + panel_source + workflow_source

    assert 'text="Map Library"' not in source
    assert "Your Library" in source
    assert "Standard Library" in source
    assert "Open your maps or explore the standard library." not in source
    assert "No maps added yet." in source
    assert "Maps you open yourself will appear here." not in source
    assert "No user-opened maps yet." not in source
    assert 'top_pad=16' in source
    assert 'bottom_pad=18' in source
    assert "Recent Maps" not in source
    assert "Available Maps" not in source
    assert "Open recent or available maps." not in source
    assert "Available to download" not in source
    assert "Scrollbar(" not in source
    assert "yscrollcommand=self._set_scrollbar_fraction" in panel_source
    assert 'self._content_scrollbar.pack(side="right", fill="y")' in panel_source
    assert "self.bind_mousewheel_if_ready(self._rows_frame)" in panel_source
    assert "recent_map_paths = _load_library_recent_map_paths()" in splash_source
    assert "self.controller.row(" in workflow_source
    assert "detail=row.detail" in panel_source
    assert "highlightthickness=0" in source
    assert "panel_border_color=_LIBRARY_PANEL_BORDER_COLOR" in style_source
    assert 'divider.pack(side="left", fill="y", padx=(px(18), px(12)), pady=px(26))' in source
    assert 'panel.pack(fill="both", expand=True, pady=self._px(26))' in panel_source
    assert "metadata_font=_LIBRARY_METADATA_FONT" in style_source
    assert "metadata_color=_LIBRARY_METADATA_COLOR" in style_source
    assert "_action_button_pixel_size" in panel_source
    assert "style.action_button_width" in panel_source
    assert "style.action_button_pad_x" in panel_source
    assert "style.action_button_pad_y" in panel_source
    assert "progress_bar_canvas = tk.Canvas(" not in source
    assert "reserve_progress=True" not in panel_source
    assert "_create_action_button" in panel_source
    assert "_draw_action_stop_progress" in panel_source
    assert "button.create_arc(" in panel_source
    assert "button.create_rectangle(" in panel_source
    assert "stop_fill_color = style.button_fg" in panel_source
    assert "action_progress_ring_diameter=" in style_source
    assert "action_stop_size=" in style_source
    assert "show_stop_progress=True" in workflow_source
    assert '"Cancel"' not in workflow_source
    assert '"Stopping…"' in workflow_source
    assert "poll_download_queue" in workflow_source
    assert "cancel_active_download_for_close" in workflow_source
    assert "directory_selection_factory" in workflow_source
    assert "start_catalog_fetch" in workflow_source
    assert "poll_download_queue" not in splash_source

    style = splash_screen._map_library_panel_style()
    assert style.progress_track_color == splash_screen.DARK_THEME.primary_button_hover
    assert style.progress_fill_color == splash_screen.DARK_THEME.primary_button_border
    assert style.progress_track_color != style.button_bg
    assert style.progress_fill_color != style.button_bg
    assert style.progress_fill_color != style.button_fg


def test_map_library_rows_use_subtle_overflow_menu_for_management():
    splash_source = inspect.getsource(splash_screen.show_splash_screen)
    style_source = inspect.getsource(splash_screen._map_library_panel_style)
    panel_source = inspect.getsource(map_library_panel.MapLibraryPanel)
    workflow_source = inspect.getsource(map_library_workflow.MapLibraryWorkflow)
    source = splash_source + style_source + panel_source + workflow_source

    assert "Remove from this list" in source
    assert "Remove cache" in source
    assert "Remove downloaded files" in source
    assert "Remove from Recent" not in source
    assert "_create_overflow_button" in panel_source
    assert "_create_recent_overflow_button" not in source
    assert "menu_actions_factory=" in source
    assert "leading_widget_factory=" not in source
    assert "leading_widget=leading_widget" in source
    assert "self.remove_recent_path(path)" in workflow_source
    assert "self.has_cache(path)" in workflow_source
    assert "self.remove_cache(path)" in workflow_source
    assert "self.remove_downloaded(" in workflow_source
    assert "remove_standard_download" in workflow_source
    assert "remove_standard_download" not in splash_source
    assert "show_row_status" in source
    assert "Cache removed" in source
    assert "Couldn’t remove cache" in source
    assert "Couldn’t remove files" in source
    assert "_cv_base_text" in source
    assert "_cv_status_after_id" in source
    assert "Removed downloaded files for" not in source
    assert "Removed cache for" not in source
    assert "has_managed_map_cache(sample_path)" not in source
    assert "self._recent_container = tk.Frame(" in panel_source
    assert "self.recent_rows" in panel_source
    assert "self._recent_empty_note = self._create_empty_note" in panel_source
    assert "_LIBRARY_OVERFLOW_TEXT" in source
    assert "Open" in source




def test_library_action_buttons_use_normalized_dimensions():
    assert splash_screen._LIBRARY_ACTION_BUTTON_WIDTH == 8
    assert splash_screen._LIBRARY_ACTION_BUTTON_PAD_X == 10
    assert splash_screen._LIBRARY_ACTION_BUTTON_PAD_Y == 5
    assert splash_screen._LIBRARY_METADATA_FONT[1] == 9
