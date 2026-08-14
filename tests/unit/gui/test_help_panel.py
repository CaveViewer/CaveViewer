"""Exercise deterministic layout policy for the splash Help presentation."""

from __future__ import annotations

import inspect

from caveviewer.gui import help_panel


def test_help_panel_uses_two_sections_only_when_the_content_is_wide_enough():
    assert (
        help_panel.help_section_column_count(
            639,
            two_column_min_width=640,
        )
        == 1
    )
    assert (
        help_panel.help_section_column_count(
            640,
            two_column_min_width=640,
        )
        == 2
    )
    assert (
        help_panel.help_section_column_count(
            "not-a-width",
            two_column_min_width=640,
        )
        == 1
    )


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


def test_help_panel_is_embedded_and_uses_the_shared_scroll_normalizer():
    source = inspect.getsource(help_panel.HelpPanel)

    assert "tk.Canvas(" in source
    assert "tk.Toplevel" not in source
    assert "vertical_scroll_units(event)" in source
    assert "scrollbar.grid_remove()" in source
    assert "def focus_content" in source
