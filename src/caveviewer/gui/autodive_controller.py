"""Auto Dive playback and streaming-readiness state for the viewer."""

from __future__ import annotations

from collections.abc import Callable, Iterable
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


class AutoDiveState(str, Enum):
    """Lifecycle states for user-facing Auto Dive."""

    IDLE = "idle"
    LOADING = "loading"
    DIVING = "diving"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AutoDiveReadiness:
    """Route-lookahead streaming readiness for Auto Dive UI decisions."""

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
    """Single-worker receding-horizon Auto Dive replanner.

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
        blackbox: Any | None = None,
    ) -> None:
        self._manifest = manifest
        self._settings = settings
        self._plan_builder = plan_builder
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

    def request(self, current_position: np.ndarray | tuple[float, float, float]) -> bool:
        """Queue one replan from the current camera position if none is pending."""
        position = tuple(
            float(value)
            for value in np.asarray(current_position, dtype=np.float64).reshape(3)
        )
        with self._lock:
            if self._shutdown:
                self._record_blackbox(
                    "replan_request_skipped",
                    reason="shutdown",
                    position=position,
                )
                return False
            if self._pending_future is not None and not self._pending_future.done():
                self._record_blackbox(
                    "replan_request_skipped",
                    reason="already_pending",
                    position=position,
                )
                return False
            self._generation += 1
            generation = self._generation
            self._record_blackbox(
                "replan_requested",
                generation=generation,
                position=position,
            )
            future = self._executor.submit(self._build_plan, generation, position)
            self._pending_future = future
            future.add_done_callback(self._store_completed_plan)
            return True

    def take_latest_plan(self) -> AutoDivePlan | None:
        """Return and clear the newest completed plan."""
        with self._lock:
            plan = self._latest_plan
            self._latest_plan = None
            return plan

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _build_plan(
        self,
        generation: int,
        current_position: tuple[float, float, float],
    ) -> tuple[int, AutoDivePlan]:
        self._record_blackbox(
            "replan_build_started",
            generation=generation,
            position=current_position,
        )
        try:
            kwargs: dict[str, Any] = {
                "current_position": current_position,
                "settings": self._settings,
            }
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
        lookahead_seconds: float = 20.0,
        replanner: AutoDiveReplanner | None = None,
        replan_distance_m: float = 0.0,
        blackbox: Any | None = None,
    ) -> None:
        self.plan = plan
        self.perf_counter = perf_counter
        self.lookahead_seconds = max(1.0, float(lookahead_seconds))
        self.replanner = replanner
        self.replan_distance_m = max(0.0, float(replan_distance_m))
        self.blackbox = blackbox
        self.state = AutoDiveState.IDLE
        self._started_at: float | None = None
        self._pause_started_at: float | None = None
        self._paused_seconds = 0.0
        self._elapsed_s = 0.0
        self._last_replan_request_position: np.ndarray | None = None
        self._last_rejected_replan_position: np.ndarray | None = None
        self._last_blackbox_frame_at: float | None = None
        self._stuck_reference_time: float | None = None
        self._stuck_reference_position: np.ndarray | None = None
        self._last_stuck_event_at: float | None = None
        self._prefetch_cells: frozenset[tuple[int, int, int]] = frozenset()
        self._readiness = AutoDiveReadiness(0, 0, 0, 0, 0, 1.0)

    @property
    def active(self) -> bool:
        return self.state in {AutoDiveState.LOADING, AutoDiveState.DIVING}

    @property
    def progress(self) -> float:
        route_progress = route_progress_fraction(self.plan.route, self._elapsed_s)
        if self.state == AutoDiveState.LOADING:
            return self._readiness.progress
        return route_progress

    @property
    def status_note(self) -> str:
        if self.state == AutoDiveState.LOADING:
            return (
                "Loading next passage "
                f"({self._readiness.loaded_cells}/"
                f"{self._readiness.expected_cells} cells)"
            )
        if self.state == AutoDiveState.DIVING:
            return "Diving centerline"
        if self.state == AutoDiveState.COMPLETE:
            return "Auto Dive complete"
        if self.state == AutoDiveState.CANCELLED:
            return "Auto Dive stopped"
        return "Auto Dive ready"

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
        self._last_replan_request_position = np.asarray(
            camera.position,
            dtype=np.float64,
        ).copy()
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
        )

    def stop(self, world=None, *, completed: bool = False) -> None:
        """Stop Auto Dive and clear owned streaming prefetch cells."""
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
            AutoDiveState.COMPLETE,
            AutoDiveState.CANCELLED,
        }:
            return self.state

        self.refresh_prefetch(world)
        self._readiness = self.readiness_for_world(world)
        if not self._readiness.ready:
            if self._pause_started_at is None:
                self._pause_started_at = now
            self.state = AutoDiveState.LOADING
            return self.state

        if self._pause_started_at is not None:
            self._paused_seconds += max(0.0, now - self._pause_started_at)
            self._pause_started_at = None

        if self._started_at is None:
            self._started_at = now
        self._elapsed_s = min(
            self.plan.route.duration_s,
            max(0.0, now - self._started_at - self._paused_seconds),
        )
        apply_pose_to_camera(camera, self.plan.route.pose_at(self._elapsed_s))
        if self._elapsed_s >= self.plan.route.duration_s:
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

        swapped = False
        latest_plan = replanner.take_latest_plan()
        rejected_replan = False
        if latest_plan is not None:
            rejection = self._replan_rejection_payload(
                latest_plan,
                current_position,
            )
            if rejection is None:
                self.plan = latest_plan
                self._started_at = now
                self._pause_started_at = None
                self._paused_seconds = 0.0
                self._elapsed_s = 0.0
                self._last_rejected_replan_position = None
                self.refresh_prefetch(world)
                self._readiness = self.readiness_for_world(world)
                self._record_blackbox(
                    "replan_accepted",
                    camera_position=_vector_payload(current_position),
                    plan=_plan_summary(latest_plan),
                    readiness=_readiness_payload(self._readiness),
                )
                swapped = True
            else:
                self._last_rejected_replan_position = current_position.copy()
                rejected_replan = True
                self._record_blackbox(
                    "replan_rejected",
                    camera_position=_vector_payload(current_position),
                    plan=_plan_summary(latest_plan),
                    **rejection,
                )

        if not rejected_replan and self._should_request_replan(current_position):
            if replanner.request(current_position):
                self._last_replan_request_position = current_position.copy()

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
        alignment = self._plan_forward_alignment(next_point, current_position)
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
    ) -> float | None:
        forward = self._current_route_forward_vector(current_position)
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
            navigation_clamped=bool(navigation_clamped),
        )

    def _update_stuck_detector(
        self,
        position: np.ndarray,
        *,
        now: float,
        navigation_clamped: bool,
    ) -> None:
        if self.state is not AutoDiveState.DIVING or not self._readiness.ready:
            self._stuck_reference_time = None
            self._stuck_reference_position = None
            return
        movement_threshold_m = max(0.05, self.replan_distance_m * 0.5)
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

    def _should_request_replan(self, current_position: np.ndarray) -> bool:
        if self.replan_distance_m <= 0.0:
            return False
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
        radius = max(1, int(self.plan.render_distance_cells))
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
            self._elapsed_s + self.lookahead_seconds,
        )
        sample_count = max(1, int(math.ceil((end_s - start_s) / step_s)))
        for index in range(sample_count + 1):
            t = start_s + (end_s - start_s) * index / max(1, sample_count)
            yield self.plan.route.pose_at(t).position


def _failed_cells_snapshot(world) -> set[tuple[int, int, int]]:
    failed_cells = getattr(world, "_failed_cells", {})
    if isinstance(failed_cells, dict):
        return set(failed_cells)
    return set(failed_cells or ())


def _plan_summary(plan: AutoDivePlan) -> dict[str, Any]:
    return {
        "route_length_m": float(plan.route_length_m),
        "duration_s": float(plan.duration_s),
        "route_point_count": len(plan.route_points),
        "route_cell_count": len(plan.route_cells),
        "render_distance_cells": int(plan.render_distance_cells),
        "circular_arc": bool(plan.circular_arc),
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
