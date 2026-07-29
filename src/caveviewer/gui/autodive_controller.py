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
    AutoDivePlanningBudgetExceeded,
    AutoDivePlan,
    AutoDiveSettings,
    auto_dive_plan_navigation_cell_size,
    build_voxel_graph_auto_dive_plan,
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
DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_FAILURES = 3
DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_FRONTIER_EXPANSIONS = 1
# A continuous scan must finish before the current route reaches its safe
# frontier. The reserve covers owner-thread handoff, route validation, and
# streaming decisions after the worker returns. Keep a full scan from
# starting unless the route has the complete scan budget plus that reserve;
# short horizons use the single authoritative fallback instead.
DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_BUDGET_S = 6.0
DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_HANDOFF_RESERVE_S = 1.0
DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MIN_SAFE_FRONTIER_S = 7.0
DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MIN_BUDGET_S = 0.75
DEFAULT_AUTO_DIVE_MESH_RECOVERY_STANDOFF_CELLS = 2.0
DEFAULT_AUTO_DIVE_MESH_RECOVERY_MIN_STANDOFF_M = 15.0
DEFAULT_AUTO_DIVE_MESH_RECOVERY_MAX_STANDOFF_FRACTION = 0.5
DEFAULT_AUTO_DIVE_ROLLING_CLEARANCE_INTERVAL_SECONDS = 0.35
DEFAULT_AUTO_DIVE_ROLLING_CLEARANCE_PLANNING_MARGIN = 1.25
DEFAULT_AUTO_DIVE_ROLLING_CLEARANCE_MIN_LEAD_M = 15.0
# Speculative replanning uses the same single bounded planner worker as an
# authoritative replan. It starts early enough to keep the next route ready,
# but does not interrupt short route legs that are better handled by their
# explicit frontier/boundary handoff.
DEFAULT_AUTO_DIVE_SPECULATIVE_REPLAN_PLANNING_MARGIN = 1.25
DEFAULT_AUTO_DIVE_SPECULATIVE_REPLAN_MIN_LEAD_SECONDS = 6.0
DEFAULT_AUTO_DIVE_SPECULATIVE_REPLAN_MIN_REMAINING_SECONDS = 2.0
DEFAULT_AUTO_DIVE_SPECULATIVE_REPLAN_INITIAL_GRACE_SECONDS = 1.0
DEFAULT_AUTO_DIVE_VOXEL_PREFETCH_INTERVAL_SECONDS = 1.0
DEFAULT_AUTO_DIVE_VOXEL_PREFETCH_HORIZON_SECONDS = 12.0
DEFAULT_AUTO_DIVE_VOXEL_PREFETCH_MAX_POINTS = 48
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


class AutoDiveContinuousScanOutcome(str, Enum):
    """Typed result of one speculative continuous scan."""

    ROUTE_READY = "route_ready"
    FRONTIER_EXHAUSTED = "frontier_exhausted"
    NO_VALID_ROUTE = "no_valid_route"
    STALE_RESULT = "stale_result"
    DEADLINE_EXCEEDED = "deadline_exceeded"


class AutoDiveContinuousScanDeadlineExceeded(RuntimeError):
    """Raised when a speculative scan cannot finish before its safe handoff."""

    reason = "deadline_exceeded"

    def __init__(self, *, budget_s: float, elapsed_s: float, phase: str) -> None:
        self.budget_s = float(budget_s)
        self.elapsed_s = float(elapsed_s)
        self.phase = str(phase)
        super().__init__(
            "Guided Dive continuous scan deadline exceeded during "
            f"{self.phase} ({self.elapsed_s:.3f}s >= {self.budget_s:.3f}s)"
        )


def _continuous_scan_plan_outcome(
    plan: AutoDivePlan | None,
) -> tuple[
    AutoDiveContinuousScanOutcome,
    tuple[object, ...] | None,
    dict[str, Any],
]:
    """Classify a plan and return the stable identity of its frontier."""
    if plan is None:
        return (
            AutoDiveContinuousScanOutcome.NO_VALID_ROUTE,
            None,
            {"reason": "plan_missing"},
        )
    selection = getattr(plan, "voxel_route_selection", None)
    selection_map = selection if isinstance(selection, Mapping) else {}
    branch = selection_map.get("branch")
    branch_map = branch if isinstance(branch, Mapping) else {}
    graph_snapshot = selection_map.get("graph_snapshot")
    graph_snapshot_map = (
        graph_snapshot if isinstance(graph_snapshot, Mapping) else {}
    )
    graph_keys = selection_map.get("graph_keys")
    normalized_graph_keys: tuple[tuple[int, ...], ...] = ()
    if isinstance(graph_keys, Sequence) and not isinstance(
        graph_keys,
        (str, bytes, bytearray),
    ):
        normalized_keys: list[tuple[int, ...]] = []
        for key in graph_keys[:16]:
            if not isinstance(key, Sequence) or isinstance(
                key,
                (str, bytes, bytearray),
            ):
                continue
            try:
                normalized_keys.append(tuple(int(value) for value in key))
            except (TypeError, ValueError):
                continue
        normalized_graph_keys = tuple(normalized_keys)

    def _key_from(value: object) -> tuple[int, ...] | None:
        if not isinstance(value, Sequence) or isinstance(
            value,
            (str, bytes, bytearray),
        ):
            return None
        try:
            return tuple(int(item) for item in value)
        except (TypeError, ValueError):
            return None

    start_key = _key_from(
        selection_map.get("executed_start_graph_key")
        or selection_map.get("start_key")
        or branch_map.get("branch_start_key")
        or (normalized_graph_keys[0] if normalized_graph_keys else None)
    )
    target_key = _key_from(
        branch_map.get("target_key")
        or selection_map.get("target_key")
        or (normalized_graph_keys[-1] if normalized_graph_keys else None)
    )
    unknown_boundary = bool(
        selection_map.get(
            "unknown_boundary_reached",
            branch_map.get("unknown_boundary", False),
        )
    )
    frontier_count = int(branch_map.get("frontier_count", 0) or 0)
    onward_exit_count = int(branch_map.get("onward_exit_count", 0) or 0)
    terminal_reached = bool(getattr(plan, "terminal_reached", False))
    route_points = getattr(plan, "route_points", ()) or ()
    selection_reason = str(getattr(plan, "selection_reason", ""))
    frontier_expansion = selection_reason == "continuous_local_frontier_expansion"
    if len(route_points) < 2:
        outcome = AutoDiveContinuousScanOutcome.NO_VALID_ROUTE
    elif (
        unknown_boundary
        and frontier_count <= 0
        and onward_exit_count <= 0
        and not terminal_reached
    ):
        outcome = AutoDiveContinuousScanOutcome.FRONTIER_EXHAUSTED
    else:
        outcome = AutoDiveContinuousScanOutcome.ROUTE_READY

    signature: tuple[object, ...] | None = None
    if start_key is not None or target_key is not None or graph_snapshot_map:
        signature = (
            tuple(
                (
                    str(key),
                    _stable_signature_value(graph_snapshot_map.get(key)),
                )
                for key in sorted(graph_snapshot_map)
            ),
            start_key,
            target_key,
            normalized_graph_keys,
            bool(unknown_boundary),
            int(frontier_count),
            int(onward_exit_count),
            bool(terminal_reached),
            tuple(
                tuple(round(float(value), 3) for value in point)
                for point in route_points[:3]
            ),
        )
    if signature is None:
        route_points = tuple(route_points)
        signature = (
            "route_fallback",
            tuple(
                tuple(float(value) for value in point)
                for point in (route_points[:1] + route_points[-1:])
            ),
            round(float(getattr(plan, "route_length_m", 0.0)), 6),
        )
    details = {
        "outcome": outcome.value,
        "graph_snapshot": dict(graph_snapshot_map),
        "start_key": None if start_key is None else list(start_key),
        "target_key": None if target_key is None else list(target_key),
        "graph_keys": [list(key) for key in normalized_graph_keys],
        "frontier_count": int(frontier_count),
        "onward_exit_count": int(onward_exit_count),
        "unknown_boundary": bool(unknown_boundary),
        "terminal_reached": bool(terminal_reached),
        "selection_reason": selection_reason,
        "frontier_expansion": bool(frontier_expansion),
        "route_length_m": float(getattr(plan, "route_length_m", 0.0)),
        "replan_at_end": bool(getattr(plan, "replan_at_end", False)),
    }
    return outcome, signature, details


def _stable_signature_value(value: object) -> object:
    """Convert diagnostic values into deterministic, comparable values."""
    if isinstance(value, Mapping):
        return tuple(
            (str(key), _stable_signature_value(item))
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        )
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return tuple(_stable_signature_value(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


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


@dataclass(frozen=True)
class AutoDiveRollingClearanceReport:
    """Immutable result from one bounded forward-route preflight."""

    plan_sequence: int
    checked_elapsed_s: float
    remaining_distance_m: float
    trigger_distance_m: float
    safe_elapsed_s: float
    safe_to_continue: bool
    needs_replan: bool
    reason: str
    sample_count: int
    maximum_turn_degrees: float
    prepared_branch_count: int
    prepared_goal_clearance_m: float | None
    error: str | None = None
    voxel_sample_count: int = 0
    voxel_covered_count: int = 0
    voxel_uncovered_count: int = 0
    voxel_occupied_count: int = 0
    voxel_min_clearance_m: float | None = None

    def diagnostic_payload(self) -> dict[str, Any]:
        return {
            "plan_sequence": int(self.plan_sequence),
            "checked_elapsed_s": float(self.checked_elapsed_s),
            "remaining_distance_m": float(self.remaining_distance_m),
            "trigger_distance_m": float(self.trigger_distance_m),
            "safe_elapsed_s": float(self.safe_elapsed_s),
            "safe_to_continue": bool(self.safe_to_continue),
            "needs_replan": bool(self.needs_replan),
            "reason": str(self.reason),
            "sample_count": int(self.sample_count),
            "maximum_turn_degrees": float(self.maximum_turn_degrees),
            "prepared_branch_count": int(self.prepared_branch_count),
            "prepared_goal_clearance_m": (
                None
                if self.prepared_goal_clearance_m is None
                else float(self.prepared_goal_clearance_m)
            ),
            "error": self.error,
            "voxel_sample_count": int(self.voxel_sample_count),
            "voxel_covered_count": int(self.voxel_covered_count),
            "voxel_uncovered_count": int(self.voxel_uncovered_count),
            "voxel_occupied_count": int(self.voxel_occupied_count),
            "voxel_min_clearance_m": (
                None
                if self.voxel_min_clearance_m is None
                else float(self.voxel_min_clearance_m)
            ),
        }


class AutoDiveRollingClearanceWorker:
    """Run forward-horizon route checks away from the render thread.

    This worker deliberately performs only bounded, immutable route inspection.
    The exact mesh/voxel route build remains owned by ``AutoDiveReplanner``.
    The two stages complement each other: this worker starts that expensive
    decision before the current safe prefix ends, while the replanner performs
    the authoritative collision and branch validation.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="AutoDiveClearance",
        )
        self._lock = threading.Lock()
        self._pending_future: Future | None = None
        self._pending_context: tuple[int, float, float, float] | None = None
        self._shutdown = False

    def request(
        self,
        plan: AutoDivePlan,
        *,
        plan_sequence: int,
        elapsed_s: float,
        trigger_distance_m: float,
        standoff_distance_m: float,
    ) -> bool:
        with self._lock:
            if self._shutdown:
                return False
            if self._pending_future is not None:
                return False
            self._pending_context = (
                int(plan_sequence),
                float(elapsed_s),
                float(trigger_distance_m),
                float(standoff_distance_m),
            )
            self._pending_future = self._executor.submit(
                _compute_rolling_clearance_report,
                plan,
                int(plan_sequence),
                float(elapsed_s),
                float(trigger_distance_m),
                float(standoff_distance_m),
            )
            return True

    def take_latest_report(self) -> AutoDiveRollingClearanceReport | None:
        with self._lock:
            future = self._pending_future
            if future is None or not future.done():
                return None
            self._pending_future = None
            context = self._pending_context
            self._pending_context = None
        try:
            return future.result()
        except Exception as exc:  # pragma: no cover - defensive worker seam
            plan_sequence = -1 if context is None else context[0]
            elapsed_s = 0.0 if context is None else context[1]
            trigger_distance_m = 0.0 if context is None else context[2]
            return AutoDiveRollingClearanceReport(
                plan_sequence=plan_sequence,
                checked_elapsed_s=elapsed_s,
                remaining_distance_m=0.0,
                trigger_distance_m=trigger_distance_m,
                safe_elapsed_s=0.0,
                safe_to_continue=False,
                needs_replan=True,
                reason="worker_error",
                sample_count=0,
                maximum_turn_degrees=0.0,
                prepared_branch_count=0,
                prepared_goal_clearance_m=None,
                error=f"{type(exc).__name__}: {exc}",
                voxel_sample_count=0,
            )

    def has_pending(self) -> bool:
        with self._lock:
            return bool(
                self._pending_future is not None
                and not self._pending_future.done()
            )

    def poll(self) -> AutoDiveRollingClearanceReport | None:
        return self.take_latest_report()

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._executor.shutdown(wait=False, cancel_futures=True)


@dataclass(frozen=True)
class AutoDiveVoxelPrefetchReport:
    """Bounded result from one predicted navigation-voxel prefetch."""

    plan_sequence: int
    elapsed_s: float
    horizon_s: float
    reason: str
    outcome: str
    point_count: int
    requested_chunk_count: int
    resident_chunk_count: int
    resident_chunk_ids: tuple[str, ...]
    storage_backend: str | None
    storage_stats: Mapping[str, object] | None
    duration_ms: float
    error: str | None = None

    def diagnostic_payload(self) -> dict[str, Any]:
        return {
            "plan_sequence": int(self.plan_sequence),
            "elapsed_s": float(self.elapsed_s),
            "horizon_s": float(self.horizon_s),
            "reason": str(self.reason),
            "outcome": str(self.outcome),
            "point_count": int(self.point_count),
            "requested_chunk_count": int(self.requested_chunk_count),
            "resident_chunk_count": int(self.resident_chunk_count),
            "resident_chunk_ids": [
                str(chunk_id) for chunk_id in self.resident_chunk_ids[:16]
            ],
            "resident_chunk_ids_truncated": len(self.resident_chunk_ids) > 16,
            "storage_backend": self.storage_backend,
            "storage_stats": (
                None
                if self.storage_stats is None
                else dict(self.storage_stats)
            ),
            "duration_ms": float(self.duration_ms),
            "error": self.error,
        }


class AutoDiveVoxelPrefetchWorker:
    """Materialize a bounded future voxel horizon off the navigation thread."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="AutoDiveVoxelPrefetch",
        )
        self._lock = threading.Lock()
        self._pending_future: Future | None = None
        self._pending_context: tuple[int, float, float, str] | None = None
        self._shutdown = False

    def request(
        self,
        plan: AutoDivePlan,
        *,
        plan_sequence: int,
        elapsed_s: float,
        reason: str,
        horizon_s: float = DEFAULT_AUTO_DIVE_VOXEL_PREFETCH_HORIZON_SECONDS,
        max_points: int = DEFAULT_AUTO_DIVE_VOXEL_PREFETCH_MAX_POINTS,
    ) -> bool:
        """Queue one bounded future-horizon materialization."""
        elapsed = max(0.0, float(elapsed_s))
        horizon = max(0.0, float(horizon_s))
        points = _predicted_voxel_prefetch_points(
            plan,
            elapsed_s=elapsed,
            horizon_s=horizon,
            max_points=max_points,
        )
        context = (int(plan_sequence), elapsed, horizon, str(reason))
        with self._lock:
            if self._shutdown:
                return False
            if self._pending_future is not None and not self._pending_future.done():
                return False
            self._pending_context = context
            self._pending_future = self._executor.submit(
                _compute_voxel_prefetch_report,
                plan,
                plan_sequence=int(plan_sequence),
                elapsed_s=elapsed,
                horizon_s=horizon,
                reason=str(reason),
                points=points,
            )
            return True

    def take_latest_report(self) -> AutoDiveVoxelPrefetchReport | None:
        """Return one completed report without blocking the owner thread."""
        with self._lock:
            future = self._pending_future
            if future is None or not future.done():
                return None
            self._pending_future = None
            context = self._pending_context
            self._pending_context = None
        try:
            return future.result()
        except Exception as exc:  # pragma: no cover - defensive worker seam
            plan_sequence, elapsed_s, horizon_s, reason = context or (
                -1,
                0.0,
                0.0,
                "unknown",
            )
            return AutoDiveVoxelPrefetchReport(
                plan_sequence=plan_sequence,
                elapsed_s=elapsed_s,
                horizon_s=horizon_s,
                reason=reason,
                outcome="worker_error",
                point_count=0,
                requested_chunk_count=0,
                resident_chunk_count=0,
                resident_chunk_ids=(),
                storage_backend=None,
                storage_stats=None,
                duration_ms=0.0,
                error=f"{type(exc).__name__}: {exc}",
            )

    def has_pending(self) -> bool:
        with self._lock:
            return bool(
                self._pending_future is not None
                and not self._pending_future.done()
            )

    def cancel_pending(self) -> None:
        with self._lock:
            future = self._pending_future
            self._pending_future = None
            self._pending_context = None
        if future is not None:
            future.cancel()

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            future = self._pending_future
            self._pending_future = None
            self._pending_context = None
        if future is not None:
            future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)


