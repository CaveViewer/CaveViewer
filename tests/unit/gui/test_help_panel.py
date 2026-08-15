"""Exercise deterministic presentation policy for the splash Keys table."""

from __future__ import annotations

import inspect

from caveviewer.gui import help_panel


def test_help_panel_scrollbar_is_shown_only_for_overflowing_content():
    class _FakeScrollbar:
        def __init__(self) -> None:
            self.calls = []

        def grid(self) -> None:
            self.calls.append("grid")

        def grid_remove(self) -> None:
            self.calls.append("grid_remove")

    panel = object.__new__(help_panel.HelpPanel)
    scrollbar = _FakeScrollbar()
    panel._scrollbar = scrollbar
    panel._scrollbar_visible = False

    panel._set_scrollbar_visible(False)
    panel._set_scrollbar_visible(True)
    panel._set_scrollbar_visible(True)
    panel._set_scrollbar_visible(False)

    assert scrollbar.calls == ["grid", "grid_remove"]


def test_help_panel_uses_a_compact_keys_table_without_redundant_labels():
    source = inspect.getsource(help_panel.HelpPanel)

    assert 'text="Keys"' in source
    assert "def _create_keycap_sequence" in source
    assert "shortcut_keycap_parts(shortcut)" in source
    assert "column=2" in source
    assert "Keyboard shortcuts" not in source
    assert "Controls are shown for this platform" not in source
    assert "help_section_column_count" not in source


def test_help_panel_is_embedded_and_uses_the_shared_scroll_normalizer():
    source = inspect.getsource(help_panel.HelpPanel)

    assert "tk.Canvas(" in source
    assert "tk.Toplevel" not in source
    assert "vertical_scroll_units(event)" in source
    assert "scrollbar.grid_remove()" in source
    assert "def focus_content" in source
