"""
caveviewer.gui.controls_overlay

A loading overlay that doubles as a controls reference diagram, shown:
  - Full-screen, right after the OpenGL window opens, while the first
    batch of chunks around the spawn point streams in.
  - As a smaller corner panel, briefly, after a minimap click teleports
    the camera somewhere new and that area's chunks need to stream in.

Both share the same content (full control list + UI feature summary) and
the same dismiss logic (auto-hides once enough chunks have loaded that the
person can actually see the cave they're standing in) -- they differ only
in how much of the screen they cover and how prominent they are while
visible.

Like the other overlay modules (LightSlider, Minimap, RenderModeButtons),
this draws its own vector shapes + bitmap-font text, independent of the
main mesh rendering pipeline.
"""

from __future__ import annotations

import sys
import time

import moderngl
import numpy as np

from caveviewer.gui import bitmap_font
from caveviewer.gui.platform.factory import get_platform_adapter
from caveviewer.core.logging_utils import get_logger


_LOG = get_logger("ControlsOverlay")


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

# Match splash_screen.py text palette exactly.
_SPLASH_TITLE_RGBA = (0.9490, 0.8510, 0.5490, 1.0)       # #f2d98c
_SPLASH_SUBTITLE_RGBA = (0.8000, 0.8039, 0.8392, 1.0)    # #cccdd6
_SPLASH_INSTRUCTION_RGBA = (0.6039, 0.6039, 0.6510, 1.0) # #9a9aa6
_SPLASH_PROGRESS_TRACK_RGBA = (0.1098, 0.1098, 0.1412, 0.98)  # #1c1c24
_SPLASH_PROGRESS_FILL_RGBA = (0.8980, 0.6314, 0.1216, 1.0)    # #e5a11f

_FULLSCREEN_BASE_WINDOW_SIZE = (1536, 864)
_FULLSCREEN_LAYOUT_SCALE_MAX = 1.32
_FULLSCREEN_SUBTITLE_TEXT_SIZE = 2.55


def _fullscreen_layout_scale(window_size: tuple[int, int]) -> float:
    """Return bounded scaling for fullscreen loading/help overlay text.

    The startup/help screen occupies the whole viewer, so it should grow on
    large XWayland/AppImage surfaces instead of staying at the compact
    1536x864 baseline.  Cap the multiplier so very large monitors do not turn
    the reference table into billboard-sized text.
    """
    try:
        width, height = window_size
        width = max(1, int(width))
        height = max(1, int(height))
    except Exception:
        width, height = _FULLSCREEN_BASE_WINDOW_SIZE

    base_width, base_height = _FULLSCREEN_BASE_WINDOW_SIZE
    size_scale = min(width / base_width, height / base_height)
    return max(1.0, min(_FULLSCREEN_LAYOUT_SCALE_MAX, size_scale))


def _get_platform_control_sections() -> list[tuple[str, list[tuple[str, str]]]]:
    """Generate platform-specific control sections for display."""
    adapter = get_platform_adapter()
    bookmark_modifier = adapter.bookmark_save_modifier()
    look_button = adapter.mouse_look_button_name()

    movement = [
        ("W A S D", "Move / strafe"),
        ("E", "Move up"),
        ("Q", "Move down"),
        ("Shift", "Speed boost"),
        ("Scroll", "Adjust fly speed"),
    ]

    look = []
    if look_button == "right":
        look.append(("Right click + mouse", "Look around"))
        look.append(("Option + left click + mouse", "Look around (alternative)"))
    else:  # left
        look.append(("Left click + mouse", "Look around"))

    look.extend([
        ("J L I K", "Look around"),
        ("Z X", "Barrel roll"),
    ])
    if sys.platform == "darwin":
        look.append(("Cmd + 0", "Reset view (level horizon)"))
    else:  # Windows/Linux
        look.append(("Ctrl + 0", "Reset view (level horizon)"))

    navigation = []
    if bookmark_modifier == "command":
        navigation.append(("Cmd + 1..9", "Save camera bookmark slot"))
    else:  # control
        navigation.append(("Ctrl + 1..9", "Save camera bookmark slot"))

    navigation.extend([
        ("1..9", "Recall camera bookmark slot"),
        ("Del + 1..9", "Delete bookmark slot"),
        ("Minimap click", "Jump to that spot"),
        ("Open button", "Switch to a different map"),
        ("Esc", "Quit"),
    ])

    recording = [
        ("REC button", "Start clean MP4 recording countdown"),
        ("Shift + R", "Stop or cancel recording"),
    ]

    return [
        ("Move", movement),
        ("Look", look),
        ("Navigate", navigation),
        ("Record", recording),
    ]


