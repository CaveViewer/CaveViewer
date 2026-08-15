"""Exercise deterministic presentation policy for the splash Keys table."""

from __future__ import annotations

import inspect

from caveviewer.gui import help_panel
from caveviewer.gui.controls_catalog import keyboard_control_sections
from caveviewer.gui.platform.presentation import select_presentation_profile


def test_help_panel_uses_the_shared_canvas_scrollbar():
    source = inspect.getsource(help_panel.HelpPanel)

    assert "CanvasVerticalScrollbar(" in source
    assert "CanvasScrollbarStyle(" in source
    assert "tk.Scrollbar(" not in source
    assert "canvas=canvas" in source
    assert 'canvas.bind("<Configure>", self._on_canvas_configure' in source


def test_help_panel_uses_a_compact_keys_table_without_redundant_labels():
    source = inspect.getsource(help_panel.HelpPanel)

    assert 'TopTab("keys", "Keys")' in source
    assert 'TopTab("capture", "Capture")' in source
    assert "TopTabbedContentSurface(" in source
    assert "def _draw_keycap_sequence" in source
    assert "shortcut_keycap_parts(shortcut)" in source
    assert 'canvas.delete("help-content")' in source
    assert "Keyboard shortcuts" not in source
    assert "Controls are shown for this platform" not in source
    assert "help_section_column_count" not in source


def test_capture_help_has_its_own_tab_with_artifact_specific_guidance():
    source = inspect.getsource(help_panel.HelpPanel)
    row_source = inspect.getsource(help_panel.HelpPanel._draw_shortcut_row)

    assert 'capture_section = next(' in source
    assert 'self._tab_sections["capture"]' in source
    assert "def _show_tab" in source
    assert 'primary_font_role = "overview" if detail else "action"' in row_source
    assert 'font=self._canvas_font("detail")' in row_source
    assert "fill=style.detail_color" in row_source


def test_keys_help_omits_capture_map_import_and_recorded_dive_sections():
    sections = keyboard_control_sections(
        select_presentation_profile(platform_name="windows")
    )

    assert [section.id for section in help_panel.key_help_sections(sections)] == [
        "movement",
        "view",
        "bookmarks",
    ]


def test_capture_help_preserves_platform_shortcuts_and_explains_artifacts():
    source_sections = keyboard_control_sections(
        select_presentation_profile(platform_name="darwin")
    )
    capture_source = next(
        section for section in source_sections if section.id == "capture"
    )
    rows = {
        shortcut.id: shortcut
        for section in help_panel.capture_help_sections(capture_source)
        for shortcut in section.shortcuts
    }

    assert rows["recording-toggle"].shortcut == "Cmd + R"
    assert rows["recording-toggle"].action == "Start/stop video recording"
    assert rows["recording-toggle"].context_note == (
        "Saves what you see while diving as an MP4 video. It is not a "
        "replay route or map."
    )
    assert rows["manual-trace-toggle"].shortcut == "Cmd + T"
    assert rows["manual-trace-toggle"].action == "Start/stop manual trace"
    assert rows["manual-trace-toggle"].context_note == (
        "Saves your camera path and timing for replay or analysis. It "
        "does not capture video or map geometry."
    )
    assert rows["slice-toggle"].shortcut == "Cmd + C"
    assert rows["slice-toggle"].action == "Start/stop cave slice"
    assert rows["slice-toggle"].context_note == (
        "Saves the selected cave section as an independent CaveViewer "
        "map. It is precompiled and cannot be rebuilt because the source "
        "model is not included."
    )
    assert "slice-cancel" not in rows


def test_help_panel_uses_a_quiet_table_without_card_borders():
    create_source = inspect.getsource(help_panel.HelpPanel.create)
    row_source = inspect.getsource(help_panel.HelpPanel._draw_shortcut_row)

    assert 'text="Help"' not in create_source
    assert "TopTabbedContentSurface(" in create_source
    assert "highlightbackground" not in create_source
    assert "row_divider_color" in row_source
    assert "canvas.create_line(" in row_source


def test_help_panel_is_embedded_and_uses_the_shared_scroll_host():
    source = inspect.getsource(help_panel.HelpPanel)

    assert "tk.Canvas(" in source
    assert "tk.Toplevel" not in source
    assert "scrollbar.sync_overflow(self._content_height)" in source
    assert "def focus_content" in source
