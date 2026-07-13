"""
caveviewer.gui.import_progress_panel

A simple full-screen progress panel, drawn while a newly-opened map is
being imported and chunked for the FIRST time (no cache yet) -- the same
one-time cost the very first launch always pays, just now also reachable
mid-session via the OPEN button (see viewer_window.py's
_handle_open_button_click).

Important limitation, stated plainly: the actual import work (OBJ
parsing, chunk-building) runs synchronously on the main thread, the same
as it always has for the very first launch of any map. That means the
normal render loop is paused while it runs -- this panel can't animate
smoothly DURING the heavy parsing work itself, only at the discrete
progress checkpoints the parser already reports via its progress_cb
callback (see caveviewer.core.obj_parser / caveviewer.core.chunker). Each time that
callback fires, this panel redraws once and the frame is explicitly
swapped to the screen, so progress is genuinely visible as it happens,
just not as a continuously smooth animation -- an honest tradeoff against
the much larger work of moving the parser to a background thread.
"""

from __future__ import annotations

import moderngl
import numpy as np
from PIL import Image

from caveviewer.gui import bitmap_font
from caveviewer.resources import image_path


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
uniform float u_alpha;
uniform float u_progress;
uniform float u_logo_alpha;
in vec2 v_uv;
out vec4 f_color;

bool is_amber(vec4 color) {
    return (
        color.a > 0.05 &&
        color.r > 0.45 &&
        color.g > 0.26 &&
        color.b < 0.22 &&
        color.r > color.b * 2.2 &&
        color.g > color.b * 1.6
    );
}

