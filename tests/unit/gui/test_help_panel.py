"""Exercise deterministic presentation policy for the splash Keys table."""

from __future__ import annotations

import inspect
import tkinter as tk
from types import SimpleNamespace

import pytest

from caveviewer.gui import help_panel
from caveviewer.gui.controls_catalog import keyboard_control_sections
from caveviewer.gui.help_style import HELP_VISUAL_METRICS
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
    assert "active_font=style.tab_font" in source
    assert "inactive_font=style.tab_inactive_font" in source
    assert "active_indicator_color=style.tab_indicator_color" in source
    assert "inactive_color=style.detail_color" in source
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
    panel._metrics = HELP_VISUAL_METRICS.scaled(round)

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

    polygon_calls = []
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
        create_polygon=lambda *args, **kwargs: polygon_calls.append((args, kwargs)),
        create_text=lambda *args, **kwargs: text_calls.append((args, kwargs)),
    )

    panel._draw_keycap_sequence(canvas, x=10, y=20, shortcut="- =")
    panel._draw_keycap_sequence(canvas, x=10, y=50, shortcut="Escape")
    panel._draw_keycap_sequence(canvas, x=10, y=80, shortcut="Space")

    first_keycap = text_calls[0]
    assert first_keycap[0] == (22.5, 30.0)
    assert first_keycap[1]["anchor"] == "center"

    def bounds(call):
        points = call[0]
        return (
            min(points[0::2]),
            min(points[1::2]),
            max(points[0::2]),
            max(points[1::2]),
        )

    first_rect = bounds(polygon_calls[0])
    second_rect = bounds(polygon_calls[1])
    assert second_rect[0] - first_rect[2] == 5
    escape_rect = bounds(polygon_calls[2])
    space_rect = bounds(polygon_calls[3])
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
    panel._metrics = HELP_VISUAL_METRICS.scaled(round)
    panel._style = SimpleNamespace(
        keycap_background_color="#111111",
        keycap_border_color="#222222",
        keycap_text_color="#eeeeee",
        section_color="#cccccc",
    )
    panel._keycap_height = lambda _shortcut: 20
    panel._canvas_font = lambda _role: "font"
    polygon_calls = []
    text_calls = []
    canvas = SimpleNamespace(
        create_polygon=lambda *args, **kwargs: polygon_calls.append((args, kwargs)),
        create_text=lambda *args, **kwargs: text_calls.append((args, kwargs)),
    )

    panel._draw_keycap_sequence(canvas, x=10, y=20, shortcut="Ctrl + 0")
    first_points = polygon_calls[0][0]
    final_points = polygon_calls[1][0]
    first_right = max(first_points[0::2])
    final_left = min(final_points[0::2])
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
    panel._metrics = HELP_VISUAL_METRICS.scaled(round)
    panel._style = SimpleNamespace(
        keycap_background_color="#111111",
        keycap_border_color="#222222",
        keycap_text_color="#eeeeee",
        section_color="#cccccc",
    )
    panel._keycap_height = lambda _shortcut: 20
    panel._canvas_font = lambda _role: "font"
    polygon_calls = []
    text_calls = []
    canvas = SimpleNamespace(
        create_polygon=lambda *args, **kwargs: polygon_calls.append((args, kwargs)),
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
    del_points = polygon_calls[2][0]
    range_points = polygon_calls[3][0]
    assert max(del_points[0::2]) - min(del_points[0::2]) == (
        max(range_points[0::2]) - min(range_points[0::2])
    )


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
        "Creates an MP4 file. It is not a replay route or map."
    )
    assert rows["manual-trace-toggle"].shortcut == "Cmd + T"
    assert rows["manual-trace-toggle"].action == "Start/stop manual trace"
    assert rows["manual-trace-toggle"].context_note == (
        "Keeps path and timing data. It does not capture video or map geometry."
    )
    assert rows["slice-toggle"].shortcut == "Cmd + C"
    assert rows["slice-toggle"].action == "Start/stop cave slice"
    assert rows["slice-toggle"].context_note == (
        "Creates an independent pre-compiled map without the original source "
        "model."
    )
    assert rows["capture-cancel"].shortcut == "Escape"
    assert rows["capture-cancel"].action == "Cancel active capture"
    assert rows["capture-cancel"].context_note == (
        "Discards the active capture and removes partial files."
    )


