"""Guided Dive playback and streaming-readiness state for the viewer."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
import math
import threading
from typing import Any

import numpy as np

from caveviewer.core.navigation.autodive import (
    AutoDivePlan,
    AutoDiveSettings,
    build_centerline_auto_dive_plan,
    route_progress_fraction,
)
from caveviewer.core.navigation.route import apply_pose_to_camera


DEFAULT_AUTO_DIVE_ROUTE_LOOKAHEAD_SECONDS = 6.0
DEFAULT_AUTO_DIVE_REPLAN_MIN_INTERVAL_SECONDS = 1.0
DEFAULT_AUTO_DIVE_ROUTE_PREFETCH_RADIUS_CELLS = 2
# Survey pauses intentionally remain opt-in: visible route-time pauses and
# yaw/pitch sweeps read as jitter during normal interactive Guided Dive playback.
DEFAULT_AUTO_DIVE_SURVEY_INTERVAL_SECONDS = 0.0
DEFAULT_AUTO_DIVE_SURVEY_DURATION_SECONDS = 0.0
DEFAULT_AUTO_DIVE_SURVEY_PREFETCH_LOOKAHEAD_MULTIPLIER = 2.0
DEFAULT_AUTO_DIVE_SURVEY_PREFETCH_RADIUS_BONUS_CELLS = 1
DEFAULT_AUTO_DIVE_SURVEY_YAW_SWEEP_DEGREES = 28.0
DEFAULT_AUTO_DIVE_SURVEY_PITCH_SWEEP_DEGREES = 5.0
DEFAULT_AUTO_DIVE_MESH_RECOVERY_TURN_SECONDS = 0.5
DEFAULT_AUTO_DIVE_MESH_RECOVERY_YAW_DEGREES = 180.0
DEFAULT_AUTO_DIVE_MESH_RECOVERY_PITCH_DEGREES = 8.0
# One automatic recovery is enough to cover a transient planning miss. More
# retries make the camera appear indecisive and can repeatedly select the same
# unsafe branch before the user gets a meaningful handoff.
DEFAULT_AUTO_DIVE_MESH_RECOVERY_MAX_ATTEMPTS = 1
DEFAULT_AUTO_DIVE_MESH_RECOVERY_STANDOFF_CELLS = 2.0
DEFAULT_AUTO_DIVE_MESH_RECOVERY_MIN_STANDOFF_M = 15.0
DEFAULT_AUTO_DIVE_MESH_RECOVERY_MAX_STANDOFF_FRACTION = 0.5
DEFAULT_AUTO_DIVE_USER_ASSIST_MOVEMENT_THRESHOLD_M = 0.1


class AutoDiveState(str, Enum):
    """Lifecycle states for user-facing Guided Dive."""

    IDLE = "idle"
    LOADING = "loading"
    DIVING = "diving"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AutoDiveReadiness:
    """Route-lookahead streaming readiness for Guided Dive UI decisions."""

    expected_cells: int
    loaded_cells: int
    pending_cells: int
    failed_cells: int
    missing_cells: int
    progress: float

    @property
    def ready(self) -> bool:
        return self.missing_cells == 0


class AutoDiveReplanner:
    """Single-worker receding-horizon Guided Dive replanner.

    The worker owns only pure route planning. It never touches camera, world,
    streaming, minimap, or OpenGL state; the render thread consumes the latest
    completed plan and decides whether it is still close enough to swap in.
    """

    def __init__(
        self,
        manifest: Any,
        settings: AutoDiveSettings,
        *,
        plan_builder: Callable[..., AutoDivePlan] = build_centerline_auto_dive_plan,
        cache_dir: str | None = None,
        blackbox: Any | None = None,
    ) -> None:
        self._manifest = manifest
        self._settings = settings
        self._plan_builder = plan_builder
        self._cache_dir = cache_dir
        self._blackbox = blackbox
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="AutoDiveReplanner",
        )
        self._lock = threading.Lock()
        self._generation = 0
        self._latest_generation = 0
        self._latest_plan: AutoDivePlan | None = None
        self._pending_future: Future | None = None
        self._shutdown = False

    def request(
        self,
        current_position: np.ndarray | tuple[float, float, float],
        *,
        current_yaw: float | None = None,
        current_pitch: float | None = None,
        current_travel_yaw: float | None = None,
        current_travel_pitch: float | None = None,
        avoid_positions: Sequence[Sequence[float]] | None = None,
        user_reposition: bool = False,
    ) -> bool:
        """Queue one replan from the current camera position if none is pending."""
        position = tuple(
            float(value)
            for value in np.asarray(current_position, dtype=np.float64).reshape(3)
        )
        yaw = None if current_yaw is None else float(current_yaw)
        pitch = None if current_pitch is None else float(current_pitch)
        travel_yaw = (
            None if current_travel_yaw is None else float(current_travel_yaw)
        )
        travel_pitch = (
            None if current_travel_pitch is None else float(current_travel_pitch)
        )
        avoided = _normalized_avoid_positions(avoid_positions)
        with self._lock:
            if self._shutdown:
                self._record_blackbox(
                    "replan_request_skipped",
                    reason="shutdown",
                    position=position,
                    yaw=yaw,
                    pitch=pitch,
                    travel_yaw=travel_yaw,
                    travel_pitch=travel_pitch,
                    avoid_positions=avoided,
                    user_reposition=bool(user_reposition),
                )
                return False
            if self._pending_future is not None and not self._pending_future.done():
                self._record_blackbox(
                    "replan_request_skipped",
                    reason="already_pending",
                    position=position,
                    yaw=yaw,
                    pitch=pitch,
                    travel_yaw=travel_yaw,
                    travel_pitch=travel_pitch,
                    avoid_positions=avoided,
                    user_reposition=bool(user_reposition),
                )
                return False
            self._generation += 1
            generation = self._generation
            self._record_blackbox(
                "replan_requested",
                generation=generation,
                position=position,
                yaw=yaw,
                pitch=pitch,
                travel_yaw=travel_yaw,
                travel_pitch=travel_pitch,
                avoid_positions=avoided,
                user_reposition=bool(user_reposition),
            )
            future = self._executor.submit(
                self._build_plan,
                generation,
                position,
                yaw,
                pitch,
                travel_yaw,
                travel_pitch,
                avoided,
                bool(user_reposition),
            )
            self._pending_future = future
            future.add_done_callback(self._store_completed_plan)
            return True

    def take_latest_plan(self) -> AutoDivePlan | None:
        """Return and clear the newest completed plan."""
        with self._lock:
            plan = self._latest_plan
            self._latest_plan = None
            return plan

    def has_pending(self) -> bool:
        with self._lock:
            return bool(
                self._pending_future is not None
                and not self._pending_future.done()
            )

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def cancel_pending(self) -> None:
        """Discard queued/completed work without ending this replanner."""
        with self._lock:
            if self._shutdown:
                return
            self._generation += 1
            invalidation_generation = self._generation
            self._latest_generation = invalidation_generation
            future = self._pending_future
            self._pending_future = None
            self._latest_plan = None
        if future is not None:
            future.cancel()

    def _build_plan(
        self,
        generation: int,
        current_position: tuple[float, float, float],
        current_yaw: float | None = None,
        current_pitch: float | None = None,
        current_travel_yaw: float | None = None,
        current_travel_pitch: float | None = None,
        avoid_positions: tuple[tuple[float, float, float], ...] = (),
        user_reposition: bool = False,
    ) -> tuple[int, AutoDivePlan]:
        self._record_blackbox(
            "replan_build_started",
            generation=generation,
            position=current_position,
            yaw=current_yaw,
            pitch=current_pitch,
            travel_yaw=current_travel_yaw,
            travel_pitch=current_travel_pitch,
            avoid_positions=avoid_positions,
            user_reposition=bool(user_reposition),
        )
        try:
            kwargs: dict[str, Any] = {
                "current_position": current_position,
                "settings": self._settings,
            }
            if current_yaw is not None:
                kwargs["current_yaw"] = current_yaw
            if current_pitch is not None:
                kwargs["current_pitch"] = current_pitch
            if current_travel_yaw is not None:
                kwargs["current_travel_yaw"] = current_travel_yaw
            if current_travel_pitch is not None:
                kwargs["current_travel_pitch"] = current_travel_pitch
            if avoid_positions:
                kwargs["avoid_positions"] = avoid_positions
            if user_reposition:
                kwargs["user_reposition"] = True
            if self._cache_dir is not None:
                kwargs["cache_dir"] = self._cache_dir
            if self._blackbox is not None:
                kwargs["diagnostics"] = (
                    lambda event, payload: self._record_blackbox(
                        event,
                        generation=generation,
                        position=current_position,
                        **dict(payload),
                    )
                )
            plan = self._plan_builder(self._manifest, **kwargs)
        except Exception as exc:
            self._record_blackbox(
                "replan_failed",
                generation=generation,
                position=current_position,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        self._record_blackbox(
            "replan_completed",
            generation=generation,
            position=current_position,
            plan=_plan_summary(plan),
        )
        return generation, plan

    def _store_completed_plan(self, future: Future) -> None:
        try:
            generation, plan = future.result()
        except Exception:
            with self._lock:
                if self._pending_future is future:
                    self._pending_future = None
            return

        with self._lock:
            if self._pending_future is future:
                self._pending_future = None
            if self._shutdown or generation < self._latest_generation:
                return
            self._latest_generation = generation
            self._latest_plan = plan

    def _record_blackbox(self, event: str, **payload: Any) -> None:
        record = getattr(self._blackbox, "record", None)
        if not callable(record):
            return
        try:
            record(event, **payload)
        except Exception:
            return


class AutoDiveController:
    """Drive a finite centerline route while pausing for route lookahead loads."""

    def __init__(
        self,
        plan: AutoDivePlan,
        *,
        perf_counter: Callable[[], float],
        lookahead_seconds: float = DEFAULT_AUTO_DIVE_ROUTE_LOOKAHEAD_SECONDS,
        replanner: AutoDiveReplanner | None = None,
        replan_distance_m: float = 0.0,
        replan_min_interval_s: float = DEFAULT_AUTO_DIVE_REPLAN_MIN_INTERVAL_SECONDS,
        replan_only_during_survey: bool = True,
        route_prefetch_radius_cells: int | None = None,
        survey_interval_s: float = DEFAULT_AUTO_DIVE_SURVEY_INTERVAL_SECONDS,
        survey_duration_s: float = DEFAULT_AUTO_DIVE_SURVEY_DURATION_SECONDS,
        blackbox: Any | None = None,
    ) -> None:
        self.plan = plan
        self.perf_counter = perf_counter
        self.lookahead_seconds = max(1.0, float(lookahead_seconds))
        self.replanner = replanner
        self.replan_distance_m = max(0.0, float(replan_distance_m))
        self.replan_min_interval_s = max(0.0, float(replan_min_interval_s))
        self.replan_only_during_survey = bool(replan_only_during_survey)
        self.route_prefetch_radius_cells = max(
            1,
            int(
                route_prefetch_radius_cells
                if route_prefetch_radius_cells is not None
                else min(
                    int(plan.render_distance_cells),
                    DEFAULT_AUTO_DIVE_ROUTE_PREFETCH_RADIUS_CELLS,
                )
            ),
        )
        self.survey_interval_s = max(0.0, float(survey_interval_s))
        self.survey_duration_s = max(0.0, float(survey_duration_s))
        self.blackbox = blackbox
        self.state = AutoDiveState.IDLE
        self._started_at: float | None = None
        self._pause_started_at: float | None = None
        self._survey_pause_started_at: float | None = None
        self._survey_replan_requested = False
        self._next_survey_elapsed_s = (
            self.survey_interval_s
            if self.survey_interval_s > 0.0 and self.survey_duration_s > 0.0
            else math.inf
        )
        self._paused_seconds = 0.0
        self._elapsed_s = 0.0
        self._last_replan_request_position: np.ndarray | None = None
        self._last_replan_request_at: float | None = None
        self._last_rejected_replan_position: np.ndarray | None = None
        self._last_blackbox_frame_at: float | None = None
        self._mesh_recovery_started_at: float | None = None
        self._mesh_recovery_replan_pending = False
        self._mesh_recovery_attempts = 0
        self._mesh_recovery_boundary_positions: list[np.ndarray] = []
        self._stuck_reference_time: float | None = None
        self._stuck_reference_position: np.ndarray | None = None
        self._last_stuck_event_at: float | None = None
        self._prefetch_cells: frozenset[tuple[int, int, int]] = frozenset()
        self._readiness = AutoDiveReadiness(0, 0, 0, 0, 0, 1.0)
        self._user_assist_reason: str | None = None
        self._user_assist_anchor_position: np.ndarray | None = None
        self._user_assist_last_position: np.ndarray | None = None
        self._user_assist_travel_vector: np.ndarray | None = None
        self._user_resume_replan_pending = False

    @property
    def active(self) -> bool:
        return self.state in {AutoDiveState.LOADING, AutoDiveState.DIVING}

    @property
    def waiting_for_user_input(self) -> bool:
        return self.state is AutoDiveState.WAITING_FOR_USER

    @property
    def progress(self) -> float:
        route_progress = route_progress_fraction(self.plan.route, self._elapsed_s)
        if self.state == AutoDiveState.LOADING and not self._mesh_recovery_active():
            return self._readiness.progress
        return route_progress

    @property
    def show_loading_indicator(self) -> bool:
        return self.state is AutoDiveState.LOADING

    @property
    def loading_progress_fraction(self) -> float | None:
        if self.state is not AutoDiveState.LOADING:
            return None
        if self._mesh_recovery_active() or self._user_resume_replan_pending:
            return None
        return self.progress

    @property
    def status_note(self) -> str:
        if self.state is AutoDiveState.WAITING_FOR_USER:
            return "Guided Dive needs input"
        if self._user_resume_replan_pending:
            return "Finding a safe continuation"
        if self._mesh_recovery_active():
            return "Thinking"
        if self.state == AutoDiveState.LOADING:
            return (
                "Loading next passage "
                f"({self._readiness.loaded_cells}/"
                f"{self._readiness.expected_cells} cells)"
            )
        if self._survey_active():
            return "Surveying next passage"
        if self.state == AutoDiveState.DIVING:
            return "Diving centerline"
        if self.state == AutoDiveState.COMPLETE:
            return "Guided Dive complete"
        if self.state == AutoDiveState.CANCELLED:
            return "Guided Dive stopped"
        return "Guided Dive ready"

    @property
    def readiness(self) -> AutoDiveReadiness:
        return self._readiness

    @property
    def prefetch_cells(self) -> frozenset[tuple[int, int, int]]:
        return self._prefetch_cells

    def _record_blackbox(self, event: str, **payload: Any) -> None:
        record = getattr(self.blackbox, "record", None)
        if not callable(record):
            return
        try:
            record(event, **payload)
        except Exception:
            return

    def start(self, camera, world, now: float | None = None) -> None:
        """Place the camera on the route and begin loading the first lookahead."""
        now = self.perf_counter() if now is None else float(now)
        self._started_at = now
        self._pause_started_at = now
        self._paused_seconds = 0.0
        self._elapsed_s = 0.0
        self._survey_pause_started_at = None
        self._survey_replan_requested = False
        self._next_survey_elapsed_s = (
            self.survey_interval_s
            if self.survey_interval_s > 0.0 and self.survey_duration_s > 0.0
            else math.inf
        )
        self._last_replan_request_position = np.asarray(
            camera.position,
            dtype=np.float64,
        ).copy()
        self._last_replan_request_at = now
        self._mesh_recovery_started_at = None
        self._mesh_recovery_replan_pending = False
        self._mesh_recovery_attempts = 0
        self._mesh_recovery_boundary_positions = []
        self._user_assist_reason = None
        self._user_assist_anchor_position = None
        self._user_assist_last_position = None
        self._user_assist_travel_vector = None
        self._user_resume_replan_pending = False
        self.state = AutoDiveState.LOADING
        apply_pose_to_camera(camera, self.plan.route.pose_at(0.0))
        self.refresh_prefetch(world)
        self._readiness = self.readiness_for_world(world)
        self._record_blackbox(
            "auto_dive_started",
            plan=_plan_summary(self.plan),
            camera=_camera_payload(camera),
            readiness=_readiness_payload(self._readiness),
            replan_distance_m=float(self.replan_distance_m),
            route_prefetch_radius_cells=int(self.route_prefetch_radius_cells),
            survey_interval_s=float(self.survey_interval_s),
            survey_duration_s=float(self.survey_duration_s),
        )

    def observe_user_assist_position(self, position) -> None:
        """Remember manual movement while Guided Dive is waiting for input."""
        if self.state is not AutoDiveState.WAITING_FOR_USER:
            return
        point = np.asarray(position, dtype=np.float64).reshape(3).copy()
        if self._user_assist_anchor_position is None:
            self._user_assist_anchor_position = point.copy()
        self._user_assist_last_position = point.copy()
        displacement = point - self._user_assist_anchor_position
        if (
            self._user_assist_travel_vector is not None
            or float(np.linalg.norm(displacement))
            < DEFAULT_AUTO_DIVE_USER_ASSIST_MOVEMENT_THRESHOLD_M
        ):
            if self._user_assist_travel_vector is not None:
                self._user_assist_travel_vector = displacement.copy()
            return
        self._user_assist_travel_vector = displacement.copy()
        travel_yaw, travel_pitch = _vector_yaw_pitch(displacement)
        self._record_blackbox(
            "user_assist_motion_observed",
            position=_vector_payload(point),
            displacement=_vector_payload(displacement),
            travel_yaw=travel_yaw,
            travel_pitch=travel_pitch,
        )

    def resume_from_user_assist(self, camera, world, now: float | None = None) -> bool:
        """Resume this session from the user's corrected camera position."""
        if self.state is not AutoDiveState.WAITING_FOR_USER:
            return False
        replanner = self.replanner
        if replanner is None:
            self._record_blackbox(
                "user_assist_resume_failed",
                reason="replanner_unavailable",
            )
            return False
        take_latest_plan = getattr(replanner, "take_latest_plan", None)
        if callable(take_latest_plan):
            stale_plan = take_latest_plan()
            if stale_plan is not None:
                self._record_blackbox(
                    "user_assist_stale_replan_discarded",
                    plan=_plan_summary(stale_plan),
                )

        now = self.perf_counter() if now is None else float(now)
        current_position = np.asarray(camera.position, dtype=np.float64).reshape(3)
        self.observe_user_assist_position(current_position)
        travel_vector = self._user_assist_travel_vector
        travel_yaw, travel_pitch = (
            (None, None)
            if travel_vector is None
            else _vector_yaw_pitch(travel_vector)
        )
        avoid_positions = tuple(
            tuple(float(value) for value in position)
            for position in self._mesh_recovery_boundary_positions
        )
        current_yaw = float(getattr(camera, "yaw", 0.0))
        current_pitch = float(getattr(camera, "pitch", 0.0))

        set_prefetch = getattr(world, "set_prefetch_wanted_cells", None)
        if callable(set_prefetch):
            set_prefetch(())
        self._prefetch_cells = frozenset()
        self._readiness = AutoDiveReadiness(0, 0, 0, 0, 0, 1.0)
        self._started_at = now
        self._pause_started_at = now
        self._paused_seconds = 0.0
        self._elapsed_s = 0.0
        self._last_replan_request_position = current_position.copy()
        self._last_replan_request_at = now
        self._last_rejected_replan_position = None
        self._mesh_recovery_started_at = None
        self._mesh_recovery_replan_pending = False
        self._user_assist_reason = None
        self.state = AutoDiveState.LOADING

        requested = replanner.request(
            current_position,
            current_yaw=current_yaw,
            current_pitch=current_pitch,
            current_travel_yaw=travel_yaw,
            current_travel_pitch=travel_pitch,
            avoid_positions=avoid_positions,
            user_reposition=True,
        )
        has_pending = getattr(replanner, "has_pending", None)
        waiting_for_plan = bool(requested)
        if not waiting_for_plan and callable(has_pending):
            waiting_for_plan = bool(has_pending())
        if not waiting_for_plan:
            self.state = AutoDiveState.WAITING_FOR_USER
            self._pause_started_at = None
            self._user_assist_reason = "user_resume_replan_unavailable"
            self._record_blackbox(
                "user_assist_resume_failed",
                reason="replan_unavailable",
                position=_vector_payload(current_position),
            )
            return False

        self._user_resume_replan_pending = True
        self._record_blackbox(
            "user_assist_resumed",
            requested=bool(requested),
            position=_vector_payload(current_position),
            yaw=current_yaw,
            pitch=current_pitch,
            travel_yaw=travel_yaw,
            travel_pitch=travel_pitch,
            avoid_positions=[list(position) for position in avoid_positions],
        )
        return True

    def stop(self, world=None, *, completed: bool = False) -> None:
        """Stop Guided Dive and clear owned streaming prefetch cells."""
        self._record_blackbox(
            "auto_dive_stopped",
            completed=bool(completed),
            state=str(self.state.value),
            elapsed_s=float(self._elapsed_s),
            progress=float(self.progress),
            readiness=_readiness_payload(self._readiness),
        )
        if world is not None:
            set_prefetch = getattr(world, "set_prefetch_wanted_cells", None)
            if callable(set_prefetch):
                set_prefetch(())
        if self.replanner is not None:
            self.replanner.shutdown()
        self._prefetch_cells = frozenset()
        self.state = AutoDiveState.COMPLETE if completed else AutoDiveState.CANCELLED
        close = getattr(self.blackbox, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def update(self, camera, world, now: float | None = None) -> AutoDiveState:
        """Advance the route when lookahead is ready; otherwise hold position."""
        now = self.perf_counter() if now is None else float(now)
        if self.state in {
            AutoDiveState.IDLE,
            AutoDiveState.WAITING_FOR_USER,
            AutoDiveState.COMPLETE,
            AutoDiveState.CANCELLED,
        }:
            return self.state
        # A user resume is a handoff to the background planner. Keep the
        # camera at the user's position until that plan is accepted; otherwise
        # the old, failed route could immediately take control again.
        if self._user_resume_replan_pending:
            self.state = AutoDiveState.LOADING
            return self.state

        self.refresh_prefetch(world)
        self._readiness = self.readiness_for_world(world)
        if not self._readiness.ready:
            if self._mesh_recovery_active():
                self._apply_mesh_recovery_pose_to_camera(camera, now=now)
                self._request_mesh_recovery_replan_if_ready(camera, now=now)
                self.state = AutoDiveState.LOADING
                return self.state
            if self._survey_active(now=now):
                self._apply_survey_pose_to_camera(camera, now=now)
                self.state = AutoDiveState.LOADING
                return self.state
            if self._pause_started_at is None:
                self._pause_started_at = now
            self.state = AutoDiveState.LOADING
            return self.state

        if self._pause_started_at is not None:
            self._paused_seconds += max(0.0, now - self._pause_started_at)
            self._pause_started_at = None

        if self._started_at is None:
            self._started_at = now

        if self._survey_active(now=now):
            self._apply_survey_pose_to_camera(camera, now=now)
            self.state = AutoDiveState.DIVING
            return self.state

        if self._mesh_recovery_active():
            self._apply_mesh_recovery_pose_to_camera(camera, now=now)
            self._request_mesh_recovery_replan_if_ready(camera, now=now)
            self.state = AutoDiveState.LOADING
            return self.state

        if self._survey_pause_started_at is not None:
            survey_duration_s = max(0.0, now - self._survey_pause_started_at)
            self._paused_seconds += survey_duration_s
            self._record_blackbox(
                "survey_completed",
                duration_s=float(survey_duration_s),
                elapsed_s=float(self._elapsed_s),
                progress=float(self.progress),
                readiness=_readiness_payload(self._readiness),
            )
            self._survey_pause_started_at = None
            self._survey_replan_requested = False
            self._advance_next_survey_elapsed()

        self._elapsed_s = min(
            self.plan.route.duration_s,
            max(0.0, now - self._started_at - self._paused_seconds),
        )
        if self._should_start_mesh_recovery_standoff():
            apply_pose_to_camera(camera, self.plan.route.pose_at(self._elapsed_s))
            if self._maybe_start_mesh_recovery_scan(
                now=now,
                world=world,
                reason="mesh_truncated_standoff",
            ):
                if self.state is not AutoDiveState.WAITING_FOR_USER:
                    self.state = AutoDiveState.LOADING
                return self.state
        if self._should_start_survey():
            self._survey_pause_started_at = now
            self._survey_replan_requested = False
            self._record_blackbox(
                "survey_started",
                elapsed_s=float(self._elapsed_s),
                progress=float(self.progress),
                readiness=_readiness_payload(self._readiness),
                route_prefetch_radius_cells=int(
                    self._effective_route_prefetch_radius_cells()
                ),
                lookahead_seconds=float(self._effective_lookahead_seconds()),
            )
            self.refresh_prefetch(world)
            self._readiness = self.readiness_for_world(world)
            self._apply_survey_pose_to_camera(camera, now=now)
            self.state = AutoDiveState.DIVING
            return self.state

        apply_pose_to_camera(camera, self.plan.route.pose_at(self._elapsed_s))
        if self._elapsed_s >= self.plan.route.duration_s:
            if _plan_requires_user_assist_at_boundary(self.plan):
                self._enter_user_assist(
                    world,
                    reason="mesh_truncated_boundary_reached",
                    position=np.asarray(camera.position, dtype=np.float64),
                    details={
                        "route_truncated_by_mesh": True,
                        "safe_prefix_length_m": getattr(
                            self.plan,
                            "mesh_safe_prefix_length_m",
                            None,
                        ),
                    },
                )
                return self.state
            if self._maybe_start_mesh_recovery_scan(
                now=now,
                world=world,
                reason="route_boundary",
            ):
                if self.state is not AutoDiveState.WAITING_FOR_USER:
                    self.state = AutoDiveState.LOADING
                return self.state
            self.stop(world, completed=True)
            return self.state
        self.state = AutoDiveState.DIVING
        return self.state

    def update_replan(self, camera, world, now: float | None = None) -> bool:
        """Consume/request background replans from the owner thread.

        Returns True when a new plan was swapped in.
        """
        replanner = self.replanner
        if replanner is None or not self.active:
            return False
        now = self.perf_counter() if now is None else float(now)
        current_position = np.asarray(camera.position, dtype=np.float64)
        user_resume = self._user_resume_replan_pending

        swapped = False
        latest_plan = replanner.take_latest_plan()
        rejected_replan = False
        if latest_plan is not None:
            mesh_recovery = self._mesh_recovery_active()
            if (
                self.replan_only_during_survey
                and not self._survey_active(now=now)
                and not mesh_recovery
                and not user_resume
            ):
                rejection = {"reason": "outside_survey_pause"}
            else:
                forward_vector = (
                    self._user_resume_forward_vector(camera)
                    if user_resume
                    else None
                )
                rejection = self._replan_rejection_payload(
                    latest_plan,
                    current_position,
                    forward_vector=forward_vector,
                    allow_direction_change=user_resume,
                )
            if rejection is None:
                resume_elapsed_s = _route_elapsed_nearest_position(
                    latest_plan,
                    current_position,
                )
                self.plan = latest_plan
                self._started_at = now - resume_elapsed_s
                self._pause_started_at = None
                self._paused_seconds = 0.0
                self._elapsed_s = resume_elapsed_s
                self._last_rejected_replan_position = None
                self._mesh_recovery_started_at = None
                self._mesh_recovery_replan_pending = False
                self._user_resume_replan_pending = False
                self._user_assist_reason = None
                if not _plan_needs_boundary_replan(latest_plan) and not user_resume:
                    self._mesh_recovery_attempts = 0
                    self._mesh_recovery_boundary_positions = []
                self.refresh_prefetch(world)
                self._readiness = self.readiness_for_world(world)
                self._record_blackbox(
                    "replan_accepted",
                    camera_position=_vector_payload(current_position),
                    user_reposition=bool(user_resume),
                    resume_elapsed_s=float(resume_elapsed_s),
                    resume_progress=float(self.progress),
                    plan=_plan_summary(latest_plan),
                    readiness=_readiness_payload(self._readiness),
                )
                swapped = True
            else:
                self._mesh_recovery_replan_pending = False
                self._user_resume_replan_pending = False
                self._last_rejected_replan_position = current_position.copy()
                rejected_replan = True
                self._record_blackbox(
                    "replan_rejected",
                    camera_position=_vector_payload(current_position),
                    plan=_plan_summary(latest_plan),
                    **rejection,
                )
                if mesh_recovery or user_resume:
                    self._enter_user_assist(
                        world,
                        reason=str(rejection.get("reason", "replan_rejected")),
                        position=current_position,
                        details=rejection,
                    )
        elif self._mesh_recovery_replan_pending or self._user_resume_replan_pending:
            has_pending = getattr(replanner, "has_pending", None)
            if callable(has_pending) and not has_pending():
                user_resume_without_plan = self._user_resume_replan_pending
                self._mesh_recovery_replan_pending = False
                self._user_resume_replan_pending = False
                event = (
                    "user_resume_replan_finished_without_plan"
                    if user_resume_without_plan
                    else "mesh_recovery_replan_finished_without_plan"
                )
                self._record_blackbox(
                    event,
                    attempts=int(self._mesh_recovery_attempts),
                    position=_vector_payload(current_position),
                    elapsed_s=float(self._elapsed_s),
                    progress=float(self.progress),
                )
                self._enter_user_assist(
                    world,
                    reason=(
                        "user_resume_replan_finished_without_plan"
                        if user_resume_without_plan
                        else "mesh_recovery_replan_finished_without_plan"
                    ),
                    position=current_position,
                )

        if not rejected_replan and self._should_request_replan(
            current_position,
            now=now,
        ):
            if replanner.request(current_position):
                self._last_replan_request_position = current_position.copy()
                self._last_replan_request_at = now
                if self._survey_active(now=now):
                    self._survey_replan_requested = True

        return swapped

    def _plan_is_useful_replan(
        self,
        plan: AutoDivePlan,
        current_position: np.ndarray,
    ) -> bool:
        return self._replan_rejection_payload(plan, current_position) is None

    def _replan_rejection_payload(
        self,
        plan: AutoDivePlan,
        current_position: np.ndarray,
        *,
        forward_vector: np.ndarray | None = None,
        allow_direction_change: bool = False,
    ) -> dict[str, Any] | None:
        if not self._plan_starts_near_current_camera(plan, current_position):
            start = (
                np.asarray(plan.route_points[0], dtype=np.float64)
                if plan.route_points
                else None
            )
            threshold = max(0.5, self.replan_distance_m * 2.0)
            return {
                "reason": "start_too_far_from_camera",
                "start_distance_m": (
                    None
                    if start is None
                    else float(np.linalg.norm(start - current_position))
                ),
                "threshold_m": float(threshold),
            }
        next_point = self._first_plan_point_after_start(plan)
        if next_point is None:
            return {"reason": "no_next_point_after_start"}
        min_step_m = max(0.05, self.replan_distance_m * 0.25)
        next_step_m = float(np.linalg.norm(next_point - current_position))
        if next_step_m < min_step_m:
            return {
                "reason": "next_point_too_close",
                "next_step_m": next_step_m,
                "min_step_m": float(min_step_m),
                "next_point": _vector_payload(next_point),
            }
        if not allow_direction_change:
            alignment = self._plan_forward_alignment(
                next_point,
                current_position,
                forward_vector=forward_vector,
            )
            if alignment is not None and alignment < -0.05:
                return {
                    "reason": "moves_backward_from_current_route",
                    "forward_alignment": float(alignment),
                    "next_step_m": next_step_m,
                    "next_point": _vector_payload(next_point),
                }
        return None

    def _plan_moves_forward_from_current_route(
        self,
        next_point: np.ndarray,
        current_position: np.ndarray,
    ) -> bool:
        forward = self._current_route_forward_vector(current_position)
        if forward is None:
            return True
        alignment = self._plan_forward_alignment(next_point, current_position)
        return alignment is not None and alignment >= -0.05

    def _plan_forward_alignment(
        self,
        next_point: np.ndarray,
        current_position: np.ndarray,
        *,
        forward_vector: np.ndarray | None = None,
    ) -> float | None:
        forward = (
            self._normalized_direction(forward_vector)
            if forward_vector is not None
            else self._current_route_forward_vector(current_position)
        )
        if forward is None:
            return None
        candidate = np.asarray(next_point, dtype=np.float64) - np.asarray(
            current_position,
            dtype=np.float64,
        )
        candidate_norm = float(np.linalg.norm(candidate))
        if candidate_norm <= 1e-9:
            return -1.0
        return float(np.dot(candidate / candidate_norm, forward))

    def _user_resume_forward_vector(self, camera) -> np.ndarray | None:
        if self._user_assist_travel_vector is not None:
            return self._normalized_direction(self._user_assist_travel_vector)
        return _direction_from_yaw_pitch_radians(
            float(getattr(camera, "yaw", 0.0)),
            float(getattr(camera, "pitch", 0.0)),
        )

    @staticmethod
    def _normalized_direction(vector: np.ndarray | None) -> np.ndarray | None:
        if vector is None:
            return None
        values = np.asarray(vector, dtype=np.float64).reshape(3)
        norm = float(np.linalg.norm(values))
        if norm <= 1e-9:
            return None
        return values / norm

    def record_navigation_guard_clamp(
        self,
        *,
        before: np.ndarray,
        after: np.ndarray,
        vertical_band: tuple[float, float] | None = None,
    ) -> None:
        self._record_blackbox(
            "navigation_guard_clamped",
            before=_vector_payload(before),
            after=_vector_payload(after),
            vertical_band=(
                None
                if vertical_band is None
                else [float(vertical_band[0]), float(vertical_band[1])]
            ),
            elapsed_s=float(self._elapsed_s),
            progress=float(self.progress),
        )

    def record_frame(
        self,
        camera,
        world,
        *,
        now: float,
        navigation_clamped: bool = False,
    ) -> None:
        if self.blackbox is None or not self.active:
            return
        now = float(now)
        position = np.asarray(camera.position, dtype=np.float64)
        self._update_stuck_detector(
            position,
            now=now,
            navigation_clamped=navigation_clamped,
        )
        if (
            not navigation_clamped
            and self._last_blackbox_frame_at is not None
            and now - self._last_blackbox_frame_at < 0.5
        ):
            return
        self._last_blackbox_frame_at = now
        self._record_blackbox(
            "frame",
            state=str(self.state.value),
            elapsed_s=float(self._elapsed_s),
            progress=float(self.progress),
            camera=_camera_payload(camera),
            current_cell=_world_cell_payload(world, position),
            nearest_route_distance_m=self._nearest_route_distance_m(position),
            next_pose=_next_pose_payload(self.plan, self._elapsed_s),
            readiness=_readiness_payload(self._readiness),
            surveying=bool(self._survey_active(now=now)),
            navigation_clamped=bool(navigation_clamped),
        )

    def _update_stuck_detector(
        self,
        position: np.ndarray,
        *,
        now: float,
        navigation_clamped: bool,
    ) -> None:
        if (
            self.state is not AutoDiveState.DIVING
            or not self._readiness.ready
            or self._survey_active(now=now)
            or self._mesh_recovery_active()
        ):
            self._stuck_reference_time = None
            self._stuck_reference_position = None
            return
        movement_threshold_m = max(
            0.05,
            min(
                self.replan_distance_m * 0.5,
                self._route_speed_m_per_second() * 0.75,
            ),
        )
        if self._stuck_reference_position is None:
            self._stuck_reference_position = position.copy()
            self._stuck_reference_time = now
            return
        moved_m = float(np.linalg.norm(position - self._stuck_reference_position))
        if moved_m >= movement_threshold_m:
            self._stuck_reference_position = position.copy()
            self._stuck_reference_time = now
            return
        if self._stuck_reference_time is None:
            self._stuck_reference_time = now
            return
        stuck_duration_s = max(0.0, now - self._stuck_reference_time)
        if stuck_duration_s < 2.0:
            return
        if (
            self._last_stuck_event_at is not None
            and now - self._last_stuck_event_at < 2.0
        ):
            return
        self._last_stuck_event_at = now
        self._record_blackbox(
            "stuck_detected",
            stuck_duration_s=stuck_duration_s,
            moved_m=moved_m,
            movement_threshold_m=float(movement_threshold_m),
            navigation_clamped=bool(navigation_clamped),
            elapsed_s=float(self._elapsed_s),
            progress=float(self.progress),
            position=_vector_payload(position),
            nearest_route_distance_m=self._nearest_route_distance_m(position),
            next_pose=_next_pose_payload(self.plan, self._elapsed_s),
            readiness=_readiness_payload(self._readiness),
        )

    def _nearest_route_distance_m(self, position: np.ndarray) -> float | None:
        if not self.plan.route_points:
            return None
        return float(
            min(
                np.linalg.norm(np.asarray(point, dtype=np.float64) - position)
                for point in self.plan.route_points
            )
        )

    def _current_route_forward_vector(
        self,
        current_position: np.ndarray,
    ) -> np.ndarray | None:
        duration = float(getattr(self.plan.route, "duration_s", 0.0))
        if duration <= 1e-9:
            return None
        speed = float(self.plan.route_length_m) / max(1e-9, duration)
        lookahead_s = max(
            0.05,
            min(
                1.0,
                max(0.05, self.replan_distance_m) / max(1e-9, speed),
            ),
        )
        future_time = min(duration, self._elapsed_s + lookahead_s)
        if future_time <= self._elapsed_s + 1e-9 and self._elapsed_s > 0.0:
            future_time = self._elapsed_s
            previous_time = max(0.0, self._elapsed_s - lookahead_s)
            future = np.asarray(
                self.plan.route.pose_at(future_time).position,
                dtype=np.float64,
            )
            previous = np.asarray(
                self.plan.route.pose_at(previous_time).position,
                dtype=np.float64,
            )
            vector = future - previous
        else:
            current_route_position = np.asarray(
                self.plan.route.pose_at(self._elapsed_s).position,
                dtype=np.float64,
            )
            future = np.asarray(
                self.plan.route.pose_at(future_time).position,
                dtype=np.float64,
            )
            vector = future - current_route_position
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-9:
            return None
        return vector / norm

    def _plan_starts_near_current_camera(
        self,
        plan: AutoDivePlan,
        current_position: np.ndarray,
    ) -> bool:
        if not plan.route_points:
            return False
        threshold = max(0.5, self.replan_distance_m * 2.0)
        start = np.asarray(plan.route_points[0], dtype=np.float64)
        return bool(np.linalg.norm(start - current_position) <= threshold)

    def _first_plan_point_after_start(self, plan: AutoDivePlan) -> np.ndarray | None:
        if len(plan.route_points) < 2:
            return None
        start = np.asarray(plan.route_points[0], dtype=np.float64)
        min_distance_sq = max(1e-9, self.replan_distance_m * 0.05) ** 2
        for point in plan.route_points[1:]:
            candidate = np.asarray(point, dtype=np.float64)
            if float(np.sum((candidate - start) ** 2)) > min_distance_sq:
                return candidate
        return None

    def _should_request_replan(
        self,
        current_position: np.ndarray,
        *,
        now: float | None = None,
    ) -> bool:
        if self.replan_distance_m <= 0.0:
            return False
        now = self.perf_counter() if now is None else float(now)
        if (
            self._last_replan_request_at is not None
            and now - self._last_replan_request_at < self.replan_min_interval_s
        ):
            return False
        if self.replan_only_during_survey:
            if not self._survey_active(now=now):
                return False
            return not self._survey_replan_requested
        rejected_position = self._last_rejected_replan_position
        if rejected_position is not None:
            reject_radius = max(0.05, self.replan_distance_m * 0.5)
            if np.linalg.norm(current_position - rejected_position) < reject_radius:
                return False
        previous = self._last_replan_request_position
        if previous is None:
            return True
        return bool(np.linalg.norm(current_position - previous) >= self.replan_distance_m)

    def refresh_prefetch(self, world) -> frozenset[tuple[int, int, int]]:
        """Keep cells around the upcoming route segment wanted by streaming."""
        cells = set()
        radius = self._effective_route_prefetch_radius_cells()
        for position in self._lookahead_positions(world):
            route_cell = world.cell_for_position(np.asarray(position, dtype=np.float32))
            cells.update(world.available_cells_in_radius(route_cell, radius))
        self._prefetch_cells = frozenset(cells)
        set_prefetch = getattr(world, "set_prefetch_wanted_cells", None)
        if callable(set_prefetch):
            set_prefetch(cells)
        return self._prefetch_cells

    def readiness_for_world(self, world) -> AutoDiveReadiness:
        """Return readiness for the current prefetch set."""
        cells = set(self._prefetch_cells)
        if not cells:
            return AutoDiveReadiness(0, 0, 0, 0, 0, 1.0)

        lock = getattr(world, "_lock", None)
        if lock is None:
            loaded_cells = set(getattr(world, "loaded_cells", set()))
            pending_cells = set(getattr(world, "_pending", set()))
            failed_cells = _failed_cells_snapshot(world)
        else:
            with lock:
                loaded_cells = set(getattr(world, "loaded_cells", set()))
                pending_cells = set(getattr(world, "_pending", set()))
                failed_cells = _failed_cells_snapshot(world)

        loaded = cells & loaded_cells
        pending = cells & pending_cells
        failed = cells & failed_cells
        settled = loaded | failed
        missing_count = max(0, len(cells) - len(settled))
        progress = (
            len(settled) + 0.25 * len(pending)
        ) / max(1, len(cells))
        return AutoDiveReadiness(
            expected_cells=len(cells),
            loaded_cells=len(loaded),
            pending_cells=len(pending),
            failed_cells=len(failed),
            missing_cells=missing_count,
            progress=max(0.0, min(1.0, progress)),
        )

    def _lookahead_positions(self, world) -> Iterable[tuple[float, float, float]]:
        chunk_size = max(
            1e-6,
            float(getattr(getattr(world, "config", None), "chunk_size", 1.0)),
        )
        speed = self.plan.route_length_m / max(1e-6, self.plan.duration_s)
        step_s = max(0.25, min(2.0, chunk_size / max(1e-6, speed)))
        start_s = self._elapsed_s
        end_s = min(
            self.plan.route.duration_s,
            self._elapsed_s + self._effective_lookahead_seconds(),
        )
        sample_count = max(1, int(math.ceil((end_s - start_s) / step_s)))
        for index in range(sample_count + 1):
            t = start_s + (end_s - start_s) * index / max(1, sample_count)
            yield self.plan.route.pose_at(t).position

    def _survey_active(self, *, now: float | None = None) -> bool:
        if self._survey_pause_started_at is None:
            return False
        if self.survey_duration_s <= 0.0:
            return False
        now = self.perf_counter() if now is None else float(now)
        return now - self._survey_pause_started_at < self.survey_duration_s

    def _mesh_recovery_active(self, *, now: float | None = None) -> bool:
        if self._mesh_recovery_started_at is None:
            return False
        return True

    def _should_start_mesh_recovery_standoff(self) -> bool:
        if self._mesh_recovery_active():
            return False
        if _plan_requires_user_assist_at_boundary(self.plan):
            return False
        if not getattr(self.plan, "route_truncated_by_mesh", False):
            return False
        if self._elapsed_s >= self.plan.route.duration_s:
            return False
        remaining_distance_m = self._route_remaining_distance_m()
        if remaining_distance_m is None:
            return False
        return remaining_distance_m <= self._mesh_recovery_standoff_distance_m()

    def _route_remaining_distance_m(self) -> float | None:
        duration_s = float(getattr(self.plan.route, "duration_s", 0.0))
        if duration_s <= 1e-9:
            return None
        remaining_s = max(0.0, duration_s - float(self._elapsed_s))
        return float(remaining_s * self._route_speed_m_per_second())

    def _mesh_recovery_standoff_distance_m(self) -> float:
        route_length_m = max(0.0, float(getattr(self.plan, "route_length_m", 0.0)))
        if route_length_m <= 1e-9:
            return 0.0
        distance_m = max(
            float(DEFAULT_AUTO_DIVE_MESH_RECOVERY_MIN_STANDOFF_M),
            float(self.replan_distance_m)
            * float(DEFAULT_AUTO_DIVE_MESH_RECOVERY_STANDOFF_CELLS),
        )
        return min(
            distance_m,
            route_length_m
            * float(DEFAULT_AUTO_DIVE_MESH_RECOVERY_MAX_STANDOFF_FRACTION),
        )

    def _maybe_start_mesh_recovery_scan(
        self,
        *,
        now: float,
        world=None,
        reason: str = "route_boundary",
    ) -> bool:
        if not _plan_needs_boundary_replan(self.plan):
            return False
        route_position = np.asarray(
            self.plan.route.pose_at(self._elapsed_s).position,
            dtype=np.float64,
        )
        if self.replanner is None:
            self._enter_user_assist(
                world,
                reason="mesh_recovery_unavailable",
                position=route_position,
            )
            return True
        if self._mesh_recovery_replan_pending:
            return True
        if self._mesh_recovery_attempts >= DEFAULT_AUTO_DIVE_MESH_RECOVERY_MAX_ATTEMPTS:
            self._enter_user_assist(
                world,
                reason="mesh_recovery_exhausted",
                position=route_position,
                details={"attempts": int(self._mesh_recovery_attempts)},
            )
            return True

        if self._mesh_recovery_started_at is None:
            self._mesh_recovery_started_at = now
            self._remember_mesh_recovery_boundary(route_position)
            self._record_blackbox(
                "mesh_recovery_scan_started",
                reason=str(reason),
                attempts=int(self._mesh_recovery_attempts),
                boundary_positions=[
                    _vector_payload(position)
                    for position in self._mesh_recovery_boundary_positions
                ],
                elapsed_s=float(self._elapsed_s),
                progress=float(self.progress),
                remaining_distance_m=self._route_remaining_distance_m(),
                standoff_distance_m=float(self._mesh_recovery_standoff_distance_m()),
                plan=_plan_summary(self.plan),
            )
        return True

    def _request_mesh_recovery_replan_if_ready(self, camera, *, now: float) -> bool:
        if self.replanner is None:
            return False
        if self._mesh_recovery_replan_pending:
            return True
        if self._mesh_recovery_started_at is None:
            return False
        scan_elapsed_s = max(0.0, float(now) - self._mesh_recovery_started_at)
        if scan_elapsed_s < DEFAULT_AUTO_DIVE_MESH_RECOVERY_TURN_SECONDS:
            return False

        current_position = np.asarray(camera.position, dtype=np.float64)
        travel_yaw, travel_pitch = self._current_route_travel_angles(current_position)
        requested = self.replanner.request(
            current_position,
            current_yaw=float(getattr(camera, "yaw", 0.0)),
            current_pitch=float(getattr(camera, "pitch", 0.0)),
            current_travel_yaw=travel_yaw,
            current_travel_pitch=travel_pitch,
            avoid_positions=tuple(
                tuple(float(value) for value in position)
                for position in self._mesh_recovery_prior_boundary_positions()
            ),
        )
        self._mesh_recovery_replan_pending = True
        if requested:
            self._mesh_recovery_attempts += 1
            self._last_replan_request_position = current_position.copy()
            self._last_replan_request_at = now
        self._record_blackbox(
            "mesh_recovery_replan_requested",
            requested=bool(requested),
            attempts=int(self._mesh_recovery_attempts),
            scan_elapsed_s=float(scan_elapsed_s),
            position=_vector_payload(current_position),
            yaw=float(getattr(camera, "yaw", 0.0)),
            pitch=float(getattr(camera, "pitch", 0.0)),
            travel_yaw=travel_yaw,
            travel_pitch=travel_pitch,
            avoid_positions=[
                _vector_payload(position)
                for position in self._mesh_recovery_prior_boundary_positions()
            ],
            elapsed_s=float(self._elapsed_s),
            progress=float(self.progress),
            plan=_plan_summary(self.plan),
        )
        return True

    def _current_route_travel_angles(
        self,
        current_position: np.ndarray,
    ) -> tuple[float | None, float | None]:
        forward = self._current_route_forward_vector(current_position)
        if forward is None:
            return None, None
        return _vector_yaw_pitch(forward)

    def _remember_mesh_recovery_boundary(self, position: np.ndarray) -> None:
        point = np.asarray(position, dtype=np.float64).reshape(3).copy()
        threshold_m = max(0.5, float(self.replan_distance_m) * 0.5)
        for existing in self._mesh_recovery_boundary_positions:
            if float(np.linalg.norm(existing - point)) <= threshold_m:
                return
        self._mesh_recovery_boundary_positions.append(point)
        if len(self._mesh_recovery_boundary_positions) > 8:
            del self._mesh_recovery_boundary_positions[:-8]

    def _mesh_recovery_prior_boundary_positions(self) -> tuple[np.ndarray, ...]:
        if len(self._mesh_recovery_boundary_positions) <= 1:
            return ()
        return tuple(self._mesh_recovery_boundary_positions[:-1])

    def _should_start_survey(self) -> bool:
        if self.survey_interval_s <= 0.0 or self.survey_duration_s <= 0.0:
            return False
        if self._survey_pause_started_at is not None:
            return False
        if self._elapsed_s + 1e-6 < self._next_survey_elapsed_s:
            return False
        return self._elapsed_s < self.plan.route.duration_s - 1e-6

    def _advance_next_survey_elapsed(self) -> None:
        if self.survey_interval_s <= 0.0:
            self._next_survey_elapsed_s = math.inf
            return
        while self._next_survey_elapsed_s <= self._elapsed_s + 1e-6:
            self._next_survey_elapsed_s += self.survey_interval_s

    def _effective_lookahead_seconds(self) -> float:
        lookahead_seconds = float(self.lookahead_seconds)
        if self._survey_active():
            lookahead_seconds *= max(
                1.0,
                DEFAULT_AUTO_DIVE_SURVEY_PREFETCH_LOOKAHEAD_MULTIPLIER,
            )
        return lookahead_seconds

    def _effective_route_prefetch_radius_cells(self) -> int:
        radius = max(1, int(self.route_prefetch_radius_cells))
        if self._survey_active():
            radius += max(0, DEFAULT_AUTO_DIVE_SURVEY_PREFETCH_RADIUS_BONUS_CELLS)
        return max(1, int(radius))

    def _apply_survey_pose_to_camera(self, camera, *, now: float) -> None:
        pose = self.plan.route.pose_at(self._elapsed_s)
        apply_pose_to_camera(camera, pose)
        if self._survey_pause_started_at is None or self.survey_duration_s <= 1e-9:
            return
        phase = max(
            0.0,
            min(
                1.0,
                (float(now) - self._survey_pause_started_at)
                / self.survey_duration_s,
            ),
        )
        camera.yaw += math.radians(
            DEFAULT_AUTO_DIVE_SURVEY_YAW_SWEEP_DEGREES
        ) * math.sin(math.tau * phase)
        camera.pitch += math.radians(
            DEFAULT_AUTO_DIVE_SURVEY_PITCH_SWEEP_DEGREES
        ) * math.sin(math.tau * phase * 2.0)
        camera.pitch = max(math.radians(-55.0), min(math.radians(55.0), camera.pitch))

    def _apply_mesh_recovery_pose_to_camera(self, camera, *, now: float) -> None:
        pose = self.plan.route.pose_at(self._elapsed_s)
        apply_pose_to_camera(camera, pose)

    def _route_speed_m_per_second(self) -> float:
        return float(self.plan.route_length_m) / max(1e-9, float(self.plan.duration_s))

    def _enter_user_assist(
        self,
        world,
        *,
        reason: str,
        position: np.ndarray,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self.state is AutoDiveState.WAITING_FOR_USER:
            return
        position = np.asarray(position, dtype=np.float64).reshape(3)
        set_prefetch = getattr(world, "set_prefetch_wanted_cells", None)
        if callable(set_prefetch):
            set_prefetch(())
        # Keep the worker alive but idle. A resume must use this controller's
        # route history and avoidance context instead of starting a new
        # session that forgets why the previous route stopped.
        cancel_pending = getattr(self.replanner, "cancel_pending", None)
        if callable(cancel_pending):
            cancel_pending()
        self._remember_mesh_recovery_boundary(position)
        self._prefetch_cells = frozenset()
        self._readiness = AutoDiveReadiness(0, 0, 0, 0, 0, 1.0)
        self._pause_started_at = None
        self._survey_pause_started_at = None
        self._survey_replan_requested = False
        self._mesh_recovery_started_at = None
        self._mesh_recovery_replan_pending = False
        self._user_resume_replan_pending = False
        self._user_assist_anchor_position = position.copy()
        self._user_assist_last_position = position.copy()
        self._user_assist_travel_vector = None
        self._last_rejected_replan_position = position.copy()
        self._user_assist_reason = str(reason)
        self.state = AutoDiveState.WAITING_FOR_USER
        self._record_blackbox(
            "user_assist_requested",
            reason=str(reason),
            position=_vector_payload(position),
            elapsed_s=float(self._elapsed_s),
            progress=float(self.progress),
            readiness=_readiness_payload(self._readiness),
            details=dict(details or {}),
        )


def _failed_cells_snapshot(world) -> set[tuple[int, int, int]]:
    failed_cells = getattr(world, "_failed_cells", {})
    if isinstance(failed_cells, dict):
        return set(failed_cells)
    return set(failed_cells or ())


def _route_elapsed_nearest_position(
    plan: AutoDivePlan,
    position: np.ndarray,
) -> float:
    """Return route elapsed time closest to the current camera position."""
    route_points = tuple(getattr(plan, "route_points", ()))
    if len(route_points) < 2:
        return 0.0
    route_length = max(1e-9, float(getattr(plan, "route_length_m", 0.0)))
    duration = max(0.0, float(getattr(plan, "duration_s", 0.0)))
    if duration <= 1e-9:
        return 0.0

    current = np.asarray(position, dtype=np.float64).reshape(3)
    cumulative_m = 0.0
    best_distance_sq = math.inf
    best_distance_m = 0.0
    for first, second in zip(route_points, route_points[1:], strict=False):
        start = np.asarray(first, dtype=np.float64)
        end = np.asarray(second, dtype=np.float64)
        segment = end - start
        segment_len_sq = float(np.dot(segment, segment))
        if segment_len_sq <= 1e-18:
            continue
        t = float(np.dot(current - start, segment) / segment_len_sq)
        t = max(0.0, min(1.0, t))
        projected = start + segment * t
        distance_sq = float(np.sum((current - projected) ** 2))
        segment_len = math.sqrt(segment_len_sq)
        distance_m = cumulative_m + segment_len * t
        if distance_sq < best_distance_sq:
            best_distance_sq = distance_sq
            best_distance_m = distance_m
        cumulative_m += segment_len

    progress = max(0.0, min(1.0, best_distance_m / route_length))
    return duration * progress


def _vector_yaw_pitch(vector: np.ndarray) -> tuple[float | None, float | None]:
    values = np.asarray(vector, dtype=np.float64).reshape(3)
    norm = float(np.linalg.norm(values))
    if norm <= 1e-9:
        return None, None
    unit = values / norm
    yaw = math.atan2(float(unit[2]), float(unit[0]))
    pitch = math.asin(max(-1.0, min(1.0, float(unit[1]))))
    return float(yaw), float(pitch)


def _direction_from_yaw_pitch_radians(yaw: float, pitch: float) -> np.ndarray:
    horizontal = math.cos(float(pitch))
    return np.asarray(
        (
            math.cos(float(yaw)) * horizontal,
            math.sin(float(pitch)),
            math.sin(float(yaw)) * horizontal,
        ),
        dtype=np.float64,
    )


def _normalized_avoid_positions(
    avoid_positions: Sequence[Sequence[float]] | None,
) -> tuple[tuple[float, float, float], ...]:
    if not avoid_positions:
        return ()
    normalized: list[tuple[float, float, float]] = []
    for position in avoid_positions:
        try:
            values = np.asarray(position, dtype=np.float64).reshape(3)
        except Exception:
            continue
        normalized.append(
            (
                float(values[0]),
                float(values[1]),
                float(values[2]),
            )
        )
    return tuple(normalized)


def _plan_summary(plan: AutoDivePlan) -> dict[str, Any]:
    return {
        "route_length_m": float(plan.route_length_m),
        "duration_s": float(plan.duration_s),
        "route_point_count": len(plan.route_points),
        "route_cell_count": len(plan.route_cells),
        "render_distance_cells": int(plan.render_distance_cells),
        "circular_arc": bool(plan.circular_arc),
        "selection_reason": str(getattr(plan, "selection_reason", "")),
        "route_truncated_by_mesh": bool(
            getattr(plan, "route_truncated_by_mesh", False)
        ),
        "replan_at_end": bool(getattr(plan, "replan_at_end", False)),
        "mesh_safe_prefix_length_m": (
            None
            if getattr(plan, "mesh_safe_prefix_length_m", None) is None
            else float(getattr(plan, "mesh_safe_prefix_length_m"))
        ),
        "centerline_source": getattr(plan.centerline_path, "source", None),
        "centerline_length_m": (
            None
            if plan.centerline_path is None
            else float(getattr(plan.centerline_path, "length_m", 0.0))
        ),
        "start": (
            None
            if not plan.route_points
            else _vector_payload(plan.route_points[0])
        ),
        "end": (
            None
            if not plan.route_points
            else _vector_payload(plan.route_points[-1])
        ),
    }


def _plan_needs_boundary_replan(plan: AutoDivePlan) -> bool:
    return bool(
        getattr(plan, "replan_at_end", False)
        and not _plan_requires_user_assist_at_boundary(plan)
    )


def _plan_requires_user_assist_at_boundary(plan: AutoDivePlan) -> bool:
    """Return whether a plan ends at an uncertain mesh boundary.

    A mesh-compromised prefix is useful for reaching the edge of known-safe
    geometry, but it is not a route that Guided Dive should continue through.
    Fully mesh-clear recovery legs retain their automatic end replan behavior.
    """
    return bool(getattr(plan, "route_truncated_by_mesh", False))


def _readiness_payload(readiness: AutoDiveReadiness) -> dict[str, Any]:
    return {
        "expected_cells": int(readiness.expected_cells),
        "loaded_cells": int(readiness.loaded_cells),
        "pending_cells": int(readiness.pending_cells),
        "failed_cells": int(readiness.failed_cells),
        "missing_cells": int(readiness.missing_cells),
        "progress": float(readiness.progress),
        "ready": bool(readiness.ready),
    }


def _camera_payload(camera) -> dict[str, Any]:
    return {
        "position": _vector_payload(getattr(camera, "position", ())),
        "yaw_deg": math.degrees(float(getattr(camera, "yaw", 0.0))),
        "pitch_deg": math.degrees(float(getattr(camera, "pitch", 0.0))),
        "roll_deg": math.degrees(float(getattr(camera, "roll", 0.0))),
    }


def _next_pose_payload(plan: AutoDivePlan, elapsed_s: float) -> dict[str, Any] | None:
    duration = float(getattr(plan.route, "duration_s", 0.0))
    if duration <= 1e-9:
        return None
    lookahead_s = min(duration, float(elapsed_s) + 0.5)
    pose = plan.route.pose_at(lookahead_s)
    return {
        "time_s": float(lookahead_s),
        "position": _vector_payload(pose.position),
        "yaw_deg": float(pose.yaw_deg),
        "pitch_deg": float(pose.pitch_deg),
    }


def _world_cell_payload(world, position: np.ndarray) -> list[int] | None:
    cell_for_position = getattr(world, "cell_for_position", None)
    if not callable(cell_for_position):
        return None
    try:
        return [int(value) for value in cell_for_position(position)]
    except Exception:
        return None


def _vector_payload(values) -> list[float]:
    try:
        array = np.asarray(values, dtype=np.float64).reshape(-1)
    except Exception:
        return []
    return [round(float(value), 6) for value in array]
