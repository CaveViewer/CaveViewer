"""Optional navigation metadata stored inside chunk-cache manifests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import heapq
import math
from typing import Any

import numpy as np

from caveviewer.core.navigation.centerline import (
    DEFAULT_CENTERLINE_ROUTE_CANDIDATE_LIMIT,
    DEFAULT_CENTERLINE_ROUTE_ENDPOINT_PERCENTILE,
    CenterlinePath,
    FootprintCell,
    Point,
    PointXZ,
    clearance_scores_for_footprint,
    footprint_cell_distance,
    footprint_path_is_circular,
    footprint_path_length,
    footprint_world_center,
    generate_centerline_paths,
    lowest_cost_footprint_path,
    navigable_footprint_neighbors,
    positive_manifest_float,
)
from caveviewer.core.navigation.voxel_cache import (
    NAVIGATION_VOXEL_CACHE_METHOD,
    NAVIGATION_VOXEL_CACHE_VERSION,
    load_cached_navigation_voxel_volume,
    supported_navigation_voxel_cache_identity,
)
from caveviewer.core.navigation.route import NavigationConfigurationError


NAVIGATION_METADATA_VERSION = 1
NAVIGATION_METADATA_METHOD = "footprint_centerline_paths_v1"
NAVIGATION_METADATA_KEY = "navigation"
NAVIGATION_SURFACE_Y_HISTOGRAM_BINS = 96
NAVIGATION_SURFACE_VERTICAL_BIN_SIZE_M = 0.25
# The navigation footprint is inferred from surface vertices.  The voxel
# builder later normalizes this candidate component to the mesh-backed cells
# it can actually sample, so retain the established span cap here while
# keeping that evidence-driven normalization explicit at cache build time.
NAVIGATION_SURFACE_SPAN_FILL_MAX_CELLS = 32
NAVIGATION_SURFACE_SPAN_SUPPORT_MAX_CELLS = 32
# A centerline cell may lie halfway across the widest span synthesized above.
# Search far enough to reach the surface evidence that justified that cell;
# otherwise one wide-but-valid passage drops every 3D route point because the
# route metadata is intentionally all-or-nothing.
NAVIGATION_SURFACE_Y_SEARCH_RADIUS_CELLS = (
    NAVIGATION_SURFACE_SPAN_FILL_MAX_CELLS // 2
)
# Suggested runtime Guided Dive Y smoothing radius for viewers that expose a
# preference. Metadata stores raw route samples; smoothing is applied by the
# route planner so the radius can be tuned without rebuilding cache.
NAVIGATION_ROUTE_Y_SMOOTHING_RADIUS_CELLS = 5
NAVIGATION_ENDPOINT_CENTERING_RADIUS_CELLS = 5
_NAVIGATION_SURFACE_BLOCK_VERTICES = 250_000
NAVIGATION_RECOVERY_HOTSPOT_METHOD = "component_recovery_hotspots_v1"
NAVIGATION_RECOVERY_HOTSPOT_LIMIT = 256
NAVIGATION_RECOVERY_HOTSPOT_MAX_RUN_CELLS = 64


@dataclass
class _SurfaceColumnProfile:
    low_y: float
    high_y: float
    occupied_y_bins: set[int] | np.ndarray


@dataclass(frozen=True)
class _SurfaceProfileIndex:
    global_low_y: float
    global_high_y: float
    columns: Mapping[FootprintCell, _SurfaceColumnProfile]
    vertical_bin_count: int = NAVIGATION_SURFACE_Y_HISTOGRAM_BINS


@dataclass(frozen=True)
class _SurfaceRouteYSample:
    y: float
    low_y: float
    high_y: float


@dataclass(frozen=True)
class _NavigationStart:
    position: Point
    label: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class _NavigationStartAnchor:
    position: Point
    source: str


@dataclass(frozen=True)
class _NavigationRouteCandidate:
    path: CenterlinePath
    selection_method: str
    candidate_count: int
    starts_at_navigation_start: bool = False
    navigation_start_distance_m: float | None = None
    starts_at_navigation_start_anchor: bool = False
    navigation_start_anchor_distance_m: float | None = None


def build_navigation_metadata(
    manifest: Mapping[str, Any],
    *,
    surface_positions: np.ndarray | None = None,
    navigation_start: Mapping[str, Any] | Sequence[object] | None = None,
    navigation_start_anchor: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return optional navigation metadata derived from an import manifest.

    The metadata is deliberately additive: callers can omit it when generation
    fails and every existing cache reader can continue to ignore it.
    """
    parsed_navigation_start = _parse_navigation_start(
        navigation_start
        if navigation_start is not None
        else manifest.get("navigation_start")
    )
    parsed_navigation_start_anchor = _parse_navigation_start_anchor(
        navigation_start_anchor
        if navigation_start_anchor is not None
        else manifest.get("navigation_start_anchor")
    )
    if parsed_navigation_start is not None:
        # A valid authored sidecar is an explicit override. The caller may
        # still provide the OBJ-order anchor so an invalid sidecar can fall
        # back safely, but only one ingress policy is published.
        parsed_navigation_start_anchor = None
    source_manifest = _navigation_manifest_from_surface_positions(
        manifest,
        surface_positions=surface_positions,
    )
    surface_profiles = _surface_vertical_profiles(
        surface_positions,
        cell_size=source_manifest.get("footprint_cell_size"),
    )
    paths = generate_centerline_paths(
        source_manifest,
        candidate_limit=DEFAULT_CENTERLINE_ROUTE_CANDIDATE_LIMIT,
        endpoint_percentile=DEFAULT_CENTERLINE_ROUTE_ENDPOINT_PERCENTILE,
    )
    eligible_paths = tuple(path for path in paths if len(path.cells) >= 2)
    anchored_path = _navigation_start_anchor_path(
        eligible_paths,
        navigation_start_anchor=parsed_navigation_start_anchor,
    )
    selected_candidates: list[_NavigationRouteCandidate] = []
    for path in eligible_paths:
        if parsed_navigation_start_anchor is not None and path is anchored_path:
            selected_candidates.extend(
                _navigation_start_anchor_route_candidates(
                    path,
                    navigation_start_anchor=parsed_navigation_start_anchor,
                )
            )
            continue
        if parsed_navigation_start is not None:
            selected_candidates.extend(
                _navigation_start_route_candidates(
                    path,
                    navigation_start=parsed_navigation_start,
                )
            )
            continue
        selected_candidates.append(
            _select_navigation_route_candidate(
                path,
                navigation_start=parsed_navigation_start,
                navigation_start_anchor=None,
            )
        )
    selected_candidates = tuple(
        sorted(
            selected_candidates,
            key=lambda candidate: _navigation_route_sort_key(
                candidate,
                navigation_start=parsed_navigation_start,
                navigation_start_anchor=parsed_navigation_start_anchor,
            ),
            reverse=True,
        )
    )
    routes = [
        _metadata_route_for_centerline_path(
            candidate.path,
            index=index,
            surface_profiles=surface_profiles,
            selection_method=candidate.selection_method,
            candidate_count=candidate.candidate_count,
            starts_at_navigation_start=candidate.starts_at_navigation_start,
            navigation_start_distance_m=candidate.navigation_start_distance_m,
            starts_at_navigation_start_anchor=(
                candidate.starts_at_navigation_start_anchor
            ),
            navigation_start_anchor_distance_m=(
                candidate.navigation_start_anchor_distance_m
            ),
            preserve_anchored_start_cell=(
                candidate.starts_at_navigation_start_anchor
                or candidate.starts_at_navigation_start
            ),
            voxel_sampling_cells=_parse_flat_cells(
                source_manifest.get("_voxel_sampling_cells")
            ),
        )
        for index, candidate in enumerate(selected_candidates)
    ]
    if not routes:
        return None

    recommended_route_id = _recommended_route_id(routes)
    metadata: dict[str, Any] = {
        "version": NAVIGATION_METADATA_VERSION,
        "method": NAVIGATION_METADATA_METHOD,
        "route_count": len(routes),
        "recommended_route_id": recommended_route_id,
        "footprint_cell_size": source_manifest.get("footprint_cell_size"),
        "navigation_footprint_source": source_manifest.get(
            "navigation_footprint_source",
            "manifest_footprint",
        ),
        "surface_driven": surface_positions is not None,
        "routes": routes,
    }
    if parsed_navigation_start is not None:
        metadata["navigation_start"] = _navigation_start_payload(
            parsed_navigation_start
        )
    if parsed_navigation_start_anchor is not None:
        metadata["navigation_start_anchor"] = (
            _navigation_start_anchor_payload(parsed_navigation_start_anchor)
        )
    surface_cell_count = source_manifest.get("surface_footprint_cell_count")
    if surface_cell_count is not None:
        metadata["surface_footprint_cell_count"] = surface_cell_count
    return metadata


def cached_centerline_path(
    manifest: Mapping[str, Any],
    *,
    route_id: str | None = None,
    cache_dir: str | None = None,
) -> CenterlinePath | None:
    """Return the selected cached centerline path, if the manifest has one."""
    navigation = manifest.get(NAVIGATION_METADATA_KEY)
    if not isinstance(navigation, Mapping):
        return None
    if navigation.get("version") != NAVIGATION_METADATA_VERSION:
        return None
    if navigation.get("method") != NAVIGATION_METADATA_METHOD:
        return None

    routes = navigation.get("routes")
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        return None
    selected_route_id = (
        route_id
        if route_id is not None
        else _string_or_none(navigation.get("recommended_route_id"))
    )
    selected_route = None
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        if selected_route_id is None or route.get("id") == selected_route_id:
            selected_route = route
            break
    if selected_route is None:
        return None
    return _centerline_path_from_metadata_route(
        manifest,
        selected_route,
        cache_dir=cache_dir,
    )


