"""Tests for Guided Dive controller pause/resume behavior."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import math
import threading
import time

import numpy as np
import pytest

from caveviewer.core.navigation.autodive import (
    AutoDivePlan,
    AutoDivePlanningBudgetExceeded,
    AutoDiveSettings,
    NavigationVoxelGraphAuthorityError,
)
from caveviewer.core.navigation.route import CameraRoute, RouteKeyframe
from caveviewer.gui.autodive_controller import (
    AutoDiveController,
    AutoDiveReplanner,
    AutoDiveRollingClearanceReport,
    AutoDiveRollingClearanceWorker,
    AutoDiveState,
    AutoDiveVoxelPrefetchReport,
    AutoDiveVoxelPrefetchWorker,
    DEFAULT_AUTO_DIVE_REPLAN_MIN_INTERVAL_SECONDS,
    DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_BUDGET_S,
    DEFAULT_AUTO_DIVE_ROUTE_PREFETCH_RADIUS_CELLS,
    DEFAULT_AUTO_DIVE_ROUTE_LOOKAHEAD_SECONDS,
    DEFAULT_AUTO_DIVE_SURVEY_DURATION_SECONDS,
    DEFAULT_AUTO_DIVE_SURVEY_INTERVAL_SECONDS,
    DEFAULT_AUTO_DIVE_USER_ASSIST_MAX_SAMPLES,
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
    def __init__(self, latest_plan=None, *, has_pending=False):
        self.latest_plan = latest_plan
        self._has_pending = has_pending
        self.requests = []
        self.voxel_prefetch_requests = []
        self.voxel_reports = []
        self.shutdown_called = False

    def request(self, current_position, **kwargs):
        self.requests.append(
            (
                tuple(float(value) for value in np.asarray(current_position)),
                _safe_request_kwargs(kwargs),
            )
        )
        return True

    def take_latest_plan(self):
        plan = self.latest_plan
        self.latest_plan = None
        return plan

    def has_pending(self):
        return self._has_pending

    def request_voxel_prefetch(self, plan, **kwargs):
        self.voxel_prefetch_requests.append((plan, dict(kwargs)))
        return True

    def take_voxel_prefetch_report(self):
        if not self.voxel_reports:
            return None
        return self.voxel_reports.pop(0)

    def has_voxel_prefetch_pending(self):
        return False

    def shutdown(self):
        self.shutdown_called = True


class _SpeculativeFakeReplanner(_FakeReplanner):
    def __init__(self, latest_plan=None, *, has_pending=False):
        super().__init__(latest_plan, has_pending=has_pending)
        self.speculative_requests = []
        self._latest_source_sequence = None
        self._last_taken_source_sequence = None

    def request_speculative(self, current_position, **kwargs):
        self.speculative_requests.append(
            (
                tuple(float(value) for value in np.asarray(current_position)),
                _safe_request_kwargs(kwargs),
            )
        )
        self._has_pending = True
        return True

    def publish_speculative_plan(self, plan, *, source_plan_sequence):
        self.latest_plan = plan
        self._latest_source_sequence = int(source_plan_sequence)
        self._has_pending = False

    def take_latest_plan(self):
        plan = super().take_latest_plan()
        self._last_taken_source_sequence = self._latest_source_sequence
        self._latest_source_sequence = None
        return plan

    @property
    def last_taken_plan_source_sequence(self):
        return self._last_taken_source_sequence


class _ContinuousFakeReplanner(_FakeReplanner):
    def __init__(self, latest_plan=None, *, has_pending=False):
        super().__init__(latest_plan, has_pending=has_pending)
        self.continuous_requests = []
        self._continuous_pending = False
        self._continuous_result = None
        self._continuous_source_sequence = None
        self._last_continuous_source_sequence = None
        self._last_continuous_generation = None
        self.continuous_scan_failure_count = 0
        self.continuous_scan_last_failure_generation = None
        self.continuous_scan_last_failure_reason = None

    def request_continuous_scan(self, current_position, **kwargs):
        self.continuous_requests.append(
            (
                tuple(float(value) for value in np.asarray(current_position)),
                _safe_request_kwargs(kwargs),
            )
        )
        self._continuous_pending = True
        return True

    def take_latest_continuous_scan(self):
        plan = self._continuous_result
        self._continuous_result = None
        self._last_continuous_source_sequence = self._continuous_source_sequence
        self._continuous_source_sequence = None
        return plan

    @property
    def last_taken_continuous_scan_source_sequence(self):
        return self._last_continuous_source_sequence

    @property
    def last_taken_continuous_scan_generation(self):
        return self._last_continuous_generation

    def has_continuous_scan_pending(self):
        return self._continuous_pending

    def has_continuous_scan_result(self):
        return self._continuous_result is not None

    def publish_continuous_plan(self, plan, *, source_plan_sequence):
        self._continuous_result = plan
        self._continuous_source_sequence = int(source_plan_sequence)
        self._last_continuous_generation = len(self.continuous_requests)
        self._continuous_pending = False

    def cancel_continuous_scan(self):
        self._continuous_pending = False
        self._continuous_result = None
        self._continuous_source_sequence = None


class _FakeBlackbox:
    def __init__(self):
        self.events = []
        self.closed = False

    def record(self, event, **payload):
        self.events.append((event, payload))

    def close(self):
        self.closed = True


class _FakeRollingClearanceWorker:
    def __init__(self, report=None):
        self.report = report

    def request(self, *_args, **_kwargs):
        return False

    def poll(self):
        report = self.report
        self.report = None
        return report

    def shutdown(self):
        return None


def _safe_request_kwargs(kwargs):
    safe = {}
    for key, value in kwargs.items():
        if value is None:
            safe[key] = None
            continue
        if key == "avoid_positions":
            safe[key] = tuple(
                tuple(float(item) for item in np.asarray(position))
                for position in value
            )
            continue
        safe[key] = float(value)
    return safe


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


def test_auto_dive_rolling_clearance_worker_reports_frontier_off_render_thread():
    worker = AutoDiveRollingClearanceWorker()
    try:
        assert worker.request(
            _plan(replan_at_end=True, selection_reason="voxel_branch_lookahead"),
            plan_sequence=3,
            elapsed_s=6.0,
            trigger_distance_m=5.0,
            standoff_distance_m=2.0,
        ) is True
        report = None
        for _ in range(100):
            report = worker.poll()
            if report is not None:
                break
            time.sleep(0.005)

        assert report is not None
        assert report.plan_sequence == 3
        assert report.needs_replan is True
        assert report.safe_to_continue is True
        assert report.reason == "approaching_forward_clearance_boundary"
        assert report.sample_count >= 2
    finally:
        worker.shutdown()


def test_auto_dive_rolling_clearance_worker_reports_explicit_voxel_block():
    class _VoxelField:
        def probe_point(self, point, *, include_clearance=True):
            if float(point[0]) >= 4.0:
                return False, 0.0
            return True, 2.0

    worker = AutoDiveRollingClearanceWorker()
    try:
        assert worker.request(
            _plan(
                centerline_path=SimpleNamespace(
                    cached_voxel_volume=_VoxelField(),
                ),
            ),
            plan_sequence=4,
            elapsed_s=2.0,
            trigger_distance_m=5.0,
            standoff_distance_m=2.0,
        ) is True
        report = None
        for _ in range(100):
            report = worker.poll()
            if report is not None:
                break
            time.sleep(0.005)

        assert report is not None
        assert report.reason == "voxel_forward_clearance_blocked"
        assert report.voxel_occupied_count > 0
        assert report.safe_to_continue is False
    finally:
        worker.shutdown()


def test_auto_dive_voxel_prefetch_worker_materializes_predicted_horizon():
    class _Store:
        def __init__(self):
            self.resident = set()
            self.released = None

        def chunk_ids_for_point(self, point):
            return ("coarse-000000",) if point[0] < 5.0 else ("coarse-000001",)

        def resident_chunk_ids(self):
            return tuple(sorted(self.resident))

        def release_unused(self, keep_chunk_ids=()):
            self.released = tuple(keep_chunk_ids)
            self.resident.intersection_update(self.released)

        def stats(self):
            return {
                "backend": "fake_lru",
                "resident_chunk_count": len(self.resident),
            }

    class _Volume:
        def __init__(self):
            self.chunk_store = _Store()
            self.points = ()

        def prefetch_for_points(self, points):
            self.points = tuple(points)
            for point in points:
                self.chunk_store.resident.update(
                    self.chunk_store.chunk_ids_for_point(point)
                )
            return self.chunk_store.resident_chunk_ids()

    volume = _Volume()
    plan = _plan(
        centerline_path=SimpleNamespace(cached_voxel_volume=volume),
    )
    worker = AutoDiveVoxelPrefetchWorker()
    try:
        assert worker.request(
            plan,
            plan_sequence=4,
            elapsed_s=0.0,
            horizon_s=6.0,
            reason="test_horizon",
        ) is True
        report = None
        for _ in range(100):
            report = worker.take_latest_report()
            if report is not None:
                break
            time.sleep(0.005)

        assert report is not None
        assert report.plan_sequence == 4
        assert report.reason == "test_horizon"
        assert report.outcome == "prefetched"
        assert report.requested_chunk_count == 2
        assert report.resident_chunk_count == 2
        assert report.storage_backend == "fake_lru"
        assert len(volume.points) >= 2
        assert volume.chunk_store.released == (
            "coarse-000000",
            "coarse-000001",
        )
    finally:
        worker.shutdown()


def test_auto_dive_voxel_prefetch_report_is_bounded_and_json_safe():
    report = AutoDiveVoxelPrefetchReport(
        plan_sequence=2,
        elapsed_s=1.0,
        horizon_s=12.0,
        reason="test",
        outcome="prefetched",
        point_count=4,
        requested_chunk_count=20,
        resident_chunk_count=20,
        resident_chunk_ids=tuple(f"chunk-{index}" for index in range(20)),
        storage_backend="disk_lru",
        storage_stats={"backend": "disk_lru"},
        duration_ms=1.5,
    )

    payload = report.diagnostic_payload()

    assert len(payload["resident_chunk_ids"]) == 16
    assert payload["resident_chunk_ids_truncated"] is True
    assert payload["storage_stats"] == {"backend": "disk_lru"}


def test_auto_dive_schedules_navigation_prefetch_for_current_plan_horizon():
    volume = SimpleNamespace(prefetch_for_points=lambda _points: ())
    plan = _plan(centerline_path=SimpleNamespace(cached_voxel_volume=volume))
    replanner = _FakeReplanner()
    clock = _FakeClock()
    controller = AutoDiveController(
        plan,
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
    )

    controller.start(
        _FakeCamera(position=np.zeros(3, dtype=np.float64)),
        _FakeWorld(),
    )

    assert len(replanner.voxel_prefetch_requests) == 1
    _requested_plan, kwargs = replanner.voxel_prefetch_requests[0]
    assert kwargs["plan_sequence"] == 0
    assert kwargs["reason"] == "initial_plan"
    assert kwargs["horizon_s"] > 0.0


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

    assert replanner.requests == [
        (
            (0.25, 0.0, 0.0),
            {
                "current_yaw": 0.0,
                "current_pitch": 0.0,
                "current_roll": 0.0,
                "current_travel_yaw": 0.0,
                "current_travel_pitch": 0.0,
            },
        )
    ]


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


def test_auto_dive_default_survey_does_not_pause_or_scan_view():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    blackbox = _FakeBlackbox()
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        lookahead_seconds=1.0,
        blackbox=blackbox,
    )
    controller.start(camera, world)
    world.loaded_cells = set(world.available_cells)
    world._pending = set()

    clock.now = 1.0
    controller.update(camera, world)
    clock.now = 6.0
    controller.update(camera, world)

    position_after_six_seconds = camera.position.copy()
    yaw_after_six_seconds = camera.yaw

    assert controller.status_note == "Diving centerline"
    assert position_after_six_seconds[0] == 5.0

    clock.now = 6.5
    controller.update(camera, world)

    assert camera.position[0] > position_after_six_seconds[0]
    assert camera.yaw == yaw_after_six_seconds
    assert not any(event == "survey_started" for event, _payload in blackbox.events)


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
    world.loaded_cells = set(world.available_cells)
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


def test_auto_dive_survey_pause_does_not_emit_stuck_diagnostic():
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

    controller.record_frame(camera, world, now=6.0)
    controller.record_frame(camera, world, now=7.9)

    assert controller.status_note == "Surveying next passage"
    assert not any(event == "stuck_detected" for event, _payload in blackbox.events)


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
    world.loaded_cells = set(world.available_cells)
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
    assert replanner.requests == [
        (
            (0.25, 0.0, 0.0),
            {
                "current_yaw": 0.0,
                "current_pitch": 0.0,
                "current_roll": 0.0,
                "current_travel_yaw": 0.0,
                "current_travel_pitch": 0.0,
            },
        )
    ]


def test_auto_dive_survey_scoped_replanning_waits_for_survey_pause():
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
    assert replanner.requests == [
        (
            (5.0, 0.0, 0.0),
            {
                "current_yaw": 0.0,
                "current_pitch": 0.0,
                "current_roll": 0.0,
                "current_travel_yaw": 0.0,
                "current_travel_pitch": 0.0,
            },
        )
    ]


def test_auto_dive_mesh_trimmed_route_requests_assist_at_boundary():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    blackbox = _FakeBlackbox()
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(route_truncated_by_mesh=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        blackbox=blackbox,
    )
    controller.start(camera, world)
    world.loaded_cells = set(world.available_cells)
    world._pending = set()

    clock.now = 1.0
    controller.update(camera, world)

    clock.now = 12.0
    state = controller.update(camera, world)

    assert state is AutoDiveState.WAITING_FOR_USER
    assert controller.waiting_for_user_input
    assert not controller.active
    assert np.allclose(camera.position, np.array([10.0, 0.0, 0.0]))
    assert replanner.requests == []
    assert controller.status_note == "Guided Dive needs input"
    assert controller.show_loading_indicator is False
    assert controller.loading_progress_fraction is None
    assert replanner.shutdown_called is False
    assert world.prefetch == set()
    assist = [
        payload
        for event, payload in blackbox.events
        if event == "user_assist_requested"
    ]
    assert assist
    assert assist[-1]["reason"] == "mesh_truncated_boundary_reached"
    assert assist[-1]["details"]["route_truncated_by_mesh"] is True


def test_auto_dive_mesh_trimmed_route_does_not_start_recovery_scan():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    blackbox = _FakeBlackbox()
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(route_truncated_by_mesh=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        blackbox=blackbox,
    )
    controller.start(camera, world)
    world.loaded_cells = set(world.available_cells)
    world._pending = set()

    clock.now = 1.0
    controller.update(camera, world)
    clock.now = 5.0
    assert controller.update(camera, world) is AutoDiveState.DIVING
    moving_position = camera.position.copy()

    clock.now = 5.125
    assert controller.update(camera, world) is AutoDiveState.DIVING

    assert not np.allclose(camera.position, moving_position)
    assert replanner.requests == []
    assert not any(
        event == "mesh_recovery_scan_started"
        for event, _payload in blackbox.events
    )


def test_auto_dive_mesh_trimmed_route_waits_at_boundary_instead_of_replanning():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    blackbox = _FakeBlackbox()
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(route_truncated_by_mesh=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=2.0,
        blackbox=blackbox,
    )
    controller.start(camera, world)
    world.loaded_cells = set(world.available_cells)
    world._pending = set()

    clock.now = 1.0
    assert controller.update(camera, world) is AutoDiveState.DIVING

    clock.now = 5.0
    assert controller.update(camera, world) is AutoDiveState.DIVING
    assert np.linalg.norm(camera.position - np.array([4.0, 0.0, 0.0])) < 1e-9
    assert not any(
        event == "mesh_recovery_scan_started"
        for event, _payload in blackbox.events
    )

    clock.now = 6.1
    assert controller.update(camera, world) is AutoDiveState.DIVING
    assert np.linalg.norm(camera.position - np.array([5.1, 0.0, 0.0])) < 1e-9
    assert np.linalg.norm(camera.position - np.array([10.0, 0.0, 0.0])) > 1.0
    assert not controller.show_loading_indicator
    assert not any(
        event == "mesh_recovery_scan_started"
        for event, _payload in blackbox.events
    )
    assert replanner.requests == []

    clock.now = 11.1
    assert controller.update(camera, world) is AutoDiveState.WAITING_FOR_USER

    assert np.linalg.norm(camera.position - np.array([10.0, 0.0, 0.0])) < 1e-9
    assert replanner.requests == []


def test_auto_dive_user_resume_rejects_a_route_against_repositioning_direction():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(route_truncated_by_mesh=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
    )
    controller.start(camera, world)
    world.loaded_cells = set(world.available_cells)
    world._pending = set()

    clock.now = 1.0
    controller.update(camera, world)
    clock.now = 12.0
    assert controller.update(camera, world) is AutoDiveState.WAITING_FOR_USER
    assert np.allclose(camera.position, np.array([10.0, 0.0, 0.0]))

    camera.position = np.array([10.0, 0.0, 2.0], dtype=np.float64)
    camera.yaw = math.pi / 2.0
    camera.pitch = 0.2
    controller.observe_user_assist_position(camera.position)

    assert controller.resume_from_user_assist(camera, world, now=20.0) is True
    assert controller.state is AutoDiveState.LOADING
    assert controller.active
    request_position, request_pose = replanner.requests[-1]
    assert request_position == pytest.approx((10.0, 0.0, 2.0))
    assert request_pose["current_yaw"] == pytest.approx(math.pi / 2.0)
    assert request_pose["current_pitch"] == pytest.approx(0.2)
    assert request_pose["current_travel_yaw"] == pytest.approx(math.pi / 2.0)
    assert request_pose["current_travel_pitch"] == pytest.approx(0.0)
    assert request_pose["user_reposition"] == pytest.approx(1.0)
    assert request_pose["avoid_positions"] == ((10.0, 0.0, 0.0),)

    # The old route must not regain control while the user-resume plan is
    # still being built.
    held_position = camera.position.copy()
    assert controller.update(camera, world, now=20.1) is AutoDiveState.LOADING
    assert np.allclose(camera.position, held_position)

    replanner.latest_plan = _plan(start=(10.0, 0.0, 2.0), end=(10.0, 0.0, -8.0))
    assert controller.update_replan(camera, world, now=20.2) is False
    assert controller.state is AutoDiveState.WAITING_FOR_USER
    assert controller._user_resume_replan_pending is False
    assert replanner.shutdown_called is False


def test_auto_dive_user_assist_trace_records_bounded_navigation_metrics():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    blackbox = _FakeBlackbox()
    replanner = _FakeReplanner()
    plan = _plan(
        route_truncated_by_mesh=True,
        centerline_path=SimpleNamespace(footprint_cell_size=1.0),
        voxel_route_selection={
            "selection_reason": "voxel_branch_lookahead",
            "start_cell": [10, 0],
            "branch": {
                "branch_start_cell": [11, 0],
                "target_cell": [15, 0],
            },
            "branch_candidates": [
                {
                    "branch_start_cell": [11, 0],
                    "target_cell": [15, 0],
                    "continuation_distance_m": 12.0,
                },
                {
                    "branch_start_cell": [10, 1],
                    "target_cell": [10, 5],
                    "continuation_distance_m": 4.0,
                },
            ],
        },
    )
    controller = AutoDiveController(
        plan,
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        blackbox=blackbox,
    )
    controller.start(camera, world)
    world.loaded_cells = set(world.available_cells)
    world._pending = set()

    clock.now = 1.0
    controller.update(camera, world)
    clock.now = 12.0
    assert controller.update(camera, world) is AutoDiveState.WAITING_FOR_USER

    camera.position = np.array([11.0, 0.0, 0.0], dtype=np.float64)
    camera.yaw = 0.25
    controller.observe_user_assist_position(
        camera.position,
        now=12.1,
        yaw=camera.yaw,
        pitch=camera.pitch,
        roll=camera.roll,
        world=world,
    )
    camera.position = np.array([11.0, 0.0, 1.0], dtype=np.float64)
    camera.yaw = math.pi / 2.0
    controller.observe_user_assist_position(
        camera.position,
        now=12.4,
        yaw=camera.yaw,
        pitch=camera.pitch,
        roll=camera.roll,
        world=world,
    )
    controller.observe_user_assist_position(
        camera.position,
        now=13.6,
        yaw=camera.yaw,
        pitch=camera.pitch,
        roll=camera.roll,
        world=world,
    )
    controller.record_navigation_guard_clamp(
        before=np.array([12.0, 0.0, 1.0], dtype=np.float64),
        after=np.array([11.5, 0.0, 1.0], dtype=np.float64),
        vertical_band=(0.0, 2.0),
    )
    camera.position = np.array([12.0, 0.0, 1.0], dtype=np.float64)
    controller.observe_user_assist_position(
        camera.position,
        now=14.7,
        yaw=camera.yaw,
        pitch=camera.pitch,
        roll=camera.roll,
        world=world,
    )

    assert controller.resume_from_user_assist(camera, world, now=15.0) is True

    samples = [
        payload
        for event, payload in blackbox.events
        if event == "user_assist_sample"
    ]
    assert samples
    assert len(samples) <= DEFAULT_AUTO_DIVE_USER_ASSIST_MAX_SAMPLES
    assert samples[1]["world_cell"] == [11, 0, 0]
    assert samples[1]["footprint_cell"] == [11, 0]
    assert samples[1]["yaw_deg"] == pytest.approx(math.degrees(0.25))
    assert samples[1]["speed_m_per_second"] > 0.0

    completed = [
        payload
        for event, payload in blackbox.events
        if event == "user_assist_trace_completed"
    ][-1]
    assert completed["reason"] == "resume"
    assert completed["final_resume_position"] == [12.0, 0.0, 1.0]
    assert completed["total_distance_m"] == pytest.approx(3.0)
    assert completed["net_displacement_m"] == pytest.approx(math.sqrt(5.0))
    assert completed["turn_count"] >= 1
    assert completed["pause_count"] == 1
    assert completed["navigation_guard_clamp_count"] == 1
    assert completed["navigation_guard_clamp_distance_m"] == pytest.approx(0.5)
    assert completed["readiness_before_assist"] is not None
    assert completed["trace_policy"]["scope"] == "guided_dive_user_assist_only"
    assert completed["plan"]["voxel_route_selection"]["branch_candidates"]
    branch_trace = completed["voxel_branch_trace"]
    assert branch_trace["first_moving_footprint_cell"] == [11, 0]
    assert branch_trace["moved_toward_branch"]["branch_start_cell"] == [11, 0]

    resumed = [
        payload
        for event, payload in blackbox.events
        if event == "user_assist_resumed"
    ][-1]
    assert resumed["user_assist_trace"] == completed


def test_auto_dive_mesh_recovery_uses_route_vector_at_boundary():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(
            start=(-94.989978, 4.595492, 82.243261),
            end=(-79.158315, 4.595492, 15.831663),
            replan_at_end=True,
        ),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
    )
    controller.start(camera, world)
    world.loaded_cells = set(world.available_cells)
    world._pending = set()

    clock.now = 1.0
    controller.update(camera, world)
    clock.now = 12.0
    assert controller.update(camera, world) is AutoDiveState.LOADING
    clock.now = 12.6
    controller.update(camera, world)

    assert len(replanner.requests) == 1
    _request_position, request_pose = replanner.requests[0]
    expected_yaw = math.atan2(
        15.831663 - 82.243261,
        -79.158315 - -94.989978,
    )
    assert request_pose["current_travel_yaw"] == pytest.approx(expected_yaw)
    assert abs(request_pose["current_travel_pitch"]) < 1e-9
    assert request_pose["force_hemisphere_scan"] == pytest.approx(1.0)


def test_auto_dive_mesh_recovery_resets_attempt_budget_after_forward_handoff():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    blackbox = _FakeBlackbox()
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(replan_at_end=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        blackbox=blackbox,
    )
    controller.start(camera, world)
    world.loaded_cells = set(world.available_cells)
    world._pending = set()

    clock.now = 1.0
    controller.update(camera, world)
    clock.now = 12.0
    assert controller.update(camera, world) is AutoDiveState.LOADING
    clock.now = 12.6
    controller.update(camera, world)
    assert controller._mesh_recovery_attempts == 1

    replanner.latest_plan = _plan(
        start=(11.0, 0.0, 0.0),
        end=(20.0, 0.0, 0.0),
        replan_at_end=True,
    )
    camera.position = np.array((11.0, 0.0, 0.0), dtype=np.float64)
    assert controller.update_replan(camera, world, now=13.0) is True
    assert controller._mesh_recovery_attempts == 0

    accepted = [
        payload
        for event, payload in blackbox.events
        if event == "replan_accepted"
    ][-1]
    assert accepted["mesh_recovery_progressed"] is True
    assert accepted["mesh_recovery_attempts"] == 0


def test_auto_dive_mesh_recovery_requests_user_assist_when_replan_has_no_plan():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    blackbox = _FakeBlackbox()
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(replan_at_end=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        blackbox=blackbox,
    )
    controller.start(camera, world)
    world.loaded_cells = set(world.available_cells)
    world._pending = set()

    clock.now = 1.0
    controller.update(camera, world)
    clock.now = 12.0
    assert controller.update(camera, world) is AutoDiveState.LOADING
    clock.now = 12.6
    controller.update(camera, world)

    assert len(replanner.requests) == 1
    assert controller.update_replan(camera, world, now=12.7) is False

    assert controller.state is AutoDiveState.WAITING_FOR_USER
    assert controller.waiting_for_user_input is True
    assert controller.active is False
    assert replanner.shutdown_called is False
    assert world.prefetch == set()
    assist = [
        payload
        for event, payload in blackbox.events
        if event == "user_assist_requested"
    ]
    assert assist
    assert assist[-1]["reason"] == "mesh_recovery_replan_finished_without_plan"


def test_auto_dive_local_recovery_leg_replans_at_end():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(replan_at_end=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
    )
    controller.start(camera, world)
    world.loaded_cells = set(controller.prefetch_cells)
    world._pending = set()

    clock.now = 1.0
    controller.update(camera, world)
    clock.now = 12.0
    state = controller.update(camera, world)

    assert state is AutoDiveState.LOADING
    assert controller.active
    assert replanner.requests == []

    clock.now = 12.6
    controller.update(camera, world)

    assert len(replanner.requests) == 1


def test_auto_dive_rolling_recovery_scans_without_holding_before_safe_frontier():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(replan_at_end=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
    )
    controller.start(camera, world)
    controller._rolling_clearance_worker.shutdown()
    controller._rolling_clearance_worker = _FakeRollingClearanceWorker(
        AutoDiveRollingClearanceReport(
            plan_sequence=0,
            checked_elapsed_s=2.0,
            remaining_distance_m=8.0,
            trigger_distance_m=2.0,
            safe_elapsed_s=5.0,
            safe_to_continue=True,
            needs_replan=True,
            reason="approaching_forward_clearance_boundary",
            sample_count=2,
            maximum_turn_degrees=0.0,
            prepared_branch_count=1,
            prepared_goal_clearance_m=2.0,
        )
    )
    world.loaded_cells = set(world.available_cells)
    world._pending = set()

    clock.now = 1.0
    assert controller.update(camera, world) is AutoDiveState.DIVING
    clock.now = 2.0
    assert controller.update_replan(camera, world, now=2.0) is False
    assert controller._mesh_recovery_active()

    clock.now = 3.0
    assert controller.update(camera, world) is AutoDiveState.DIVING
    assert camera.position[0] == pytest.approx(2.0)
    assert len(replanner.requests) == 1

    clock.now = 6.0
    assert controller.update(camera, world) is AutoDiveState.LOADING
    assert camera.position[0] == pytest.approx(5.0)


def test_auto_dive_voxel_lookahead_requests_forward_replan_at_boundary():
    clock = _FakeClock()
    world = _FakeWorld()
    blackbox = _FakeBlackbox()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _FakeReplanner()
    controller = AutoDiveController(
        _plan(
            replan_at_end=True,
            selection_reason="voxel_branch_lookahead",
        ),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        blackbox=blackbox,
    )
    controller.start(camera, world)
    controller._rolling_clearance_worker.shutdown()
    controller._rolling_clearance_worker = _FakeRollingClearanceWorker()
    world.loaded_cells = set(controller.prefetch_cells)
    world._pending = set()

    clock.now = 1.0
    assert controller.update(camera, world) is AutoDiveState.DIVING
    controller._rolling_clearance_worker.report = AutoDiveRollingClearanceReport(
        plan_sequence=0,
        checked_elapsed_s=10.0,
        remaining_distance_m=1.0,
        trigger_distance_m=1.0,
        safe_elapsed_s=10.0,
        safe_to_continue=True,
        needs_replan=True,
        reason="approaching_forward_clearance_boundary",
        sample_count=2,
        maximum_turn_degrees=0.0,
        prepared_branch_count=1,
        prepared_goal_clearance_m=5.0,
    )
    clock.now = 11.0
    assert controller.update(camera, world) is AutoDiveState.LOADING
    assert controller._lookahead_replan_pending is True
    assert len(replanner.requests) == 1
    assert not any(
        event == "mesh_recovery_scan_started" for event, _payload in blackbox.events
    )

    replanner.latest_plan = _plan(
        start=(10.0, 0.0, 0.0),
        end=(20.0, 0.0, 0.0),
        replan_at_end=True,
        selection_reason="voxel_branch_lookahead",
    )
    assert controller.update_replan(camera, world, now=10.1) is True
    assert controller._lookahead_replan_pending is False
    assert controller.plan.route_points[-1] == (20.0, 0.0, 0.0)


def test_auto_dive_speculative_replan_keeps_current_route_active():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _SpeculativeFakeReplanner()
    controller = AutoDiveController(
        _long_plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_only_during_survey=True,
    )
    controller.start(camera, world)
    world.loaded_cells = set(controller.prefetch_cells)
    world._pending = set()

    clock.now = 1.0
    assert controller.update(camera, world) is AutoDiveState.DIVING
    clock.now = 11.0
    assert controller.update(camera, world) is AutoDiveState.DIVING
    assert controller.update_replan(camera, world, now=11.0) is False

    assert len(replanner.speculative_requests) == 1
    assert controller._speculative_replan_pending is True
    assert controller.state is AutoDiveState.DIVING
    assert controller.plan.route_points == ((0.0, 0.0, 0.0), (20.0, 0.0, 0.0))

    camera.position = np.array([10.5, 0.0, 0.0], dtype=np.float64)
    replanner.publish_speculative_plan(
        _long_plan(start=(10.5, 0.0, 0.0), end=(30.5, 0.0, 0.0)),
        source_plan_sequence=0,
    )

    assert controller.update_replan(camera, world, now=10.5) is True
    assert controller.state is AutoDiveState.DIVING
    assert controller._speculative_replan_pending is False
    assert controller.plan.route_points == (
        (10.5, 0.0, 0.0),
        (30.5, 0.0, 0.0),
    )


def test_auto_dive_discards_speculative_plan_from_an_older_route_sequence():
    clock = _FakeClock()
    world = _FakeWorld()
    blackbox = _FakeBlackbox()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _SpeculativeFakeReplanner()
    controller = AutoDiveController(
        _long_plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        blackbox=blackbox,
    )
    controller.start(camera, world)
    world.loaded_cells = set(controller.prefetch_cells)
    world._pending = set()

    clock.now = 1.0
    controller.update(camera, world)
    clock.now = 11.0
    controller.update(camera, world)
    controller.update_replan(camera, world, now=11.0)

    controller._plan_sequence = 1
    replanner.publish_speculative_plan(
        _long_plan(start=(10.0, 0.0, 0.0), end=(30.0, 0.0, 0.0)),
        source_plan_sequence=0,
    )

    assert controller.update_replan(camera, world, now=11.1) is False
    assert controller.plan.route_points == (
        (0.0, 0.0, 0.0),
        (20.0, 0.0, 0.0),
    )
    # The stale result is discarded, then the new current sequence may use
    # the single planner slot again while the accepted route continues.
    assert controller._speculative_replan_pending is True
    assert len(replanner.speculative_requests) == 2
    assert controller.waiting_for_user_input is False
    discarded = [
        payload
        for event, payload in blackbox.events
        if event == "replan_rejected"
    ]
    assert discarded[-1]["reason"] == "stale_source_plan_sequence"
    assert discarded[-1]["speculative_replan"] is True


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

    assert replanner.requests == [
        (
            (0.5, 0.0, 0.0),
            {
                "current_yaw": 0.0,
                "current_pitch": 0.0,
                "current_roll": 0.0,
                "current_travel_yaw": 0.0,
                "current_travel_pitch": 0.0,
            },
        )
    ]


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


def test_auto_dive_replanner_forwards_cache_dir_to_plan_builder():
    calls = []

    def build_plan(manifest, **kwargs):
        calls.append((manifest, kwargs))
        return _plan()

    replanner = AutoDiveReplanner(
        {"chunks": {}},
        AutoDiveSettings(),
        plan_builder=build_plan,
        cache_dir="/cache/cave",
    )

    _generation, plan = replanner._build_plan(1, (1.0, 2.0, 3.0))
    replanner.shutdown()

    assert plan is not None
    assert calls[0][1]["cache_dir"] == "/cache/cave"


def test_auto_dive_replanner_preserves_preflight_navigation_route_id():
    calls = []

    def build_plan(manifest, **kwargs):
        calls.append((manifest, kwargs))
        return _plan()

    replanner = AutoDiveReplanner(
        {"chunks": {}},
        AutoDiveSettings(),
        plan_builder=build_plan,
        navigation_route_id="longest-passage",
    )

    _generation, plan = replanner._build_plan(1, (1.0, 2.0, 3.0))
    replanner.shutdown()

    assert plan is not None
    assert calls[0][1]["route_id"] == "longest-passage"


def test_auto_dive_replanner_preserves_speculative_source_sequence():
    blackbox = _FakeBlackbox()
    replanner = AutoDiveReplanner(
        {"chunks": {}},
        AutoDiveSettings(),
        plan_builder=lambda _manifest, **_kwargs: _plan(),
        blackbox=blackbox,
    )
    try:
        assert replanner.request_speculative(
            (0.0, 0.0, 0.0),
            source_plan_sequence=7,
        ) is True
        latest_plan = None
        for _ in range(100):
            latest_plan = replanner.take_latest_plan()
            if latest_plan is not None:
                break
            time.sleep(0.005)

        assert latest_plan is not None
        assert replanner.last_taken_plan_source_sequence == 7
        requested = [
            payload
            for event, payload in blackbox.events
            if event == "replan_requested"
        ][-1]
        completed = [
            payload
            for event, payload in blackbox.events
            if event == "replan_completed"
        ][-1]
        assert requested["speculative_replan"] is True
        assert requested["source_plan_sequence"] == 7
        assert completed["speculative_replan"] is True
        assert completed["source_plan_sequence"] == 7
    finally:
        replanner.shutdown()


def test_auto_dive_replanner_continuous_scan_is_bounded_and_sequence_safe():
    calls = []
    blackbox = _FakeBlackbox()

    def build_plan(manifest, **kwargs):
        calls.append((manifest, kwargs))
        return _plan()

    replanner = AutoDiveReplanner(
        {"chunks": {}},
        AutoDiveSettings(),
        plan_builder=build_plan,
        blackbox=blackbox,
    )
    try:
        assert replanner.request_continuous_scan(
            (1.0, 2.0, 3.0),
            current_yaw=0.25,
            current_pitch=-0.1,
            source_plan_sequence=7,
        ) is True
        for _ in range(100):
            if replanner.has_continuous_scan_result():
                break
            time.sleep(0.005)

        assert replanner.has_continuous_scan_result() is True
        assert replanner.take_latest_continuous_scan() is not None
        assert replanner.last_taken_continuous_scan_source_sequence == 7
        assert calls[0][1]["settings"].planning_budget_s == pytest.approx(
            DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_BUDGET_S,
            abs=0.05,
        )
        assert calls[0][1]["settings"].planning_budget_s > 0.0
        assert calls[0][1]["force_hemisphere_scan"] is True
        assert any(
            event == "continuous_scan_completed"
            for event, _payload in blackbox.events
        )
    finally:
        replanner.shutdown()


def test_auto_dive_replanner_discards_scan_that_misses_deadline():
    blackbox = _FakeBlackbox()

    def build_plan(manifest, **kwargs):
        del manifest, kwargs
        time.sleep(0.02)
        return _plan()

    replanner = AutoDiveReplanner(
        {"chunks": {}},
        AutoDiveSettings(),
        plan_builder=build_plan,
        blackbox=blackbox,
    )
    try:
        assert replanner.request_continuous_scan(
            (0.0, 0.0, 0.0),
            scan_budget_s=0.005,
        ) is True
        for _ in range(100):
            if not replanner.has_continuous_scan_pending():
                break
            time.sleep(0.005)

        assert replanner.has_continuous_scan_result() is False
        assert replanner.continuous_scan_last_failure_reason == (
            "deadline_exceeded"
        )
        assert replanner.continuous_scan_last_failure_outcome.value == (
            "deadline_exceeded"
        )
        failed = [
            payload
            for event, payload in blackbox.events
            if event == "continuous_scan_failed"
        ][-1]
        assert failed["scan_outcome"] == "deadline_exceeded"
        assert failed["failure_reason"] == "deadline_exceeded"
    finally:
        replanner.shutdown()


def test_auto_dive_continuous_scan_uses_one_authoritative_fallback_at_frontier():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _ContinuousFakeReplanner()
    controller = AutoDiveController(
        _plan(replan_at_end=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        blackbox=_FakeBlackbox(),
    )

    controller.start(camera, world)
    replanner._continuous_pending = False
    controller._elapsed_s = 9.0

    assert controller._request_continuous_scan_if_needed(
        camera,
        now=9.0,
        reason="safe_frontier_deadline",
        world=world,
    ) is True
    assert len(replanner.continuous_requests) == 1
    assert len(replanner.requests) == 1
    assert replanner.requests[0][1]["force_hemisphere_scan"] == pytest.approx(1.0)
    assert controller._lookahead_replan_pending is True
    assert controller._continuous_scan_fallback_attempted is True

    controller.stop(world)


def test_auto_dive_holds_frontier_ended_scan_until_expansion():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    selection = {
        "graph_snapshot": {
            "cache_version": 8,
            "cache_method": "whole_cave_voxel_atlas_v8",
            "graph_method": "navigation_voxel_graph_3d_v1",
            "node_count": 20,
            "edge_count": 19,
        },
        "graph_keys": [[0, 0, 0], [1, 0, 0]],
        "executed_start_graph_key": [0, 0, 0],
        "unknown_boundary_reached": True,
        "branch": {
            "branch_start_key": [0, 0, 0],
            "target_key": [1, 0, 0],
            "frontier_count": 0,
            "onward_exit_count": 0,
            "unknown_boundary": True,
        },
    }
    initial = _plan(
        replan_at_end=True,
        voxel_route_selection=selection,
    )
    expansion = _plan(
        end=(12.0, 0.0, 0.0),
        replan_at_end=True,
        selection_reason="continuous_local_frontier_expansion",
        voxel_route_selection=selection,
    )
    replanner = _ContinuousFakeReplanner()
    controller = AutoDiveController(
        _plan(replan_at_end=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        replan_only_during_survey=False,
    )

    controller.start(camera, world)
    replanner.publish_continuous_plan(initial, source_plan_sequence=0)

    assert controller.update_replan(camera, world, now=1.0) is False
    assert controller.plan is not initial
    assert len(replanner.continuous_requests) == 2
    assert replanner.continuous_requests[-1][1]["expand_frontier"] == 1.0

    replanner.publish_continuous_plan(expansion, source_plan_sequence=0)
    assert controller.update_replan(camera, world, now=1.1) is True
    assert controller.plan is expansion

    controller.stop(world)


def test_auto_dive_does_not_start_full_scan_with_less_than_seven_safe_seconds():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    blackbox = _FakeBlackbox()
    replanner = _ContinuousFakeReplanner()
    controller = AutoDiveController(
        _plan(replan_at_end=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        blackbox=blackbox,
    )

    controller.start(camera, world)
    replanner._continuous_pending = False
    controller._elapsed_s = 4.0

    assert controller._request_continuous_scan_if_needed(
        camera,
        now=4.0,
        reason="safe_frontier_deadline",
        world=world,
    ) is True
    assert len(replanner.continuous_requests) == 1
    assert len(replanner.requests) == 1
    fallback = [
        payload
        for event, payload in blackbox.events
        if event == "continuous_scan_deadline_fallback_requested"
    ][-1]
    assert fallback["reason"] == "safe_frontier_horizon_too_short"

    controller.stop(world)


def test_auto_dive_replanner_retains_fatal_mesh_guard_failure_reason():
    blackbox = _FakeBlackbox()

    def build_plan(manifest, **kwargs):
        del manifest, kwargs
        raise NavigationVoxelGraphAuthorityError(
            "mesh guard unavailable",
            reason="mesh_collision_guard_unavailable",
            status={"available": False},
        )

    replanner = AutoDiveReplanner(
        {"chunks": {}},
        AutoDiveSettings(),
        plan_builder=build_plan,
        blackbox=blackbox,
    )
    try:
        assert replanner.request_continuous_scan((0.0, 0.0, 0.0)) is True
        for _ in range(100):
            if not replanner.has_continuous_scan_pending():
                break
            time.sleep(0.005)

        assert replanner.continuous_scan_failure_count == 1
        assert replanner.continuous_scan_last_failure_reason == (
            "mesh_collision_guard_unavailable"
        )
        failed = [
            payload
            for event, payload in blackbox.events
            if event == "continuous_scan_failed"
        ][-1]
        assert failed["failure_reason"] == "mesh_collision_guard_unavailable"
    finally:
        replanner.shutdown()


def test_auto_dive_accepts_continuous_scan_and_starts_next_cycle():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    blackbox = _FakeBlackbox()
    replanner = _ContinuousFakeReplanner()
    controller = AutoDiveController(
        _plan(replan_at_end=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        blackbox=blackbox,
    )

    controller.start(camera, world)
    assert len(replanner.continuous_requests) == 1
    replanner.publish_continuous_plan(
        _plan(start=(0.0, 0.0, 0.0), end=(12.0, 0.0, 0.0)),
        source_plan_sequence=0,
    )

    assert controller.update_replan(camera, world, now=1.0) is True
    accepted = [
        payload
        for event, payload in blackbox.events
        if event == "replan_accepted"
    ][-1]
    assert accepted["continuous_scan"] is True
    assert accepted["source_plan_sequence"] == 0
    assert len(replanner.continuous_requests) == 2

    controller.stop(world)


def test_auto_dive_continuous_scan_forwards_frontier_expansion_request():
    blackbox = _FakeBlackbox()
    calls = []

    def build_plan(manifest, **kwargs):
        calls.append((manifest, kwargs))
        return _plan()

    replanner = AutoDiveReplanner(
        {"chunks": {}},
        AutoDiveSettings(),
        plan_builder=build_plan,
        blackbox=blackbox,
    )
    try:
        assert replanner.request_continuous_scan(
            (1.0, 2.0, 3.0),
            current_travel_yaw=0.5,
            expand_frontier=True,
        ) is True
        for _ in range(100):
            if replanner.has_continuous_scan_result():
                break
            time.sleep(0.005)

        assert replanner.has_continuous_scan_result() is True
        assert calls[0][1]["expand_frontier"] is True
        completed = [
            payload
            for event, payload in blackbox.events
            if event == "continuous_scan_completed"
        ][-1]
        assert completed["expand_frontier"] is True
        assert completed["scan_outcome"] == "route_ready"
    finally:
        replanner.shutdown()


def test_auto_dive_rejects_same_continuous_frontier_after_one_bounded_expansion():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    blackbox = _FakeBlackbox()
    selection = {
        "graph_snapshot": {
            "cache_version": 7,
            "cache_method": "navigation_voxel_cache_v3",
            "graph_method": "navigation_voxel_graph_3d_v1",
            "node_count": 20,
            "edge_count": 19,
        },
        "graph_keys": [[-2, 6, 2], [-2, 5, 1]],
        "executed_start_graph_key": [-2, 6, 2],
        "unknown_boundary_reached": True,
        "branch": {
            "branch_start_key": [-2, 6, 2],
            "target_key": [-2, 5, 1],
            "frontier_count": 0,
            "onward_exit_count": 0,
            "unknown_boundary": True,
        },
    }
    stalled = _plan(
        start=(0.0, 0.0, 0.0),
        end=(0.01, 0.0, 0.0),
        replan_at_end=True,
        voxel_route_selection=selection,
    )
    replanner = _ContinuousFakeReplanner()
    controller = AutoDiveController(
        _plan(replan_at_end=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        replan_only_during_survey=False,
        blackbox=blackbox,
    )

    controller.start(camera, world)
    replanner.publish_continuous_plan(stalled, source_plan_sequence=0)
    assert controller.update_replan(camera, world, now=1.0) is False
    assert controller.state is not AutoDiveState.WAITING_FOR_USER
    assert len(replanner.continuous_requests) == 2
    assert replanner.continuous_requests[-1][1]["expand_frontier"] == 1.0

    replanner.publish_continuous_plan(stalled, source_plan_sequence=0)
    assert controller.update_replan(camera, world, now=1.1) is False
    assert controller.state is AutoDiveState.WAITING_FOR_USER
    assert len(replanner.continuous_requests) == 2
    assert any(
        event == "continuous_scan_frontier_exhausted"
        for event, _payload in blackbox.events
    )


def test_auto_dive_holds_safe_route_when_mesh_guard_disappears():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _ContinuousFakeReplanner()
    controller = AutoDiveController(
        _plan(replan_at_end=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        blackbox=_FakeBlackbox(),
    )

    controller.start(camera, world)
    replanner._continuous_pending = False
    replanner.continuous_scan_failure_count = 3
    replanner.continuous_scan_last_failure_reason = (
        "mesh_collision_guard_unavailable"
    )

    assert controller._request_continuous_scan_if_needed(
        camera,
        now=1.0,
        reason="runtime_mesh_guard_loss",
        world=world,
    ) is False
    assert controller.state is AutoDiveState.WAITING_FOR_USER


def test_auto_dive_handoff_skips_near_duplicate_graph_anchor():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    route_plan = _plan(start=(0.0, 0.0, 0.0), end=(4.0, 0.0, 0.0))
    route_plan = route_plan.__class__(
        **{
            **route_plan.__dict__,
            "route_points": (
                (0.0, 0.0, 0.0),
                (0.01, 0.0, 0.0),
                (4.0, 0.0, 0.0),
            ),
            "route_length_m": 4.0,
        }
    )
    replanner = _FakeReplanner(route_plan)
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        replan_distance_m=0.25,
        replan_only_during_survey=False,
    )

    controller.start(camera, world)
    assert controller.update_replan(camera, world, now=1.0) is True
    assert controller.plan is route_plan


def test_auto_dive_continuous_mesh_recovery_does_not_start_bounded_wait():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    blackbox = _FakeBlackbox()
    replanner = _ContinuousFakeReplanner()
    controller = AutoDiveController(
        _plan(replan_at_end=True),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        blackbox=blackbox,
    )
    controller.start(camera, world)
    clock.now = 1.0
    controller.update(camera, world)
    world.loaded_cells = set(world.available_cells)
    world._pending = set()

    clock.now = 12.0
    controller.update(camera, world)
    clock.now = 22.0
    controller.update(camera, world)
    clock.now = 22.6
    controller.update(camera, world)
    clock.now = 29.0
    controller.update_replan(camera, world)

    assert controller.state is not AutoDiveState.WAITING_FOR_USER
    assert controller._replan_wait_started_at is None
    assert not any(
        event == "replan_planning_budget_exceeded"
        for event, _payload in blackbox.events
    )
    assert any(
        payload.get("continuous_scan") is True
        for event, payload in blackbox.events
        if event == "mesh_recovery_replan_requested"
    )

    controller.stop(world)


def test_auto_dive_replanner_logs_correlated_build_timing():
    clock = _FakeClock()
    blackbox = _FakeBlackbox()

    def build_plan(manifest, **kwargs):
        diagnostics = kwargs.get("diagnostics")
        if diagnostics is not None:
            diagnostics(
                "candidate_scores",
                {"selected": "raw", "decision_duration_ms": 1.5},
            )
        return _plan()

    replanner = AutoDiveReplanner(
        {"chunks": {}},
        AutoDiveSettings(),
        plan_builder=build_plan,
        blackbox=blackbox,
        perf_counter=clock,
    )

    _generation, plan = replanner._build_plan(
        3,
        (1.0, 2.0, 3.0),
        request_started_at=-1.0,
    )
    replanner.shutdown()

    assert plan is not None
    started = [
        payload
        for event, payload in blackbox.events
        if event == "replan_build_started"
    ][-1]
    completed = [
        payload
        for event, payload in blackbox.events
        if event == "replan_completed"
    ][-1]
    diagnostic = [
        payload
        for event, payload in blackbox.events
        if event == "candidate_scores"
    ][-1]

    assert started["replan_id"] == "replan-3"
    assert started["generation"] == 3
    assert started["queue_duration_ms"] == pytest.approx(1000.0)
    assert completed["replan_id"] == "replan-3"
    assert completed["build_duration_ms"] == pytest.approx(0.0)
    assert completed["total_duration_ms"] == pytest.approx(1000.0)
    assert diagnostic["replan_id"] == "replan-3"
    assert diagnostic["replan_generation"] == 3


def test_auto_dive_replanner_applies_budget_to_runtime_settings_and_logs_failure():
    blackbox = _FakeBlackbox()
    captured_settings = []

    def build_plan(manifest, **kwargs):
        captured_settings.append(kwargs["settings"])
        raise AutoDivePlanningBudgetExceeded(
            budget_s=0.25,
            elapsed_s=0.3,
            phase="test",
        )

    replanner = AutoDiveReplanner(
        {"chunks": {}},
        AutoDiveSettings(),
        plan_builder=build_plan,
        blackbox=blackbox,
        planning_budget_s=0.25,
    )

    with pytest.raises(AutoDivePlanningBudgetExceeded):
        replanner._build_plan(4, (1.0, 2.0, 3.0))
    replanner.shutdown()

    assert captured_settings[0].planning_budget_s == pytest.approx(0.25)
    failed = [
        payload
        for event, payload in blackbox.events
        if event == "replan_failed"
    ][-1]
    assert failed["error_type"] == "AutoDivePlanningBudgetExceeded"
    assert failed["planning_budget_s"] == pytest.approx(0.25)


def test_auto_dive_hands_off_when_replan_wait_exceeds_budget():
    clock = _FakeClock()
    world = _FakeWorld()
    blackbox = _FakeBlackbox()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    replanner = _FakeReplanner(has_pending=True)
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        replanner=replanner,  # type: ignore[arg-type]
        blackbox=blackbox,
    )
    controller.start(camera, world, now=0.0)
    controller._lookahead_replan_pending = True
    controller._begin_replan_wait("lookahead", now=0.0)

    state = controller.update(
        camera,
        world,
        now=controller._replan_planning_budget_s + 0.01,
    )

    assert state is AutoDiveState.WAITING_FOR_USER
    assert controller._lookahead_replan_pending is False
    exceeded = [
        payload
        for event, payload in blackbox.events
        if event == "replan_planning_budget_exceeded"
    ][-1]
    assert exceeded["kind"] == "lookahead"
    assist = [
        payload
        for event, payload in blackbox.events
        if event == "user_assist_requested"
    ][-1]
    assert assist["reason"] == "replan_planning_budget_exceeded"


def test_auto_dive_frame_and_stop_log_motion_and_prefetch_summary():
    clock = _FakeClock()
    world = _FakeWorld()
    blackbox = _FakeBlackbox()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    controller = AutoDiveController(
        _plan(),
        perf_counter=clock,
        blackbox=blackbox,
    )

    controller.start(camera, world)
    world.loaded_cells = set(world.available_cells)
    world._pending = set()
    clock.now = 2.0
    controller.update(camera, world)
    clock.now = 3.0
    controller.update(camera, world)
    camera.position = np.array([1.25, 0.0, 0.0], dtype=np.float64)
    controller.record_frame(camera, world, now=3.0)

    frame = [
        payload
        for event, payload in blackbox.events
        if event == "frame"
    ][-1]
    assert frame["command_error_m"] == pytest.approx(0.25)
    assert frame["observed_displacement_m"] == pytest.approx(1.25)
    assert frame["observed_distance_m"] == pytest.approx(1.25)
    assert frame["plan_sequence"] == 0
    assert frame["prefetch"]["cell_count"] == len(controller.prefetch_cells)
    assert frame["prefetch"]["cell_sample"]

    controller.stop(world)
    stopped = [
        payload
        for event, payload in blackbox.events
        if event == "auto_dive_stopped"
    ][-1]
    assert stopped["outcome"] == "stopped"
    assert stopped["observed_distance_m"] == pytest.approx(1.25)
    assert stopped["final_command_error_m"] == pytest.approx(0.25)


def test_auto_dive_mesh_safe_frontier_plan_moves_before_boundary_replan():
    clock = _FakeClock()
    world = _FakeWorld()
    camera = _FakeCamera(position=np.zeros(3, dtype=np.float64))
    controller = AutoDiveController(
        _plan(
            replan_at_end=True,
            selection_reason="preflight_mesh_safe_graph_frontier",
        ),
        perf_counter=clock,
    )

    controller.start(camera, world, now=0.0)
    world.loaded_cells = set(world.available_cells)
    controller.update(camera, world)
    clock.now = 5.0

    state = controller.update(camera, world)

    assert state is AutoDiveState.DIVING
    assert camera.position[0] == pytest.approx(5.0)
    controller.stop(world)


def _plan(
    *,
    start: tuple[float, float, float] = (0.0, 0.0, 0.0),
    end: tuple[float, float, float] = (10.0, 0.0, 0.0),
    render_distance_cells: int = 2,
    route_truncated_by_mesh: bool = False,
    replan_at_end: bool = False,
    selection_reason: str | None = None,
    centerline_path=None,
    voxel_route_selection: dict[str, object] | None = None,
) -> AutoDivePlan:
    route = CameraRoute.from_keyframes(
        (
            RouteKeyframe(0.0, start, 0.0, 0.0),
            RouteKeyframe(10.0, end, 0.0, 0.0),
        )
    )
    return AutoDivePlan(
        route=route,
        centerline_path=centerline_path,  # type: ignore[arg-type]
        route_points=(start, end),
        route_cells=((0, 0), (10, 0)),
        circular_arc=False,
        route_length_m=10.0,
        duration_s=10.0,
        render_distance_cells=render_distance_cells,
        route_truncated_by_mesh=route_truncated_by_mesh,
        mesh_safe_prefix_length_m=10.0 if route_truncated_by_mesh else None,
        replan_at_end=replan_at_end,
        selection_reason=(
            selection_reason
            if selection_reason is not None
            else (
                "mesh_compromised_prefix_fallback"
                if route_truncated_by_mesh
                else "trusted_route_clear"
                if replan_at_end
                else ""
            )
        ),
        voxel_route_selection=voxel_route_selection,
    )


def _long_plan(
    *,
    start: tuple[float, float, float] = (0.0, 0.0, 0.0),
    end: tuple[float, float, float] = (20.0, 0.0, 0.0),
) -> AutoDivePlan:
    route = CameraRoute.from_keyframes(
        (
            RouteKeyframe(0.0, start, 0.0, 0.0),
            RouteKeyframe(20.0, end, 0.0, 0.0),
        )
    )
    return AutoDivePlan(
        route=route,
        centerline_path=None,  # type: ignore[arg-type]
        route_points=(start, end),
        route_cells=((0, 0), (20, 0)),
        circular_arc=False,
        route_length_m=20.0,
        duration_s=20.0,
        render_distance_cells=2,
        selection_reason="trusted_route_clear",
    )
