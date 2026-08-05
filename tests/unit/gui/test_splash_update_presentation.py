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
from caveviewer.gui.platform.default import DefaultSplashPlatformAdapter
from caveviewer.gui.platform.macos import MacOSSplashPlatformAdapter
from caveviewer.gui.features import FeatureDecision, FeatureId, FeatureState
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


def test_disabled_update_gate_shows_its_safe_explanation_without_an_action():
    presentation = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.IDLE,
            current_version="1.0.63",
            automatic_update=FeatureDecision(
                feature=FeatureId.AUTOMATIC_UPDATE,
                state=FeatureState.DISABLED,
                reason_code="automatic_update_target_unsupported",
                explanation="Automatic updates are unavailable for this installation.",
            ),
        ),
        "Show in Finder",
    )

    assert presentation == splash_screen._UpdatePresentation(
        status_text="Automatic updates are unavailable for this installation."
    )


def test_disabled_update_package_reveal_gate_hides_ready_action():
    presentation = splash_screen._update_presentation(
        UpdateSnapshot(
            state=UpdateState.READY,
            current_version="1.0.63",
            available_version="1.0.64",
            payload_path="/downloads/CaveViewer.bin",
            update_package_reveal=FeatureDecision(
                feature=FeatureId.UPDATE_PACKAGE_REVEAL,
                state=FeatureState.DISABLED,
                reason_code="update_package_reveal_route_unsupported",
                explanation="The verified update package cannot be revealed automatically.",
            ),
        ),
        "Show in Finder",
    )

    assert presentation == splash_screen._UpdatePresentation(
        status_text="The verified update package cannot be revealed automatically."
    )


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
    monkeypatch.setattr(
        splash_screen,
        "_SPLASH_LAYOUT_POLICY",
        MacOSSplashPlatformAdapter().splash_layout_policy(),
    )
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
    monkeypatch.setattr(
        splash_screen,
        "_SPLASH_LAYOUT_POLICY",
        MacOSSplashPlatformAdapter().splash_layout_policy(),
    )
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
    monkeypatch.setattr(
        splash_screen,
        "_SPLASH_LAYOUT_POLICY",
        DefaultSplashPlatformAdapter().splash_layout_policy(),
    )
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


def test_splash_fonts_scale_from_runtime_tk_default(monkeypatch):
    font_globals = (
        "_UI_FONT_FAMILY",
        "_TK_TEXT_SCALE",
        "_TITLE_FONT",
        "_VERSION_FONT",
        "_BODY_FONT",
        "_SMALL_FONT",
        "_LIBRARY_SECTION_FONT",
        "_LIBRARY_METADATA_FONT",
        "_INSTRUCTION_FONT",
        "_FOOTER_FONT",
        "_LINK_FONT",
        "_BUTTON_FONT",
    )
    original_values = {name: getattr(splash_screen, name) for name in font_globals}

    class FakeDefaultFont:
        def actual(self, key):
            return {"family": "Helvetica Neue", "size": 15}[key]

    import tkinter.font as tkfont

    monkeypatch.setattr(tkfont, "families", lambda _root: ["Helvetica Neue"])
    monkeypatch.setattr(tkfont, "nametofont", lambda _name: FakeDefaultFont())
    monkeypatch.setattr(splash_screen, "_PLATFORM_ADAPTER", MacOSSplashPlatformAdapter())
    monkeypatch.setattr(splash_screen, "_LINUX_SPLASH_LAYOUT", False)
    monkeypatch.setattr(splash_screen, "_ROOMY_SPLASH_LAYOUT", False)

    try:
        splash_screen._configure_runtime_tk_fonts(object())

        assert splash_screen._TK_TEXT_SCALE == pytest.approx(1.4)
        assert splash_screen._BODY_FONT == ("Helvetica Neue", 17)
        assert splash_screen._SMALL_FONT == ("Helvetica Neue", 14)
        assert splash_screen._LIBRARY_METADATA_FONT == ("Helvetica Neue", 13)
        assert splash_screen._BUTTON_FONT == ("Helvetica Neue", 18)
    finally:
        for name, value in original_values.items():
            setattr(splash_screen, name, value)


