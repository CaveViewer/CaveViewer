"""OpenGL progress and capture-feedback presentation for the viewer.

A simple full-screen progress panel, drawn while a newly-opened map is
being imported and chunked for the FIRST time (no cache yet) -- the same
one-time cost the very first launch always pays, just now also reachable
mid-session via the OPEN button (see viewer_window.py's
_handle_open_button_click).

The actual import work (OBJ/GLB parsing and cache construction) runs in a
spawned child process. The viewer process keeps rendering this panel and
drains progress events sent by that child, so resize/repaint/window-manager
events stay responsive while the cache is built. The same render-thread-owned
component also presents the short capture messages beneath their indicators
before and after video and dive-trace capture.
"""

from __future__ import annotations

import time

import moderngl
import numpy as np
from PIL import Image

from caveviewer.branding import BrandingAssets, resolve_branding_assets
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

_LOGO_VERT_SRC = """
#version 330
in vec2 in_pos;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    gl_Position = vec4(in_pos, 0.0, 1.0);
    v_uv = in_uv;
}
"""

_LOGO_FRAG_SRC = """
#version 330
uniform sampler2D u_texture;
uniform sampler2D u_rim_mask;
uniform float u_alpha;
uniform float u_progress;
uniform float u_indeterminate;
uniform float u_spinner_phase;
uniform float u_logo_alpha;
uniform vec3 u_track_rgb;
uniform vec3 u_fill_rgb;
uniform float u_use_rim_mask;
in vec2 v_uv;
out vec4 f_color;

void main() {
    vec4 tex_color = texture(u_texture, v_uv);
    float rim_mask_alpha = 0.0;
    if (u_use_rim_mask > 0.5) {
        rim_mask_alpha = texture(u_rim_mask, v_uv).a;
    }
    tex_color.a *= u_logo_alpha;

    vec2 centered = v_uv - vec2(0.5, 0.5);
    float dist = length(centered);
    float ring_inner = 0.398;
    float ring_outer = 0.442;
    // Derivative-sized smoothing keeps ring edges one-ish framebuffer pixel
    // wide at every display scale instead of using one fixed UV blur width.
    float edge = clamp(fwidth(dist), 0.00075, 0.010);
    float outer_mask = 1.0 - smoothstep(ring_outer - edge, ring_outer + edge, dist);
    float inner_mask = smoothstep(ring_inner - edge, ring_inner + edge, dist);
    float circular_ring_alpha = outer_mask * inner_mask;
    float ring_alpha = mix(
        circular_ring_alpha,
        rim_mask_alpha,
        step(0.5, u_use_rim_mask)
    );

    float angle = atan(centered.x, centered.y);
    if (angle < 0.0) {
        angle += 6.28318530718;
    }
    float pixel_progress = angle / 6.28318530718;
    float progress = clamp(u_progress, 0.0, 1.0);
    float progress_edge = clamp(fwidth(pixel_progress), 0.00075, 0.012);
    float fill_strength = 0.0;
    if (progress >= 0.999) {
        fill_strength = 1.0;
    } else if (progress > 0.0) {
        fill_strength = 1.0 - smoothstep(
            progress - progress_edge,
            progress + progress_edge,
            pixel_progress
        );
    }
    if (u_indeterminate > 0.5) {
        float phase = fract(u_spinner_phase);
        float arc_offset = fract(pixel_progress - phase + 1.0);
        float arc_span = 0.24 + 0.04 * sin(phase * 12.56637061436);
        float arc_edge = clamp(fwidth(arc_offset), 0.00075, 0.012);
        float arc_mask = 1.0 - smoothstep(
            arc_span - arc_edge,
            arc_span + arc_edge,
            arc_offset
        );
        float leading_taper = 1.0 - smoothstep(0.0, arc_span, arc_offset);
        fill_strength = arc_mask * mix(0.48, 1.0, leading_taper);
    }

    vec4 ring_color = vec4(
        mix(u_track_rgb, u_fill_rgb, fill_strength),
        ring_alpha
    );

    float out_alpha = tex_color.a + ring_color.a * (1.0 - tex_color.a);
    vec3 out_rgb = mix(tex_color.rgb, ring_color.rgb, ring_color.a);
    f_color = vec4(out_rgb, out_alpha * u_alpha);
}
"""


