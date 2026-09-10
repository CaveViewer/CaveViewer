"""Pure policy and state for viewer streaming frames.

The native window gathers snapshots from the map and render-thread owners,
asks this component for decisions, and then performs the corresponding world
updates and GPU uploads itself.  This module owns no OpenGL objects and does
not import the window adapter.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
from typing import Callable

import numpy as np

from caveviewer.gui import view_culling


Cell = tuple[int, int, int]


@dataclass(frozen=True)
class UploadLimits:
    """Bounded render-thread work allowed for one streaming frame."""

    chunks: int
    operations_per_chunk: int
    time_budget_ms: float


@dataclass(frozen=True)
class UploadBudgetPolicy:
    """Normal, catch-up, and startup upload limits."""

    normal: UploadLimits
    catchup: UploadLimits = UploadLimits(2, 8, 8.0)
    startup: UploadLimits = UploadLimits(4, 8, 12.0)

    def limits_for(
        self,
        stats: Mapping[str, object] | None,
        *,
        startup_active: bool,
    ) -> UploadLimits:
        """Return the effective limits for an immutable world snapshot."""
        if startup_active:
            return _max_limits(self.normal, self.startup)
        if stats is not None:
            ready = max(0, int(stats.get("ready", 0)))
            wanted = max(0, int(stats.get("wanted", 0)))
            loaded = max(
                0,
                int(stats.get("loaded_wanted", stats.get("loaded", 0))),
            )
            failed = max(0, int(stats.get("failed_wanted", 0)))
            if ready > 0 and wanted - loaded - failed > 0:
                return _max_limits(self.normal, self.catchup)
        return self.normal


def _max_limits(left: UploadLimits, right: UploadLimits) -> UploadLimits:
    return UploadLimits(
        chunks=max(left.chunks, right.chunks),
        operations_per_chunk=max(
            left.operations_per_chunk,
            right.operations_per_chunk,
        ),
        time_budget_ms=max(left.time_budget_ms, right.time_budget_ms),
    )


def target_load_radius(
    base_radius: int,
    *,
    startup_active: bool,
    maximum_radius: int,
    startup_extra: int = 3,
    startup_maximum: int = 10,
) -> int:
    """Return the render radius plus any bounded startup prefetch margin."""
    base_radius = max(1, int(base_radius))
    if not startup_active:
        return base_radius
    maximum_radius = max(
        base_radius,
        min(int(maximum_radius), int(startup_maximum)),
    )
    return min(maximum_radius, base_radius + int(startup_extra))


def streaming_cell_priority_key(
    *,
    camera_position: Iterable[float],
    camera_forward: Iterable[float],
    chunk_size: float,
    window_size: tuple[int, int],
    fov_deg: float,
) -> Callable[[Cell], tuple[int, int, float, float, float]]:
    """Rank eligible cells by view relevance and then camera distance."""
    chunk_size = max(1e-6, float(chunk_size))
    position = np.asarray(camera_position, dtype=np.float64)
    forward = np.asarray(camera_forward, dtype=np.float64)
    forward_norm = float(np.linalg.norm(forward))
    if forward_norm < 1e-9:
        forward = np.array([0.0, 0.0, -1.0], dtype=np.float64)
    else:
        forward = forward / forward_norm

    width, height = window_size
    aspect = max(1.0, float(width) / max(1.0, float(height)))
    half_fov = math.radians(max(1.0, min(179.0, float(fov_deg))) * 0.5)
    visible_cone_tan = math.tan(half_fov) * aspect * 1.25
    chunk_size_sq = chunk_size * chunk_size
    camera_x, camera_y, camera_z = (float(value) for value in position)
    forward_x, forward_y, forward_z = (float(value) for value in forward)

    def priority(cell: Cell) -> tuple[int, int, float, float, float]:
        center_x = (cell[0] + 0.5) * chunk_size
        center_y = (cell[1] + 0.5) * chunk_size
        center_z = (cell[2] + 0.5) * chunk_size
        rel_x = center_x - camera_x
        rel_y = center_y - camera_y
        rel_z = center_z - camera_z
        depth = rel_x * forward_x + rel_y * forward_y + rel_z * forward_z
        distance_sq = rel_x * rel_x + rel_y * rel_y + rel_z * rel_z
        lateral_sq = max(0.0, distance_sq - depth * depth)
        front_penalty = 0 if depth >= -chunk_size else 1
        cone_depth = max(chunk_size, depth)
        visible_radius = cone_depth * visible_cone_tan + chunk_size
        visible_penalty = int(
            front_penalty != 0 or lateral_sq > visible_radius * visible_radius
        )
        return (
            front_penalty,
            visible_penalty,
            max(0.0, depth / chunk_size),
            lateral_sq / max(chunk_size_sq, depth * depth),
            distance_sq / chunk_size_sq,
        )

    return priority


def initial_chunk_load_needed(
    stats: Mapping[str, object],
    max_loaded_chunks: int,
    *,
    minimum_chunks: int = 6,
) -> int:
    """Return how many currently wanted cells must reach a terminal state."""
    total_available = max(1, int(stats.get("total_available", 1)))
    wanted = max(1, int(stats.get("wanted", minimum_chunks)))
    return min(total_available, max(1, int(max_loaded_chunks)), wanted)


def initial_chunk_load_is_ready(
    stats: Mapping[str, object],
    max_loaded_chunks: int,
    *,
    minimum_chunks: int = 6,
) -> bool:
    loaded = max(0, int(stats.get("loaded_wanted", stats.get("loaded", 0))))
    failed = max(0, int(stats.get("failed_wanted", 0)))
    needed = initial_chunk_load_needed(
        stats,
        max_loaded_chunks,
        minimum_chunks=minimum_chunks,
    )
    return loaded + failed >= needed


def initial_chunk_load_progress(
    stats: Mapping[str, object],
    max_loaded_chunks: int,
    *,
    minimum_chunks: int = 6,
) -> float:
    """Return weighted CPU-decode/GPU-upload progress for startup UI."""
    loaded = max(0, int(stats.get("loaded_wanted", stats.get("loaded", 0))))
    ready = max(0, int(stats.get("ready", 0)))
    pending = max(0, int(stats.get("pending", 0)))
    failed = max(0, int(stats.get("failed_wanted", 0)))
    needed = initial_chunk_load_needed(
        stats,
        max_loaded_chunks,
        minimum_chunks=minimum_chunks,
    )
    effective = loaded + failed + 0.75 * ready + 0.25 * min(pending, needed)
    return max(0.0, min(1.0, effective / needed))


def texture_source_key(source: object) -> object:
    try:
        hash(source)
    except TypeError:
        return id(source)
    return source


def texture_sources_for_cells(
    cells: Iterable[Cell],
    *,
    manifest: Mapping[str, object],
    material_to_file: Mapping[str, object],
) -> set[object]:
    chunks = manifest.get("chunks", {})
    if not isinstance(chunks, Mapping):
        return set()
    sources: set[object] = set()
    for cell in cells:
        chunk_info = chunks.get(f"{cell[0]}_{cell[1]}_{cell[2]}")
        if not isinstance(chunk_info, Mapping):
            continue
        materials = chunk_info.get("materials", ())
        if not isinstance(materials, Iterable) or isinstance(materials, str):
            materials = ()
        for material in materials:
            source = material_to_file.get(str(material))
            if source:
                sources.add(texture_source_key(source))
    return sources


def texture_sources_for_visible_cells(
    visible_cells: Iterable[tuple[Cell, list]] | None,
    *,
    material_to_file: Mapping[str, object],
) -> set[object]:
    sources: set[object] = set()
    for _cell, vao_list in visible_cells or ():
        for _vao, _vbo, material_name, _texture in vao_list:
            source = material_to_file.get(str(material_name))
            if source:
                sources.add(texture_source_key(source))
    return sources


@dataclass(frozen=True)
class TextureReadiness:
    textures_ready: bool = True
    required_textures: int = 0
    resident_textures: int = 0
    missing_textures: int = 0
    visible_textures: int = 0
    resident_texture_bytes: int = 0
    resident_texture_budget_bytes: int = 0

    def as_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def texture_readiness(
    *,
    wanted_sources: set[object],
    visible_sources: set[object],
    resident_sources: set[object],
    exact_sources_known: bool,
    manager_stats: Mapping[str, object],
) -> TextureReadiness:
    required_sources = wanted_sources if wanted_sources else visible_sources
    required_count = len(required_sources)
    resident_count = max(
        0,
        int(manager_stats.get("unique_files_resident", required_count)),
    )
    missing_sources = (
        required_sources - resident_sources
        if exact_sources_known and required_sources
        else set()
    )
    ready = (
        not missing_sources
        if exact_sources_known and required_sources
        else required_count <= 0 or resident_count >= required_count
    )
    return TextureReadiness(
        textures_ready=ready,
        required_textures=required_count,
        resident_textures=resident_count,
        missing_textures=len(missing_sources),
        visible_textures=len(visible_sources),
        resident_texture_bytes=int(manager_stats.get("resident_texture_bytes", 0)),
        resident_texture_budget_bytes=int(
            manager_stats.get("resident_texture_budget_bytes", 0)
        ),
    )


@dataclass(frozen=True)
class CoverageStats:
    coverage_ready: bool = True
    expected_chunks: int = 0
    covered_chunks: int = 0
    missing_chunks: int = 0
    coverage_pct: float = 100.0

    def as_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def manifest_chunk_bounds(
    manifest: Mapping[str, object],
    cell: Cell,
) -> tuple[np.ndarray, np.ndarray] | None:
    chunks = manifest.get("chunks", {})
    chunk_info = (
        chunks.get(f"{cell[0]}_{cell[1]}_{cell[2]}")
        if isinstance(chunks, Mapping)
        else None
    )
    if not isinstance(chunk_info, Mapping):
        return None
    try:
        bounds_min = np.asarray(chunk_info["bounds_min"], dtype=np.float64)
        bounds_max = np.asarray(chunk_info["bounds_max"], dtype=np.float64)
    except (KeyError, TypeError, ValueError):
        return None
    if bounds_min.shape != (3,) or bounds_max.shape != (3,):
        return None
    return bounds_min, bounds_max


def startup_visual_coverage(
    *,
    wanted_cells: frozenset[Cell],
    visible_cells: Iterable[tuple[Cell, list]] | None,
    failed_cells: frozenset[Cell],
    manifest: Mapping[str, object],
    view: np.ndarray | None,
    projection: np.ndarray | None,
) -> CoverageStats:
    if view is None or projection is None or not wanted_cells:
        return CoverageStats()
    planes = view_culling.frustum_planes(
        np.asarray(view, dtype=np.float64),
        np.asarray(projection, dtype=np.float64),
    )
    expected = {
        cell
        for cell in wanted_cells
        if (bounds := manifest_chunk_bounds(manifest, cell)) is not None
        and view_culling.aabb_inside_frustum(planes, bounds[0], bounds[1])
    }
    visible = {tuple(cell) for cell, _vao_list in visible_cells or ()}
    covered = len(expected & (visible | set(failed_cells)))
    missing = max(0, len(expected) - covered)
    return CoverageStats(
        coverage_ready=missing == 0,
        expected_chunks=len(expected),
        covered_chunks=covered,
        missing_chunks=missing,
        coverage_pct=(100.0 if not expected else 100.0 * covered / len(expected)),
    )


@dataclass(frozen=True)
class RoutePrefetchStats:
    active: bool = False
    ready: bool = True
    expected_cells: int = 0
    loaded_cells: int = 0
    pending_cells: int = 0
    failed_cells: int = 0
    missing_cells: int = 0
    coverage_pct: float = 100.0

    def as_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def route_prefetch_stats(
    prefetch_cells: frozenset[Cell],
    *,
    loaded_cells: frozenset[Cell] = frozenset(),
    pending_cells: frozenset[Cell] = frozenset(),
    failed_cells: frozenset[Cell] = frozenset(),
    world_available: bool = True,
) -> RoutePrefetchStats:
    if not prefetch_cells:
        return RoutePrefetchStats()
    if not world_available:
        return RoutePrefetchStats(
            active=True,
            ready=False,
            expected_cells=len(prefetch_cells),
            missing_cells=len(prefetch_cells),
            coverage_pct=0.0,
        )
    loaded = prefetch_cells & loaded_cells
    pending = prefetch_cells & pending_cells
    failed = prefetch_cells & failed_cells
    covered = len(loaded | failed)
    missing = max(0, len(prefetch_cells) - covered)
    return RoutePrefetchStats(
        active=True,
        ready=missing == 0,
        expected_cells=len(prefetch_cells),
        loaded_cells=len(loaded),
        pending_cells=len(pending),
        failed_cells=len(failed),
        missing_cells=missing,
        coverage_pct=100.0 * covered / max(1, len(prefetch_cells)),
    )


@dataclass
class ViewerStreamingRuntime:
    """Own startup-readiness state and pure per-frame streaming policy."""

    initial_chunks_loaded: bool = False
    initial_visual_ready: bool = False
    initial_visual_ready_frames: int = 0
    initial_visual_ready_visible_chunks: int = 0
    initial_visual_ready_required_textures: int = 0
    initial_visual_ready_resident_textures: int = 0
    initial_visual_ready_visible_textures: int = 0
    initial_visual_ready_missing_textures: int = 0
    initial_visual_ready_expected_chunks: int = 0
    initial_visual_ready_covered_chunks: int = 0
    initial_visual_ready_missing_chunks: int = 0
    initial_visual_ready_coverage_pct: float = 100.0
    initial_route_prefetch_expected_cells: int = 0
    initial_route_prefetch_loaded_cells: int = 0
    initial_route_prefetch_pending_cells: int = 0
    initial_route_prefetch_failed_cells: int = 0
    initial_route_prefetch_missing_cells: int = 0
    initial_route_prefetch_coverage_pct: float = 100.0
    initial_visual_ready_logged: bool = False

    def reset(self) -> None:
        """Reset all state associated with startup streaming readiness."""
        fresh = type(self)()
        self.__dict__.update(fresh.__dict__)

    def observe_visual_readiness(
        self,
        stats: Mapping[str, object],
        *,
        visible_chunk_count: int,
        max_loaded_chunks: int,
        upload_state_count: int,
        textures: TextureReadiness,
        coverage: CoverageStats,
        route: RoutePrefetchStats,
        settle_frames: int = 3,
    ) -> tuple[dict[str, object], bool]:
        """Advance startup readiness and return augmented telemetry."""
        was_ready = self.initial_visual_ready
        became_ready = False
        if not self.initial_visual_ready:
            settled = (
                self.initial_chunks_loaded
                and initial_chunk_load_is_ready(stats, max_loaded_chunks)
                and max(0, int(stats.get("pending", 0))) == 0
                and max(0, int(stats.get("ready", 0))) == 0
                and upload_state_count == 0
                and textures.textures_ready
                and coverage.coverage_ready
                and route.ready
            )
            self.initial_visual_ready_frames = (
                self.initial_visual_ready_frames + 1 if settled else 0
            )
            self.initial_visual_ready_visible_chunks = (
                int(visible_chunk_count) if settled else 0
            )
            self.initial_visual_ready_required_textures = (
                textures.required_textures if settled else 0
            )
            self.initial_visual_ready_resident_textures = textures.resident_textures
            self.initial_visual_ready_visible_textures = textures.visible_textures
            self.initial_visual_ready_missing_textures = textures.missing_textures
            self.initial_visual_ready_expected_chunks = coverage.expected_chunks
            self.initial_visual_ready_covered_chunks = coverage.covered_chunks
            self.initial_visual_ready_missing_chunks = coverage.missing_chunks
            self.initial_visual_ready_coverage_pct = coverage.coverage_pct
            self._record_route(route)
            if self.initial_visual_ready_frames >= settle_frames:
                self.initial_visual_ready = True
                became_ready = True

        result = dict(stats)
        visual_stats = self._visual_stats(visible_chunk_count)
        if not was_ready:
            visual_stats.update(
                {
                    "visual_ready_required_textures": textures.required_textures,
                    "visual_ready_resident_textures": textures.resident_textures,
                    "visual_ready_visible_textures": textures.visible_textures,
                    "visual_ready_missing_textures": textures.missing_textures,
                    "visual_ready_expected_chunks": coverage.expected_chunks,
                    "visual_ready_covered_chunks": coverage.covered_chunks,
                    "visual_ready_missing_chunks": coverage.missing_chunks,
                    "visual_ready_coverage_pct": round(coverage.coverage_pct, 3),
                    "route_prefetch_expected_cells": route.expected_cells,
                    "route_prefetch_loaded_cells": route.loaded_cells,
                    "route_prefetch_pending_cells": route.pending_cells,
                    "route_prefetch_failed_cells": route.failed_cells,
                    "route_prefetch_missing_cells": route.missing_cells,
                    "route_prefetch_coverage_pct": round(route.coverage_pct, 3),
                }
            )
        result.update(visual_stats)
        return result, became_ready

    def _record_route(self, route: RoutePrefetchStats) -> None:
        self.initial_route_prefetch_expected_cells = route.expected_cells
        self.initial_route_prefetch_loaded_cells = route.loaded_cells
        self.initial_route_prefetch_pending_cells = route.pending_cells
        self.initial_route_prefetch_failed_cells = route.failed_cells
        self.initial_route_prefetch_missing_cells = route.missing_cells
        self.initial_route_prefetch_coverage_pct = route.coverage_pct

    def _visual_stats(self, visible_chunk_count: int) -> dict[str, object]:
        visible = (
            self.initial_visual_ready_visible_chunks
            if self.initial_visual_ready
            else int(visible_chunk_count)
        )
        return {
            "visual_ready": self.initial_visual_ready,
            "visual_ready_frames": self.initial_visual_ready_frames,
            "visual_ready_visible_chunks": visible,
            "visual_ready_required_textures": self.initial_visual_ready_required_textures,
            "visual_ready_resident_textures": self.initial_visual_ready_resident_textures,
            "visual_ready_visible_textures": self.initial_visual_ready_visible_textures,
            "visual_ready_missing_textures": self.initial_visual_ready_missing_textures,
            "visual_ready_expected_chunks": self.initial_visual_ready_expected_chunks,
            "visual_ready_covered_chunks": self.initial_visual_ready_covered_chunks,
            "visual_ready_missing_chunks": self.initial_visual_ready_missing_chunks,
            "visual_ready_coverage_pct": round(self.initial_visual_ready_coverage_pct, 3),
            "route_prefetch_expected_cells": self.initial_route_prefetch_expected_cells,
            "route_prefetch_loaded_cells": self.initial_route_prefetch_loaded_cells,
            "route_prefetch_pending_cells": self.initial_route_prefetch_pending_cells,
            "route_prefetch_failed_cells": self.initial_route_prefetch_failed_cells,
            "route_prefetch_missing_cells": self.initial_route_prefetch_missing_cells,
            "route_prefetch_coverage_pct": round(
                self.initial_route_prefetch_coverage_pct,
                3,
            ),
        }