def test_splash_label_actions_are_keyboard_accessible_without_fallthrough():
    source = inspect.getsource(splash_screen.show_splash_screen)

    assert "highlightthickness=1" in source
    assert "takefocus=True" in source
    assert 'label.bind("<Return>", invoke)' in source
    assert 'label.bind("<space>", invoke)' in source
    assert "def _invoke_and_break(callback):" in source
    assert "def _bind_activation(widget, callback) -> None:" in source
    assert "_bind_activation(browse_button, on_open_map_folder)" in source
    assert 'text="Open map…"' in source
    assert "open_recorded_dive_link" not in source
    assert 'text="Open recorded dive…"' not in source
    assert '"Maps use .obj files with matching .mtl and textures."' in source
    assert '"Maps use .glb, or .obj with matching .mtl and textures."' not in source
    assert "def on_open(event=None):" not in source
    assert "_bind_activation(preferences_link, _on_preferences_click)" in source
    assert "MapLibraryWorkflow(" in source
    assert "load_initial_standard_library_catalog" in source
    assert "KNOWN_STANDARD_LIBRARY_MAPS" not in source
    assert "start_sample_download_worker(" not in source
    assert "show_sample_maps_dialog(" not in source


def test_cache_rebuild_starts_from_splash_without_a_confirmation_window():
    splash_source = inspect.getsource(splash_screen.show_splash_screen)

    assert "CacheRebuildJobController()" in splash_source
    assert "confirm_cache_rebuild=" not in splash_source
    assert "_confirm_cache_rebuild_dialog" not in splash_source
    assert "request_cache_rebuild_pause" in splash_source
    assert "def _splash_is_foreground()" in splash_source
    assert "splash_is_foreground=_splash_is_foreground" in splash_source


def test_splash_map_picker_checks_its_directory_selection_route_before_calling_it():
    source = inspect.getsource(splash_screen.show_splash_screen)

    assert "directory_selection_preflight(" in source
    assert "choose_authorized_directory(" in source
    assert source.index("directory_selection_preflight(") < source.index(
        "choose_authorized_directory("
    )


def test_splash_map_library_panel_is_scrollable_and_generically_labeled():
    splash_source = inspect.getsource(splash_screen.show_splash_screen)
    style_source = inspect.getsource(splash_screen._map_library_panel_style)
    panel_source = inspect.getsource(map_library_panel.MapLibraryPanel)
    workflow_source = inspect.getsource(map_library_workflow.MapLibraryWorkflow)
    source = splash_source + style_source + panel_source + workflow_source
    section_source = panel_source[
        panel_source.find("def _create_section") : panel_source.find(
            "def _create_empty_note"
        )
    ]

    assert 'text="Map Library"' not in source
    assert "Your Recent Maps" in source
    assert "CaveViewer Maps" in source
    assert "Your Library" not in source
    assert "Standard Library" not in source
    assert "Open your maps or explore the standard library." not in source
    assert "No maps added yet." in source
    assert "Maps you open yourself will appear here." not in source
    assert "No user-opened maps yet." not in source
    assert 'top_pad=16' in source
    assert 'bottom_pad=18' in source
    assert '"Recent Maps"' not in source
    assert "Available Maps" not in source
    assert "Open recent or available maps." not in source
    assert "Available to download" not in source
    assert "font=self._style.section_font" in panel_source
    assert "font=self._style.small_font" not in section_source
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
    assert "section_font=_LIBRARY_SECTION_FONT" in style_source
    assert "metadata_color=_LIBRARY_METADATA_COLOR" in style_source
    assert "_action_button_pixel_size" in panel_source
    assert "style.action_button_size" in panel_source
    assert "style.action_icon_stroke_width" in panel_source
    assert "style.overflow_button_size" in panel_source
    assert "progress_bar_canvas = tk.Canvas(" not in source
    assert "reserve_progress=True" not in panel_source
    assert "_create_action_button" in panel_source
    assert "_draw_action_stop_progress" in panel_source
    assert "button.create_arc(" in panel_source
    assert "button.create_rectangle(" in panel_source
    assert "_draw_download" in panel_source
    download_source = inspect.getsource(map_library_panel.MapLibraryPanel._draw_download)
    assert "create_rectangle" not in download_source
    assert "_draw_retry" in panel_source
    assert "_set_row_open_activation" in panel_source
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
    assert 'self.set_standard_row_metadata(key, "Downloading…")' in panel_source
    assert "Downloading… %" not in panel_source

    style = splash_screen._map_library_panel_style()
    assert style.progress_track_color == splash_screen.DARK_THEME.entry_background
    assert style.progress_fill_color == splash_screen.DARK_THEME.primary_button
    assert style.progress_track_color != style.button_bg
    assert style.progress_fill_color != style.button_bg
    assert style.progress_fill_color == style.button_fg


