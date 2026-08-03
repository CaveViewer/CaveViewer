"""Top-down cave-position overlay for the OpenGL viewer.

A small top-down minimap overlay, bottom-left of the screen, showing a
crude outline of the entire cave map's footprint (computed once from the
chunk manifest's bounding boxes -- no extra rendering pass needed) with a
red ARROW tracking both the camera's current X/Z position and which way
it's currently facing, live as you fly.

The arrow points along the camera's horizontal heading (yaw) only --
not full 3D forward including pitch. This is deliberate: the minimap is
a top-down (X/Z plane) silhouette, so projecting pitch into it too would
mean the arrow shrinks toward a dot whenever you look straight up or down,
which would read as broken rather than informative.

Deliberately crude by design: this is a top-down (X/Z plane) silhouette of
"where does the cave occupy space", not a literal rendered view. For a cave
system with real vertical complexity (multiple levels, shafts), a literal
top-down render would just show overlapping passages on top of each other
and be more confusing than helpful -- an outline of occupied footprint is
the actually-useful version of "where am I in the whole system".

Like LightSlider, this owns its own tiny 2D shader pass and geometry,
independent of the main mesh rendering pipeline.
"""

from __future__ import annotations

import concurrent.futures
import math
from collections.abc import Iterable

import moderngl
import numpy as np


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