void main() {
    vec4 tex_color = texture(u_texture, v_uv);
    if (is_amber(tex_color)) {
        tex_color.a = 0.0;
    }
    tex_color.a *= u_logo_alpha;

    vec2 centered = v_uv - vec2(0.5, 0.5);
    float dist = length(centered);
    float ring_inner = 0.398;
    float ring_outer = 0.442;
    float edge = 0.005;
    float outer_mask = 1.0 - smoothstep(ring_outer - edge, ring_outer + edge, dist);
    float inner_mask = smoothstep(ring_inner - edge, ring_inner + edge, dist);
    float ring_alpha = outer_mask * inner_mask;

    float angle = atan(centered.x, centered.y);
    if (angle < 0.0) {
        angle += 6.28318530718;
    }
    float pixel_progress = angle / 6.28318530718;
    float fill_active = step(pixel_progress, clamp(u_progress, 0.0, 1.0));

    vec3 track_rgb = vec3(0.315, 0.325, 0.360);
    vec3 fill_rgb = vec3(0.8980, 0.6314, 0.1216);
    vec4 ring_color = vec4(mix(track_rgb, fill_rgb, fill_active), ring_alpha * mix(0.58, 1.0, fill_active));

    float out_alpha = tex_color.a + ring_color.a * (1.0 - tex_color.a);
    vec3 out_rgb = vec3(0.0);
    if (out_alpha > 0.0) {
        out_rgb = (
            tex_color.rgb * tex_color.a +
            ring_color.rgb * ring_color.a * (1.0 - tex_color.a)
        ) / out_alpha;
    }
    f_color = vec4(out_rgb, out_alpha * u_alpha);
}
"""

_LOGO_PATH = str(image_path("app_mark_transparent.png"))


class ImportProgressPanel:
    LOGO_SIZE = 172.0
    STAGE_TEXT_SIZE = 2.65

    _BACKDROP_RGBA = (0.0039, 0.0078, 0.0118, 0.88)  # near-black blue
    _STAGE_TEXT_RGBA = (0.8000, 0.8039, 0.8392, 1.0)

    def __init__(self, ctx: moderngl.Context):
        self.ctx = ctx
        self.program = ctx.program(vertex_shader=_VERT_SRC, fragment_shader=_FRAG_SRC)

        self._max_verts = 4000
        self._vbo = ctx.buffer(reserve=self._max_verts * 6 * 4)
        self._vao = ctx.vertex_array(
            self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
        )

        self.logo_program = ctx.program(vertex_shader=_LOGO_VERT_SRC, fragment_shader=_LOGO_FRAG_SRC)
        self._logo_vbo = ctx.buffer(reserve=6 * 4 * 4)
        self._logo_vao = ctx.vertex_array(
            self.logo_program, [(self._logo_vbo, "2f 2f", "in_pos", "in_uv")]
        )
        self._logo_texture = None
        self._logo_aspect = 1.0
        self._load_logo_texture()

        self._display_fraction = 0.0
        self._progress_token = None

    def _load_logo_texture(self) -> None:
        try:
            img = Image.open(_LOGO_PATH).convert("RGBA")
            self._logo_aspect = img.size[0] / img.size[1]
            self._logo_texture = self.ctx.texture(img.size, 4, img.tobytes())
            self._logo_texture.build_mipmaps()
        except Exception:
            self._logo_texture = None
            self._logo_aspect = 1.0

    def release(self) -> None:
        for attr in ("_logo_texture", "_logo_vao", "_logo_vbo", "logo_program"):
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

    def render(self, window_size: tuple[int, int], map_name: str, stage: str, fraction: float,
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

        def add_text(text, x, y, pixel_size, rgba):
            r, g, b, a = rgba
            for glyph in bitmap_font.iter_text_pixels(text, x, y, pixel_size):
                px0, py0, px1, py1 = glyph[0], glyph[1], glyph[2], glyph[3]
                glyph_alpha = glyph[4] if len(glyph) > 4 else 1.0
                add_quad_px(px0, py0, px1, py1, (r, g, b, a * glyph_alpha))

        add_quad_px(0, 0, w, h, self._BACKDROP_RGBA)

        panel_h = 310.0
        panel_y0 = h * 0.33

        fraction_clamped = max(0.0, min(1.0, fraction))
        token = (map_name, title, stage)
        if self._progress_token != token:
            self.reset_progress()
            self._progress_token = token

        # If this panel is reused for the same token right after a full
        # run (e.g. opening the same map again), allow a fresh start.
        if fraction_clamped <= 0.05 and self._display_fraction >= 0.95:
            self._display_fraction = 0.0

        self._display_fraction = max(self._display_fraction, fraction_clamped)

        logo_cx = w / 2.0
        logo_cy = panel_y0 + panel_h * 0.50
        stage_label = self._stage_label(stage)
        stage_size = self.STAGE_TEXT_SIZE
        stage_w = bitmap_font.text_width_px(stage_label, stage_size)
        stage_x = (w - stage_w) / 2.0
        stage_y = logo_cy + (self.LOGO_SIZE / 2.0) + 30.0
        add_text(stage_label, stage_x, stage_y, stage_size, self._STAGE_TEXT_RGBA)

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
        self._render_logo(logo_cx, logo_cy, window_size, self._display_fraction)
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    def _render_logo(
        self,
        center_x: float,
        center_y: float,
        window_size: tuple[int, int],
        progress: float,
        alpha: float = 1.0,
        logo_alpha: float = 1.0,
    ) -> None:
        if self._logo_texture is None:
            return

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
        self.logo_program["u_texture"].value = 0
        self.logo_program["u_alpha"].value = alpha
        self.logo_program["u_progress"].value = max(0.0, min(1.0, progress))
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
    ) -> None:
        """Render the loading ring with a centered countdown number."""
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
        progress: float = 1.0,
        pixel_size: float = 5.4,
        alpha: float = 1.0,
        fixed_text_scale: float | None = None,
    ) -> None:
        """Render the loading ring with a centered text label."""
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

    def _stage_label(self, stage: str) -> str:
        normalized = " ".join((stage or "").strip().lower().split())
        labels = {
            "starting import": "Starting import…",
            "scanning file": "Scanning map…",
            "computing face centroids": "Analyzing geometry…",
            "grouping faces by cell": "Building spatial index…",
            "grouping chunk faces": "Building map chunks…",
            "writing chunk files": "Writing map cache…",
            "writing manifest": "Finalizing map cache…",
            "loading cached map": "Loading cached map…",
            "loading chunks": "Opening cave…",
            "opening cave": "Opening cave…",
            "done": "Finishing…",
        }
        if normalized in labels:
            return labels[normalized]
        if normalized:
            return normalized[:1].upper() + normalized[1:] + "…"
        return "Working…"