def _hex_color_rgb(color: str) -> tuple[float, float, float]:
    """Convert a validated six-digit brand color to shader RGB values."""
    return tuple(int(color[index : index + 2], 16) / 255.0 for index in (1, 3, 5))


def _progress_label_layout_scale(window_size: tuple[int, int]) -> float:
    """Match the fullscreen controls prompt's responsive text scale."""
    try:
        width, height = (max(1, int(value)) for value in window_size)
    except (TypeError, ValueError):
        return 1.0
    surface_ratio = min(width / 1536.0, height / 864.0)
    return max(1.0, min(1.32, surface_ratio))


class ImportProgressPanel:
    LOGO_SIZE = 172.0
    PROGRESS_BAR_WIDTH = 300.0
    PROGRESS_BAR_HEIGHT = 4.0
    INDETERMINATE_SEGMENT_FRACTION = 0.28
    # Match the established full-screen “Press Space to begin” prompt.
    TITLE_TEXT_SIZE = 2.55
    STAGE_TEXT_SIZE = 2.55
    NOTE_TEXT_SIZE = 1.94

    _BACKDROP_RGBA = (0.0039, 0.0078, 0.0118, 0.88)  # near-black blue
    _TITLE_TEXT_RGBA = (0.8980, 0.6314, 0.1216, 1.0)
    _STAGE_TEXT_RGBA = (0.8000, 0.8039, 0.8392, 1.0)
    _NOTE_TEXT_RGBA = (0.690, 0.720, 0.750, 0.92)

    def __init__(
        self,
        ctx: moderngl.Context,
        *,
        branding_assets: BrandingAssets | None = None,
    ):
        self.ctx = ctx
        self._branding_assets = branding_assets or resolve_branding_assets(environ={})
        self.program = ctx.program(vertex_shader=_VERT_SRC, fragment_shader=_FRAG_SRC)

        self._max_verts = 4000
        self._vbo = ctx.buffer(reserve=self._max_verts * 6 * 4)
        self._vao = ctx.vertex_array(
            self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
        )

        self.logo_program = ctx.program(vertex_shader=_LOGO_VERT_SRC, fragment_shader=_LOGO_FRAG_SRC)
        self.logo_program["u_track_rgb"].value = _hex_color_rgb(
            self._branding_assets.loading_ring.track_color
        )
        self.logo_program["u_fill_rgb"].value = _hex_color_rgb(
            self._branding_assets.loading_ring.fill_color
        )
        self._progress_track_rgba = (
            *_hex_color_rgb(self._branding_assets.loading_ring.track_color),
            1.0,
        )
        self._progress_fill_rgba = (
            *_hex_color_rgb(self._branding_assets.loading_ring.fill_color),
            1.0,
        )
        self._logo_vbo = ctx.buffer(reserve=6 * 4 * 4)
        self._logo_vao = ctx.vertex_array(
            self.logo_program, [(self._logo_vbo, "2f 2f", "in_pos", "in_uv")]
        )
        self._logo_texture = None
        self._rim_mask_texture = None
        self._logo_aspect = 1.0
        self._logo_available = False
        self._rim_mask_available = False
        self._load_logo_texture()

        self._display_fraction = 0.0
        self._progress_token = None

    def _load_logo_texture(self) -> None:
        texture = None
        try:
            with Image.open(self._branding_assets.loading_mark) as image:
                img = image.convert("RGBA")
            self._logo_aspect = img.size[0] / img.size[1]
            texture = self.ctx.texture(img.size, 4, img.tobytes())
            texture.build_mipmaps()
            self._logo_texture = texture
            self._logo_available = True
        except Exception:
            if texture is not None:
                try:
                    texture.release()
                except Exception:
                    pass
            self._logo_aspect = 1.0
            self._logo_available = False
            try:
                self._logo_texture = self.ctx.texture(
                    (1, 1), 4, b"\x00\x00\x00\x00"
                )
            except Exception:
                self._logo_texture = None

        mask_texture = None
        try:
            with Image.open(self._branding_assets.loading_progress_mask) as image:
                mask = image.convert("RGBA")
            mask_texture = self.ctx.texture(mask.size, 4, mask.tobytes())
            mask_texture.build_mipmaps()
            self._rim_mask_texture = mask_texture
            self._rim_mask_available = True
        except Exception:
            if mask_texture is not None:
                try:
                    mask_texture.release()
                except Exception:
                    pass
            self._rim_mask_available = False
            try:
                self._rim_mask_texture = self.ctx.texture(
                    (1, 1), 4, b"\x00\x00\x00\x00"
                )
            except Exception:
                self._rim_mask_texture = None

    def release(self) -> None:
        for attr in (
            "_logo_texture",
            "_rim_mask_texture",
            "_logo_vao",
            "_logo_vbo",
            "logo_program",
        ):
            obj = getattr(self, attr, None)
            if obj is not None and hasattr(obj, "release"):
                try:
                    obj.release()
                except Exception:
                    pass
            setattr(self, attr, None)

    def reset_progress(self) -> None:
        """Reset monotonic progress state between distinct loading runs."""
        self._display_fraction = 0.0
        self._progress_token = None

    def render(self, window_size: tuple[int, int], map_name: str, stage: str, fraction: float | None,
               title: str = "Preparing Map",
               note: str = "First-time setup in progress. Next time, this map will open much faster.") -> None:
        verts = []
        w, h = window_size

        def px_to_ndc(x, y):
            return (x / w) * 2.0 - 1.0, 1.0 - (y / h) * 2.0

        def add_quad_px(x0, y0, x1, y1, rgba):
            (nx0, ny0) = px_to_ndc(x0, y0)
            (nx1, ny1) = px_to_ndc(x1, y1)
            top, bottom = max(ny0, ny1), min(ny0, ny1)
            left, right = min(nx0, nx1), max(nx0, nx1)
            quad = [
                (left, bottom), (right, bottom), (right, top),
                (left, bottom), (right, top), (left, top),
            ]
            for (x, y) in quad:
                verts.append((x, y, *rgba))

        add_quad_px(0, 0, w, h, self._BACKDROP_RGBA)

        panel_h = 310.0
        panel_y0 = h * 0.33

        indeterminate = fraction is None
        fraction_clamped = 0.0 if indeterminate else max(0.0, min(1.0, fraction))
        token = (map_name, title, stage, indeterminate)
        if self._progress_token != token:
            self.reset_progress()
            self._progress_token = token

        # If this panel is reused for the same token right after a full
        # run (e.g. opening the same map again), allow a fresh start.
        if fraction_clamped <= 0.05 and self._display_fraction >= 0.95:
            self._display_fraction = 0.0

        if not indeterminate:
            self._display_fraction = max(self._display_fraction, fraction_clamped)

        bar_cx = w / 2.0
        # Keep the active stage where it was, but place progress after it in
        # reading order and before the explanatory note.
        bar_cy = panel_y0 + panel_h * 0.50 + 70.0
        bar_x0 = bar_cx - self.PROGRESS_BAR_WIDTH / 2.0
        bar_x1 = bar_cx + self.PROGRESS_BAR_WIDTH / 2.0
        bar_y0 = bar_cy - self.PROGRESS_BAR_HEIGHT / 2.0
        bar_y1 = bar_cy + self.PROGRESS_BAR_HEIGHT / 2.0
        add_quad_px(bar_x0, bar_y0, bar_x1, bar_y1, self._progress_track_rgba)
        for fill_x0, fill_x1 in self._progress_bar_fill_bounds(
            bar_x0,
            bar_x1,
            None if indeterminate else self._display_fraction,
            (time.perf_counter() * 0.72) % 1.0,
        ):
            add_quad_px(
                fill_x0,
                bar_y0,
                fill_x1,
                bar_y1,
                self._progress_fill_rgba,
            )
        self._add_bar_labels(
            add_quad_px=add_quad_px,
            center_x=bar_cx,
            center_y=bar_cy,
            window_width=w,
            title=title,
            stage=self._stage_label(stage),
            note=note,
            layout_scale=_progress_label_layout_scale(window_size),
        )

        data = np.array(verts, dtype=np.float32)
        if data.nbytes > self._max_verts * 6 * 4:
            self._vbo.release()
            self._max_verts = max(self._max_verts * 2, len(verts))
            self._vbo = self.ctx.buffer(reserve=self._max_verts * 6 * 4)
            self._vao = self.ctx.vertex_array(
                self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
            )

        self._vbo.write(data.tobytes())

        self.ctx.clear(0.04, 0.05, 0.07)
        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        self._vao.render(moderngl.TRIANGLES, vertices=len(verts))
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    @classmethod
    def _progress_bar_fill_bounds(
        cls,
        left: float,
        right: float,
        progress: float | None,
        phase: float,
    ) -> tuple[tuple[float, float], ...]:
        """Return determinate fill or a wrapping indeterminate segment."""
        width = max(0.0, right - left)
        if width == 0.0:
            return ()
        if progress is not None:
            fill_right = left + width * max(0.0, min(1.0, progress))
            return () if fill_right <= left else ((left, fill_right),)

        segment_width = width * cls.INDETERMINATE_SEGMENT_FRACTION
        start = left + (width + segment_width) * (phase % 1.0) - segment_width
        end = start + segment_width
        bounds = []
        if end > left and start < right:
            bounds.append((max(left, start), min(right, end)))
        return tuple(bounds)

    def _add_bar_labels(
        self,
        *,
        add_quad_px,
        center_x: float,
        center_y: float,
        window_width: float,
        title: str | None = None,
        stage: str | None = None,
        note: str | None = None,
        layout_scale: float = 1.0,
    ) -> None:
        """Append the import title, stage, and note around the flat progress bar."""
        self._add_labels(
            add_quad_px=add_quad_px,
            center_x=center_x,
            window_width=window_width,
            title=title,
            title_y=center_y - 136.0 * layout_scale,
            stage=stage,
            stage_y=center_y - 60.0 * layout_scale,
            note=note,
            note_y=center_y + 30.0 * layout_scale,
            layout_scale=layout_scale,
        )

    def _render_logo(
        self,
        center_x: float,
        center_y: float,
        window_size: tuple[int, int],
        progress: float | None,
        alpha: float = 1.0,
        logo_alpha: float = 1.0,
    ) -> None:
        mode = self._branding_assets.loading_ring.mode
        if self._logo_texture is None or mode == "text_only":
            return
        if mode == "ring_only" or not self._logo_available:
            logo_alpha = 0.0

        size_px = self.LOGO_SIZE
        if self._logo_aspect >= 1.0:
            half_w = size_px / 2.0
            half_h = (size_px / self._logo_aspect) / 2.0
        else:
            half_h = size_px / 2.0
            half_w = (size_px * self._logo_aspect) / 2.0

        w, h = window_size
        x0, x1 = center_x - half_w, center_x + half_w
        y0, y1 = center_y - half_h, center_y + half_h

        def px_to_ndc(x, y):
            return (x / w) * 2.0 - 1.0, 1.0 - (y / h) * 2.0

        vertices = [
            (*px_to_ndc(x0, y1), 0.0, 0.0),
            (*px_to_ndc(x1, y1), 1.0, 0.0),
            (*px_to_ndc(x1, y0), 1.0, 1.0),
            (*px_to_ndc(x0, y1), 0.0, 0.0),
            (*px_to_ndc(x1, y0), 1.0, 1.0),
            (*px_to_ndc(x0, y0), 0.0, 1.0),
        ]

        data = np.array(vertices, dtype=np.float32)
        self._logo_vbo.write(data.tobytes())
        self._logo_texture.use(location=0)
        if self._rim_mask_texture is not None:
            self._rim_mask_texture.use(location=1)
        self.logo_program["u_texture"].value = 0
        self.logo_program["u_rim_mask"].value = 1
        self.logo_program["u_use_rim_mask"].value = (
            1.0 if self._rim_mask_available else 0.0
        )
        self.logo_program["u_alpha"].value = alpha
        self.logo_program["u_progress"].value = (
            0.0 if progress is None else max(0.0, min(1.0, progress))
        )
        self.logo_program["u_indeterminate"].value = 1.0 if progress is None else 0.0
        self.logo_program["u_spinner_phase"].value = (
            time.perf_counter() * 0.72
        ) % 1.0
        self.logo_program["u_logo_alpha"].value = max(0.0, min(1.0, logo_alpha))
        self._logo_vao.render(moderngl.TRIANGLES, vertices=6)

    def draw_logo(
        self,
        center_x: float,
        center_y: float,
        window_size: tuple[int, int],
        progress: float,
        alpha: float = 1.0,
    ) -> None:
        """Public entry point: render just the logo+ring with its own GL state.
        Safe to call from other overlay modules (e.g. ControlsOverlay)."""
        if self._logo_texture is None:
            return
        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        self._render_logo(center_x, center_y, window_size, progress, alpha)
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    def draw_countdown_number(
        self,
        center_x: float,
        center_y: float,
        window_size: tuple[int, int],
        number: int,
        progress: float,
        alpha: float = 1.0,
        fixed_text_scale: float | None = None,
        title: str | None = None,
        stage: str | None = None,
        note: str | None = None,
    ) -> None:
        """Render the loading ring with import-style labels and a countdown."""
        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        self._render_logo(center_x, center_y, window_size, progress, alpha, logo_alpha=0.0)

        verts = []
        w, h = window_size

        def px_to_ndc(x, y):
            return (x / w) * 2.0 - 1.0, 1.0 - (y / h) * 2.0

        def add_quad_px(x0, y0, x1, y1, rgba):
            (nx0, ny0) = px_to_ndc(x0, y0)
            (nx1, ny1) = px_to_ndc(x1, y1)
            top, bottom = max(ny0, ny1), min(ny0, ny1)
            left, right = min(nx0, nx1), max(nx0, nx1)
            quad = [
                (left, bottom), (right, bottom), (right, top),
                (left, bottom), (right, top), (left, top),
            ]
            for (vx, vy) in quad:
                verts.append((vx, vy, *rgba))

        self._add_ring_labels(
            add_quad_px=add_quad_px,
            center_x=center_x,
            center_y=center_y,
            window_width=w,
            title=title,
            stage=stage,
            note=note,
            alpha=alpha,
            fixed_text_scale=fixed_text_scale,
        )

        text = str(max(0, min(9, int(number))))
        pixel_size = 9.0
        if fixed_text_scale is not None:
            pixel_size = bitmap_font.pixel_size_at_text_scale(pixel_size, fixed_text_scale)
        bounds = bitmap_font.text_bounds_px(text, pixel_size)
        text_w = bounds[2] - bounds[0]
        text_h = bounds[3] - bounds[1]
        origin_x = center_x - text_w / 2.0 - bounds[0]
        origin_y = center_y - text_h / 2.0 - bounds[1]
        r, g, b, a = (0.8980, 0.6314, 0.1216, alpha)
        for glyph in bitmap_font.iter_text_pixels(text, origin_x, origin_y, pixel_size):
            px0, py0, px1, py1 = glyph[0], glyph[1], glyph[2], glyph[3]
            glyph_alpha = glyph[4] if len(glyph) > 4 else 1.0
            add_quad_px(px0, py0, px1, py1, (r, g, b, a * glyph_alpha))

        if verts:
            data = np.array(verts, dtype=np.float32)
            if data.nbytes > self._max_verts * 6 * 4:
                self._vbo.release()
                self._max_verts = max(self._max_verts * 2, len(verts))
                self._vbo = self.ctx.buffer(reserve=self._max_verts * 6 * 4)
                self._vao = self.ctx.vertex_array(
                    self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
                )
            self._vbo.write(data.tobytes())
            self._vao.render(moderngl.TRIANGLES, vertices=len(verts))

        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    def draw_ring_label(
        self,
        center_x: float,
        center_y: float,
        window_size: tuple[int, int],
        label: str,
        progress: float | None = 1.0,
        pixel_size: float = 5.4,
        alpha: float = 1.0,
        fixed_text_scale: float | None = None,
        title: str | None = None,
        stage: str | None = None,
        note: str | None = None,
    ) -> None:
        """Render the loading ring with import-style labels and center text."""
        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        self._render_logo(center_x, center_y, window_size, progress, alpha, logo_alpha=0.0)

        verts = []
        w, h = window_size

        def px_to_ndc(x, y):
            return (x / w) * 2.0 - 1.0, 1.0 - (y / h) * 2.0

        def add_quad_px(x0, y0, x1, y1, rgba):
            (nx0, ny0) = px_to_ndc(x0, y0)
            (nx1, ny1) = px_to_ndc(x1, y1)
            top, bottom = max(ny0, ny1), min(ny0, ny1)
            left, right = min(nx0, nx1), max(nx0, nx1)
            quad = [
                (left, bottom), (right, bottom), (right, top),
                (left, bottom), (right, top), (left, top),
            ]
            for (vx, vy) in quad:
                verts.append((vx, vy, *rgba))

        self._add_ring_labels(
            add_quad_px=add_quad_px,
            center_x=center_x,
            center_y=center_y,
            window_width=w,
            title=title,
            stage=stage,
            note=note,
            alpha=alpha,
            fixed_text_scale=fixed_text_scale,
        )

        if fixed_text_scale is not None:
            pixel_size = bitmap_font.pixel_size_at_text_scale(pixel_size, fixed_text_scale)
        bounds = bitmap_font.text_bounds_px(label, pixel_size)
        text_w = bounds[2] - bounds[0]
        text_h = bounds[3] - bounds[1]
        origin_x = center_x - text_w / 2.0 - bounds[0]
        origin_y = center_y - text_h / 2.0 - bounds[1]
        r, g, b, a = (0.8980, 0.6314, 0.1216, alpha)
        for glyph in bitmap_font.iter_text_pixels(label, origin_x, origin_y, pixel_size):
            px0, py0, px1, py1 = glyph[0], glyph[1], glyph[2], glyph[3]
            glyph_alpha = glyph[4] if len(glyph) > 4 else 1.0
            add_quad_px(px0, py0, px1, py1, (r, g, b, a * glyph_alpha))

        if verts:
            data = np.array(verts, dtype=np.float32)
            if data.nbytes > self._max_verts * 6 * 4:
                self._vbo.release()
                self._max_verts = max(self._max_verts * 2, len(verts))
                self._vbo = self.ctx.buffer(reserve=self._max_verts * 6 * 4)
                self._vao = self.ctx.vertex_array(
                    self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
                )
            self._vbo.write(data.tobytes())
            self._vao.render(moderngl.TRIANGLES, vertices=len(verts))

        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    def _add_ring_labels(
        self,
        *,
        add_quad_px,
        center_x: float,
        center_y: float,
        window_width: float,
        title: str | None = None,
        stage: str | None = None,
        note: str | None = None,
        alpha: float = 1.0,
        fixed_text_scale: float | None = None,
    ) -> None:
        """Append labels using the shared title, stage, and note hierarchy."""
        self._add_labels(
            add_quad_px=add_quad_px,
            center_x=center_x,
            window_width=window_width,
            title=title,
            title_y=center_y - (self.LOGO_SIZE / 2.0) - 42.0,
            stage=stage,
            stage_y=center_y + (self.LOGO_SIZE / 2.0) + 30.0,
            note=note,
            alpha=alpha,
            fixed_text_scale=fixed_text_scale,
        )

    def _add_labels(
        self,
        *,
        add_quad_px,
        center_x: float,
        window_width: float,
        title: str | None,
        title_y: float,
        stage: str | None,
        stage_y: float,
        note: str | None,
        note_y: float | None = None,
        alpha: float = 1.0,
        fixed_text_scale: float | None = None,
        layout_scale: float = 1.0,
    ) -> None:
        """Append title, stage, and note at caller-selected vertical anchors."""
        def add_centered_text(
            text: str | None,
            y: float,
            pixel_size: float,
            rgba: tuple[float, float, float, float],
        ) -> float:
            text = " ".join(str(text or "").split())
            if not text:
                return 0.0
            pixel_size *= layout_scale
            if fixed_text_scale is not None:
                pixel_size = bitmap_font.pixel_size_at_text_scale(
                    pixel_size,
                    fixed_text_scale,
                )
            max_width = window_width - 96.0
            min_pixel_size = 1.20
            bounds = bitmap_font.text_bounds_px(text, pixel_size)
            text_width = bounds[2] - bounds[0]
            if text_width > max_width:
                pixel_size = max(min_pixel_size, pixel_size * max_width / text_width)
                bounds = bitmap_font.text_bounds_px(text, pixel_size)
                text_width = bounds[2] - bounds[0]
            text_height = bounds[3] - bounds[1]
            origin_x = center_x - text_width / 2.0 - bounds[0]
            origin_y = y - bounds[1]
            r, g, b, base_alpha = rgba
            for glyph in bitmap_font.iter_text_pixels(
                text,
                origin_x,
                origin_y,
                pixel_size,
            ):
                px0, py0, px1, py1 = glyph[0], glyph[1], glyph[2], glyph[3]
                glyph_alpha = glyph[4] if len(glyph) > 4 else 1.0
                add_quad_px(
                    px0,
                    py0,
                    px1,
                    py1,
                    (r, g, b, base_alpha * alpha * glyph_alpha),
                )
            return text_height

        add_centered_text(title, title_y, self.TITLE_TEXT_SIZE, self._TITLE_TEXT_RGBA)

        stage_height = add_centered_text(
            stage,
            stage_y,
            self.STAGE_TEXT_SIZE,
            self._STAGE_TEXT_RGBA,
        )
        if note_y is None:
            note_y = (
                stage_y
                if stage_height == 0.0
                else stage_y + stage_height + 22.0 * layout_scale
            )
        add_centered_text(note, note_y, self.NOTE_TEXT_SIZE, self._NOTE_TEXT_RGBA)

    def _stage_label(self, stage: str) -> str:
        normalized = " ".join((stage or "").strip().lower().split())
        labels = {
            "starting import": "Starting import…",
            "scanning file": "Scanning map…",
            "computing face centroids": "Analyzing geometry…",
            "grouping faces by cell": "Building spatial index…",
            "grouping chunk faces": "Building map chunks…",
            "writing chunk files": "Writing map cache…",
            "assembling render manifest": "Assembling render manifest…",
            "building guided dive identity": "Creating dive plan identity…",
            "writing manifest": "Finalizing map cache…",
            "loading cached map": "Loading cached map…",
            "resuming import": "Resuming import…",
            "continuing saved import": "Continuing saved import…",
            "pausing import": "Pausing import…",
            "resume point saved": "Resume point saved",
            "loading chunks": "Opening cave…",
            "opening cave": "Opening cave…",
            "thinking": "Looking for a path…",
            "done": "Finishing…",
        }
        if normalized in labels:
            return labels[normalized]
        if normalized:
            return normalized[:1].upper() + normalized[1:] + "…"
        return ""