class Minimap:
    # Layout, in pixels from the bottom-left corner of the window.
    MARGIN = 18
    PANEL_SIZE = 200       # square panel, in pixels
    ARROW_LENGTH = 9       # tip-to-tail-center distance, in panel pixels
    ARROW_HALF_WIDTH = 5   # half the width across the back of the arrowhead
    CELL_PIXEL_SIZE = 3.0  # how big each occupied chunk-cell renders as, in panel pixels
    _MAX_STATIC_OCCUPANCY_PIXELS = PANEL_SIZE * PANEL_SIZE

    def __init__(self, ctx: moderngl.Context, manifest: dict):
        """
        manifest: the same chunk manifest produced by caveviewer.core.chunking.builder
        (build_cache) / loaded via chunker.load_manifest(). Used once here
        to compute the overall footprint outline -- this does not require
        any chunks to be loaded into memory, since bounding boxes are
        already stored in the manifest itself.
        """
        self.ctx = ctx
        self.program = ctx.program(vertex_shader=_VERT_SRC, fragment_shader=_FRAG_SRC)

        self._footprint_cells_flat = None
        self.occupied_xz = set()
        self._footprint_cell_count = 0
        self._active_route_points_xz: tuple[tuple[float, float], ...] = ()
        self._compute_footprint(manifest)

        # Static geometry (background + footprint + active route + border) and dynamic
        # geometry (camera + bookmark markers) are kept in separate buffers.
        # That avoids re-uploading thousands of static footprint vertices
        # every frame just because the camera arrow moved.
        # The minimap is only 200x200 pixels.  Reserving one quad per map
        # footprint cell can allocate hundreds of MB on very large caves even
        # though most cells collapse onto the same visible pixels.  Reserve for
        # the maximum drawable panel resolution instead; _build_static_geom()
        # performs the same pixel de-duplication before upload.
        visible_footprint_cells = min(
            self._footprint_cell_count, self._MAX_STATIC_OCCUPANCY_PIXELS
        )
        self._max_static_verts = max(256, (visible_footprint_cells + 8) * 6)
        self._static_vbo = ctx.buffer(reserve=self._max_static_verts * 6 * 4)  # 2f pos + 4f color
        self._static_vao = ctx.vertex_array(
            self.program, [(self._static_vbo, "2f 4f", "in_pos", "in_color")]
        )
        self._static_vert_count: int = 0
        self._static_geom_window_size: tuple | None = None

        self._max_dynamic_verts = 256
        self._dynamic_vbo = ctx.buffer(reserve=self._max_dynamic_verts * 6 * 4)
        self._dynamic_vao = ctx.vertex_array(
            self.program, [(self._dynamic_vbo, "2f 4f", "in_pos", "in_color")]
        )
        self._dynamic_vert_count: int = 0

        # CPU-only static byte generation can run off the render thread. The
        # main thread still performs all OpenGL buffer writes/draws.
        self._static_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="CaveViewerMinimap",
        )
        self._static_future: concurrent.futures.Future | None = None
        self._static_future_window_size: tuple | None = None

    def set_active_route_points_xz(
        self,
        points: Iterable[tuple[float, float]] | None,
    ) -> None:
        """Set the highlighted route overlay shown over the minimap footprint."""
        normalized = tuple(
            (float(point[0]), float(point[1]))
            for point in (points or ())
        )
        if normalized == self._active_route_points_xz:
            return
        self._active_route_points_xz = normalized
        if self._static_future is not None:
            self._static_future.cancel()
            self._static_future = None
            self._static_future_window_size = None
        self._static_geom_window_size = None

    # -- footprint computation (done once, at startup) -----------------------

    def _compute_footprint(self, manifest: dict) -> None:
        """
        Builds the set of occupied (cx, cz) cells used by the minimap.

        Prefers the fine-grained "footprint_cells" field written by
        build_cache() (chunker.py) since v1.0.49+: a fixed-resolution
        occupancy grid independent of the 3D chunk_size, so the minimap
        looks detailed even when chunks are large (e.g. 100 m).  Falls
        back to collapsing the 3D chunk cell list for older caches.
        """
        if "footprint_cells" in manifest and "footprint_cell_size" in manifest:
            # Fine-grained path: flat [cx0,cz0, cx1,cz1, ...] int list. Keep
            # the manifest's list by reference instead of copying it into a
            # second full-size set; the overlay later de-duplicates only at the
            # final 200x200 panel-pixel resolution.
            chunk_size = float(manifest["footprint_cell_size"])
            flat = manifest["footprint_cells"]
            self._footprint_cells_flat = flat
            footprint_cells = self._iter_flat_footprint_cells(flat)
        else:
            # Legacy path: derive footprint from 3D chunk cells.
            chunk_size = manifest["chunk_size"]
            occupied_xz = set()
            for cell_str in manifest["chunks"]:
                cx, cy, cz = (int(v) for v in cell_str.split("_"))
                occupied_xz.add((cx, cz))
            self.occupied_xz = occupied_xz
            footprint_cells = iter(occupied_xz)

        min_x = min_z = float("inf")
        max_x = max_z = float("-inf")
        count = 0
        for cx, cz in footprint_cells:
            count += 1
            if cx < min_x: min_x = cx
            if cx > max_x: max_x = cx
            if cz < min_z: min_z = cz
            if cz > max_z: max_z = cz
        if count == 0:
            min_x = max_x = min_z = max_z = 0

        self.chunk_size = chunk_size
        self._footprint_cell_count = count
        self.min_cell_x = min_x
        self.max_cell_x = max_x
        self.min_cell_z = min_z
        self.max_cell_z = max_z
        self._span_x = max(max_x - min_x, 1)
        self._span_z = max(max_z - min_z, 1)

    @staticmethod
    def _iter_flat_footprint_cells(flat) -> Iterable[tuple[int, int]]:
        for i in range(0, len(flat) - 1, 2):
            yield int(flat[i]), int(flat[i + 1])

    def _iter_footprint_cells(self) -> Iterable[tuple[int, int]]:
        if self._footprint_cells_flat is not None:
            return self._iter_flat_footprint_cells(self._footprint_cells_flat)
        return iter(self.occupied_xz)

    # -- coordinate mapping ---------------------------------------------------

    def _panel_rect_px(self, window_size: tuple[int, int]) -> tuple[float, float, float, float]:
        """Returns (x0, y0, x1, y1) of the minimap panel in pixel coords,
        origin top-left, anchored to the bottom-left of the window."""
        w, h = window_size
        x0 = self.MARGIN
        y1 = h - self.MARGIN
        x1 = x0 + self.PANEL_SIZE
        y0 = y1 - self.PANEL_SIZE
        return x0, y0, x1, y1

    def _world_to_panel_px(self, world_x: float, world_z: float,
                             window_size: tuple[int, int]) -> tuple[float, float]:
        """
        Maps a world X/Z position to a pixel position inside the panel,
        preserving aspect ratio (no stretching) by fitting the longer axis
        to the panel and centering the shorter axis, with a small margin
        so the dot/outline doesn't touch the panel's edge.
        """
        x0, y0, x1, y1 = self._panel_rect_px(window_size)
        inner_pad = 10
        inner_x0, inner_y0 = x0 + inner_pad, y0 + inner_pad
        inner_w = (x1 - x0) - 2 * inner_pad
        inner_h = (y1 - y0) - 2 * inner_pad

        cell_x = world_x / self.chunk_size
        cell_z = world_z / self.chunk_size

        span = max(self._span_x, self._span_z)
        # uniform scale so the footprint isn't distorted even if the cave
        # is much longer in one direction than the other
        scale = min(inner_w, inner_h) / max(span, 1e-6)

        # center the (possibly non-square) footprint within the square panel
        center_cell_x = (self.min_cell_x + self.max_cell_x) / 2.0
        center_cell_z = (self.min_cell_z + self.max_cell_z) / 2.0

        px = inner_x0 + inner_w / 2.0 + (cell_x - center_cell_x) * scale
        # panel Y grows downward; world Z growing "away" maps to panel Y
        # growing downward too, which matches typical top-down map
        # conventions (north/forward = up on the map -- but since cave
        # coordinate conventions vary, this is a reasonable default and
        # easy to flip later if it reads backwards for a given map).
        py = inner_y0 + inner_h / 2.0 + (cell_z - center_cell_z) * scale

        return px, py

    def _panel_px_to_world_xz(self, px: float, py: float,
                                window_size: tuple[int, int]) -> tuple[float, float]:
        """
        Exact algebraic inverse of _world_to_panel_px: given a pixel
        position inside the panel, returns the corresponding world (x, z)
        coordinate. Used for click-to-teleport -- the person clicks
        somewhere on the minimap, and this turns that click into an actual
        world position to fly the camera to.
        """
        x0, y0, x1, y1 = self._panel_rect_px(window_size)
        inner_pad = 10
        inner_x0, inner_y0 = x0 + inner_pad, y0 + inner_pad
        inner_w = (x1 - x0) - 2 * inner_pad
        inner_h = (y1 - y0) - 2 * inner_pad

        span = max(self._span_x, self._span_z)
        scale = min(inner_w, inner_h) / max(span, 1e-6)

        center_cell_x = (self.min_cell_x + self.max_cell_x) / 2.0
        center_cell_z = (self.min_cell_z + self.max_cell_z) / 2.0

        cell_x = center_cell_x + (px - inner_x0 - inner_w / 2.0) / scale
        cell_z = center_cell_z + (py - inner_y0 - inner_h / 2.0) / scale

        world_x = cell_x * self.chunk_size
        world_z = cell_z * self.chunk_size
        return world_x, world_z

    def hit_test(self, x: float, y: float, window_size: tuple[int, int]) -> bool:
        """True if (x, y) in pixel coords (origin top-left) lands inside
        the minimap panel."""
        x0, y0, x1, y1 = self._panel_rect_px(window_size)
        return x0 <= x <= x1 and y0 <= y <= y1

    def world_xz_for_click(self, x: float, y: float,
                             window_size: tuple[int, int]) -> tuple[float, float] | None:
        """
        Returns the world (x, z) corresponding to a click at panel pixel
        (x, y), or None if the click landed outside the panel entirely.
        Caller (viewer_window.py) combines this with the camera's current
        Y to build a full teleport target -- the minimap only knows X/Z,
        so it can't and shouldn't decide what height to land at.
        """
        if not self.hit_test(x, y, window_size):
            return None
        return self._panel_px_to_world_xz(x, y, window_size)

    @staticmethod
    def _px_to_ndc(x: float, y: float, window_size: tuple[int, int]) -> tuple[float, float]:
        w, h = window_size
        nx = (x / w) * 2.0 - 1.0
        ny = 1.0 - (y / h) * 2.0
        return nx, ny

    # -- rendering -----------------------------------------------------------

    def _upload_static_geom(self, geom_bytes: bytes, vert_count: int,
                            window_size: tuple[int, int]) -> None:
        if len(geom_bytes) > self._max_static_verts * 6 * 4:
            self._static_vao.release()
            self._static_vbo.release()
            self._max_static_verts = max(self._max_static_verts * 2, vert_count)
            self._static_vbo = self.ctx.buffer(reserve=self._max_static_verts * 6 * 4)
            self._static_vao = self.ctx.vertex_array(
                self.program, [(self._static_vbo, "2f 4f", "in_pos", "in_color")]
            )
        self._static_vbo.write(geom_bytes)
        self._static_vert_count = vert_count
        self._static_geom_window_size = window_size

    def _update_static_geom(self, window_size: tuple[int, int]) -> bool:
        """
        Keep static geometry current for the given window size.

        Returns True when matching static GL buffers are ready to draw. If a
        worker build is still in progress, render() can keep drawing the
        previous size's static buffer during resize, or skip only the first
        frame before the initial build completes.
        """
        if window_size == self._static_geom_window_size:
            return True

        if self._static_future is not None and self._static_future.done():
            future = self._static_future
            future_size = self._static_future_window_size
            self._static_future = None
            self._static_future_window_size = None
            if future.cancelled():
                return self._static_geom_window_size is not None
            geom_bytes, vert_count = future.result()
            if future_size == window_size:
                self._upload_static_geom(geom_bytes, vert_count, window_size)
                return True

        if (
            self._static_future is None
            or self._static_future_window_size != window_size
        ):
            if self._static_future is not None:
                self._static_future.cancel()
            self._static_future_window_size = window_size
            self._static_future = self._static_executor.submit(
                self._build_static_geom, window_size
            )

        return self._static_geom_window_size is not None

    def _build_static_geom(self, window_size: tuple[int, int]) -> tuple[bytes, int]:
        """
        Builds the background + visible footprint + border as pre-serialised
        float32 bytes and a vertex count. Called once per unique window size;
        the result is cached in render() so this loop over every occupied cell
        only runs on startup and on resize.

        The cave footprint can contain far more occupied cells than the minimap
        has pixels.  Emit at most one quad per rounded panel pixel so huge maps
        cannot turn the 2D overlay into an unbounded CPU/GPU allocation.
        """
        verts: list = []

        def add_quad(x0, y0, x1, y1, rgba):
            nx0, ny0 = self._px_to_ndc(x0, y0, window_size)
            nx1, ny1 = self._px_to_ndc(x1, y1, window_size)
            top, bottom = max(ny0, ny1), min(ny0, ny1)
            left, right = min(nx0, nx1), max(nx0, nx1)
            for xy in ((left, bottom), (right, bottom), (right, top),
                       (left, bottom), (right, top), (left, top)):
                verts.append((*xy, *rgba))

        x0, y0, x1, y1 = self._panel_rect_px(window_size)

        add_quad(x0, y0, x1, y1, (0.06, 0.07, 0.10, 0.88))

        cell_px_size = self.CELL_PIXEL_SIZE
        half = cell_px_size / 2.0
        for px, py in self._visible_footprint_pixels(window_size):
            add_quad(px - half, py - half, px + half, py + half, (0.45, 0.52, 0.64, 0.85))

        self._add_route_geom(
            verts,
            window_size,
            self._active_route_points_xz,
            color=(1.0, 0.78, 0.20, 0.95),
            thickness_px=3.4,
        )

        border = 1.5
        border_color = (0.42, 0.54, 0.72, 0.70)
        add_quad(x0, y0, x1, y0 + border, border_color)
        add_quad(x0, y1 - border, x1, y1, border_color)
        add_quad(x0, y0, x0 + border, y1, border_color)
        add_quad(x1 - border, y0, x1, y1, border_color)

        return np.array(verts, dtype=np.float32).tobytes(), len(verts)

    def _add_route_geom(
        self,
        verts: list,
        window_size: tuple[int, int],
        points_xz: tuple[tuple[float, float], ...],
        *,
        color: tuple[float, float, float, float],
        thickness_px: float,
    ) -> None:
        """Add a clipped route polyline overlay to static minimap geometry."""
        if len(points_xz) < 2:
            return

        panel_x0, panel_y0, panel_x1, panel_y1 = self._panel_rect_px(window_size)
        half_thickness = thickness_px / 2.0

        def clamp_point(px: float, py: float) -> tuple[float, float]:
            return (
                max(panel_x0, min(panel_x1, px)),
                max(panel_y0, min(panel_y1, py)),
            )

        def add_segment(first: tuple[float, float], second: tuple[float, float]) -> None:
            x0, y0 = first
            x1, y1 = second
            dx = x1 - x0
            dy = y1 - y0
            length = math.hypot(dx, dy)
            if length <= 1e-6:
                return
            normal_x = -dy / length * half_thickness
            normal_y = dx / length * half_thickness
            corners = (
                (x0 + normal_x, y0 + normal_y),
                (x1 + normal_x, y1 + normal_y),
                (x1 - normal_x, y1 - normal_y),
                (x0 - normal_x, y0 - normal_y),
            )
            ndc_corners = [
                self._px_to_ndc(
                    max(panel_x0, min(panel_x1, px)),
                    max(panel_y0, min(panel_y1, py)),
                    window_size,
                )
                for px, py in corners
            ]
            for xy in (
                ndc_corners[0],
                ndc_corners[1],
                ndc_corners[2],
                ndc_corners[0],
                ndc_corners[2],
                ndc_corners[3],
            ):
                verts.append((*xy, *color))

        panel_points = tuple(
            clamp_point(*self._world_to_panel_px(x, z, window_size))
            for x, z in points_xz
        )
        for first, second in zip(panel_points, panel_points[1:]):
            add_segment(first, second)

    def _visible_footprint_pixels(
        self, window_size: tuple[int, int]
    ) -> Iterable[tuple[int, int]]:
        """Return occupied minimap pixels, de-duplicated at panel resolution."""
        x0, y0, x1, y1 = self._panel_rect_px(window_size)
        pixels: set[tuple[int, int]] = set()
        for cx, cz in self._iter_footprint_cells():
            world_x = (cx + 0.5) * self.chunk_size
            world_z = (cz + 0.5) * self.chunk_size
            px, py = self._world_to_panel_px(world_x, world_z, window_size)
            pixel = (int(round(px)), int(round(py)))
            if x0 <= pixel[0] <= x1 and y0 <= pixel[1] <= y1:
                pixels.add(pixel)
        return pixels

    def render(self, window_size: tuple[int, int], camera_position: np.ndarray,
               camera_forward: np.ndarray,
               bookmarks: dict | None = None) -> None:
        static_ready = self._update_static_geom(window_size)
        if not static_ready:
            return

        # Dynamic part: only the camera arrow/dot (3-36 verts) changes per frame.
        arrow_verts: list = []
        w, h = window_size

        def add_quad_px(x0, y0, x1, y1, rgba):
            nx0 = (x0 / w) * 2.0 - 1.0;  ny0 = 1.0 - (y0 / h) * 2.0
            nx1 = (x1 / w) * 2.0 - 1.0;  ny1 = 1.0 - (y1 / h) * 2.0
            top, bottom = max(ny0, ny1), min(ny0, ny1)
            left, right  = min(nx0, nx1), max(nx0, nx1)
            for xy in ((left, bottom), (right, bottom), (right, top),
                       (left, bottom), (right, top),  (left, top)):
                arrow_verts.append((*xy, *rgba))

        def add_circle_px(cx, cy, radius, rgba, segments=12):
            for i in range(segments):
                a0 = (i / segments) * 2 * np.pi
                a1 = ((i + 1) / segments) * 2 * np.pi
                for (px, py) in [(cx, cy),
                                  (cx + radius * np.cos(a0), cy + radius * np.sin(a0)),
                                  (cx + radius * np.cos(a1), cy + radius * np.sin(a1))]:
                    arrow_verts.append(((px / w) * 2.0 - 1.0, 1.0 - (py / h) * 2.0, *rgba))

        # Bookmark markers: small cross (+) in muted amber, drawn before the
        # camera arrow so the arrow always reads on top of any overlap.
        if bookmarks:
            _BM_COLOR = (0.88, 0.72, 0.28, 0.90)
            arm = 6.5   # half-length of each cross arm in pixels
            thick = 2.0  # half-thickness of each arm
            x0_panel, y0_panel, x1_panel, y1_panel = self._panel_rect_px(window_size)
            for slot_data in bookmarks.values():
                pos = slot_data.get("position")
                if pos is None or len(pos) < 3:
                    continue
                bx, by = self._world_to_panel_px(float(pos[0]), float(pos[2]), window_size)
                # clip to panel interior so markers don't bleed outside the border
                if not (x0_panel < bx < x1_panel and y0_panel < by < y1_panel):
                    continue
                add_quad_px(bx - arm, by - thick, bx + arm, by + thick, _BM_COLOR)  # horizontal
                add_quad_px(bx - thick, by - arm, bx + thick, by + arm, _BM_COLOR)  # vertical

        cam_px, cam_py = self._world_to_panel_px(
            float(camera_position[0]), float(camera_position[2]), window_size
        )

        # Heading-only direction: project the camera's forward vector's
        # X/Z components. Fall back to a plain dot when looking straight
        # up/down (no meaningful horizontal heading).
        forward_x = float(camera_forward[0])
        forward_z = float(camera_forward[2])
        heading_len = math.hypot(forward_x, forward_z)
        if heading_len < 1e-6:
            add_circle_px(cam_px, cam_py, 5, (1.0, 0.15, 0.15, 1.0))
        else:
            ahead_world_x = float(camera_position[0]) + forward_x / heading_len
            ahead_world_z = float(camera_position[2]) + forward_z / heading_len
            ahead_px, ahead_py = self._world_to_panel_px(ahead_world_x, ahead_world_z, window_size)

            dir_x, dir_y = ahead_px - cam_px, ahead_py - cam_py
            dir_len = math.hypot(dir_x, dir_y)
            if dir_len < 1e-6:
                add_circle_px(cam_px, cam_py, 5, (1.0, 0.15, 0.15, 1.0))
            else:
                dir_x, dir_y = dir_x / dir_len, dir_y / dir_len
                perp_x, perp_y = -dir_y, dir_x
                tip = (cam_px + dir_x * self.ARROW_LENGTH,
                       cam_py + dir_y * self.ARROW_LENGTH)
                back_center = (cam_px - dir_x * self.ARROW_LENGTH * 0.6,
                               cam_py - dir_y * self.ARROW_LENGTH * 0.6)
                back_left  = (back_center[0] + perp_x * self.ARROW_HALF_WIDTH,
                              back_center[1] + perp_y * self.ARROW_HALF_WIDTH)
                back_right = (back_center[0] - perp_x * self.ARROW_HALF_WIDTH,
                              back_center[1] - perp_y * self.ARROW_HALF_WIDTH)
                for (px, py) in (tip, back_left, back_right):
                    arrow_verts.append(((px / w) * 2.0 - 1.0, 1.0 - (py / h) * 2.0,
                                        1.0, 0.15, 0.15, 1.0))

        if arrow_verts:
            arrow_bytes = np.array(arrow_verts, dtype=np.float32).tobytes()
            if len(arrow_bytes) > self._max_dynamic_verts * 6 * 4:
                self._dynamic_vao.release()
                self._dynamic_vbo.release()
                self._max_dynamic_verts = max(self._max_dynamic_verts * 2, len(arrow_verts))
                self._dynamic_vbo = self.ctx.buffer(reserve=self._max_dynamic_verts * 6 * 4)
                self._dynamic_vao = self.ctx.vertex_array(
                    self.program, [(self._dynamic_vbo, "2f 4f", "in_pos", "in_color")]
                )
            self._dynamic_vbo.write(arrow_bytes)
            self._dynamic_vert_count = len(arrow_verts)
        else:
            self._dynamic_vert_count = 0

        # Face culling is meaningless for a flat 2D overlay (there's no
        # "back" of a UI element that should ever be hidden), but the main
        # 3D mesh pass leaves CULL_FACE enabled globally. The quad helper
        # above happens to wind consistently with the enabled cull mode, so
        # quads render fine -- but the circle/fan helper winds the opposite
        # rotational direction, so without this disable, the position dot
        # (drawn as a fan) gets silently backface-culled every frame while
        # everything else keeps rendering normally. This was the actual
        # cause of the missing red dot.
        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        if static_ready and self._static_vert_count:
            self._static_vao.render(moderngl.TRIANGLES, vertices=self._static_vert_count)
        if self._dynamic_vert_count:
            self._dynamic_vao.render(moderngl.TRIANGLES, vertices=self._dynamic_vert_count)
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    def release(self) -> None:
        if self._static_future is not None:
            self._static_future.cancel()
            self._static_future = None
        self._static_executor.shutdown(wait=False, cancel_futures=True)
        for attr_name in ("_static_vao", "_static_vbo", "_dynamic_vao", "_dynamic_vbo", "program"):
            resource = getattr(self, attr_name, None)
            if resource is None:
                continue
            try:
                resource.release()
            except Exception:
                pass
            setattr(self, attr_name, None)
