"""OpenGL text readout for live viewer performance statistics.

A small text readout showing live FPS and chunk-loading stats (loaded /
pending), positioned directly above the minimap panel in the bottom-left
corner. This is the same information the console already prints every
couple seconds (see viewer_window.py's periodic "[caveviewer] X fps |
chunks loaded=..." line), surfaced on-screen too -- it used to live next
to the old render-distance slider before that was replaced with a
plain +/- stepper button (which has no natural place to attach a readout
the way a slider's track did), so it's now its own small standalone
overlay instead.

Updates every frame using a smoothed rolling average (the same
_frame_time_history the spike-detector already maintains), rather than
only refreshing every 2 seconds like the console line -- a number that
visibly updates in real time is much more useful for judging the effect
of something you just changed (e.g. just bumped the render distance
stepper) than one that lags behind by up to 2 seconds.

Same drawing pattern as every other 2D overlay module here: a small
self-contained shader pass, independent of the main mesh rendering
pipeline.
"""

from __future__ import annotations

import moderngl
import numpy as np

from caveviewer.gui import bitmap_font


_VERT_SRC = """
#version 330
in vec2 in_pos;
in vec4 in_color;
out vec4 v_color;
void main() {
    gl_Position = vec4(in_pos, 0.0, 1.0);
    v_color = in_color;
}
"""

_FRAG_SRC = """
#version 330
in vec4 v_color;
out vec4 f_color;
void main() {
    f_color = v_color;
}
"""


class StatsReadout:
    TEXT_SIZE = 2.1
    LINE_GAP = 6
    PANEL_PAD_X = 14
    PANEL_PAD_Y = 10
    PANEL_BORDER_PX = 1.5
    PANEL_BG_RGBA = (0.015, 0.018, 0.024, 0.94)
    PANEL_BORDER_RGBA = (0.42, 0.54, 0.72, 0.70)

    def __init__(self, ctx: moderngl.Context):
        self.ctx = ctx
        self.program = ctx.program(vertex_shader=_VERT_SRC, fragment_shader=_FRAG_SRC)
        self._max_verts = 800
        self._vbo = ctx.buffer(reserve=self._max_verts * 6 * 4)
        self._vao = ctx.vertex_array(
            self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
        )
        self._render_cache_key: tuple | None = None
        self._render_cache_verts: int = 0

    def total_height(self) -> float:
        """Total vertical space this readout occupies, including panel padding."""
        line_h = bitmap_font.text_height_px(self.TEXT_SIZE)
        return line_h + self.PANEL_PAD_Y * 2

    def render(self, window_size: tuple[int, int], bottom_left_x: float, bottom_y: float,
               fps: float, chunks_loaded: int, chunks_pending: int,
               panel_width: float | None = None) -> None:
        """
        Draws one line of text -- "Frames/second: xx.x" -- with its panel's BOTTOM-left
        corner at (bottom_left_x, bottom_y), so
        the caller can position this readout's bottom edge flush against
        the top of the minimap panel without needing to know this
        class's exact text-height internals first.
        """
        fps_prefix = "Frames/second: "
        fps_value = f"{fps:.0f}"
        fps_text = f"{fps_prefix}{fps_value}"
        fps_color = (0.86, 0.88, 0.92, 1.0)
        text_w = bitmap_font.text_width_px(fps_text, self.TEXT_SIZE)
        prefix_w = bitmap_font.text_width_px(fps_prefix, self.TEXT_SIZE)
        line_h = bitmap_font.text_height_px(self.TEXT_SIZE)
        panel_w = max(float(panel_width or 0), text_w + self.PANEL_PAD_X * 2)
        panel_h = line_h + self.PANEL_PAD_Y * 2

        cache_key = (
            fps_text,
            prefix_w,
            fps_color,
            window_size,
            bottom_left_x,
            bottom_y,
            panel_w,
            panel_h,
            bitmap_font.raster_scale(),
        )
        if cache_key != self._render_cache_key:
            verts = []

            def add_quad_px(x0, y0, x1, y1, rgba):
                w, h = window_size
                nx0, ny0 = (x0 / w) * 2.0 - 1.0, 1.0 - (y0 / h) * 2.0
                nx1, ny1 = (x1 / w) * 2.0 - 1.0, 1.0 - (y1 / h) * 2.0
                top, bottom = max(ny0, ny1), min(ny0, ny1)
                left, right = min(nx0, nx1), max(nx0, nx1)
                quad = [
                    (left, bottom), (right, bottom), (right, top),
                    (left, bottom), (right, top), (left, top),
                ]
                for (x, y) in quad:
                    verts.append((x, y, *rgba))

            def add_text(text, x, y, pixel_size, rgba):
                r, g, b, a = rgba
                for glyph in bitmap_font.iter_text_pixels(text, x, y, pixel_size):
                    px0, py0, px1, py1 = glyph[0], glyph[1], glyph[2], glyph[3]
                    glyph_alpha = glyph[4] if len(glyph) > 4 else 1.0
                    add_quad_px(px0, py0, px1, py1, (r, g, b, a * glyph_alpha))

            def add_bold_text(text, x, y, pixel_size, rgba):
                add_text(text, x, y, pixel_size, rgba)
                add_text(text, x + 1.0, y, pixel_size, rgba)

            panel_x0 = bottom_left_x
            panel_y1 = bottom_y
            panel_x1 = panel_x0 + panel_w
            panel_y0 = panel_y1 - panel_h

            add_quad_px(panel_x0, panel_y0, panel_x1, panel_y1, self.PANEL_BG_RGBA)
            border = self.PANEL_BORDER_PX
            add_quad_px(panel_x0, panel_y0, panel_x1, panel_y0 + border, self.PANEL_BORDER_RGBA)
            add_quad_px(panel_x0, panel_y1 - border, panel_x1, panel_y1, self.PANEL_BORDER_RGBA)
            add_quad_px(panel_x0, panel_y0, panel_x0 + border, panel_y1, self.PANEL_BORDER_RGBA)
            add_quad_px(panel_x1 - border, panel_y0, panel_x1, panel_y1, self.PANEL_BORDER_RGBA)

            fps_line_y = panel_y0 + self.PANEL_PAD_Y
            text_x = panel_x0 + self.PANEL_PAD_X
            add_text(fps_prefix, text_x, fps_line_y, self.TEXT_SIZE, fps_color)
            add_bold_text(fps_value, text_x + prefix_w, fps_line_y, self.TEXT_SIZE, fps_color)

            if not verts:
                return
            data = np.array(verts, dtype=np.float32)
            if data.nbytes > self._max_verts * 6 * 4:
                self._vbo.release()
                self._max_verts = max(self._max_verts * 2, len(verts))
                self._vbo = self.ctx.buffer(reserve=self._max_verts * 6 * 4)
                self._vao = self.ctx.vertex_array(
                    self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
                )
            self._vbo.write(data.tobytes())
            self._render_cache_verts = len(verts)
            self._render_cache_key = cache_key

        if self._render_cache_verts == 0:
            return

        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        self._vao.render(moderngl.TRIANGLES, vertices=self._render_cache_verts)
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)
