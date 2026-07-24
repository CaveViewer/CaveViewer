"""Tests for Auto Dive controller pause/resume behavior."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import math
import threading

import numpy as np

from caveviewer.core.navigation.autodive import AutoDivePlan
from caveviewer.core.navigation.route import CameraRoute, RouteKeyframe
from caveviewer.gui.autodive_controller import AutoDiveController, AutoDiveState


@dataclass
class _FakeCamera:
    position: np.ndarray
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeWorld:
    def __init__(self):
        self.config = SimpleNamespace(chunk_size=1.0)
        self.available_cells = {
            (x, y, z)
            for x in range(-12, 24)
            for y in range(-10, 12)
            for z in range(-10, 12)
        }
        self.loaded_cells = set()
        self._pending = set()
        self._failed_cells = {}
        self._lock = threading.Lock()
        self.prefetch = set()

    def cell_for_position(self, position):
        return tuple(int(math.floor(float(value))) for value in position)

    def available_cells_in_radius(self, center, radius):
        return {
            cell
            for cell in self.available_cells
            if max(abs(cell[index] - center[index]) for index in range(3))
            <= radius
        }

    def set_prefetch_wanted_cells(self, cells):
        self.prefetch = set(cells)
        self._pending = self.prefetch - self.loaded_cells


def test_auto_dive_pauses_until_lookahead_cells_are_loaded():
    plan = _plan()
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    controller = AutoDiveController(plan, perf_counter=clock, lookahead_seconds=2.0)

    controller.start(camera, world)

    assert controller.state is AutoDiveState.LOADING
    assert controller.prefetch_cells
    assert controller.readiness.ready is False
    assert camera.position.tolist() == [0.0, 0.0, 0.0]

    clock.now = 5.0
    controller.update(camera, world)

    assert controller.state is AutoDiveState.LOADING
    assert camera.position.tolist() == [0.0, 0.0, 0.0]

    world.loaded_cells = set(controller.prefetch_cells)
    world._pending = set()
    clock.now = 6.0
    controller.update(camera, world)

    assert controller.state is AutoDiveState.DIVING
    assert camera.position.tolist() == [0.0, 0.0, 0.0]

    clock.now = 7.0
    controller.update(camera, world)

    assert controller.state is AutoDiveState.DIVING
    assert camera.position[0] > 0.0


def _plan() -> AutoDivePlan:
    route = CameraRoute.from_keyframes(
        (
            RouteKeyframe(0.0, (0.0, 0.0, 0.0), 0.0, 0.0),
            RouteKeyframe(10.0, (10.0, 0.0, 0.0), 0.0, 0.0),
        )
    )
    return AutoDivePlan(
        route=route,
        centerline_path=None,  # type: ignore[arg-type]
        route_points=((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)),
        route_cells=((0, 0), (10, 0)),
        circular_arc=False,
        route_length_m=10.0,
        duration_s=10.0,
        render_distance_cells=2,
    )