def _select_navigation_route_candidate(
    path: CenterlinePath,
    *,
    navigation_start: _NavigationStart | None,
    navigation_start_anchor: _NavigationStartAnchor | None,
) -> _NavigationRouteCandidate:
    candidates: list[_NavigationRouteCandidate] = [
        _NavigationRouteCandidate(
            path=path,
            selection_method="clearance_candidate_v1",
            candidate_count=1,
        )
    ]
    diameter_candidate = _physical_diameter_route_candidate(path)
    if diameter_candidate is not None:
        candidates.append(diameter_candidate)
    if navigation_start_anchor is not None:
        anchor_candidate = _navigation_start_anchor_route_candidate(
            path,
            navigation_start_anchor=navigation_start_anchor,
        )
        if anchor_candidate is not None:
            candidates.append(anchor_candidate)
    elif navigation_start is not None:
        start_candidate = _navigation_start_route_candidate(
            path,
            navigation_start=navigation_start,
        )
        if start_candidate is not None:
            candidates.append(start_candidate)
        candidates = [
            _orient_candidate_from_navigation_start(
                candidate,
                navigation_start=navigation_start,
            )
            for candidate in candidates
        ]

    candidate_count = len(candidates)
    deduped = _dedupe_navigation_route_candidates(
        candidates,
        navigation_start=navigation_start,
        navigation_start_anchor=navigation_start_anchor,
    )
    best = max(
        deduped,
        key=lambda candidate: _navigation_route_sort_key(
            candidate,
            navigation_start=navigation_start,
            navigation_start_anchor=navigation_start_anchor,
        ),
    )
    return _navigation_route_candidate_with_count(best, candidate_count)


def _navigation_route_sort_key(
    candidate: _NavigationRouteCandidate,
    *,
    navigation_start: _NavigationStart | None,
    navigation_start_anchor: _NavigationStartAnchor | None,
) -> tuple[object, ...]:
    if navigation_start_anchor is not None:
        start_distance = (
            candidate.navigation_start_anchor_distance_m
            if candidate.navigation_start_anchor_distance_m is not None
            else math.inf
        )
        return (
            bool(candidate.starts_at_navigation_start_anchor),
            -float(start_distance),
            float(candidate.path.length_m),
            len(candidate.path.cells),
            int(candidate.path.component_size),
        )
    if navigation_start is not None:
        start_distance = (
            candidate.navigation_start_distance_m
            if candidate.navigation_start_distance_m is not None
            else math.inf
        )
        return (
            bool(candidate.starts_at_navigation_start),
            -float(start_distance),
            float(candidate.path.length_m),
            len(candidate.path.cells),
            int(candidate.path.component_size),
        )
    return (
        float(candidate.path.length_m),
        len(candidate.path.cells),
        int(candidate.path.component_size),
    )


def _physical_diameter_route_candidate(
    path: CenterlinePath,
) -> _NavigationRouteCandidate | None:
    component = path.component_cells
    if len(component) < 2:
        return None
    seed = path.cells[0] if path.cells else min(component)
    first_endpoint = _furthest_component_cell(
        component,
        seed,
        cell_size=path.footprint_cell_size,
    )
    second_endpoint = _furthest_component_cell(
        component,
        first_endpoint,
        cell_size=path.footprint_cell_size,
    )
    first_endpoint = _centered_component_cell(
        path,
        first_endpoint,
        search_radius_cells=NAVIGATION_ENDPOINT_CENTERING_RADIUS_CELLS,
    )
    second_endpoint = _centered_component_cell(
        path,
        second_endpoint,
        search_radius_cells=NAVIGATION_ENDPOINT_CENTERING_RADIUS_CELLS,
    )
    candidate_path = _candidate_path_between_cells(
        path,
        first_endpoint,
        second_endpoint,
    )
    if candidate_path is None:
        return None
    return _NavigationRouteCandidate(
        path=candidate_path,
        selection_method="physical_endpoint_diameter_v1",
        candidate_count=1,
    )


def _navigation_start_route_candidate(
    path: CenterlinePath,
    *,
    navigation_start: _NavigationStart,
) -> _NavigationRouteCandidate | None:
    component = path.component_cells
    if len(component) < 2:
        return None
    start_cell = _nearest_component_cell_for_point(
        path,
        navigation_start.position,
    )
    end_cell = _furthest_component_cell(
        component,
        start_cell,
        cell_size=path.footprint_cell_size,
    )
    end_cell = _centered_component_cell(
        path,
        end_cell,
        search_radius_cells=NAVIGATION_ENDPOINT_CENTERING_RADIUS_CELLS,
    )
    candidate_path = _candidate_path_between_cells(path, start_cell, end_cell)
    if candidate_path is None:
        return None
    return _NavigationRouteCandidate(
        path=candidate_path,
        selection_method="navigation_start_to_farthest_endpoint_v1",
        candidate_count=1,
        starts_at_navigation_start=True,
        navigation_start_distance_m=_point_to_cell_center_distance_m(
            path,
            navigation_start.position,
            candidate_path.cells[0],
        ),
    )


def _navigation_start_route_candidates(
    path: CenterlinePath,
    *,
    navigation_start: _NavigationStart,
) -> tuple[_NavigationRouteCandidate, ...]:
    """Return terminal alternatives from one executable map start."""
    primary = _select_navigation_route_candidate(
        path,
        navigation_start=navigation_start,
        navigation_start_anchor=None,
    )
    candidates = [primary]
    diameter = _physical_diameter_route_candidate(path)
    if diameter is not None:
        start_cell = primary.path.cells[0]
        for end_cell in (diameter.path.cells[0], diameter.path.cells[-1]):
            if end_cell == start_cell:
                continue
            candidate_path = _candidate_path_between_cells(
                path,
                start_cell,
                end_cell,
            )
            if candidate_path is None:
                continue
            candidates.append(
                _NavigationRouteCandidate(
                    path=candidate_path,
                    selection_method=(
                        "navigation_start_to_diameter_endpoint_v1"
                    ),
                    candidate_count=1,
                    starts_at_navigation_start=True,
                    navigation_start_distance_m=(
                        _point_to_cell_center_distance_m(
                            path,
                            navigation_start.position,
                            candidate_path.cells[0],
                        )
                    ),
                )
            )
    deduped = _dedupe_navigation_route_candidates(
        candidates,
        navigation_start=navigation_start,
        navigation_start_anchor=None,
    )
    count = len(deduped)
    return tuple(
        _navigation_route_candidate_with_count(candidate, count)
        for candidate in sorted(
            deduped,
            key=lambda candidate: _navigation_route_sort_key(
                candidate,
                navigation_start=navigation_start,
                navigation_start_anchor=None,
            ),
            reverse=True,
        )
    )


def _navigation_start_anchor_path(
    paths: Sequence[CenterlinePath],
    *,
    navigation_start_anchor: _NavigationStartAnchor | None,
) -> CenterlinePath | None:
    if navigation_start_anchor is None or not paths:
        return None
    return min(
        paths,
        key=lambda path: (
            min(
                _point_to_cell_center_distance_m(
                    path,
                    navigation_start_anchor.position,
                    cell,
                )
                for cell in path.component_cells
            ),
            -float(path.length_m),
            path.cells,
        ),
    )


def _navigation_start_anchor_route_candidate(
    path: CenterlinePath,
    *,
    navigation_start_anchor: _NavigationStartAnchor,
) -> _NavigationRouteCandidate | None:
    component = path.component_cells
    if len(component) < 2:
        return None
    # Keep the first discrete cell attached to OBJ vertex zero. The 3D route
    # sample is centered inside that cell later without advancing along the
    # cave merely to gain more 2D clearance.
    start_cell = _nearest_component_cell_for_point(
        path,
        navigation_start_anchor.position,
    )
    end_cell = _furthest_component_cell(
        component,
        start_cell,
        cell_size=path.footprint_cell_size,
    )
    end_cell = _centered_component_cell(
        path,
        end_cell,
        search_radius_cells=NAVIGATION_ENDPOINT_CENTERING_RADIUS_CELLS,
    )
    candidate_path = _candidate_path_between_cells(path, start_cell, end_cell)
    if candidate_path is None:
        return None
    return _NavigationRouteCandidate(
        path=candidate_path,
        selection_method="obj_source_anchor_to_farthest_endpoint_v1",
        candidate_count=1,
        starts_at_navigation_start_anchor=True,
        navigation_start_anchor_distance_m=_point_to_cell_center_distance_m(
            path,
            navigation_start_anchor.position,
            candidate_path.cells[0],
        ),
    )


