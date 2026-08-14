"""Exercise the shared, platform-aware keyboard-control catalog."""

from __future__ import annotations

from caveviewer.gui.controls_catalog import keyboard_control_sections
from caveviewer.gui.platform.presentation import select_presentation_profile


def _shortcut_rows(profile, *, include_main_window: bool = True):
    return {
        shortcut.id: shortcut
        for section in keyboard_control_sections(
            profile,
            include_main_window=include_main_window,
        )
        for shortcut in section.shortcuts
    }


def test_keyboard_catalog_covers_each_supported_command_once():
    profile = select_presentation_profile(platform_name="unsupported")
    sections = keyboard_control_sections(profile)
    rows = _shortcut_rows(profile)

    assert [section.id for section in sections] == [
        "main-window",
        "move",
        "look",
        "navigate",
        "capture",
    ]
    assert set(rows) == {
        "main-window-focus",
        "main-window-activate",
        "main-window-open-local-map",
        "main-window-back-or-close",
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
        "map-open",
        "recorded-dive-space",
        "viewer-escape",
        "recording-toggle",
        "manual-trace-toggle",
        "slice-toggle",
        "import-pause",
    }


def test_keyboard_catalog_uses_control_labels_and_capture_standard():
    rows = _shortcut_rows(select_presentation_profile(platform_name="windows"))

    assert rows["view-reset"].shortcut == "Ctrl + 0"
    assert rows["bookmark-save"].shortcut == "Ctrl + 1..9"
    assert rows["map-open"].shortcut == "Ctrl + O"
    assert rows["recording-toggle"].shortcut == "Ctrl + R"
    assert rows["recording-toggle"].action == "Start/stop recording"
    assert rows["manual-trace-toggle"].shortcut == "Ctrl + T"
    assert rows["manual-trace-toggle"].action == "Start/stop manual trace"
    assert rows["slice-toggle"].shortcut == "Ctrl + C"
    assert rows["slice-toggle"].action == "Start/stop slice"
    assert "finishes and saves" in rows["slice-toggle"].context_note
    assert rows["import-pause"].shortcut == "Ctrl + Shift + P"
    assert rows["look-arrows"].shortcut == "Arrow keys"


def test_keyboard_catalog_uses_command_labels_and_bookmark_fallback_on_macos():
    rows = _shortcut_rows(select_presentation_profile(platform_name="darwin"))

    assert rows["view-reset"].shortcut == "Cmd + 0"
    assert rows["bookmark-save"].shortcut == "Cmd + 1..9"
    assert "Shift + digit" in rows["bookmark-save"].context_note
    assert rows["map-open"].shortcut == "Cmd + O"
    assert rows["recording-toggle"].shortcut == "Cmd + R"
    assert rows["manual-trace-toggle"].shortcut == "Cmd + T"
    assert rows["slice-toggle"].shortcut == "Cmd + C"
    assert rows["import-pause"].shortcut == "Cmd + Shift + P"


def test_viewer_catalog_omits_main_window_only_shortcuts():
    rows = _shortcut_rows(
        select_presentation_profile(platform_name="unsupported"),
        include_main_window=False,
    )

    assert "main-window-focus" not in rows
    assert "recording-toggle" in rows
    assert "import-pause" in rows