def _predicted_voxel_prefetch_points(
    plan: AutoDivePlan,
    *,
    elapsed_s: float,
    horizon_s: float,
    max_points: int,
) -> tuple[tuple[float, float, float], ...]:
    """Sample the route horizon at bounded spacing for chunk prediction."""
    route = getattr(plan, "route", None)
    pose_at = getattr(route, "pose_at", None)
    duration = max(0.0, float(getattr(route, "duration_s", 0.0)))
    start = max(0.0, min(duration, float(elapsed_s)))
    end = min(duration, start + max(0.0, float(horizon_s)))
    if callable(pose_at) and duration > 1e-9:
        route_length = max(0.0, float(getattr(plan, "route_length_m", 0.0)))
        speed = route_length / duration
        estimated_distance = max(0.0, (end - start) * speed)
        sample_count = max(
            2 if end > start + 1e-9 else 1,
            min(
                max(1, int(max_points)),
                int(math.ceil(estimated_distance / 4.0)) + 1,
            ),
        )
        points: list[tuple[float, float, float]] = []
        for index in range(sample_count):
            sample_time = start + (end - start) * index / max(1, sample_count - 1)
            try:
                position = pose_at(sample_time).position
                point = tuple(float(value) for value in position)
            except (AttributeError, TypeError, ValueError):
                continue
            if len(point) == 3 and all(math.isfinite(value) for value in point):
                points.append(point)
        if points:
            return tuple(dict.fromkeys(points))
    fallback = getattr(plan, "route_points", ())
    points = []
    for value in fallback:
        try:
            point = tuple(float(item) for item in value)
        except (TypeError, ValueError):
            continue
        if len(point) == 3 and all(math.isfinite(item) for item in point):
            points.append(point)
    return tuple(dict.fromkeys(points[: max(1, int(max_points))]))


def _compute_voxel_prefetch_report(
    plan: AutoDivePlan,
    *,
    plan_sequence: int,
    elapsed_s: float,
    horizon_s: float,
    reason: str,
    points: tuple[tuple[float, float, float], ...],
) -> AutoDiveVoxelPrefetchReport:
    """Load predicted chunks and release chunks outside the current horizon."""
    started_at = time.perf_counter()
    volume = getattr(plan, "navigation_atlas", None)
    if volume is None:
        volume = getattr(
            getattr(plan, "centerline_path", None),
            "cached_voxel_volume",
            None,
        )
    prefetch = getattr(volume, "prefetch_for_points", None)
    store = getattr(volume, "chunk_store", None)
    if not callable(prefetch) or store is None:
        return AutoDiveVoxelPrefetchReport(
            plan_sequence=int(plan_sequence),
            elapsed_s=float(elapsed_s),
            horizon_s=float(horizon_s),
            reason=str(reason),
            outcome="no_navigation_chunk_store",
            point_count=len(points),
            requested_chunk_count=0,
            resident_chunk_count=0,
            resident_chunk_ids=(),
            storage_backend=None,
            storage_stats=None,
            duration_ms=(time.perf_counter() - started_at) * 1000.0,
        )

    requested_ids: tuple[str, ...] = ()
    requested: list[str] = []
    for point in points:
        requested.extend(store.chunk_ids_for_point(point))
    requested_ids = tuple(dict.fromkeys(requested))
    try:
        prefetch(points)
        resident_before_release = tuple(store.resident_chunk_ids())
        # Keep the complete requested horizon, including chunks loaded by the
        # prefetch call itself. Passing only the previously resident subset
        # would immediately evict newly materialized route chunks.
        del resident_before_release
        store.release_unused(requested_ids)
        resident_ids = tuple(store.resident_chunk_ids())
        stats = store.stats()
        backend = stats.get("backend")
        backend_name = None if backend is None else str(backend)
    except Exception as exc:
        return AutoDiveVoxelPrefetchReport(
            plan_sequence=int(plan_sequence),
            elapsed_s=float(elapsed_s),
            horizon_s=float(horizon_s),
            reason=str(reason),
            outcome="prefetch_error",
            point_count=len(points),
            requested_chunk_count=len(requested_ids),
            resident_chunk_count=0,
            resident_chunk_ids=(),
            storage_backend=None,
            storage_stats=None,
            duration_ms=(time.perf_counter() - started_at) * 1000.0,
            error=f"{type(exc).__name__}: {exc}",
        )
    outcome = (
        "prefetched"
        if not requested_ids or set(requested_ids) <= set(resident_ids)
        else "partially_prefetched"
    )
    return AutoDiveVoxelPrefetchReport(
        plan_sequence=int(plan_sequence),
        elapsed_s=float(elapsed_s),
        horizon_s=float(horizon_s),
        reason=str(reason),
        outcome=outcome,
        point_count=len(points),
        requested_chunk_count=len(requested_ids),
        resident_chunk_count=len(resident_ids),
        resident_chunk_ids=resident_ids,
        storage_backend=backend_name,
        storage_stats=stats,
        duration_ms=(time.perf_counter() - started_at) * 1000.0,
    )


