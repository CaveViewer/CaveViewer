"""Tests for loading/help overlay layout scaling."""

from __future__ import annotations

import inspect

import pytest

from caveviewer.gui import controls_overlay
from caveviewer.gui.platform.presentation import select_presentation_profile


def test_fullscreen_layout_scale_grows_for_large_viewer_surfaces():
    assert controls_overlay._fullscreen_layout_scale((1536, 864)) == 1.0
    assert controls_overlay._fullscreen_layout_scale((2048, 1152)) == pytest.approx(
        controls_overlay._FULLSCREEN_LAYOUT_SCALE_MAX
    )


def test_fullscreen_layout_scale_is_capped_for_very_large_viewer_surfaces():
    assert controls_overlay._fullscreen_layout_scale((3840, 2160)) == pytest.approx(
        controls_overlay._FULLSCREEN_LAYOUT_SCALE_MAX
    )


def test_minimum_control_row_height_reserves_space_between_keycaps(monkeypatch):
    monkeypatch.setattr(
        controls_overlay.bitmap_font,
        "text_height_px",
        lambda _size: 20.0,
    )

    row_height = controls_overlay._minimum_control_row_height(
        key_size=1.0,
        desc_size=1.0,
        key_pad_y=4.0,
        keycap_row_gap=4.0,
    )

    assert row_height == 32.0


def test_keycap_tiers_share_widths_while_descriptive_controls_fit_labels(
    monkeypatch,
):
    overlay = controls_overlay.ControlsOverlay.__new__(controls_overlay.ControlsOverlay)
    widths = {
        "W": 16.0,
        "A": 10.0,
        "-": 4.0,
        "Cmd": 22.0,
        "Ctrl": 24.0,
        "Shift": 32.0,
        "Scroll": 36.0,
        "Space": 30.0,
        "Escape": 42.0,
        "Del": 20.0,
        "Minimap click": 64.0,
    }
    monkeypatch.setattr(
        controls_overlay.bitmap_font,
        "text_width_px",
        lambda text, _size: widths[text],
    )

    assert overlay._keycap_width("W", 1.0, 4.0) == 24.0
    assert overlay._keycap_width("A", 1.0, 4.0) == 24.0
    assert overlay._keycap_width("-", 1.0, 4.0) == 24.0
    assert overlay._keycap_width("Ctrl", 1.0, 4.0) == 44.0
    assert overlay._keycap_width("Shift", 1.0, 4.0) == 44.0
    assert overlay._keycap_width("Scroll", 1.0, 4.0) == 44.0
    assert overlay._keycap_width("Space", 1.0, 4.0) == 50.0
    assert overlay._keycap_width("Escape", 1.0, 4.0) == 50.0
    assert overlay._keycap_width("Del", 1.0, 4.0) == 44.0
    assert overlay._keycap_width("Minimap click", 1.0, 4.0) == 72.0


def test_single_keycap_glyph_is_centered_in_its_shared_width(monkeypatch):
    overlay = controls_overlay.ControlsOverlay.__new__(controls_overlay.ControlsOverlay)
    overlay._keycap_parts = lambda _label: ["A"]
    overlay._keycap_width = lambda _part, _size, _pad: 24.0
    monkeypatch.setattr(
        controls_overlay.bitmap_font,
        "text_width_px",
        lambda _text, _size: 10.0,
    )
    monkeypatch.setattr(
        controls_overlay.bitmap_font,
        "text_height_px",
        lambda _size: 12.0,
    )
    text_calls = []

    overlay._draw_keycap_sequence(
        lambda *_args: None,
        lambda text, x, *_args: text_calls.append((text, x)),
        "A",
        x=20.0,
        y=10.0,
        key_size=1.0,
        key_pad_x=4.0,
        key_pad_y=3.0,
    )

    assert text_calls == [("A", 27.0)]


def test_capture_keycap_labels_are_centered_in_their_shared_width(monkeypatch):
    overlay = controls_overlay.ControlsOverlay.__new__(controls_overlay.ControlsOverlay)
    overlay._keycap_parts = lambda _label: ["Space", "Escape"]
    overlay._keycap_width = lambda _part, _size, _pad: 50.0
    widths = {"Space": 30.0, "Escape": 42.0}
    monkeypatch.setattr(
        controls_overlay.bitmap_font,
        "text_width_px",
        lambda text, _size: widths[text],
    )
    monkeypatch.setattr(
        controls_overlay.bitmap_font,
        "text_height_px",
        lambda _size: 12.0,
    )
    text_calls = []

    overlay._draw_keycap_sequence(
        lambda *_args: None,
        lambda text, x, *_args: text_calls.append((text, x)),
        "Space Escape",
        x=20.0,
        y=10.0,
        key_size=1.0,
        key_pad_x=4.0,
        key_pad_y=3.0,
    )

    assert text_calls == [("Space", 30.0), ("Escape", 79.0)]


