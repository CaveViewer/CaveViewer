"""Guided Dive playback and streaming-readiness state for the viewer."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum
import heapq
import math
import threading
import time
from typing import Any

import numpy as np

from caveviewer.core.navigation.autodive import (
    DEFAULT_AUTO_DIVE_REPLAN_PLANNING_BUDGET_S,
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
DEFAULT_AUTO_DIVE_USER_ASSIST_SAMPLE_INTERVAL_S = 0.25
DEFAULT_AUTO_DIVE_USER_ASSIST_SAMPLE_DISTANCE_M = 1.0
DEFAULT_AUTO_DIVE_USER_ASSIST_MAX_SAMPLES = 512
DEFAULT_AUTO_DIVE_USER_ASSIST_PAUSE_THRESHOLD_S = 1.0
DEFAULT_AUTO_DIVE_USER_ASSIST_MOTION_THRESHOLD_M = 0.05
DEFAULT_AUTO_DIVE_USER_ASSIST_TURN_ANGLE_DEGREES = 30.0


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
        perf_counter: Callable[[], float] | None = None,
        planning_budget_s: float = DEFAULT_AUTO_DIVE_REPLAN_PLANNING_BUDGET_S,
    ) -> None:
        self._manifest = manifest
        self._settings = settings
        self._plan_builder = plan_builder
        self._cache_dir = cache_dir
        self._blackbox = blackbox
        self._perf_counter = perf_counter or time.perf_counter
        budget = float(planning_budget_s)
        if not math.isfinite(budget) or budget <= 0.0:
            raise ValueError("replan planning budget must be positive")
        self._planning_budget_s = budget
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="AutoDiveReplanner",
        )
        self._lock = threading.Lock()
        self._generation = 0
        self._latest_generation = 0
        self._latest_plan: AutoDivePlan | None = None
        self._latest_plan_generation: int | None = None
        self._last_taken_plan_generation: int | None = None
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
        request_started_at = self._perf_counter()
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
                    replan_id=None,
                    reason="shutdown",
                    position=position,
                    yaw=yaw,
                    pitch=pitch,
                    travel_yaw=travel_yaw,
                    travel_pitch=travel_pitch,
                    avoid_positions=avoided,
                    user_reposition=bool(user_reposition),
                    planning_budget_s=float(self._planning_budget_s),
                )
                return False
            if self._pending_future is not None and not self._pending_future.done():
                self._record_blackbox(
                    "replan_request_skipped",
                    replan_id=None,
                    reason="already_pending",
                    position=position,
                    yaw=yaw,
                    pitch=pitch,
                    travel_yaw=travel_yaw,
                    travel_pitch=travel_pitch,
                    avoid_positions=avoided,
                    user_reposition=bool(user_reposition),
                    planning_budget_s=float(self._planning_budget_s),
                )
                return False
            self._generation += 1
            generation = self._generation
            replan_id = f"replan-{generation}"
            self._record_blackbox(
                "replan_requested",
                replan_id=replan_id,
                generation=generation,
                position=position,
                yaw=yaw,
                pitch=pitch,
                travel_yaw=travel_yaw,
                travel_pitch=travel_pitch,
                avoid_positions=avoided,
                user_reposition=bool(user_reposition),
                planning_budget_s=float(self._planning_budget_s),
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
                request_started_at,
            )
            self._pending_future = future
            future.add_done_callback(self._store_completed_plan)
            return True

    def take_latest_plan(self) -> AutoDivePlan | None:
        """Return and clear the newest completed plan."""
        with self._lock:
            plan = self._latest_plan
            self._last_taken_plan_generation = self._latest_plan_generation
            self._latest_plan = None
            self._latest_plan_generation = None
            return plan

    @property
    def last_taken_plan_generation(self) -> int | None:
        """Return the generation associated with the last taken plan."""
        with self._lock:
            return self._last_taken_plan_generation

    @property
    def planning_budget_s(self) -> float:
        """Return the worker budget mirrored by the owner-thread timeout."""
        return float(self._planning_budget_s)

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
            self._latest_plan_generation = None
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
        request_started_at: float | None = None,
    ) -> tuple[int, AutoDivePlan]:
        build_started_at = self._perf_counter()
        replan_id = f"replan-{generation}"
        queue_duration_ms = (
            None
            if request_started_at is None
            else max(0.0, (build_started_at - request_started_at) * 1000.0)
        )
        self._record_blackbox(
            "replan_build_started",
            replan_id=replan_id,
            generation=generation,
            position=current_position,
            yaw=current_yaw,
            pitch=current_pitch,
            travel_yaw=current_travel_yaw,
            travel_pitch=current_travel_pitch,
            avoid_positions=avoid_positions,
            user_reposition=bool(user_reposition),
            queue_duration_ms=queue_duration_ms,
            planning_budget_s=float(self._planning_budget_s),
        )
        try:
            kwargs: dict[str, Any] = {
                "current_position": current_position,
                "settings": replace(
                    self._settings,
                    planning_budget_s=float(self._planning_budget_s),
                ),
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
                def record_diagnostic(event: str, payload: Mapping[str, Any]) -> None:
                    enriched = dict(payload)
                    enriched.setdefault("replan_id", replan_id)
                    enriched.setdefault("replan_generation", generation)
                    enriched.setdefault("generation", generation)
                    enriched.setdefault("position", current_position)
                    self._record_blackbox(event, **enriched)

                kwargs["diagnostics"] = record_diagnostic
            plan = self._plan_builder(self._manifest, **kwargs)
        except Exception as exc:
            now = self._perf_counter()
            self._record_blackbox(
                "replan_failed",
                replan_id=replan_id,
                generation=generation,
                position=current_position,
                error_type=type(exc).__name__,
                error=str(exc),
                queue_duration_ms=queue_duration_ms,
                build_duration_ms=max(
                    0.0,
                    (now - build_started_at) * 1000.0,
                ),
                total_duration_ms=(
                    None
                    if request_started_at is None
                    else max(0.0, (now - request_started_at) * 1000.0)
                ),
                planning_budget_s=float(self._planning_budget_s),
            )
            raise
        completed_at = self._perf_counter()
        self._record_blackbox(
            "replan_completed",
            replan_id=replan_id,
            generation=generation,
            position=current_position,
            plan=_plan_summary(plan),
            queue_duration_ms=queue_duration_ms,
            build_duration_ms=max(
                0.0,
                (completed_at - build_started_at) * 1000.0,
            ),
            total_duration_ms=(
                None
                if request_started_at is None
                else max(0.0, (completed_at - request_started_at) * 1000.0)
            ),
            planning_budget_s=float(self._planning_budget_s),
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
            self._latest_plan_generation = generation

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
        replanner_budget = getattr(
            replanner,
            "planning_budget_s",
            DEFAULT_AUTO_DIVE_REPLAN_PLANNING_BUDGET_S,
        )
        try:
            replanner_budget = float(replanner_budget)
        except (TypeError, ValueError):
            replanner_budget = DEFAULT_AUTO_DIVE_REPLAN_PLANNING_BUDGET_S
        self._replan_planning_budget_s = (
            replanner_budget
            if math.isfinite(replanner_budget) and replanner_budget > 0.0
            else DEFAULT_AUTO_DIVE_REPLAN_PLANNING_BUDGET_S
        )
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
        self._last_commanded_position: np.ndarray | None = None
        self._last_observed_position: np.ndarray | None = None
        self._last_observed_at: float | None = None
        self._observed_distance_m = 0.0
        self._plan_sequence = 0
        self._mesh_recovery_started_at: float | None = None
        self._mesh_recovery_replan_pending = False
        self._lookahead_replan_pending = False
        self._replan_wait_started_at: float | None = None
        self._replan_wait_kind: str | None = None
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
        self._reset_user_assist_trace()

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
        if (
            self._mesh_recovery_active()
            or self._lookahead_replan_pending
            or self._user_resume_replan_pending
        ):
            return None
        return self.progress

    @property
    def status_note(self) -> str:
        if self.state is AutoDiveState.WAITING_FOR_USER:
            return "Guided Dive needs input"
        if self._user_resume_replan_pending:
            return "Finding a safe continuation"
        if self._lookahead_replan_pending:
            return "Finding the next branch"
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

    def _begin_replan_wait(self, kind: str, *, now: float) -> None:
        self._replan_wait_started_at = float(now)
        self._replan_wait_kind = str(kind)

    def _clear_replan_wait(self) -> None:
        self._replan_wait_started_at = None
        self._replan_wait_kind = None

    def _replan_wait_expired(self, *, now: float) -> bool:
        if self._replan_wait_started_at is None:
            return False
        if not (
            self._mesh_recovery_replan_pending
            or self._lookahead_replan_pending
            or self._user_resume_replan_pending
        ):
            self._clear_replan_wait()
            return False
        return (
            float(now) - self._replan_wait_started_at
            >= self._replan_planning_budget_s
        )

    def _handoff_after_replan_budget(
        self,
        camera,
        world,
        *,
        now: float,
    ) -> bool:
        if not self._replan_wait_expired(now=now):
            return False
        position = np.asarray(camera.position, dtype=np.float64).reshape(3)
        started_at = self._replan_wait_started_at
        elapsed_s = (
            None
            if started_at is None
            else max(0.0, float(now) - float(started_at))
        )
        kind = self._replan_wait_kind or "unknown"
        cancel_pending = getattr(self.replanner, "cancel_pending", None)
        if callable(cancel_pending):
            cancel_pending()
        self._mesh_recovery_replan_pending = False
        self._lookahead_replan_pending = False
        self._user_resume_replan_pending = False
        self._record_blackbox(
            "replan_planning_budget_exceeded",
            kind=kind,
            budget_s=float(self._replan_planning_budget_s),
            elapsed_s=elapsed_s,
            position=_vector_payload(position),
            elapsed_route_s=float(self._elapsed_s),
            progress=float(self.progress),
            plan_sequence=int(self._plan_sequence),
            plan=_plan_summary(self.plan),
        )
        self._enter_user_assist(
            world,
            reason="replan_planning_budget_exceeded",
            position=position,
            details={
                "kind": kind,
                "budget_s": float(self._replan_planning_budget_s),
                "elapsed_s": elapsed_s,
            },
            now=now,
        )
        return True

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
        self._last_blackbox_frame_at = None
        self._last_commanded_position = None
        self._last_observed_position = None
        self._last_observed_at = None
        self._observed_distance_m = 0.0
        self._plan_sequence = 0
        self._mesh_recovery_started_at = None
        self._mesh_recovery_replan_pending = False
        self._lookahead_replan_pending = False
        self._clear_replan_wait()
        self._mesh_recovery_attempts = 0
        self._mesh_recovery_boundary_positions = []
        self._user_assist_reason = None
        self._user_assist_anchor_position = None
        self._user_assist_last_position = None
        self._user_assist_travel_vector = None
        self._user_resume_replan_pending = False
        self._reset_user_assist_trace()
        self.state = AutoDiveState.LOADING
        apply_pose_to_camera(camera, self.plan.route.pose_at(0.0))
        self._set_commanded_position(camera)
        self._set_observed_position(camera, now=now, reset=True)
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
            plan_sequence=int(self._plan_sequence),
            route_speed_m_per_second=float(self._route_speed_m_per_second()),
            lookahead_seconds=float(self._effective_lookahead_seconds()),
            replan_planning_budget_s=float(self._replan_planning_budget_s),
            prefetch=self._prefetch_payload(),
        )

    def observe_user_assist_position(
        self,
        position,
        *,
        now: float | None = None,
        yaw: float | None = None,
        pitch: float | None = None,
        roll: float | None = None,
        world=None,
    ) -> None:
        """Remember manual movement while Guided Dive is waiting for input."""
        if self.state is not AutoDiveState.WAITING_FOR_USER:
            return
        now = self.perf_counter() if now is None else float(now)
        point = np.asarray(position, dtype=np.float64).reshape(3).copy()
        if self._user_assist_anchor_position is None:
            self._user_assist_anchor_position = point.copy()
        self._user_assist_last_position = point.copy()
        self._record_user_assist_sample(
            point,
            now=now,
            yaw=yaw,
            pitch=pitch,
            roll=roll,
            world=world,
        )
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
        self.observe_user_assist_position(
            current_position,
            now=now,
            yaw=float(getattr(camera, "yaw", 0.0)),
            pitch=float(getattr(camera, "pitch", 0.0)),
            roll=float(getattr(camera, "roll", 0.0)),
            world=world,
        )
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
        self._last_blackbox_frame_at = None
        self._last_commanded_position = current_position.copy()
        self._last_observed_position = current_position.copy()
        self._last_observed_at = now
        self._observed_distance_m = 0.0
        self._plan_sequence = 0
        self._mesh_recovery_started_at = None
        self._mesh_recovery_replan_pending = False
        self._lookahead_replan_pending = False
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

        trace_summary = self._finish_user_assist_trace(
            current_position,
            now=now,
            reason="resume",
        )
        self._user_resume_replan_pending = True
        if requested or self._replan_wait_started_at is None:
            self._begin_replan_wait("user_resume", now=now)
        self._record_blackbox(
            "user_assist_resumed",
            requested=bool(requested),
            position=_vector_payload(current_position),
            yaw=current_yaw,
            pitch=current_pitch,
            travel_yaw=travel_yaw,
            travel_pitch=travel_pitch,
            avoid_positions=[list(position) for position in avoid_positions],
            plan_sequence=int(self._plan_sequence),
            prefetch=self._prefetch_payload(),
            user_assist_trace=trace_summary,
        )
        return True

    def stop(self, world=None, *, completed: bool = False) -> None:
        """Stop Guided Dive and clear owned streaming prefetch cells."""
        user_assist_trace = self._finish_user_assist_trace(
            self._user_assist_last_position,
            now=self.perf_counter(),
            reason="completed" if completed else "stop",
        )
        self._record_blackbox(
            "auto_dive_stopped",
            completed=bool(completed),
            outcome="completed" if completed else "stopped",
            state=str(self.state.value),
            elapsed_s=float(self._elapsed_s),
            progress=float(self.progress),
            readiness=_readiness_payload(self._readiness),
            plan_sequence=int(self._plan_sequence),
            observed_distance_m=float(self._observed_distance_m),
            last_commanded_position=_vector_payload(self._last_commanded_position),
            last_observed_position=_vector_payload(self._last_observed_position),
            final_command_error_m=self._command_error_m(),
            mesh_recovery_attempts=int(self._mesh_recovery_attempts),
            user_assist_reason=self._user_assist_reason,
            user_assist_trace=user_assist_trace,
            remaining_distance_m=self._route_remaining_distance_m(),
            prefetch=self._prefetch_payload(),
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
        if self._handoff_after_replan_budget(camera, world, now=now):
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
            self._set_commanded_position(camera)
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
        self._set_commanded_position(camera)
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
                    now=now,
                )
                return self.state
            if self._maybe_request_lookahead_replan(
                camera,
                now=now,
                world=world,
            ):
                if self.state is not AutoDiveState.WAITING_FOR_USER:
                    self.state = AutoDiveState.LOADING
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
        if self._handoff_after_replan_budget(camera, world, now=now):
            return False
        user_resume = self._user_resume_replan_pending

        swapped = False
        latest_plan = replanner.take_latest_plan()
        replan_generation = getattr(replanner, "last_taken_plan_generation", None)
        rejected_replan = False
        if latest_plan is not None:
            mesh_recovery = self._mesh_recovery_active()
            lookahead_replan = self._lookahead_replan_pending
            if (
                self.replan_only_during_survey
                and not self._survey_active(now=now)
                and not mesh_recovery
                and not lookahead_replan
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
                )
            if rejection is None:
                resume_elapsed_s = _route_elapsed_nearest_position(
                    latest_plan,
                    current_position,
                )
                self.plan = latest_plan
                previous_plan_sequence = self._plan_sequence
                self._plan_sequence += 1
                self._started_at = now - resume_elapsed_s
                self._pause_started_at = None
                self._paused_seconds = 0.0
                self._elapsed_s = resume_elapsed_s
                self._last_rejected_replan_position = None
                self._mesh_recovery_started_at = None
                self._mesh_recovery_replan_pending = False
                self._lookahead_replan_pending = False
                self._user_resume_replan_pending = False
                self._clear_replan_wait()
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
                    plan_sequence=int(self._plan_sequence),
                    previous_plan_sequence=int(previous_plan_sequence),
                    replan_generation=replan_generation,
                    remaining_distance_m=self._route_remaining_distance_m(),
                    prefetch=self._prefetch_payload(),
                )
                swapped = True
            else:
                self._mesh_recovery_replan_pending = False
                self._lookahead_replan_pending = False
                self._user_resume_replan_pending = False
                self._clear_replan_wait()
                self._last_rejected_replan_position = current_position.copy()
                rejected_replan = True
                self._record_blackbox(
                    "replan_rejected",
                    camera_position=_vector_payload(current_position),
                    plan=_plan_summary(latest_plan),
                    plan_sequence=int(self._plan_sequence),
                    replan_generation=replan_generation,
                    **rejection,
                )
                if mesh_recovery or lookahead_replan or user_resume:
                    self._enter_user_assist(
                        world,
                        reason=str(rejection.get("reason", "replan_rejected")),
                        position=current_position,
                        details=rejection,
                        now=now,
                    )
        elif (
            self._mesh_recovery_replan_pending
            or self._lookahead_replan_pending
            or self._user_resume_replan_pending
        ):
            has_pending = getattr(replanner, "has_pending", None)
            if callable(has_pending) and not has_pending():
                user_resume_without_plan = self._user_resume_replan_pending
                lookahead_without_plan = self._lookahead_replan_pending
                self._mesh_recovery_replan_pending = False
                self._lookahead_replan_pending = False
                self._user_resume_replan_pending = False
                self._clear_replan_wait()
                event = (
                    "user_resume_replan_finished_without_plan"
                    if user_resume_without_plan
                    else "lookahead_replan_finished_without_plan"
                    if lookahead_without_plan
                    else "mesh_recovery_replan_finished_without_plan"
                )
                self._record_blackbox(
                    event,
                    attempts=int(self._mesh_recovery_attempts),
                    position=_vector_payload(current_position),
                    elapsed_s=float(self._elapsed_s),
                    progress=float(self.progress),
                    plan_sequence=int(self._plan_sequence),
                    remaining_distance_m=self._route_remaining_distance_m(),
                )
                self._enter_user_assist(
                    world,
                    reason=(
                        "user_resume_replan_finished_without_plan"
                        if user_resume_without_plan
                        else "lookahead_replan_finished_without_plan"
                        if lookahead_without_plan
                        else "mesh_recovery_replan_finished_without_plan"
                    ),
                    position=current_position,
                    now=now,
                )

        if not rejected_replan and self._should_request_replan(
            current_position,
            now=now,
        ):
            travel_yaw, travel_pitch = self._current_route_travel_angles(
                current_position
            )
            if replanner.request(
                current_position,
                current_yaw=float(getattr(camera, "yaw", 0.0)),
                current_pitch=float(getattr(camera, "pitch", 0.0)),
                current_travel_yaw=travel_yaw,
                current_travel_pitch=travel_pitch,
            ):
                self._begin_replan_wait("distance", now=now)
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
        clamp_distance_m = float(
            np.linalg.norm(
                np.asarray(after, dtype=np.float64)
                - np.asarray(before, dtype=np.float64)
            )
        )
        if self._user_assist_trace_active():
            self._user_assist_trace_clamp_count += 1
            self._user_assist_trace_clamp_distance_m += clamp_distance_m
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
            plan_sequence=int(self._plan_sequence),
            clamp_distance_m=clamp_distance_m,
            clamp_vector=_vector_payload(
                np.asarray(after, dtype=np.float64)
                - np.asarray(before, dtype=np.float64)
            ),
            commanded_camera_position=_vector_payload(
                self._last_commanded_position
            ),
            user_assist=bool(self._user_assist_trace_active()),
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
        observation = self._observe_position(position, now=now)
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
            plan_sequence=int(self._plan_sequence),
            commanded_camera_position=_vector_payload(
                self._last_commanded_position
            ),
            command_error_m=_command_error_payload(
                position,
                self._last_commanded_position,
            ),
            observed_displacement_m=float(observation["displacement_m"]),
            observed_speed_m_per_second=observation["speed_m_per_second"],
            observed_distance_m=float(self._observed_distance_m),
            observation_interval_s=observation["interval_s"],
            route_prefetch_radius_cells=int(
                self._effective_route_prefetch_radius_cells()
            ),
            effective_lookahead_seconds=float(self._effective_lookahead_seconds()),
            prefetch=self._prefetch_payload(),
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

    def _set_commanded_position(self, camera) -> None:
        try:
            self._last_commanded_position = np.asarray(
                camera.position,
                dtype=np.float64,
            ).reshape(3).copy()
        except Exception:
            self._last_commanded_position = None

    def _set_observed_position(
        self,
        camera,
        *,
        now: float,
        reset: bool = False,
    ) -> None:
        try:
            position = np.asarray(camera.position, dtype=np.float64).reshape(3)
        except Exception:
            return
        self._set_observed_point(position, now=now, reset=reset)

    def _set_observed_point(
        self,
        position: np.ndarray,
        *,
        now: float,
        reset: bool = False,
    ) -> dict[str, float | None]:
        point = np.asarray(position, dtype=np.float64).reshape(3).copy()
        if reset or self._last_observed_position is None:
            self._last_observed_position = point
            self._last_observed_at = float(now)
            return {
                "displacement_m": 0.0,
                "speed_m_per_second": None,
                "interval_s": None,
            }
        previous = self._last_observed_position
        previous_at = self._last_observed_at
        displacement_m = float(np.linalg.norm(point - previous))
        interval_s = (
            None
            if previous_at is None
            else max(0.0, float(now) - float(previous_at))
        )
        speed_m_per_second = (
            None
            if interval_s is None or interval_s <= 1e-9
            else displacement_m / interval_s
        )
        self._observed_distance_m += displacement_m
        self._last_observed_position = point
        self._last_observed_at = float(now)
        return {
            "displacement_m": displacement_m,
            "speed_m_per_second": speed_m_per_second,
            "interval_s": interval_s,
        }

    def _observe_position(
        self,
        position: np.ndarray,
        *,
        now: float,
    ) -> dict[str, float | None]:
        return self._set_observed_point(position, now=now)

    def _command_error_m(self) -> float | None:
        if self._last_commanded_position is None or self._last_observed_position is None:
            return None
        return float(
            np.linalg.norm(self._last_observed_position - self._last_commanded_position)
        )

    def _prefetch_payload(self) -> dict[str, Any]:
        sample_limit = 24
        cell_count = len(self._prefetch_cells)
        cells = heapq.nsmallest(
            sample_limit,
            (
                tuple(int(value) for value in cell)
                for cell in self._prefetch_cells
            ),
        )
        return {
            "cell_count": cell_count,
            "cell_sample": [list(cell) for cell in cells[:sample_limit]],
            "cell_sample_truncated": cell_count > sample_limit,
        }

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

    def _maybe_request_lookahead_replan(
        self,
        camera,
        *,
        now: float,
        world=None,
    ) -> bool:
        """Request the next voxel branch when a bounded prefix is complete."""
        if not _plan_uses_voxel_lookahead_boundary(self.plan):
            return False
        if self.replanner is None:
            self._enter_user_assist(
                world,
                reason="lookahead_replan_unavailable",
                position=np.asarray(camera.position, dtype=np.float64),
                now=now,
            )
            return True
        if self._lookahead_replan_pending:
            return True

        current_position = np.asarray(camera.position, dtype=np.float64)
        travel_yaw, travel_pitch = self._current_route_travel_angles(
            current_position
        )
        requested = self.replanner.request(
            current_position,
            current_yaw=float(getattr(camera, "yaw", 0.0)),
            current_pitch=float(getattr(camera, "pitch", 0.0)),
            current_travel_yaw=travel_yaw,
            current_travel_pitch=travel_pitch,
        )
        self._lookahead_replan_pending = True
        if requested or self._replan_wait_started_at is None:
            self._begin_replan_wait("lookahead", now=now)
        if requested:
            self._last_replan_request_position = current_position.copy()
            self._last_replan_request_at = now
        self._record_blackbox(
            "lookahead_replan_requested",
            requested=bool(requested),
            position=_vector_payload(current_position),
            yaw=float(getattr(camera, "yaw", 0.0)),
            pitch=float(getattr(camera, "pitch", 0.0)),
            travel_yaw=travel_yaw,
            travel_pitch=travel_pitch,
            elapsed_s=float(self._elapsed_s),
            progress=float(self.progress),
            plan_sequence=int(self._plan_sequence),
            plan=_plan_summary(self.plan),
        )
        return True

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
                now=now,
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
                now=now,
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
        if requested or self._replan_wait_started_at is None:
            self._begin_replan_wait("mesh_recovery", now=now)
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
        self._set_commanded_position(camera)
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
        self._set_commanded_position(camera)

    def _route_speed_m_per_second(self) -> float:
        return float(self.plan.route_length_m) / max(1e-9, float(self.plan.duration_s))

    def _user_assist_trace_active(self) -> bool:
        return self._user_assist_trace_started_at is not None

    def _reset_user_assist_trace(self) -> None:
        self._user_assist_trace_started_at: float | None = None
        self._user_assist_trace_readiness_before_assist: dict[str, Any] | None = None
        self._user_assist_trace_last_observed_at: float | None = None
        self._user_assist_trace_last_observed_position: np.ndarray | None = None
        self._user_assist_trace_last_sample_at: float | None = None
        self._user_assist_trace_last_sample_position: np.ndarray | None = None
        self._user_assist_trace_samples: list[dict[str, Any]] = []
        self._user_assist_trace_total_distance_m = 0.0
        self._user_assist_trace_turn_count = 0
        self._user_assist_trace_last_direction: np.ndarray | None = None
        self._user_assist_trace_max_speed_m_per_second = 0.0
        self._user_assist_trace_pause_started_at: float | None = None
        self._user_assist_trace_pause_count = 0
        self._user_assist_trace_paused_seconds = 0.0
        self._user_assist_trace_clamp_count = 0
        self._user_assist_trace_clamp_distance_m = 0.0
        self._user_assist_trace_readiness_sample_count = 0
        self._user_assist_trace_max_expected_cells = 0
        self._user_assist_trace_max_pending_cells = 0
        self._user_assist_trace_max_missing_cells = 0
        self._user_assist_trace_max_readiness_progress = 0.0
        self._user_assist_trace_first_moving_footprint_cell: tuple[int, int] | None = None

    def _user_assist_footprint_cell(
        self,
        position: np.ndarray,
    ) -> tuple[int, int] | None:
        centerline_path = getattr(self.plan, "centerline_path", None)
        try:
            cell_size = float(
                getattr(centerline_path, "footprint_cell_size", None)
            )
        except (TypeError, ValueError):
            return None
        if not math.isfinite(cell_size) or cell_size <= 1e-9:
            return None
        return (
            int(math.floor(float(position[0]) / cell_size)),
            int(math.floor(float(position[2]) / cell_size)),
        )

    def _record_user_assist_sample(
        self,
        position: np.ndarray,
        *,
        now: float,
        yaw: float | None = None,
        pitch: float | None = None,
        roll: float | None = None,
        world=None,
    ) -> None:
        if not self._user_assist_trace_active():
            return
        point = np.asarray(position, dtype=np.float64).reshape(3).copy()
        sample_now = float(now)
        previous_position = self._user_assist_trace_last_observed_position
        previous_at = self._user_assist_trace_last_observed_at
        interval_s = (
            0.0
            if previous_at is None
            else max(0.0, sample_now - previous_at)
        )
        movement_distance_m = (
            0.0
            if previous_position is None
            else float(np.linalg.norm(point - previous_position))
        )
        if previous_position is not None:
            self._user_assist_trace_total_distance_m += movement_distance_m
            if interval_s > 1e-9:
                self._user_assist_trace_max_speed_m_per_second = max(
                    self._user_assist_trace_max_speed_m_per_second,
                    movement_distance_m / interval_s,
                )
            if movement_distance_m >= DEFAULT_AUTO_DIVE_USER_ASSIST_MOTION_THRESHOLD_M:
                direction = (point - previous_position) / movement_distance_m
                previous_direction = self._user_assist_trace_last_direction
                if previous_direction is not None:
                    turn_alignment = float(np.dot(previous_direction, direction))
                    if turn_alignment < math.cos(
                        math.radians(DEFAULT_AUTO_DIVE_USER_ASSIST_TURN_ANGLE_DEGREES)
                    ):
                        self._user_assist_trace_turn_count += 1
                self._user_assist_trace_last_direction = direction
                if self._user_assist_trace_pause_started_at is not None:
                    pause_s = max(
                        0.0,
                        sample_now - self._user_assist_trace_pause_started_at,
                    )
                    if pause_s >= DEFAULT_AUTO_DIVE_USER_ASSIST_PAUSE_THRESHOLD_S:
                        self._user_assist_trace_pause_count += 1
                        self._user_assist_trace_paused_seconds += pause_s
                    self._user_assist_trace_pause_started_at = None
                if self._user_assist_trace_first_moving_footprint_cell is None:
                    self._user_assist_trace_first_moving_footprint_cell = (
                        self._user_assist_footprint_cell(point)
                    )
            elif self._user_assist_trace_pause_started_at is None:
                self._user_assist_trace_pause_started_at = (
                    previous_at if previous_at is not None else sample_now
                )
        self._user_assist_trace_last_observed_position = point
        self._user_assist_trace_last_observed_at = sample_now

        readiness = _readiness_payload(self._readiness)
        self._user_assist_trace_readiness_sample_count += 1
        self._user_assist_trace_max_expected_cells = max(
            self._user_assist_trace_max_expected_cells,
            int(readiness["expected_cells"]),
        )
        self._user_assist_trace_max_pending_cells = max(
            self._user_assist_trace_max_pending_cells,
            int(readiness["pending_cells"]),
        )
        self._user_assist_trace_max_missing_cells = max(
            self._user_assist_trace_max_missing_cells,
            int(readiness["missing_cells"]),
        )
        self._user_assist_trace_max_readiness_progress = max(
            self._user_assist_trace_max_readiness_progress,
            float(readiness["progress"]),
        )

        last_sample_position = self._user_assist_trace_last_sample_position
        last_sample_at = self._user_assist_trace_last_sample_at
        sample_interval_s = (
            0.0
            if last_sample_at is None
            else max(0.0, sample_now - last_sample_at)
        )
        sample_distance_m = (
            0.0
            if last_sample_position is None
            else float(np.linalg.norm(point - last_sample_position))
        )
        sample_due = (
            last_sample_position is None
            or sample_interval_s >= DEFAULT_AUTO_DIVE_USER_ASSIST_SAMPLE_INTERVAL_S
            or sample_distance_m >= DEFAULT_AUTO_DIVE_USER_ASSIST_SAMPLE_DISTANCE_M
        )
        if (
            not sample_due
            or len(self._user_assist_trace_samples)
            >= DEFAULT_AUTO_DIVE_USER_ASSIST_MAX_SAMPLES
        ):
            return

        anchor = self._user_assist_anchor_position
        displacement = (
            np.zeros(3, dtype=np.float64)
            if anchor is None
            else point - anchor
        )
        sample_speed_m_per_second = (
            0.0
            if sample_interval_s <= 1e-9
            else sample_distance_m / sample_interval_s
        )
        footprint_cell = self._user_assist_footprint_cell(point)
        sample = {
            "sample_index": len(self._user_assist_trace_samples),
            "elapsed_s": max(
                0.0,
                sample_now - float(self._user_assist_trace_started_at),
            ),
            "position": _vector_payload(point),
            "displacement_from_assist_start": _vector_payload(displacement),
            "world_cell": _world_cell_payload(world, point),
            "footprint_cell": (
                None
                if footprint_cell is None
                else [int(footprint_cell[0]), int(footprint_cell[1])]
            ),
            "yaw_deg": None if yaw is None else math.degrees(float(yaw)),
            "pitch_deg": None if pitch is None else math.degrees(float(pitch)),
            "roll_deg": None if roll is None else math.degrees(float(roll)),
            "distance_since_previous_sample_m": float(sample_distance_m),
            "interval_since_previous_sample_s": float(sample_interval_s),
            "speed_m_per_second": float(sample_speed_m_per_second),
            "movement_distance_m": float(movement_distance_m),
            "total_distance_m": float(self._user_assist_trace_total_distance_m),
            "moving": bool(
                movement_distance_m >= DEFAULT_AUTO_DIVE_USER_ASSIST_MOTION_THRESHOLD_M
            ),
            "readiness": readiness,
        }
        self._user_assist_trace_samples.append(sample)
        self._user_assist_trace_last_sample_position = point
        self._user_assist_trace_last_sample_at = sample_now
        self._record_blackbox("user_assist_sample", **sample)

    def _user_assist_branch_trace_payload(
        self,
        displacement: np.ndarray,
    ) -> dict[str, Any] | None:
        selection = getattr(self.plan, "voxel_route_selection", None)
        if not isinstance(selection, Mapping):
            return None

        def cell_value(value: Any) -> tuple[int, int] | None:
            try:
                values = tuple(value)
                if len(values) < 2:
                    return None
                return int(values[0]), int(values[1])
            except (TypeError, ValueError):
                return None

        horizontal = np.asarray(
            (float(displacement[0]), float(displacement[2])),
            dtype=np.float64,
        )
        horizontal_norm = float(np.linalg.norm(horizontal))
        dominant_direction_xz = (
            None
            if horizontal_norm <= 1e-9
            else [
                float(horizontal[0] / horizontal_norm),
                float(horizontal[1] / horizontal_norm),
            ]
        )
        start_cell = cell_value(selection.get("start_cell"))
        first_cell = self._user_assist_trace_first_moving_footprint_cell
        candidate_payloads: list[dict[str, Any]] = []
        candidates = selection.get("branch_candidates", ())
        if isinstance(candidates, Sequence) and not isinstance(
            candidates,
            (str, bytes, bytearray),
        ):
            for candidate in candidates[:8]:
                if not isinstance(candidate, Mapping):
                    continue
                branch_cell = cell_value(candidate.get("branch_start_cell"))
                enriched = dict(candidate)
                alignment = None
                distance_to_first = None
                branch_delta = None
                if start_cell is not None and branch_cell is not None:
                    branch_delta = np.asarray(
                        (
                            branch_cell[0] - start_cell[0],
                            branch_cell[1] - start_cell[1],
                        ),
                        dtype=np.float64,
                    )
                    branch_norm = float(np.linalg.norm(branch_delta))
                    if horizontal_norm > 1e-9 and branch_norm > 1e-9:
                        alignment = float(
                            np.dot(horizontal / horizontal_norm, branch_delta / branch_norm)
                        )
                if first_cell is not None and branch_cell is not None:
                    distance_to_first = float(
                        math.hypot(
                            branch_cell[0] - first_cell[0],
                            branch_cell[1] - first_cell[1],
                        )
                    )
                enriched["manual_direction_alignment"] = alignment
                enriched["manual_distance_to_branch_start_cells"] = distance_to_first
                enriched["manual_branch_match"] = bool(
                    first_cell is not None
                    and branch_cell is not None
                    and branch_cell == first_cell
                )
                candidate_payloads.append(enriched)

        moved_toward_branch = None
        if candidate_payloads:
            moved_toward_branch = max(
                candidate_payloads,
                key=lambda candidate: (
                    bool(candidate.get("manual_branch_match")),
                    -float(
                        candidate.get(
                            "manual_distance_to_branch_start_cells",
                            math.inf,
                        )
                        if candidate.get("manual_distance_to_branch_start_cells") is not None
                        else math.inf
                    ),
                    float(
                        candidate.get("manual_direction_alignment")
                        if candidate.get("manual_direction_alignment") is not None
                        else -math.inf
                    ),
                ),
            )

        return {
            "selection_reason": selection.get("selection_reason"),
            "start_cell": selection.get("start_cell"),
            "first_moving_footprint_cell": (
                None
                if first_cell is None
                else [int(first_cell[0]), int(first_cell[1])]
            ),
            "dominant_direction_xz": dominant_direction_xz,
            "planned_branch": selection.get("branch"),
            "branch_candidates": candidate_payloads,
            "moved_toward_branch": moved_toward_branch,
        }

    def _finish_user_assist_trace(
        self,
        final_position: np.ndarray | None,
        *,
        now: float,
        reason: str,
    ) -> dict[str, Any] | None:
        if not self._user_assist_trace_active():
            return None
        trace_now = float(now)
        point = None
        if final_position is not None:
            try:
                point = np.asarray(final_position, dtype=np.float64).reshape(3).copy()
            except (TypeError, ValueError):
                point = None
        if point is None:
            point = self._user_assist_trace_last_observed_position
        if point is None:
            point = self._user_assist_anchor_position
        if point is not None:
            self._record_user_assist_sample(point, now=trace_now)

        if self._user_assist_trace_pause_started_at is not None:
            pause_s = max(
                0.0,
                trace_now - self._user_assist_trace_pause_started_at,
            )
            if pause_s >= DEFAULT_AUTO_DIVE_USER_ASSIST_PAUSE_THRESHOLD_S:
                self._user_assist_trace_pause_count += 1
                self._user_assist_trace_paused_seconds += pause_s
            self._user_assist_trace_pause_started_at = None

        anchor = self._user_assist_anchor_position
        if anchor is None:
            anchor = point
        displacement = (
            np.zeros(3, dtype=np.float64)
            if anchor is None or point is None
            else point - anchor
        )
        net_distance_m = float(np.linalg.norm(displacement))
        duration_s = max(
            0.0,
            trace_now - float(self._user_assist_trace_started_at),
        )
        dominant_yaw, dominant_pitch = _vector_yaw_pitch(displacement)
        summary = {
            "reason": str(reason),
            "assist_reason": self._user_assist_reason,
            "duration_s": float(duration_s),
            "sample_count": len(self._user_assist_trace_samples),
            "sample_cap_reached": len(self._user_assist_trace_samples)
            >= DEFAULT_AUTO_DIVE_USER_ASSIST_MAX_SAMPLES,
            "anchor_position": _vector_payload(anchor),
            "final_resume_position": _vector_payload(point),
            "net_displacement": _vector_payload(displacement),
            "net_displacement_m": net_distance_m,
            "total_distance_m": float(self._user_assist_trace_total_distance_m),
            "path_efficiency": (
                None
                if self._user_assist_trace_total_distance_m <= 1e-9
                else net_distance_m / self._user_assist_trace_total_distance_m
            ),
            "dominant_direction_vector": _vector_payload(displacement),
            "dominant_direction_yaw": dominant_yaw,
            "dominant_direction_pitch": dominant_pitch,
            "turn_count": int(self._user_assist_trace_turn_count),
            "pause_count": int(self._user_assist_trace_pause_count),
            "paused_seconds": float(self._user_assist_trace_paused_seconds),
            "mean_speed_m_per_second": (
                0.0
                if duration_s <= 1e-9
                else float(self._user_assist_trace_total_distance_m) / duration_s
            ),
            "max_speed_m_per_second": float(
                self._user_assist_trace_max_speed_m_per_second
            ),
            "navigation_guard_clamp_count": int(
                self._user_assist_trace_clamp_count
            ),
            "navigation_guard_clamp_distance_m": float(
                self._user_assist_trace_clamp_distance_m
            ),
            "readiness_before_assist": self._user_assist_trace_readiness_before_assist,
            "readiness_sample_count": int(
                self._user_assist_trace_readiness_sample_count
            ),
            "max_expected_cells": int(self._user_assist_trace_max_expected_cells),
            "max_pending_cells": int(self._user_assist_trace_max_pending_cells),
            "max_missing_cells": int(self._user_assist_trace_max_missing_cells),
            "max_readiness_progress": float(
                self._user_assist_trace_max_readiness_progress
            ),
            "last_readiness": _readiness_payload(self._readiness),
            "first_sample_position": (
                None
                if not self._user_assist_trace_samples
                else self._user_assist_trace_samples[0]["position"]
            ),
            "last_sample_position": (
                None
                if not self._user_assist_trace_samples
                else self._user_assist_trace_samples[-1]["position"]
            ),
            "trace_policy": _user_assist_trace_policy_payload(),
            "plan_sequence": int(self._plan_sequence),
            "plan": _plan_summary(self.plan),
            "voxel_branch_trace": self._user_assist_branch_trace_payload(
                displacement
            ),
        }
        self._record_blackbox("user_assist_trace_completed", **summary)
        self._reset_user_assist_trace()
        return summary

    def _enter_user_assist(
        self,
        world,
        *,
        reason: str,
        position: np.ndarray,
        details: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        if self.state is AutoDiveState.WAITING_FOR_USER:
            return
        trace_started_at = (
            self.perf_counter() if now is None else float(now)
        )
        position = np.asarray(position, dtype=np.float64).reshape(3)
        readiness_before_assist = _readiness_payload(self._readiness)
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
        self._lookahead_replan_pending = False
        self._user_resume_replan_pending = False
        self._clear_replan_wait()
        self._user_assist_anchor_position = position.copy()
        self._user_assist_last_position = position.copy()
        self._user_assist_travel_vector = None
        self._reset_user_assist_trace()
        self._user_assist_trace_started_at = trace_started_at
        self._user_assist_trace_readiness_before_assist = (
            readiness_before_assist
        )
        self._user_assist_trace_max_expected_cells = int(
            readiness_before_assist["expected_cells"]
        )
        self._user_assist_trace_max_pending_cells = int(
            readiness_before_assist["pending_cells"]
        )
        self._user_assist_trace_max_missing_cells = int(
            readiness_before_assist["missing_cells"]
        )
        self._user_assist_trace_max_readiness_progress = float(
            readiness_before_assist["progress"]
        )
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
            readiness_before_assist=readiness_before_assist,
            details=dict(details or {}),
            plan_sequence=int(self._plan_sequence),
            remaining_distance_m=self._route_remaining_distance_m(),
            observed_distance_m=float(self._observed_distance_m),
            prefetch=self._prefetch_payload(),
            assist_trace_policy=_user_assist_trace_policy_payload(),
            plan=_plan_summary(self.plan),
        )
        self._record_user_assist_sample(
            position,
            now=trace_started_at,
            world=world,
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


def _user_assist_trace_policy_payload() -> dict[str, Any]:
    """Return the fixed sampling policy used by manual-assist traces."""
    return {
        "sample_interval_s": float(DEFAULT_AUTO_DIVE_USER_ASSIST_SAMPLE_INTERVAL_S),
        "sample_distance_m": float(DEFAULT_AUTO_DIVE_USER_ASSIST_SAMPLE_DISTANCE_M),
        "max_samples": int(DEFAULT_AUTO_DIVE_USER_ASSIST_MAX_SAMPLES),
        "pause_threshold_s": float(DEFAULT_AUTO_DIVE_USER_ASSIST_PAUSE_THRESHOLD_S),
        "motion_threshold_m": float(DEFAULT_AUTO_DIVE_USER_ASSIST_MOTION_THRESHOLD_M),
        "turn_angle_degrees": float(DEFAULT_AUTO_DIVE_USER_ASSIST_TURN_ANGLE_DEGREES),
        "scope": "guided_dive_user_assist_only",
    }


def _bounded_voxel_route_selection_payload(
    selection: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Copy bounded voxel branch evidence into lifecycle summaries."""
    if not isinstance(selection, Mapping):
        return None
    payload = dict(selection)
    branch_candidates = selection.get("branch_candidates", ())
    if isinstance(branch_candidates, Sequence) and not isinstance(
        branch_candidates,
        (str, bytes, bytearray),
    ):
        payload["branch_candidates"] = [
            dict(candidate)
            for candidate in branch_candidates[:8]
            if isinstance(candidate, Mapping)
        ]
    for key in ("first_cells", "last_cells"):
        values = selection.get(key, ())
        if isinstance(values, Sequence) and not isinstance(
            values,
            (str, bytes, bytearray),
        ):
            payload[key] = [list(cell) for cell in values[:8]]
    return payload


