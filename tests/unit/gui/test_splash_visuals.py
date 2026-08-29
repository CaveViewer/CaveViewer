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


def test_progress_control_stop_square_is_centered_at_250_percent_scale():
    """Keep the download cancel square aligned with its 55px progress ring."""
    control = splash_visuals.render_progress_control(
        image_size=60,
        ring_diameter=55,
        stroke_width=5,
        track_color="#ffffff",
        fill_color=(0, 0, 0, 0),
        start_degrees=90,
        extent_degrees=0,
        center_glyph="stop",
        center_glyph_size=18,
        center_glyph_color="#e5a11f",
    )
    alpha = control.getchannel("A")
    pixels = control.load()

    def opaque_bounds_and_center(matches_color):
        points = [
            (x, y, pixels[x, y][3])
            for y in range(control.height)
            for x in range(control.width)
            if matches_color(*pixels[x, y])
        ]
        left = min(x for x, _y, _alpha in points)
        top = min(y for _x, y, _alpha in points)
        right = max(x for x, _y, _alpha in points) + 1
        bottom = max(y for _x, y, _alpha in points) + 1
        total_alpha = sum(alpha for _x, _y, alpha in points)
        return (
            (left, top, right, bottom),
            (
                sum(x * alpha for x, _y, alpha in points) / total_alpha,
                sum(y * alpha for _x, y, alpha in points) / total_alpha,
            ),
        )

    ring_bounds, ring_center = opaque_bounds_and_center(
        lambda red, green, blue, alpha: (
            red > 240 and green > 240 and blue > 240 and alpha >= 128
        )
    )
    stop_bounds, stop_center = opaque_bounds_and_center(
        lambda red, green, blue, alpha: (
            red > 180 and 100 < green < 220 and blue < 80 and alpha >= 128
        )
    )

    assert ring_bounds == (5, 5, 55, 55)
    assert stop_bounds == (21, 21, 39, 39)
    assert abs(ring_center[0] - stop_center[0]) < 0.2
    assert abs(ring_center[1] - stop_center[1]) < 0.2
    assert any(0 < value < 255 for value in alpha.get_flattened_data())


def test_progress_control_pause_bars_share_the_ring_center_at_250_percent_scale():
    """Keep cache-rebuild pause bars symmetric around the progress center."""
    control = splash_visuals.render_progress_control(
        image_size=60,
        ring_diameter=55,
        stroke_width=5,
        track_color=(0, 0, 0, 0),
        fill_color=(0, 0, 0, 0),
        start_degrees=90,
        extent_degrees=-100,
        center_glyph="pause",
        center_glyph_size=18,
        center_glyph_color="#e5a11f",
    )
    alpha = control.getchannel("A")
    opaque = alpha.point(lambda value: 255 if value >= 128 else 0)
    left, top, right, bottom = opaque.getbbox()

    assert (left + right) / 2 == 30
    assert (top + bottom) / 2 == 30
    assert alpha.getpixel((30, 30)) < 20
    assert alpha.getpixel((24, 30)) > 200
    assert alpha.getpixel((35, 30)) > 200
    assert any(0 < value < 255 for value in alpha.get_flattened_data())


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


def test_font_awesome_retry_icon_uses_an_inset_optical_diameter():
    icon = splash_visuals.render_retry_icon(
        image_size=(32, 32),
        glyph_diameter=18,
        color="#e5a11f",
    )
    alpha = icon.getchannel("A")
    opaque = alpha.point(lambda value: 255 if value >= 128 else 0)

    assert icon.mode == "RGBA"
    assert icon.size == (32, 32)
    # Font Awesome's circular glyph occupies its full view box. The 18px
    # source scaling intentionally leaves room inside the 32px action target.
    assert opaque.getbbox() == (7, 7, 25, 25)
    assert any(0 < value < 255 for value in alpha.get_flattened_data())


def test_font_awesome_retry_icon_retains_its_inset_proportions_at_high_dpi():
    icon = splash_visuals.render_retry_icon(
        image_size=(80, 80),
        glyph_diameter=45,
        color="#e5a11f",
    )
    alpha = icon.getchannel("A")
    opaque = alpha.point(lambda value: 255 if value >= 128 else 0)
    left, top, right, bottom = opaque.getbbox()

    assert 44 <= right - left <= 46
    assert 44 <= bottom - top <= 46
    assert abs((left + right) / 2 - 40) <= 1
    assert abs((top + bottom) / 2 - 40) <= 1
    assert any(0 < value < 255 for value in alpha.get_flattened_data())


def test_vector_arc_honors_both_coordinates_of_its_center():
    points = splash_visuals._arc_points(
        center=(14.0, 11.0),
        radius=8.0,
        start_degrees=0.0,
        extent_degrees=-315.0,
    )

    assert points[0] == (22.0, 11.0)
    assert round(points[-1][0], 3) == 19.657
    assert round(points[-1][1], 3) == 5.343


def test_vector_icon_places_an_offset_arc_at_its_declared_vertical_center():
    icon = splash_visuals.render_vector_icon(
        image_size=(28, 28),
        arcs=(
            splash_visuals.VectorArc(
                center=(14.0, 11.0),
                radius=5.0,
                start_degrees=0.0,
                extent_degrees=90.0,
                color="#e5a11f",
                width=2.0,
            ),
        ),
    )

    alpha = icon.getchannel("A")

    assert alpha.getpixel((19, 11)) > 200
    assert alpha.getpixel((19, 14)) < 20


def test_sampled_vector_arc_uses_only_endpoint_caps():
    class _RecordingDrawer:
        def __init__(self) -> None:
            self.line_calls = []
            self.ellipse_calls = []

        def line(self, points, **options) -> None:
            self.line_calls.append((points, options))

        def ellipse(self, bounds, **options) -> None:
            self.ellipse_calls.append((bounds, options))

    drawer = _RecordingDrawer()
    splash_visuals._draw_rounded_path(
        drawer,
        ((2.0, 2.0), (8.0, 4.0), (12.0, 10.0)),
        color="#e5a11f",
        width=4,
        round_vertices=False,
    )

    assert len(drawer.line_calls) == 1
    assert len(drawer.ellipse_calls) == 2


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
