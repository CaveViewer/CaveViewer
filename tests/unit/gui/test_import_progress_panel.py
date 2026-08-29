"""Tests for import-progress panel presentation helpers."""

from __future__ import annotations

import pytest

from caveviewer.gui import import_progress_panel
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
        "Creating dive plan identity…"
    )


def test_ring_labels_share_the_import_title_stage_note_layout(monkeypatch):
    panel = object.__new__(ImportProgressPanel)
    text_calls = []

    monkeypatch.setattr(
        import_progress_panel.bitmap_font,
        "text_bounds_px",
        lambda text, pixel_size: (0.0, 0.0, len(text) * pixel_size, pixel_size),
    )

    def record_text(text, _x, y, pixel_size):
        text_calls.append((text, y, pixel_size))
        return ()

    monkeypatch.setattr(
        import_progress_panel.bitmap_font,
        "iter_text_pixels",
        record_text,
    )

    panel._add_ring_labels(
        add_quad_px=lambda *_args: None,
        center_x=400.0,
        center_y=300.0,
        window_width=800.0,
        title="Preparing Map",
        stage="Building map chunks…",
        note="First-time setup in progress.",
    )

    assert text_calls == [
        ("Preparing Map", 172.0, ImportProgressPanel.TITLE_TEXT_SIZE),
        ("Building map chunks…", 416.0, ImportProgressPanel.STAGE_TEXT_SIZE),
        (
            "First-time setup in progress.",
            pytest.approx(440.65),
            ImportProgressPanel.NOTE_TEXT_SIZE,
        ),
    ]


def test_progress_ring_shader_uses_framebuffer_derivative_smoothing():
    source = import_progress_panel._LOGO_FRAG_SRC

    assert "fwidth(dist)" in source
    assert "fwidth(pixel_progress)" in source
    assert "fwidth(arc_offset)" in source
    assert "float edge = 0.005;" not in source
    assert "step(pixel_progress, progress)" not in source
