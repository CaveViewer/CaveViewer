"""Regression tests for persistent OpenGL overlay geometry caches."""

from __future__ import annotations

import pytest

from caveviewer.gui import bitmap_font
from caveviewer.gui.render_mode_buttons import RenderModeButtons
from caveviewer.gui.stepper_control import StepperControl


class _TrackingBuffer:
    def __init__(self):
        self.write_count = 0
        self.last_write_size = 0

    def write(self, data: bytes) -> None:
        self.write_count += 1
        self.last_write_size = len(data)

    def release(self) -> None:
        pass


class _TrackingVertexArray:
    def __init__(self):
        self.render_count = 0
        self.last_vertex_count = 0

    def render(self, _mode, *, vertices: int) -> None:
        self.render_count += 1
        self.last_vertex_count = vertices


class _TrackingContext:
    def program(self, **_kwargs):
        return object()

    def buffer(self, *, reserve: int):
        return _TrackingBuffer()

    def vertex_array(self, *_args):
        return _TrackingVertexArray()

    def disable(self, _flag) -> None:
        pass

    def enable(self, _flag) -> None:
        pass


@pytest.fixture(autouse=True)
def deterministic_bitmap_font(monkeypatch):
    """Keep cache tests independent of host fonts and FreeType rendering."""

    monkeypatch.setattr(bitmap_font, "pixel_size_at_text_scale", lambda size, _scale: size)
    monkeypatch.setattr(
        bitmap_font,
        "text_bounds_px",
        lambda text, size: (0.0, 0.0, len(text) * size, size),
    )
    monkeypatch.setattr(bitmap_font, "text_width_px", lambda text, size: len(text) * size)
    monkeypatch.setattr(bitmap_font, "text_height_px", lambda size: size)

    def iter_text_pixels(text, origin_x, origin_y, _pixel_size):
        for index, _character in enumerate(text):
            x0 = origin_x + index
            yield x0, origin_y, x0 + 1.0, origin_y + 1.0, 1.0

    monkeypatch.setattr(bitmap_font, "iter_text_pixels", iter_text_pixels)


def test_stepper_reuses_geometry_until_value_or_layout_changes():
    control = StepperControl(
        _TrackingContext(),
        "BRIGHTNESS",
        initial_value=5,
        min_value=0,
        max_value=10,
    )

    control.render((1920, 1080), 1700, 300)
    buffer = control._vbo
    vertex_array = control._vao
    first_vertex_count = vertex_array.last_vertex_count

    control.render((1920, 1080), 1700, 300)

    assert buffer.write_count == 1
    assert vertex_array.render_count == 2
    assert vertex_array.last_vertex_count == first_vertex_count

    control.increment()
    control.render((1920, 1080), 1700, 300)
    assert buffer.write_count == 2

    control.render((1920, 1080), 1700, 300, label_above=False)
    assert buffer.write_count == 3

    control.render((1600, 900), 1400, 250)
    assert buffer.write_count == 4
    assert vertex_array.render_count == 5


def test_render_mode_buttons_reuse_geometry_until_state_or_layout_changes():
    buttons = RenderModeButtons(_TrackingContext())

    buttons.render((1920, 1080), 500, right_inset=24)
    buffer = buttons._vbo
    vertex_array = buttons._vao
    first_vertex_count = vertex_array.last_vertex_count

    buttons.render((1920, 1080), 500, right_inset=24)

    assert buffer.write_count == 1
    assert vertex_array.render_count == 2
    assert vertex_array.last_vertex_count == first_vertex_count

    buttons.texture_enabled = False
    buttons.render((1920, 1080), 500, right_inset=24)
    assert buffer.write_count == 2

    buttons.wireframe_enabled = True
    buttons.render((1920, 1080), 500, right_inset=24)
    assert buffer.write_count == 3

    buttons.smooth_shading_enabled = False
    buttons.render((1920, 1080), 500, right_inset=24)
    assert buffer.write_count == 4

    buttons.render((1920, 1080), 500, help_active=True, right_inset=24)
    assert buffer.write_count == 5

    buttons.render(
        (1920, 1080),
        500,
        help_active=True,
        color_active=True,
        right_inset=24,
    )
    assert buffer.write_count == 6

    buttons.render(
        (1920, 1080),
        500,
        help_active=True,
        color_active=True,
        recording_armed=True,
        right_inset=24,
    )
    assert buffer.write_count == 7

    buttons.render(
        (1600, 900),
        420,
        help_active=True,
        color_active=True,
        recording_armed=True,
        right_inset=20,
    )
    assert buffer.write_count == 8
    assert vertex_array.render_count == 9
