"""Auto Dive playback and streaming-readiness state for the viewer."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
import math

import numpy as np

from caveviewer.core.navigation.autodive import AutoDivePlan, route_progress_fraction
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


class AutoDiveController:
    """Drive a finite centerline route while pausing for route lookahead loads."""

    def __init__(
        self,
        plan: AutoDivePlan,
        *,
        perf_counter: Callable[[], float],
        lookahead_seconds: float = 20.0,
    ) -> None:
        self.plan = plan
        self.perf_counter = perf_counter
        self.lookahead_seconds = max(1.0, float(lookahead_seconds))
        self.state = AutoDiveState.IDLE
        self._started_at: float | None = None
        self._pause_started_at: float | None = None
        self._paused_seconds = 0.0
        self._elapsed_s = 0.0
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

    def start(self, camera, world, now: float | None = None) -> None:
        """Place the camera on the route and begin loading the first lookahead."""
        now = self.perf_counter() if now is None else float(now)
        self._started_at = now
        self._pause_started_at = now
        self._paused_seconds = 0.0
        self._elapsed_s = 0.0
        self.state = AutoDiveState.LOADING
        apply_pose_to_camera(camera, self.plan.route.pose_at(0.0))
        self.refresh_prefetch(world)
        self._readiness = self.readiness_for_world(world)

    def stop(self, world=None, *, completed: bool = False) -> None:
        """Stop Auto Dive and clear owned streaming prefetch cells."""
        if world is not None:
            set_prefetch = getattr(world, "set_prefetch_wanted_cells", None)
            if callable(set_prefetch):
                set_prefetch(())
        self._prefetch_cells = frozenset()
        self.state = AutoDiveState.COMPLETE if completed else AutoDiveState.CANCELLED

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
