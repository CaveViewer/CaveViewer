"""Verify the splash labels and actions for every visible update state."""

from __future__ import annotations

import inspect
import sys
from types import SimpleNamespace

import pytest

from caveviewer.gui import (
    map_history,
    map_library_controller,
    map_library_panel,
    map_library_workflow,
    splash_screen,
)
from caveviewer.gui.platform.default import DefaultSplashPlatformAdapter
from caveviewer.gui.platform.macos import MacOSSplashPlatformAdapter
from caveviewer.gui.platform.presentation import select_presentation_profile
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
        "_NAVIGATION_BRAND_FONT",
        "_VERSION_FONT",
        "_BODY_FONT",
        "_SMALL_FONT",
        "_LIBRARY_SECTION_FONT",
        "_LIBRARY_METADATA_FONT",
        "_UPDATE_ACTION_FONT",
    )
    original_values = {name: getattr(splash_screen, name) for name in font_globals}

    class FakeDefaultFont:
        def actual(self, key):
            return {"family": "Helvetica Neue", "size": 15}[key]

    import tkinter.font as tkfont

    monkeypatch.setattr(tkfont, "families", lambda _root: ["Helvetica Neue"])
    monkeypatch.setattr(tkfont, "nametofont", lambda _name: FakeDefaultFont())
    monkeypatch.setattr(splash_screen, "_LINUX_SPLASH_LAYOUT", False)

    try:
        splash_screen._configure_runtime_tk_fonts(
            object(),
            presentation_profile=select_presentation_profile(platform_name="darwin"),
        )

        assert splash_screen._TK_TEXT_SCALE == pytest.approx(1.4)
        assert splash_screen._NAVIGATION_BRAND_FONT == (
            "Helvetica Neue",
            20,
            "bold",
        )
        assert splash_screen._BODY_FONT == ("Helvetica Neue", 17)
        assert splash_screen._SMALL_FONT == ("Helvetica Neue", 14)
        assert splash_screen._LIBRARY_METADATA_FONT == ("Helvetica Neue", 13)
        assert splash_screen._UPDATE_ACTION_FONT == ("Helvetica Neue", 15, "bold")
    finally:
        for name, value in original_values.items():
            setattr(splash_screen, name, value)


def test_splash_navigation_actions_are_keyboard_accessible_without_fallthrough():
    source = inspect.getsource(splash_screen.show_splash_screen)

    assert "navigation_frame = tk.Frame(" in source
    assert "def _create_navigation_item(" in source
    assert "takefocus=True" in source
    assert 'label.bind("<Return>", invoke)' in source
    assert 'label.bind("<space>", invoke)' in source
    assert "def _invoke_and_break(callback):" in source
    assert "def _bind_activation(widget, callback) -> None:" in source
    assert "map_library_navigation_item = _create_navigation_item(" in source
    assert '"Map Library",' in source
    assert '_create_navigation_item("Open Map", on_open_map_folder)' not in source
    assert "preferences_navigation_item = _create_navigation_item(" in source
    assert "about_navigation_item = _create_navigation_item(" in source
    assert "open_map_folder=on_open_map_folder" in source
    assert "def _focus_map_library() -> None:" in source
    assert "panel.focus_content()" in source
    assert "map_library_surface = tk.Frame(right_frame, bg=_BG_COLOR)" in source
    assert "preferences_surface = tk.Frame(right_frame, bg=_BG_COLOR)" in source
    assert "about_surface = tk.Frame(right_frame, bg=_BG_COLOR)" in source
    assert "def _show_preferences_surface() -> None:" in source
    assert "preferences_surface_required_height" in source
    assert "_ensure_preferences_panel()" in source
    assert "map_library_surface.pack_forget()" in source
    assert "preferences_surface.pack_forget()" in source
    assert "def _show_about_surface() -> None:" in source
    assert "PreferencesPanel(" in source
    assert "_build_themed_about_content(" in source
    assert "show_close=False" in source
    assert "def _request_leave_preferences" in source
    assert "_show_discard_preferences_dialog(" in source
    assert 'root.bind("<Return>", _handle_root_return)' in source
    assert 'root.bind("<Escape>", _cancel_preferences_or_close)' in source
    assert "_show_themed_about_dialog(" not in source
    assert "browse_button_frame" not in source
    assert 'text="Open map…"' not in source
    assert "open_recorded_dive_link" not in source
    assert 'text="Open recorded dive…"' not in source
    assert "instruction_label" not in source
    assert '"Maps use .glb, or .obj with matching .mtl and textures."' not in source
    assert "def on_open(event=None):" not in source
    assert "preferences_link" not in source
    assert "MapLibraryWorkflow(" in source
    assert "load_initial_standard_library_catalog" in source
    assert "KNOWN_STANDARD_LIBRARY_MAPS" not in source
    assert "start_sample_download_worker(" not in source
    assert "show_sample_maps_dialog(" not in source