def auto_dive_plan_summary(plan: AutoDivePlan) -> dict[str, Any]:
    """Return a bounded, JSON-safe summary for a plan lifecycle event."""
    route_points = tuple(getattr(plan, "route_points", ()) or ())
    centerline_path = getattr(plan, "centerline_path", None)
    return {
        "route_length_m": float(getattr(plan, "route_length_m", 0.0)),
        "duration_s": float(getattr(plan, "duration_s", 0.0)),
        "route_point_count": len(route_points),
        "route_cell_count": len(getattr(plan, "route_cells", ()) or ()),
        "render_distance_cells": int(getattr(plan, "render_distance_cells", 0)),
        "circular_arc": bool(getattr(plan, "circular_arc", False)),
        "selection_reason": str(getattr(plan, "selection_reason", "")),
        "route_truncated_by_mesh": bool(
            getattr(plan, "route_truncated_by_mesh", False)
        ),
        "replan_at_end": bool(getattr(plan, "replan_at_end", False)),
        "voxel_route_selection": _bounded_voxel_route_selection_payload(
            getattr(plan, "voxel_route_selection", None)
        ),
        "mesh_safe_prefix_length_m": (
            None
            if getattr(plan, "mesh_safe_prefix_length_m", None) is None
            else float(getattr(plan, "mesh_safe_prefix_length_m"))
        ),
        "centerline_source": getattr(centerline_path, "source", None),
        "centerline_length_m": (
            None
            if centerline_path is None
            else float(getattr(centerline_path, "length_m", 0.0))
        ),
        "start": (
            None
            if not route_points
            else _vector_payload(route_points[0])
        ),
        "end": (
            None
            if not route_points
            else _vector_payload(route_points[-1])
        ),
    }


def _plan_summary(plan: AutoDivePlan) -> dict[str, Any]:
    return auto_dive_plan_summary(plan)


def _plan_needs_boundary_replan(plan: AutoDivePlan) -> bool:
    return bool(
        getattr(plan, "replan_at_end", False)
        and not _plan_requires_user_assist_at_boundary(plan)
    )


def _plan_uses_voxel_lookahead_boundary(plan: AutoDivePlan) -> bool:
    """Return whether a clear route ends at a voxel lookahead frontier."""
    if not _plan_needs_boundary_replan(plan):
        return False
    reason = str(getattr(plan, "selection_reason", ""))
    return reason.startswith("voxel_branch_lookahead")


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


def _command_error_payload(
    observed: np.ndarray,
    commanded: np.ndarray | None,
) -> float | None:
    if commanded is None:
        return None
    try:
        return float(
            np.linalg.norm(
                np.asarray(observed, dtype=np.float64).reshape(3)
                - np.asarray(commanded, dtype=np.float64).reshape(3)
            )
        )
    except Exception:
        return None
