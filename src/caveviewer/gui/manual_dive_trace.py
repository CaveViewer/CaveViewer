"""Asynchronous, map-local camera tracing for manual Guided Dive references."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any
import uuid

import numpy as np

from caveviewer.core.map.cache_identity import (
    guided_dive_cache_identity_from_manifest,
    parse_guided_dive_cache_identity,
)

MANUAL_DIVE_TRACE_SCHEMA_VERSION = 2
MANUAL_DIVE_TRACE_DIRECTORY = "_guided_dives"
MANUAL_DIVE_TRACE_FILENAME_PREFIX = "guided_dive_manual_trace"
DEFAULT_SAMPLE_INTERVAL_S = 0.10
DEFAULT_SAMPLE_DISTANCE_M = 0.25
DEFAULT_SAMPLE_ANGLE_DEG = 2.0
DEFAULT_HEARTBEAT_INTERVAL_S = 1.0
DEFAULT_SAMPLE_CAP = 100_000
DEFAULT_QUEUE_CAP = 2_048

_STOP_WRITER = object()


def manual_dive_trace_directory(
    map_root: str | os.PathLike[str],
) -> Path:
    """Return the canonical map-local directory for completed Guided Dives."""
    raw_map_root = os.fspath(map_root).strip()
    if not raw_map_root:
        raise ValueError("manual Guided Dive traces require a map root")
    return Path(raw_map_root).expanduser().resolve() / MANUAL_DIVE_TRACE_DIRECTORY


def manual_dive_trace_map_context(
    manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return bounded map identity and cache evidence for a trace header."""
    manifest = manifest if isinstance(manifest, Mapping) else {}
    source_obj = os.path.basename(
        os.fspath(manifest.get("source_obj") or "map")
    )
    context = {
        "source_obj": source_obj,
        "manifest_version": manifest.get("version"),
        "chunk_size_m": manifest.get("chunk_size"),
        "triangle_count": manifest.get("triangle_count"),
        "coordinate_space": "manifest_xyz",
        "distance_unit": "meter",
        "orientation_unit": "radian",
    }
    cache_identity = guided_dive_cache_identity_from_manifest(manifest)
    if cache_identity is not None:
        context["cache_identity"] = cache_identity.payload()
    return context


@dataclass(frozen=True)
class ManualDivePose:
    """One exact camera pose captured on the render thread."""

    position: tuple[float, float, float]
    forward: tuple[float, float, float]
    up: tuple[float, float, float]
    right: tuple[float, float, float]
    yaw: float
    pitch: float
    roll: float
    move_speed_m_per_second: float

    @classmethod
    def from_camera(cls, camera: Any) -> ManualDivePose:
        return cls(
            position=_vector3(camera.position),
            forward=_vector3(camera.forward()),
            up=_vector3(camera.up()),
            right=_vector3(camera.right()),
            yaw=_finite_float(camera.yaw),
            pitch=_finite_float(camera.pitch),
            roll=_finite_float(camera.roll),
            move_speed_m_per_second=_finite_float(camera.move_speed),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "position": list(self.position),
            "forward": list(self.forward),
            "up": list(self.up),
            "right": list(self.right),
            "yaw": self.yaw,
            "pitch": self.pitch,
            "roll": self.roll,
            "move_speed_m_per_second": self.move_speed_m_per_second,
        }


@dataclass(frozen=True)
class ManualDiveTraceResult:
    """Final background-writer outcome, safe to poll from the render thread."""

    output_path: str
    partial_path: str
    completed: bool
    error: str | None
    canceled: bool = False


