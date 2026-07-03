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


class ImportProgressPanel:
    # Match splash-screen update and sample-download bars: thin dark track
    # with amber fill, no border.
    BAR_WIDTH = 300
    BAR_HEIGHT = 4

    # Colors mirror gui/splash_screen.py's update progress visuals.
    _TRACK_RGBA = (0.1098, 0.1098, 0.1412, 0.98)   # #1c1c24
    _FILL_RGBA = (0.7922, 0.6353, 0.2431, 1.00)    # #caa23e (_BUTTON_BG)
    _TITLE_RGBA = (0.9490, 0.8510, 0.5490, 1.0)    # #f2d98c (_TITLE_COLOR)
    _SUBTITLE_RGBA = (0.8000, 0.8039, 0.8392, 1.0) # #cccdd6 (_SUBTITLE_COLOR)
    _NOTE_RGBA = (0.6039, 0.6039, 0.6510, 1.0)     # #9a9aa6 (_INSTRUCTION_COLOR)

    def __init__(self, ctx: moderngl.Context):
        self.ctx = ctx
        self.program = ctx.program(vertex_shader=_VERT_SRC, fragment_shader=_FRAG_SRC)

        self._max_verts = 2000
        self._vbo = ctx.buffer(reserve=self._max_verts * 6 * 4)
        self._vao = ctx.vertex_array(
            self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
        )

        self._display_fraction = 0.0
        self._progress_token = None

    def reset_progress(self) -> None:
        """Reset monotonic progress state between distinct loading runs."""
        self._display_fraction = 0.0
        self._progress_token = None

    def _format_stage_label(self, stage: str) -> str:
        text = (stage or "").strip()
        if not text:
            return "Preparing data"
        text = text.replace("_", " ").replace("-", " ")
        text = " ".join(text.split())
        text = text.rstrip(" .,:;!?")
        return text[:1].upper() + text[1:]

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

        add_quad_px(0, 0, w, h, (0.04, 0.04, 0.05, 0.60))

        title_size = 3.5
        title_w = bitmap_font.text_width_px(title, title_size)
        title_y = h * 0.38
        add_text(title, (w - title_w) / 2.0, title_y, title_size, self._TITLE_RGBA)

        name_size = 1.9
        name_text = map_name.upper()
        name_w = bitmap_font.text_width_px(name_text, name_size)
        name_y = title_y + bitmap_font.text_height_px(title_size) + 16
        add_text(name_text, (w - name_w) / 2.0, name_y, name_size, self._SUBTITLE_RGBA)

        bar_x0 = (w - self.BAR_WIDTH) / 2.0
        bar_y0 = name_y + bitmap_font.text_height_px(name_size) + 30
        bar_x1 = bar_x0 + self.BAR_WIDTH
        bar_y1 = bar_y0 + self.BAR_HEIGHT

        add_quad_px(bar_x0, bar_y0, bar_x1, bar_y1, self._TRACK_RGBA)
        fraction_clamped = max(0.0, min(1.0, fraction))
        token = (map_name, title)
        if self._progress_token != token:
            self.reset_progress()
            self._progress_token = token

        # If this panel is reused for the same token right after a full
        # run (e.g. opening the same map again), allow a fresh start.
        if fraction_clamped <= 0.05 and self._display_fraction >= 0.95:
            self._display_fraction = 0.0

        self._display_fraction = max(self._display_fraction, fraction_clamped)
        fill_x1 = bar_x0 + self._display_fraction * self.BAR_WIDTH
        if fill_x1 > bar_x0:
            add_quad_px(bar_x0, bar_y0, fill_x1, bar_y1, self._FILL_RGBA)

        stage_size = 1.75
        stage_text = self._format_stage_label(stage)
        stage_w = bitmap_font.text_width_px(stage_text, stage_size)
        stage_y = bar_y1 + 12
        add_text(stage_text, (w - stage_w) / 2.0, stage_y, stage_size, self._SUBTITLE_RGBA)

        note_size = 1.4
        note_y = stage_y + bitmap_font.text_height_px(stage_size) + 20
        if note:
            note_w = bitmap_font.text_width_px(note, note_size)
            add_text(note, (w - note_w) / 2.0, note_y, note_size, self._NOTE_RGBA)

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
