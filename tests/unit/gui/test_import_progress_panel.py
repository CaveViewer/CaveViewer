"""Tests for import-progress panel presentation helpers."""

from __future__ import annotations

import pytest

from caveviewer.gui import import_progress_panel
from caveviewer.gui.import_progress_panel import ImportProgressPanel


def test_blank_stage_label_stays_blank():
    panel = object.__new__(ImportProgressPanel)

    assert panel._stage_label("") == ""
    assert panel._stage_label("   ") == ""


def test_import_progress_bar_uses_the_viewport_midpoint_as_its_anchor():
    assert import_progress_panel._progress_bar_center_y(600.0) == 300.0


def test_render_cache_finalization_phases_are_user_facing():
    panel = object.__new__(ImportProgressPanel)

    assert panel._stage_label("building cache") == "Building map cache…"
    assert panel._stage_label("assembling render manifest") == (
        "Assembling render manifest…"
    )
    assert panel._stage_label("building Guided Dive identity") == (
        "Creating dive plan identity…"
    )
    assert panel._stage_label("preparing cave") == "Preparing cave…"


def test_resume_stage_uses_the_active_scanning_label():
    panel = object.__new__(ImportProgressPanel)

    assert panel._stage_label("resuming import") == "Scanning map…"


def test_circle_labels_share_the_import_title_stage_note_layout(monkeypatch):
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

    panel._add_circle_labels(
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
            pytest.approx(440.55),
            ImportProgressPanel.NOTE_TEXT_SIZE,
        ),
    ]


def test_bar_labels_use_compact_progress_layout(monkeypatch):
    panel = object.__new__(ImportProgressPanel)
    text_calls = []

    monkeypatch.setattr(
        import_progress_panel.bitmap_font,
        "text_bounds_px",
        lambda text, pixel_size: (0.0, 0.0, len(text) * pixel_size, pixel_size),
    )
    monkeypatch.setattr(
        import_progress_panel.bitmap_font,
        "iter_text_pixels",
        lambda text, _x, y, pixel_size: text_calls.append(
            (text, y, pixel_size)
        )
        or (),
    )

    panel._add_bar_labels(
        add_quad_px=lambda *_args: None,
        center_x=400.0,
        bar_center_y=300.0,
        window_width=800.0,
        title="Preparing Map",
        stage="Building map chunksâ€¦",
        note="First-time setup in progress.",
    )

    assert text_calls == [
        ("Preparing Map", 164.0, ImportProgressPanel.TITLE_TEXT_SIZE),
        (
            "Building map chunksâ€¦",
            pytest.approx(255.45),
            ImportProgressPanel.STAGE_TEXT_SIZE,
        ),
        (
            "First-time setup in progress.",
            pytest.approx(332.0),
            ImportProgressPanel.NOTE_TEXT_SIZE,
        ),
    ]


def test_bar_note_flattens_line_breaks_to_one_supporting_line(monkeypatch):
    panel = object.__new__(ImportProgressPanel)
    text_calls = []
    note = "First-time setup in progress.\nNext time, this map will open faster."
    flattened_note = "First-time setup in progress. Next time, this map will open faster."

    monkeypatch.setattr(
        import_progress_panel.bitmap_font,
        "text_bounds_px",
        lambda text, pixel_size: (0.0, 0.0, len(text) * pixel_size, pixel_size),
    )
    monkeypatch.setattr(
        import_progress_panel.bitmap_font,
        "iter_text_pixels",
        lambda text, x, y, pixel_size: text_calls.append((text, x, y, pixel_size))
        or (),
    )

    panel._add_bar_labels(
        add_quad_px=lambda *_args: None,
        center_x=400.0,
        bar_center_y=300.0,
        window_width=800.0,
        title="",
        stage="",
        note=note,
    )

    assert text_calls == [
        (
            flattened_note,
            pytest.approx(400.0 - len(flattened_note) * 1.94 / 2.0),
            pytest.approx(332.0),
            pytest.approx(1.94),
        )
    ]


