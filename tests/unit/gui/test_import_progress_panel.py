"""Tests for import-progress panel presentation helpers."""

from __future__ import annotations

from caveviewer.gui.import_progress_panel import ImportProgressPanel


def test_blank_stage_label_stays_blank():
    panel = object.__new__(ImportProgressPanel)

    assert panel._stage_label("") == ""
    assert panel._stage_label("   ") == ""


def test_render_cache_finalization_phases_are_user_facing():
    panel = object.__new__(ImportProgressPanel)

    assert panel._stage_label("assembling render manifest") == (
        "Assembling render manifest…"
    )
    assert panel._stage_label("building Guided Dive identity") == (
        "Creating Guided Dive identity…"
    )