def test_help_panel_uses_rounded_cards_without_heading_rules():
    create_source = inspect.getsource(help_panel.HelpPanel.create)
    card_source = inspect.getsource(help_panel.HelpPanel._finish_card)

    assert 'text="Help"' not in create_source
    assert "TopTabbedContentSurface(" in create_source
    assert "highlightbackground" not in create_source
    assert "draw_rounded_canvas_box(" in card_source
    assert 'tags=("help-content", "help-card")' in card_source
    assert "section_background_color" in card_source
    assert "canvas.create_line(" not in card_source


def test_help_uses_the_shared_primary_surface_origin():
    create_source = inspect.getsource(help_panel.HelpPanel.create)

    assert "content_pad_left_x=0" in create_source
    assert "content_pad_right_x=style.content_pad_x" in create_source
    assert "self._px(PRIMARY_LABEL_ROW_TOP_INSET)" in create_source
    assert "self._px(PRIMARY_SURFACE_VERTICAL_MARGIN)" in create_source
    assert "row_height=PRIMARY_LABEL_ROW_HEIGHT" in create_source


@pytest.mark.gui
def test_help_and_preferences_first_tabs_share_the_same_shell_origin(monkeypatch):
    from caveviewer.gui import preferences as settings
    from caveviewer.gui import preferences_dialog, splash_screen

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        root.geometry("1000x740+0+0")
        host = tk.Frame(root, bg="#0a0a0d")
        host.pack(fill="both", expand=True)
        preferences_host = tk.Frame(host, bg="#0a0a0d")
        help_host = tk.Frame(host, bg="#0a0a0d")
        for surface in (preferences_host, help_host):
            surface.place(x=0, y=0, relwidth=1, relheight=1)

        profile = select_presentation_profile(platform_name="windows")
        layout_px = lambda value: int(round(float(value) * 1.425))
        monkeypatch.setattr(
            preferences_dialog,
            "load_preferences",
            lambda: settings.resolve_preferences(),
        )
        preferences_panel = preferences_dialog.PreferencesPanel(
            preferences_host,
            ui_font_family="Arial",
            desktop_services=SimpleNamespace(),
            presentation_profile=profile,
            typography=splash_screen._embedded_panel_typography(),
            px=layout_px,
        )
        help_surface = help_panel.HelpPanel(
            help_host,
            px=layout_px,
            style=splash_screen._help_panel_style(px=layout_px),
            sections=keyboard_control_sections(profile),
        )
        help_surface.create()
        root.update_idletasks()

        streaming = preferences_panel.tab_strip._tab_labels["streaming"]
        keys = help_surface._tab_strip._tab_labels["keys"]
        host_origin = (host.winfo_rootx(), host.winfo_rooty())

        def relative_origin(widget):
            return (
                widget.winfo_rootx() - host_origin[0],
                widget.winfo_rooty() - host_origin[1],
            )

        assert relative_origin(streaming) == relative_origin(keys)
    finally:
        root.destroy()


def test_help_panel_uses_the_shared_card_alignment_and_spacing():
    table_source = inspect.getsource(help_panel.HelpPanel._render_table)
    card_source = inspect.getsource(help_panel.HelpPanel._draw_shortcut_card)
    intro_source = inspect.getsource(help_panel.HelpPanel._draw_card_intro)

    assert "y += self._metrics.section_gap_y" in table_source
    assert "card_left = self._px(TABBED_CONTENT_ALIGNMENT_INSET)" in intro_source
    assert "initial_geometry = measure_help_card(" in intro_source
    assert "content_x = initial_geometry.content.left" in intro_source
    assert "metrics.section_heading_to_description_y" in intro_source
    assert "metrics.section_description_to_content_y" in intro_source
    assert "y += metrics.card_item_gap_y" in card_source
    assert "canvas.create_line(" not in card_source


