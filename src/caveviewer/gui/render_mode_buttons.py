"""OpenGL overlay buttons for viewer render and workflow actions.

Small buttons, stacked just below the headlamp brightness slider on
the right side of the screen:
  - "Mesh"    toggles wireframe display on/off (see the actual triangle
              edges/mesh density -- useful for inspecting scan quality).
  - "Texture" toggles whether the photo texture is sampled, or the surface
              renders as a plain lit gray (useful for inspecting pure
              geometry/shape without photo detail, and is a small free
              performance win since it skips texture sampling entirely).
  - "Help"    brings the controls reference screen back up (the same
              dimmed full-screen list shown while a map is loading),
              and hides it again on a second click. Unlike Mesh/Texture,
              this button is stateless on its own -- viewer_window.py
              checks ControlsOverlay.is_manual_mode to decide whether a
              click should show or hide it, rather than this module
              tracking a separate "is help showing" flag that could drift
              out of sync with the overlay's own actual state.
  - "Color"   opens/closes the background color picker panel (see
              caveviewer.gui.color_picker). Stateless here for the same reason as
              Help -- viewer_window.py checks ColorPicker.is_active.
  - "Open"    opens the folder-browse dialog to switch to a different
              map without closing the program. Always stateless/one-shot
              -- there's no "is open mode active" toggle state, a click
              just triggers viewer_window.py's map-switch flow once.
  - "Rec"     starts the recording countdown. Recording state
              lives in viewer_window.py because it owns the framebuffer
              and ffmpeg process lifecycle.

Mesh and Texture are independent toggles (not mutually exclusive), giving
four possible combined states:
    texture only        (default)            -- normal textured view
    texture + wireframe                       -- triangulation overlaid on the photo
    wireframe only                            -- pure geometry inspection
    neither (gray, no wireframe)               -- plain lit shape, no detail at all

Like LightSlider and Minimap, this owns its own tiny 2D shader pass and
geometry, independent of the main mesh rendering pipeline.
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


class RenderModeButtons:
    # Layout, in pixels. The vertical starting position is no longer
    # fixed here -- it's passed in explicitly (see _button_rect_px's
    # top_y parameter) by viewer_window.py, which is the one place that
    # knows about the brightness/render-distance controls stacked above
    # this button block and can correctly anchor the WHOLE right-side
    # column (controls + buttons together) from a single position,
    # currently the bottom-right corner -- see
    # CaveViewerWindow._right_column_layout().
    # Match StepperControl.total_width() so the button stack visually
    # aligns with the brightness/ambient/view-distance controls above.
    BUTTON_WIDTH = 120
    BUTTON_HEIGHT = 34
    BUTTON_GAP = 10
    MARGIN_RIGHT = 18
    GROUP_GAP = 22
    FIXED_TEXT_SCALE = 1.28

    def __init__(
        self,
        ctx: moderngl.Context,
        texture_enabled: bool = True,
        wireframe_enabled: bool = False,
        smooth_shading_enabled: bool = True,
        text_scale: float = 1.0,
        geometry_scale: float = 1.0,
    ):
        self.ctx = ctx
        self.program = ctx.program(vertex_shader=_VERT_SRC, fragment_shader=_FRAG_SRC)

        self.texture_enabled = texture_enabled
        self.wireframe_enabled = wireframe_enabled
        # ON = smooth (averaged) normals, OFF = flat (per-triangle)
        # normals. Defaults to True to match caveviewer.core.chunking.builder's "smooth"
        # default import shading -- so a freshly imported map's button
        # state reflects how it was actually imported.
        self.smooth_shading_enabled = smooth_shading_enabled

        # Sized generously for seven buttons, two section headers, and separator.
        self._max_verts = 5000
        self._vbo = ctx.buffer(reserve=self._max_verts * 6 * 4)
        self._vao = ctx.vertex_array(
            self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
        )
        # Bitmap text expands into thousands of vertices, so keep it resident
        # until a displayed state or layout input actually changes.
        self._render_cache_key: tuple | None = None
        self._render_cache_verts = 0
        self._text_scale = max(0.35, float(text_scale))
        self._geometry_scale = max(0.35, float(geometry_scale))

    def set_scale(self, *, text_scale: float, geometry_scale: float) -> None:
        """Update cached text/geometry scale without recreating GL resources."""
        next_text_scale = max(0.35, float(text_scale))
        next_geometry_scale = max(0.35, float(geometry_scale))
        if (
            next_text_scale == self._text_scale
            and next_geometry_scale == self._geometry_scale
        ):
            return
        self._text_scale = next_text_scale
        self._geometry_scale = next_geometry_scale
        self._render_cache_key = None

    # -- layout ---------------------------------------------------------------

    @classmethod
    def total_stack_height(cls, scale: float = 1.0) -> float:
        """Full height of the grouped 7-button stack, at a given
        scale factor -- used by viewer_window.py to figure out how much
        vertical room this whole block needs when laying out the
        bottom-anchored right-side column."""
        view_group_h = 3 * (cls.BUTTON_HEIGHT * scale) + 2 * (cls.BUTTON_GAP * scale)
        utility_group_h = 4 * (cls.BUTTON_HEIGHT * scale) + 3 * (cls.BUTTON_GAP * scale)
        return view_group_h + (cls.GROUP_GAP * scale) + utility_group_h

    def _group_layout(self, window_size: tuple[int, int], top_y: float) -> dict:
        """Compute grouped button/header geometry and a shared scale."""
        _w, h = window_size

        full_stack_height = self.total_stack_height(scale=self._geometry_scale)
        available_height = h - top_y - 10  # 10px bottom breathing room
        scale = self._geometry_scale
        if full_stack_height > available_height and available_height > 0:
            scale = max(0.35, self._geometry_scale * available_height / full_stack_height)

        button_h = self.BUTTON_HEIGHT * scale
        button_gap = self.BUTTON_GAP * scale
        group_gap = self.GROUP_GAP * scale

        view_buttons_top_y = top_y
        view_group_h = 3 * button_h + 2 * button_gap

        tools_buttons_top_y = view_buttons_top_y + view_group_h + group_gap

        return {
            "scale": scale,
            "button_h": button_h,
            "button_gap": button_gap,
            "view_buttons_top_y": view_buttons_top_y,
            "tools_buttons_top_y": tools_buttons_top_y,
        }

    def _button_rect_px(self, index: int, window_size: tuple[int, int], top_y: float,
                        right_inset: float | None = None) -> tuple[float, float, float, float]:
        """Returns (x0, y0, x1, y1) for button `index`
        (0=Mesh, 1=Texture, 2=Shade, 3=Open, 4=Help, 5=Color, 6=Rec).
        top_y is where the FIRST button (Mesh) starts -- passed in by the
        caller, which owns the overall column layout."""
        w, _h = window_size
        layout = self._group_layout(window_size, top_y)
        button_h = layout["button_h"]
        button_gap = layout["button_gap"]

        if right_inset is None:
            right_inset = self.MARGIN_RIGHT

        x1 = w - right_inset
        x0 = x1 - self.BUTTON_WIDTH * layout["scale"]
        if index <= 2:
            group_top = layout["view_buttons_top_y"]
            index_in_group = index
        else:
            group_top = layout["tools_buttons_top_y"]
            index_in_group = index - 3

        y0 = group_top + index_in_group * (button_h + button_gap)
        y1 = y0 + button_h
        return x0, y0, x1, y1

    def _mesh_button_rect(self, window_size, top_y, right_inset: float | None = None):
        return self._button_rect_px(0, window_size, top_y, right_inset)

    def _texture_button_rect(self, window_size, top_y, right_inset: float | None = None):
        return self._button_rect_px(1, window_size, top_y, right_inset)

    def _shade_button_rect(self, window_size, top_y, right_inset: float | None = None):
        return self._button_rect_px(2, window_size, top_y, right_inset)

    def _help_button_rect(self, window_size, top_y, right_inset: float | None = None):
        return self._button_rect_px(4, window_size, top_y, right_inset)

    def _color_button_rect(self, window_size, top_y, right_inset: float | None = None):
        return self._button_rect_px(5, window_size, top_y, right_inset)

    def _open_button_rect(self, window_size, top_y, right_inset: float | None = None):
        return self._button_rect_px(3, window_size, top_y, right_inset)

    def _record_button_rect(self, window_size, top_y, right_inset: float | None = None):
        return self._button_rect_px(6, window_size, top_y, right_inset)

    @staticmethod
    def _px_to_ndc(x: float, y: float, window_size: tuple[int, int]) -> tuple[float, float]:
        w, h = window_size
        nx = (x / w) * 2.0 - 1.0
        ny = 1.0 - (y / h) * 2.0
        return nx, ny

    # -- interaction ------------------------------------------------------------

    def hit_test_mesh(self, x: float, y: float, window_size: tuple[int, int], top_y: float,
                      right_inset: float | None = None) -> bool:
        x0, y0, x1, y1 = self._mesh_button_rect(window_size, top_y, right_inset)
        return x0 <= x <= x1 and y0 <= y <= y1

    def hit_test_texture(self, x: float, y: float, window_size: tuple[int, int], top_y: float,
                         right_inset: float | None = None) -> bool:
        x0, y0, x1, y1 = self._texture_button_rect(window_size, top_y, right_inset)
        return x0 <= x <= x1 and y0 <= y <= y1

    def hit_test_shade(self, x: float, y: float, window_size: tuple[int, int], top_y: float,
                       right_inset: float | None = None) -> bool:
        x0, y0, x1, y1 = self._shade_button_rect(window_size, top_y, right_inset)
        return x0 <= x <= x1 and y0 <= y <= y1

    def hit_test_help(self, x: float, y: float, window_size: tuple[int, int], top_y: float,
                      right_inset: float | None = None) -> bool:
        x0, y0, x1, y1 = self._help_button_rect(window_size, top_y, right_inset)
        return x0 <= x <= x1 and y0 <= y <= y1

    def hit_test_color(self, x: float, y: float, window_size: tuple[int, int], top_y: float,
                       right_inset: float | None = None) -> bool:
        x0, y0, x1, y1 = self._color_button_rect(window_size, top_y, right_inset)
        return x0 <= x <= x1 and y0 <= y <= y1

    def hit_test_open(self, x: float, y: float, window_size: tuple[int, int], top_y: float,
                      right_inset: float | None = None) -> bool:
        x0, y0, x1, y1 = self._open_button_rect(window_size, top_y, right_inset)
        return x0 <= x <= x1 and y0 <= y <= y1

    def hit_test_record(self, x: float, y: float, window_size: tuple[int, int], top_y: float,
                        right_inset: float | None = None) -> bool:
        x0, y0, x1, y1 = self._record_button_rect(window_size, top_y, right_inset)
        return x0 <= x <= x1 and y0 <= y <= y1

    def on_mouse_press(self, x: float, y: float, window_size: tuple[int, int], top_y: float,
                       right_inset: float | None = None) -> str | None:
        """
        Returns a string identifying which button was clicked ("mesh",
        "texture", "shade", "help", "color", "open", or "record"), or None if the
        click missed all seven -- the caller (viewer_window.py) acts on the
        result, since Help/Color/Open's actual behavior depends on state
        this module doesn't have access to (see the module docstring for
        why they're intentionally stateless here). top_y is where this
        button block starts -- see total_stack_height()'s docstring for
        why the caller, not this class, owns that position.
        """
        if self.hit_test_mesh(x, y, window_size, top_y, right_inset):
            self.wireframe_enabled = not self.wireframe_enabled
            return "mesh"
        if self.hit_test_texture(x, y, window_size, top_y, right_inset):
            self.texture_enabled = not self.texture_enabled
            return "texture"
        if self.hit_test_shade(x, y, window_size, top_y, right_inset):
            self.smooth_shading_enabled = not self.smooth_shading_enabled
            return "shade"
        if self.hit_test_help(x, y, window_size, top_y, right_inset):
            return "help"
        if self.hit_test_color(x, y, window_size, top_y, right_inset):
            return "color"
        if self.hit_test_open(x, y, window_size, top_y, right_inset):
            return "open"
        if self.hit_test_record(x, y, window_size, top_y, right_inset):
            return "record"
        return None

    # -- rendering --------------------------------------------------------------

    def render(self, window_size: tuple[int, int], top_y: float, help_active: bool = False,
               color_active: bool = False, recording_armed: bool = False,
               right_inset: float | None = None) -> None:
        cache_key = (
            tuple(window_size),
            float(top_y),
            bool(help_active),
            bool(color_active),
            bool(recording_armed),
            None if right_inset is None else float(right_inset),
            self.texture_enabled,
            self.wireframe_enabled,
            self.smooth_shading_enabled,
            self._text_scale,
            self._geometry_scale,
            bitmap_font.raster_scale(),
        )
        if cache_key != self._render_cache_key:
            self._rebuild_render_cache(
                cache_key,
                window_size,
                top_y,
                help_active,
                color_active,
                recording_armed,
                right_inset,
            )

        if self._render_cache_verts == 0:
            return

        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        self._vao.render(moderngl.TRIANGLES, vertices=self._render_cache_verts)
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    def _rebuild_render_cache(
        self,
        cache_key: tuple,
        window_size: tuple[int, int],
        top_y: float,
        help_active: bool,
        color_active: bool,
        recording_armed: bool,
        right_inset: float | None,
    ) -> None:
        """Rebuild GPU geometry after displayed state or layout changes."""
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

        # Pick ONE text pixel_size that fits the longer label ("TEXTURE"),
        # then use that same size for BOTH buttons. Previously each button
        # auto-fit its own label independently, so "MESH" (short) rendered
        # noticeably larger/differently-proportioned than "TEXTURE" (long)
        # sitting right next to it -- two adjacent buttons with
        # inconsistent text sizing is a large part of what reads as
        # unpolished. A single shared size keeps them visually matched.
        layout = self._group_layout(window_size, top_y)
        available_w = self.BUTTON_WIDTH * layout["scale"] - 16
        available_h = self.BUTTON_HEIGHT * layout["scale"] - 10
        nominal_pixel_size = 3.1
        shared_pixel_size = bitmap_font.pixel_size_at_text_scale(
            nominal_pixel_size,
            self.FIXED_TEXT_SCALE * self._text_scale,
        )
        while nominal_pixel_size > 0.5:
            w = bitmap_font.text_width_px("TEXTURE", shared_pixel_size)
            _bx0, _by0, _bx1, _by1 = bitmap_font.text_bounds_px("TEXTURE", shared_pixel_size)
            h = _by1 - _by0
            if w <= available_w and h <= available_h:
                break
            nominal_pixel_size -= 0.1
            shared_pixel_size = bitmap_font.pixel_size_at_text_scale(
                nominal_pixel_size,
                self.FIXED_TEXT_SCALE * self._text_scale,
            )

        def draw_toggle_button(rect, is_on: bool, label: str):
            x0, y0, x1, y1 = rect

            # Soft drop shadow: a slightly offset, darker, larger rect
            # behind the button gives a sense of depth/elevation instead
            # of a flat color block sitting directly on the 3D view --
            # drawn first so everything else layers on top of it.
            shadow_offset = 2
            add_quad_px(x0 + shadow_offset, y0 + shadow_offset,
                        x1 + shadow_offset, y1 + shadow_offset,
                        (0.0, 0.0, 0.0, 0.22))

            # Button face: warm amber when active, cool dark slate when
            # inactive -- higher contrast than before between the two
            # states, and a less muddy "off" color (slate-blue-gray reads
            # more deliberately "inactive" than a plain flat gray).
            if is_on:
                bg = (0.20, 0.55, 0.98, 1.0)
            else:
                bg = (0.34, 0.37, 0.43, 0.95)
            add_quad_px(x0, y0, x1, y1, bg)

            # A thin brighter strip along the top edge of the button face
            # simulates a subtle highlight/bevel, the cheapest way to make
            # a flat-shaded rectangle read as a slightly raised, tactile
            # button rather than a painted-on color swatch.
            highlight_h = 3
            if is_on:
                highlight_color = (0.60, 0.78, 1.0, 0.95)
            else:
                highlight_color = (0.56, 0.60, 0.68, 0.88)
            add_quad_px(x0, y0, x1, y0 + highlight_h, highlight_color)

            # Crisp outer border, thicker than before (1.5px was too thin
            # to read clearly as a button edge) -- brighter and thicker
            # when active so the "on" state is unmistakable even at a
            # glance from across the room.
            border = 2.5 if is_on else 2.0
            border_color = (0.98, 0.99, 1.0, 1.0) if is_on else (0.62, 0.67, 0.75, 1.0)
            add_quad_px(x0, y0, x1, y0 + border, border_color)
            add_quad_px(x0, y1 - border, x1, y1, border_color)
            add_quad_px(x0, y0, x0 + border, y1, border_color)
            add_quad_px(x1 - border, y0, x1, y1, border_color)

            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            text_color = (0.98, 0.99, 1.0, 1.0) if is_on else (0.92, 0.95, 0.99, 1.0)

            bx0, by0, bx1, by1 = bitmap_font.text_bounds_px(label, shared_pixel_size)
            text_w = bx1 - bx0
            text_h = by1 - by0
            origin_x = cx - text_w / 2.0 - bx0
            origin_y = cy - text_h / 2.0 - by0

            r, g, b, a = text_color
            for glyph in bitmap_font.iter_text_pixels(label, origin_x, origin_y, shared_pixel_size):
                px0, py0, px1, py1 = glyph[0], glyph[1], glyph[2], glyph[3]
                glyph_alpha = glyph[4] if len(glyph) > 4 else 1.0
                add_quad_px(px0, py0, px1, py1, (r, g, b, a * glyph_alpha))

        mesh_rect = self._mesh_button_rect(window_size, top_y, right_inset)

        draw_toggle_button(mesh_rect, self.wireframe_enabled, "MESH")
        draw_toggle_button(self._texture_button_rect(window_size, top_y, right_inset), self.texture_enabled, "TEXTURE")
        draw_toggle_button(self._shade_button_rect(window_size, top_y, right_inset), self.smooth_shading_enabled, "SHADE")
        draw_toggle_button(self._open_button_rect(window_size, top_y, right_inset), False, "OPEN")
        draw_toggle_button(self._help_button_rect(window_size, top_y, right_inset), help_active, "HELP")
        draw_toggle_button(self._color_button_rect(window_size, top_y, right_inset), color_active, "COLOR")
        draw_toggle_button(self._record_button_rect(window_size, top_y, right_inset), recording_armed, "REC")

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
