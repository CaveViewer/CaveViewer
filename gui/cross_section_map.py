"""
gui/cross_section_map.py

A longitudinal geometric cross-section panel, stacked above the minimap.
It slices the cached cave mesh with a vertical slab aligned to the camera's
horizontal travel/facing axis.

The panel is not a minimap: it draws only section lines plus a travel
arrow. The horizontal screen axis is distance along the travel axis; the
vertical screen axis is world elevation.

The expensive CPU work -- reading candidate chunk files and intersecting
triangles -- runs on worker threads. The panel intentionally shows a short,
coarse local profile rather than a long exact survey section, so it loads
quickly and avoids over-promising cave shape beyond practical visibility.
"""

from __future__ import annotations

import concurrent.futures
import math
import threading
from collections import OrderedDict

import moderngl
import numpy as np

from core import chunker
from core.logging_utils import get_logger


_LOG = get_logger("LongitudinalMap")


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


RawSegment = tuple[float, float, float, float, float]  # u0, y0, u1, y1, weight


class CrossSectionMap:
    # Bottom-left stack layout. The minimap occupies the bottom slot.
    MARGIN = 18
    MINIMAP_PANEL_SIZE = 200
    STACK_GAP = 10
    PANEL_WIDTH = 200
    PANEL_HEIGHT = 120

    RAW_ALONG_STEP = 8.0              # meters between raw section rebuilds
    DISPLAY_ALONG_STEP = 4.0          # meters between visible cross-section redraws
    LATERAL_AXIS_STEP = 1.5           # meters; tolerate side-to-side camera drift
    HEADING_STEP_RADIANS = math.radians(5.0)
    MAX_VIEW_LENGTH = 36.0            # meters shown along the travel axis
    MIN_VIEW_LENGTH = 16.0
    VIEW_WORLD_FRACTION = 0.08
    LOOK_BEHIND_FRACTION = 0.15       # current position sits 15% from the left edge
    RAW_WINDOW_MARGIN = 8.0           # extra raw data around the visible section
    LINE_WIDTH_PX = 1.2
    ENVELOPE_LINE_WIDTH_PX = 1.8
    ENVELOPE_BIN_COUNT = 48
    SLAB_HALF_WIDTH = 3.0             # coarse local corridor width
    SLAB_SAMPLE_COUNT = 3
    # A tight slice is not a reliable inside/outside test: a camera inside a
    # broad chamber can miss the surface while a camera outside can still
    # intersect nearby mesh. Use a targeted wider search when needed.
    RECOVERY_SLAB_HALF_WIDTH = 24.0
    RECOVERY_SLAB_SAMPLE_COUNT = 3
    RAW_CACHE_LIMIT = 8
    # A typical local section touches 40-150 small positions-only files.
    # Retaining the whole working set avoids rereading it during the nearest-
    # geometry probe and the targeted recovery slice that immediately follows.
    TRIANGLE_CACHE_LIMIT = 256
    # One running build plus one replaceable latest request. A single feeder
    # avoids out-of-order completions and is fast enough for the ~50 ms local
    # profile workload.
    RAW_PENDING_LIMIT = 2
    # Live navigation always wins over speculative work. Nearby completed
    # views are already retained in _raw_cache, so prefetching here mostly
    # competes with the diver's current view during fast travel.
    PREFETCH_AHEAD_COUNT = 0
    PREFETCH_BEHIND_COUNT = 0
    PLANE_MARGIN = 1e-4

    def __init__(self, ctx: moderngl.Context, cache_dir: str, manifest: dict):
        self.ctx = ctx
        self.cache_dir = cache_dir
        self.program = ctx.program(vertex_shader=_VERT_SRC, fragment_shader=_FRAG_SRC)
        self._has_cross_section_cache = bool(manifest.get("cross_section_cache"))

        self._compute_manifest_bounds(manifest)

        self._max_verts = 4096
        self._vbo = ctx.buffer(reserve=self._max_verts * 6 * 4)  # 2f pos + 4f color
        self._vao = ctx.vertex_array(
            self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
        )
        self._vert_count = 0

        self._last_heading = (1.0, 0.0)
        self._active_raw_key: tuple | None = None
        self._active_segments: list[RawSegment] = []
        self._raw_cache: OrderedDict[tuple, list[RawSegment]] = OrderedDict()
        self._triangle_cache: OrderedDict[tuple[int, int, int], np.ndarray] = OrderedDict()
        self._triangle_cache_lock = threading.Lock()
        self._pending_raw_builds: OrderedDict[tuple, concurrent.futures.Future] = OrderedDict()
        self._profile_stop_event = threading.Event()
        self._uploaded_frame_key: tuple | None = None

        self._slice_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="CaveViewerLongSection",
        )

    # -- manifest geometry -------------------------------------------------

    def _compute_manifest_bounds(self, manifest: dict) -> None:
        chunk_bounds: list[tuple[tuple[int, int, int], tuple[float, float, float], tuple[float, float, float]]] = []
        min_x = min_y = min_z = float("inf")
        max_x = max_y = max_z = float("-inf")
        try:
            self._manifest_chunk_size = float(manifest.get("chunk_size", 0.0))
        except (TypeError, ValueError):
            self._manifest_chunk_size = 0.0

        for cell_str, info in manifest.get("chunks", {}).items():
            try:
                cell = tuple(int(v) for v in cell_str.split("_"))
            except ValueError:
                continue
            if len(cell) != 3:
                continue

            bmin = info.get("bounds_min")
            bmax = info.get("bounds_max")
            if bmin is None or bmax is None or len(bmin) < 3 or len(bmax) < 3:
                continue

            bmin_t = (float(bmin[0]), float(bmin[1]), float(bmin[2]))
            bmax_t = (float(bmax[0]), float(bmax[1]), float(bmax[2]))
            chunk_bounds.append((cell, bmin_t, bmax_t))

            min_x = min(min_x, bmin_t[0])
            min_y = min(min_y, bmin_t[1])
            min_z = min(min_z, bmin_t[2])
            max_x = max(max_x, bmax_t[0])
            max_y = max(max_y, bmax_t[1])
            max_z = max(max_z, bmax_t[2])

        if not chunk_bounds:
            chunk_bounds.append(((0, 0, 0), (-1.0, -1.0, -1.0), (1.0, 1.0, 1.0)))
            min_x = min_y = min_z = -1.0
            max_x = max_y = max_z = 1.0

        self._chunk_bounds = chunk_bounds
        self._world_min_y = min_y
        self._world_max_y = max_y
        self._world_span_y = max(max_y - min_y, 1e-6)

        world_span_x = max(max_x - min_x, 1e-6)
        world_span_z = max(max_z - min_z, 1e-6)
        world_diag_xz = math.hypot(world_span_x, world_span_z)
        view_length = max(
            self.MIN_VIEW_LENGTH,
            min(self.MAX_VIEW_LENGTH, world_diag_xz * self.VIEW_WORLD_FRACTION),
        )
        self._look_behind = view_length * self.LOOK_BEHIND_FRACTION
        self._look_ahead = view_length - self._look_behind
        self._build_chunk_spatial_index()

    def _build_chunk_spatial_index(self) -> None:
        chunk_size = float(getattr(self, "_manifest_chunk_size", 0.0) or 0.0)
        if chunk_size <= 0.0:
            world_span_x = max(
                max(bmax[0] for _cell, _bmin, bmax in self._chunk_bounds)
                - min(bmin[0] for _cell, bmin, _bmax in self._chunk_bounds),
                1.0,
            )
            world_span_z = max(
                max(bmax[2] for _cell, _bmin, bmax in self._chunk_bounds)
                - min(bmin[2] for _cell, bmin, _bmax in self._chunk_bounds),
                1.0,
            )
            chunk_size = max(8.0, min(world_span_x, world_span_z) / 64.0)
        chunk_size = max(1.0, chunk_size)

        self._chunk_index_cell_size = chunk_size
        index: dict[tuple[int, int], list[int]] = {}
        for idx, (_cell, bmin, bmax) in enumerate(self._chunk_bounds):
            gx0 = math.floor(bmin[0] / chunk_size)
            gx1 = math.floor(bmax[0] / chunk_size)
            gz0 = math.floor(bmin[2] / chunk_size)
            gz1 = math.floor(bmax[2] / chunk_size)
            for gx in range(gx0, gx1 + 1):
                for gz in range(gz0, gz1 + 1):
                    index.setdefault((gx, gz), []).append(idx)
        self._chunk_spatial_index = index

    # -- panel mapping -----------------------------------------------------

    def _panel_rect_px(self, window_size: tuple[int, int]) -> tuple[float, float, float, float]:
        _w, h = window_size
        x0 = self.MARGIN
        y1 = h - self.MARGIN - self.MINIMAP_PANEL_SIZE - self.STACK_GAP
        x1 = x0 + self.PANEL_WIDTH
        y0 = y1 - self.PANEL_HEIGHT
        return x0, y0, x1, y1

    def _inner_rect_px(self, window_size: tuple[int, int]) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = self._panel_rect_px(window_size)
        inner_pad = 10
        return x0 + inner_pad, y0 + inner_pad, x1 - inner_pad, y1 - inner_pad

    def _section_to_panel_px(self, distance_along_axis: float, world_y: float,
                             window_size: tuple[int, int],
                             min_y: float, max_y: float) -> tuple[float, float]:
        ix0, iy0, ix1, iy1 = self._inner_rect_px(window_size)
        iw = ix1 - ix0
        ih = iy1 - iy0
        distance_t = (distance_along_axis + self._look_behind) / (self._look_behind + self._look_ahead)
        height_span = max(max_y - min_y, 1e-6)
        height_t = (world_y - min_y) / height_span
        px = ix0 + distance_t * iw
        py = iy1 - height_t * ih
        return px, py

    @staticmethod
    def _px_to_ndc(x: float, y: float, window_size: tuple[int, int]) -> tuple[float, float]:
        w, h = window_size
        return (x / w) * 2.0 - 1.0, 1.0 - (y / h) * 2.0

    # -- camera/view keys --------------------------------------------------

    def _view_for_camera(self, window_size: tuple[int, int],
                         camera_position: np.ndarray,
                         camera_forward: np.ndarray) -> tuple[tuple, float]:
        fx = float(camera_forward[0])
        fz = float(camera_forward[2])
        horizontal_len = math.hypot(fx, fz)
        if horizontal_len < 1e-6:
            dir_x, dir_z = self._last_heading
        else:
            dir_x = fx / horizontal_len
            dir_z = fz / horizontal_len

        angle = math.atan2(dir_z, dir_x)
        quant_angle = round(angle / self.HEADING_STEP_RADIANS) * self.HEADING_STEP_RADIANS
        dir_x = math.cos(quant_angle)
        dir_z = math.sin(quant_angle)
        self._last_heading = (dir_x, dir_z)

        cam_x = float(camera_position[0])
        cam_z = float(camera_position[2])
        normal_x = -dir_z
        normal_z = dir_x
        camera_along = cam_x * dir_x + cam_z * dir_z
        lateral = cam_x * normal_x + cam_z * normal_z

        raw_center_along = round(camera_along / self.RAW_ALONG_STEP) * self.RAW_ALONG_STEP
        snapped_lateral = round(lateral / self.LATERAL_AXIS_STEP) * self.LATERAL_AXIS_STEP
        raw_key = (raw_center_along, snapped_lateral, quant_angle)
        return raw_key, camera_along

    def _display_along(self, camera_along: float) -> float:
        return round(camera_along / self.DISPLAY_ALONG_STEP) * self.DISPLAY_ALONG_STEP

    @staticmethod
    def _camera_along_for_key(camera_position: np.ndarray, raw_key: tuple) -> float:
        angle = raw_key[2]
        return float(camera_position[0]) * math.cos(angle) + float(camera_position[2]) * math.sin(angle)

    # -- CPU draw helpers --------------------------------------------------

    def _add_quad_px(self, verts: list, window_size: tuple[int, int],
                     x0: float, y0: float, x1: float, y1: float,
                     rgba: tuple[float, float, float, float]) -> None:
        nx0, ny0 = self._px_to_ndc(x0, y0, window_size)
        nx1, ny1 = self._px_to_ndc(x1, y1, window_size)
        top, bottom = max(ny0, ny1), min(ny0, ny1)
        left, right = min(nx0, nx1), max(nx0, nx1)
        for xy in ((left, bottom), (right, bottom), (right, top),
                   (left, bottom), (right, top), (left, top)):
            verts.append((*xy, *rgba))

    def _add_segment_quad_px(self, verts: list, window_size: tuple[int, int],
                             x0: float, y0: float, x1: float, y1: float,
                             rgba: tuple[float, float, float, float],
                             line_width_px: float | None = None) -> None:
        dx = x1 - x0
        dy = y1 - y0
        length = math.hypot(dx, dy)
        if length < 1e-6:
            return
        half_w = (self.LINE_WIDTH_PX if line_width_px is None else line_width_px) / 2.0
        px = -dy / length * half_w
        py = dx / length * half_w
        corners = (
            (x0 + px, y0 + py),
            (x1 + px, y1 + py),
            (x1 - px, y1 - py),
            (x0 - px, y0 - py),
        )
        ndc = [self._px_to_ndc(x, y, window_size) for x, y in corners]
        for idx in (0, 1, 2, 0, 2, 3):
            verts.append((*ndc[idx], *rgba))

    # -- slicing -----------------------------------------------------------

    def _abort_if_profile_build_stopped(self, cancellable: bool) -> None:
        if cancellable and self._profile_stop_event.is_set():
            raise concurrent.futures.CancelledError()

    def _build_raw_segments(
        self, raw_key: tuple, cancellable: bool = False
    ) -> list[RawSegment]:
        self._abort_if_profile_build_stopped(cancellable)
        segments = self._build_raw_segments_for_slab(
            raw_key, self.SLAB_HALF_WIDTH, self.SLAB_SAMPLE_COUNT, cancellable
        )
        if segments:
            return segments
        self._abort_if_profile_build_stopped(cancellable)

        recovery_offset = self._nearest_recovery_plane_offset(raw_key, cancellable)
        if recovery_offset is None:
            return []
        recovery_offsets = sorted({
            max(-self.RECOVERY_SLAB_HALF_WIDTH, recovery_offset - 1.5),
            recovery_offset,
            min(self.RECOVERY_SLAB_HALF_WIDTH, recovery_offset + 1.5),
        })
        return self._build_raw_segments_for_slab(
            raw_key, self.RECOVERY_SLAB_HALF_WIDTH, self.RECOVERY_SLAB_SAMPLE_COUNT,
            cancellable, recovery_offsets,
        )

    def _build_raw_segments_for_slab(
        self,
        raw_key: tuple,
        slab_half_width: float,
        slab_sample_count: int,
        cancellable: bool = False,
        sample_offsets: list[float] | None = None,
    ) -> list[RawSegment]:
        self._abort_if_profile_build_stopped(cancellable)
        raw_center_along, lateral, angle = raw_key
        dir_x = math.cos(angle)
        dir_z = math.sin(angle)
        normal_x = -dir_z
        normal_z = dir_x
        u_min = raw_center_along - self._look_behind - self.RAW_WINDOW_MARGIN
        u_max = raw_center_along + self._look_ahead + self.RAW_WINDOW_MARGIN

        candidate_cells = [
            cell for cell, bmin, bmax in self._candidate_chunk_bounds_for_section_window(
                lateral, dir_x, dir_z, normal_x, normal_z, u_min, u_max, slab_half_width
            )
            if self._chunk_aabb_intersects_section_window(
                lateral, dir_x, dir_z, normal_x, normal_z,
                u_min, u_max, slab_half_width, bmin, bmax
            )
        ]

        if sample_offsets is not None:
            offsets = sample_offsets
        elif slab_sample_count <= 1:
            offsets = [0.0]
        else:
            offsets = np.linspace(-slab_half_width, slab_half_width, slab_sample_count).tolist()

        segments: list[RawSegment] = []
        for cell in candidate_cells:
            # A newer camera key can arrive while this worker is reading or
            # slicing hundreds of candidate chunks. Stop between chunks so
            # an obsolete view cannot monopolize a profile worker.
            self._abort_if_profile_build_stopped(cancellable)
            section_tris = self._load_cached_section_triangles(cell)
            if section_tris is not None:
                self._append_slab_intersections(
                    segments, section_tris, offsets, lateral,
                    slab_half_width, u_min, u_max,
                    dir_x, dir_z, normal_x, normal_z,
                )
                continue

            self._append_legacy_chunk_intersections(
                segments, cell, offsets, lateral,
                slab_half_width, u_min, u_max,
                dir_x, dir_z, normal_x, normal_z,
            )
        return segments

    def _nearest_recovery_plane_offset(
        self, raw_key: tuple, cancellable: bool
    ) -> float | None:
        """Find a useful nearby slice plane without sweeping the whole slab."""
        raw_center_along, lateral, angle = raw_key
        dir_x = math.cos(angle)
        dir_z = math.sin(angle)
        normal_x = -dir_z
        normal_z = dir_x
        u_min = raw_center_along - self._look_behind - self.RAW_WINDOW_MARGIN
        u_max = raw_center_along + self._look_ahead + self.RAW_WINDOW_MARGIN
        half_width = self.RECOVERY_SLAB_HALF_WIDTH

        candidates = self._candidate_chunk_bounds_for_section_window(
            lateral, dir_x, dir_z, normal_x, normal_z, u_min, u_max, half_width
        )
        best_offset: float | None = None
        best_distance = float("inf")
        eps = 1e-4

        for cell, bmin, bmax in candidates:
            self._abort_if_profile_build_stopped(cancellable)
            if not self._chunk_aabb_intersects_section_window(
                lateral, dir_x, dir_z, normal_x, normal_z,
                u_min, u_max, half_width, bmin, bmax,
            ):
                continue
            tris = self._load_cached_section_triangles(cell)
            if tris is None:
                continue
            tris = self._filter_triangles_for_section_window(
                tris, lateral, half_width, u_min, u_max,
                dir_x, dir_z, normal_x, normal_z,
            )
            if len(tris) == 0:
                continue

            signed = tris[:, :, 0] * normal_x + tris[:, :, 2] * normal_z - lateral
            lows = signed.min(axis=1)
            highs = signed.max(axis=1)
            widths = highs - lows
            valid = widths > eps
            if not np.any(valid):
                continue
            lows = lows[valid]
            highs = highs[valid]
            widths = widths[valid]

            offsets = np.where(
                lows > 0.0,
                lows + np.minimum(widths * 0.25, 0.5),
                np.where(
                    highs < 0.0,
                    highs - np.minimum(widths * 0.25, 0.5),
                    0.0,
                ),
            )
            idx = int(np.argmin(np.abs(offsets)))
            distance = abs(float(offsets[idx]))
            if distance < best_distance:
                best_distance = distance
                best_offset = float(offsets[idx])
                if best_distance <= eps:
                    break

        return best_offset

    def _load_cached_section_triangles(self, cell: tuple[int, int, int]) -> np.ndarray | None:
        if not self._has_cross_section_cache:
            return None
        with self._triangle_cache_lock:
            cached = self._triangle_cache.get(cell)
            if cached is not None:
                self._triangle_cache.move_to_end(cell)
                return cached
        try:
            tris = chunker.load_cross_section_triangles(self.cache_dir, cell)
        except Exception as exc:
            _LOG.warning("Could not read profile triangles for chunk %s: %s", cell, exc)
            return None
        if tris is None:
            return None
        with self._triangle_cache_lock:
            # Another profile worker may have loaded the same cell while
            # this thread was reading it. Keep the existing array in that
            # case so all LRU operations remain serialized.
            cached = self._triangle_cache.get(cell)
            if cached is not None:
                self._triangle_cache.move_to_end(cell)
                return cached
            self._triangle_cache[cell] = tris
            self._triangle_cache.move_to_end(cell)
            while len(self._triangle_cache) > self.TRIANGLE_CACHE_LIMIT:
                self._triangle_cache.popitem(last=False)
            return tris

    def _append_legacy_chunk_intersections(
        self,
        segments: list[RawSegment],
        cell: tuple[int, int, int],
        offsets: list[float],
        lateral: float,
        slab_half_width: float,
        u_min: float,
        u_max: float,
        dir_x: float,
        dir_z: float,
        normal_x: float,
        normal_z: float,
    ) -> None:
        try:
            chunk_data = chunker.load_chunk_file(self.cache_dir, cell)
        except Exception:
            return

        for group in chunk_data.groups.values():
            positions = group.positions
            tri_count = len(positions) // 3
            if tri_count <= 0:
                continue
            tris = positions[:tri_count * 3].reshape(tri_count, 3, 3)
            self._append_slab_intersections(
                segments, tris, offsets, lateral,
                slab_half_width, u_min, u_max,
                dir_x, dir_z, normal_x, normal_z,
            )

    def _append_slab_intersections(
        self,
        segments: list[RawSegment],
        tris: np.ndarray,
        offsets: list[float],
        lateral: float,
        slab_half_width: float,
        u_min: float,
        u_max: float,
        dir_x: float,
        dir_z: float,
        normal_x: float,
        normal_z: float,
    ) -> None:
        tris = self._filter_triangles_for_section_window(
            tris, lateral, slab_half_width, u_min, u_max,
            dir_x, dir_z, normal_x, normal_z,
        )
        if len(tris) == 0:
            return

        for offset in offsets:
            sample_lateral = lateral + offset
            sample_weight = 1.0 - min(abs(offset) / max(slab_half_width, 1e-6), 1.0) * 0.45
            raw_segments = self._intersect_triangles_vertical_plane_batch(
                tris, sample_lateral, dir_x, dir_z, normal_x, normal_z
            )
            segments.extend(
                (u0, y0, u1, y1, sample_weight)
                for u0, y0, u1, y1 in raw_segments
            )

    @staticmethod
    def _filter_triangles_for_section_window(
        tris: np.ndarray,
        lateral: float,
        slab_half_width: float,
        u_min: float,
        u_max: float,
        dir_x: float,
        dir_z: float,
        normal_x: float,
        normal_z: float,
    ) -> np.ndarray:
        if len(tris) == 0:
            return tris

        tri_u = tris[:, :, 0] * dir_x + tris[:, :, 2] * dir_z
        tri_signed = tris[:, :, 0] * normal_x + tris[:, :, 2] * normal_z - lateral
        mask = (
            (tri_u.max(axis=1) >= u_min)
            & (tri_u.min(axis=1) <= u_max)
            & (tri_signed.max(axis=1) >= -slab_half_width)
            & (tri_signed.min(axis=1) <= slab_half_width)
        )
        if np.all(mask):
            return tris
        return tris[mask]

    def _chunk_aabb_intersects_section_window(self, lateral: float,
                                              dir_x: float, dir_z: float,
                                              normal_x: float, normal_z: float,
                                              u_min: float, u_max: float,
                                              slab_half_width: float,
                                              bmin: tuple[float, float, float],
                                              bmax: tuple[float, float, float]) -> bool:
        signed = []
        distances = []
        for x in (bmin[0], bmax[0]):
            for z in (bmin[2], bmax[2]):
                signed.append(x * normal_x + z * normal_z - lateral)
                distances.append(x * dir_x + z * dir_z)
        return (
            min(signed) <= slab_half_width + self.PLANE_MARGIN
            and max(signed) >= -slab_half_width - self.PLANE_MARGIN
            and max(distances) >= u_min
            and min(distances) <= u_max
        )

    def _candidate_chunk_bounds_for_section_window(
        self,
        lateral: float,
        dir_x: float,
        dir_z: float,
        normal_x: float,
        normal_z: float,
        u_min: float,
        u_max: float,
        slab_half_width: float,
    ) -> list[tuple[tuple[int, int, int], tuple[float, float, float], tuple[float, float, float]]]:
        cell_size = self._chunk_index_cell_size
        corners = []
        for u in (u_min, u_max):
            for side in (lateral - slab_half_width, lateral + slab_half_width):
                corners.append((
                    u * dir_x + side * normal_x,
                    u * dir_z + side * normal_z,
                ))

        min_x = min(x for x, _z in corners)
        max_x = max(x for x, _z in corners)
        min_z = min(z for _x, z in corners)
        max_z = max(z for _x, z in corners)
        gx0 = math.floor(min_x / cell_size) - 1
        gx1 = math.floor(max_x / cell_size) + 1
        gz0 = math.floor(min_z / cell_size) - 1
        gz1 = math.floor(max_z / cell_size) + 1

        candidate_indices: set[int] = set()
        for gx in range(gx0, gx1 + 1):
            for gz in range(gz0, gz1 + 1):
                candidate_indices.update(self._chunk_spatial_index.get((gx, gz), ()))
        if not candidate_indices:
            return []
        return [self._chunk_bounds[idx] for idx in candidate_indices]

    @staticmethod
    def _intersect_triangle_vertical_plane(
        tri: np.ndarray,
        lateral: float,
        dir_x: float,
        dir_z: float,
        normal_x: float,
        normal_z: float,
    ) -> tuple[float, float, float, float] | None:
        eps = 1e-5
        points: list[tuple[float, float]] = []

        def signed_distance(point: np.ndarray) -> float:
            return float(point[0]) * normal_x + float(point[2]) * normal_z - lateral

        def add_point(point: np.ndarray) -> None:
            u = float(point[0]) * dir_x + float(point[2]) * dir_z
            y = float(point[1])
            for existing in points:
                if abs(existing[0] - u) < eps and abs(existing[1] - y) < eps:
                    return
            points.append((u, y))

        distances = [signed_distance(tri[i]) for i in range(3)]
        if all(abs(d) <= eps for d in distances):
            candidates = [
                (tri[0], tri[1]),
                (tri[1], tri[2]),
                (tri[2], tri[0]),
            ]
            p0, p1 = max(candidates, key=lambda pair: float(np.linalg.norm(pair[1] - pair[0])))
            add_point(p0)
            add_point(p1)
        else:
            for i0, i1 in ((0, 1), (1, 2), (2, 0)):
                p0 = tri[i0]
                p1 = tri[i1]
                d0 = distances[i0]
                d1 = distances[i1]
                if abs(d0) <= eps and abs(d1) <= eps:
                    continue
                if abs(d0) <= eps:
                    add_point(p0)
                if abs(d1) <= eps:
                    add_point(p1)
                if d0 * d1 < 0.0:
                    t = d0 / (d0 - d1)
                    add_point(p0 + (p1 - p0) * t)

        if len(points) < 2:
            return None
        return (points[0][0], points[0][1], points[1][0], points[1][1])

    def _intersect_triangles_vertical_plane_batch(
        self,
        tris: np.ndarray,
        lateral: float,
        dir_x: float,
        dir_z: float,
        normal_x: float,
        normal_z: float,
    ) -> list[tuple[float, float, float, float]]:
        eps = 1e-5
        if len(tris) == 0:
            return []

        distances = tris[:, :, 0] * normal_x + tris[:, :, 2] * normal_z - lateral
        near_plane = np.any(np.abs(distances) <= eps, axis=1)
        edge_hit = np.zeros((len(tris), 3), dtype=bool)
        hit_u = np.empty((len(tris), 3), dtype=np.float32)
        hit_y = np.empty((len(tris), 3), dtype=np.float32)

        for edge_idx, (i0, i1) in enumerate(((0, 1), (1, 2), (2, 0))):
            d0 = distances[:, i0]
            d1 = distances[:, i1]
            mask = (d0 * d1 < 0.0) & ~near_plane
            if not np.any(mask):
                continue

            p0 = tris[mask, i0, :]
            p1 = tris[mask, i1, :]
            t = d0[mask] / (d0[mask] - d1[mask])
            points = p0 + (p1 - p0) * t[:, None]
            edge_hit[mask, edge_idx] = True
            hit_u[mask, edge_idx] = points[:, 0] * dir_x + points[:, 2] * dir_z
            hit_y[mask, edge_idx] = points[:, 1]

        segments: list[tuple[float, float, float, float]] = []
        hit_counts = edge_hit.sum(axis=1)
        for tri_idx in np.flatnonzero(hit_counts == 2):
            edge_ids = np.flatnonzero(edge_hit[tri_idx])
            segments.append((
                float(hit_u[tri_idx, edge_ids[0]]),
                float(hit_y[tri_idx, edge_ids[0]]),
                float(hit_u[tri_idx, edge_ids[1]]),
                float(hit_y[tri_idx, edge_ids[1]]),
            ))

        for tri_idx in np.flatnonzero(near_plane):
            segment = self._intersect_triangle_vertical_plane(
                tris[tri_idx], lateral, dir_x, dir_z, normal_x, normal_z
            )
            if segment is not None:
                segments.append(segment)

        return segments

    # -- render geometry build --------------------------------------------

    def _clip_segment_to_visible_window(
        self, segment: RawSegment, camera_along: float
    ) -> RawSegment | None:
        u0, y0, u1, y1, weight = segment
        rel0 = u0 - camera_along
        rel1 = u1 - camera_along
        lo = -self._look_behind
        hi = self._look_ahead
        if (rel0 < lo and rel1 < lo) or (rel0 > hi and rel1 > hi):
            return None
        du = rel1 - rel0
        if abs(du) < 1e-9:
            if lo <= rel0 <= hi:
                return rel0, y0, rel1, y1, weight
            return None
        t0 = 0.0
        t1 = 1.0
        for edge, sign in ((lo, 1.0), (hi, -1.0)):
            v0 = sign * (rel0 - edge)
            v1 = sign * (rel1 - edge)
            if v0 >= 0.0 and v1 >= 0.0:
                continue
            if v0 < 0.0 and v1 < 0.0:
                return None
            t = v0 / (v0 - v1)
            if v0 < 0.0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
        if t0 > t1:
            return None
        cu0 = rel0 + du * t0
        cy0 = y0 + (y1 - y0) * t0
        cu1 = rel0 + du * t1
        cy1 = y0 + (y1 - y0) * t1
        return cu0, cy0, cu1, cy1, weight

    def _envelope_points_from_segments(
        self, clipped_segments: list[RawSegment]
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        if not clipped_segments:
            return [], []

        lo = -self._look_behind
        hi = self._look_ahead
        span = max(hi - lo, 1e-6)
        floors: list[float | None] = [None] * self.ENVELOPE_BIN_COUNT
        ceilings: list[float | None] = [None] * self.ENVELOPE_BIN_COUNT

        def add_sample(u: float, y: float) -> None:
            idx = int((u - lo) / span * self.ENVELOPE_BIN_COUNT)
            idx = max(0, min(self.ENVELOPE_BIN_COUNT - 1, idx))
            floor_y = floors[idx]
            ceiling_y = ceilings[idx]
            floors[idx] = y if floor_y is None else min(floor_y, y)
            ceilings[idx] = y if ceiling_y is None else max(ceiling_y, y)

        for u0, y0, u1, y1, _weight in clipped_segments:
            add_sample(u0, y0)
            add_sample(u1, y1)
            add_sample((u0 + u1) * 0.5, (y0 + y1) * 0.5)

        floor_points: list[tuple[float, float]] = []
        ceiling_points: list[tuple[float, float]] = []
        for idx, (floor_y, ceiling_y) in enumerate(zip(floors, ceilings)):
            if floor_y is None or ceiling_y is None:
                continue
            u = lo + (idx + 0.5) / self.ENVELOPE_BIN_COUNT * span
            floor_points.append((u, floor_y))
            ceiling_points.append((u, ceiling_y))

        return (
            self._smooth_envelope_points(floor_points),
            self._smooth_envelope_points(ceiling_points),
        )

    @staticmethod
    def _smooth_envelope_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(points) < 3:
            return points
        smoothed = [points[0]]
        for idx in range(1, len(points) - 1):
            prev_u, prev_y = points[idx - 1]
            cur_u, cur_y = points[idx]
            next_u, next_y = points[idx + 1]
            smoothed.append((cur_u, prev_y * 0.2 + cur_y * 0.6 + next_y * 0.2))
        smoothed.append(points[-1])
        return smoothed

    def _add_envelope_polyline_px(
        self,
        verts: list,
        window_size: tuple[int, int],
        points: list[tuple[float, float]],
        section_min_y: float,
        section_max_y: float,
        rgba: tuple[float, float, float, float],
    ) -> None:
        for idx in range(len(points) - 1):
            u0, sy0 = points[idx]
            u1, sy1 = points[idx + 1]
            px0, py0 = self._section_to_panel_px(u0, sy0, window_size, section_min_y, section_max_y)
            px1, py1 = self._section_to_panel_px(u1, sy1, window_size, section_min_y, section_max_y)
            self._add_segment_quad_px(
                verts, window_size, px0, py0, px1, py1,
                rgba, line_width_px=self.ENVELOPE_LINE_WIDTH_PX,
            )

    def _build_frame_geom(self, window_size: tuple[int, int],
                          camera_along: float,
                          segments: list[RawSegment]) -> tuple[bytes, int]:
        verts: list = []
        x0, y0, x1, y1 = self._panel_rect_px(window_size)
        self._add_quad_px(verts, window_size, x0, y0, x1, y1, (0.06, 0.07, 0.10, 0.88))

        clipped_segments = []
        for segment in segments:
            clipped = self._clip_segment_to_visible_window(segment, camera_along)
            if clipped is not None:
                clipped_segments.append(clipped)

        floor_points, ceiling_points = self._envelope_points_from_segments(clipped_segments)
        envelope_points = floor_points + ceiling_points

        if envelope_points:
            local_min_y = min(sy for _u, sy in envelope_points)
            local_max_y = max(sy for _u, sy in envelope_points)
            pad = max(1.5, (local_max_y - local_min_y) * 0.12)
            section_min_y = local_min_y - pad
            section_max_y = local_max_y + pad
        else:
            section_min_y = self._world_min_y
            section_max_y = self._world_max_y

        self._add_envelope_polyline_px(
            verts, window_size, ceiling_points, section_min_y, section_max_y,
            (0.70, 0.88, 0.94, 0.96),
        )
        self._add_envelope_polyline_px(
            verts, window_size, floor_points, section_min_y, section_max_y,
            (0.45, 0.72, 0.82, 0.92),
        )

        border = 1.5
        border_color = (0.42, 0.64, 0.68, 0.70)
        self._add_quad_px(verts, window_size, x0, y0, x1, y0 + border, border_color)
        self._add_quad_px(verts, window_size, x0, y1 - border, x1, y1, border_color)
        self._add_quad_px(verts, window_size, x0, y0, x0 + border, y1, border_color)
        self._add_quad_px(verts, window_size, x1 - border, y0, x1, y1, border_color)

        return np.array(verts, dtype=np.float32).tobytes(), len(verts)

    # -- render-thread cache/scheduling -----------------------------------

    def _cache_get(self, key: tuple) -> list[RawSegment] | None:
        cached = self._raw_cache.get(key)
        if cached is None:
            return None
        self._raw_cache.move_to_end(key)
        return cached

    def _cache_put(self, key: tuple, segments: list[RawSegment]) -> None:
        self._raw_cache[key] = segments
        self._raw_cache.move_to_end(key)
        while len(self._raw_cache) > self.RAW_CACHE_LIMIT:
            self._raw_cache.popitem(last=False)

    def prime(self, camera_position: np.ndarray, camera_forward: np.ndarray,
              window_size: tuple[int, int] | None = None) -> None:
        """Build and optionally upload the initial profile before streaming.

        Chunk upload throughput must not decide whether the longitudinal
        map appears. Priming one small local view synchronously and uploading
        it immediately gives the panel a GPU-ready frame before any cave
        chunks are queued. Subsequent camera views still use the background
        executor. ``window_size`` is optional for CPU-only callers and tests.
        """
        raw_key, _camera_along = self._view_for_camera(
            (self.PANEL_WIDTH, self.PANEL_HEIGHT), camera_position, camera_forward
        )
        try:
            segments = self._build_raw_segments(raw_key)
        except Exception as exc:
            _LOG.exception("Initial longitudinal profile build failed: %s", exc)
            return
        self._cache_put(raw_key, segments)
        self._active_raw_key = raw_key
        self._active_segments = segments
        if not segments:
            _LOG.warning("Initial longitudinal profile contained no cave intersections.")

        if window_size is not None:
            profile_camera_along = self._camera_along_for_key(
                camera_position, raw_key
            )
            display_along = self._display_along(profile_camera_along)
            geom_bytes, vert_count = self._build_frame_geom(
                window_size, display_along, segments
            )
            self._upload_geom(geom_bytes, vert_count)
            self._uploaded_frame_key = (raw_key, window_size, display_along)

    def _offset_raw_key(self, raw_key: tuple, offset_steps: int) -> tuple:
        center_along, lateral, angle = raw_key
        return center_along + self.RAW_ALONG_STEP * offset_steps, lateral, angle

    def _process_completed_futures(self) -> None:
        completed_keys = [
            raw_key for raw_key, future in self._pending_raw_builds.items()
            if future.done()
        ]
        for raw_key in completed_keys:
            future = self._pending_raw_builds.pop(raw_key)
            if future.cancelled():
                continue
            try:
                segments = future.result()
            except concurrent.futures.CancelledError:
                continue
            except Exception as exc:
                _LOG.exception("Longitudinal profile build failed for %s: %s", raw_key, exc)
                continue
            self._cache_put(raw_key, segments)
            # Never replace a useful visible profile with an empty result.
            # With a single worker, non-empty completions arrive in request
            # order and can be displayed immediately while the queued latest
            # request catches up.
            if segments:
                self._active_raw_key = raw_key
                self._active_segments = segments

    def _schedule_raw_build(self, raw_key: tuple) -> bool:
        if raw_key in self._raw_cache:
            return True
        if raw_key in self._pending_raw_builds:
            self._pending_raw_builds.move_to_end(raw_key)
            return True

        while len(self._pending_raw_builds) >= self.RAW_PENDING_LIMIT:
            cancelled_key = None
            for old_key, old_future in self._pending_raw_builds.items():
                if old_future.cancel():
                    cancelled_key = old_key
                    break
            if cancelled_key is None:
                # Every slot is already executing. Do not submit another
                # job: rapidly changing camera keys would otherwise grow an
                # unbounded executor backlog, leaving the current profile
                # stuck behind stale views that are no longer useful.
                return False
            self._pending_raw_builds.pop(cancelled_key, None)

        self._pending_raw_builds[raw_key] = self._slice_executor.submit(
            self._build_raw_segments, raw_key, True
        )
        return True

    def _schedule_prefetch_near(self, raw_key: tuple) -> None:
        for offset in range(1, self.PREFETCH_AHEAD_COUNT + 1):
            self._schedule_raw_build(self._offset_raw_key(raw_key, offset))
        for offset in range(1, self.PREFETCH_BEHIND_COUNT + 1):
            self._schedule_raw_build(self._offset_raw_key(raw_key, -offset))

    def _ensure_segments_for_view(self, raw_key: tuple) -> bool:
        # Remove obsolete work that has not started. The one running build is
        # allowed to finish and become the next visible update; the one queued
        # slot is continuously replaced with the newest camera request.
        for pending_key, future in list(self._pending_raw_builds.items()):
            if pending_key != raw_key and future.cancel():
                self._pending_raw_builds.pop(pending_key, None)

        self._process_completed_futures()

        cached = self._cache_get(raw_key)
        if cached is not None:
            if cached:
                self._active_raw_key = raw_key
                self._active_segments = cached
            self._schedule_prefetch_near(raw_key)
            return True

        self._schedule_raw_build(raw_key)
        return False

    # -- render ------------------------------------------------------------

    def _upload_geom(self, geom_bytes: bytes, vert_count: int) -> None:
        if len(geom_bytes) > self._max_verts * 6 * 4:
            self._vao.release()
            self._vbo.release()
            self._max_verts = max(self._max_verts * 2, vert_count)
            self._vbo = self.ctx.buffer(reserve=self._max_verts * 6 * 4)
            self._vao = self.ctx.vertex_array(
                self.program, [(self._vbo, "2f 4f", "in_pos", "in_color")]
            )
        self._vbo.write(geom_bytes)
        self._vert_count = vert_count

    def render(self, window_size: tuple[int, int],
               camera_position: np.ndarray,
               camera_forward: np.ndarray) -> None:
        raw_key, _camera_along = self._view_for_camera(
            window_size, camera_position, camera_forward
        )
        self._ensure_segments_for_view(raw_key)

        # Visibility has exactly one rule: draw the most recently completed
        # non-empty profile. It remains on screen while the feeder builds a
        # replacement, whether the diver is moving or stationary.
        if self._active_raw_key is not None and self._active_segments:
            profile_camera_along = self._camera_along_for_key(
                camera_position, self._active_raw_key
            )
            display_along = self._display_along(profile_camera_along)
            frame_key = (self._active_raw_key, window_size, display_along)
            if frame_key != self._uploaded_frame_key:
                geom_bytes, vert_count = self._build_frame_geom(
                    window_size, display_along, self._active_segments
                )
                self._upload_geom(geom_bytes, vert_count)
                self._uploaded_frame_key = frame_key
        else:
            # This is limited to maps whose initial synchronous prime found no
            # intersections. Keep the panel location visible while waiting for
            # the first useful feeder result.
            empty_frame_key = (None, window_size, raw_key)
            if empty_frame_key != self._uploaded_frame_key:
                geom_bytes, vert_count = self._build_frame_geom(
                    window_size, self._display_along(_camera_along), []
                )
                self._upload_geom(geom_bytes, vert_count)
                self._uploaded_frame_key = empty_frame_key

        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        if self._vert_count:
            self._vao.render(moderngl.TRIANGLES, vertices=self._vert_count)
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    def release(self) -> None:
        self._profile_stop_event.set()
        for future in self._pending_raw_builds.values():
            future.cancel()
        self._pending_raw_builds.clear()
        self._raw_cache.clear()
        with self._triangle_cache_lock:
            self._triangle_cache.clear()
        self._active_segments.clear()
        self._slice_executor.shutdown(wait=False, cancel_futures=True)
        for attr_name in ("_vao", "_vbo", "program"):
            resource = getattr(self, attr_name, None)
            if resource is None:
                continue
            try:
                resource.release()
            except Exception:
                pass
            setattr(self, attr_name, None)