def test_splash_navigation_uses_a_compact_horizontal_brand_masthead():
    source = inspect.getsource(splash_screen.show_splash_screen)

    assert "_NAVIGATION_BRAND_FONT = _tk_font(14, \"bold\")" in inspect.getsource(
        splash_screen
    )
    assert "brand_frame = tk.Frame(left_frame, bg=_BG_COLOR)" in source
    assert 'brand_frame.pack(fill="x", padx=px(14), pady=(px(18), px(10)))' in source
    assert "max_logo_dim = px(56)" in source
    assert 'logo_label.pack(side="left", padx=(0, px(10)))' in source
    assert "brand_text = tk.Frame(brand_frame, bg=_BG_COLOR)" in source
    assert 'brand_text.pack(side="left", fill="x", expand=True)' in source
    assert "font=_NAVIGATION_BRAND_FONT" in source
    assert 'title_label.pack(anchor="w")' in source
    assert 'version_label.pack(anchor="w", pady=(px(1), 0))' in source
    assert 'navigation_frame.pack(fill="x", pady=(px(8), 0))' in source


def test_themed_about_content_reuses_the_splash_identity_in_both_hosts():
    content_source = inspect.getsource(splash_screen._build_themed_about_content)
    dialog_source = inspect.getsource(splash_screen._show_themed_about_dialog)

    assert "tk.Toplevel(root)" in dialog_source
    assert "_build_themed_about_content(" in dialog_source
    assert "dialog.grab_set()" in dialog_source
    assert "_LOGO_PATH" in content_source
    assert "_CREDITS_TEXT.strip()" in content_source
    assert "bg=_BG_COLOR" in content_source
    assert "fg=_TITLE_COLOR" in content_source
    assert "text=\"Close\"" in content_source
    assert "show_close: bool = True" in content_source
    assert "center_vertically" in content_source
    assert "credits_panel" not in content_source
    assert "highlightbackground=_BORDER_COLOR" not in content_source


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


def test_splash_map_library_uses_navigation_and_an_overflow_cue():
    splash_source = inspect.getsource(splash_screen.show_splash_screen)
    style_source = inspect.getsource(splash_screen._map_library_panel_style)
    controller_source = inspect.getsource(map_library_controller.MapLibraryController)
    panel_source = inspect.getsource(map_library_panel.MapLibraryPanel)
    workflow_source = inspect.getsource(map_library_workflow.MapLibraryWorkflow)
    source = (
        splash_source
        + style_source
        + controller_source
        + panel_source
        + workflow_source
    )
    section_source = panel_source[
        panel_source.find("def _create_section") : panel_source.find(
            "def _create_empty_note"
        )
    ]

    assert '"Map Library"' in splash_source
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
    assert "Open a local map" in panel_source
    assert "Browse a cave map folder" in panel_source
    assert "def _create_open_map_action" in panel_source
    assert "def _draw_open_map_action" in panel_source
    assert "self._open_map_folder = open_map_folder" in panel_source
    assert "open_map_shell = tk.Frame(panel, bg=style.panel_color)" in panel_source
    assert "self._create_open_map_action(open_map_shell)" in panel_source
    assert "scroll_row = 1" in panel_source
    assert '"Recent Maps"' not in source
    assert "Available Maps" not in source
    assert "Open recent or available maps." not in source
    assert "Available to download" not in source
    assert "font=self._style.section_font" in panel_source
    assert "font=self._style.small_font" not in section_source
    assert "Scrollbar(" not in source
    assert "Scroll to browse more maps ↓" in panel_source
    assert "self._content_scrollbar" not in panel_source
    assert "self._content_overflows" in panel_source
    assert "self.sync_scroll_region()" in panel_source
    assert "self._content_canvas.yview_scroll" in panel_source
    assert "self.bind_mousewheel_if_ready(self._rows_frame)" in panel_source
    assert "self.sync_after_row_change()" in panel_source
    assert "recent_map_paths = _load_library_recent_map_paths()" in splash_source
    assert "self.controller.row(" in workflow_source
    assert "detail=row.detail" in panel_source
    assert "highlightthickness=0" in source
    assert "panel_border_color=_LIBRARY_PANEL_BORDER_COLOR" in style_source
    assert 'left_frame = tk.Frame(content_frame, bg=_BG_COLOR, width=px(220))' in source
    assert 'divider.pack(side="left", fill="y", padx=(px(14), px(18)), pady=px(10))' in source
    assert 'panel.pack(fill="both", expand=True, pady=self._px(14))' in panel_source
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
    assert "Local-only former library maps" not in source
    assert "No longer a part of the standard library" in source
    assert "create_polygon(" in section_source
    assert 'text="Hide"' not in section_source
    assert 'text="Show"' not in section_source

    style = splash_screen._map_library_panel_style()
    assert style.progress_track_color == splash_screen.DARK_THEME.entry_background
    assert style.progress_fill_color == splash_screen.DARK_THEME.primary_button
    assert style.progress_track_color != style.button_bg
    assert style.progress_fill_color != style.button_bg
    assert style.progress_fill_color == style.button_fg
    assert style.former_map_title_color == splash_screen.DARK_THEME.secondary_text
    assert style.former_map_title_color != style.title_color