def test_compound_shortcut_plus_uses_one_centered_keycap_unit(monkeypatch):
    overlay = controls_overlay.ControlsOverlay.__new__(controls_overlay.ControlsOverlay)
    overlay._keycap_parts = lambda _label: ["Ctrl", "+", "R"]
    overlay._keycap_width = lambda _part, _size, _pad: 24.0
    widths = {"Ctrl": 18.0, "+": 8.0, "R": 10.0}
    monkeypatch.setattr(
        controls_overlay.bitmap_font,
        "text_width_px",
        lambda text, _size: widths[text],
    )
    monkeypatch.setattr(
        controls_overlay.bitmap_font,
        "text_height_px",
        lambda _size: 12.0,
    )
    text_calls = []

    assert overlay._measure_keycap_sequence("Ctrl + R", 1.0, 4.0) == 82.0
    overlay._draw_keycap_sequence(
        lambda *_args: None,
        lambda text, x, *_args: text_calls.append((text, x)),
        "Ctrl + R",
        x=20.0,
        y=10.0,
        key_size=1.0,
        key_pad_x=4.0,
        key_pad_y=3.0,
    )

    assert text_calls == [("Ctrl", 23.0), ("+", 57.0), ("R", 85.0)]


def test_fullscreen_begin_prompt_respects_budget_limited_wanted_count():
    overlay = controls_overlay.ControlsOverlay.__new__(controls_overlay.ControlsOverlay)
    overlay._active = True
    overlay._awaiting_begin = True
    overlay._ready_to_begin = False
    overlay._manual_mode = False
    overlay._progress_fraction = 0.0

    overlay.update({"loaded": 3, "pending": 0, "ready": 0, "wanted": 3})

    assert overlay.is_ready_to_begin is True


def test_fullscreen_begin_prompt_counts_failed_wanted_chunks():
    overlay = controls_overlay.ControlsOverlay.__new__(controls_overlay.ControlsOverlay)
    overlay._active = True
    overlay._awaiting_begin = True
    overlay._ready_to_begin = False
    overlay._manual_mode = False
    overlay._progress_fraction = 0.0

    overlay.update(
        {
            "loaded": 2,
            "failed_wanted": 1,
            "pending": 0,
            "ready": 0,
            "wanted": 3,
        }
    )

    assert overlay.is_ready_to_begin is True


def test_fullscreen_begin_prompt_waits_for_current_wanted_cells():
    overlay = controls_overlay.ControlsOverlay.__new__(controls_overlay.ControlsOverlay)
    overlay._active = True
    overlay._awaiting_begin = True
    overlay._ready_to_begin = False
    overlay._manual_mode = False
    overlay._progress_fraction = 0.0

    overlay.update(
        {
            "loaded_wanted": 6,
            "pending": 21,
            "ready": 0,
            "wanted": 27,
            "total_available": 1655,
        }
    )

    assert overlay.is_ready_to_begin is False

    overlay.update(
        {
            "loaded_wanted": 27,
            "pending": 0,
            "ready": 0,
            "wanted": 27,
            "total_available": 1655,
        }
    )

    assert overlay.is_ready_to_begin is True


def test_fullscreen_begin_prompt_waits_for_visual_ready_signal():
    overlay = controls_overlay.ControlsOverlay.__new__(controls_overlay.ControlsOverlay)
    overlay._active = True
    overlay._awaiting_begin = True
    overlay._ready_to_begin = False
    overlay._manual_mode = False
    overlay._progress_fraction = 0.0

    overlay.update(
        {
            "loaded_wanted": 27,
            "pending": 0,
            "ready": 0,
            "wanted": 27,
            "total_available": 1655,
            "visual_ready": False,
        }
    )

    assert overlay.is_ready_to_begin is False

    overlay.update(
        {
            "loaded_wanted": 27,
            "pending": 0,
            "ready": 0,
            "wanted": 27,
            "total_available": 1655,
            "visual_ready": True,
        }
    )

    assert overlay.is_ready_to_begin is True


def test_recording_help_copy_is_shortcut_only_and_format_neutral():
    rows = _control_rows_for_profile(select_presentation_profile(platform_name="unsupported"))

    assert "REC button" not in rows
    assert rows["W A S D"] == "Move forward, left, backward, and right"
    assert rows["E Q"] == "Move up / down"
    assert "E / Q" not in rows
    assert rows["Ctrl + R"] == "Start/stop recording"
    assert rows["Ctrl + T"] == "Start/stop manual trace"
    assert rows["Ctrl + C"] == "Start/stop slice"
    assert rows["Escape"] == "Cancel active capture"
    assert rows["Space"] == "Pause/resume recorded dive"
    assert rows["← → ↑ ↓"] == "Look left, right, up, and down"
    assert "Arrow keys" not in rows
    assert "Ctrl + Shift + P" not in rows
    assert "Pause active import" not in rows.values()
    assert "Ctrl + Shift + 1–9" not in rows