def _navigation_start_anchor_route_candidates(
    path: CenterlinePath,
    *,
    navigation_start_anchor: _NavigationStartAnchor,
) -> tuple[_NavigationRouteCandidate, ...]:
    """Return geometry-derived terminal alternatives from the OBJ entrance.

    A 2D footprint can merge stacked passages, so its single farthest endpoint
    is only a candidate. Include both independently derived diameter endpoints;
    cache-time 3D voxel and exact mesh certification will reject disconnected
    choices, then select the longest surviving non-circular route.
    """
    primary = _select_navigation_route_candidate(
        path,
        navigation_start=None,
        navigation_start_anchor=navigation_start_anchor,
    )
    candidates = [primary]
    diameter = _physical_diameter_route_candidate(path)
    if diameter is not None:
        start_cell = primary.path.cells[0]
        for end_cell in (diameter.path.cells[0], diameter.path.cells[-1]):
            if end_cell == start_cell:
                continue
            candidate_path = _candidate_path_between_cells(
                path,
                start_cell,
                end_cell,
            )
            if candidate_path is None:
                continue
            candidates.append(
                _NavigationRouteCandidate(
                    path=candidate_path,
                    selection_method=(
                        "obj_source_anchor_to_diameter_endpoint_v1"
                    ),
                    candidate_count=1,
                    starts_at_navigation_start_anchor=True,
                    navigation_start_anchor_distance_m=(
                        _point_to_cell_center_distance_m(
                            path,
                            navigation_start_anchor.position,
                            candidate_path.cells[0],
                        )
                    ),
                )
            )
    deduped = _dedupe_navigation_route_candidates(
        candidates,
        navigation_start=None,
        navigation_start_anchor=navigation_start_anchor,
    )
    count = len(deduped)
    return tuple(
        _navigation_route_candidate_with_count(candidate, count)
        for candidate in sorted(
            deduped,
            key=lambda candidate: _navigation_route_sort_key(
                candidate,
                navigation_start=None,
                navigation_start_anchor=navigation_start_anchor,
            ),
            reverse=True,
        )
    )


def _candidate_path_between_cells(
    path: CenterlinePath,
    start_cell: FootprintCell,
    end_cell: FootprintCell,
) -> CenterlinePath | None:
    route_cells = lowest_cost_footprint_path(
        path.component_cells,
        start_cell,
        end_cell,
        path.clearance_scores,
    )
    if len(route_cells) < 2:
        return None
    return _path_with_cells(path, route_cells)


def _orient_candidate_from_navigation_start(
    candidate: _NavigationRouteCandidate,
    *,
    navigation_start: _NavigationStart,
) -> _NavigationRouteCandidate:
    cells = candidate.path.cells
    if len(cells) < 2:
        return candidate
    first_distance = _point_to_cell_center_distance_m(
        candidate.path,
        navigation_start.position,
        cells[0],
    )
    last_distance = _point_to_cell_center_distance_m(
        candidate.path,
        navigation_start.position,
        cells[-1],
    )
    if last_distance < first_distance:
        return _NavigationRouteCandidate(
            path=_path_with_cells(candidate.path, tuple(reversed(cells))),
            selection_method=candidate.selection_method,
            candidate_count=candidate.candidate_count,
            starts_at_navigation_start=candidate.starts_at_navigation_start,
            navigation_start_distance_m=last_distance,
        )
    return _NavigationRouteCandidate(
        path=candidate.path,
        selection_method=candidate.selection_method,
        candidate_count=candidate.candidate_count,
        starts_at_navigation_start=candidate.starts_at_navigation_start,
        navigation_start_distance_m=first_distance,
    )


def _dedupe_navigation_route_candidates(
    candidates: Sequence[_NavigationRouteCandidate],
    *,
    navigation_start: _NavigationStart | None,
    navigation_start_anchor: _NavigationStartAnchor | None,
) -> tuple[_NavigationRouteCandidate, ...]:
    best_by_cells: dict[tuple[FootprintCell, ...], _NavigationRouteCandidate] = {}
    for candidate in candidates:
        cells = candidate.path.cells
        canonical = min(cells, tuple(reversed(cells)))
        previous = best_by_cells.get(canonical)
        if previous is None or _navigation_route_sort_key(
            candidate,
            navigation_start=navigation_start,
            navigation_start_anchor=navigation_start_anchor,
        ) > _navigation_route_sort_key(
            previous,
            navigation_start=navigation_start,
            navigation_start_anchor=navigation_start_anchor,
        ):
            best_by_cells[canonical] = candidate
            continue
    return tuple(best_by_cells.values())


def _navigation_route_candidate_with_count(
    candidate: _NavigationRouteCandidate,
    candidate_count: int,
) -> _NavigationRouteCandidate:
    return _NavigationRouteCandidate(
        path=candidate.path,
        selection_method=candidate.selection_method,
        candidate_count=max(1, int(candidate_count)),
        starts_at_navigation_start=candidate.starts_at_navigation_start,
        navigation_start_distance_m=candidate.navigation_start_distance_m,
        starts_at_navigation_start_anchor=(
            candidate.starts_at_navigation_start_anchor
        ),
        navigation_start_anchor_distance_m=(
            candidate.navigation_start_anchor_distance_m
        ),
    )


def _furthest_component_cell(
    component: frozenset[FootprintCell],
    start: FootprintCell,
    *,
    cell_size: float,
) -> FootprintCell:
    distances = _component_distances_from(
        component,
        start,
        cell_size=cell_size,
        max_distance_m=None,
    )
    return max(
        distances,
        key=lambda cell: (
            distances[cell],
            footprint_cell_distance(start, cell),
            cell,
        ),
    )


def _centered_component_cell(
    path: CenterlinePath,
    cell: FootprintCell,
    *,
    search_radius_cells: int,
) -> FootprintCell:
    distances = _component_distances_from(
        path.component_cells,
        cell,
        cell_size=path.footprint_cell_size,
        max_distance_m=(
            max(0.0, float(search_radius_cells))
            * float(path.footprint_cell_size)
        ),
    )
    if not distances:
        return cell
    return max(
        distances,
        key=lambda candidate: (
            path.clearance_scores.get(candidate, 0),
            -distances[candidate],
            -footprint_cell_distance(cell, candidate),
            candidate,
        ),
    )


def _component_distances_from(
    component: frozenset[FootprintCell],
    start: FootprintCell,
    *,
    cell_size: float,
    max_distance_m: float | None,
) -> dict[FootprintCell, float]:
    if not component:
        return {}
    if start not in component:
        start = min(
            component,
            key=lambda cell: (footprint_cell_distance(start, cell), cell),
        )
    max_distance = None if max_distance_m is None else max(0.0, float(max_distance_m))
    frontier: list[tuple[float, FootprintCell]] = [(0.0, start)]
    distances: dict[FootprintCell, float] = {start: 0.0}
    while frontier:
        current_distance, current = heapq.heappop(frontier)
        if current_distance > distances[current]:
            continue
        if max_distance is not None and current_distance > max_distance:
            continue
        for neighbor in navigable_footprint_neighbors(current, component):
            next_distance = (
                current_distance
                + footprint_cell_distance(current, neighbor) * float(cell_size)
            )
            if max_distance is not None and next_distance > max_distance:
                continue
            if next_distance >= distances.get(neighbor, math.inf):
                continue
            distances[neighbor] = next_distance
            heapq.heappush(frontier, (next_distance, neighbor))
    return distances


def _nearest_component_cell_for_point(
    path: CenterlinePath,
    point: Point,
) -> FootprintCell:
    return min(
        path.component_cells,
        key=lambda cell: (
            _point_to_cell_center_distance_m(path, point, cell),
            cell,
        ),
    )


def _point_to_cell_center_distance_m(
    path: CenterlinePath,
    point: Point,
    cell: FootprintCell,
) -> float:
    center = path.centers.get(cell, footprint_world_center(cell, path.footprint_cell_size))
    return math.hypot(float(point[0]) - center[0], float(point[2]) - center[1])


def _path_with_cells(
    path: CenterlinePath,
    cells: tuple[FootprintCell, ...],
) -> CenterlinePath:
    return CenterlinePath(
        source=path.source,
        footprint_cell_size=path.footprint_cell_size,
        footprint_cell_count=path.footprint_cell_count,
        component_size=path.component_size,
        component_cells=path.component_cells,
        cells=tuple(cells),
        centers=path.centers,
        clearance_scores=path.clearance_scores,
        endpoint_percentile=path.endpoint_percentile,
        endpoint_threshold_clearance_cells=path.endpoint_threshold_clearance_cells,
        length_m=footprint_path_length(tuple(cells), path.centers),
        cached_points=path.cached_points,
        cached_y_ranges=path.cached_y_ranges,
        cached_clearance_margins=path.cached_clearance_margins,
        cached_recovery_hotspots=path.cached_recovery_hotspots,
        cached_voxel_volume=path.cached_voxel_volume,
        cached_voxel_metrics=path.cached_voxel_metrics,
    )


def _parse_navigation_start(value: object) -> _NavigationStart | None:
    if value is None:
        return None
    payload = value
    source = None
    label = None
    if isinstance(value, Mapping):
        nested = value.get("navigation_start", value.get("start"))
        if nested is not None:
            payload = nested
        source = _string_or_none(value.get("source"))
        label = _string_or_none(value.get("label"))

    position_value = payload
    if isinstance(payload, Mapping):
        position_value = payload.get("position", payload.get("point", payload.get("xyz")))
        source = _string_or_none(payload.get("source")) or source
        label = _string_or_none(payload.get("label")) or label

    position = _parse_point(position_value)
    if position is None:
        return None
    return _NavigationStart(position=position, label=label, source=source)


def _parse_point(value: object) -> Point | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if len(value) != 3:
        return None
    try:
        point = (float(value[0]), float(value[1]), float(value[2]))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(coordinate) for coordinate in point):
        return None
    return point


def _navigation_start_payload(navigation_start: _NavigationStart) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "position": [float(value) for value in navigation_start.position],
    }
    if navigation_start.label is not None:
        payload["label"] = navigation_start.label
    if navigation_start.source is not None:
        payload["source"] = navigation_start.source
    return payload


