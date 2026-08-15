"""Exercise deterministic presentation policy for the splash Keys table."""

from __future__ import annotations

import inspect

from caveviewer.gui import help_panel


def test_help_panel_uses_the_shared_canvas_scrollbar():
    source = inspect.getsource(help_panel.HelpPanel)

    assert "CanvasVerticalScrollbar(" in source
    assert "CanvasScrollbarStyle(" in source
    assert "tk.Scrollbar(" not in source
    assert "self._scrollbar.bind_mousewheel(content)" in source


def test_help_panel_uses_a_compact_keys_table_without_redundant_labels():
    source = inspect.getsource(help_panel.HelpPanel)

    assert 'TopTab("keys", "Keys")' in source
    assert "TopTabStrip(" in source
    assert "def _create_keycap_sequence" in source
    assert "shortcut_keycap_parts(shortcut)" in source
    assert "column=2" in source
    assert "Keyboard shortcuts" not in source
    assert "Controls are shown for this platform" not in source
    assert "help_section_column_count" not in source


def test_help_panel_is_embedded_and_uses_the_shared_scroll_host():
    source = inspect.getsource(help_panel.HelpPanel)

    assert "tk.Canvas(" in source
    assert "tk.Toplevel" not in source
    assert "scrollbar.sync_overflow(content_height)" in source
    assert "def focus_content" in source