def test_controls_overlay_uses_the_shared_keyboard_catalog():
    source = inspect.getsource(controls_overlay._get_platform_control_sections)

    assert "keyboard_control_sections(" in source
    assert '"Start/stop recording"' not in source
    assert '"Start/stop manual trace"' not in source
    assert '"Start/stop slice"' not in source


def test_controls_overlay_groups_recorded_dive_with_capture():
    sections = dict(
        controls_overlay._get_platform_control_sections(
            select_presentation_profile(platform_name="unsupported")
        )
    )

    assert ("Space", "Pause/resume recorded dive") not in sections["Navigate"]
    assert ("Space", "Pause/resume recorded dive") in sections["Capture"]


def test_grouped_control_section_headings_share_one_left_edge(monkeypatch):
    overlay = controls_overlay.ControlsOverlay.__new__(controls_overlay.ControlsOverlay)
    overlay._measure_keycap_sequence = lambda label, *_args: len(label) * 10.0
    overlay._draw_keycap_sequence = lambda *_args: None
    monkeypatch.setattr(
        controls_overlay.bitmap_font,
        "text_width_px",
        lambda text, _size: len(text) * 10.0,
    )
    monkeypatch.setattr(
        controls_overlay.bitmap_font,
        "text_height_px",
        lambda _size: 10.0,
    )
    text_calls = []

    overlay._draw_control_column(
        lambda *_args: None,
        lambda text, x, *_args: text_calls.append((text, x)),
        [
            ("Look", [("← → ↑ ↓", "Look left, right, up, and down")]),
            ("Capture", [("Ctrl + R", "Start recording")]),
        ],
        x=20.0,
        top_y=10.0,
        key_col_width=100.0,
        heading_size=1.0,
        key_size=1.0,
        desc_size=1.0,
        row_height=20.0,
        heading_gap=5.0,
        section_gap=10.0,
        key_pad_x=4.0,
        key_pad_y=3.0,
        key_desc_gap=12.0,
    )

    heading_x = {text: x for text, x in text_calls if text in {"LOOK", "CAPTURE"}}

    assert heading_x["CAPTURE"] == heading_x["LOOK"]


def _control_rows_for_profile(profile) -> dict[str, str]:
    return {
        key: description
        for _section, rows in controls_overlay._get_platform_control_sections(profile)
        for key, description in rows
    }


def test_control_help_copy_uses_profile_for_macos_shortcuts():
    rows = _control_rows_for_profile(
        select_presentation_profile(platform_name="darwin")
    )

    assert rows["Right click + mouse"] == "Look around"
    assert rows["Option + left click + mouse"] == "Look around (alternative)"
    assert rows["Cmd + 0"] == "Reset view (level horizon)"
    assert rows["Cmd + 1–9"] == "Save camera bookmark"
    assert "Cmd + O" not in rows
    assert "Open another map" not in rows.values()
    assert rows["Escape"] == "Cancel active capture"
    assert "Cmd + W" not in rows
    assert rows["Cmd + R"] == "Start/stop recording"
    assert rows["Cmd + T"] == "Start/stop manual trace"
    assert rows["Cmd + C"] == "Start/stop slice"


def test_control_help_copy_uses_profile_for_control_shortcuts():
    rows = _control_rows_for_profile(
        select_presentation_profile(platform_name="unsupported")
    )

    assert rows["-"] == "Decrease fly speed"
    assert rows["="] == "Increase fly speed"
    assert rows["Scroll"] == "Adjust fly speed"
    assert rows["Left click + mouse"] == "Look around"
    assert "Right click + mouse" not in rows
    assert rows["Ctrl + 0"] == "Reset view (level horizon)"
    assert rows["Ctrl + 1–9"] == "Save camera bookmark"
    assert "Ctrl + A" not in rows
    assert "Ctrl + O" not in rows
    assert "Open another map" not in rows.values()
    assert "Open button" not in rows
    assert "Switch to a different map" not in rows.values()
    assert rows["Escape"] == "Cancel active capture"
    assert "Ctrl + W" not in rows
    assert rows["Ctrl + R"] == "Start/stop recording"
    assert rows["Ctrl + T"] == "Start/stop manual trace"
    assert rows["Ctrl + C"] == "Start/stop slice"


def test_presentation_profiles_define_controls_overlay_layout_policy():
    default_profile = select_presentation_profile(platform_name="unsupported")
    macos_profile = select_presentation_profile(platform_name="darwin")

    assert default_profile.primary_shortcut_modifier_label == "Ctrl"
    assert default_profile.compact_manual_controls_layout is True
    assert macos_profile.primary_shortcut_modifier_label == "Cmd"
    assert macos_profile.compact_manual_controls_layout is False
