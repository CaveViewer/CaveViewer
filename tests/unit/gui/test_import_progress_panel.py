"""Tests for import-progress panel presentation helpers."""

from __future__ import annotations

from caveviewer.gui.import_progress_panel import ImportProgressPanel


def test_blank_stage_label_stays_blank():
    panel = object.__new__(ImportProgressPanel)

    assert panel._stage_label("") == ""
    assert panel._stage_label("   ") == ""


def test_auto_dive_indeterminate_stage_labels_are_specific():
    panel = object.__new__(ImportProgressPanel)

    assert panel._stage_label("planning guided dive") == "Planning Guided Dive…"
    assert panel._stage_label("looking for a path") == "Looking for a path…"