def _parse_navigation_start_anchor(value: object) -> _NavigationStartAnchor | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("OBJ navigation start anchor must be an object")
    expected_fields = {
        "position",
        "kind",
        "source",
        "source_vertex_index",
        "source_order",
        "executable",
        "attachment_required",
        "attachment_coordinate_space",
    }
    if set(value) != expected_fields:
        raise ValueError("OBJ navigation start anchor schema is malformed")
    position_value = value["position"]
    source = value["source"]
    source_vertex_index = value["source_vertex_index"]
    if (
        type(position_value) is not list
        or len(position_value) != 3
        or any(
            type(coordinate) not in (int, float)
            for coordinate in position_value
        )
        or type(source) is not str
        or not source.strip()
        or type(source_vertex_index) is not int
        or source_vertex_index != 0
        or value["kind"] != "obj_surface_vertex"
        or value["source_order"] != "obj_declaration_order"
        or value["executable"] is not False
        or value["attachment_required"] is not True
        or value["attachment_coordinate_space"] != "xyz"
    ):
        raise ValueError("OBJ navigation start anchor policy is malformed")
    position = tuple(float(coordinate) for coordinate in position_value)
    if not all(math.isfinite(coordinate) for coordinate in position):
        raise ValueError("OBJ navigation start anchor position is malformed")
    return _NavigationStartAnchor(position=position, source=source)


def _navigation_start_anchor_payload(
    anchor: _NavigationStartAnchor,
) -> dict[str, Any]:
    return {
        "position": [float(value) for value in anchor.position],
        "kind": "obj_surface_vertex",
        "source": anchor.source,
        "source_vertex_index": 0,
        "source_order": "obj_declaration_order",
        "executable": False,
        "attachment_required": True,
        "attachment_coordinate_space": "xyz",
    }


def _metadata_route_for_centerline_path(
    path: CenterlinePath,
    *,
    index: int,
    surface_profiles: _SurfaceProfileIndex | None = None,
    selection_method: str = "clearance_candidate_v1",
    candidate_count: int = 1,
    starts_at_navigation_start: bool = False,
    navigation_start_distance_m: float | None = None,
    starts_at_navigation_start_anchor: bool = False,
    navigation_start_anchor_distance_m: float | None = None,
    preserve_anchored_start_cell: bool = False,
    voxel_sampling_cells: Sequence[FootprintCell] = (),
) -> dict[str, Any]:
    route_id = f"centerline-{index}"
    component_cells = tuple(sorted(path.component_cells))
    route: dict[str, Any] = {
        "id": route_id,
        "kind": "centerline",
        "source": path.source,
        "selection_method": selection_method,
        "candidate_count": max(1, int(candidate_count)),
        "closed_loop": footprint_path_is_circular(path.cells),
        "length_m": path.length_m,
        "footprint_cell_size": path.footprint_cell_size,
        "footprint_cell_count": path.footprint_cell_count,
        "component_size": path.component_size,
        "component_cells": _flat_cells(component_cells),
        "cells": _flat_cells(path.cells),
        "endpoint_percentile": path.endpoint_percentile,
        "endpoint_threshold_clearance_cells": (
            path.endpoint_threshold_clearance_cells
        ),
    }
    support_cells = tuple(sorted(set(voxel_sampling_cells)))
    if support_cells:
        route["voxel_sampling_cells"] = _flat_cells(support_cells)
    if starts_at_navigation_start:
        route["starts_at_navigation_start"] = True
    if navigation_start_distance_m is not None:
        route["navigation_start_distance_m"] = max(
            0.0,
            float(navigation_start_distance_m),
        )
    if starts_at_navigation_start_anchor:
        route["starts_at_navigation_start_anchor"] = True
    if navigation_start_anchor_distance_m is not None:
        route["navigation_start_anchor_distance_m"] = max(
            0.0,
            float(navigation_start_anchor_distance_m),
        )
    (
        route_points,
        route_y_ranges,
        route_clearance_margins,
        interpolated_route_point_count,
    ) = _surface_route_points_for_path(
        path,
        surface_profiles=surface_profiles,
        preserve_anchored_start_cell=preserve_anchored_start_cell,
    )
    if route_points:
        route["points"] = _flat_points(route_points)
        route["point_source"] = (
            "surface_vertical_gap_interpolated_v2"
            if interpolated_route_point_count
            else "surface_vertical_gap_raw"
        )
        route["surface_route_raw_point_count"] = (
            len(route_points) - interpolated_route_point_count
        )
        route["surface_route_interpolated_point_count"] = int(
            interpolated_route_point_count
        )
        route["y_ranges"] = _flat_y_ranges(route_y_ranges)
        route["clearance_margins"] = [
            float(margin)
            for margin in route_clearance_margins
        ]
        route["runtime_y_smoothing"] = True
        route["recommended_smoothing_radius_cells"] = (
            NAVIGATION_ROUTE_Y_SMOOTHING_RADIUS_CELLS
        )
    component_y_ranges, missing_component_y_range_count = (
        _surface_component_y_ranges_for_path(
            path,
            component_cells=component_cells,
            surface_profiles=surface_profiles,
            route_y_ranges=route_y_ranges,
        )
    )
    if component_y_ranges:
        route["component_y_ranges"] = _flat_y_ranges(component_y_ranges)
        route["surface_component_missing_y_range_count"] = int(
            missing_component_y_range_count
        )
    component_vertical_gap_intervals = (
        _surface_component_vertical_gap_intervals_for_path(
            component_cells=component_cells,
            surface_profiles=surface_profiles,
        )
    )
    component_vertical_gap_seeds = tuple(
        (cell, (float(low_y) + float(high_y)) * 0.5)
        for cell, low_y, high_y in component_vertical_gap_intervals
    )
    if component_vertical_gap_seeds:
        route["component_vertical_gap_seeds"] = [
            coordinate
            for cell, y in component_vertical_gap_seeds
            for coordinate in (int(cell[0]), int(cell[1]), float(y))
        ]
        route["component_vertical_gap_intervals"] = [
            coordinate
            for cell, low_y, high_y in component_vertical_gap_intervals
            for coordinate in (
                int(cell[0]),
                int(cell[1]),
                float(low_y),
                float(high_y),
            )
        ]
        route["surface_component_vertical_gap_seed_count"] = len(
            component_vertical_gap_seeds
        )
        if surface_profiles is not None:
            route["surface_vertical_bin_size_m"] = float(
                (
                    surface_profiles.global_high_y
                    - surface_profiles.global_low_y
                )
                / max(1, int(surface_profiles.vertical_bin_count))
            )
    recovery_hotspots = _recovery_hotspots_for_path(
        path,
        component_cells=component_cells,
    )
    if recovery_hotspots:
        route["recovery_hotspots"] = recovery_hotspots
    return route


def _recovery_hotspots_for_path(
    path: CenterlinePath,
    *,
    component_cells: tuple[FootprintCell, ...],
) -> dict[str, Any] | None:
    if not component_cells:
        return None
    scored = [
        _recovery_hotspot_score(path, cell)
        for cell in component_cells
    ]
    scored = sorted(
        scored,
        key=lambda item: (
            item["score"],
            item["corridor_run_cells"],
            item["straight_run_cells"],
            item["clearance_score"],
            item["degree_score"],
            item["cell"],
        ),
        reverse=True,
    )
    hotspots = scored[:NAVIGATION_RECOVERY_HOTSPOT_LIMIT]
    if not hotspots:
        return None
    return {
        "version": 1,
        "method": NAVIGATION_RECOVERY_HOTSPOT_METHOD,
        "score_source": "geometry_only_v1",
        "cell_count": len(hotspots),
        "component_cell_count": len(component_cells),
        "limit": NAVIGATION_RECOVERY_HOTSPOT_LIMIT,
        "light_path_scores_available": False,
        "texture_feature_scores_available": False,
        "future_signal_slots": [
            "light_path_score",
            "texture_feature_score",
        ],
        "cells": _flat_cells(tuple(item["cell"] for item in hotspots)),
        "scores": [float(item["score"]) for item in hotspots],
        "clearance_scores": [
            float(item["clearance_score"]) for item in hotspots
        ],
        "straight_run_cells": [
            float(item["straight_run_cells"]) for item in hotspots
        ],
        "corridor_run_cells": [
            float(item["corridor_run_cells"]) for item in hotspots
        ],
        "degree_scores": [
            float(item["degree_score"]) for item in hotspots
        ],
    }


def _recovery_hotspot_score(
    path: CenterlinePath,
    cell: FootprintCell,
) -> dict[str, Any]:
    neighbors = navigable_footprint_neighbors(cell, path.component_cells)
    straight_run = _longest_straight_recovery_run_cells(path, cell)
    corridor_run = _longest_bidirectional_recovery_run_cells(path, cell)
    clearance = float(path.clearance_scores.get(cell, 1))
    degree = float(len(neighbors))
    score = (
        corridor_run
        + (0.75 * straight_run)
        + (0.50 * clearance)
        + (0.25 * degree)
    )
    return {
        "cell": cell,
        "score": float(score),
        "clearance_score": float(clearance),
        "straight_run_cells": float(straight_run),
        "corridor_run_cells": float(corridor_run),
        "degree_score": float(degree),
    }


def _longest_straight_recovery_run_cells(
    path: CenterlinePath,
    cell: FootprintCell,
) -> float:
    return max(
        (
            _straight_recovery_run_cells(path, cell, direction)
            for direction in _NAVIGATION_RECOVERY_DIRECTIONS
        ),
        default=0.0,
    )


