"""Tests for CaveViewer's FreeType bitmap-font scaling helpers."""

from caveviewer.gui import bitmap_font
from caveviewer.gui.platform.presentation import select_presentation_profile


def test_explicit_presentation_profile_controls_overlay_font_selection(monkeypatch):
    profile = select_presentation_profile(platform_name="darwin")
    original_profile = bitmap_font._PRESENTATION_PROFILE
    monkeypatch.delenv("CAVEVIEWER_UI_FONT", raising=False)
    try:
        bitmap_font.set_presentation_profile(profile)

        assert bitmap_font._active_presentation_profile() is profile
        assert bitmap_font._font_candidates()[0] == profile.font_candidates[0]
    finally:
        bitmap_font.set_presentation_profile(original_profile)


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
