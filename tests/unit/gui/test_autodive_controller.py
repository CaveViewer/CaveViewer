"""Tests for Auto Dive controller pause/resume behavior."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import math
import threading

import numpy as np

from caveviewer.core.navigation.autodive import AutoDivePlan, AutoDiveSettings
from caveviewer.core.navigation.route import CameraRoute, RouteKeyframe
from caveviewer.gui.autodive_controller import (
    AutoDiveController,
    AutoDiveReplanner,
    AutoDiveState,
    DEFAULT_AUTO_DIVE_REPLAN_MIN_INTERVAL_SECONDS,
    DEFAULT_AUTO_DIVE_ROUTE_PREFETCH_RADIUS_CELLS,
    DEFAULT_AUTO_DIVE_ROUTE_LOOKAHEAD_SECONDS,
    DEFAULT_AUTO_DIVE_SURVEY_DURATION_SECONDS,
    DEFAULT_AUTO_DIVE_SURVEY_INTERVAL_SECONDS,
)


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


class _FakeReplanner:
    def __init__(self, latest_plan=None):
        self.latest_plan = latest_plan
        self.requests = []
        self.shutdown_called = False

    def request(self, current_position):
        self.requests.append(
            tuple(float(value) for value in np.asarray(current_position))
        )
        return True

    def take_latest_plan(self):
        plan = self.latest_plan
        self.latest_plan = None
        return plan

    def shutdown(self):
        self.shutdown_called = True


class _FakeBlackbox:
    def __init__(self):
        self.events = []
        self.closed = False

    def record(self, event, **payload):
        self.events.append((event, payload))

    def close(self):
        self.closed = True


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
    assert (
        controller.route_prefetch_radius_cells
        == DEFAULT_AUTO_DIVE_ROUTE_PREFETCH_RADIUS_CELLS
    )
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


def test_auto_dive_requests_replan_after_fractional_cell_travel():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        replan_min_interval_s=0.0,
        replan_only_during_survey=False,
    )
    controller.start(camera, world)

    camera.position = np.array([0.1, 0.0, 0.0], dtype=np.float64)
    assert controller.update_replan(camera, world, now=0.0) is False
    assert replanner.requests == []

    camera.position = np.array([0.25, 0.0, 0.0], dtype=np.float64)
    assert controller.update_replan(camera, world, now=0.0) is False

    assert replanner.requests == [(0.25, 0.0, 0.0)]


def test_auto_dive_route_prefetch_radius_is_narrower_than_render_radius():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    controller = AutoDiveController(
        _plan(render_distance_cells=6),
        perf_counter=clock,
        lookahead_seconds=1.0,
    )

    controller.start(camera, world)

    assert controller.route_prefetch_radius_cells == 2
    assert controller.prefetch_cells
    assert all(
        abs(cell[1]) <= 2
        for cell in controller.prefetch_cells
    )


def test_auto_dive_periodic_survey_pause_holds_position_and_scans_view():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    blackbox = _FakeBlackbox()
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        lookahead_seconds=1.0,
        survey_interval_s=5.0,
        survey_duration_s=2.0,
        blackbox=blackbox,
    )
    controller.start(camera, world)
    world.loaded_cells = set(controller.prefetch_cells)
    world._pending = set()

    clock.now = 1.0
    controller.update(camera, world)
    clock.now = 6.0
    controller.update(camera, world)

    survey_position = camera.position.copy()
    survey_yaw = camera.yaw

    assert controller.status_note == "Surveying next passage"
    assert survey_position[0] == 5.0

    clock.now = 6.5
    controller.update(camera, world)

    assert camera.position.tolist() == survey_position.tolist()
    assert camera.yaw != survey_yaw

    world.loaded_cells = set(controller.prefetch_cells)
    world._pending = set()
    clock.now = 9.1
    controller.update(camera, world)
    clock.now = 10.1
    controller.update(camera, world)

    assert camera.position[0] > survey_position[0]
    assert any(event == "survey_started" for event, _payload in blackbox.events)
    assert any(event == "survey_completed" for event, _payload in blackbox.events)


def test_auto_dive_survey_pause_temporarily_widens_route_prefetch():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    controller = AutoDiveController(
        _plan(render_distance_cells=6),
        perf_counter=clock,
        lookahead_seconds=1.0,
        survey_interval_s=5.0,
        survey_duration_s=2.0,
    )
    controller.start(camera, world)
    normal_prefetch_count = len(controller.prefetch_cells)
    world.loaded_cells = set(controller.prefetch_cells)
    world._pending = set()

    clock.now = 1.0
    controller.update(camera, world)
    clock.now = 6.0
    controller.update(camera, world)

    assert controller.route_prefetch_radius_cells == 2
    assert len(controller.prefetch_cells) > normal_prefetch_count
    assert any(
        abs(cell[1]) == 3
        for cell in controller.prefetch_cells
    )


def test_auto_dive_replan_defaults_to_bounded_runtime_cadence():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        replan_only_during_survey=False,
    )
    controller.start(camera, world)

    assert controller.lookahead_seconds == DEFAULT_AUTO_DIVE_ROUTE_LOOKAHEAD_SECONDS
    assert controller.survey_interval_s == DEFAULT_AUTO_DIVE_SURVEY_INTERVAL_SECONDS
    assert controller.survey_duration_s == DEFAULT_AUTO_DIVE_SURVEY_DURATION_SECONDS
    assert (
        controller.replan_min_interval_s
        == DEFAULT_AUTO_DIVE_REPLAN_MIN_INTERVAL_SECONDS
    )

    camera.position = np.array([0.25, 0.0, 0.0], dtype=np.float64)
    assert controller.update_replan(camera, world, now=0.5) is False
    assert replanner.requests == []

    assert controller.update_replan(
        camera,
        world,
        now=DEFAULT_AUTO_DIVE_REPLAN_MIN_INTERVAL_SECONDS,
    ) is False
    assert replanner.requests == [(0.25, 0.0, 0.0)]


def test_auto_dive_default_replanning_waits_for_survey_pause():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=10.0,
        survey_interval_s=5.0,
        survey_duration_s=2.0,
    )
    controller.start(camera, world)
    world.loaded_cells = set(controller.prefetch_cells)
    world._pending = set()

    camera.position = np.array([4.0, 0.0, 0.0], dtype=np.float64)
    assert controller.update_replan(camera, world, now=4.0) is False
    assert replanner.requests == []

    clock.now = 1.0
    controller.update(camera, world)
    clock.now = 6.0
    controller.update(camera, world)
    camera.position = np.array([5.0, 0.0, 0.0], dtype=np.float64)

    assert controller.update_replan(camera, world, now=6.0) is False
    assert replanner.requests == [(5.0, 0.0, 0.0)]


def test_auto_dive_swaps_latest_nearby_replan_on_owner_thread():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.array([1.0, 0.0, 0.0], dtype=np.float64))
    latest_plan = _plan(start=(1.0, 0.0, 0.0), end=(11.0, 0.0, 0.0))
    replanner = _FakeReplanner(latest_plan)
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        replan_only_during_survey=False,
    )
    controller.start(camera, world)
    camera.position = np.array([1.0, 0.0, 0.0], dtype=np.float64)

    assert controller.update_replan(camera, world, now=2.0) is True

    assert controller.plan is latest_plan
    assert controller.prefetch_cells


def test_auto_dive_replan_handoff_resumes_nearest_new_route_time():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.array([4.0, 0.0, 0.0], dtype=np.float64))
    latest_plan = _plan(start=(3.0, 0.0, 0.0), end=(13.0, 0.0, 0.0))
    replanner = _FakeReplanner(latest_plan)
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.5,
        replan_only_during_survey=False,
    )
    controller.start(camera, world)
    camera.position = np.array([4.0, 0.0, 0.0], dtype=np.float64)

    assert controller.update_replan(camera, world, now=5.0) is True

    assert controller.plan is latest_plan
    assert controller._elapsed_s == 1.0
    assert controller._started_at == 4.0


def test_auto_dive_rejects_replan_that_points_backward_on_current_route():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.array([5.0, 0.0, 0.0], dtype=np.float64))
    backward_plan = _plan(start=(5.0, 0.0, 0.0), end=(4.0, 0.0, 0.0))
    replanner = _FakeReplanner(backward_plan)
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        replan_only_during_survey=False,
    )
    controller.start(camera, world)
    controller._elapsed_s = 5.0
    camera.position = np.array([5.0, 0.0, 0.0], dtype=np.float64)

    assert controller.update_replan(camera, world, now=5.0) is False

    assert controller.plan is not backward_plan
    assert replanner.requests == []


def test_auto_dive_blackbox_records_replan_rejection_reason():
    clock = _FakeClock()
    world = _FakeWorld()
    blackbox = _FakeBlackbox()
    camera = _FakeCamera(position=np.array([5.0, 0.0, 0.0], dtype=np.float64))
    backward_plan = _plan(start=(5.0, 0.0, 0.0), end=(4.0, 0.0, 0.0))
    replanner = _FakeReplanner(backward_plan)
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        replan_only_during_survey=False,
        blackbox=blackbox,
    )
    controller.start(camera, world)
    controller._elapsed_s = 5.0
    camera.position = np.array([5.0, 0.0, 0.0], dtype=np.float64)

    assert controller.update_replan(camera, world, now=5.0) is False

    rejected = [
        payload
        for event, payload in blackbox.events
        if event == "replan_rejected"
    ]
    assert rejected
    assert rejected[-1]["reason"] == "moves_backward_from_current_route"


def test_auto_dive_blackbox_records_stuck_detection():
    clock = _FakeClock()
    world = _FakeWorld()
    blackbox = _FakeBlackbox()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        replan_distance_m=0.25,
        blackbox=blackbox,
    )
    controller.start(camera, world)
    world.loaded_cells = set(controller.prefetch_cells)
    world._pending = set()
    clock.now = 1.0
    controller.update(camera, world)

    controller.record_frame(camera, world, now=1.0)
    controller.record_frame(camera, world, now=3.1)

    stuck = [
        payload
        for event, payload in blackbox.events
        if event == "stuck_detected"
    ]
    assert stuck
    assert stuck[-1]["stuck_duration_s"] >= 2.0


def test_auto_dive_discards_stale_replan_that_starts_too_far_from_camera():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.array([5.0, 0.0, 0.0], dtype=np.float64))
    stale_plan = _plan(start=(0.0, 0.0, 0.0), end=(10.0, 0.0, 0.0))
    replanner = _FakeReplanner(stale_plan)
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        replan_only_during_survey=False,
    )
    controller.start(camera, world)
    camera.position = np.array([5.0, 0.0, 0.0], dtype=np.float64)

    assert controller.update_replan(camera, world, now=2.0) is False

    assert controller.plan is not stale_plan


def test_auto_dive_rejects_stalled_replan_and_does_not_retry_same_pose():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    stalled_plan = _plan(start=(0.25, 0.0, 0.0), end=(0.26, 0.0, 0.0))
    replanner = _FakeReplanner(stalled_plan)
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        replan_only_during_survey=False,
    )
    controller.start(camera, world)
    camera.position = np.array([0.25, 0.0, 0.0], dtype=np.float64)

    assert controller.update_replan(camera, world, now=2.0) is False
    assert controller.plan is not stalled_plan
    assert replanner.requests == []

    assert controller.update_replan(camera, world, now=2.1) is False
    assert replanner.requests == []

    camera.position = np.array([0.5, 0.0, 0.0], dtype=np.float64)
    assert controller.update_replan(camera, world, now=2.2) is False

    assert replanner.requests == [(0.5, 0.0, 0.0)]


def test_auto_dive_shutdown_stops_replanner():
    replanner = _FakeReplanner()
    blackbox = _FakeBlackbox()
    controller = AutoDiveController(
        _plan(),
        perf_counter=_FakeClock(),
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        blackbox=blackbox,
    )

    controller.stop(completed=True)

    assert replanner.shutdown_called is True
    assert blackbox.closed is True


def test_auto_dive_replanner_can_be_constructed_and_shutdown():
    replanner = AutoDiveReplanner({}, AutoDiveSettings())

    replanner.shutdown()


def _plan(
    *,
    start: tuple[float, float, float] = (0.0, 0.0, 0.0),
    end: tuple[float, float, float] = (10.0, 0.0, 0.0),
    render_distance_cells: int = 2,
) -> AutoDivePlan:
    route = CameraRoute.from_keyframes(
        (
            RouteKeyframe(0.0, start, 0.0, 0.0),
            RouteKeyframe(10.0, end, 0.0, 0.0),
        )
    )
    return AutoDivePlan(
        route=route,
        centerline_path=None,  # type: ignore[arg-type]
        route_points=(start, end),
        route_cells=((0, 0), (10, 0)),
        circular_arc=False,
        route_length_m=10.0,
        duration_s=10.0,
        render_distance_cells=render_distance_cells,
    )