def _longest_bidirectional_recovery_run_cells(
    path: CenterlinePath,
    cell: FootprintCell,
) -> float:
    best = 0.0
    for direction in _NAVIGATION_RECOVERY_DIRECTIONS[:4]:
        reverse = (-direction[0], -direction[1])
        best = max(
            best,
            _straight_recovery_run_cells(path, cell, direction)
            + _straight_recovery_run_cells(path, cell, reverse),
        )
    return float(best)


_NAVIGATION_RECOVERY_DIRECTIONS: tuple[tuple[int, int], ...] = (
    (1, 0),
    (1, 1),
    (0, 1),
    (-1, 1),
    (-1, 0),
    (-1, -1),
    (0, -1),
    (1, -1),
)


def _straight_recovery_run_cells(
    path: CenterlinePath,
    cell: FootprintCell,
    direction: tuple[int, int],
) -> float:
    dx, dz = direction
    current = cell
    distance = 0.0
    while True:
        if distance >= NAVIGATION_RECOVERY_HOTSPOT_MAX_RUN_CELLS:
            break
        next_cell = (current[0] + dx, current[1] + dz)
        if next_cell not in path.component_cells:
            break
        if next_cell not in navigable_footprint_neighbors(
            current,
            path.component_cells,
        ):
            break
        distance += footprint_cell_distance(current, next_cell)
        current = next_cell
    return float(min(distance, float(NAVIGATION_RECOVERY_HOTSPOT_MAX_RUN_CELLS)))


def _recommended_route_id(routes: Sequence[Mapping[str, Any]]) -> str:
    for route in routes:
        if not bool(route.get("closed_loop")):
            route_id = _string_or_none(route.get("id"))
            if route_id is not None:
                return route_id
    fallback_id = _string_or_none(routes[0].get("id"))
    return fallback_id or "centerline-0"


def _centerline_path_from_metadata_route(
    manifest: Mapping[str, Any],
    route: Mapping[str, Any],
    *,
    cache_dir: str | None = None,
) -> CenterlinePath | None:
    try:
        cell_size = positive_manifest_float(
            route.get("footprint_cell_size", manifest.get("footprint_cell_size")),
            "navigation route footprint_cell_size",
        )
        route_cells = _parse_flat_cells(route.get("cells"))
        component_cells = _parse_flat_cells(route.get("component_cells"))
        if not component_cells:
            component_cells = route_cells
        if len(route_cells) < 2 or not component_cells:
            return None
        component = frozenset((*component_cells, *route_cells))
        centers: dict[FootprintCell, PointXZ] = {
            cell: footprint_world_center(cell, cell_size)
            for cell in component
        }
        route_points = _parse_flat_points(route.get("points"))
        cached_points = None
        if len(route_points) == len(route_cells):
            cached_points = {
                cell: point
                for cell, point in zip(route_cells, route_points, strict=False)
            }
            for cell, point in zip(route_cells, route_points, strict=False):
                centers[cell] = (point[0], point[2])
        component_y_ranges = _parse_flat_y_ranges(route.get("component_y_ranges"))
        cached_y_ranges = None
        if len(component_y_ranges) == len(component_cells):
            cached_y_ranges = {
                cell: y_range
                for cell, y_range in zip(
                    component_cells,
                    component_y_ranges,
                    strict=False,
                )
            }
        route_y_ranges = _parse_flat_y_ranges(route.get("y_ranges"))
        if len(route_y_ranges) == len(route_cells):
            route_cell_y_ranges = {
                cell: y_range
                for cell, y_range in zip(
                    route_cells,
                    route_y_ranges,
                    strict=False,
                )
            }
            if cached_y_ranges is None:
                cached_y_ranges = route_cell_y_ranges
            else:
                cached_y_ranges.update(route_cell_y_ranges)
        route_clearance_margins = _parse_float_sequence(route.get("clearance_margins"))
        cached_clearance_margins = None
        if len(route_clearance_margins) == len(route_cells):
            cached_clearance_margins = {
                cell: max(0.0, float(margin))
                for cell, margin in zip(
                    route_cells,
                    route_clearance_margins,
                    strict=False,
                )
            }
        cached_recovery_hotspots = _parse_recovery_hotspots(
            route.get("recovery_hotspots")
        )
        cached_voxel_metrics = _parse_voxel_metrics(
            route.get("voxel_corridor")
        )
        route_id = _string_or_none(route.get("id"))
        cached_voxel_volume = (
            None
            if cache_dir is None or route_id is None
            else load_cached_navigation_voxel_volume(
                cache_dir,
                manifest,
                route_id,
            )
        )
        clearance_scores = clearance_scores_for_footprint(component)
        length_m = _float_or_default(
            route.get("length_m"),
            footprint_path_length(route_cells, centers),
        )
        footprint_cell_count = _int_or_default(
            route.get("footprint_cell_count"),
            _manifest_footprint_cell_count(manifest),
        )
        endpoint_percentile = _float_or_default(
            route.get("endpoint_percentile"),
            DEFAULT_CENTERLINE_ROUTE_ENDPOINT_PERCENTILE,
        )
        endpoint_threshold = _int_or_default(
            route.get("endpoint_threshold_clearance_cells"),
            1,
        )
    except Exception:
        return None

    return CenterlinePath(
        source="cached_navigation_metadata",
        footprint_cell_size=cell_size,
        footprint_cell_count=footprint_cell_count,
        component_size=len(component),
        component_cells=component,
        cells=tuple(route_cells),
        centers=centers,
        clearance_scores=clearance_scores,
        endpoint_percentile=endpoint_percentile,
        endpoint_threshold_clearance_cells=endpoint_threshold,
        length_m=length_m,
        cached_points=cached_points,
        cached_y_ranges=cached_y_ranges,
        cached_clearance_margins=cached_clearance_margins,
        cached_recovery_hotspots=cached_recovery_hotspots,
        cached_voxel_volume=cached_voxel_volume,
        cached_voxel_metrics=cached_voxel_metrics,
    )


def _flat_cells(cells: Sequence[FootprintCell] | frozenset[FootprintCell]) -> list[int]:
    flat: list[int] = []
    for x, z in cells:
        flat.extend((int(x), int(z)))
    return flat