@pytest.mark.gui
def test_help_cards_and_compact_shortcuts_keep_measured_alignment():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        style = help_panel.HelpPanelStyle(
            background_color="#0a0a0d",
            section_background_color="#12121a",
            tab_active_color="#e5a11f",
            tab_indicator_color="#30343d",
            tab_focus_color="#5d6f8a",
            section_color="#cccdd6",
            keycap_background_color="#1c1c24",
            keycap_border_color="#3a4454",
            keycap_text_color="#cccdd6",
            action_color="#cccdd6",
            detail_color="#9a9aa6",
            error_color="#ff9b90",
            content_pad_x=24,
            tab_font=("Arial", 10, "bold"),
            tab_inactive_font=("Arial", 10),
            section_font=("Arial", 13, "bold"),
            section_summary_font=("Arial", 11),
            keycap_font=("Arial", 10, "bold"),
            action_font=("Arial", 10),
            overview_font=("Arial", 10, "bold"),
            detail_font=("Arial", 9),
            error_font=("Courier", 9),
        )
        panel = help_panel.HelpPanel(
            root,
            px=round,
            style=style,
            sections=keyboard_control_sections(
                select_presentation_profile(platform_name="windows")
            ),
        )
        panel.create()
        canvas = panel._content_canvas
        assert canvas is not None

        panel._render_table(800)
        def rendered_card_bounds():
            bounds = []
            for item in canvas.find_withtag("help-card"):
                points = canvas.coords(item)
                bounds.append(
                    (
                        min(points[0::2]),
                        min(points[1::2]),
                        max(points[0::2]),
                        max(points[1::2]),
                    )
                )
            return bounds

        card_bounds = rendered_card_bounds()
        assert len(card_bounds) == 3
        assert {(left, right) for left, _, right, _ in card_bounds} == {(12, 800)}
        card_gaps = [
            card_bounds[index + 1][1] - card_bounds[index][3]
            for index in range(2)
        ]
        assert card_gaps == [20, 20]

        title_item = next(
            item
            for item in canvas.find_all()
            if canvas.type(item) == "text" and canvas.itemcget(item, "text") == "Move"
        )
        first_action = panel._tab_sections["keys"][0].shortcuts[0].action
        action_item = next(
            item
            for item in canvas.find_all()
            if canvas.type(item) == "text"
            and canvas.itemcget(item, "text") == first_action
        )
        assert canvas.coords(title_item)[0] == 34
        assert canvas.coords(action_item)[0] > 34

        panel._render_table(380)
        compact_action_item = next(
            item
            for item in canvas.find_all()
            if canvas.type(item) == "text"
            and canvas.itemcget(item, "text") == first_action
        )
        assert canvas.coords(compact_action_item)[0] == 34
        keycap_item = canvas.find_withtag("help-keycap")[0]
        keycap_bottom = max(canvas.coords(keycap_item)[1::2])
        assert canvas.coords(compact_action_item)[1] > keycap_bottom

        panel._show_tab("capture")
        panel._render_table(800)
        capture_bounds = rendered_card_bounds()
        assert len(capture_bounds) == 4
        assert {
            capture_bounds[index + 1][1] - capture_bounds[index][3]
            for index in range(3)
        } == {20}

        panel._show_tab("troubleshooting")
        panel._render_table(800)
        troubleshooting_bounds = rendered_card_bounds()
        assert len(troubleshooting_bounds) == 2
        assert troubleshooting_bounds[1][1] - troubleshooting_bounds[0][3] == 20
        assert panel._troubleshooting_button is not None
        assert int(panel._troubleshooting_button.widget.cget("height")) == 40
    finally:
        root.destroy()


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
    assert "command=self._show_latest_log" in create_source
    assert "button.set_enabled(state.can_reveal)" in render_source
    assert "command=self._copy_last_error" in create_source
    assert 'section_id="application-logs"' in render_source
    assert 'section_id="last-error"' in render_source
    assert "state.error_excerpt" in render_source


def test_help_panel_only_renders_log_action_status_when_present():
    render_source = inspect.getsource(help_panel.HelpPanel._render_troubleshooting)

    assert "if state.status_text:" in render_source


def test_troubleshooting_aligns_and_places_copy_below_available_error_details():
    render_source = inspect.getsource(help_panel.HelpPanel._render_troubleshooting)

    assert "error_intro.content_x + text_pad" in render_source
    assert "error_intro.content_width - (text_pad * 2)" in render_source
    assert "radius=metrics.control_corner_radius" in render_source
    assert 'tags=("help-content", "help-error-excerpt")' in render_source
    assert "if copy_button is not None:" in render_source
    assert "window=copy_button.widget" in render_source
    assert "y = excerpt_bounds.bottom + metrics.card_item_gap_y" in render_source


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