def test_map_library_sections_start_expanded_and_toggle_in_place():
    class _FakeSectionContent:
        def __init__(self) -> None:
            self.pack_calls = []
            self.pack_forget_calls = 0

        def pack(self, **options) -> None:
            self.pack_calls.append(options)

        def pack_forget(self) -> None:
            self.pack_forget_calls += 1

    header = object()
    content = _FakeSectionContent()
    section = map_library_panel.MapLibrarySectionWidgets(
        header=header,
        content=content,
        title="CaveViewer Maps",
    )
    panel = object.__new__(map_library_panel.MapLibraryPanel)
    drawn_states = []
    closed_menus = []
    sync_calls = []
    panel._widget_exists = lambda _widget: True
    panel._draw_section_header = lambda target: drawn_states.append(target.expanded)
    panel.close_active_menu = lambda: closed_menus.append(True)
    panel.sync_after_row_change = lambda: sync_calls.append(True)

    assert section.expanded is True

    panel._toggle_section(section)

    assert section.expanded is False
    assert content.pack_forget_calls == 1
    assert content.pack_calls == []

    panel._toggle_section(section)

    assert section.expanded is True
    assert content.pack_calls == [{"fill": "x", "after": header}]
    assert drawn_states == [False, True]
    assert len(closed_menus) == 2
    assert len(sync_calls) == 2


def test_map_library_section_headers_use_adjacent_disclosure_triangles():
    class _FakeHeader:
        def __init__(self) -> None:
            self.text_calls = []
            self.polygon_calls = []

        def delete(self, _tag) -> None:
            pass

        def winfo_width(self) -> int:
            return 320

        def winfo_height(self) -> int:
            return 24

        def create_text(self, *coordinates, **options):
            self.text_calls.append((coordinates, options))
            return f"text-{len(self.text_calls)}"

        def bbox(self, _item):
            return (2, 0, 118, 24)

        def create_polygon(self, *coordinates, **options) -> None:
            self.polygon_calls.append((coordinates, options))

    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel._widget_exists = lambda _widget: True
    panel._px = lambda value: int(value)
    panel._style = type(
        "Style",
        (),
        {
            "section_font": ("TkDefaultFont", 10, "bold"),
            "metadata_font": ("TkDefaultFont", 9),
            "instruction_color": "#ffffff",
        },
    )()
    header = _FakeHeader()
    section = map_library_panel.MapLibrarySectionWidgets(
        header=header,
        content=object(),
        title="CaveViewer Maps",
    )

    panel._draw_section_header(section)
    section.expanded = False
    panel._draw_section_header(section)

    assert [options["text"] for _coordinates, options in header.text_calls] == [
        "CaveViewer Maps",
        "CaveViewer Maps",
    ]
    assert len(header.polygon_calls) == 2
    expanded_points, expanded_options = header.polygon_calls[0]
    collapsed_points, collapsed_options = header.polygon_calls[1]
    assert expanded_points[1] == expanded_points[3] < expanded_points[5]
    assert collapsed_points[0] == collapsed_points[2] < collapsed_points[4]
    assert expanded_options == collapsed_options == {
        "fill": "#ffffff",
        "outline": "",
        "tags": "cv_section_header",
    }


