"""Tests for minimap memory behavior on large map footprints."""

from __future__ import annotations

from caveviewer.gui.minimap import Minimap


class _TrackingBuffer:
    def write(self, _data: bytes) -> None:
        pass

    def release(self) -> None:
        pass


class _TrackingVertexArray:
    def render(self, *_args, **_kwargs) -> None:
        pass

    def release(self) -> None:
        pass


class _TrackingContext:
    def __init__(self):
        self.buffer_reserves: list[int] = []

    def program(self, **_kwargs):
        return object()

    def buffer(self, *, reserve: int):
        self.buffer_reserves.append(reserve)
        return _TrackingBuffer()

    def vertex_array(self, *_args):
        return _TrackingVertexArray()


def test_minimap_static_buffer_reserve_is_bounded_by_panel_resolution():
    footprint_count = Minimap._MAX_STATIC_OCCUPANCY_PIXELS * 3
    flat_footprint = []
    for index in range(footprint_count):
        flat_footprint.extend((index, 0))
    manifest = {
        "chunk_size": 8.0,
        "chunks": {},
        "footprint_cell_size": 1.0,
        "footprint_cells": flat_footprint,
    }
    context = _TrackingContext()

    minimap = Minimap(context, manifest)

    max_static_verts = (Minimap._MAX_STATIC_OCCUPANCY_PIXELS + 8) * 6
    max_static_bytes = max_static_verts * 6 * 4
    assert context.buffer_reserves[0] <= max_static_bytes
    assert minimap._footprint_cell_count == footprint_count
    # The fine-grained manifest list should be reused by reference instead of
    # copied into a second full-size occupied-cell set during construction.
    assert minimap._footprint_cells_flat is flat_footprint
    assert minimap.occupied_xz == set()


def test_minimap_static_geometry_is_deduplicated_to_visible_pixels():
    flat_footprint = []
    for _ in range(1000):
        flat_footprint.extend((10, 20))
    manifest = {
        "chunk_size": 8.0,
        "chunks": {},
        "footprint_cell_size": 1.0,
        "footprint_cells": flat_footprint,
    }

    minimap = Minimap(_TrackingContext(), manifest)

    assert len(list(minimap._visible_footprint_pixels((800, 600)))) == 1


def test_minimap_ignores_navigation_metadata():
    manifest = {
        "chunk_size": 10.0,
        "chunks": {"0_0_0": {"bounds_min": [0, 0, 0], "bounds_max": [10, 10, 10]}},
        "navigation": {
            "version": 1,
            "method": "footprint_centerline_paths_v1",
            "routes": [{"id": "centerline-0", "points": [[0.0, 0.0, 0.0]]}],
        },
    }
    plain_manifest = {
        "chunk_size": 10.0,
        "chunks": {"0_0_0": {"bounds_min": [0, 0, 0], "bounds_max": [10, 10, 10]}},
    }
    minimap = Minimap(_TrackingContext(), manifest)
    plain_minimap = Minimap(_TrackingContext(), plain_manifest)

    navigation_geom = minimap._build_static_geom((800, 600))
    plain_geom = plain_minimap._build_static_geom((800, 600))

    assert navigation_geom == plain_geom


def test_minimap_active_route_overlay_invalidates_static_geometry():
    manifest = {
        "chunk_size": 10.0,
        "chunks": {"0_0_0": {"bounds_min": [0, 0, 0], "bounds_max": [10, 10, 10]}},
    }
    minimap = Minimap(_TrackingContext(), manifest)

    _, baseline_vert_count = minimap._build_static_geom((800, 600))
    minimap._static_geom_window_size = (800, 600)
    minimap.set_active_route_points_xz(((0.0, 0.0), (10.0, 10.0), (20.0, 10.0)))
    _, active_vert_count = minimap._build_static_geom((800, 600))

    assert minimap._static_geom_window_size is None
    assert active_vert_count > baseline_vert_count
