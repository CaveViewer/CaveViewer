"""Exercise raster assets and high-density progress rings for the Tk splash."""

from __future__ import annotations

from PIL import Image

from caveviewer.gui import splash_screen, splash_visuals
from caveviewer.resources import image_path


def test_bundled_launch_background_is_a_dark_cave_image():
    background_path = image_path("splash_ginnie_dark.jpg")

    assert background_path.is_file()
    with Image.open(background_path) as image:
        assert image.size == (2048, 1369)
        # A one-pixel LANCZOS reduction is a stable brightness summary that
        # keeps the cave detail decisively behind amber and white launch content.
        brightness = (
            sum(
                image.convert("RGB")
                .resize((1, 1), Image.Resampling.LANCZOS)
                .getpixel((0, 0))
            )
            / 3
        )
        assert brightness < 18


def test_launch_background_loader_keeps_the_flat_fallback_when_missing(monkeypatch):
    monkeypatch.setattr(splash_screen, "_SPLASH_BACKGROUND_PATH", None)

    assert splash_screen._load_launch_background_image() is None


def test_launch_background_fills_the_target_without_letterboxing():
    source = Image.new("RGB", (8, 4), "#17324a")

    fitted = splash_visuals.fit_splash_background(source, size=(5, 5))

    assert fitted.mode == "RGB"
    assert fitted.size == (5, 5)
    assert fitted.getpixel((0, 0)) == (23, 50, 74)
    assert fitted.getpixel((4, 4)) == (23, 50, 74)


def test_progress_ring_supersampling_covers_a_4k_250_percent_target():
    assert splash_visuals.progress_ring_supersample_factor(330) == 4
    assert splash_visuals.progress_ring_supersample_factor(4096) == 1

    ring = splash_visuals.render_progress_ring(
        image_size=330,
        ring_diameter=228,
        stroke_width=5,
        track_color="#50535c",
        fill_color="#e5a11f",
        start_degrees=90,
        extent_degrees=-100,
    )

    assert ring.mode == "RGBA"
    assert ring.size == (330, 330)


def test_progress_ring_downsampling_keeps_smooth_edge_alpha():
    ring = splash_visuals.render_progress_ring(
        image_size=48,
        ring_diameter=36,
        stroke_width=2,
        track_color="#50535c",
        fill_color="#e5a11f",
        start_degrees=90,
        extent_degrees=-120,
    )

    alpha_values = set(ring.getchannel("A").get_flattened_data())

    assert 0 in alpha_values
    assert 255 in alpha_values
    assert any(0 < alpha < 255 for alpha in alpha_values)


def test_vector_icon_downsampling_keeps_curves_and_diagonals_smooth():
    icon = splash_visuals.render_vector_icon(
        image_size=(48, 48),
        paths=(
            splash_visuals.VectorPath(
                points=((7, 36), (24, 12), (41, 36)),
                color="#e5a11f",
                width=2,
            ),
        ),
        arcs=(
            splash_visuals.VectorArc(
                center=(24, 24),
                radius=15,
                start_degrees=35,
                extent_degrees=270,
                color="#ffffff",
                width=2,
            ),
        ),
    )

    alpha_values = set(icon.getchannel("A").get_flattened_data())

    assert icon.mode == "RGBA"
    assert icon.size == (48, 48)
    assert 0 in alpha_values
    assert 255 in alpha_values
    assert any(0 < alpha < 255 for alpha in alpha_values)


def test_progress_ring_keeps_determinate_and_indeterminate_arc_lengths():
    options = {
        "image_size": 80,
        "ring_diameter": 60,
        "stroke_width": 3,
        "track_color": "#50535c",
        "fill_color": "#e5a11f",
        "start_degrees": 90,
    }
    empty = splash_visuals.render_progress_ring(**options, extent_degrees=0)
    indeterminate = splash_visuals.render_progress_ring(
        **options,
        extent_degrees=-100,
    )
    complete = splash_visuals.render_progress_ring(**options, extent_degrees=-360)

    def fill_pixel_count(image) -> int:
        return sum(
            red > 180 and green > 100 and blue < 80
            for red, green, blue, _alpha in image.get_flattened_data()
        )

    assert fill_pixel_count(indeterminate) > fill_pixel_count(empty)
    assert fill_pixel_count(complete) > fill_pixel_count(indeterminate) * 2