def _compute_rolling_clearance_report(
    plan: AutoDivePlan,
    plan_sequence: int,
    elapsed_s: float,
    trigger_distance_m: float,
    standoff_distance_m: float,
) -> AutoDiveRollingClearanceReport:
    """Inspect a bounded route horizon without touching camera/world state."""
    duration_s = max(0.0, float(getattr(plan.route, "duration_s", 0.0)))
    route_length_m = max(0.0, float(getattr(plan, "route_length_m", 0.0)))
    elapsed = max(0.0, min(duration_s, float(elapsed_s)))
    speed = route_length_m / max(1e-9, duration_s)
    remaining_distance = max(0.0, (duration_s - elapsed) * speed)
    trigger_distance = max(0.0, float(trigger_distance_m))
    standoff_distance = max(0.0, min(route_length_m, float(standoff_distance_m)))
    safe_remaining = min(remaining_distance, standoff_distance)
    safe_elapsed = max(0.0, duration_s - safe_remaining / max(1e-9, speed))

    sample_distance = max(0.5, min(trigger_distance, route_length_m))
    horizon_s = min(
        max(0.0, duration_s - elapsed),
        sample_distance / max(1e-9, speed),
    )
    sample_count = max(2, min(16, int(math.ceil(sample_distance / 5.0)) + 1))
    sample_times = [
        elapsed + horizon_s * index / max(1, sample_count - 1)
        for index in range(sample_count)
    ]
    sample_positions = [
        np.asarray(plan.route.pose_at(sample_time).position, dtype=np.float64)
        for sample_time in sample_times
    ]
    voxel_sample_count = 0
    voxel_covered_count = 0
    voxel_uncovered_count = 0
    voxel_occupied_count = 0
    voxel_clearances: list[float] = []
    voxel_volume = getattr(plan, "navigation_atlas", None)
    if voxel_volume is None:
        centerline_path = getattr(plan, "centerline_path", None)
        voxel_volume = getattr(centerline_path, "cached_voxel_volume", None)
    probe_point = getattr(voxel_volume, "probe_point", None)
    if callable(probe_point):
        for point in sample_positions:
            voxel_sample_count += 1
            try:
                probe = probe_point(point, include_clearance=True)
            except Exception:
                probe = None
            if probe is None:
                voxel_uncovered_count += 1
                continue
            is_free, clearance_m = probe
            voxel_covered_count += 1
            if bool(is_free):
                voxel_clearances.append(max(0.0, float(clearance_m)))
            else:
                voxel_occupied_count += 1
    maximum_turn_degrees = 0.0
    previous_direction: np.ndarray | None = None
    for first, second in zip(sample_positions, sample_positions[1:], strict=False):
        direction = second - first
        norm = float(np.linalg.norm(direction))
        if norm <= 1e-9:
            continue
        direction /= norm
        if previous_direction is not None:
            dot = max(-1.0, min(1.0, float(np.dot(previous_direction, direction))))
            maximum_turn_degrees = max(
                maximum_turn_degrees,
                math.degrees(math.acos(dot)),
            )
        previous_direction = direction

    route_selection = getattr(plan, "voxel_route_selection", None)
    if not isinstance(route_selection, Mapping):
        route_selection = {}
    raw_candidates = route_selection.get("branch_candidates")
    prepared_branch_count = (
        len(raw_candidates)
        if isinstance(raw_candidates, Sequence)
        and not isinstance(raw_candidates, (str, bytes))
        else 0
    )
    prepared_goal_clearance = route_selection.get("goal_clearance_m")
    try:
        prepared_goal_clearance_m = (
            None
            if prepared_goal_clearance is None
            else float(prepared_goal_clearance)
        )
    except (TypeError, ValueError):
        prepared_goal_clearance_m = None

    boundary_plan = bool(getattr(plan, "replan_at_end", False))
    terminal_plan = bool(getattr(plan, "terminal_reached", False))
    voxel_blocked = voxel_occupied_count > 0
    needs_replan = (
        not terminal_plan
        and (
            voxel_blocked
            or (boundary_plan and remaining_distance <= trigger_distance)
        )
    )
    if terminal_plan:
        reason = "terminal_route"
    elif voxel_blocked:
        reason = "voxel_forward_clearance_blocked"
    elif not boundary_plan:
        reason = "no_rolling_boundary"
    elif needs_replan:
        reason = "approaching_forward_clearance_boundary"
    else:
        reason = "forward_clearance_horizon_clear"
    return AutoDiveRollingClearanceReport(
        plan_sequence=int(plan_sequence),
        checked_elapsed_s=float(elapsed),
        remaining_distance_m=float(remaining_distance),
        trigger_distance_m=float(trigger_distance),
        safe_elapsed_s=float(safe_elapsed),
        safe_to_continue=bool(
            not voxel_blocked
            and (not needs_replan or elapsed < safe_elapsed - 1e-6)
        ),
        needs_replan=bool(needs_replan),
        reason=reason,
        sample_count=len(sample_positions),
        maximum_turn_degrees=float(maximum_turn_degrees),
        prepared_branch_count=int(prepared_branch_count),
        prepared_goal_clearance_m=prepared_goal_clearance_m,
        voxel_sample_count=int(voxel_sample_count),
        voxel_covered_count=int(voxel_covered_count),
        voxel_uncovered_count=int(voxel_uncovered_count),
        voxel_occupied_count=int(voxel_occupied_count),
        voxel_min_clearance_m=(
            None if not voxel_clearances else min(voxel_clearances)
        ),
    )


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
        plan_builder: Callable[..., AutoDivePlan] = build_voxel_graph_auto_dive_plan,
        cache_dir: str | None = None,
        blackbox: Any | None = None,
        navigation_route_id: str | None = None,
        perf_counter: Callable[[], float] | None = None,
        planning_budget_s: float = DEFAULT_AUTO_DIVE_REPLAN_PLANNING_BUDGET_S,
    ) -> None:
        self._manifest = manifest
        self._settings = settings
        self._plan_builder = plan_builder
        self._cache_dir = cache_dir
        self._blackbox = blackbox
        self._navigation_route_id = navigation_route_id
        self._perf_counter = perf_counter or time.perf_counter
        budget = float(planning_budget_s)
        if not math.isfinite(budget) or budget <= 0.0:
            raise ValueError("replan planning budget must be positive")
        self._planning_budget_s = budget
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="AutoDiveReplanner",
        )
        # Continuous recovery scanning is intentionally independent from the
        # authoritative replan worker. The owner supplies a safe-frontier
        # deadline for each scan, so an expensive scan cannot consume the
        # authoritative replan handoff budget or outlive the validated route.
        self._continuous_scan_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="AutoDiveContinuousScan",
        )
        self._lock = threading.Lock()
        self._generation = 0
        self._latest_generation = 0
        self._latest_plan: AutoDivePlan | None = None
        self._latest_plan_generation: int | None = None
        self._latest_plan_source_sequence: int | None = None
        self._last_taken_plan_generation: int | None = None
        self._last_taken_plan_source_sequence: int | None = None
        self._pending_future: Future | None = None
        self._pending_generation: int | None = None
        self._generation_source_sequences: dict[int, int | None] = {}
        self._shutdown = False
        self._voxel_prefetch_worker = AutoDiveVoxelPrefetchWorker()
        self._continuous_scan_generation = 0
        self._continuous_scan_latest_generation = 0
        self._continuous_scan_pending_future: Future | None = None
        self._continuous_scan_pending_generation: int | None = None
        self._continuous_scan_generation_sources: dict[int, int | None] = {}
        self._latest_continuous_scan_plan: AutoDivePlan | None = None
        self._latest_continuous_scan_generation: int | None = None
        self._latest_continuous_scan_source_sequence: int | None = None
        self._latest_continuous_scan_outcome: AutoDiveContinuousScanOutcome | None = None
        self._last_taken_continuous_scan_generation: int | None = None
        self._last_taken_continuous_scan_source_sequence: int | None = None
        self._last_taken_continuous_scan_outcome: AutoDiveContinuousScanOutcome | None = None
        self._continuous_scan_failure_count = 0
        self._continuous_scan_last_failure_generation: int | None = None
        self._continuous_scan_last_failure_reason: str | None = None
        self._continuous_scan_last_failure_outcome: AutoDiveContinuousScanOutcome | None = None

    def request(
        self,
        current_position: np.ndarray | tuple[float, float, float],
        *,
        current_yaw: float | None = None,
        current_pitch: float | None = None,
        current_roll: float | None = None,
        current_travel_yaw: float | None = None,
        current_travel_pitch: float | None = None,
        avoid_positions: Sequence[Sequence[float]] | None = None,
        user_reposition: bool = False,
        force_hemisphere_scan: bool = False,
        source_plan_sequence: int | None = None,
        speculative_replan: bool = False,
    ) -> bool:
        """Queue one replan from the current camera position if none is pending."""
        request_started_at = self._perf_counter()
        position = tuple(
            float(value)
            for value in np.asarray(current_position, dtype=np.float64).reshape(3)
        )
        yaw = None if current_yaw is None else float(current_yaw)
        pitch = None if current_pitch is None else float(current_pitch)
        roll = None if current_roll is None else float(current_roll)
        travel_yaw = (
            None if current_travel_yaw is None else float(current_travel_yaw)
        )
        travel_pitch = (
            None if current_travel_pitch is None else float(current_travel_pitch)
        )
        avoided = _normalized_avoid_positions(avoid_positions)
        try:
            source_sequence = (
                None
                if source_plan_sequence is None
                else int(source_plan_sequence)
            )
        except (TypeError, ValueError):
            source_sequence = None
        with self._lock:
            if self._shutdown:
                self._record_blackbox(
                    "replan_request_skipped",
                    replan_id=None,
                    reason="shutdown",
                    position=position,
                    yaw=yaw,
                    pitch=pitch,
                    roll=roll,
                    travel_yaw=travel_yaw,
                    travel_pitch=travel_pitch,
                    avoid_positions=avoided,
                    user_reposition=bool(user_reposition),
                    force_hemisphere_scan=bool(force_hemisphere_scan),
                    source_plan_sequence=source_sequence,
                    speculative_replan=bool(speculative_replan),
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
                    roll=roll,
                    travel_yaw=travel_yaw,
                    travel_pitch=travel_pitch,
                    avoid_positions=avoided,
                    user_reposition=bool(user_reposition),
                    force_hemisphere_scan=bool(force_hemisphere_scan),
                    source_plan_sequence=source_sequence,
                    speculative_replan=bool(speculative_replan),
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
                roll=roll,
                travel_yaw=travel_yaw,
                travel_pitch=travel_pitch,
                avoid_positions=avoided,
                user_reposition=bool(user_reposition),
                force_hemisphere_scan=bool(force_hemisphere_scan),
                source_plan_sequence=source_sequence,
                speculative_replan=bool(speculative_replan),
                planning_budget_s=float(self._planning_budget_s),
            )
            self._generation_source_sequences[generation] = source_sequence
            future = self._executor.submit(
                self._build_plan,
                generation,
                position,
                yaw,
                pitch,
                roll,
                travel_yaw,
                travel_pitch,
                avoided,
                bool(user_reposition),
                bool(force_hemisphere_scan),
                request_started_at,
                source_sequence,
                bool(speculative_replan),
            )
            self._pending_future = future
            self._pending_generation = generation
        # Register outside the replanner lock. ``Future.add_done_callback``
        # invokes immediately when a tiny plan already completed; registering
        # while holding the lock would make the callback wait on itself.
        future.add_done_callback(self._store_completed_plan)
        return True

    def request_speculative(
        self,
        current_position: np.ndarray | tuple[float, float, float],
        *,
        source_plan_sequence: int,
        current_yaw: float | None = None,
        current_pitch: float | None = None,
        current_roll: float | None = None,
        current_travel_yaw: float | None = None,
        current_travel_pitch: float | None = None,
        avoid_positions: Sequence[Sequence[float]] | None = None,
    ) -> bool:
        """Queue one ahead-of-frontier plan without taking ownership of it.

        The controller keeps the currently accepted route active while this
        request runs. ``source_plan_sequence`` lets the owner discard a
        result that belongs to an older route after a concurrent handoff.
        """
        return self.request(
            current_position,
            current_yaw=current_yaw,
            current_pitch=current_pitch,
            current_roll=current_roll,
            current_travel_yaw=current_travel_yaw,
            current_travel_pitch=current_travel_pitch,
            avoid_positions=avoid_positions,
            source_plan_sequence=source_plan_sequence,
            speculative_replan=True,
        )

    def request_continuous_scan(
        self,
        current_position: np.ndarray | tuple[float, float, float],
        *,
        current_yaw: float | None = None,
        current_pitch: float | None = None,
        current_roll: float | None = None,
        current_travel_yaw: float | None = None,
        current_travel_pitch: float | None = None,
        avoid_positions: Sequence[Sequence[float]] | None = None,
        source_plan_sequence: int | None = None,
        expand_frontier: bool = False,
        scan_budget_s: float | None = None,
    ) -> bool:
        """Keep one bounded speculative forward scan running.

        The scan is advisory until the owner thread accepts its immutable
        plan. It is deliberately separate from ``request()`` so the scan can
        use its own safe-frontier deadline without consuming the authoritative
        replan handoff budget.
        """
        request_started_at = self._perf_counter()
        if scan_budget_s is None:
            scan_budget = DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_BUDGET_S
        else:
            try:
                scan_budget = float(scan_budget_s)
            except (TypeError, ValueError):
                return False
        if not math.isfinite(scan_budget) or scan_budget <= 0.0:
            return False
        position = tuple(
            float(value)
            for value in np.asarray(current_position, dtype=np.float64).reshape(3)
        )
        yaw = None if current_yaw is None else float(current_yaw)
        pitch = None if current_pitch is None else float(current_pitch)
        roll = None if current_roll is None else float(current_roll)
        travel_yaw = (
            None if current_travel_yaw is None else float(current_travel_yaw)
        )
        travel_pitch = (
            None if current_travel_pitch is None else float(current_travel_pitch)
        )
        avoided = _normalized_avoid_positions(avoid_positions)
        try:
            source_sequence = (
                None
                if source_plan_sequence is None
                else int(source_plan_sequence)
            )
        except (TypeError, ValueError):
            source_sequence = None
        with self._lock:
            if self._shutdown:
                return False
            if (
                self._continuous_scan_pending_future is not None
                or self._latest_continuous_scan_plan is not None
            ):
                return False
            self._continuous_scan_generation += 1
            generation = self._continuous_scan_generation
            scan_id = f"continuous-scan-{generation}"
            self._record_blackbox(
                "continuous_scan_requested",
                scan_id=scan_id,
                scan_generation=generation,
                position=position,
                yaw=yaw,
                pitch=pitch,
                roll=roll,
                travel_yaw=travel_yaw,
                travel_pitch=travel_pitch,
                avoid_positions=avoided,
                source_plan_sequence=source_sequence,
                planning_budget_s=float(scan_budget),
                scan_budget_s=float(scan_budget),
                expand_frontier=bool(expand_frontier),
            )
            self._continuous_scan_generation_sources[generation] = source_sequence
            future = self._continuous_scan_executor.submit(
                self._build_continuous_scan_plan,
                generation,
                position,
                yaw,
                pitch,
                roll,
                travel_yaw,
                travel_pitch,
                avoided,
                source_sequence,
                request_started_at,
                bool(expand_frontier),
                float(scan_budget),
            )
            self._continuous_scan_pending_future = future
            self._continuous_scan_pending_generation = generation
        future.add_done_callback(self._store_completed_continuous_scan)
        return True

    def take_latest_continuous_scan(self) -> AutoDivePlan | None:
        """Return one completed speculative scan plan, if available."""
        with self._lock:
            plan = self._latest_continuous_scan_plan
            self._last_taken_continuous_scan_generation = (
                self._latest_continuous_scan_generation
            )
            self._last_taken_continuous_scan_source_sequence = (
                self._latest_continuous_scan_source_sequence
            )
            self._last_taken_continuous_scan_outcome = (
                self._latest_continuous_scan_outcome
            )
            self._latest_continuous_scan_plan = None
            self._latest_continuous_scan_generation = None
            self._latest_continuous_scan_source_sequence = None
            self._latest_continuous_scan_outcome = None
            return plan

    @property
    def last_taken_continuous_scan_generation(self) -> int | None:
        with self._lock:
            return self._last_taken_continuous_scan_generation

    @property
    def last_taken_continuous_scan_source_sequence(self) -> int | None:
        with self._lock:
            return self._last_taken_continuous_scan_source_sequence

    @property
    def last_taken_continuous_scan_outcome(
        self,
    ) -> AutoDiveContinuousScanOutcome | None:
        with self._lock:
            return self._last_taken_continuous_scan_outcome

    def has_continuous_scan_pending(self) -> bool:
        with self._lock:
            return self._continuous_scan_pending_future is not None

    def has_continuous_scan_result(self) -> bool:
        with self._lock:
            return self._latest_continuous_scan_plan is not None

    @property
    def continuous_scan_failure_count(self) -> int:
        """Return consecutive continuous-scan build failures."""
        with self._lock:
            return int(self._continuous_scan_failure_count)

    @property
    def continuous_scan_last_failure_generation(self) -> int | None:
        """Return the generation of the latest failed continuous scan."""
        with self._lock:
            return self._continuous_scan_last_failure_generation

    @property
    def continuous_scan_last_failure_reason(self) -> str | None:
        """Return the reason for the latest failed continuous scan."""
        with self._lock:
            return self._continuous_scan_last_failure_reason

    @property
    def continuous_scan_last_failure_outcome(
        self,
    ) -> AutoDiveContinuousScanOutcome | None:
        """Return the typed outcome of the latest failed scan, if any."""
        with self._lock:
            return self._continuous_scan_last_failure_outcome

    def take_latest_plan(self) -> AutoDivePlan | None:
        """Return and clear the newest completed plan."""
        with self._lock:
            plan = self._latest_plan
            self._last_taken_plan_generation = self._latest_plan_generation
            self._last_taken_plan_source_sequence = (
                self._latest_plan_source_sequence
            )
            self._latest_plan = None
            self._latest_plan_generation = None
            self._latest_plan_source_sequence = None
            return plan

    @property
    def last_taken_plan_generation(self) -> int | None:
        """Return the generation associated with the last taken plan."""
        with self._lock:
            return self._last_taken_plan_generation

    @property
    def last_taken_plan_source_sequence(self) -> int | None:
        """Return the accepted-route sequence a completed plan was based on."""
        with self._lock:
            return self._last_taken_plan_source_sequence

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

    def request_voxel_prefetch(
        self,
        plan: AutoDivePlan,
        *,
        plan_sequence: int,
        elapsed_s: float,
        reason: str,
        horizon_s: float = DEFAULT_AUTO_DIVE_VOXEL_PREFETCH_HORIZON_SECONDS,
    ) -> bool:
        """Queue predicted navigation chunks without blocking route planning."""
        return self._voxel_prefetch_worker.request(
            plan,
            plan_sequence=plan_sequence,
            elapsed_s=elapsed_s,
            reason=reason,
            horizon_s=horizon_s,
        )

    def take_voxel_prefetch_report(self) -> AutoDiveVoxelPrefetchReport | None:
        """Return one completed navigation-voxel prefetch report."""
        return self._voxel_prefetch_worker.take_latest_report()

    def has_voxel_prefetch_pending(self) -> bool:
        """Return whether predicted voxel materialization is still running."""
        return self._voxel_prefetch_worker.has_pending()

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._voxel_prefetch_worker.shutdown()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._continuous_scan_executor.shutdown(wait=False, cancel_futures=True)

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
            self._pending_generation = None
            self._generation_source_sequences.clear()
            self._latest_plan = None
            self._latest_plan_generation = None
            self._latest_plan_source_sequence = None
        if future is not None:
            future.cancel()
        self._voxel_prefetch_worker.cancel_pending()

    def cancel_continuous_scan(self) -> None:
        """Discard a stale scan result without cancelling authoritative work."""
        with self._lock:
            if self._shutdown:
                return
            self._continuous_scan_generation += 1
            invalidation_generation = self._continuous_scan_generation
            self._continuous_scan_latest_generation = invalidation_generation
            future = self._continuous_scan_pending_future
            self._continuous_scan_pending_future = None
            self._continuous_scan_pending_generation = None
            self._continuous_scan_generation_sources.clear()
            self._latest_continuous_scan_plan = None
            self._latest_continuous_scan_generation = None
            self._latest_continuous_scan_source_sequence = None
            self._latest_continuous_scan_outcome = None
            self._continuous_scan_failure_count = 0
            self._continuous_scan_last_failure_generation = None
            self._continuous_scan_last_failure_reason = None
            self._continuous_scan_last_failure_outcome = None
        if future is not None:
            future.cancel()

    def _build_continuous_scan_plan(
        self,
        generation: int,
        current_position: tuple[float, float, float],
        current_yaw: float | None,
        current_pitch: float | None,
        current_roll: float | None,
        current_travel_yaw: float | None,
        current_travel_pitch: float | None,
        avoid_positions: tuple[tuple[float, float, float], ...],
        source_plan_sequence: int | None,
        request_started_at: float,
        expand_frontier: bool = False,
        scan_budget_s: float = DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_BUDGET_S,
    ) -> tuple[int, int | None, AutoDivePlan]:
        build_started_at = self._perf_counter()
        scan_id = f"continuous-scan-{generation}"
        queue_elapsed_s = max(0.0, build_started_at - request_started_at)
        remaining_budget_s = float(scan_budget_s) - queue_elapsed_s
        self._record_blackbox(
            "continuous_scan_build_started",
            scan_id=scan_id,
            scan_generation=generation,
            position=current_position,
            source_plan_sequence=source_plan_sequence,
            planning_budget_s=float(max(0.0, remaining_budget_s)),
            scan_budget_s=float(scan_budget_s),
            expand_frontier=bool(expand_frontier),
            queue_duration_ms=max(
                0.0,
                queue_elapsed_s * 1000.0,
            ),
        )
        try:
            if remaining_budget_s <= 0.0:
                raise AutoDiveContinuousScanDeadlineExceeded(
                    budget_s=float(scan_budget_s),
                    elapsed_s=float(queue_elapsed_s),
                    phase="worker_queue",
                )
            kwargs: dict[str, Any] = {
                "current_position": current_position,
                "settings": replace(
                    self._settings,
                    planning_budget_s=float(remaining_budget_s),
                ),
                "force_hemisphere_scan": True,
            }
            if expand_frontier:
                kwargs["expand_frontier"] = True
            if current_yaw is not None:
                kwargs["current_yaw"] = current_yaw
            if current_pitch is not None:
                kwargs["current_pitch"] = current_pitch
            if current_roll is not None:
                kwargs["current_roll"] = current_roll
            if current_travel_yaw is not None:
                kwargs["current_travel_yaw"] = current_travel_yaw
            if current_travel_pitch is not None:
                kwargs["current_travel_pitch"] = current_travel_pitch
            if avoid_positions:
                kwargs["avoid_positions"] = avoid_positions
            if self._cache_dir is not None:
                kwargs["cache_dir"] = self._cache_dir
            if self._navigation_route_id is not None:
                kwargs["route_id"] = self._navigation_route_id
            if self._blackbox is not None:
                def record_diagnostic(event: str, payload: Mapping[str, Any]) -> None:
                    enriched = dict(payload)
                    enriched.setdefault("scan_id", scan_id)
                    enriched.setdefault("continuous_scan_generation", generation)
                    enriched.setdefault("source_plan_sequence", source_plan_sequence)
                    enriched.setdefault("continuous_scan", True)
                    self._record_blackbox(event, **enriched)

                kwargs["diagnostics"] = record_diagnostic
            plan = self._plan_builder(self._manifest, **kwargs)
            completed_at = self._perf_counter()
            total_elapsed_s = max(0.0, completed_at - request_started_at)
            if total_elapsed_s >= float(scan_budget_s):
                raise AutoDiveContinuousScanDeadlineExceeded(
                    budget_s=float(scan_budget_s),
                    elapsed_s=float(total_elapsed_s),
                    phase="route_publication",
                )
        except Exception as exc:
            now = self._perf_counter()
            deadline_exceeded = isinstance(
                exc,
                (
                    AutoDivePlanningBudgetExceeded,
                    AutoDiveContinuousScanDeadlineExceeded,
                ),
            )
            failure = (
                exc
                if isinstance(exc, AutoDiveContinuousScanDeadlineExceeded)
                else AutoDiveContinuousScanDeadlineExceeded(
                    budget_s=float(scan_budget_s),
                    elapsed_s=max(0.0, now - request_started_at),
                    phase=str(getattr(exc, "phase", "planner")),
                )
            )
            self._record_blackbox(
                "continuous_scan_failed",
                scan_id=scan_id,
                scan_generation=generation,
                position=current_position,
                source_plan_sequence=source_plan_sequence,
                error_type=type(exc).__name__,
                error=str(exc),
                scan_outcome=(
                    AutoDiveContinuousScanOutcome.DEADLINE_EXCEEDED.value
                    if deadline_exceeded
                    else AutoDiveContinuousScanOutcome.NO_VALID_ROUTE.value
                ),
                failure_reason=(
                    "deadline_exceeded"
                    if deadline_exceeded
                    else str(getattr(exc, "reason", type(exc).__name__))
                ),
                planning_budget_s=float(scan_budget_s),
                elapsed_s=max(0.0, now - request_started_at),
                deadline_phase=(
                    str(getattr(failure, "phase", "planner"))
                    if deadline_exceeded
                    else None
                ),
                expand_frontier=bool(expand_frontier),
                build_duration_ms=max(
                    0.0,
                    (now - build_started_at) * 1000.0,
                ),
                total_duration_ms=max(
                    0.0,
                    (now - request_started_at) * 1000.0,
                ),
            )
            if isinstance(exc, AutoDivePlanningBudgetExceeded):
                raise failure from exc
            raise
        completed_at = self._perf_counter()
        scan_outcome, frontier_signature, frontier_details = (
            _continuous_scan_plan_outcome(plan)
        )
        self._record_blackbox(
            "continuous_scan_completed",
            scan_id=scan_id,
            scan_generation=generation,
            position=current_position,
            source_plan_sequence=source_plan_sequence,
            plan=_plan_summary(plan),
            scan_outcome=scan_outcome.value,
            frontier_signature=frontier_signature,
            frontier_details=frontier_details,
            expand_frontier=bool(expand_frontier),
            planning_budget_s=float(scan_budget_s),
            scan_budget_s=float(scan_budget_s),
            build_duration_ms=max(
                0.0,
                (completed_at - build_started_at) * 1000.0,
            ),
            total_duration_ms=max(
                0.0,
                (completed_at - request_started_at) * 1000.0,
            ),
        )
        return generation, source_plan_sequence, plan

    def _store_completed_continuous_scan(self, future: Future) -> None:
        try:
            generation, source_sequence, plan = future.result()
        except Exception as exc:
            with self._lock:
                failed_generation = self._continuous_scan_pending_generation
                if self._continuous_scan_pending_future is future:
                    self._continuous_scan_pending_future = None
                    if self._continuous_scan_pending_generation is not None:
                        self._continuous_scan_generation_sources.pop(
                            self._continuous_scan_pending_generation,
                            None,
                        )
                    self._continuous_scan_pending_generation = None
                if (
                    failed_generation is not None
                    and not self._shutdown
                    and failed_generation >= self._continuous_scan_latest_generation
                ):
                    self._continuous_scan_failure_count += 1
                    self._continuous_scan_last_failure_generation = (
                        failed_generation
                    )
                    self._continuous_scan_last_failure_reason = str(
                        getattr(exc, "reason", type(exc).__name__)
                    )
                    self._continuous_scan_last_failure_outcome = (
                        AutoDiveContinuousScanOutcome.DEADLINE_EXCEEDED
                        if isinstance(
                            exc,
                            AutoDiveContinuousScanDeadlineExceeded,
                        )
                        or isinstance(exc, AutoDivePlanningBudgetExceeded)
                        else AutoDiveContinuousScanOutcome.NO_VALID_ROUTE
                    )
            return
        with self._lock:
            if self._continuous_scan_pending_future is future:
                self._continuous_scan_pending_future = None
                self._continuous_scan_pending_generation = None
            self._continuous_scan_generation_sources.pop(generation, None)
            if self._shutdown or generation < self._continuous_scan_latest_generation:
                return
            self._continuous_scan_latest_generation = generation
            self._latest_continuous_scan_plan = plan
            self._latest_continuous_scan_generation = generation
            self._latest_continuous_scan_source_sequence = source_sequence
            self._latest_continuous_scan_outcome = (
                _continuous_scan_plan_outcome(plan)[0]
            )
            self._continuous_scan_failure_count = 0
            self._continuous_scan_last_failure_generation = None
            self._continuous_scan_last_failure_reason = None
            self._continuous_scan_last_failure_outcome = None

    def _build_plan(
        self,
        generation: int,
        current_position: tuple[float, float, float],
        current_yaw: float | None = None,
        current_pitch: float | None = None,
        current_roll: float | None = None,
        current_travel_yaw: float | None = None,
        current_travel_pitch: float | None = None,
        avoid_positions: tuple[tuple[float, float, float], ...] = (),
        user_reposition: bool = False,
        force_hemisphere_scan: bool = False,
        request_started_at: float | None = None,
        source_plan_sequence: int | None = None,
        speculative_replan: bool = False,
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
            roll=current_roll,
            travel_yaw=current_travel_yaw,
            travel_pitch=current_travel_pitch,
            avoid_positions=avoid_positions,
            user_reposition=bool(user_reposition),
            force_hemisphere_scan=bool(force_hemisphere_scan),
            source_plan_sequence=source_plan_sequence,
            speculative_replan=bool(speculative_replan),
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
            if current_roll is not None:
                kwargs["current_roll"] = current_roll
            if current_travel_yaw is not None:
                kwargs["current_travel_yaw"] = current_travel_yaw
            if current_travel_pitch is not None:
                kwargs["current_travel_pitch"] = current_travel_pitch
            if avoid_positions:
                kwargs["avoid_positions"] = avoid_positions
            if user_reposition:
                kwargs["user_reposition"] = True
            if force_hemisphere_scan:
                kwargs["force_hemisphere_scan"] = True
            if self._cache_dir is not None:
                kwargs["cache_dir"] = self._cache_dir
            if self._navigation_route_id is not None:
                kwargs["route_id"] = self._navigation_route_id
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
                source_plan_sequence=source_plan_sequence,
                speculative_replan=bool(speculative_replan),
                force_hemisphere_scan=bool(force_hemisphere_scan),
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
            source_plan_sequence=source_plan_sequence,
            speculative_replan=bool(speculative_replan),
            force_hemisphere_scan=bool(force_hemisphere_scan),
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
                    if self._pending_generation is not None:
                        self._generation_source_sequences.pop(
                            self._pending_generation,
                            None,
                        )
                    self._pending_generation = None
            return

        with self._lock:
            if self._pending_future is future:
                self._pending_future = None
                self._pending_generation = None
            source_sequence = self._generation_source_sequences.pop(
                generation,
                None,
            )
            if self._shutdown or generation < self._latest_generation:
                return
            self._latest_generation = generation
            self._latest_plan = plan
            self._latest_plan_generation = generation
            self._latest_plan_source_sequence = source_sequence

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
        speculative_replan_enabled: bool = True,
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
        self.speculative_replan_enabled = bool(speculative_replan_enabled)
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
        self._speculative_replan_pending = False
        self._speculative_replan_plan_sequence: int | None = None
        self._speculative_replan_attempted_plan_sequence: int | None = None
        self._speculative_replan_requested_at: float | None = None
        self._replan_wait_started_at: float | None = None
        self._replan_wait_kind: str | None = None
        self._mesh_recovery_attempts = 0
        self._mesh_recovery_boundary_positions: list[np.ndarray] = []
        self._continuous_scan_frontier_requested = False
        self._continuous_scan_frontier_signature: tuple[object, ...] | None = None
        self._continuous_scan_frontier_position: np.ndarray | None = None
        self._continuous_scan_frontier_expansion_count = 0
        self._continuous_scan_frontier_expansion_requested = False
        self._continuous_scan_frontier_exhausted = False
        self._continuous_scan_fallback_attempted = False
        self._rolling_clearance_worker = AutoDiveRollingClearanceWorker()
        self._rolling_clearance_next_check_at = 0.0
        self._rolling_clearance_hold_elapsed_s: float | None = None
        self._rolling_clearance_report: AutoDiveRollingClearanceReport | None = None
        self._last_rolling_clearance_hold_at: float | None = None
        self._stuck_reference_time: float | None = None
        self._stuck_reference_position: np.ndarray | None = None
        self._last_stuck_event_at: float | None = None
        self._prefetch_cells: frozenset[tuple[int, int, int]] = frozenset()
        self._next_voxel_prefetch_at = 0.0
        self._last_voxel_prefetch_report: dict[str, object] | None = None
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
        if self._speculative_replan_pending:
            return "Preparing the next route"
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
            if bool(getattr(self.plan, "terminal_reached", False)):
                return "End of cave reached"
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
        self._speculative_replan_pending = False
        self._speculative_replan_plan_sequence = None
        self._speculative_replan_requested_at = None
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
        self._speculative_replan_pending = False
        self._speculative_replan_plan_sequence = None
        self._speculative_replan_attempted_plan_sequence = None
        self._speculative_replan_requested_at = None
        self._clear_replan_wait()
        self._mesh_recovery_attempts = 0
        self._mesh_recovery_boundary_positions = []
        self._continuous_scan_frontier_requested = False
        self._reset_continuous_scan_frontier_guard()
        self._rolling_clearance_next_check_at = now
        self._rolling_clearance_hold_elapsed_s = None
        self._rolling_clearance_report = None
        self._last_rolling_clearance_hold_at = None
        self._next_voxel_prefetch_at = now
        self._last_voxel_prefetch_report = None
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
        self._maybe_request_voxel_prefetch(now=now, reason="initial_plan")
        self._request_continuous_scan_if_needed(
            camera,
            now=now,
            reason="auto_dive_started",
        )
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
            speculative_replan_enabled=bool(self.speculative_replan_enabled),
            continuous_scan_enabled=bool(self._continuous_scan_supported()),
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
        cancel_continuous_scan = getattr(replanner, "cancel_continuous_scan", None)
        if callable(cancel_continuous_scan):
            cancel_continuous_scan()
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
        self._continuous_scan_frontier_requested = False
        self._reset_continuous_scan_frontier_guard()
        self._lookahead_replan_pending = False
        self._speculative_replan_pending = False
        self._speculative_replan_plan_sequence = None
        self._speculative_replan_attempted_plan_sequence = None
        self._speculative_replan_requested_at = None
        self._rolling_clearance_next_check_at = now
        self._rolling_clearance_hold_elapsed_s = None
        self._rolling_clearance_report = None
        self._user_assist_reason = None
        self.state = AutoDiveState.LOADING

        requested = replanner.request(
            current_position,
            current_yaw=current_yaw,
            current_pitch=current_pitch,
            current_roll=float(getattr(camera, "roll", 0.0)),
            current_travel_yaw=travel_yaw,
            current_travel_pitch=travel_pitch,
            avoid_positions=avoid_positions,
            user_reposition=True,
            force_hemisphere_scan=True,
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
            terminal_reached=bool(getattr(self.plan, "terminal_reached", False)),
            completion_message=(
                "End of cave reached. No valid forward passage remains."
                if completed and bool(getattr(self.plan, "terminal_reached", False))
                else None
            ),
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
            rolling_clearance=self._rolling_clearance_payload(),
            prefetch=self._prefetch_payload(),
        )
        if world is not None:
            set_prefetch = getattr(world, "set_prefetch_wanted_cells", None)
            if callable(set_prefetch):
                set_prefetch(())
        if self.replanner is not None:
            self.replanner.shutdown()
        self._rolling_clearance_worker.shutdown()
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
                if self._mesh_recovery_hold_reached(now=now):
                    self._apply_mesh_recovery_pose_to_camera(camera, now=now)
                self._request_mesh_recovery_replan_if_ready(
                    camera,
                    now=now,
                    world=world,
                )
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
            # Recovery scans are deliberately non-blocking before the safe
            # frontier; the rolling-clearance hold below will update elapsed
            # time to that exact boundary once it is reached.
            self._request_mesh_recovery_replan_if_ready(
                camera,
                now=now,
                world=world,
            )

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
        if (
            self._rolling_clearance_hold_elapsed_s is not None
            and (
                self._lookahead_replan_pending
                or self._mesh_recovery_active()
            )
            and self._elapsed_s >= self._rolling_clearance_hold_elapsed_s
        ):
            self._elapsed_s = min(
                self._elapsed_s,
                max(0.0, float(self._rolling_clearance_hold_elapsed_s)),
            )
            apply_pose_to_camera(camera, self.plan.route.pose_at(self._elapsed_s))
            self._set_commanded_position(camera)
            if (
                self._last_rolling_clearance_hold_at is None
                or now - self._last_rolling_clearance_hold_at >= 1.0
            ):
                self._last_rolling_clearance_hold_at = now
                self._record_blackbox(
                    "rolling_clearance_hold",
                    elapsed_s=float(self._elapsed_s),
                    safe_elapsed_s=float(self._rolling_clearance_hold_elapsed_s),
                    remaining_distance_m=self._route_remaining_distance_m(),
                    plan_sequence=int(self._plan_sequence),
                    report=(
                        None
                        if self._rolling_clearance_report is None
                        else self._rolling_clearance_report.diagnostic_payload()
                    ),
                )
            if self._mesh_recovery_active():
                self._request_mesh_recovery_replan_if_ready(
                    camera,
                    now=now,
                    world=world,
                )
            self.state = AutoDiveState.LOADING
            return self.state
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
            if bool(getattr(self.plan, "terminal_reached", False)):
                self._record_blackbox(
                    "end_of_cave_reached",
                    message="End of cave reached. No valid forward passage remains.",
                    position=_vector_payload(camera.position),
                    elapsed_s=float(self._elapsed_s),
                    progress=float(self.progress),
                    plan_sequence=int(self._plan_sequence),
                    plan=auto_dive_plan_summary(self.plan),
                )
                self.stop(world, completed=True)
                return self.state
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
        take_latest_plan = getattr(replanner, "take_latest_plan", None)
        latest_plan = (
            take_latest_plan() if callable(take_latest_plan) else None
        )
        replan_generation = getattr(replanner, "last_taken_plan_generation", None)
        replan_source_sequence = getattr(
            replanner,
            "last_taken_plan_source_sequence",
            None,
        )
        continuous_scan_replan = False
        continuous_scan_outcome = AutoDiveContinuousScanOutcome.NO_VALID_ROUTE
        continuous_scan_frontier_signature: tuple[object, ...] | None = None
        continuous_scan_frontier_details: dict[str, Any] = {}
        take_continuous_scan = getattr(
            replanner,
            "take_latest_continuous_scan",
            None,
        )
        continuous_scan_plan = (
            take_continuous_scan() if callable(take_continuous_scan) else None
        )
        if continuous_scan_plan is not None:
            (
                continuous_scan_outcome,
                continuous_scan_frontier_signature,
                continuous_scan_frontier_details,
            ) = _continuous_scan_plan_outcome(continuous_scan_plan)
            recorded_outcome = getattr(
                replanner,
                "last_taken_continuous_scan_outcome",
                None,
            )
            if isinstance(recorded_outcome, AutoDiveContinuousScanOutcome):
                continuous_scan_outcome = recorded_outcome
            continuous_generation = getattr(
                replanner,
                "last_taken_continuous_scan_generation",
                None,
            )
            continuous_source_sequence = getattr(
                replanner,
                "last_taken_continuous_scan_source_sequence",
                None,
            )
            if latest_plan is None and not user_resume:
                latest_plan = continuous_scan_plan
                replan_generation = continuous_generation
                replan_source_sequence = continuous_source_sequence
                continuous_scan_replan = True
            else:
                self._record_blackbox(
                    "continuous_scan_discarded",
                    reason=(
                        "authoritative_replan_available"
                        if latest_plan is not None
                        else "user_resume_pending"
                    ),
                    scan_generation=continuous_generation,
                    source_plan_sequence=continuous_source_sequence,
                    current_plan_sequence=int(self._plan_sequence),
                    scan_outcome=continuous_scan_outcome.value,
                    frontier_signature=continuous_scan_frontier_signature,
                    frontier_details=continuous_scan_frontier_details,
                    plan=_plan_summary(continuous_scan_plan),
                )
        rejected_replan = False
        if latest_plan is not None:
            mesh_recovery = self._mesh_recovery_active()
            lookahead_replan = self._lookahead_replan_pending
            speculative_replan = bool(
                self._speculative_replan_pending
                and self._speculative_replan_plan_sequence
                == self._plan_sequence
                and (
                    replan_source_sequence is None
                    or replan_source_sequence == self._plan_sequence
                )
            )
            stale_speculative_replan = bool(
                self._speculative_replan_pending
                and replan_source_sequence is not None
                and replan_source_sequence
                == self._speculative_replan_plan_sequence
            )
            if (
                replan_source_sequence is not None
                and replan_source_sequence != self._plan_sequence
            ):
                rejection = {
                    "reason": "stale_source_plan_sequence",
                    "stale_source_plan_sequence": int(replan_source_sequence),
                    "current_plan_sequence": int(self._plan_sequence),
                }
                if continuous_scan_replan:
                    continuous_scan_outcome = AutoDiveContinuousScanOutcome.STALE_RESULT
                    continuous_scan_frontier_signature = None
                    continuous_scan_frontier_details = {
                        "outcome": continuous_scan_outcome.value,
                        "reason": "stale_source_plan_sequence",
                    }
            elif (
                self.replan_only_during_survey
                and not self._survey_active(now=now)
                and not mesh_recovery
                and not lookahead_replan
                and not user_resume
                and not speculative_replan
                and not continuous_scan_replan
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
            frontier_expansion_required = bool(
                continuous_scan_replan
                and continuous_scan_outcome
                is AutoDiveContinuousScanOutcome.FRONTIER_EXHAUSTED
                and not bool(
                    continuous_scan_frontier_details.get(
                        "frontier_expansion",
                        False,
                    )
                )
            )
            if rejection is None and frontier_expansion_required:
                rejection = {"reason": "frontier_expansion_required"}
            if rejection is None:
                resume_elapsed_s = _route_elapsed_nearest_position(
                    latest_plan,
                    current_position,
                )
                mesh_recovery_progressed = bool(
                    mesh_recovery
                    and self._mesh_recovery_plan_made_progress(
                        latest_plan,
                        current_position,
                    )
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
                self._continuous_scan_frontier_requested = False
                self._continuous_scan_fallback_attempted = False
                if continuous_scan_replan:
                    self._reset_continuous_scan_frontier_guard()
                self._lookahead_replan_pending = False
                self._speculative_replan_pending = False
                self._speculative_replan_plan_sequence = None
                self._speculative_replan_attempted_plan_sequence = None
                self._speculative_replan_requested_at = None
                self._rolling_clearance_next_check_at = now
                self._rolling_clearance_hold_elapsed_s = None
                self._rolling_clearance_report = None
                self._next_voxel_prefetch_at = now
                self._last_voxel_prefetch_report = None
                self._user_resume_replan_pending = False
                self._clear_replan_wait()
                self._user_assist_reason = None
                cancel_continuous_scan = getattr(
                    replanner,
                    "cancel_continuous_scan",
                    None,
                )
                if callable(cancel_continuous_scan):
                    cancel_continuous_scan()
                if mesh_recovery_progressed:
                    # Recovery attempts are bounded per frontier. Preserve the
                    # prior frontier list for avoidance, but do not let a
                    # successful forward handoff consume the next frontier's
                    # one-shot scan budget.
                    self._mesh_recovery_attempts = 0
                elif not _plan_needs_boundary_replan(latest_plan) and not user_resume:
                    self._mesh_recovery_attempts = 0
                    self._mesh_recovery_boundary_positions = []
                self.refresh_prefetch(world)
                self._readiness = self.readiness_for_world(world)
                self._record_blackbox(
                    "replan_accepted",
                    camera_position=_vector_payload(current_position),
                    user_reposition=bool(user_resume),
                    speculative_replan=bool(speculative_replan),
                    continuous_scan=bool(continuous_scan_replan),
                    source_plan_sequence=replan_source_sequence,
                    resume_elapsed_s=float(resume_elapsed_s),
                    resume_progress=float(self.progress),
                    plan=_plan_summary(latest_plan),
                    readiness=_readiness_payload(self._readiness),
                    plan_sequence=int(self._plan_sequence),
                    previous_plan_sequence=int(previous_plan_sequence),
                    replan_generation=replan_generation,
                    mesh_recovery_progressed=mesh_recovery_progressed,
                    scan_outcome=(
                        continuous_scan_outcome.value
                        if continuous_scan_replan
                        else None
                    ),
                    frontier_signature=(
                        continuous_scan_frontier_signature
                        if continuous_scan_replan
                        else None
                    ),
                    frontier_details=(
                        continuous_scan_frontier_details
                        if continuous_scan_replan
                        else None
                    ),
                    mesh_recovery_attempts=int(self._mesh_recovery_attempts),
                    remaining_distance_m=self._route_remaining_distance_m(),
                    prefetch=self._prefetch_payload(),
                )
                swapped = True
            else:
                if not continuous_scan_replan:
                    self._mesh_recovery_replan_pending = False
                    self._lookahead_replan_pending = False
                if speculative_replan or stale_speculative_replan:
                    self._speculative_replan_pending = False
                    self._speculative_replan_plan_sequence = None
                    self._speculative_replan_requested_at = None
                if not continuous_scan_replan:
                    self._user_resume_replan_pending = False
                    self._clear_replan_wait()
                elif mesh_recovery:
                    self._continuous_scan_frontier_requested = False
                self._last_rejected_replan_position = current_position.copy()
                rejected_replan = True
                self._record_blackbox(
                    "continuous_scan_rejected"
                    if continuous_scan_replan
                    else "replan_rejected",
                    camera_position=_vector_payload(current_position),
                    plan=_plan_summary(latest_plan),
                    plan_sequence=int(self._plan_sequence),
                    replan_generation=replan_generation,
                    continuous_scan=bool(continuous_scan_replan),
                    speculative_replan=bool(
                        speculative_replan or stale_speculative_replan
                    ),
                    source_plan_sequence=replan_source_sequence,
                    scan_outcome=(
                        continuous_scan_outcome.value
                        if continuous_scan_replan
                        else None
                    ),
                    frontier_signature=(
                        continuous_scan_frontier_signature
                        if continuous_scan_replan
                        else None
                    ),
                    frontier_details=(
                        continuous_scan_frontier_details
                        if continuous_scan_replan
                        else None
                    ),
                    **rejection,
                )
                if continuous_scan_replan:
                    self._handle_continuous_scan_no_progress(
                        plan=latest_plan,
                        outcome=continuous_scan_outcome,
                        signature=continuous_scan_frontier_signature,
                        details=continuous_scan_frontier_details,
                        rejection=rejection,
                        current_position=current_position,
                        world=world,
                        now=now,
                    )
                if (
                    not continuous_scan_replan
                    and (mesh_recovery or lookahead_replan or user_resume)
                ):
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
            or self._speculative_replan_pending
        ):
            has_pending = getattr(replanner, "has_pending", None)
            if callable(has_pending) and not has_pending():
                user_resume_without_plan = self._user_resume_replan_pending
                lookahead_without_plan = self._lookahead_replan_pending
                speculative_without_plan = self._speculative_replan_pending
                self._mesh_recovery_replan_pending = False
                self._lookahead_replan_pending = False
                self._user_resume_replan_pending = False
                self._speculative_replan_pending = False
                self._speculative_replan_plan_sequence = None
                self._speculative_replan_requested_at = None
                self._clear_replan_wait()
                event = (
                    "user_resume_replan_finished_without_plan"
                    if user_resume_without_plan
                    else "lookahead_replan_finished_without_plan"
                    if lookahead_without_plan
                    else "speculative_replan_finished_without_plan"
                    if speculative_without_plan
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
                    speculative_replan=bool(speculative_without_plan),
                )
                if not speculative_without_plan:
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

        self._poll_voxel_prefetch_report()
        self._update_rolling_forward_clearance(
            camera,
            world,
            now=now,
        )

        if not rejected_replan and self._should_request_replan(
            current_position,
            now=now,
        ):
            if self._continuous_scan_supported():
                self._request_continuous_scan_if_needed(
                    camera,
                    now=now,
                    reason="route_horizon",
                    world=world,
                )
            else:
                travel_yaw, travel_pitch = self._current_route_travel_angles(
                    current_position
                )
                if replanner.request(
                    current_position,
                    current_yaw=float(getattr(camera, "yaw", 0.0)),
                    current_pitch=float(getattr(camera, "pitch", 0.0)),
                    current_roll=float(getattr(camera, "roll", 0.0)),
                    current_travel_yaw=travel_yaw,
                    current_travel_pitch=travel_pitch,
                ):
                    self._begin_replan_wait("distance", now=now)
                    self._last_replan_request_position = current_position.copy()
                    self._last_replan_request_at = now
                    if self._survey_active(now=now):
                        self._survey_replan_requested = True

        self._maybe_request_voxel_prefetch(now=now, reason="rolling_horizon")
        if self._continuous_scan_supported():
            self._request_continuous_scan_if_needed(
                camera,
                now=now,
                reason="continuous_cycle",
                world=world,
            )
        else:
            self._maybe_request_speculative_replan(camera, now=now)

        return swapped

    def _maybe_request_speculative_replan(self, camera, *, now: float) -> bool:
        """Keep one future route ready while the accepted route keeps running.

        This is intentionally a separate seam from distance-triggered and
        boundary-triggered replanning. A speculative request never enters the
        replan wait state, so an unfinished future cannot freeze the camera or
        replace the route that is currently being followed.
        """
        replanner = self.replanner
        if (
            not self.speculative_replan_enabled
            or replanner is None
            or not self.active
            or self._continuous_scan_supported()
            or bool(getattr(self.plan, "terminal_reached", False))
            or self._mesh_recovery_active()
            or self._lookahead_replan_pending
            or self._mesh_recovery_replan_pending
            or self._user_resume_replan_pending
            or self._speculative_replan_pending
            or self._speculative_replan_attempted_plan_sequence
            == self._plan_sequence
        ):
            return False

        request = getattr(replanner, "request_speculative", None)
        if not callable(request):
            # Keep compatibility with test/embedding replanners from before
            # the speculative slot was introduced.
            return False
        has_pending = getattr(replanner, "has_pending", None)
        if callable(has_pending) and has_pending():
            return False

        duration_s = max(0.0, float(getattr(self.plan.route, "duration_s", 0.0)))
        elapsed_s = max(0.0, float(self._elapsed_s))
        remaining_s = max(0.0, duration_s - elapsed_s)
        if duration_s <= 1e-9:
            return False
        if elapsed_s < min(
            DEFAULT_AUTO_DIVE_SPECULATIVE_REPLAN_INITIAL_GRACE_SECONDS,
            duration_s * 0.25,
        ):
            return False
        lead_s = max(
            DEFAULT_AUTO_DIVE_SPECULATIVE_REPLAN_MIN_LEAD_SECONDS,
            self._replan_planning_budget_s
            * DEFAULT_AUTO_DIVE_SPECULATIVE_REPLAN_PLANNING_MARGIN,
        )
        minimum_remaining_s = (
            DEFAULT_AUTO_DIVE_SPECULATIVE_REPLAN_MIN_REMAINING_SECONDS
        )
        # A short leg has no useful speculative window. Its explicit voxel or
        # mesh frontier handoff remains the authoritative route transition.
        if duration_s <= lead_s + minimum_remaining_s:
            return False
        if not minimum_remaining_s < remaining_s <= lead_s:
            return False

        current_position = np.asarray(camera.position, dtype=np.float64)
        travel_yaw, travel_pitch = self._current_route_travel_angles(
            current_position
        )
        try:
            requested = bool(
                request(
                    current_position,
                    source_plan_sequence=int(self._plan_sequence),
                    current_yaw=float(getattr(camera, "yaw", 0.0)),
                    current_pitch=float(getattr(camera, "pitch", 0.0)),
                    current_roll=float(getattr(camera, "roll", 0.0)),
                    current_travel_yaw=travel_yaw,
                    current_travel_pitch=travel_pitch,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive worker seam
            self._speculative_replan_attempted_plan_sequence = self._plan_sequence
            self._record_blackbox(
                "speculative_replan_request_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                position=_vector_payload(current_position),
                elapsed_s=elapsed_s,
                remaining_s=remaining_s,
                lead_s=lead_s,
                plan_sequence=int(self._plan_sequence),
            )
            return False

        if requested:
            self._speculative_replan_pending = True
            self._speculative_replan_plan_sequence = self._plan_sequence
            self._speculative_replan_attempted_plan_sequence = self._plan_sequence
            self._speculative_replan_requested_at = float(now)
        self._record_blackbox(
            "speculative_replan_requested",
            requested=bool(requested),
            position=_vector_payload(current_position),
            elapsed_s=elapsed_s,
            remaining_s=remaining_s,
            lead_s=lead_s,
            planning_budget_s=float(self._replan_planning_budget_s),
            plan_sequence=int(self._plan_sequence),
            route_duration_s=duration_s,
            worker="AutoDiveReplanner",
        )
        return requested

    def _update_rolling_forward_clearance(
        self,
        camera,
        world,
        *,
        now: float,
    ) -> None:
        """Poll and schedule the worker that protects the next route edge."""
        report = self._rolling_clearance_worker.poll()
        if report is not None:
            if report.plan_sequence != self._plan_sequence:
                self._record_blackbox(
                    "rolling_clearance_result_discarded",
                    reason="stale_plan_sequence",
                    report=report.diagnostic_payload(),
                    current_plan_sequence=int(self._plan_sequence),
                )
            else:
                self._rolling_clearance_report = report
                self._record_blackbox(
                    "rolling_clearance_result",
                    **report.diagnostic_payload(),
                )
                if report.needs_replan and self.replanner is not None:
                    self._rolling_clearance_hold_elapsed_s = min(
                        float(self.plan.route.duration_s),
                        max(0.0, float(report.safe_elapsed_s)),
                    )
                    if report.reason == "voxel_forward_clearance_blocked":
                        triggered = self._request_rolling_replan(
                            camera,
                            now=now,
                            world=world,
                        )
                        kind = "voxel_clearance"
                    elif _plan_uses_voxel_lookahead_boundary(self.plan):
                        triggered = self._maybe_request_lookahead_replan(
                            camera,
                            now=now,
                            world=world,
                        )
                        kind = "voxel_lookahead"
                    elif _plan_needs_boundary_replan(self.plan):
                        triggered = self._maybe_start_mesh_recovery_scan(
                            now=now,
                            world=world,
                            reason="rolling_forward_clearance",
                        )
                        kind = "mesh_recovery"
                    else:
                        triggered = False
                        kind = "none"
                    self._record_blackbox(
                        "rolling_clearance_replan_triggered",
                        triggered=bool(triggered),
                        kind=kind,
                        report=report.diagnostic_payload(),
                        plan_sequence=int(self._plan_sequence),
                    )

        if (
            self.replanner is None
            or bool(getattr(self.plan, "terminal_reached", False))
            or self._lookahead_replan_pending
            or self._mesh_recovery_active()
            or self._user_resume_replan_pending
            or now < self._rolling_clearance_next_check_at
            or (
                not _plan_needs_boundary_replan(self.plan)
                and not self._plan_has_cached_voxel_volume()
            )
        ):
            return
        trigger_distance = self._rolling_clearance_trigger_distance_m()
        standoff_distance = self._rolling_clearance_standoff_distance_m()
        submitted = self._rolling_clearance_worker.request(
            self.plan,
            plan_sequence=self._plan_sequence,
            elapsed_s=self._elapsed_s,
            trigger_distance_m=trigger_distance,
            standoff_distance_m=standoff_distance,
        )
        self._rolling_clearance_next_check_at = (
            now + DEFAULT_AUTO_DIVE_ROLLING_CLEARANCE_INTERVAL_SECONDS
        )
        if submitted:
            self._record_blackbox(
                "rolling_clearance_check_started",
                plan_sequence=int(self._plan_sequence),
                elapsed_s=float(self._elapsed_s),
                remaining_distance_m=self._route_remaining_distance_m(),
                trigger_distance_m=float(trigger_distance),
                standoff_distance_m=float(standoff_distance),
                worker="AutoDiveClearance",
            )

    def _rolling_clearance_trigger_distance_m(self) -> float:
        route_length = max(0.0, float(getattr(self.plan, "route_length_m", 0.0)))
        if route_length <= 1e-9:
            return 0.0
        speed = self._route_speed_m_per_second()
        planning_window_s = (
            self._replan_planning_budget_s
            * DEFAULT_AUTO_DIVE_ROLLING_CLEARANCE_PLANNING_MARGIN
            + 1.0
        )
        lead_distance = max(
            DEFAULT_AUTO_DIVE_ROLLING_CLEARANCE_MIN_LEAD_M,
            speed * planning_window_s,
            self.replan_distance_m * 2.0,
        )
        return min(route_length * 0.75, lead_distance)

    def _rolling_clearance_standoff_distance_m(self) -> float:
        route_length = max(0.0, float(getattr(self.plan, "route_length_m", 0.0)))
        if route_length <= 1e-9:
            return 0.0
        return min(
            route_length * 0.35,
            max(
                DEFAULT_AUTO_DIVE_ROLLING_CLEARANCE_MIN_LEAD_M,
                self.replan_distance_m * 1.25,
                self._route_speed_m_per_second() * 0.75,
            ),
        )

    def _poll_voxel_prefetch_report(self) -> None:
        """Consume completed navigation prefetch work without blocking frames."""
        if self.replanner is None:
            return
        take_report = getattr(self.replanner, "take_voxel_prefetch_report", None)
        if not callable(take_report):
            return
        report = take_report()
        if report is None:
            return
        payload = report.diagnostic_payload()
        if report.plan_sequence != self._plan_sequence:
            self._record_blackbox(
                "voxel_prefetch_result_discarded",
                reason="stale_plan_sequence",
                report=payload,
                current_plan_sequence=int(self._plan_sequence),
            )
            return
        self._last_voxel_prefetch_report = payload
        self._record_blackbox("voxel_prefetch_result", **payload)

    def _maybe_request_voxel_prefetch(self, *, now: float, reason: str) -> bool:
        """Schedule a bounded future voxel horizon when the worker is free."""
        replanner = self.replanner
        if (
            replanner is None
            or not self.active
            or bool(getattr(self.plan, "terminal_reached", False))
            or float(now) < self._next_voxel_prefetch_at
        ):
            return False
        volume = getattr(self.plan, "navigation_atlas", None)
        if volume is None:
            volume = getattr(
                getattr(self.plan, "centerline_path", None),
                "cached_voxel_volume",
                None,
            )
        if not callable(getattr(volume, "prefetch_for_points", None)):
            return False
        has_pending = getattr(replanner, "has_voxel_prefetch_pending", None)
        if callable(has_pending) and has_pending():
            self._next_voxel_prefetch_at = (
                float(now) + DEFAULT_AUTO_DIVE_VOXEL_PREFETCH_INTERVAL_SECONDS
            )
            return False
        request = getattr(replanner, "request_voxel_prefetch", None)
        if not callable(request):
            return False
        horizon_s = DEFAULT_AUTO_DIVE_VOXEL_PREFETCH_HORIZON_SECONDS
        if self._survey_active(now=now):
            horizon_s *= max(
                1.0,
                DEFAULT_AUTO_DIVE_SURVEY_PREFETCH_LOOKAHEAD_MULTIPLIER,
            )
        try:
            requested = bool(
                request(
                    self.plan,
                    plan_sequence=int(self._plan_sequence),
                    elapsed_s=float(self._elapsed_s),
                    reason=str(reason),
                    horizon_s=float(horizon_s),
                )
            )
        except Exception as exc:  # pragma: no cover - defensive worker seam
            self._record_blackbox(
                "voxel_prefetch_request_failed",
                reason=str(reason),
                error_type=type(exc).__name__,
                error=str(exc),
                plan_sequence=int(self._plan_sequence),
            )
            requested = False
        self._next_voxel_prefetch_at = (
            float(now) + DEFAULT_AUTO_DIVE_VOXEL_PREFETCH_INTERVAL_SECONDS
        )
        if requested:
            self._record_blackbox(
                "voxel_prefetch_started",
                reason=str(reason),
                plan_sequence=int(self._plan_sequence),
                elapsed_s=float(self._elapsed_s),
                horizon_s=float(horizon_s),
                worker="AutoDiveVoxelPrefetch",
            )
        return requested

    def _plan_has_cached_voxel_volume(self) -> bool:
        volume = getattr(self.plan, "navigation_atlas", None)
        if volume is None:
            centerline_path = getattr(self.plan, "centerline_path", None)
            volume = getattr(centerline_path, "cached_voxel_volume", None)
        return callable(
            getattr(volume, "probe_point", None)
        )

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
        min_step_m = max(0.05, self.replan_distance_m * 0.25)
        next_point = self._first_plan_point_after_start(
            plan,
            minimum_distance_m=min_step_m,
        )
        if next_point is None:
            # A graph route may contain a nearest-node anchor followed by a
            # meaningful continuation. Ignore only near-duplicate anchors;
            # retain a useful rejection when the entire route is too short.
            short_point = self._first_plan_point_after_start(plan)
            if short_point is None:
                return {"reason": "no_next_point_after_start"}
            short_step_m = float(np.linalg.norm(short_point - current_position))
            return {
                "reason": "next_point_too_close",
                "next_step_m": short_step_m,
                "min_step_m": float(min_step_m),
                "next_point": _vector_payload(short_point),
            }
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
            rolling_clearance=self._rolling_clearance_payload(),
            prefetch=self._prefetch_payload(),
        )

    def _rolling_clearance_payload(self) -> dict[str, Any]:
        report = self._rolling_clearance_report
        return {
            "worker": "AutoDiveClearance",
            "pending": bool(self._rolling_clearance_worker.has_pending()),
            "hold_elapsed_s": (
                None
                if self._rolling_clearance_hold_elapsed_s is None
                else float(self._rolling_clearance_hold_elapsed_s)
            ),
            "report": None if report is None else report.diagnostic_payload(),
        }

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

    def _first_plan_point_after_start(
        self,
        plan: AutoDivePlan,
        *,
        minimum_distance_m: float | None = None,
    ) -> np.ndarray | None:
        if len(plan.route_points) < 2:
            return None
        start = np.asarray(plan.route_points[0], dtype=np.float64)
        minimum_distance = (
            self.replan_distance_m * 0.05
            if minimum_distance_m is None
            else float(minimum_distance_m)
        )
        min_distance_sq = max(1e-9, minimum_distance) ** 2
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
            "navigation_voxel": self._last_voxel_prefetch_report,
            "speculative_replan": {
                "enabled": bool(self.speculative_replan_enabled),
                "pending": bool(self._speculative_replan_pending),
                "plan_sequence": self._speculative_replan_plan_sequence,
                "attempted_plan_sequence": (
                    self._speculative_replan_attempted_plan_sequence
                ),
                "requested_at": self._speculative_replan_requested_at,
            },
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

    def _mesh_recovery_hold_reached(self, *, now: float | None = None) -> bool:
        """Return whether a recovery scan has reached its safe frontier.

        Rolling-clearance reports are intentionally consumed before the safe
        prefix ends.  Starting the scan must not turn that lead time into a
        camera freeze; the route is held only once its reported frontier is
        reached.  Boundary recovery without a rolling report still holds
        immediately, preserving the existing end-of-route behavior.
        """
        if not self._mesh_recovery_active():
            return False
        hold_elapsed_s = self._rolling_clearance_hold_elapsed_s
        if hold_elapsed_s is None:
            return True
        if self._pause_started_at is not None or self._started_at is None:
            elapsed_s = float(self._elapsed_s)
        else:
            now = self.perf_counter() if now is None else float(now)
            elapsed_s = min(
                float(self.plan.route.duration_s),
                max(0.0, now - self._started_at - self._paused_seconds),
            )
        return elapsed_s + 1e-6 >= float(hold_elapsed_s)

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

    def _request_rolling_replan(self, camera, *, now: float, world=None) -> bool:
        """Request an authoritative route rebuild after a voxel preflight hit."""
        if self.replanner is None:
            return False
        if self._continuous_scan_supported():
            requested = self._request_continuous_scan_if_needed(
                camera,
                now=now,
                reason="rolling_forward_clearance",
                world=world,
            )
            self._record_blackbox(
                "rolling_clearance_replan_requested",
                requested=bool(requested),
                continuous_scan=True,
                position=_vector_payload(camera.position),
                elapsed_s=float(self._elapsed_s),
                progress=float(self.progress),
                plan_sequence=int(self._plan_sequence),
                plan=_plan_summary(self.plan),
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
            current_roll=float(getattr(camera, "roll", 0.0)),
            current_travel_yaw=travel_yaw,
            current_travel_pitch=travel_pitch,
        )
        self._lookahead_replan_pending = True
        if requested or self._replan_wait_started_at is None:
            self._begin_replan_wait("rolling_clearance", now=now)
        if requested:
            self._last_replan_request_position = current_position.copy()
            self._last_replan_request_at = now
        self._record_blackbox(
            "rolling_clearance_replan_requested",
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
        if self._continuous_scan_supported():
            requested = self._request_continuous_scan_if_needed(
                camera,
                now=now,
                reason="voxel_lookahead_boundary",
                world=world,
            )
            self._record_blackbox(
                "lookahead_replan_requested",
                requested=bool(requested),
                continuous_scan=True,
                position=_vector_payload(camera.position),
                elapsed_s=float(self._elapsed_s),
                progress=float(self.progress),
                plan_sequence=int(self._plan_sequence),
                plan=_plan_summary(self.plan),
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
            current_roll=float(getattr(camera, "roll", 0.0)),
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
        if (
            not self._continuous_scan_supported()
            and self._mesh_recovery_attempts
            >= DEFAULT_AUTO_DIVE_MESH_RECOVERY_MAX_ATTEMPTS
        ):
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
            self._continuous_scan_frontier_requested = False
            self._remember_mesh_recovery_boundary(route_position)
            self._record_blackbox(
                "mesh_recovery_scan_started",
                reason=str(reason),
                scan_mode=(
                    "continuous_forward_hemisphere"
                    if self._continuous_scan_supported()
                    else "forward_hemisphere"
                ),
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

    def _request_mesh_recovery_replan_if_ready(
        self,
        camera,
        *,
        now: float,
        world=None,
    ) -> bool:
        if self.replanner is None:
            return False
        if self._continuous_scan_supported():
            if self._mesh_recovery_started_at is None:
                return False
            scan_elapsed_s = max(
                0.0,
                float(now) - self._mesh_recovery_started_at,
            )
            if scan_elapsed_s < DEFAULT_AUTO_DIVE_MESH_RECOVERY_TURN_SECONDS:
                return False
            if not self._continuous_scan_frontier_requested:
                requested = self._request_continuous_scan_if_needed(
                    camera,
                    now=now,
                    reason="mesh_recovery_frontier",
                    world=world,
                )
                self._continuous_scan_frontier_requested = True
                self._record_blackbox(
                    "mesh_recovery_replan_requested",
                    requested=bool(requested),
                    continuous_scan=True,
                    attempts=int(self._mesh_recovery_attempts),
                    scan_elapsed_s=float(scan_elapsed_s),
                    position=_vector_payload(camera.position),
                    yaw=float(getattr(camera, "yaw", 0.0)),
                    pitch=float(getattr(camera, "pitch", 0.0)),
                    travel_yaw=self._current_route_travel_angles(
                        np.asarray(camera.position, dtype=np.float64)
                    )[0],
                    travel_pitch=self._current_route_travel_angles(
                        np.asarray(camera.position, dtype=np.float64)
                    )[1],
                    force_hemisphere_scan=True,
                    avoid_positions=[
                        _vector_payload(position)
                        for position in self._mesh_recovery_prior_boundary_positions()
                    ],
                    elapsed_s=float(self._elapsed_s),
                    progress=float(self.progress),
                    plan=_plan_summary(self.plan),
                )
            return True
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
            current_roll=float(getattr(camera, "roll", 0.0)),
            current_travel_yaw=travel_yaw,
            current_travel_pitch=travel_pitch,
            force_hemisphere_scan=True,
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
            force_hemisphere_scan=True,
            avoid_positions=[
                _vector_payload(position)
                for position in self._mesh_recovery_prior_boundary_positions()
            ],
            elapsed_s=float(self._elapsed_s),
            progress=float(self.progress),
            plan=_plan_summary(self.plan),
        )
        return True

    def _continuous_scan_supported(self) -> bool:
        """Return whether the attached replanner supports the always-on path."""
        replanner = self.replanner
        if replanner is None:
            return False
        return all(
            callable(getattr(replanner, name, None))
            for name in (
                "request_continuous_scan",
                "take_latest_continuous_scan",
                "has_continuous_scan_pending",
                "has_continuous_scan_result",
            )
        )

    def _continuous_scan_safe_frontier_elapsed_s(self) -> float:
        """Return the route time at which a scan must already be handed off."""
        duration_s = max(0.0, float(getattr(self.plan.route, "duration_s", 0.0)))
        hold_elapsed_s = self._rolling_clearance_hold_elapsed_s
        if hold_elapsed_s is None or not math.isfinite(float(hold_elapsed_s)):
            return duration_s
        return min(
            duration_s,
            max(float(self._elapsed_s), float(hold_elapsed_s)),
        )

    def _continuous_scan_budget_s(self) -> float:
        """Return the scan budget left after the safe handoff reserve."""
        safe_frontier_s = self._continuous_scan_safe_frontier_elapsed_s()
        remaining_safe_s = max(0.0, safe_frontier_s - float(self._elapsed_s))
        return min(
            float(DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_BUDGET_S),
            remaining_safe_s
            - float(DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_HANDOFF_RESERVE_S),
        )

    def _request_continuous_scan_fallback(
        self,
        camera,
        *,
        now: float,
        reason: str,
        world=None,
        scan_budget_s: float | None = None,
    ) -> bool:
        """Run one bounded authoritative graph replan after a scan deadline."""
        replanner = self.replanner
        if replanner is None:
            return False
        if self._continuous_scan_fallback_attempted:
            if not (
                self._lookahead_replan_pending
                or self._mesh_recovery_replan_pending
            ):
                self._enter_user_assist(
                    world,
                    reason="continuous_scan_deadline_exceeded",
                    position=np.asarray(camera.position, dtype=np.float64),
                    details={
                        "fallback": "authoritative_graph_replan",
                        "reason": str(reason),
                    },
                    now=now,
                )
            return False
        request = getattr(replanner, "request", None)
        if not callable(request):
            self._continuous_scan_fallback_attempted = True
            self._enter_user_assist(
                world,
                reason="continuous_scan_fallback_unavailable",
                position=np.asarray(camera.position, dtype=np.float64),
                now=now,
            )
            return False

        current_position = np.asarray(camera.position, dtype=np.float64).reshape(3)
        travel_yaw, travel_pitch = self._current_route_travel_angles(
            current_position
        )
        request_kwargs = {
            "current_yaw": float(getattr(camera, "yaw", 0.0)),
            "current_pitch": float(getattr(camera, "pitch", 0.0)),
            "current_roll": float(getattr(camera, "roll", 0.0)),
            "current_travel_yaw": travel_yaw,
            "current_travel_pitch": travel_pitch,
            "force_hemisphere_scan": True,
            "source_plan_sequence": int(self._plan_sequence),
            "avoid_positions": tuple(
                tuple(float(value) for value in position)
                for position in self._mesh_recovery_prior_boundary_positions()
            ),
        }
        requested = bool(request(current_position, **request_kwargs))
        self._continuous_scan_fallback_attempted = True
        fallback_kind = (
            "mesh_recovery" if self._mesh_recovery_active() else "safe_frontier"
        )
        if fallback_kind == "mesh_recovery":
            self._mesh_recovery_replan_pending = True
        else:
            self._lookahead_replan_pending = True
        if requested or self._replan_wait_started_at is None:
            self._begin_replan_wait("continuous_scan_fallback", now=now)
        if requested:
            self._last_replan_request_position = current_position.copy()
            self._last_replan_request_at = float(now)
        self._record_blackbox(
            "continuous_scan_deadline_fallback_requested",
            requested=bool(requested),
            reason=str(reason),
            fallback="authoritative_graph_replan",
            fallback_kind=fallback_kind,
            position=_vector_payload(current_position),
            source_plan_sequence=int(self._plan_sequence),
            elapsed_s=float(self._elapsed_s),
            safe_frontier_elapsed_s=float(
                self._continuous_scan_safe_frontier_elapsed_s()
            ),
            scan_budget_s=(
                None if scan_budget_s is None else float(scan_budget_s)
            ),
            planning_budget_s=float(self._replan_planning_budget_s),
        )
        if not requested:
            has_pending = getattr(replanner, "has_pending", None)
            if not callable(has_pending) or not bool(has_pending()):
                self._enter_user_assist(
                    world,
                    reason="continuous_scan_fallback_unavailable",
                    position=current_position,
                    now=now,
                )
        return requested

    def _request_continuous_scan_if_needed(
        self,
        camera,
        *,
        now: float,
        reason: str,
        world=None,
        expand_frontier: bool | None = None,
    ) -> bool:
        """Keep one owner-safe, deadline-bounded forward scan in flight."""
        if expand_frontier is None:
            expand_frontier = self._continuous_scan_frontier_expansion_requested
        if (
            not self._continuous_scan_supported()
            or not self.active
            or bool(getattr(self.plan, "terminal_reached", False))
            or self._user_resume_replan_pending
            or self._continuous_scan_frontier_exhausted
        ):
            return False
        replanner = self.replanner
        failure_count = int(getattr(replanner, "continuous_scan_failure_count", 0))
        failure_reason = getattr(
            replanner,
            "continuous_scan_last_failure_reason",
            None,
        )
        deadline_failure = failure_reason == "deadline_exceeded"
        if deadline_failure:
            return self._request_continuous_scan_fallback(
                camera,
                now=now,
                reason=reason,
                world=world,
                scan_budget_s=None,
            )
        mesh_guard_failure = failure_reason == "mesh_collision_guard_unavailable"
        if (
            (self._mesh_recovery_active() or mesh_guard_failure)
            and failure_count >= DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_FAILURES
        ):
            position = np.asarray(camera.position, dtype=np.float64).reshape(3)
            details = {
                "failure_count": failure_count,
                "max_failures": int(DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_FAILURES),
                "last_failure_generation": getattr(
                    replanner,
                    "continuous_scan_last_failure_generation",
                    None,
                ),
                "last_failure_reason": failure_reason,
            }
            self._record_blackbox(
                "continuous_scan_exhausted",
                reason=str(reason),
                position=_vector_payload(position),
                plan_sequence=int(self._plan_sequence),
                **details,
            )
            self._enter_user_assist(
                world,
                reason=(
                    "mesh_collision_guard_unavailable"
                    if mesh_guard_failure
                    else "continuous_scan_exhausted"
                ),
                position=position,
                details=details,
                now=now,
            )
            return False
        has_pending = getattr(replanner, "has_continuous_scan_pending")
        has_result = getattr(replanner, "has_continuous_scan_result")
        if bool(has_pending()) or bool(has_result()):
            return False

        scan_budget_s = self._continuous_scan_budget_s()
        safe_frontier_elapsed_s = self._continuous_scan_safe_frontier_elapsed_s()
        remaining_safe_s = max(
            0.0,
            safe_frontier_elapsed_s - float(self._elapsed_s),
        )
        safe_horizon_too_short = (
            remaining_safe_s
            < DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MIN_SAFE_FRONTIER_S
        )
        budget_too_short = (
            scan_budget_s < DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MIN_BUDGET_S
        )
        if safe_horizon_too_short or budget_too_short:
            self._record_blackbox(
                "continuous_scan_deadline_unavailable",
                reason=str(reason),
                position=_vector_payload(camera.position),
                plan_sequence=int(self._plan_sequence),
                elapsed_s=float(self._elapsed_s),
                safe_frontier_elapsed_s=float(safe_frontier_elapsed_s),
                remaining_safe_s=float(remaining_safe_s),
                scan_budget_s=float(scan_budget_s),
                minimum_scan_budget_s=float(
                    DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MIN_BUDGET_S
                ),
                handoff_reserve_s=float(
                    DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_HANDOFF_RESERVE_S
                ),
                minimum_safe_frontier_s=float(
                    DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MIN_SAFE_FRONTIER_S
                ),
                safe_horizon_too_short=bool(safe_horizon_too_short),
            )
            return self._request_continuous_scan_fallback(
                camera,
                now=now,
                reason=(
                    "safe_frontier_horizon_too_short"
                    if safe_horizon_too_short
                    else "safe_frontier_budget_too_short"
                ),
                world=world,
                scan_budget_s=scan_budget_s,
            )

        current_position = np.asarray(camera.position, dtype=np.float64).reshape(3)
        travel_yaw, travel_pitch = self._current_route_travel_angles(
            current_position
        )
        request_kwargs: dict[str, Any] = {
            "current_yaw": float(getattr(camera, "yaw", 0.0)),
            "current_pitch": float(getattr(camera, "pitch", 0.0)),
            "current_roll": float(getattr(camera, "roll", 0.0)),
            "current_travel_yaw": travel_yaw,
            "current_travel_pitch": travel_pitch,
            "avoid_positions": tuple(
                tuple(float(value) for value in position)
                for position in self._mesh_recovery_prior_boundary_positions()
            ),
            "source_plan_sequence": int(self._plan_sequence),
        }
        # Keep compatibility with older test/dummy replanners and avoid
        # changing the ordinary scan request shape when no expansion is
        # needed.
        if expand_frontier:
            request_kwargs["expand_frontier"] = True
        request_kwargs["scan_budget_s"] = float(scan_budget_s)
        requested = bool(
            replanner.request_continuous_scan(
                current_position,
                **request_kwargs,
            )
        )
        if requested and expand_frontier:
            self._continuous_scan_frontier_expansion_requested = False
        if requested:
            self._last_replan_request_position = current_position.copy()
            self._last_replan_request_at = float(now)
        self._record_blackbox(
            "continuous_scan_cycle_requested",
            requested=bool(requested),
            reason=str(reason),
            position=_vector_payload(current_position),
            yaw=float(getattr(camera, "yaw", 0.0)),
            pitch=float(getattr(camera, "pitch", 0.0)),
            travel_yaw=travel_yaw,
            travel_pitch=travel_pitch,
            source_plan_sequence=int(self._plan_sequence),
            elapsed_s=float(self._elapsed_s),
            progress=float(self.progress),
            now=float(now),
            expand_frontier=bool(expand_frontier),
            scan_budget_s=float(scan_budget_s),
            safe_frontier_elapsed_s=float(
                self._continuous_scan_safe_frontier_elapsed_s()
            ),
            handoff_reserve_s=float(
                DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_HANDOFF_RESERVE_S
            ),
        )
        return requested

    def _reset_continuous_scan_frontier_guard(self) -> None:
        """Forget no-progress state after a new session or accepted route."""
        self._continuous_scan_frontier_signature = None
        self._continuous_scan_frontier_position = None
        self._continuous_scan_frontier_expansion_count = 0
        self._continuous_scan_frontier_expansion_requested = False
        self._continuous_scan_frontier_exhausted = False

    def _handle_continuous_scan_no_progress(
        self,
        *,
        plan: AutoDivePlan,
        outcome: AutoDiveContinuousScanOutcome,
        signature: tuple[object, ...] | None,
        details: Mapping[str, Any],
        rejection: Mapping[str, Any],
        current_position: np.ndarray,
        world,
        now: float,
    ) -> None:
        """Expand one stable frontier once, then stop repeated retries."""
        reason = str(rejection.get("reason", ""))
        if outcome is not AutoDiveContinuousScanOutcome.FRONTIER_EXHAUSTED and reason not in {
            "no_next_point_after_start",
            "next_point_too_close",
            "no_valid_route",
        }:
            return
        threshold_m = max(0.5, float(self.replan_distance_m) * 0.5)
        same_position = bool(
            self._continuous_scan_frontier_position is not None
            and float(
                np.linalg.norm(
                    current_position - self._continuous_scan_frontier_position
                )
            )
            <= threshold_m
        )
        same_signature = (
            signature is not None
            and self._continuous_scan_frontier_signature == signature
        )
        if not same_position or not same_signature:
            self._continuous_scan_frontier_signature = signature
            self._continuous_scan_frontier_position = current_position.copy()
            self._continuous_scan_frontier_expansion_count = 0
            self._continuous_scan_frontier_expansion_requested = False
            self._continuous_scan_frontier_exhausted = False

        if (
            self._continuous_scan_frontier_expansion_count
            < DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_FRONTIER_EXPANSIONS
        ):
            self._continuous_scan_frontier_expansion_count += 1
            self._continuous_scan_frontier_expansion_requested = True
            self._record_blackbox(
                "continuous_scan_frontier_expansion_requested",
                outcome=outcome.value,
                frontier_signature=signature,
                frontier_details=dict(details),
                rejection=dict(rejection),
                expansion_count=int(
                    self._continuous_scan_frontier_expansion_count
                ),
                max_expansions=int(
                    DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_FRONTIER_EXPANSIONS
                ),
                position=_vector_payload(current_position),
                plan_sequence=int(self._plan_sequence),
            )
            return

        self._continuous_scan_frontier_exhausted = True
        cancel_continuous_scan = getattr(
            self.replanner,
            "cancel_continuous_scan",
            None,
        )
        if callable(cancel_continuous_scan):
            cancel_continuous_scan()
        exhausted_details = {
            **dict(details),
            "frontier_signature": signature,
            "rejection": dict(rejection),
            "expansion_count": int(
                self._continuous_scan_frontier_expansion_count
            ),
            "max_expansions": int(
                DEFAULT_AUTO_DIVE_CONTINUOUS_SCAN_MAX_FRONTIER_EXPANSIONS
            ),
        }
        self._record_blackbox(
            "continuous_scan_frontier_exhausted",
            position=_vector_payload(current_position),
            plan_sequence=int(self._plan_sequence),
            **exhausted_details,
        )
        self._enter_user_assist(
            world,
            reason="frontier_expansion_exhausted",
            position=current_position,
            details=exhausted_details,
            now=now,
        )

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

    def _mesh_recovery_plan_made_progress(
        self,
        plan: AutoDivePlan,
        current_position: np.ndarray,
    ) -> bool:
        """Return whether an accepted recovery plan moved beyond its frontier."""
        if not self._mesh_recovery_boundary_positions:
            return False
        boundary = self._mesh_recovery_boundary_positions[-1]
        threshold_m = max(0.5, float(self.replan_distance_m) * 0.5)
        accepted_start = np.asarray(
            plan.route.pose_at(0.0).position,
            dtype=np.float64,
        )
        accepted_end = np.asarray(
            plan.route.pose_at(float(plan.route.duration_s)).position,
            dtype=np.float64,
        )
        return max(
            float(np.linalg.norm(np.asarray(current_position) - boundary)),
            float(np.linalg.norm(accepted_start - boundary)),
            float(np.linalg.norm(accepted_end - boundary)),
        ) > threshold_m

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
        try:
            cell_size = float(auto_dive_plan_navigation_cell_size(self.plan))
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
        cancel_continuous_scan = getattr(
            self.replanner,
            "cancel_continuous_scan",
            None,
        )
        if callable(cancel_continuous_scan):
            cancel_continuous_scan()
        self._remember_mesh_recovery_boundary(position)
        self._prefetch_cells = frozenset()
        self._readiness = AutoDiveReadiness(0, 0, 0, 0, 0, 1.0)
        self._pause_started_at = None
        self._survey_pause_started_at = None
        self._survey_replan_requested = False
        self._mesh_recovery_started_at = None
        self._mesh_recovery_replan_pending = False
        self._continuous_scan_frontier_requested = False
        self._continuous_scan_fallback_attempted = False
        self._reset_continuous_scan_frontier_guard()
        self._lookahead_replan_pending = False
        self._speculative_replan_pending = False
        self._speculative_replan_plan_sequence = None
        self._speculative_replan_attempted_plan_sequence = None
        self._speculative_replan_requested_at = None
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
        "navigation_route_id": getattr(plan, "navigation_route_id", None),
        "preflight_validated": bool(
            getattr(plan, "preflight_validated", False)
        ),
        "route_truncated_by_mesh": bool(
            getattr(plan, "route_truncated_by_mesh", False)
        ),
        "replan_at_end": bool(getattr(plan, "replan_at_end", False)),
        "terminal_reached": bool(getattr(plan, "terminal_reached", False)),
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
        "start_roll_deg": (
            None
            if not getattr(plan, "route", None)
            else float(plan.route.keyframes[0].roll_deg)
        ),
        "end": (
            None
            if not route_points
            else _vector_payload(route_points[-1])
        ),
        "end_roll_deg": (
            None
            if not getattr(plan, "route", None)
            else float(plan.route.keyframes[-1].roll_deg)
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
        "roll_deg": float(pose.roll_deg),
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
