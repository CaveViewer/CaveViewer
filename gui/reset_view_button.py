"""
gui/reset_view_button.py

A simple button to reset the camera view to right-side-up (reset roll to 0).
Appears above the minimap in the bottom-left corner.
"""

from __future__ import annotations

import moderngl
import numpy as np

from gui import bitmap_font


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


class ResetViewButton:
    BUTTON_WIDTH = 110
    BUTTON_HEIGHT = 32
    
    def __init__(self, ctx: moderngl.Context):
        self.ctx = ctx
        self.program = ctx.program(vertex_shader=_VERT_SRC, fragment_shader=_FRAG_SRC)
        self._max_verts = 600
        self._vbo = ctx.buffer(reserve=self._max_verts * 6 * 4)
        self._vao = ctx.vertex_array(
            self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
        )
    
    def total_height(self) -> float:
        """Return the button's height for layout calculations."""
        return float(self.BUTTON_HEIGHT)
    
    def _px_to_ndc(self, x: float, y: float, window_size: tuple[int, int]) -> tuple[float, float]:
        """Convert pixel coordinates to normalized device coordinates."""
        w, h = window_size
        return (2.0 * x / w - 1.0, 1.0 - 2.0 * y / h)
    
    def _button_rect(self, anchor_x: float, anchor_y: float) -> tuple[float, float, float, float]:
        """Return the button's bounding box in pixel coordinates."""
        return (anchor_x, anchor_y, anchor_x + self.BUTTON_WIDTH, anchor_y + self.BUTTON_HEIGHT)
    
    def render(self, window_size: tuple[int, int], anchor_x: float, anchor_y: float) -> None:
        """Render the button."""
        verts = []
        
        def add_quad_px(x0, y0, x1, y1, rgba):
            (nx0, ny0) = self._px_to_ndc(x0, y0, window_size)
            (nx1, ny1) = self._px_to_ndc(x1, y1, window_size)
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
        
        x0, y0, x1, y1 = self._button_rect(anchor_x, anchor_y)
        
        # Button background
        bg_color = (0.36, 0.41, 0.50, 0.96)
        add_quad_px(x0, y0, x1, y1, bg_color)
        
        # Button border
        border_color = (0.55, 0.70, 0.95, 1.0)
        border = 2.0
        add_quad_px(x0, y0, x1, y0 + border, border_color)
        add_quad_px(x0, y1 - border, x1, y1, border_color)
        add_quad_px(x0, y0, x0 + border, y1, border_color)
        add_quad_px(x1 - border, y0, x1, y1, border_color)
        
        # Button text
        text = "Reset View"
        text_size = 2.0
        text_color = (0.95, 0.97, 1.0, 1.0)
        tbx0, tby0, tbx1, tby1 = bitmap_font.text_bounds_px(text, text_size)
        text_w = tbx1 - tbx0
        text_h = tby1 - tby0
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        add_text(text, cx - text_w / 2.0 - tbx0, cy - text_h / 2.0 - tby0, text_size, text_color)
        
        # Render to screen
        if verts:
            data = np.array(verts, dtype=np.float32)
            
            # Dynamically grow buffer if needed
            if data.nbytes > self._max_verts * 6 * 4:
                self._max_verts = max(self._max_verts * 2, len(verts))
                self._vbo = self.ctx.buffer(reserve=self._max_verts * 6 * 4)
                self._vao = self.ctx.vertex_array(
                    self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
                )
            
            self._vbo.write(data.tobytes())
            self._vao.render(moderngl.TRIANGLES, vertices=len(verts))
    
    def on_mouse_press(self, x: float, y: float, anchor_x: float, anchor_y: float) -> bool:
        """
        Check if the click landed on the button.
        Returns True if clicked, False otherwise.
        """
        bx0, by0, bx1, by1 = self._button_rect(anchor_x, anchor_y)
        return bx0 <= x <= bx1 and by0 <= y <= by1