def test_progress_labels_match_fullscreen_prompt_scaling(monkeypatch):
    panel = object.__new__(ImportProgressPanel)
    text_calls = []
    monkeypatch.setattr(
        import_progress_panel.bitmap_font,
        "text_bounds_px",
        lambda text, pixel_size: (0.0, 0.0, len(text) * pixel_size, pixel_size),
    )
    monkeypatch.setattr(
        import_progress_panel.bitmap_font,
        "iter_text_pixels",
        lambda text, _x, y, pixel_size: text_calls.append((text, y, pixel_size))
        or (),
    )

    assert import_progress_panel._progress_label_layout_scale((1280, 720)) == 1.0
    assert import_progress_panel._progress_label_layout_scale(
        (1920, 1080)
    ) == pytest.approx(1.25)
    assert import_progress_panel._progress_label_layout_scale(
        (3840, 2160)
    ) == pytest.approx(1.32)

    panel._add_bar_labels(
        add_quad_px=lambda *_args: None,
        center_x=400.0,
        bar_center_y=300.0,
        window_width=800.0,
        title="Preparing map",
        stage="Building map chunks",
        note="First-time setup in progress.",
        layout_scale=1.25,
    )

    assert text_calls == [
        ("Preparing map", pytest.approx(130.0), pytest.approx(3.1875)),
        (
            "Building map chunks",
            pytest.approx(244.3125),
            pytest.approx(3.1875),
        ),
        (
            "First-time setup in progress.",
            pytest.approx(340.0),
            pytest.approx(2.425),
        ),
    ]


def test_progress_bar_fill_bounds_cover_determinate_and_indeterminate_states():
    assert ImportProgressPanel._progress_bar_fill_bounds(100.0, 400.0, 0.0, 0.5) == ()
    assert ImportProgressPanel._progress_bar_fill_bounds(100.0, 400.0, 0.25, 0.5) == (
        (100.0, 175.0),
    )
    assert ImportProgressPanel._progress_bar_fill_bounds(100.0, 400.0, 2.0, 0.5) == (
        (100.0, 400.0),
    )
    indeterminate = ImportProgressPanel._progress_bar_fill_bounds(
        100.0, 400.0, None, 0.5
    )
    assert len(indeterminate) == 1
    assert indeterminate[0][1] - indeterminate[0][0] == pytest.approx(84.0)


def test_import_stage_change_keeps_the_current_progress_fraction():
    panel = object.__new__(ImportProgressPanel)
    panel._display_fraction = 0.64
    panel._progress_token = ("cave.obj", False)

    panel._begin_progress_run(
        map_name="cave.obj",
        indeterminate=False,
        fraction=0.65,
    )

    assert panel._display_fraction == pytest.approx(0.64)
    assert panel._progress_token == ("cave.obj", False)


def test_explicit_opening_session_does_not_reset_at_the_import_streaming_handoff():
    panel = object.__new__(ImportProgressPanel)
    panel._display_fraction = 0.90
    panel._progress_token = ("map-opening", 7)

    panel._begin_progress_run(
        map_name="cave.obj",
        indeterminate=False,
        fraction=0.0,
        progress_session_id=7,
    )

    assert panel._display_fraction == pytest.approx(0.90)
    assert panel._progress_token == ("map-opening", 7)


def test_new_opening_session_resets_the_panel_after_a_completed_map():
    panel = object.__new__(ImportProgressPanel)
    panel._display_fraction = 1.0
    panel._progress_token = ("map-opening", 7)

    panel._begin_progress_run(
        map_name="next-cave.obj",
        indeterminate=False,
        fraction=0.0,
        progress_session_id=8,
    )

    assert panel._display_fraction == 0.0
    assert panel._progress_token == ("map-opening", 8)


def test_import_progress_uses_shared_routine_layout_tokens():
    assert ImportProgressPanel.PROGRESS_BAR_WIDTH == (
        import_progress_panel.ROUTINE_PROGRESS_BAR_WIDTH
    )
    assert ImportProgressPanel.PROGRESS_BAR_HEIGHT == (
        import_progress_panel.ROUTINE_PROGRESS_BAR_HEIGHT
    )
    assert ImportProgressPanel.STAGE_TEXT_SIZE == (
        import_progress_panel.OPENGL_PROGRESS_LABEL_TEXT_SIZE
    )
    assert import_progress_panel.ROUTINE_PROGRESS_TITLE_TO_BAR_GAP == 40.0
    assert import_progress_panel.ROUTINE_PROGRESS_BAR_TO_DESCRIPTION_GAP == 30.0


