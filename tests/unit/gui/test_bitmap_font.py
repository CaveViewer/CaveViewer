"""Tests for CaveViewer's FreeType bitmap-font scaling helpers."""

from pathlib import Path

from caveviewer.gui import bitmap_font
from caveviewer.gui.platform.presentation import select_presentation_profile
from caveviewer.resources import inter_font_path


def test_explicit_presentation_profile_controls_overlay_font_selection(monkeypatch):
    profile = select_presentation_profile(platform_name="darwin")
    original_profile = bitmap_font._PRESENTATION_PROFILE
    monkeypatch.delenv("CAVEVIEWER_UI_FONT", raising=False)
    try:
        bitmap_font.set_presentation_profile(profile)

        assert bitmap_font._active_presentation_profile() is profile
        assert bitmap_font._font_candidates()[0] == str(inter_font_path("regular"))
    finally:
        bitmap_font.set_presentation_profile(original_profile)


def test_explicit_environment_font_precedes_bundled_inter(monkeypatch):
    bitmap_font.clear_runtime_style()
    monkeypatch.setenv("CAVEVIEWER_UI_FONT", "/custom/ui.ttf")

    assert bitmap_font._font_candidates()[0:2] == [
        "/custom/ui.ttf",
        str(inter_font_path("regular")),
    ]


def test_runtime_font_precedes_bundled_inter(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_UI_FONT", "/ignored/environment.ttf")
    try:
        bitmap_font.configure_runtime_style(
            font_path="/runtime/ui.ttf",
            antialiasing_mode="normal",
        )

        assert bitmap_font._font_candidates()[0:2] == [
            "/runtime/ui.ttf",
            str(inter_font_path("regular")),
        ]
    finally:
        bitmap_font.clear_runtime_style()


def test_font_resolution_skips_missing_and_corrupt_candidates(monkeypatch, tmp_path):
    missing = tmp_path / "missing.ttf"
    corrupt = tmp_path / "corrupt.ttf"
    valid = tmp_path / "valid.ttf"
    corrupt.write_bytes(b"corrupt")
    valid.write_bytes(b"valid")
    opened: list[Path] = []

    def open_face(candidate):
        path = Path(candidate)
        opened.append(path)
        if path == corrupt:
            raise RuntimeError("invalid font")
        return object()

    monkeypatch.setattr(
        bitmap_font,
        "_font_candidates",
        lambda: [str(missing), str(corrupt), str(valid)],
    )
    monkeypatch.setattr(bitmap_font.freetype, "Face", open_face)
    bitmap_font._resolve_font_path.cache_clear()
    try:
        assert bitmap_font._resolve_font_path() == str(valid)
        assert opened == [corrupt, valid]
    finally:
        bitmap_font._resolve_font_path.cache_clear()


def test_bundled_inter_rasterizes_representative_ui_unicode():
    original_profile = bitmap_font._PRESENTATION_PROFILE
    try:
        bitmap_font.clear_runtime_style()
        bitmap_font.set_presentation_profile(
            select_presentation_profile(platform_name="win32")
        )
        bitmap_font._resolve_font_path.cache_clear()

        assert bitmap_font._resolve_font_path() == str(inter_font_path("regular"))
        assert bitmap_font.text_width_px("Cañón Δ Ж", 2.0) > 0
    finally:
        bitmap_font.set_presentation_profile(original_profile)
        bitmap_font._resolve_font_path.cache_clear()


def test_raster_scale_increases_font_pixels_without_changing_logical_size():
    try:
        bitmap_font.set_text_scale(1.0)
        bitmap_font.set_raster_scale(1.0)
        normal_height = bitmap_font._font_pixel_height(2.0)

        bitmap_font.set_raster_scale(2.0)
        hidpi_height = bitmap_font._font_pixel_height(2.0)

        assert hidpi_height == normal_height * 2
    finally:
        bitmap_font.set_text_scale(1.0)
        bitmap_font.set_raster_scale(1.0)
