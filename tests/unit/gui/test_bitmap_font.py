"""Tests for CaveViewer's FreeType bitmap-font scaling helpers."""

from caveviewer.gui import bitmap_font


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