def _parse_flat_cells(value: object) -> tuple[FootprintCell, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    if len(value) % 2 != 0:
        return ()
    cells: list[FootprintCell] = []
    for index in range(0, len(value), 2):
        try:
            cells.append((int(value[index]), int(value[index + 1])))
        except (TypeError, ValueError):
            return ()
    return tuple(cells)


def _flat_points(points: Sequence[Point]) -> list[float]:
    flat: list[float] = []
    for x, y, z in points:
        flat.extend((float(x), float(y), float(z)))
    return flat


def _flat_y_ranges(y_ranges: Sequence[tuple[float, float]]) -> list[float]:
    flat: list[float] = []
    for low_y, high_y in y_ranges:
        flat.extend((float(low_y), float(high_y)))
    return flat


def _parse_flat_points(value: object) -> tuple[Point, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    if len(value) % 3 != 0:
        return ()
    points: list[Point] = []
    for index in range(0, len(value), 3):
        try:
            points.append(
                (
                    float(value[index]),
                    float(value[index + 1]),
                    float(value[index + 2]),
                )
            )
        except (TypeError, ValueError):
            return ()
    return tuple(points)


def _parse_flat_y_ranges(value: object) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    if len(value) % 2 != 0:
        return ()
    ranges: list[tuple[float, float]] = []
    for index in range(0, len(value), 2):
        try:
            low_y = float(value[index])
            high_y = float(value[index + 1])
        except (TypeError, ValueError):
            return ()
        ranges.append((min(low_y, high_y), max(low_y, high_y)))
    return tuple(ranges)


def _parse_float_sequence(value: object) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    floats: list[float] = []
    for item in value:
        try:
            floats.append(float(item))
        except (TypeError, ValueError):
            return ()
    return tuple(floats)


def _parse_recovery_hotspots(
    value: object,
) -> dict[FootprintCell, dict[str, float]] | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("version") != 1:
        return None
    if value.get("method") != NAVIGATION_RECOVERY_HOTSPOT_METHOD:
        return None
    cells = _parse_flat_cells(value.get("cells"))
    scores = _parse_float_sequence(value.get("scores"))
    if not cells or len(scores) != len(cells):
        return None
    clearance_scores = _parse_float_sequence(value.get("clearance_scores"))
    straight_runs = _parse_float_sequence(value.get("straight_run_cells"))
    corridor_runs = _parse_float_sequence(value.get("corridor_run_cells"))
    degree_scores = _parse_float_sequence(value.get("degree_scores"))
    available_volumes = _parse_float_sequence(
        value.get("available_volume_m3")
    )
    volume_per_route = _parse_float_sequence(
        value.get("volume_per_route_m")
    )
    voxel_clearance = _parse_float_sequence(
        value.get("voxel_mean_clearance_m")
    )
    parsed: dict[FootprintCell, dict[str, float]] = {}
    for index, cell in enumerate(cells):
        hotspot = {"score": float(scores[index])}
        if index < len(clearance_scores):
            hotspot["clearance_score"] = float(clearance_scores[index])
        if index < len(straight_runs):
            hotspot["straight_run_cells"] = float(straight_runs[index])
        if index < len(corridor_runs):
            hotspot["corridor_run_cells"] = float(corridor_runs[index])
        if index < len(degree_scores):
            hotspot["degree_score"] = float(degree_scores[index])
        if index < len(available_volumes):
            hotspot["available_volume_m3"] = float(
                available_volumes[index]
            )
        if index < len(volume_per_route):
            hotspot["volume_per_route_m"] = float(volume_per_route[index])
        if index < len(voxel_clearance):
            hotspot["voxel_mean_clearance_m"] = float(voxel_clearance[index])
        parsed[cell] = hotspot
    return parsed


def _parse_voxel_metrics(value: object) -> dict[str, Any] | None:
    """Parse compact cache-time voxel metrics without loading the sidecar."""
    if not isinstance(value, Mapping):
        return None
    if not supported_navigation_voxel_cache_identity(
        value.get("version"),
        value.get("method"),
    ):
        return None
    parsed: dict[str, Any] = {
        "version": value.get("version"),
        "method": value.get("method"),
    }
    for key in (
        "curvature_method",
        "coverage_scope",
        "model_kind",
        "navigation_graph_method",
        "branch_lookahead_method",
    ):
        raw = value.get(key)
        if isinstance(raw, str):
            parsed[key] = raw
    numeric_keys = (
        "voxel_size_m",
        "tile_size_m",
        "fine_voxel_size_m",
        "fine_tile_radius_m",
        "max_tiles",
        "max_fine_tiles",
        "max_fine_tile_cells",
        "max_cells",
        "max_surface_samples",
        "available_volume_m3",
        "volume_per_route_m",
        "free_cell_count",
        "seed_count",
        "surface_fraction",
        "min_clearance_m",
        "mean_clearance_m",
        "clearance_sample_count",
        "route_length_m",
        "triangle_count",
        "surface_sample_count",
        "curvature_region_count",
        "selected_region_count",
        "tile_count",
        "fine_tile_count",
        "coverage_cell_count",
        "tiles_skipped",
        "navigation_cell_count",
        "filled_free_cell_count",
        "progress_max_m",
    )
    float_keys = {
        "voxel_size_m",
        "tile_size_m",
        "fine_voxel_size_m",
        "fine_tile_radius_m",
        "available_volume_m3",
        "volume_per_route_m",
        "surface_fraction",
        "min_clearance_m",
        "mean_clearance_m",
        "route_length_m",
        "progress_max_m",
    }
    for key in numeric_keys:
        raw = value.get(key)
        if raw is None:
            continue
        try:
            parsed[key] = float(raw) if key in float_keys else int(raw)
        except (TypeError, ValueError):
            continue
    for key in (
        "built",
        "sampling_truncated",
        "fine_sampling_truncated",
        "flood_fill_truncated",
        "coverage_includes_preceding_curvature",
    ):
        if key in value:
            parsed[key] = bool(value.get(key))
    for key in ("bounds_min", "bounds_max"):
        raw = value.get(key)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            continue
        if len(raw) != 3:
            continue
        try:
            bounds = [float(item) for item in raw]
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(item) for item in bounds):
            parsed[key] = bounds
    raw_regions = value.get("selected_regions")
    if isinstance(raw_regions, Sequence) and not isinstance(
        raw_regions,
        (str, bytes),
    ):
        regions: list[dict[str, Any]] = []
        for raw_region in raw_regions[:8]:
            if not isinstance(raw_region, Mapping):
                continue
            region: dict[str, Any] = {}
            for key in ("start_index", "end_index", "max_rank_0_100"):
                raw = raw_region.get(key)
                try:
                    region[key] = int(raw)
                except (TypeError, ValueError):
                    continue
            for key in (
                "start_distance_m",
                "end_distance_m",
                "max_curvature_density_rad_per_m",
            ):
                raw = raw_region.get(key)
                try:
                    number = float(raw)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number):
                    region[key] = number
            if region:
                regions.append(region)
        parsed["selected_regions"] = regions
    return parsed


def _navigation_manifest_from_surface_positions(
    manifest: Mapping[str, Any],
    *,
    surface_positions: np.ndarray | None,
) -> Mapping[str, Any]:
    if surface_positions is None:
        return manifest
    positions = np.asarray(surface_positions)
    if positions.ndim != 2 or positions.shape[1] != 3 or len(positions) < 2:
        return manifest
    try:
        cell_size = positive_manifest_float(
            manifest.get("footprint_cell_size"),
            "footprint_cell_size",
        )
    except Exception:
        return manifest

    surface_cells = _surface_footprint_cells(positions, cell_size=cell_size)
    if not surface_cells:
        return manifest
    navigation_cells = _surface_span_filled_footprint_cells(
        surface_cells,
        max_span_cells=NAVIGATION_SURFACE_SPAN_FILL_MAX_CELLS,
    )
    sampling_cells = _surface_span_filled_footprint_cells(
        surface_cells,
        max_span_cells=NAVIGATION_SURFACE_SPAN_SUPPORT_MAX_CELLS,
    )
    navigation_manifest = dict(manifest)
    navigation_manifest["footprint_cell_size"] = cell_size
    navigation_manifest["footprint_cells"] = _flat_cells(
        tuple(sorted(navigation_cells))
    )
    navigation_manifest["surface_footprint_cell_count"] = len(surface_cells)
    navigation_manifest["navigation_footprint_source"] = "surface_span_fill_v1"
    navigation_manifest["_voxel_sampling_cells"] = _flat_cells(sampling_cells)
    return navigation_manifest