def test_former_standard_row_title_uses_a_muted_style_without_moving_the_row():
    class _FakeLabel:
        def __init__(self) -> None:
            self.config_calls = []

        def config(self, **options) -> None:
            self.config_calls.append(options)

    title_label = _FakeLabel()
    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel.standard_rows = {
        "former-map": SimpleNamespace(title_label=title_label),
    }
    panel._standard_row_former = {}
    panel._widget_exists = lambda _widget: True
    panel._style = SimpleNamespace(
        title_color="#ffffff",
        former_map_title_color="#9a9aa6",
    )

    panel.set_standard_row_former("former-map", True)
    panel.set_standard_row_former("former-map", False)

    assert panel._standard_row_former == {"former-map": False}
    assert title_label.config_calls == [
        {"fg": "#9a9aa6"},
        {"fg": "#ffffff"},
    ]


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
    assert "self._recent_container = self._recent_section.content" in panel_source
    assert "self._standard_container = self._standard_section.content" in panel_source
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
    assert "_install_menu_dismissal_bindings" in panel_source
    assert '"<ButtonPress-1>"' in panel_source
    assert '"<FocusOut>"' in panel_source
    assert ".bind_all(" not in panel_source


def test_map_library_menu_outside_click_binding_is_scoped_and_removed_on_close():
    class _FakeRoot:
        def __init__(self) -> None:
            self.callbacks = {}
            self.unbound = []

        def bind(self, sequence, callback, add=None):
            assert add == "+"
            callback_id = f"callback-{len(self.callbacks) + 1}"
            self.callbacks[sequence] = (callback_id, callback)
            return callback_id

        def unbind(self, sequence, callback_id):
            self.unbound.append((sequence, callback_id))

    panel = object.__new__(map_library_panel.MapLibraryPanel)
    root = _FakeRoot()
    menu = object()
    opener = object()
    panel.root = root
    panel._active_menu = menu
    panel._active_menu_root_bindings = []
    panel.recent_rows = {}
    panel.standard_rows = {}
    panel._widget_exists = lambda _widget: False

    panel._install_menu_dismissal_bindings(menu, opener)
    _callback_id, outside_click = root.callbacks["<ButtonPress-1>"]

    outside_click(SimpleNamespace(widget=object()))

    assert panel._active_menu is None
    assert root.unbound == [
        ("<ButtonPress-1>", "callback-1"),
        ("<FocusOut>", "callback-2"),
    ]


def test_library_action_buttons_use_normalized_dimensions():
    assert splash_screen._LIBRARY_ACTION_BUTTON_SIZE == 32
    assert splash_screen._LIBRARY_ACTION_ICON_STROKE_WIDTH == 2
    assert splash_screen._LIBRARY_OVERFLOW_BUTTON_SIZE == 28
    assert splash_screen._LIBRARY_METADATA_FONT[1] == 9
    style = splash_screen._map_library_panel_style()
    assert not hasattr(style, "scrollbar_right_inset")
    assert style.panel_border_color == splash_screen._LIBRARY_PANEL_BORDER_COLOR


def test_map_library_scroll_hint_appears_only_when_rows_overflow():
    class _FakeHint:
        def __init__(self) -> None:
            self.manager = ""
            self.calls = []

        def winfo_manager(self) -> str:
            return self.manager

        def grid(self) -> None:
            self.manager = "grid"
            self.calls.append("grid")

        def grid_remove(self) -> None:
            self.manager = ""
            self.calls.append("grid_remove")

    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel._scroll_hint = _FakeHint()
    panel._content_overflows = False
    panel._widget_exists = lambda widget: widget is not None

    panel._set_scroll_hint_visible(True)

    assert panel._content_overflows is True
    assert panel._scroll_hint.calls == ["grid"]

    panel._set_scroll_hint_visible(True)
    assert panel._scroll_hint.calls == ["grid"]

    panel._set_scroll_hint_visible(False)
    assert panel._content_overflows is False
    assert panel._scroll_hint.calls == ["grid", "grid_remove"]