def test_import_progress_note_does_not_move_the_stage_or_bar(monkeypatch):
    panel = object.__new__(ImportProgressPanel)
    monkeypatch.setattr(
        import_progress_panel.bitmap_font,
        "text_bounds_px",
        lambda text, pixel_size: (
            0.0,
            0.0,
            len(text) * pixel_size,
            pixel_size * (2.0 if text.startswith("First-time") else 1.0),
        ),
    )

    without_note = panel._routine_progress_layout(
        center_x=400.0,
        bar_center_y=300.0,
        window_width=800.0,
        stage="Opening cave…",
        note="",
        layout_scale=1.0,
    )
    with_note = panel._routine_progress_layout(
        center_x=400.0,
        bar_center_y=300.0,
        window_width=800.0,
        stage="Scanning map…",
        note="First-time setup in progress.",
        layout_scale=1.0,
    )

    assert with_note.title_top == without_note.title_top
    assert with_note.title_bottom == without_note.title_bottom
    assert with_note.bar_left == without_note.bar_left
    assert with_note.bar_top == without_note.bar_top == 298.0
    assert with_note.bar_right == without_note.bar_right
    assert with_note.bar_bottom == without_note.bar_bottom == 302.0
    assert with_note.description_top == 332.0
    assert with_note.description_bottom == pytest.approx(335.88)


def test_import_progress_uses_the_shared_void_background():
    assert ImportProgressPanel._BACKDROP_RGBA == (
        *import_progress_panel.hex_color_rgb(
            import_progress_panel.DARK_THEME.background
        ),
        1.0,
    )


def test_import_render_uses_flat_bar_without_large_logo():
    source = import_progress_panel.ImportProgressPanel.render.__code__.co_names

    assert "_progress_bar_fill_bounds" in source
    assert "_add_bar_labels" in source
    assert "_render_logo" not in source


def test_hex_brand_color_conversion_matches_shader_values():
    assert import_progress_panel._hex_color_rgb("#FF8000") == (
        1.0,
        pytest.approx(128 / 255),
        0.0,
    )


def test_countdown_circle_uses_shared_progress_thickness_and_brand_colors():
    assets = import_progress_panel.resolve_branding_assets(environ={})
    panel = object.__new__(ImportProgressPanel)
    panel._progress_track_rgba = (*import_progress_panel._hex_color_rgb(
        assets.loading_progress.track_color
    ), 1.0)
    panel._progress_fill_rgba = (*import_progress_panel._hex_color_rgb(
        assets.loading_progress.fill_color
    ), 1.0)
    arcs = []
    panel._append_circle_arc = lambda *args: arcs.append(args[4:7])

    panel._append_progress_circle(
        [], lambda x, y: (x, y), 100.0, 100.0, 0.25, 0.5
    )

    assert ImportProgressPanel.COUNTDOWN_STROKE_WIDTH == (
        ImportProgressPanel.PROGRESS_BAR_HEIGHT
    )
    assert arcs[0][:2] == (0.0, 1.0)
    assert arcs[1][:2] == (0.0, 0.25)
    assert arcs[1][2][:3] == panel._progress_fill_rgba[:3]
    assert arcs[1][2][3] == 0.5


def test_circle_arc_builds_clockwise_geometry_from_twelve_oclock():
    panel = object.__new__(ImportProgressPanel)
    vertices = []

    panel._append_circle_arc(
        vertices,
        lambda x, y: (x, y),
        100.0,
        100.0,
        0.0,
        0.25,
        (1.0, 0.5, 0.0, 1.0),
    )

    assert vertices[0][:2] == pytest.approx((100.0, 14.0))
    assert vertices[-5][:2] == pytest.approx((186.0, 100.0))