def _surface_footprint_cells(
    positions: np.ndarray,
    *,
    cell_size: float,
) -> frozenset[FootprintCell]:
    cells: set[FootprintCell] = set()
    for start in range(0, len(positions), _NAVIGATION_SURFACE_BLOCK_VERTICES):
        end = min(start + _NAVIGATION_SURFACE_BLOCK_VERTICES, len(positions))
        block = positions[start:end]
        block_cx = np.floor(block[:, 0] / cell_size).astype(np.int64, copy=False)
        block_cz = np.floor(block[:, 2] / cell_size).astype(np.int64, copy=False)
        if len(block_cx) == 0:
            continue
        min_cx = int(block_cx.min())
        min_cz = int(block_cz.min())
        max_cz = int(block_cz.max())
        z_span = max(1, max_cz - min_cz + 1)
        keys = (block_cx - min_cx) * z_span + (block_cz - min_cz)
        for key in np.unique(keys).tolist():
            cells.add((int(key // z_span) + min_cx, int(key % z_span) + min_cz))
    return frozenset(cells)


def _surface_span_filled_footprint_cells(
    surface_cells: frozenset[FootprintCell],
    *,
    max_span_cells: int,
) -> frozenset[FootprintCell]:
    """Return surface cells plus short X/Z spans likely to be traversable void.

    OBJ/GLB vertices live on cave surfaces, so a footprint made only from
    vertex cells often traces walls rather than the passage volume. This
    conservative fill closes short spans between opposite surface cells in
    each X column and Z row. The span cap avoids joining unrelated passages
    across large empty regions while allowing narrow cave cross-sections to
    expose their interior cells to the centerline search.
    """
    if not surface_cells:
        return surface_cells
    max_span = max(1, int(max_span_cells))
    filled: set[FootprintCell] = set(surface_cells)

    by_x: dict[int, set[int]] = {}
    by_z: dict[int, set[int]] = {}
    for x, z in surface_cells:
        by_x.setdefault(x, set()).add(z)
        by_z.setdefault(z, set()).add(x)

    for x, z_values in by_x.items():
        ordered = sorted(z_values)
        for first_z, second_z in zip(ordered, ordered[1:], strict=False):
            if second_z - first_z > max_span:
                continue
            for z in range(first_z, second_z + 1):
                filled.add((x, z))

    for z, x_values in by_z.items():
        ordered = sorted(x_values)
        for first_x, second_x in zip(ordered, ordered[1:], strict=False):
            if second_x - first_x > max_span:
                continue
            for x in range(first_x, second_x + 1):
                filled.add((x, z))

    return frozenset(filled)


def _surface_vertical_profiles(
    surface_positions: np.ndarray | None,
    *,
    cell_size: object,
) -> _SurfaceProfileIndex | None:
    if surface_positions is None:
        return None
    positions = np.asarray(surface_positions)
    if positions.ndim != 2 or positions.shape[1] != 3 or len(positions) < 2:
        return None
    try:
        size = positive_manifest_float(cell_size, "footprint_cell_size")
    except Exception:
        return None

    global_low_y = float(positions[:, 1].min())
    global_high_y = float(positions[:, 1].max())
    global_span_y = global_high_y - global_low_y
    if not np.isfinite(global_span_y) or global_span_y <= 1e-9:
        return None
    vertical_bin_count = max(
        NAVIGATION_SURFACE_Y_HISTOGRAM_BINS,
        int(
            math.ceil(
                global_span_y / NAVIGATION_SURFACE_VERTICAL_BIN_SIZE_M
            )
        ),
    )

    columns: dict[FootprintCell, _SurfaceColumnProfile] = {}
    for start in range(0, len(positions), _NAVIGATION_SURFACE_BLOCK_VERTICES):
        end = min(start + _NAVIGATION_SURFACE_BLOCK_VERTICES, len(positions))
        block = positions[start:end]
        block_cx = np.floor(block[:, 0] / size).astype(np.int64, copy=False)
        block_cz = np.floor(block[:, 2] / size).astype(np.int64, copy=False)
        block_y = block[:, 1].astype(np.float64, copy=False)
        block_y_bins = np.floor(
            (block_y - global_low_y)
            / global_span_y
            * vertical_bin_count
        ).astype(np.int64, copy=False)
        np.clip(
            block_y_bins,
            0,
            vertical_bin_count - 1,
            out=block_y_bins,
        )
        if len(block_cx) == 0:
            continue
        min_cx = int(block_cx.min())
        min_cz = int(block_cz.min())
        max_cz = int(block_cz.max())
        z_span = max(1, max_cz - min_cz + 1)
        keys = (block_cx - min_cx) * z_span + (block_cz - min_cz)
        order = np.argsort(keys, kind="stable")
        sorted_keys = keys[order]
        sorted_y = block_y[order]
        sorted_y_bins = block_y_bins[order]
        boundaries = np.nonzero(np.diff(sorted_keys))[0] + 1
        run_starts = np.concatenate(([0], boundaries))
        run_ends = np.concatenate((boundaries, [len(sorted_keys)]))
        for run_start, run_end in zip(run_starts, run_ends, strict=False):
            key = int(sorted_keys[run_start])
            cell = (int(key // z_span) + min_cx, int(key % z_span) + min_cz)
            low_y = float(sorted_y[run_start:run_end].min())
            high_y = float(sorted_y[run_start:run_end].max())
            existing = columns.get(cell)
            if existing is None:
                occupied = set(
                    int(value)
                    for value in np.unique(
                        sorted_y_bins[run_start:run_end]
                    ).tolist()
                )
                columns[cell] = _SurfaceColumnProfile(
                    low_y=low_y,
                    high_y=high_y,
                    occupied_y_bins=occupied,
                )
            else:
                existing.low_y = min(existing.low_y, low_y)
                existing.high_y = max(existing.high_y, high_y)
                assert isinstance(existing.occupied_y_bins, set)
                existing.occupied_y_bins.update(
                    int(value)
                    for value in np.unique(
                        sorted_y_bins[run_start:run_end]
                    ).tolist()
                )
    if not columns:
        return None
    return _SurfaceProfileIndex(
        global_low_y=global_low_y,
        global_high_y=global_high_y,
        columns=columns,
        vertical_bin_count=vertical_bin_count,
    )


def _surface_route_points_for_path(
    path: CenterlinePath,
    *,
    surface_profiles: _SurfaceProfileIndex | None,
    preserve_anchored_start_cell: bool = False,
) -> tuple[
    tuple[Point, ...],
    tuple[tuple[float, float], ...],
    tuple[float, ...],
    int,
]:
    if surface_profiles is None:
        return (), (), (), 0
    point_xz: list[PointXZ] = []
    medial_cells: list[FootprintCell] = []
    raw_y_samples: list[_SurfaceRouteYSample | None] = []
    for index, cell in enumerate(path.cells):
        # Exact-ingress routes must retain the ordered footprint topology.
        # Generic
        # transverse medial centering can jump across a very wide synthesized
        # surface span (Devil's Eye moved the second point by >200 m), severing
        # an otherwise adjacent entrance path before exact 3D certification.
        xz = (
            path.centers[cell]
            if preserve_anchored_start_cell
            else _surface_medial_xz_for_path_cell(path, index=index)
        )
        medial_cell = _footprint_cell_for_xz(xz, path.footprint_cell_size)
        if medial_cell not in path.component_cells:
            medial_cell = cell
        point_xz.append(xz)
        medial_cells.append(medial_cell)
        raw_y_samples.append(
            _surface_medial_y_for_cell(medial_cell, surface_profiles)
        )
    y_samples = _interpolate_missing_surface_route_y_samples(
        point_xz,
        raw_y_samples,
    )
    if not y_samples:
        return (), (), (), 0
    clearance_margins: list[float] = []
    for medial_cell, y_sample in zip(
        medial_cells,
        y_samples,
        strict=True,
    ):
        clearance_margins.append(
            _route_clearance_margin_m(
                path,
                cell=medial_cell,
                y_sample=y_sample,
            )
        )
    return (
        tuple(
            (xz[0], y_sample.y, xz[1])
            for xz, y_sample in zip(point_xz, y_samples, strict=True)
        ),
        tuple((sample.low_y, sample.high_y) for sample in y_samples),
        tuple(clearance_margins),
        sum(sample is None for sample in raw_y_samples),
    )


def _surface_component_y_ranges_for_path(
    path: CenterlinePath,
    *,
    component_cells: tuple[FootprintCell, ...],
    surface_profiles: _SurfaceProfileIndex | None,
    route_y_ranges: Sequence[tuple[float, float]] = (),
) -> tuple[tuple[tuple[float, float], ...], int]:
    if surface_profiles is None:
        return (), 0
    del path, route_y_ranges
    y_ranges: list[tuple[float, float]] = []
    missing_profile_count = 0
    for cell in component_cells:
        profile = _merged_surface_profile_for_cell(cell, surface_profiles)
        if profile is None:
            y_ranges.append(
                tuple(
                    sorted(
                        (
                            float(surface_profiles.global_low_y),
                            float(surface_profiles.global_high_y),
                        )
                    )
                )
            )
            missing_profile_count += 1
            continue
        y_ranges.append(
            tuple(sorted((float(profile.low_y), float(profile.high_y))))
        )
    return tuple(y_ranges), missing_profile_count


def _surface_component_vertical_gap_seeds_for_path(
    *,
    component_cells: Sequence[FootprintCell],
    surface_profiles: _SurfaceProfileIndex | None,
) -> tuple[tuple[FootprintCell, float], ...]:
    """Persist every bounded vertical passage candidate from surface evidence."""
    return tuple(
        (cell, (float(low_y) + float(high_y)) * 0.5)
        for cell, low_y, high_y in (
            _surface_component_vertical_gap_intervals_for_path(
                component_cells=component_cells,
                surface_profiles=surface_profiles,
            )
        )
    )


def _surface_component_vertical_gap_intervals_for_path(
    *,
    component_cells: Sequence[FootprintCell],
    surface_profiles: _SurfaceProfileIndex | None,
) -> tuple[tuple[FootprintCell, float, float], ...]:
    """Persist bounded vertical intervals as candidate layer evidence.

    Raw OBJ vertex bins are not watertight ray crossings, so their parity
    cannot establish solid versus cave air. Retain every bounded interval and
    let the OBJ-anchored continuity selector choose one proposal. Fixed voxels,
    global component connectivity, and exact cached-mesh checks still decide
    whether that proposal can become executable.
    """
    if surface_profiles is None:
        return ()
    intervals: list[tuple[FootprintCell, float, float]] = []
    for cell in component_cells:
        profile = _merged_surface_profile_for_cell(cell, surface_profiles)
        if profile is None:
            continue
        intervals.extend(
            (cell, float(sample.low_y), float(sample.high_y))
            for sample in _surface_vertical_gap_samples(
                profile,
                surface_profiles,
            )
        )
    return tuple(dict.fromkeys(intervals))


def _interpolate_missing_surface_route_y_samples(
    points_xz: Sequence[PointXZ],
    samples: Sequence[_SurfaceRouteYSample | None],
) -> tuple[_SurfaceRouteYSample, ...]:
    """Fill sparse vertical-profile gaps along one metadata route.

    Surface-span footprint filling can place a valid centerline cell farther
    from raw vertices than the bounded column lookup. Interpolate only between
    real samples on that ordered route (or extend the nearest endpoint sample)
    so V12 receives a complete 3D hint without inventing free-space authority.
    Fixed voxels and exact mesh checks still decide whether a route is usable.
    """
    if len(points_xz) != len(samples) or not samples:
        return ()
    known_indices = tuple(
        index for index, sample in enumerate(samples) if sample is not None
    )
    if not known_indices:
        return ()
    cumulative_distance_m = [0.0]
    for first, second in zip(points_xz, points_xz[1:], strict=False):
        cumulative_distance_m.append(
            cumulative_distance_m[-1] + math.dist(first, second)
        )
    resolved: list[_SurfaceRouteYSample] = []
    for index, raw_sample in enumerate(samples):
        if raw_sample is not None:
            resolved.append(raw_sample)
            continue
        previous_indices = tuple(
            candidate for candidate in known_indices if candidate < index
        )
        following_indices = tuple(
            candidate for candidate in known_indices if candidate > index
        )
        if not previous_indices:
            nearest = samples[following_indices[0]]
            assert nearest is not None
            resolved.append(nearest)
            continue
        if not following_indices:
            nearest = samples[previous_indices[-1]]
            assert nearest is not None
            resolved.append(nearest)
            continue
        previous_index = previous_indices[-1]
        following_index = following_indices[0]
        previous = samples[previous_index]
        following = samples[following_index]
        assert previous is not None and following is not None
        span_m = (
            cumulative_distance_m[following_index]
            - cumulative_distance_m[previous_index]
        )
        fraction = (
            0.0
            if span_m <= 1e-9
            else (
                cumulative_distance_m[index]
                - cumulative_distance_m[previous_index]
            )
            / span_m
        )
        low_y = previous.low_y + (
            following.low_y - previous.low_y
        ) * fraction
        high_y = previous.high_y + (
            following.high_y - previous.high_y
        ) * fraction
        low_y, high_y = sorted((float(low_y), float(high_y)))
        y = previous.y + (following.y - previous.y) * fraction
        resolved.append(
            _SurfaceRouteYSample(
                y=min(high_y, max(low_y, float(y))),
                low_y=low_y,
                high_y=high_y,
            )
        )
    return tuple(resolved)


def _surface_medial_xz_for_path_cell(
    path: CenterlinePath,
    *,
    index: int,
) -> PointXZ:
    """Return a route point moved toward the local X/Z passage middle.

    Route cells are discrete and can land one or two cells off the actual
    medial line, especially when the footprint was reconstructed from surface
    spans. Adjust only the inferred cross-passage axis so we do not average
    points forward/backward along the cave length.
    """
    cells = path.cells
    if not cells:
        raise NavigationConfigurationError("cannot center an empty route")
    index = max(0, min(len(cells) - 1, int(index)))
    cell = cells[index]
    x, z = path.centers[cell]
    x_span = _component_axis_span(path.component_cells, cell, axis="x")
    z_span = _component_axis_span(path.component_cells, cell, axis="z")
    axis = _route_cross_passage_axis(cells, index=index, x_span=x_span, z_span=z_span)
    if axis == "x":
        x = _axis_span_midpoint(x_span, path.footprint_cell_size)
    elif axis == "z":
        z = _axis_span_midpoint(z_span, path.footprint_cell_size)
    else:
        x_candidate = _axis_span_midpoint(x_span, path.footprint_cell_size)
        z_candidate = _axis_span_midpoint(z_span, path.footprint_cell_size)
        candidate_cell = (
            int(math.floor(x_candidate / path.footprint_cell_size)),
            int(math.floor(z_candidate / path.footprint_cell_size)),
        )
        if candidate_cell in path.component_cells:
            x, z = x_candidate, z_candidate
    candidate = (float(x), float(z))
    candidate_cell = (
        int(math.floor(candidate[0] / path.footprint_cell_size)),
        int(math.floor(candidate[1] / path.footprint_cell_size)),
    )
    if candidate_cell not in path.component_cells:
        return path.centers[cell]
    return candidate


def _route_cross_passage_axis(
    cells: tuple[FootprintCell, ...],
    *,
    index: int,
    x_span: tuple[int, int],
    z_span: tuple[int, int],
) -> str:
    if len(cells) >= 2:
        previous_cell = cells[max(0, index - 1)]
        next_cell = cells[min(len(cells) - 1, index + 1)]
        dx = abs(next_cell[0] - previous_cell[0])
        dz = abs(next_cell[1] - previous_cell[1])
        if dx > dz:
            return "z"
        if dz > dx:
            return "x"

    x_width = x_span[1] - x_span[0] + 1
    z_width = z_span[1] - z_span[0] + 1
    if x_width < z_width:
        return "x"
    if z_width < x_width:
        return "z"
    return "both"


def _component_axis_span(
    component: frozenset[FootprintCell],
    cell: FootprintCell,
    *,
    axis: str,
) -> tuple[int, int]:
    axis = str(axis)
    coordinate_index = 0 if axis == "x" else 1
    fixed_index = 1 - coordinate_index
    fixed_value = cell[fixed_index]
    coordinate = cell[coordinate_index]
    low = coordinate
    high = coordinate
    while _axis_cell(cell, axis=axis, coordinate=low - 1) in component:
        low -= 1
    while _axis_cell(cell, axis=axis, coordinate=high + 1) in component:
        high += 1
    return low, high


def _axis_cell(
    cell: FootprintCell,
    *,
    axis: str,
    coordinate: int,
) -> FootprintCell:
    if axis == "x":
        return (int(coordinate), cell[1])
    return (cell[0], int(coordinate))


def _axis_span_midpoint(span: tuple[int, int], cell_size: float) -> float:
    low, high = span
    return (float(low) + float(high) + 1.0) * 0.5 * float(cell_size)


def _footprint_cell_for_xz(xz: PointXZ, cell_size: float) -> FootprintCell:
    return (
        int(math.floor(float(xz[0]) / float(cell_size))),
        int(math.floor(float(xz[1]) / float(cell_size))),
    )


def _route_clearance_margin_m(
    path: CenterlinePath,
    *,
    cell: FootprintCell,
    y_sample: _SurfaceRouteYSample,
) -> float:
    lateral_margin_m = max(
        0.0,
        (float(path.clearance_scores.get(cell, 1)) - 0.5)
        * float(path.footprint_cell_size),
    )
    vertical_margin_m = max(
        0.0,
        min(
            abs(float(y_sample.y) - float(y_sample.low_y)),
            abs(float(y_sample.high_y) - float(y_sample.y)),
        ),
    )
    return min(lateral_margin_m, vertical_margin_m)


def _surface_medial_y_for_cell(
    cell: FootprintCell,
    surface_profiles: _SurfaceProfileIndex,
) -> _SurfaceRouteYSample | None:
    profile = _merged_surface_profile_for_cell(cell, surface_profiles)
    if profile is None:
        return None
    samples = _surface_vertical_gap_samples(profile, surface_profiles)
    if not samples:
        return None
    return max(
        samples,
        key=lambda sample: (
            float(sample.high_y - sample.low_y),
            -abs(float(sample.y)),
        ),
    )


def _surface_occupied_bin_indices(
    profile: _SurfaceColumnProfile,
) -> tuple[int, ...]:
    value = profile.occupied_y_bins
    if isinstance(value, np.ndarray):
        return tuple(int(index) for index in np.flatnonzero(value).tolist())
    return tuple(sorted(int(index) for index in value))


def _surface_vertical_gap_samples(
    profile: _SurfaceColumnProfile,
    surface_profiles: _SurfaceProfileIndex,
) -> tuple[_SurfaceRouteYSample, ...]:
    """Return every bounded surface gap at 0.25 m-or-finer resolution.

    Adjacent occupied bins belong to one surface band. Raw OBJ vertex bins are
    not complete ray crossings, so parity would be unsafe here; every interval
    between neighboring bands remains a proposal for the later entrance-
    connected layer selector. Unbounded space is never proposed.
    """
    occupied_runs = _surface_occupied_bin_runs(profile)
    samples: list[_SurfaceRouteYSample] = []
    for lower_run, upper_run in zip(
        occupied_runs,
        occupied_runs[1:],
        strict=False,
    ):
        gap_start_index = int(lower_run[1]) + 1
        gap_stop_index = int(upper_run[0])
        if gap_stop_index <= gap_start_index:
            continue
        gap_low_y = _surface_bin_boundary_y(
            surface_profiles,
            gap_start_index,
        )
        gap_high_y = _surface_bin_boundary_y(
            surface_profiles,
            gap_stop_index,
        )
        low_y, high_y = sorted((float(gap_low_y), float(gap_high_y)))
        samples.append(
            _SurfaceRouteYSample(
                y=(low_y + high_y) * 0.5,
                low_y=low_y,
                high_y=high_y,
            )
        )
    if samples:
        return tuple(samples)
    # A half-metre passage can place its two surfaces in one contiguous run
    # at quarter-metre binning, especially on a slope. The observed per-cell
    # surface span is still bounded evidence, so retain it when it is wide
    # enough to contain at least one vertical evidence cell. A zero/thin span
    # remains ambiguous and fails closed.
    low_y, high_y = sorted((float(profile.low_y), float(profile.high_y)))
    bin_size_m = (
        float(surface_profiles.global_high_y)
        - float(surface_profiles.global_low_y)
    ) / max(1, int(surface_profiles.vertical_bin_count))
    if high_y - low_y + 1e-9 < max(1e-9, float(bin_size_m)):
        return ()
    return (
        _SurfaceRouteYSample(
            y=(low_y + high_y) * 0.5,
            low_y=low_y,
            high_y=high_y,
        ),
    )


def _surface_occupied_bin_runs(
    profile: _SurfaceColumnProfile,
) -> tuple[tuple[int, int], ...]:
    """Collapse contiguous occupied bins into vertical crossing bands."""
    indices = _surface_occupied_bin_indices(profile)
    if not indices:
        return ()
    runs: list[tuple[int, int]] = []
    start = previous = int(indices[0])
    for raw_index in indices[1:]:
        index = int(raw_index)
        if index <= previous + 1:
            previous = index
            continue
        runs.append((start, previous))
        start = previous = index
    runs.append((start, previous))
    return tuple(runs)


def _surface_bin_boundary_y(
    surface_profiles: _SurfaceProfileIndex,
    bin_index: int,
) -> float:
    clamped_index = max(
        0,
        min(int(surface_profiles.vertical_bin_count), int(bin_index)),
    )
    fraction = clamped_index / max(
        1,
        int(surface_profiles.vertical_bin_count),
    )
    return surface_profiles.global_low_y + (
        surface_profiles.global_high_y - surface_profiles.global_low_y
    ) * fraction


def _merged_surface_profile_for_cell(
    cell: FootprintCell,
    surface_profiles: _SurfaceProfileIndex,
) -> _SurfaceColumnProfile | None:
    for radius in range(0, NAVIGATION_SURFACE_Y_SEARCH_RADIUS_CELLS + 1):
        profiles = [
            surface_profiles.columns[(cell[0] + dx, cell[1] + dz)]
            for dx in range(-radius, radius + 1)
            for dz in range(-radius, radius + 1)
            if (cell[0] + dx, cell[1] + dz) in surface_profiles.columns
        ]
        if not profiles:
            continue
        occupied: set[int] = set()
        low_y = min(profile.low_y for profile in profiles)
        high_y = max(profile.high_y for profile in profiles)
        for profile in profiles:
            occupied.update(_surface_occupied_bin_indices(profile))
        return _SurfaceColumnProfile(
            low_y=low_y,
            high_y=high_y,
            occupied_y_bins=occupied,
        )
    return None


def _manifest_footprint_cell_count(manifest: Mapping[str, Any]) -> int:
    flat = manifest.get("footprint_cells")
    if isinstance(flat, Sequence) and not isinstance(flat, (str, bytes)):
        return len(flat) // 2
    return 0


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _float_or_default(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed


def _int_or_default(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)
