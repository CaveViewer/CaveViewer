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
        center_y=300.0,
        window_width=800.0,
        title="Preparing Map",
        stage="Building map chunksâ€¦",
        note="First-time setup in progress.",
    )

    assert text_calls == [
        ("Preparing Map", 164.0, ImportProgressPanel.TITLE_TEXT_SIZE),
        ("Building map chunksâ€¦", 240.0, ImportProgressPanel.STAGE_TEXT_SIZE),
        (
            "First-time setup in progress.",
            330.0,
            ImportProgressPanel.NOTE_TEXT_SIZE,
        ),
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
        center_y=300.0,
        window_width=800.0,
        title="Opening map",
        stage="Building map chunks",
        note="First-time setup in progress.",
        layout_scale=1.25,
    )

    assert text_calls == [
        ("Opening map", pytest.approx(130.0), pytest.approx(3.1875)),
        ("Building map chunks", pytest.approx(225.0), pytest.approx(3.1875)),
        (
            "First-time setup in progress.",
            pytest.approx(337.5),
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


def test_import_render_uses_flat_bar_without_large_logo():
    source = import_progress_panel.ImportProgressPanel.render.__code__.co_names

    assert "_progress_bar_fill_bounds" in source
    assert "_add_bar_labels" in source
    assert "_render_logo" not in source


def test_progress_ring_shader_uses_framebuffer_derivative_smoothing():
    source = import_progress_panel._LOGO_FRAG_SRC

    assert "fwidth(dist)" in source
    assert "fwidth(pixel_progress)" in source
    assert "fwidth(arc_offset)" in source
    assert "float edge = 0.005;" not in source
    assert "step(pixel_progress, progress)" not in source


def test_progress_ring_shader_uses_profile_colors_without_logo_color_filtering():
    source = import_progress_panel._LOGO_FRAG_SRC

    assert "uniform vec3 u_track_rgb;" in source
    assert "uniform vec3 u_fill_rgb;" in source
    assert "vec3 track_rgb =" not in source
    assert "vec3 fill_rgb =" not in source
    assert "is_amber" not in source
    assert "uniform sampler2D u_rim_mask;" in source
    assert "rim_mask_alpha" in source


def test_hex_brand_color_conversion_matches_shader_values():
    assert import_progress_panel._hex_color_rgb("#FF8000") == (
        1.0,
        pytest.approx(128 / 255),
        0.0,
    )


def test_failed_loading_mark_decode_falls_back_to_transparent_ring_texture(
    tmp_path,
):
    class FakeTexture:
        def __init__(self):
            self.released = False

        def release(self):
            self.released = True

        def build_mipmaps(self):
            return None

    class FakeContext:
        def __init__(self):
            self.calls = []

        def texture(self, size, components, data):
            self.calls.append((size, components, data))
            return FakeTexture()

    branding_assets = import_progress_panel.resolve_branding_assets(environ={})
    panel = object.__new__(ImportProgressPanel)
    panel.ctx = FakeContext()
    panel._branding_assets = import_progress_panel.BrandingAssets(
        profile_id=branding_assets.profile_id,
        application_mark=branding_assets.application_mark,
        about_mark=branding_assets.about_mark,
        loading_mark=tmp_path / "missing.png",
        loading_progress_mask=branding_assets.loading_progress_mask,
        windows_app_icon=branding_assets.windows_app_icon,
        macos_app_icon=branding_assets.macos_app_icon,
        linux_app_icon=branding_assets.linux_app_icon,
        loading_ring=branding_assets.loading_ring,
    )
    panel._logo_texture = None
    panel._logo_aspect = 2.0
    panel._logo_available = True

    panel._load_logo_texture()

    assert panel._logo_available is False
    assert panel._logo_aspect == 1.0
    assert panel._logo_texture is not None
    assert panel.ctx.calls[0] == ((1, 1), 4, b"\x00\x00\x00\x00")
    assert panel.ctx.calls[1][:2] == ((1024, 1024), 4)
    assert panel._rim_mask_available is True


def test_failed_loading_mark_upload_retries_with_ring_only_texture():
    class FakeTexture:
        def build_mipmaps(self):
            return None

    class FakeContext:
        def __init__(self):
            self.calls = []

        def texture(self, size, components, data):
            self.calls.append((size, components, len(data)))
            if len(self.calls) == 1:
                raise RuntimeError("simulated full-mark upload failure")
            return FakeTexture()

    panel = object.__new__(ImportProgressPanel)
    panel.ctx = FakeContext()
    panel._branding_assets = import_progress_panel.resolve_branding_assets(environ={})
    panel._logo_texture = None
    panel._logo_aspect = 1.0
    panel._logo_available = False

    panel._load_logo_texture()

    assert panel._logo_available is False
    assert panel._logo_texture is not None
    assert len(panel.ctx.calls) == 3
    assert panel.ctx.calls[1] == ((1, 1), 4, 4)
    assert panel.ctx.calls[2][:2] == ((1024, 1024), 4)
    assert panel._rim_mask_available is True
