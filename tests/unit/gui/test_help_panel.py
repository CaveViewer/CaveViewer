"""Exercise deterministic presentation policy for the splash Keys table."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

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


def test_help_keycaps_use_geometric_unit_spans_and_center_glyphs():
    panel = help_panel.HelpPanel.__new__(help_panel.HelpPanel)
    widths = {
        "W": 11,
        "Mouse": 33,
        "+": 7,
        "/": 5,
    }
    panel._font_width = lambda _role, text: widths[text]
    panel._px = lambda value: value

    assert panel._keycap_width("-") == 25
    assert panel._keycap_width("=") == 25
    assert panel._keycap_span_width(1) == 25
    assert panel._keycap_span_width(2) == 55
    assert panel._keycap_width("Cmd") == 55
    assert panel._keycap_width("Ctrl") == 55
    assert panel._keycap_width("Del") == 55
    assert panel._keycap_width("Scroll") == 55
    assert panel._keycap_width("Shift") == 55
    assert panel._keycap_width("Escape") == 55
    assert panel._keycap_width("Space") == 55
    assert panel._keycap_width("Space") == panel._keycap_width("Escape")
    assert panel._keycap_width("1–9") == 55
    assert panel._keycap_width("1–9") == panel._keycap_width("Ctrl")
    assert panel._keycap_width("Shift") == (
        panel._keycap_width("E") + 5 + panel._keycap_width("Q")
    )
    assert panel._keycap_width("Mouse") == 47
    assert panel._keycap_separator_width("+") == 25
    assert panel._keycap_separator_width("/") == 25

    rectangle_calls = []
    text_calls = []
    panel._style = SimpleNamespace(
        keycap_background_color="#111111",
        keycap_border_color="#222222",
        keycap_text_color="#eeeeee",
        section_color="#cccccc",
    )
    panel._keycap_height = lambda _shortcut: 20
    panel._canvas_font = lambda _role: "font"
    canvas = SimpleNamespace(
        create_rectangle=lambda *args, **kwargs: rectangle_calls.append(
            (args, kwargs)
        ),
        create_text=lambda *args, **kwargs: text_calls.append((args, kwargs)),
    )

    panel._draw_keycap_sequence(canvas, x=10, y=20, shortcut="- =")
    panel._draw_keycap_sequence(canvas, x=10, y=50, shortcut="Escape")
    panel._draw_keycap_sequence(canvas, x=10, y=80, shortcut="Space")

    first_keycap = text_calls[0]
    assert first_keycap[0] == (22.5, 30.0)
    assert first_keycap[1]["anchor"] == "center"
    assert rectangle_calls[1][0][0] - rectangle_calls[0][0][2] == 5
    escape_rect = rectangle_calls[2][0]
    space_rect = rectangle_calls[3][0]
    assert escape_rect[2] - escape_rect[0] == space_rect[2] - space_rect[0]
    assert text_calls[2][0] == (
        (escape_rect[0] + escape_rect[2]) / 2,
        (escape_rect[1] + escape_rect[3]) / 2,
    )
    assert text_calls[3][0] == (
        (space_rect[0] + space_rect[2]) / 2,
        (space_rect[1] + space_rect[3]) / 2,
    )
    assert text_calls[2][1]["anchor"] == text_calls[3][1]["anchor"] == "center"


def test_help_compound_shortcut_centers_separator_between_keycaps():
    panel = help_panel.HelpPanel.__new__(help_panel.HelpPanel)
    widths = {
        "W": 11,
        "Cmd": 21,
        "Ctrl": 24,
        "Del": 18,
        "Scroll": 30,
        "Shift": 28,
        "Escape": 38,
        "Space": 25,
        "1–9": 26,
        "+": 7,
    }
    panel._font_width = lambda _role, text: widths[text]
    panel._px = lambda value: value
    panel._style = SimpleNamespace(
        keycap_background_color="#111111",
        keycap_border_color="#222222",
        keycap_text_color="#eeeeee",
        section_color="#cccccc",
    )
    panel._keycap_height = lambda _shortcut: 20
    panel._canvas_font = lambda _role: "font"
    rectangle_calls = []
    text_calls = []
    canvas = SimpleNamespace(
        create_rectangle=lambda *args, **kwargs: rectangle_calls.append(
            (args, kwargs)
        ),
        create_text=lambda *args, **kwargs: text_calls.append((args, kwargs)),
    )

    panel._draw_keycap_sequence(canvas, x=10, y=20, shortcut="Ctrl + 0")
    first_right = rectangle_calls[0][0][2]
    final_left = rectangle_calls[1][0][0]
    separator = next(call for call in text_calls if call[1]["text"] == "+")
    separator_x = separator[0][0]
    assert first_right == 65
    assert final_left == 100
    assert separator_x == 82.5
    assert separator[1]["anchor"] == "center"
    assert separator_x - first_right == final_left - separator_x


def test_help_compound_rows_share_the_same_separator_lane():
    panel = help_panel.HelpPanel.__new__(help_panel.HelpPanel)
    widths = {
        "W": 11,
        "Cmd": 21,
        "Ctrl": 24,
        "Del": 18,
        "Scroll": 30,
        "Shift": 28,
        "Escape": 38,
        "Space": 25,
        "1–9": 26,
        "+": 7,
    }
    panel._font_width = lambda _role, text: widths[text]
    panel._px = lambda value: value
    panel._style = SimpleNamespace(
        keycap_background_color="#111111",
        keycap_border_color="#222222",
        keycap_text_color="#eeeeee",
        section_color="#cccccc",
    )
    panel._keycap_height = lambda _shortcut: 20
    panel._canvas_font = lambda _role: "font"
    rectangle_calls = []
    text_calls = []
    canvas = SimpleNamespace(
        create_rectangle=lambda *args, **kwargs: rectangle_calls.append(
            (args, kwargs)
        ),
        create_text=lambda *args, **kwargs: text_calls.append((args, kwargs)),
    )

    panel._draw_keycap_sequence(canvas, x=10, y=20, shortcut="Ctrl + 0")
    panel._draw_keycap_sequence(canvas, x=10, y=50, shortcut="Del + 1–9")

    separator_xs = [
        args[0]
        for args, kwargs in text_calls
        if kwargs["text"] == "+"
    ]
    assert separator_xs == [82.5, 82.5]
    del_keycap = rectangle_calls[2][0]
    range_keycap = rectangle_calls[3][0]
    assert del_keycap[2] - del_keycap[0] == range_keycap[2] - range_keycap[0]


def test_capture_help_has_its_own_tab_with_artifact_specific_guidance():
    source = inspect.getsource(help_panel.HelpPanel)
    row_source = inspect.getsource(help_panel.HelpPanel._draw_shortcut_row)

    assert 'capture_section = next(' in source
    assert 'self._tab_sections["capture"]' in source
    assert "def _show_tab" in source
    assert 'primary_font_role = "overview" if detail else "action"' in row_source
    assert 'font=self._canvas_font("detail")' in row_source
    assert "fill=style.detail_color" in row_source


def test_keys_help_groups_shortcuts_as_move_look_and_navigate():
    sections = keyboard_control_sections(
        select_presentation_profile(platform_name="windows")
    )

    key_sections = help_panel.key_help_sections(sections)

    assert [(section.id, section.title) for section in key_sections] == [
        ("move", "Move"),
        ("look", "Look"),
        ("navigate", "Navigate"),
    ]
    assert [shortcut.id for shortcut in key_sections[0].shortcuts] == [
        "move-strafe",
        "move-vertical",
        "move-speed-boost",
        "move-speed-adjust",
    ]
    assert key_sections[0].shortcuts[1].shortcut == "E Q"
    assert key_sections[0].shortcuts[-1].shortcut == "- ="
    assert key_sections[0].shortcuts[-1].action == "Decrease/increase speed"
    assert [shortcut.id for shortcut in key_sections[1].shortcuts] == [
        "look-arrows",
        "look-jlik",
        "look-roll",
        "view-reset",
    ]
    assert [shortcut.id for shortcut in key_sections[2].shortcuts] == [
        "bookmark-save",
        "bookmark-recall",
        "bookmark-delete",
        "map-open",
        "recorded-dive-space",
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
    assert rows["capture-cancel"].shortcut == "Escape"
    assert rows["capture-cancel"].action == "Cancel active capture"
    assert rows["capture-cancel"].context_note == (
        "Only one capture can run at a time. Other capture shortcuts are "
        "ignored; Escape discards the active capture and removes partial files, "
        "then confirms that nothing was saved before the viewer closes."
    )


def test_help_panel_uses_a_quiet_table_without_card_borders():
    create_source = inspect.getsource(help_panel.HelpPanel.create)
    row_source = inspect.getsource(help_panel.HelpPanel._draw_shortcut_row)

    assert 'text="Help"' not in create_source
    assert "TopTabbedContentSurface(" in create_source
    assert "highlightbackground" not in create_source
    assert "baseline_color" not in create_source
    assert "canvas.create_line(" not in row_source


def test_help_uses_the_shared_primary_surface_origin():
    create_source = inspect.getsource(help_panel.HelpPanel.create)

    assert "content_pad_left_x=0" in create_source
    assert "content_pad_right_x=style.content_pad_x" in create_source
    assert "pady=self._px(PRIMARY_SURFACE_VERTICAL_MARGIN)" in create_source


def test_help_panel_uses_standard_section_spacing_without_heading_rules():
    table_source = inspect.getsource(help_panel.HelpPanel._render_table)
    heading_source = inspect.getsource(help_panel.HelpPanel._draw_section_heading)

    assert "STANDARD_CONTENT_SECTION_SPACING.between_sections_y" in table_source
    assert "top_pad_y=0 if shortcut_index == 0 else None" in table_source
    assert "STANDARD_CONTENT_SECTION_SPACING.heading_to_content_y" in heading_source
    assert "self._px(TABBED_CONTENT_ALIGNMENT_INSET)," in heading_source
    assert "x=self._px(TABBED_CONTENT_ALIGNMENT_INSET)," in inspect.getsource(
        help_panel.HelpPanel._draw_shortcut_row
    )
    assert "canvas.create_line(" not in heading_source


def test_help_panel_is_embedded_and_uses_the_shared_scroll_host():
    source = inspect.getsource(help_panel.HelpPanel)

    assert "tk.Canvas(" in source
    assert "tk.Toplevel" not in source
    assert "scrollbar.sync_overflow(self._content_height)" in source
    assert "def focus_content" in source


def test_help_panel_exposes_troubleshooting_tab_and_log_action():
    init_source = inspect.getsource(help_panel.HelpPanel.__init__)
    create_source = inspect.getsource(help_panel.HelpPanel.create)
    render_source = inspect.getsource(help_panel.HelpPanel._render_troubleshooting)

    assert 'TopTab("troubleshooting", "Troubleshooting")' in init_source
    assert '"Show latest log"' in create_source
    assert "command=self._show_latest_log" in render_source
    assert "enabled=state.can_reveal" in render_source
    assert "command=self._copy_last_error" in render_source
    assert "APPLICATION LOGS" in render_source
    assert "LAST ERROR" in render_source
    assert "state.error_excerpt" in render_source


def test_help_panel_only_renders_log_action_status_when_present():
    render_source = inspect.getsource(help_panel.HelpPanel._render_troubleshooting)

    assert "if state.status_text:" in render_source


def test_troubleshooting_aligns_and_places_copy_below_available_error_details():
    render_source = inspect.getsource(help_panel.HelpPanel._render_troubleshooting)

    assert "content_x = self._px(TABBED_CONTENT_ALIGNMENT_INSET)" in render_source
    assert "content_right = width - self._px(12)" in render_source
    assert "if copy_button is not None:" in render_source
    assert "# Copy is an inline utility for the excerpt" in render_source
    assert "canvas.create_window(\n                    content_x,\n                    copy_y," in render_source
    assert "enabled=state.error_excerpt is not None" not in render_source


def test_help_copy_confirmation_reuses_the_shared_mark_and_standard_gap():
    render_source = inspect.getsource(help_panel.HelpPanel._render_troubleshooting)
    copy_source = inspect.getsource(help_panel.HelpPanel._copy_last_error)

    assert "create_confirmation_mark" in inspect.getsource(help_panel)
    assert "ACTION_CONFIRMATION_GAP" in render_source
    assert "self._copy_confirmation_visible" in render_source
    assert 'self._copy_feedback = "" if copied else' in copy_source
    assert "confirmation.show()" in copy_source
    assert 'accessible_name="Error details copied"' in inspect.getsource(
        help_panel.HelpPanel.create
    )
    assert 'text="Copied"' not in copy_source


def test_copy_error_excerpt_uses_exact_displayed_text():
    class Clipboard:
        def __init__(self):
            self.value = "old"

        def clipboard_clear(self):
            self.value = ""

        def clipboard_append(self, text):
            self.value += text

    clipboard = Clipboard()
    excerpt = "context café\nERROR: boom\n  traceback"

    assert help_panel.copy_error_excerpt_to_clipboard(clipboard, excerpt) is True
    assert clipboard.value == excerpt


def test_copy_error_excerpt_reports_clipboard_failure():
    class BrokenClipboard:
        def clipboard_clear(self):
            raise RuntimeError("clipboard unavailable")

    assert (
        help_panel.copy_error_excerpt_to_clipboard(BrokenClipboard(), "ERROR")
        is False
    )


def test_help_copy_confirmation_clears_when_panel_is_left():
    cleared = []
    panel = help_panel.HelpPanel.__new__(help_panel.HelpPanel)
    panel._copy_confirmation = SimpleNamespace(clear=lambda: cleared.append(True))
    panel._copy_feedback = "Couldn’t copy. Select the text manually."
    panel._copy_feedback_is_error = False

    panel.on_hidden()

    assert cleared == [True]
    assert panel._copy_feedback == ""
    assert panel._copy_feedback_is_error is False
