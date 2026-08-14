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
    assert rows["Ctrl + R"] == "Start/stop recording"
    assert rows["Ctrl + T"] == "Start/stop manual trace"
    assert rows["Ctrl + C"] == "Start/stop slice"
    assert rows["Space"] == "Pause/resume a recorded dive"
    assert rows["Arrow keys"] == "Look around"
    assert rows["Ctrl + Shift + P"] == "Pause active map import"


def test_controls_overlay_uses_the_shared_keyboard_catalog():
    source = inspect.getsource(controls_overlay._get_platform_control_sections)

    assert "keyboard_control_sections(" in source
    assert '"Start/stop recording"' not in source
    assert '"Start/stop manual trace"' not in source
    assert '"Start/stop slice"' not in source


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
    assert rows["Cmd + 1..9"] == "Save camera bookmark slot"
    assert rows["Cmd + O"] == "Switch to a different map"
    assert rows["Esc"] == "Close window"
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
    assert rows["Ctrl + 1..9"] == "Save camera bookmark slot"
    assert "Ctrl + A" not in rows
    assert rows["Ctrl + O"] == "Switch to a different map"
    assert rows["Esc"] == "Close window"
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