def test_map_library_rows_use_subtle_overflow_menu_for_management():
    splash_source = inspect.getsource(splash_screen.show_splash_screen)
    style_source = inspect.getsource(splash_screen._map_library_panel_style)
    panel_source = inspect.getsource(map_library_panel.MapLibraryPanel)
    workflow_source = inspect.getsource(map_library_workflow.MapLibraryWorkflow)
    source = splash_source + style_source + panel_source + workflow_source

    assert "Remove from this list" in source
    assert "Remove cache" in source
    assert "Remove map files" in source
    assert "Remove from Recent" not in source
    assert "_create_overflow_button" in panel_source
    assert "_create_recent_overflow_button" not in source
    assert "menu_actions_factory=" in source
    assert "leading_widget_factory=" not in source
    assert "overflow_button=overflow_button" in source
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
    assert "Removed downloaded maps for" not in source
    assert "Removed cache for" not in source
    assert "has_managed_map_cache(sample_path)" not in source
    assert "self._recent_container = tk.Frame(" in panel_source
    assert "self.recent_rows" in panel_source
    assert "self._recent_empty_note = self._create_empty_note" in panel_source
    assert "Open dive plan…" in source
    assert "guided_dive_preflight" in workflow_source
    assert "file_selection_preflight" in workflow_source
    assert "choose_authorized_file" in workflow_source
    assert "desktop_services.choose_file(" not in workflow_source
    assert "platform_runtime=platform_runtime" in splash_source
    assert "Open" in source
    assert "button.create_oval(" in panel_source
    assert 'button.pack(side="right", padx=(0, self._px(12))' in panel_source
    assert "padx=(0, self._px(8))" in panel_source


def test_library_action_buttons_use_normalized_dimensions():
    assert splash_screen._LIBRARY_ACTION_BUTTON_SIZE == 32
    assert splash_screen._LIBRARY_ACTION_ICON_STROKE_WIDTH == 2
    assert splash_screen._LIBRARY_OVERFLOW_BUTTON_SIZE == 28
    assert splash_screen._LIBRARY_METADATA_FONT[1] == 9


@pytest.mark.parametrize(
    ("action_text", "show_stop_progress", "icon", "tooltip", "row_activates"),
    [
        ("Open", False, "chevron-right", "Open map", True),
        ("Get", False, "download", "Download map", False),
        ("Retry", False, "retry", "Retry download", False),
        ("", True, "stop-progress", "Stop download", False),
    ],
)
def test_map_library_row_actions_use_state_aware_icons(
    action_text,
    show_stop_progress,
    icon,
    tooltip,
    row_activates,
):
    visual = map_library_panel.map_library_action_visual(
        action_text,
        show_stop_progress=show_stop_progress,
    )

    assert visual.icon == icon
    assert visual.tooltip == tooltip
    assert visual.row_activates is row_activates