def _get_platform_control_rows() -> list[tuple[str, str]]:
    """Generate a flattened platform-specific control list."""
    rows = []
    for _, section_rows in _get_platform_control_sections():
        rows.extend(section_rows)
    return rows


class ControlsOverlay:
    # Minimum loaded chunks before the fullscreen startup overlay starts
    # dismissing.  Shared by the awaiting-begin path.
    MIN_CHUNKS_TO_DISMISS = 6

    # Separate (higher) threshold for the compact teleport panel: we want
    # enough geometry in the frustum that revealing the view looks populated,
    # not a sparse scattering of tiles around an otherwise black screen.
    # Kept at 12 rather than 20 so slower machines can reach it within the
    # extended timeout below without the safety ceiling firing first.
    MIN_CHUNKS_TO_DISMISS_PANEL = 12

    # How long to hold the overlay up at minimum, even if chunks finish
    # loading instantly (e.g. a very fast machine or a small nearby area
    # already cached) -- a flash-then-gone overlay is more confusing than
    # informative, so the fullscreen controls reference gets enough real
    # reading time while the compact teleport panel stays brief.
    MIN_DISPLAY_SECONDS_FULLSCREEN = 6.0
    MIN_DISPLAY_SECONDS_PANEL = 0.8

    # How long to keep the teleport panel visible after streaming completes
    # (pending == 0 and enough chunks loaded) so the screen doesn't flicker
    # as the panel vanishes the instant new geometry pops in.
    PANEL_LINGER_AFTER_LOAD = 0.6

    # How long the fade-out transition takes once dismiss conditions are met.
    FADE_OUT_SECONDS = 0.5

    # Hard ceiling on how long the panel variant can stay up regardless of
    # load progress.  Raised to 20 s so slower machines have time to decode
    # and upload MIN_CHUNKS_TO_DISMISS_PANEL chunks before the safety net
    # fires and reveals a sparsely populated cave.
    MAX_DISPLAY_SECONDS_PANEL = 20.0

    def __init__(self, ctx: moderngl.Context):
        self.ctx = ctx
        self.program = ctx.program(vertex_shader=_VERT_SRC, fragment_shader=_FRAG_SRC)

        self._max_verts = 6000
        self._vbo = ctx.buffer(reserve=self._max_verts * 6 * 4)
        self._vao = ctx.vertex_array(
            self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
        )

        self._active = False
        self._fullscreen = True
        self._manual_mode = False
        self._awaiting_begin = False
        self._ready_to_begin = False
        self._start_time = 0.0
        self._fade_start_time = None
        self._progress_fraction = 0.0
        self._panel_loaded_time: float | None = None
        self._logo_renderer = None  # set via set_logo_renderer() after construction
        
        # Generate platform-specific control rows.
        self._control_sections = _get_platform_control_sections()
        self._control_rows = [
            row for _, section_rows in self._control_sections for row in section_rows
        ]

    # -- lifecycle ------------------------------------------------------------

    def show_fullscreen(self) -> None:
        """Call once, right after the window opens / first chunks start streaming."""
        self._active = True
        self._fullscreen = True
        self._manual_mode = False
        self._awaiting_begin = True
        self._ready_to_begin = False
        self._start_time = time.perf_counter()
        self._fade_start_time = None
        self._progress_fraction = 0.0

    def set_logo_renderer(self, renderer) -> None:
        """Wire in an ImportProgressPanel so the teleport panel uses its
        logo+ring progress indicator instead of a plain bar."""
        self._logo_renderer = renderer

    def show_panel(self) -> None:
        """Call after a minimap teleport, while the new area's chunks stream in."""
        self._active = True
        self._fullscreen = False
        self._manual_mode = False
        self._awaiting_begin = False
        self._ready_to_begin = False
        self._start_time = time.perf_counter()
        self._fade_start_time = None
        self._panel_loaded_time = None
        self._progress_fraction = 0.0

    def show_help(self) -> None:
        """
        Call when the HELP button is clicked to bring the controls list
        back up manually, mid-flight. Unlike show_fullscreen/show_panel,
        this does NOT auto-dismiss based on chunk-loading progress --
        it's the fullscreen layout (dimmed background, centered text,
        same look as the startup screen), but stays open until
        hide_help() is called (i.e. the button is clicked again), since
        there's no "loading" actually happening to wait on here.
        """
        self._active = True
        self._fullscreen = True
        self._manual_mode = True
        self._awaiting_begin = False
        self._ready_to_begin = True
        self._fade_start_time = None

    def hide_help(self) -> None:
        """Call when the HELP button is clicked again to close the
        manually-toggled controls screen. Immediate -- no fade-out delay,
        since this is a direct response to a click rather than something
        dismissing itself once a background condition is met."""
        if self._manual_mode:
            self._active = False
            self._manual_mode = False
            self._awaiting_begin = False
            self._ready_to_begin = False
            self._fade_start_time = None

    def dismiss_begin_screen(self) -> None:
        """Dismiss the startup controls screen after the user presses Space."""
        if self._awaiting_begin and self._ready_to_begin:
            self._active = False
            self._awaiting_begin = False
            self._ready_to_begin = False
            self._fade_start_time = None

    @property
    def is_manual_mode(self) -> bool:
        """True while the HELP-triggered screen is showing -- lets the
        caller (viewer_window.py) know not to call show_panel()/
        show_fullscreen() over top of it, and lets the HELP button know
        whether a click should show or hide."""
        return self._manual_mode

    @property
    def is_waiting_for_begin(self) -> bool:
        return self._active and self._awaiting_begin

    @property
    def is_ready_to_begin(self) -> bool:
        return self._active and self._awaiting_begin and self._ready_to_begin

    def update(self, streaming_stats: dict) -> None:
        """
        Call once per frame with the StreamingWorld.stats() dict. Handles
        the auto-dismiss timing: once enough chunks are loaded, starts a
        short fade-out (after the minimum display time); once the fade
        finishes, the overlay deactivates entirely (render() becomes a
        no-op).

        The startup fullscreen screen remains visible until the map is
        ready, then until Space is pressed. The auto-dismiss logic below
        still applies to the compact panel variant:
          - Panel (teleport) does NOT require pending to fully reach zero
            -- a teleport can land somewhere needing many chunks loaded
            (especially if it's a totally new, previously-uncached area of
            a large map), and waiting for every single one to finish
            would make a supposedly-brief panel linger far longer than
            intended. It dismisses once a reasonable number have loaded,
            even if some are still streaming in the background -- chunks
            keep arriving after the panel is gone, same as they always do.

        The manually-toggled HELP screen (show_help/hide_help) ignores
        this method's auto-dismiss logic entirely -- it has no loading to
        wait on, so it just stays open until explicitly hidden.
        """
        if not self._active:
            return
        loaded = streaming_stats.get("loaded", 0)
        pending = streaming_stats.get("pending", 0)
        ready  = streaming_stats.get("ready",   0)
        wanted = max(1, int(streaming_stats.get("wanted", self.MIN_CHUNKS_TO_DISMISS)))
        chunks_needed = min(self.MIN_CHUNKS_TO_DISMISS, wanted)
        # Include ready (decoded-but-not-yet-uploaded) in the denominator so
        # the ring reflects all in-flight work.  Without this, when pending
        # briefly hits 0 while ready > 0, total becomes 0 and the ring
        # freezes -- noticeable on slower machines where decode is the
        # bottleneck and chunks pile up in the ready queue between frames.
        total = max(0, loaded + pending + ready)
        if total > 0:
            # Give partial credit so the ring moves as soon as background
            # decode starts (pending), not only once GPU uploads finish:
            #   pending  0.25  decode in progress
            #   ready    0.75  decode done, upload queued
            #   loaded   1.00  fully on GPU
            frac = max(0.0, min(1.0,
                (loaded + 0.75 * ready + 0.25 * pending) / float(total)
            ))
            # Keep progress monotonic so the bar doesn't jump backward
            # when pending work is reprioritized across frames.
            self._progress_fraction = max(self._progress_fraction, frac)

        if self._awaiting_begin and loaded >= chunks_needed and pending == 0:
            self._ready_to_begin = True

        if self._manual_mode or self._awaiting_begin:
            return

        now = time.perf_counter()
        elapsed = now - self._start_time
        min_display = self.MIN_DISPLAY_SECONDS_FULLSCREEN if self._fullscreen else self.MIN_DISPLAY_SECONDS_PANEL

        if self._fullscreen:
            enough_loaded = loaded >= chunks_needed and pending == 0
        else:
            # For the teleport panel, also wait for the GPU upload queue to
            # drain (ready == 0) before starting the linger countdown.
            # pending == 0 alone only means background decode is done; with
            # UPLOAD_CHUNKS_PER_FRAME = 1, a non-empty ready queue means chunks
            # are still uploading frame-by-frame and textures aren't on the GPU
            # yet -- starting the fade while that queue has items produces a
            # brief "untextured geometry" flash as the overlay reveals the view.
            if (self._panel_loaded_time is None
                    and loaded >= self.MIN_CHUNKS_TO_DISMISS_PANEL
                    and pending == 0
                    and ready == 0):
                self._panel_loaded_time = now
            enough_loaded = (
                (self._panel_loaded_time is not None
                 and (now - self._panel_loaded_time) >= self.PANEL_LINGER_AFTER_LOAD)
                or elapsed >= self.MAX_DISPLAY_SECONDS_PANEL
            )

        if self._fade_start_time is None:
            if enough_loaded and elapsed >= min_display:
                self._fade_start_time = now
        else:
            fade_elapsed = now - self._fade_start_time
            if fade_elapsed >= self.FADE_OUT_SECONDS:
                self._active = False
                self._fade_start_time = None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_fading(self) -> bool:
        """True while the fade-out animation is running (overlay still active
        but dismiss has been triggered).  Used by viewer_window to re-enable
        texture rendering before the dim overlay fully reveals the cave,
        so the cave is already textured when it becomes visible."""
        return self._active and self._fade_start_time is not None

    def _current_alpha_multiplier(self) -> float:
        """1.0 = fully opaque, fading down to 0.0 during the dismiss fade."""
        if self._fade_start_time is None:
            return 1.0
        fade_elapsed = time.perf_counter() - self._fade_start_time
        t = min(fade_elapsed / self.FADE_OUT_SECONDS, 1.0)
        return 1.0 - t

    # -- rendering --------------------------------------------------------------

    def render(self, window_size: tuple[int, int]) -> None:
        if not self._active:
            return

        alpha_mult = self._current_alpha_multiplier()
        if alpha_mult <= 0.0:
            return

        verts = []
        w, h = window_size

        def px_to_ndc(x, y):
            nx = (x / w) * 2.0 - 1.0
            ny = 1.0 - (y / h) * 2.0
            return nx, ny

        def add_quad_px(x0, y0, x1, y1, rgba):
            r, g, b, a = rgba
            a = a * alpha_mult
            (nx0, ny0) = px_to_ndc(x0, y0)
            (nx1, ny1) = px_to_ndc(x1, y1)
            top, bottom = max(ny0, ny1), min(ny0, ny1)
            left, right = min(nx0, nx1), max(nx0, nx1)
            quad = [
                (left, bottom), (right, bottom), (right, top),
                (left, bottom), (right, top), (left, top),
            ]
            for (vx, vy) in quad:
                verts.append((vx, vy, r, g, b, a))

        def add_text(text, x, y, pixel_size, rgba):
            r, g, b, a = rgba
            for glyph in bitmap_font.iter_text_pixels(text, x, y, pixel_size):
                px0, py0, px1, py1 = glyph[0], glyph[1], glyph[2], glyph[3]
                glyph_alpha = glyph[4] if len(glyph) > 4 else 1.0
                add_quad_px(px0, py0, px1, py1, (r, g, b, a * glyph_alpha))

        if self._fullscreen:
            self._build_fullscreen(add_quad_px, add_text, window_size)
        else:
            self._build_panel(add_quad_px, add_text, window_size)

        data = np.array(verts, dtype=np.float32)
        if data.nbytes > self._max_verts * 6 * 4:
            self._vbo.release()
            self._max_verts = max(self._max_verts * 2, len(verts))
            self._vbo = self.ctx.buffer(reserve=self._max_verts * 6 * 4)
            self._vao = self.ctx.vertex_array(
                self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
            )

        if verts:
            self._vbo.write(data.tobytes())

        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        if verts:
            self._vao.render(moderngl.TRIANGLES, vertices=len(verts))
        # For the compact teleport panel, draw the logo+ring centred on screen.
        if not self._fullscreen and self._logo_renderer is not None:
            self._logo_renderer.draw_logo(
                center_x=w / 2.0,
                center_y=h / 2.0,
                window_size=window_size,
                progress=self._progress_fraction,
                alpha=alpha_mult,
            )
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    # -- layout: full-screen variant --------------------------------------------

    def _build_fullscreen(self, add_quad_px, add_text, window_size):
        w, h = window_size
        layout_scale = _fullscreen_layout_scale(window_size)

        # Dim the 3D view heavily while the controls reference is shown.
        add_quad_px(0, 0, w, h, (0.001, 0.002, 0.005, 0.96))

        # Manual help has a title; startup help is intentionally lighter:
        # the map is already visually loading, so the only text needed
        # above the controls is the explicit begin prompt.
        if self._manual_mode:
            title = "Controls"
            subtitle = "Click anywhere on the screen to close"
        else:
            title = ""
            subtitle = "Press Space to begin" if self._ready_to_begin else "Loading map..."

        title_size = 4.3 * layout_scale
        title_y = h * 0.12
        if title:
            title_w = bitmap_font.text_width_px(title, title_size)
            title_x = (w - title_w) / 2.0
            add_text(title, title_x, title_y, title_size, _SPLASH_TITLE_RGBA)
            subtitle_y = title_y + bitmap_font.text_height_px(title_size) + 18 * layout_scale
        else:
            subtitle_y = title_y

        sub_size = _FULLSCREEN_SUBTITLE_TEXT_SIZE * layout_scale
        sub_y = subtitle_y
        if not self._manual_mode and subtitle == "Press Space to begin":
            bar_bottom_y = self._draw_begin_prompt(add_quad_px, add_text, w, sub_y, sub_size)
        else:
            sub_w = bitmap_font.text_width_px(subtitle, sub_size)
            sub_x = (w - sub_w) / 2.0
            add_text(subtitle, sub_x, sub_y, sub_size, _SPLASH_SUBTITLE_RGBA)
            bar_bottom_y = sub_y + bitmap_font.text_height_px(sub_size)
        if not self._manual_mode:
            bar_w = 300.0 * layout_scale
            bar_h = 4.0 * layout_scale
            bar_x0 = (w - bar_w) / 2.0
            bar_x1 = bar_x0 + bar_w
            bar_y0 = bar_bottom_y + 22.0 * layout_scale
            bar_y1 = bar_y0 + bar_h

            add_quad_px(bar_x0, bar_y0, bar_x1, bar_y1, _SPLASH_PROGRESS_TRACK_RGBA)
            fill_x1 = bar_x0 + self._progress_fraction * bar_w
            if fill_x1 > bar_x0:
                add_quad_px(bar_x0, bar_y0, fill_x1, bar_y1, _SPLASH_PROGRESS_FILL_RGBA)
            table_start_offset = 126.0 * layout_scale
        else:
            table_start_offset = 126.0 * layout_scale

        table_top_y = bar_bottom_y + table_start_offset
        self._draw_grouped_controls(
            add_quad_px,
            add_text,
            window_size=window_size,
            top_y=table_top_y,
            available_height=max(80.0, h - table_top_y - 20.0),
            layout_scale=layout_scale,
        )

        return None

    def _draw_begin_prompt(self, add_quad_px, add_text, window_width, y, text_size):
        key_label = "Space"
        prompt_scale = max(1.0, text_size / _FULLSCREEN_SUBTITLE_TEXT_SIZE)
        key_size = 1.95 * prompt_scale
        key_pad_x = 10.0 * prompt_scale
        key_pad_y = 5.0 * prompt_scale
        gap = 10.0 * prompt_scale

        left_text = "Press"
        right_text = "to begin"
        left_w = bitmap_font.text_width_px(left_text, text_size)
        right_w = bitmap_font.text_width_px(right_text, text_size)
        key_w = self._measure_keycap_sequence(key_label, key_size, key_pad_x)
        text_h = bitmap_font.text_height_px(text_size)
        key_h = bitmap_font.text_height_px(key_size) + key_pad_y * 2.0
        prompt_h = max(text_h, key_h)
        total_w = left_w + gap + key_w + gap + right_w

        x = (window_width - total_w) / 2.0
        text_y = y + (prompt_h - text_h) / 2.0
        key_y = y + (prompt_h - key_h) / 2.0

        add_text(left_text, x, text_y, text_size, _SPLASH_SUBTITLE_RGBA)
        x += left_w + gap
        self._draw_keycap_sequence(add_quad_px, add_text, key_label, x, key_y, key_size, key_pad_x, key_pad_y)
        x += key_w + gap
        add_text(right_text, x, text_y, text_size, _SPLASH_SUBTITLE_RGBA)

        return y + prompt_h

    def _draw_grouped_controls(
        self,
        add_quad_px,
        add_text,
        window_size,
        top_y,
        available_height,
        layout_scale=1.0,
    ):
        w, h = window_size

        heading_size = 1.68 * layout_scale
        key_size = 1.76 * layout_scale
        desc_size = 1.80 * layout_scale
        row_height = 31.0 * layout_scale
        heading_gap = 13.0 * layout_scale
        section_gap = 58.0 * layout_scale
        key_pad_x = 8.0 * layout_scale
        key_pad_y = 4.0 * layout_scale
        key_desc_gap = 20.0 * layout_scale

        if self._manual_mode and sys.platform != "darwin":
            heading_size = 1.55 * layout_scale
            key_size = 1.62 * layout_scale
            desc_size = 1.66 * layout_scale
            row_height = 28.0 * layout_scale
            heading_gap = 11.0 * layout_scale
            section_gap = 48.0 * layout_scale

        # CAVEVIEWER_UI_TEXT_SCALE changes the actual FreeType line metrics,
        # while the values above describe the layout at its normal size.  Do
        # not let a fixed row height become smaller than the scaled text (or
        # its keycap), otherwise adjacent control rows overlap on high-DPI
        # displays.
        def ensure_text_fits_row(candidate_height):
            return max(
                candidate_height,
                bitmap_font.text_height_px(key_size) + key_pad_y * 2.0,
                bitmap_font.text_height_px(desc_size) + 8.0,
            )

        row_height = ensure_text_fits_row(row_height)

        columns = [
            [self._control_sections[0]],
            [self._control_sections[1], self._control_sections[3]],
            [self._control_sections[2]],
        ]
        column_gap = max(34.0, min(72.0, w * 0.035))

        def measure_columns():
            return [
                self._measure_control_column(
                    sections, heading_size, key_size, desc_size, row_height,
                    heading_gap, section_gap, key_pad_x, key_pad_y, key_desc_gap
                )
                for sections in columns
            ]

        metrics = measure_columns()
        max_height = max((metric["height"] for metric in metrics), default=0.0)
        total_width = sum(metric["width"] for metric in metrics) + column_gap * max(0, len(columns) - 1)
        available_width = max(240.0, w - 80.0)
        if max_height > available_height or total_width > available_width:
            height_ratio = available_height / max(max_height, 1.0)
            width_ratio = available_width / max(total_width, 1.0)
            fit_ratio = max(0.72, min(1.0, height_ratio, width_ratio))
            heading_size = max(1.18, heading_size * fit_ratio)
            key_size = max(1.24, key_size * fit_ratio)
            desc_size = max(1.24, desc_size * fit_ratio)
            row_height = max(21.0, row_height * fit_ratio)
            heading_gap = max(8.0, heading_gap * fit_ratio)
            section_gap = max(18.0, section_gap * fit_ratio)
            key_pad_x = max(7.0, key_pad_x * fit_ratio)
            key_pad_y = max(3.0, key_pad_y * fit_ratio)
            key_desc_gap = max(12.0, key_desc_gap * fit_ratio)
            column_gap = max(28.0, column_gap * fit_ratio)
            row_height = ensure_text_fits_row(row_height)
            metrics = measure_columns()

        total_width = sum(metric["width"] for metric in metrics) + column_gap * max(0, len(columns) - 1)

        x = max(28.0, (w - total_width) / 2.0)
        for sections, metric in zip(columns, metrics):
            self._draw_control_column(
                add_quad_px, add_text, sections,
                x=x,
                top_y=top_y,
                key_col_width=metric["key_col_width"],
                heading_size=heading_size,
                key_size=key_size,
                desc_size=desc_size,
                row_height=row_height,
                heading_gap=heading_gap,
                section_gap=section_gap,
                key_pad_x=key_pad_x,
                key_pad_y=key_pad_y,
                key_desc_gap=key_desc_gap,
            )
            x += metric["width"] + column_gap

    def _measure_control_column(
        self, sections, heading_size, key_size, desc_size, row_height,
        heading_gap, section_gap, key_pad_x, key_pad_y, key_desc_gap
    ):
        key_col_width = 0.0
        desc_col_width = 0.0
        heading_width = 0.0
        height = 0.0
        heading_height = bitmap_font.text_height_px(heading_size)

        for heading, rows in sections:
            heading_width = max(heading_width, bitmap_font.text_width_px(heading.upper(), heading_size))
            height += heading_height + heading_gap
            for key, desc in rows:
                key_col_width = max(
                    key_col_width,
                    self._measure_keycap_sequence(key, key_size, key_pad_x),
                )
                desc_col_width = max(desc_col_width, bitmap_font.text_width_px(desc, desc_size))
                height += row_height
            height += section_gap

        if sections:
            height -= section_gap

        width = max(heading_width, key_col_width + key_desc_gap + desc_col_width)
        return {
            "width": width,
            "height": height,
            "key_col_width": key_col_width,
        }

    def _draw_control_column(
        self, add_quad_px, add_text, sections, x, top_y, key_col_width,
        heading_size, key_size, desc_size, row_height, heading_gap, section_gap,
        key_pad_x, key_pad_y, key_desc_gap
    ):
        y = top_y
        heading_height = bitmap_font.text_height_px(heading_size)
        key_text_height = bitmap_font.text_height_px(key_size)
        desc_text_height = bitmap_font.text_height_px(desc_size)

        desc_x = x + key_col_width + key_desc_gap

        for heading, rows in sections:
            heading_text = heading.upper()
            heading_w = bitmap_font.text_width_px(heading_text, heading_size)
            section_desc_width = max(
                (bitmap_font.text_width_px(desc, desc_size) for _, desc in rows),
                default=0.0,
            )
            section_content_width = max(
                heading_w,
                key_col_width + key_desc_gap + section_desc_width,
            )
            heading_x = x + (section_content_width - heading_w) / 2.0
            add_text(heading_text, heading_x, y, heading_size, _SPLASH_TITLE_RGBA)
            y += heading_height + heading_gap

            for key, desc in rows:
                key_h = key_text_height + key_pad_y * 2.0
                key_sequence_width = self._measure_keycap_sequence(key, key_size, key_pad_x)
                key_x = desc_x - key_desc_gap - key_sequence_width
                key_y = y + (row_height - key_h) / 2.0
                self._draw_keycap_sequence(
                    add_quad_px, add_text, key, key_x, key_y,
                    key_size, key_pad_x, key_pad_y,
                )

                desc_y = y + (row_height - desc_text_height) / 2.0
                add_text(desc, desc_x, desc_y, desc_size, _SPLASH_SUBTITLE_RGBA)
                y += row_height

            y += section_gap

    def _keycap_parts(self, label: str) -> list[str]:
        if " + " in label:
            parts = []
            tokens = label.split(" + ")
            for i, token in enumerate(tokens):
                if i:
                    parts.append("+")
                parts.append(token)
            return parts
        if label in {"W A S D", "J L I K", "Z X"}:
            return label.split()
        return [label]

    def _measure_keycap_sequence(self, label, key_size, key_pad_x):
        total_w = 0.0
        gap = 5.0
        plus_gap = 4.0
        for part in self._keycap_parts(label):
            if part == "+":
                total_w += bitmap_font.text_width_px(part, key_size) + plus_gap * 2.0
            else:
                total_w += bitmap_font.text_width_px(part, key_size) + key_pad_x * 2.0
            total_w += gap
        return max(0.0, total_w - gap)

    def _draw_keycap_sequence(
        self, add_quad_px, add_text, label, x, y, key_size, key_pad_x, key_pad_y
    ):
        cursor_x = x
        gap = 5.0
        plus_gap = 4.0
        key_h = bitmap_font.text_height_px(key_size) + key_pad_y * 2.0
        for part in self._keycap_parts(label):
            part_w = bitmap_font.text_width_px(part, key_size)
            if part == "+":
                add_text(part, cursor_x + plus_gap, y + key_pad_y, key_size, _SPLASH_INSTRUCTION_RGBA)
                cursor_x += part_w + plus_gap * 2.0 + gap
                continue

            key_w = part_w + key_pad_x * 2.0
            self._draw_keycap(add_quad_px, cursor_x, y, cursor_x + key_w, y + key_h)
            add_text(part, cursor_x + key_pad_x, y + key_pad_y, key_size, _SPLASH_TITLE_RGBA)
            cursor_x += key_w + gap

    def _draw_keycap(self, add_quad_px, x0, y0, x1, y1):
        fill = (0.020, 0.030, 0.045, 0.78)
        border = (0.235, 0.365, 0.450, 0.56)
        highlight = (0.360, 0.520, 0.600, 0.32)
        add_quad_px(x0, y0, x1, y1, fill)
        add_quad_px(x0, y0, x1, y0 + 1.0, highlight)
        add_quad_px(x0, y1 - 1.0, x1, y1, border)
        add_quad_px(x0, y0, x0 + 1.0, y1, border)
        add_quad_px(x1 - 1.0, y0, x1, y1, border)

    # -- layout: small panel variant (used after teleport) ----------------------

    def _build_panel(self, add_quad_px, add_text, window_size):
        w, h = window_size
        # Dim the 3D view so that unloaded geometry (black void) is never
        # directly visible while streaming catches up after a teleport.
        # The dim and the logo progress ring fade out together via alpha_mult.
        add_quad_px(0, 0, w, h, (0.003, 0.005, 0.008, 0.90))

    # -- shared control-table drawing --------------------------------------------

    def _draw_control_table(self, add_quad_px, add_text, label_col_right_x, top_y,
                              label_size, desc_size, row_height, gap):
        """
        Draws the control list as two aligned columns: right-aligned key
        labels ending exactly at `label_col_right_x`, then descriptions
        starting `gap` pixels after that -- keeps every row's description
        starting at the same X regardless of how long each label text is,
        which reads much cleaner than left-aligning both columns
        independently (labels of very different lengths would otherwise
        produce a ragged, hard-to-scan list).

        label_col_right_x is an explicit boundary passed in by the caller
        (computed from real content measurements -- see _build_fullscreen
        and _build_panel) rather than derived here from a center point and
        an assumed column width. The earlier center-relative formula could
        place the description column past the edge of whatever container
        it was being drawn into, since it had no actual awareness of that
        container's real boundaries -- this is what let the panel
        variant's description text overflow its own box.
        """
        desc_col_x = label_col_right_x + gap

        y = top_y
        for label, desc in self._control_rows:
            label_w = bitmap_font.text_width_px(label, label_size)
            label_x = label_col_right_x - label_w
            add_text(label, label_x, y, label_size, _SPLASH_SUBTITLE_RGBA)
            add_text(desc, desc_col_x, y, desc_size, _SPLASH_INSTRUCTION_RGBA)
            y += row_height
