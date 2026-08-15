"""Exercise deterministic state changes for the shared underline-tab strip."""

from __future__ import annotations

import inspect

import pytest

from caveviewer.gui.top_tab_strip import (
    TABBED_CONTENT_TOP_GAP,
    TopTab,
    TopTabbedContentSurface,
    TopTabbedContentSurfaceStyle,
    TopTabStrip,
    TopTabStripStyle,
    next_tab_key,
)


_TABS = (
    TopTab("streaming", "Streaming"),
    TopTab("parsing", "Import"),
    TopTab("storage", "Storage"),
)


def test_next_tab_key_moves_in_both_directions_and_wraps():
    assert next_tab_key(_TABS, "streaming", 1) == "parsing"
    assert next_tab_key(_TABS, "streaming", -1) == "storage"
    assert next_tab_key(_TABS, "storage", 1) == "streaming"


def test_next_tab_key_rejects_an_empty_strip():
    with pytest.raises(ValueError, match="must not be empty"):
        next_tab_key((), "keys", 1)


def test_tab_strip_selects_the_active_label_and_notifies_its_owner():
    class _FakeWidget:
        def __init__(self) -> None:
            self.configurations = []

        def configure(self, **options) -> None:
            self.configurations.append(options)

    strip = object.__new__(TopTabStrip)
    strip._tabs = _TABS
    strip._active_key = "streaming"
    strip._style = TopTabStripStyle(
        background_color="#101018",
        baseline_color="#292b35",
        active_color="#f0ad22",
        inactive_color="#a9abb8",
        focus_color="#f0ad22",
        font=("TkDefaultFont", 12, "bold"),
    )
    strip._tab_labels = {tab.key: _FakeWidget() for tab in _TABS}
    strip._indicators = {tab.key: _FakeWidget() for tab in _TABS}
    selected = []
    strip._on_selected = selected.append

    strip.select("storage")

    assert strip.active_key == "storage"
    assert selected == ["storage"]
    assert strip._tab_labels["storage"].configurations[-1] == {"fg": "#f0ad22"}
    assert strip._tab_labels["streaming"].configurations[-1] == {"fg": "#a9abb8"}
    assert strip._indicators["storage"].configurations[-1] == {"bg": "#f0ad22"}
    assert strip._indicators["streaming"].configurations[-1] == {"bg": "#101018"}


def test_tabbed_content_surface_defines_one_standard_content_gap():
    style = TopTabbedContentSurfaceStyle(
        background_color="#101018",
        content_pad_x=32,
    )
    source = inspect.getsource(TopTabbedContentSurface)

    assert TABBED_CONTENT_TOP_GAP == 26
    assert style.content_bottom_pad_y == 0
    assert "horizontal_inset=0" in source
    assert "top_inset=0" in source
    assert "TABBED_CONTENT_TOP_GAP" in source
