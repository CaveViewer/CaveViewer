"""
gui/import_progress_panel.py

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
callback (see core/obj_parser.py / core/chunker.py). Each time that
callback fires, this panel redraws once and the frame is explicitly
swapped to the screen, so progress is genuinely visible as it happens,
just not as a continuously smooth animation -- an honest tradeoff against
the much larger work of moving the parser to a background thread.
"""

from __future__ import annotations

import math
import os
import sys

import moderngl
import numpy as np
from PIL import Image


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
void use_nearby_amber(vec2 offset, inout vec4 tex_color, inout bool amber_pixel) {
    vec4 sample_color = texture(u_texture, v_uv + offset);
    if (is_amber(sample_color) && sample_color.a > tex_color.a) {
        tex_color = sample_color;
        tex_color.a *= 0.88;
        amber_pixel = true;
    }
}
void main() {
    vec4 tex_color = texture(u_texture, v_uv);
    bool amber_pixel = is_amber(tex_color);
    if (!amber_pixel) {
        vec2 ring_expand = vec2(0.010, 0.010);
        use_nearby_amber(vec2( ring_expand.x, 0.0), tex_color, amber_pixel);
        use_nearby_amber(vec2(-ring_expand.x, 0.0), tex_color, amber_pixel);
        use_nearby_amber(vec2(0.0,  ring_expand.y), tex_color, amber_pixel);
        use_nearby_amber(vec2(0.0, -ring_expand.y), tex_color, amber_pixel);
        use_nearby_amber(vec2( ring_expand.x,  ring_expand.y), tex_color, amber_pixel);
        use_nearby_amber(vec2(-ring_expand.x,  ring_expand.y), tex_color, amber_pixel);
        use_nearby_amber(vec2( ring_expand.x, -ring_expand.y), tex_color, amber_pixel);
        use_nearby_amber(vec2(-ring_expand.x, -ring_expand.y), tex_color, amber_pixel);
    }
    if (amber_pixel) {
        vec2 centered = v_uv - vec2(0.5, 0.5);
        float angle = atan(centered.x, centered.y);
        if (angle < 0.0) {
            angle += 6.28318530718;
        }
        float pixel_progress = angle / 6.28318530718;
        if (pixel_progress > clamp(u_progress, 0.0, 1.0)) {
            float lum = dot(tex_color.rgb, vec3(0.299, 0.587, 0.114));
            tex_color.rgb = mix(vec3(lum), vec3(0.39, 0.40, 0.44), 0.72);
        }
    }
    f_color = vec4(tex_color.rgb, tex_color.a * u_alpha);
}
"""

def _resource_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_ASSETS_DIR = os.path.join(_resource_base_dir(), "gui", "assets")
_LOGO_PATH = os.path.join(_ASSETS_DIR, "app_mark_transparent.png")


class ImportProgressPanel:
    LOGO_SIZE = 132.0
    _BURST_PARTICLE_COUNT = 170

    _BACKDROP_RGBA = (0.0039, 0.0078, 0.0118, 0.88)  # near-black blue

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
        self._burst_particles = self._make_burst_particles()

    def _make_burst_particles(self) -> list[tuple[float, float, float, float, float, tuple[float, float, float]]]:
        particles = []
        golden_angle = math.pi * (3.0 - math.sqrt(5.0))
        for i in range(self._BURST_PARTICLE_COUNT):
            angle = i * golden_angle
            speed = 150.0 + ((i * 47) % 290)
            start_radius = 39.0 + ((i * 19) % 22)
            size = 0.35 + ((i * 13) % 18) / 32.0
            delay = ((i * 29) % 100) / 520.0
            if i % 5 == 0:
                color = (0.7922, 0.6353, 0.2431)
            elif i % 3 == 0:
                color = (0.4745, 0.8078, 0.9255)
            else:
                color = (0.3451, 0.3882, 0.4235)
            particles.append((angle, speed, start_radius, size, delay, color))
        return particles

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
               note: str = "First-time setup in progress. Next time, this map will open much faster.",
               completion_t: float | None = None) -> None:
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

        def add_dust_particles(cx, cy, t):
            if t <= 0.0:
                return
            for angle, speed, start_radius, size, delay, color in self._burst_particles:
                local_t = max(0.0, min(1.0, (t - delay) / max(1.0 - delay, 0.001)))
                if local_t <= 0.0:
                    continue
                ease = 1.0 - (1.0 - local_t) ** 3
                drift = start_radius + speed * ease
                swirl = math.sin(local_t * math.tau + angle * 0.37) * 18.0 * (1.0 - local_t)
                x = cx + math.cos(angle) * drift - math.sin(angle) * swirl
                y = cy + math.sin(angle) * drift + math.cos(angle) * swirl
                particle_size = size * (1.0 + local_t * 0.55)
                alpha = 0.62 * max(0.0, (1.0 - local_t) ** 1.55)
                r, g, b = color
                add_quad_px(
                    x - particle_size, y - particle_size,
                    x + particle_size, y + particle_size,
                    (r, g, b, alpha),
                )

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
        burst_t = None if completion_t is None else max(0.0, min(1.0, completion_t))
        if burst_t is not None:
            add_dust_particles(logo_cx, logo_cy, burst_t)

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
        self._render_logo(logo_cx, logo_cy, window_size, self._display_fraction, burst_t)
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    def _render_logo(
        self,
        center_x: float,
        center_y: float,
        window_size: tuple[int, int],
        progress: float,
        completion_t: float | None = None,
    ) -> None:
        if self._logo_texture is None:
            return

        burst_t = 0.0 if completion_t is None else max(0.0, min(1.0, completion_t))
        burst_ease = 1.0 - (1.0 - burst_t) ** 3
        size_px = self.LOGO_SIZE * (1.0 + burst_ease * 1.35)
        alpha = 1.0 if completion_t is None else max(0.0, 1.0 - burst_t * 1.2)
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
        self._logo_vao.render(moderngl.TRIANGLES, vertices=6)
