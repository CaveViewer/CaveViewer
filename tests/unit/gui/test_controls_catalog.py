"""Exercise the shared, platform-aware catalog of direct viewer keys."""

from __future__ import annotations

from caveviewer.gui.controls_catalog import (
    is_help_shortcut_visible,
    keyboard_control_sections,
    shortcut_keycap_parts,
    shortcut_keycap_unit_count,
)
from caveviewer.gui.platform.presentation import select_presentation_profile


def _shortcut_rows(profile):
    return {
        shortcut.id: shortcut
        for section in keyboard_control_sections(profile)
        for shortcut in section.shortcuts
    }


def test_keyboard_catalog_covers_each_direct_viewer_binding_once():
    profile = select_presentation_profile(platform_name="unsupported")
    sections = keyboard_control_sections(profile)
    rows = _shortcut_rows(profile)
    sections_by_id = {section.id: section for section in sections}

    assert [section.id for section in sections] == [
        "movement",
        "view",
        "bookmarks",
        "map",
        "map-import",
        "recorded-dive",
        "capture",
    ]
    assert set(rows) == {
        "move-strafe",
        "move-vertical",
        "move-speed-boost",
        "move-speed-decrease",
        "move-speed-increase",
        "look-arrows",
        "look-jlik",
        "look-roll",
        "view-reset",
        "bookmark-save",
        "bookmark-recall",
        "bookmark-delete",
        "bookmark-delete-control-shift",
        "map-open",
        "import-pause",
        "recorded-dive-space",
        "recording-toggle",
        "manual-trace-toggle",
        "slice-toggle",
        "capture-cancel",
    }
    assert sections_by_id["map-import"].title == "Map Import"
    assert [
        shortcut.id for shortcut in sections_by_id["map-import"].shortcuts
    ] == ["import-pause"]
    assert is_help_shortcut_visible(rows["import-pause"]) is False


def test_keyboard_catalog_uses_direct_control_bindings_and_capture_standard():
    rows = _shortcut_rows(select_presentation_profile(platform_name="windows"))

    assert rows["move-strafe"].action == "Move forward, left, backward, and right"
    assert rows["view-reset"].shortcut == "Ctrl + 0"
    assert rows["bookmark-save"].shortcut == "Ctrl + 1–9"
    assert rows["bookmark-delete-control-shift"].shortcut == "Ctrl + Shift + 1–9"
    assert rows["bookmark-delete"].shortcut == "Del + 1–9"
    assert rows["map-open"].shortcut == "Ctrl + O"
    assert rows["recording-toggle"].shortcut == "Ctrl + R"
    assert rows["recording-toggle"].action == "Start/stop recording"
    assert rows["manual-trace-toggle"].shortcut == "Ctrl + T"
    assert rows["manual-trace-toggle"].action == "Start/stop manual trace"
    assert rows["slice-toggle"].shortcut == "Ctrl + C"
    assert rows["slice-toggle"].action == "Start/stop slice"
    assert rows["capture-cancel"].shortcut == "Escape"
    assert rows["capture-cancel"].action == "Cancel active capture"
    assert rows["import-pause"].shortcut == "Ctrl + Shift + P"
    assert rows["look-arrows"].shortcut == "← → ↑ ↓"
    assert rows["recorded-dive-space"].action == "Pause/resume recorded dive"


def test_keyboard_catalog_uses_command_labels_and_bookmark_fallback_on_macos():
    rows = _shortcut_rows(select_presentation_profile(platform_name="darwin"))

    assert rows["view-reset"].shortcut == "Cmd + 0"
    assert rows["bookmark-save"].shortcut == "Cmd + 1–9"
    assert rows["bookmark-save-shift-fallback"].shortcut == "Shift + 1–9"
    assert rows["map-open"].shortcut == "Cmd + O"
    assert rows["recording-toggle"].shortcut == "Cmd + R"
    assert rows["manual-trace-toggle"].shortcut == "Cmd + T"
    assert rows["slice-toggle"].shortcut == "Cmd + C"
    assert rows["import-pause"].shortcut == "Cmd + Shift + P"


def test_keyboard_catalog_excludes_contextual_splash_navigation_shortcuts():
    rows = _shortcut_rows(select_presentation_profile(platform_name="unsupported"))
    rendered_shortcuts = {shortcut.shortcut for shortcut in rows.values()}
    actions = {shortcut.action for shortcut in rows.values()}

    assert "Ctrl + W" not in rendered_shortcuts
    assert "Return" not in rendered_shortcuts
    assert "Tab / Shift + Tab" not in rendered_shortcuts
    assert "Return, cancel, or close" not in actions


def test_keycap_parts_keep_compound_shortcuts_readable():
    assert shortcut_keycap_parts("W A S D") == ("W", "A", "S", "D")
    assert shortcut_keycap_parts("E Q") == ("E", "Q")
    assert shortcut_keycap_parts("- =") == ("-", "=")
    assert shortcut_keycap_parts("Ctrl + Shift + P") == (
        "Ctrl",
        "+",
        "Shift",
        "+",
        "P",
    )
    assert shortcut_keycap_parts("← → ↑ ↓") == ("←", "→", "↑", "↓")


def test_keycap_unit_counts_define_complete_compact_spans():
    for part in ("W", "Q", "←", "="):
        assert shortcut_keycap_unit_count(part) == 1

    for part in (
        "Cmd",
        "Ctrl",
        "Del",
        "Scroll",
        "Shift",
        "1–9",
        "Escape",
        "Space",
    ):
        assert shortcut_keycap_unit_count(part) == 2

    assert shortcut_keycap_unit_count("Left-drag") is None
    assert shortcut_keycap_unit_count("Minimap click") is None
    assert shortcut_keycap_unit_count("+") is None
    assert shortcut_keycap_unit_count("/") is None