class ManualDiveTraceRecorder:
    """Capture a manual camera route without doing file I/O on the render thread."""

    def __init__(
        self,
        trace_dir: str | os.PathLike[str],
        *,
        map_context: Mapping[str, Any] | None = None,
        perf_counter: Callable[[], float] = time.perf_counter,
        utc_now: Callable[[], datetime] | None = None,
        sample_interval_s: float = DEFAULT_SAMPLE_INTERVAL_S,
        sample_distance_m: float = DEFAULT_SAMPLE_DISTANCE_M,
        sample_angle_deg: float = DEFAULT_SAMPLE_ANGLE_DEG,
        heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        sample_cap: int = DEFAULT_SAMPLE_CAP,
        queue_cap: int = DEFAULT_QUEUE_CAP,
    ) -> None:
        self.trace_dir = Path(trace_dir).resolve()
        self.map_context = dict(map_context or {})
        self._perf_counter = perf_counter
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self.sample_interval_s = max(0.0, float(sample_interval_s))
        self.sample_distance_m = max(0.0, float(sample_distance_m))
        self.sample_angle_deg = max(0.0, float(sample_angle_deg))
        self.heartbeat_interval_s = max(0.0, float(heartbeat_interval_s))
        self.sample_cap = max(2, int(sample_cap))
        self._queue: queue.Queue[dict[str, Any] | object] = queue.Queue(
            maxsize=max(4, int(queue_cap))
        )
        self._result_lock = threading.Lock()
        self._writer_done = threading.Event()
        self._cancel_requested = threading.Event()
        self._writer_error: str | None = None
        self._cancel_cleanup_error: str | None = None
        self._final_record: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None
        self._active = False
        self._started_at = 0.0
        self._started_utc = ""
        self._session_id = ""
        self._output_path = Path()
        self._partial_path = Path()
        self._last_observed_at = 0.0
        self._last_observed_pose: ManualDivePose | None = None
        self._last_sample_at = 0.0
        self._last_sample_pose: ManualDivePose | None = None
        self._sample_count = 0
        self._dropped_sample_count = 0
        self._total_distance_m = 0.0
        self._teleport_distance_m = 0.0
        self._discontinuity_count = 0
        self._minimum = np.full(3, math.inf, dtype=np.float64)
        self._maximum = np.full(3, -math.inf, dtype=np.float64)

    @property
    def active(self) -> bool:
        return self._active

    @property
    def output_path(self) -> str:
        return os.fspath(self._output_path)

    @property
    def writer_failed(self) -> bool:
        return self._writer_done.is_set() and self._writer_error is not None

    def start(self, pose: ManualDivePose, *, now: float | None = None) -> str:
        """Start one trace and enqueue its exact initial pose."""
        if self._active or self._thread is not None:
            raise RuntimeError("manual dive trace recorder is single-use")
        if (
            parse_guided_dive_cache_identity(
                self.map_context.get("cache_identity")
            )
            is None
        ):
            raise ValueError(
                "manual Guided Dive traces require a cache with stable identity; "
                "rebuild the map before recording"
            )
        sample_now = self._perf_counter() if now is None else float(now)
        started_utc = self._utc_now().astimezone(timezone.utc)
        self._started_at = sample_now
        self._started_utc = started_utc.isoformat(timespec="milliseconds")
        self._session_id = uuid.uuid4().hex
        timestamp = started_utc.strftime("%Y%m%dT%H%M%SZ")
        filename = (
            f"{MANUAL_DIVE_TRACE_FILENAME_PREFIX}-{timestamp}-"
            f"{self._session_id[:8]}.jsonl"
        )
        self._output_path = self.trace_dir / filename
        self._partial_path = self.trace_dir / f".{filename}.part"
        header = {
            "record": "trace_started",
            "schema_version": MANUAL_DIVE_TRACE_SCHEMA_VERSION,
            "session_id": self._session_id,
            "started_at_utc": self._started_utc,
            "map": self.map_context,
            "sampling_policy": {
                "minimum_interval_s": self.sample_interval_s,
                "distance_threshold_m": self.sample_distance_m,
                "orientation_threshold_deg": self.sample_angle_deg,
                "stationary_heartbeat_interval_s": self.heartbeat_interval_s,
                "sample_cap": self.sample_cap,
                "queue_cap": self._queue.maxsize,
                "simplification": "none",
            },
        }
        self._active = True
        self._thread = threading.Thread(
            target=self._write_records,
            args=(header,),
            name="caveviewer-manual-dive-trace",
            daemon=False,
        )
        self._thread.start()
        self._last_observed_pose = pose
        self._last_observed_at = sample_now
        self._record_pose(pose, sample_now, kind="sample", force=True)
        return self.output_path

    def observe(
        self,
        pose: ManualDivePose,
        *,
        now: float | None = None,
    ) -> bool:
        """Observe a rendered pose and retain it when a sampling threshold fires."""
        if not self._active:
            return False
        sample_now = self._perf_counter() if now is None else float(now)
        previous_observed = self._last_observed_pose
        if previous_observed is not None:
            self._total_distance_m += _position_distance(previous_observed, pose)
        self._last_observed_pose = pose
        self._last_observed_at = sample_now

        last_sample = self._last_sample_pose
        if last_sample is None:
            return self._record_pose(pose, sample_now, kind="sample", force=True)
        elapsed_s = max(0.0, sample_now - self._last_sample_at)
        distance_m = _position_distance(last_sample, pose)
        angle_deg = _orientation_angle_deg(last_sample, pose)
        changed = distance_m > 1e-6 or angle_deg > 1e-4
        threshold_reached = (
            distance_m >= self.sample_distance_m
            or angle_deg >= self.sample_angle_deg
            or (changed and elapsed_s >= self.sample_interval_s)
            or elapsed_s >= self.heartbeat_interval_s
        )
        if not threshold_reached:
            return False
        return self._record_pose(pose, sample_now, kind="sample", force=False)

    def mark_discontinuity(
        self,
        before: ManualDivePose,
        after: ManualDivePose,
        *,
        reason: str,
        now: float | None = None,
    ) -> bool:
        """Record a teleport/bookmark jump without counting it as flown distance."""
        if not self._active:
            return False
        sample_now = self._perf_counter() if now is None else float(now)
        teleport_distance_m = _position_distance(before, after)
        self._teleport_distance_m += teleport_distance_m
        self._discontinuity_count += 1
        self._last_observed_pose = after
        self._last_observed_at = sample_now
        return self._record_pose(
            after,
            sample_now,
            kind="discontinuity",
            force=True,
            extra={
                "reason": str(reason),
                "from_position": list(before.position),
                "teleport_distance_m": teleport_distance_m,
            },
        )

    def stop(
        self,
        pose: ManualDivePose | None,
        *,
        reason: str = "user_stopped",
        now: float | None = None,
    ) -> str:
        """Finish sampling and ask the background writer to publish the trace."""
        if not self._active:
            return self.output_path
        stopped_at = self._perf_counter() if now is None else float(now)
        if pose is not None:
            previous_observed = self._last_observed_pose
            if previous_observed is not None:
                self._total_distance_m += _position_distance(
                    previous_observed,
                    pose,
                )
            self._last_observed_pose = pose
            self._last_observed_at = stopped_at
            self._record_pose(
                pose,
                stopped_at,
                kind="sample",
                force=True,
                extra={"final": True},
            )
        self._active = False
        final_pose = self._last_observed_pose
        self._final_record = {
            "record": "trace_completed",
            "schema_version": MANUAL_DIVE_TRACE_SCHEMA_VERSION,
            "session_id": self._session_id,
            "started_at_utc": self._started_utc,
            "completed_at_utc": self._utc_now()
            .astimezone(timezone.utc)
            .isoformat(timespec="milliseconds"),
            "reason": str(reason),
            "duration_s": max(0.0, stopped_at - self._started_at),
            "sample_count": self._sample_count,
            "dropped_sample_count": self._dropped_sample_count,
            "sample_cap_reached": self._sample_count >= self.sample_cap,
            "total_flown_distance_m": self._total_distance_m,
            "discontinuity_count": self._discontinuity_count,
            "teleport_distance_m": self._teleport_distance_m,
            "final_pose": None if final_pose is None else final_pose.payload(),
            "bounds": _bounds_payload(self._minimum, self._maximum),
        }
        self._enqueue_stop()
        return self.output_path

    def cancel(self) -> bool:
        """Stop the writer and discard every queued or on-disk trace artifact."""
        if self._thread is None:
            return False
        if self._writer_done.is_set():
            self._cancel_requested.set()
            self._active = False
            # Publication may finish between the user's stop and Escape key.
            # Move late deletion back to a worker so the render thread never
            # performs filesystem cleanup.
            self._writer_done.clear()
            self._thread = threading.Thread(
                target=self._cleanup_published_cancellation,
                name="caveviewer-manual-dive-trace-cancel",
                daemon=False,
            )
            try:
                self._thread.start()
            except RuntimeError as exc:
                with self._result_lock:
                    self._cancel_cleanup_error = (
                        f"Could not start trace cleanup: {exc}"
                    )
                self._writer_done.set()
            return True
        self._cancel_requested.set()
        self._active = False
        self._final_record = None
        # Releasing queued pose dictionaries here bounds cancellation memory;
        # the writer still owns any record it is currently flushing.
        self._discard_queued_records()
        self._enqueue_stop()
        return True

    def poll_result(self) -> ManualDiveTraceResult | None:
        if not self._writer_done.is_set():
            return None
        canceled = self._cancel_requested.is_set()
        with self._result_lock:
            error = (
                self._cancel_cleanup_error if canceled else self._writer_error
            )
        return ManualDiveTraceResult(
            output_path=self.output_path,
            partial_path=os.fspath(self._partial_path),
            completed=(
                not canceled
                and error is None
                and self._output_path.exists()
            ),
            error=error,
            canceled=canceled,
        )

    def wait(self, timeout: float = 2.0) -> ManualDiveTraceResult | None:
        """Test/CLI seam; the OpenGL render thread polls instead of joining."""
        self._writer_done.wait(max(0.0, float(timeout)))
        return self.poll_result()

    def _record_pose(
        self,
        pose: ManualDivePose,
        sample_now: float,
        *,
        kind: str,
        force: bool,
        extra: Mapping[str, Any] | None = None,
    ) -> bool:
        if self._sample_count >= self.sample_cap and not force:
            self._dropped_sample_count += 1
            return False
        if self._sample_count >= self.sample_cap and force:
            self._discard_one_queued_sample()
        elapsed_s = max(0.0, sample_now - self._started_at)
        record = {
            "record": kind,
            "schema_version": MANUAL_DIVE_TRACE_SCHEMA_VERSION,
            "session_id": self._session_id,
            "sample_index": self._sample_count,
            "elapsed_s": elapsed_s,
            **pose.payload(),
            **dict(extra or {}),
        }
        if not self._enqueue_sample(record):
            return False
        self._sample_count += 1
        self._last_sample_pose = pose
        self._last_sample_at = sample_now
        position = np.asarray(pose.position, dtype=np.float64)
        self._minimum = np.minimum(self._minimum, position)
        self._maximum = np.maximum(self._maximum, position)
        return True

    def _enqueue_sample(self, record: dict[str, Any]) -> bool:
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            self._dropped_sample_count += 1
            return False

    def _discard_one_queued_sample(self) -> None:
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            return
        if item is _STOP_WRITER:
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                pass
            return
        self._dropped_sample_count += 1

    def _discard_queued_records(self) -> None:
        """Release all queued trace records before cancellation finalizes."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _enqueue_stop(self) -> None:
        while True:
            try:
                self._queue.put_nowait(_STOP_WRITER)
                return
            except queue.Full:
                self._discard_one_queued_sample()

    def _write_records(self, header: dict[str, Any]) -> None:
        try:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            with self._partial_path.open("x", encoding="utf-8") as file_obj:
                _write_jsonl_record(file_obj, header)
                file_obj.flush()
                while True:
                    record = self._queue.get()
                    if record is _STOP_WRITER:
                        if self._cancel_requested.is_set():
                            break
                        final_record = dict(self._final_record or {})
                        final_record["sample_count"] = self._sample_count
                        final_record["dropped_sample_count"] = (
                            self._dropped_sample_count
                        )
                        _write_jsonl_record(file_obj, final_record)
                        file_obj.flush()
                        break
                    _write_jsonl_record(file_obj, record)
                    file_obj.flush()
            # Windows cannot replace a file that this process still has open.
            # Closing the temporary sibling before publishing preserves the
            # atomic replacement guarantee on every supported platform.
            if not self._cancel_requested.is_set():
                os.replace(self._partial_path, self._output_path)
        except Exception as exc:
            with self._result_lock:
                self._writer_error = f"{type(exc).__name__}: {exc}"
        finally:
            if self._cancel_requested.is_set():
                cleanup_error = self._remove_canceled_artifacts()
                with self._result_lock:
                    self._cancel_cleanup_error = cleanup_error
            self._writer_done.set()

    def _remove_canceled_artifacts(self) -> str | None:
        """Remove private and published paths after a cancellation request."""
        failures: list[str] = []
        for path in (self._partial_path, self._output_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                failures.append(f"{path}: {type(exc).__name__}: {exc}")
        if not failures:
            return None
        return "Could not remove canceled trace files: " + "; ".join(failures)

    def _cleanup_published_cancellation(self) -> None:
        """Remove a just-published trace without blocking the render thread."""
        cleanup_error: str | None = None
        try:
            cleanup_error = self._remove_canceled_artifacts()
        except Exception as exc:
            cleanup_error = f"Could not remove canceled trace files: {exc}"
        finally:
            with self._result_lock:
                self._cancel_cleanup_error = cleanup_error
            # Always release capture ownership, even if an unexpected cleanup
            # implementation failure escaped the normal per-path handling.
            self._writer_done.set()


def _write_jsonl_record(file_obj: Any, record: Mapping[str, Any]) -> None:
    file_obj.write(
        json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    )
    file_obj.write("\n")


def _vector3(value: Sequence[float] | np.ndarray) -> tuple[float, float, float]:
    array = np.asarray(value, dtype=np.float64).reshape(3)
    return (
        _finite_float(array[0]),
        _finite_float(array[1]),
        _finite_float(array[2]),
    )


def _finite_float(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("manual dive trace values must be finite")
    return result


def _position_distance(first: ManualDivePose, second: ManualDivePose) -> float:
    return float(
        np.linalg.norm(
            np.asarray(second.position, dtype=np.float64)
            - np.asarray(first.position, dtype=np.float64)
        )
    )


def _orientation_angle_deg(first: ManualDivePose, second: ManualDivePose) -> float:
    dot = float(
        np.dot(
            np.asarray(first.forward, dtype=np.float64),
            np.asarray(second.forward, dtype=np.float64),
        )
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def _bounds_payload(
    minimum: np.ndarray,
    maximum: np.ndarray,
) -> dict[str, list[float]] | None:
    if not np.all(np.isfinite(minimum)) or not np.all(np.isfinite(maximum)):
        return None
    return {
        "minimum": [float(value) for value in minimum],
        "maximum": [float(value) for value in maximum],
    }