def test_map_library_scroll_region_shows_guidance_only_for_overflow():
    class _FakeCanvas:
        def __init__(self) -> None:
            self.height = 200
            self.configurations = []
            self.moveto_calls = []

        def winfo_width(self) -> int:
            return 320

        def winfo_height(self) -> int:
            return self.height

        def configure(self, **options) -> None:
            self.configurations.append(options)

        def yview_moveto(self, fraction: float) -> None:
            self.moveto_calls.append(fraction)

    class _FakeRows:
        def __init__(self) -> None:
            self.height = 320

        def winfo_reqheight(self) -> int:
            return self.height

    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel._content_canvas = _FakeCanvas()
    panel._rows_frame = _FakeRows()
    overflow_states = []
    panel._set_scroll_hint_visible = overflow_states.append

    panel.sync_scroll_region()

    assert panel._content_canvas.configurations == [
        {"scrollregion": (0, 0, 320, 320)}
    ]
    assert overflow_states == [True]
    assert panel._content_canvas.moveto_calls == []

    panel._rows_frame.height = 200
    panel.sync_scroll_region()

    assert overflow_states == [True, False]
    assert panel._content_canvas.moveto_calls == [0]


def test_map_library_scrolls_only_when_rows_overflow():
    class _FakeCanvas:
        def __init__(self) -> None:
            self.scroll_calls = []

        def yview_scroll(self, amount, units) -> None:
            self.scroll_calls.append((amount, units))

    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel._content_canvas = _FakeCanvas()
    panel._content_overflows = False

    assert panel._scroll_content(SimpleNamespace(delta=-120)) is None
    assert panel._content_canvas.scroll_calls == []

    panel._content_overflows = True
    assert panel._scroll_content(SimpleNamespace(delta=-120)) == "break"
    assert panel._content_canvas.scroll_calls == [(1, "units")]


def test_map_library_open_map_action_uses_the_existing_folder_callback(monkeypatch):
    class _FakeCanvas:
        def __init__(self, _parent, **options) -> None:
            self.options = options
            self.bindings = {}
            self.pack_calls = []
            self.config_calls = []
            self.draw_calls = []

        def bind(self, sequence, callback, add=None) -> None:
            self.bindings[sequence] = callback

        def pack(self, **options) -> None:
            self.pack_calls.append(options)

        def config(self, **options) -> None:
            self.config_calls.append(options)

        def winfo_width(self) -> int:
            return 420

        def winfo_height(self) -> int:
            return 58

        def delete(self, tag) -> None:
            self.draw_calls.append(("delete", tag))

        def create_rectangle(self, *coordinates, **options) -> None:
            self.draw_calls.append(("rectangle", coordinates, options))

        def create_line(self, *coordinates, **options) -> None:
            self.draw_calls.append(("line", coordinates, options))

        def create_text(self, *coordinates, **options) -> None:
            self.draw_calls.append(("text", coordinates, options))

    canvases = []
    monkeypatch.setattr(
        map_library_panel.tk,
        "Canvas",
        lambda *args, **kwargs: (
            canvases.append(_FakeCanvas(*args, **kwargs)) or canvases[-1]
        ),
    )
    opened = []
    closed_menus = []
    activations = []
    wheel_targets = []
    panel = object.__new__(map_library_panel.MapLibraryPanel)
    panel._open_map_folder = lambda: opened.append(True)
    panel.close_active_menu = lambda: closed_menus.append(True)
    panel._bind_activation = lambda _widget, callback: activations.append(callback)
    panel.bind_mousewheel_if_ready = wheel_targets.append
    panel._widget_exists = lambda _widget: True
    panel._px = lambda value: int(value)
    panel._style = SimpleNamespace(
        small_font=("TkDefaultFont", 10),
        section_font=("TkDefaultFont", 10, "bold"),
        metadata_font=("TkDefaultFont", 9),
        title_color="#f5d77d",
        metadata_color="#6f717f",
        panel_color="#101018",
        panel_border_color="#1e2028",
        button_border_color="#a77a10",
        button_hover_bg="#2a2a33",
        menu_hover_bg="#343442",
        progress_fill_color="#f0ad22",
    )

    panel._create_open_map_action(object())

    action = canvases[0]
    assert action.options["takefocus"] is True
    assert action.options["height"] == 58
    assert action.pack_calls == [{"anchor": "w", "fill": "x"}]
    assert wheel_targets == [action]

    action.bindings["<Configure>"](None)
    assert [entry[2]["text"] for entry in action.draw_calls if entry[0] == "text"] == [
        "Open a local map",
        "Browse a cave map folder",
    ]

    activations[0]()

    assert closed_menus == [True]
    assert opened == [True]


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
