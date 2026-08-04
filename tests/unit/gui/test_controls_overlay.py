"""Tests for loading/help overlay layout scaling."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caveviewer.gui import controls_overlay
from caveviewer.gui.platform.default import DefaultSplashPlatformAdapter
from caveviewer.gui.platform.macos import MacOSSplashPlatformAdapter


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


def test_recording_help_copy_is_format_neutral():
    rows = dict(controls_overlay._get_platform_control_rows())

    assert rows["REC button"] == "Start recording countdown"
    assert "MP4" not in rows["REC button"]
    assert rows["Ctrl + R"] == "Stop or cancel recording"
    assert rows["Ctrl + T"] == "Start/stop manual route trace"
    assert rows["Space"] == "Pause/resume a recorded dive"


def _control_rows_for_adapter(adapter) -> dict[str, str]:
    return {
        key: description
        for _section, rows in controls_overlay._get_platform_control_sections(adapter)
        for key, description in rows
    }


def test_control_help_copy_uses_adapter_for_macos_shortcuts():
    adapter = SimpleNamespace(
        bookmark_save_modifier=lambda: "command",
        primary_shortcut_modifier_label=lambda: "Cmd",
        mouse_look_button_name=lambda: "right",
    )

    rows = _control_rows_for_adapter(adapter)

    assert rows["Right click + mouse"] == "Look around"
    assert rows["Option + left click + mouse"] == "Look around (alternative)"
    assert rows["Cmd + 0"] == "Reset view (level horizon)"
    assert rows["Cmd + 1..9"] == "Save camera bookmark slot"
    assert rows["Cmd + O"] == "Switch to a different map"
    assert rows["Cmd + W"] == "Close window"
    assert rows["Cmd + R"] == "Stop or cancel recording"
    assert rows["Cmd + T"] == "Start/stop manual route trace"


def test_control_help_copy_uses_adapter_for_control_shortcuts():
    adapter = SimpleNamespace(
        bookmark_save_modifier=lambda: "control",
        primary_shortcut_modifier_label=lambda: "Ctrl",
        mouse_look_button_name=lambda: "left",
    )

    rows = _control_rows_for_adapter(adapter)

    assert rows["Left click + mouse"] == "Look around"
    assert "Right click + mouse" not in rows
    assert rows["Ctrl + 0"] == "Reset view (level horizon)"
    assert rows["Ctrl + 1..9"] == "Save camera bookmark slot"
    assert "Ctrl + A" not in rows
    assert rows["Ctrl + O"] == "Switch to a different map"
    assert rows["Ctrl + W"] == "Close window"
    assert rows["Ctrl + R"] == "Stop or cancel recording"
    assert rows["Ctrl + T"] == "Start/stop manual route trace"


def test_platform_adapters_define_controls_overlay_layout_policy():
    default_adapter = DefaultSplashPlatformAdapter()
    macos_adapter = MacOSSplashPlatformAdapter()

    assert default_adapter.primary_shortcut_modifier_label() == "Ctrl"
    assert default_adapter.compact_manual_controls_layout() is True
    assert macos_adapter.primary_shortcut_modifier_label() == "Cmd"
    assert macos_adapter.compact_manual_controls_layout() is False
