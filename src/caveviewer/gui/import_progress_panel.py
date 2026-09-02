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

import math
import time

import moderngl
import numpy as np

from caveviewer.branding import BrandingAssets, resolve_branding_assets
from caveviewer.gui import bitmap_font
from caveviewer.gui.loading_progress import (
    OPENGL_COUNTDOWN_DIAMETER,
    OPENGL_COUNTDOWN_RING_SEGMENTS,
    OPENGL_COUNTDOWN_STROKE_WIDTH,
    OPENGL_PROGRESS_BAR_HEIGHT,
    OPENGL_PROGRESS_BAR_WIDTH,
    OPENGL_PROGRESS_LABEL_TEXT_SIZE,
    circular_progress_ranges,
    hex_color_rgb,
    progress_layout_scale,
    progress_segments,
)


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

def _hex_color_rgb(color: str) -> tuple[float, float, float]:
    """Convert a validated six-digit brand color to shader RGB values."""
    return hex_color_rgb(color)


def _progress_label_layout_scale(window_size: tuple[int, int]) -> float:
    """Match the fullscreen controls prompt's responsive text scale."""
    return progress_layout_scale(window_size)


class ImportProgressPanel:
    COUNTDOWN_DIAMETER = OPENGL_COUNTDOWN_DIAMETER
    COUNTDOWN_STROKE_WIDTH = OPENGL_COUNTDOWN_STROKE_WIDTH
    PROGRESS_BAR_WIDTH = OPENGL_PROGRESS_BAR_WIDTH
    PROGRESS_BAR_HEIGHT = OPENGL_PROGRESS_BAR_HEIGHT
    INDETERMINATE_SEGMENT_FRACTION = 0.28
    # Match the established full-screen “Press Space to begin” prompt.
    TITLE_TEXT_SIZE = OPENGL_PROGRESS_LABEL_TEXT_SIZE
    STAGE_TEXT_SIZE = OPENGL_PROGRESS_LABEL_TEXT_SIZE
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

        progress_tokens = self._branding_assets.loading_progress
        self._progress_track_rgba = (
            *_hex_color_rgb(progress_tokens.track_color),
            1.0,
        )
        self._progress_fill_rgba = (
            *_hex_color_rgb(progress_tokens.fill_color),
            1.0,
        )
        self._display_fraction = 0.0
        self._progress_token = None

    def release(self) -> None:
        for attr in ("_vao", "_vbo", "program"):
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
        return progress_segments(
            left,
            right,
            progress,
            phase=phase,
            segment_fraction=cls.INDETERMINATE_SEGMENT_FRACTION,
        )

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

    def _append_circle_arc(
        self,
        verts: list[tuple[float, ...]],
        px_to_ndc,
        center_x: float,
        center_y: float,
        start: float,
        end: float,
        rgba: tuple[float, float, float, float],
    ) -> None:
        """Append a clockwise annular arc starting at twelve o'clock."""
        span = max(0.0, end - start)
        steps = max(1, math.ceil(span * OPENGL_COUNTDOWN_RING_SEGMENTS))
        outer_radius = self.COUNTDOWN_DIAMETER / 2.0
        inner_radius = outer_radius - self.COUNTDOWN_STROKE_WIDTH
        for index in range(steps):
            first = start + span * index / steps
            second = start + span * (index + 1) / steps
            angle0 = -math.pi / 2.0 + first * math.tau
            angle1 = -math.pi / 2.0 + second * math.tau
            outer0 = px_to_ndc(
                center_x + math.cos(angle0) * outer_radius,
                center_y + math.sin(angle0) * outer_radius,
            )
            outer1 = px_to_ndc(
                center_x + math.cos(angle1) * outer_radius,
                center_y + math.sin(angle1) * outer_radius,
            )
            inner0 = px_to_ndc(
                center_x + math.cos(angle0) * inner_radius,
                center_y + math.sin(angle0) * inner_radius,
            )
            inner1 = px_to_ndc(
                center_x + math.cos(angle1) * inner_radius,
                center_y + math.sin(angle1) * inner_radius,
            )
            for x, y in (outer0, outer1, inner1, outer0, inner1, inner0):
                verts.append((x, y, *rgba))

    def _append_progress_circle(
        self,
        verts: list[tuple[float, ...]],
        px_to_ndc,
        center_x: float,
        center_y: float,
        progress: float | None,
        alpha: float,
    ) -> None:
        """Append the standard track plus determinate or moving fill arc."""
        track = (*self._progress_track_rgba[:3], self._progress_track_rgba[3] * alpha)
        fill = (*self._progress_fill_rgba[:3], self._progress_fill_rgba[3] * alpha)
        self._append_circle_arc(verts, px_to_ndc, center_x, center_y, 0.0, 1.0, track)
        for start, end in circular_progress_ranges(
            progress,
            phase=(time.perf_counter() * 0.72) % 1.0,
            segment_fraction=self.INDETERMINATE_SEGMENT_FRACTION,
        ):
            self._append_circle_arc(
                verts, px_to_ndc, center_x, center_y, start, end, fill
            )

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
        """Render the standard progress circle, labels, and countdown."""
        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)

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

        self._append_progress_circle(
            verts, px_to_ndc, center_x, center_y, progress, alpha
        )

        self._add_circle_labels(
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
        r, g, b = self._progress_fill_rgba[:3]
        for glyph in bitmap_font.iter_text_pixels(text, origin_x, origin_y, pixel_size):
            px0, py0, px1, py1 = glyph[0], glyph[1], glyph[2], glyph[3]
            glyph_alpha = glyph[4] if len(glyph) > 4 else 1.0
            add_quad_px(px0, py0, px1, py1, (r, g, b, alpha * glyph_alpha))

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

    def draw_circle_label(
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
        """Render the standard progress circle with labels and center text."""
        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)

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

        self._append_progress_circle(
            verts, px_to_ndc, center_x, center_y, progress, alpha
        )

        self._add_circle_labels(
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
        r, g, b = self._progress_fill_rgba[:3]
        for glyph in bitmap_font.iter_text_pixels(label, origin_x, origin_y, pixel_size):
            px0, py0, px1, py1 = glyph[0], glyph[1], glyph[2], glyph[3]
            glyph_alpha = glyph[4] if len(glyph) > 4 else 1.0
            add_quad_px(px0, py0, px1, py1, (r, g, b, alpha * glyph_alpha))

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

    def _add_circle_labels(
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
            title_y=center_y - (self.COUNTDOWN_DIAMETER / 2.0) - 42.0,
            stage=stage,
            stage_y=center_y + (self.COUNTDOWN_DIAMETER / 2.0) + 30.0,
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
