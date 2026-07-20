"""Check spatial chunk selection and distance-aware eviction policies."""

from __future__ import annotations

from caveviewer.core.streaming.scheduler import (
    cell_in_cube_radius,
    cells_outside_cube_radius,
    select_evictions,
    select_wanted_cells,
)


def test_wanted_cells_follow_requested_render_distance():
    available = {(distance, 0, 0) for distance in range(1, 6)}

    wanted = select_wanted_cells(
        available,
        center=(0, 0, 0),
        radius=5,
        max_loaded_chunks=2,
    )

    assert wanted == available


def test_cube_radius_and_outside_selection_share_one_policy():
    loaded = {(0, 0, 0), (1, 1, 1), (2, 0, 0)}

    assert cell_in_cube_radius((1, 1, 1), (0, 0, 0), 1)
    assert cells_outside_cube_radius(loaded, (0, 0, 0), 1) == {(2, 0, 0)}


def test_eviction_prefers_farthest_cells_outside_wanted_set():
    loaded = {(0, 0, 0), (1, 0, 0), (5, 0, 0), (9, 0, 0)}
    wanted = {(0, 0, 0), (1, 0, 0)}

    evictions = select_evictions(
        loaded,
        wanted,
        center=(0, 0, 0),
        max_loaded_chunks=2,
    )

    assert evictions == [(9, 0, 0), (5, 0, 0)]


def test_eviction_never_reduces_residency_below_wanted_count():
    loaded = {(0, 0, 0), (1, 0, 0), (2, 0, 0)}

    assert select_evictions(
        loaded,
        loaded,
        center=(0, 0, 0),
        max_loaded_chunks=1,
    ) == []
