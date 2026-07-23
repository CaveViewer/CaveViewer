"""Deterministic viewer benchmark scenarios, frame metrics, and comparisons.

This module is intentionally split from the OpenGL window. Unit tests exercise
scenario parsing, route interpolation, summary calculation, and regression
thresholds without needing a GPU, while `viewer_window.py` uses the controller
here to drive and record real render-loop benchmark runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np


SCENARIO_VERSION = 1
THRESHOLDS_VERSION = 1
DEFAULT_WARMUP_SECONDS = 5.0
DEFAULT_MEASUREMENT_SECONDS = 30.0
DEFAULT_MAX_RUNTIME_MARGIN_SECONDS = 30.0
DEFAULT_WINDOW_SIZE = (1600, 1000)
DEFAULT_RENDER_DISTANCE = 3
DEFAULT_STUTTER_THRESHOLDS_MS = (33.3, 50.0, 100.0)


class BenchmarkConfigurationError(ValueError):
    """Raised when benchmark inputs are missing or invalid."""


@dataclass(frozen=True)
class BenchmarkKeyframe:
    """One camera pose in a benchmark route."""

    time_s: float
    position: tuple[float, float, float]
    yaw_deg: float
    pitch_deg: float
    roll_deg: float = 0.0

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, index: int) -> "BenchmarkKeyframe":
        try:
            time_s = float(payload["time_s"])
            position = _float_tuple(payload["position"], length=3, field=f"route[{index}].position")
            yaw_deg = float(payload.get("yaw_deg", payload.get("yaw", 0.0)))
            pitch_deg = float(payload.get("pitch_deg", payload.get("pitch", 0.0)))
            roll_deg = float(payload.get("roll_deg", payload.get("roll", 0.0)))
        except KeyError as exc:
            raise BenchmarkConfigurationError(
                f"route[{index}] is missing required field {exc.args[0]!r}"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise BenchmarkConfigurationError(f"route[{index}] contains invalid values") from exc
        if time_s < 0:
            raise BenchmarkConfigurationError(f"route[{index}].time_s must be non-negative")
        return cls(
            time_s=time_s,
            position=position,
            yaw_deg=yaw_deg,
            pitch_deg=pitch_deg,
            roll_deg=roll_deg,
        )

    def identity_payload(self) -> dict[str, Any]:
        """Return the route content that affects benchmark comparability."""
        return {
            "time_s": self.time_s,
            "position": list(self.position),
            "yaw_deg": self.yaw_deg,
            "pitch_deg": self.pitch_deg,
            "roll_deg": self.roll_deg,
        }


@dataclass(frozen=True)
class BenchmarkScenario:
    """A deterministic route and measurement policy for one benchmark run."""

    name: str
    route: tuple[BenchmarkKeyframe, ...]
    warmup_seconds: float = DEFAULT_WARMUP_SECONDS
    measurement_seconds: float = DEFAULT_MEASUREMENT_SECONDS
    max_runtime_seconds: float | None = None
    window_size: tuple[int, int] = DEFAULT_WINDOW_SIZE
    render_distance: int = DEFAULT_RENDER_DISTANCE
    sample_every_n_frames: int = 1
    stutter_thresholds_ms: tuple[float, ...] = DEFAULT_STUTTER_THRESHOLDS_MS
    position_mode: str = "absolute"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_duration_seconds(self) -> float:
        return self.warmup_seconds + self.measurement_seconds

    @property
    def resolved_max_runtime_seconds(self) -> float:
        if self.max_runtime_seconds is not None:
            return self.max_runtime_seconds
        return self.total_duration_seconds + DEFAULT_MAX_RUNTIME_MARGIN_SECONDS

    @property
    def identity_payload(self) -> dict[str, Any]:
        """Return normalized scenario fields that must match for comparison."""
        return {
            "version": SCENARIO_VERSION,
            "name": self.name,
            "warmup_seconds": self.warmup_seconds,
            "measurement_seconds": self.measurement_seconds,
            "max_runtime_seconds": self.max_runtime_seconds,
            "window_size": list(self.window_size),
            "render_distance": self.render_distance,
            "sample_every_n_frames": self.sample_every_n_frames,
            "stutter_thresholds_ms": list(self.stutter_thresholds_ms),
            "position_mode": self.position_mode,
            "route": [frame.identity_payload() for frame in self.route],
        }

    @property
    def fingerprint(self) -> str:
        """Stable SHA-256 of the benchmark-affecting scenario configuration."""
        return _stable_json_sha256(self.identity_payload)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BenchmarkScenario":
        try:
            version = int(payload.get("version", SCENARIO_VERSION))
        except (TypeError, ValueError) as exc:
            raise BenchmarkConfigurationError("version must be an integer") from exc
        if version != SCENARIO_VERSION:
            raise BenchmarkConfigurationError(
                f"unsupported benchmark scenario version {version}; expected {SCENARIO_VERSION}"
            )

        name = str(payload.get("name", "")).strip()
        if not name:
            raise BenchmarkConfigurationError("benchmark scenario requires a non-empty name")

        route_payload = payload.get("route")
        if not isinstance(route_payload, list) or not route_payload:
            raise BenchmarkConfigurationError("benchmark scenario requires at least one route keyframe")
        route = tuple(
            BenchmarkKeyframe.from_mapping(frame, index=index)
            for index, frame in enumerate(route_payload)
        )
        for previous, current in zip(route, route[1:]):
            if current.time_s <= previous.time_s:
                raise BenchmarkConfigurationError("route keyframe time_s values must be strictly increasing")

        warmup_seconds = _non_negative_float(
            payload.get("warmup_seconds", DEFAULT_WARMUP_SECONDS),
            "warmup_seconds",
        )
        measurement_seconds = _positive_float(
            payload.get("measurement_seconds", DEFAULT_MEASUREMENT_SECONDS),
            "measurement_seconds",
        )
        max_runtime_raw = payload.get("max_runtime_seconds")
        max_runtime_seconds = (
            None
            if max_runtime_raw is None
            else _positive_float(max_runtime_raw, "max_runtime_seconds")
        )
        if max_runtime_seconds is not None and max_runtime_seconds < warmup_seconds + measurement_seconds:
            raise BenchmarkConfigurationError(
                "max_runtime_seconds must be at least warmup_seconds + measurement_seconds"
            )

        window_size = _int_tuple(
            payload.get("window_size", DEFAULT_WINDOW_SIZE),
            length=2,
            field="window_size",
        )
        if window_size[0] <= 0 or window_size[1] <= 0:
            raise BenchmarkConfigurationError("window_size dimensions must be positive")

        try:
            render_distance = int(
                payload.get("render_distance", DEFAULT_RENDER_DISTANCE)
            )
        except (TypeError, ValueError) as exc:
            raise BenchmarkConfigurationError("render_distance must be an integer") from exc
        if render_distance <= 0:
            raise BenchmarkConfigurationError("render_distance must be positive")
        try:
            sample_every_n_frames = int(payload.get("sample_every_n_frames", 1))
        except (TypeError, ValueError) as exc:
            raise BenchmarkConfigurationError(
                "sample_every_n_frames must be an integer"
            ) from exc
        if sample_every_n_frames <= 0:
            raise BenchmarkConfigurationError("sample_every_n_frames must be positive")

        stutter_thresholds_ms = tuple(
            sorted(
                _positive_float(value, "stutter_thresholds_ms")
                for value in payload.get("stutter_thresholds_ms", DEFAULT_STUTTER_THRESHOLDS_MS)
            )
        )
        position_mode = str(payload.get("position_mode", "absolute")).strip().lower()
        if position_mode not in {"absolute", "first_chunk_center_offset"}:
            raise BenchmarkConfigurationError(
                "position_mode must be 'absolute' or 'first_chunk_center_offset'"
            )

        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise BenchmarkConfigurationError("metadata must be an object when provided")

        return cls(
            name=name,
            route=route,
            warmup_seconds=warmup_seconds,
            measurement_seconds=measurement_seconds,
            max_runtime_seconds=max_runtime_seconds,
            window_size=window_size,
            render_distance=render_distance,
            sample_every_n_frames=sample_every_n_frames,
            stutter_thresholds_ms=stutter_thresholds_ms,
            position_mode=position_mode,
            metadata=dict(metadata),
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "BenchmarkScenario":
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except OSError as exc:
            raise BenchmarkConfigurationError(f"could not read benchmark scenario {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise BenchmarkConfigurationError(f"benchmark scenario {path} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise BenchmarkConfigurationError("benchmark scenario root must be a JSON object")
        return cls.from_mapping(payload)

    def pose_at(self, elapsed_s: float) -> BenchmarkKeyframe:
        """Return the interpolated camera pose for a benchmark elapsed time."""
        elapsed_s = max(0.0, float(elapsed_s))
        if elapsed_s <= self.route[0].time_s or len(self.route) == 1:
            return self.route[0]
        if elapsed_s >= self.route[-1].time_s:
            return self.route[-1]

        for previous, current in zip(self.route, self.route[1:]):
            if previous.time_s <= elapsed_s <= current.time_s:
                span = max(1e-9, current.time_s - previous.time_s)
                t = (elapsed_s - previous.time_s) / span
                position = tuple(
                    _lerp(previous.position[index], current.position[index], t)
                    for index in range(3)
                )
                return BenchmarkKeyframe(
                    time_s=elapsed_s,
                    position=position,
                    yaw_deg=_lerp_angle_degrees(previous.yaw_deg, current.yaw_deg, t),
                    pitch_deg=_lerp(previous.pitch_deg, current.pitch_deg, t),
                    roll_deg=_lerp_angle_degrees(previous.roll_deg, current.roll_deg, t),
                )

        return self.route[-1]


@dataclass(frozen=True)
class BenchmarkFrameSample:
    """One measured frame from a benchmark run."""

    frame_index: int
    elapsed_s: float
    measured_elapsed_s: float
    frame_ms: float
    fps: float
    streaming_ms: float
    scene_setup_ms: float
    mesh_draw_ms: float
    mesh_cull_ms: float
    mesh_submit_ms: float
    overlay_ms: float
    other_ms: float
    drawn_chunks: int
    resident_chunks: int
    loaded_chunks: int
    pending_chunks: int
    ready_chunks: int
    unload_pending_chunks: int
    wanted_chunks: int
    chunks_uploaded: int
    chunks_unloaded: int
    bytes_uploaded: int
    upload_stalls: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "elapsed_s": round(self.elapsed_s, 6),
            "measured_elapsed_s": round(self.measured_elapsed_s, 6),
            "frame_ms": round(self.frame_ms, 6),
            "fps": round(self.fps, 6),
            "streaming_ms": round(self.streaming_ms, 6),
            "scene_setup_ms": round(self.scene_setup_ms, 6),
            "mesh_draw_ms": round(self.mesh_draw_ms, 6),
            "mesh_cull_ms": round(self.mesh_cull_ms, 6),
            "mesh_submit_ms": round(self.mesh_submit_ms, 6),
            "overlay_ms": round(self.overlay_ms, 6),
            "other_ms": round(self.other_ms, 6),
            "drawn_chunks": self.drawn_chunks,
            "resident_chunks": self.resident_chunks,
            "loaded_chunks": self.loaded_chunks,
            "pending_chunks": self.pending_chunks,
            "ready_chunks": self.ready_chunks,
            "unload_pending_chunks": self.unload_pending_chunks,
            "wanted_chunks": self.wanted_chunks,
            "chunks_uploaded": self.chunks_uploaded,
            "chunks_unloaded": self.chunks_unloaded,
            "bytes_uploaded": self.bytes_uploaded,
            "upload_stalls": self.upload_stalls,
        }


@dataclass(frozen=True)
class BenchmarkThresholds:
    """Allowed candidate-vs-baseline performance deltas."""

    max_median_fps_drop_pct: float = 5.0
    max_one_percent_low_fps_drop_pct: float = 10.0
    max_p95_frame_ms_increase_pct: float = 15.0
    max_stutter_frame_increase_pct: float = 20.0

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BenchmarkThresholds":
        if "thresholds" in payload:
            try:
                version = int(payload.get("version", THRESHOLDS_VERSION))
            except (TypeError, ValueError) as exc:
                raise BenchmarkConfigurationError(
                    "threshold version must be an integer"
                ) from exc
            if version != THRESHOLDS_VERSION:
                raise BenchmarkConfigurationError(
                    f"unsupported benchmark threshold version {version}; "
                    f"expected {THRESHOLDS_VERSION}"
                )
            threshold_payload = payload["thresholds"]
            if not isinstance(threshold_payload, Mapping):
                raise BenchmarkConfigurationError("thresholds must be an object")
            payload = threshold_payload
        try:
            return cls(
                max_median_fps_drop_pct=float(
                    payload.get("max_median_fps_drop_pct", 5.0)
                ),
                max_one_percent_low_fps_drop_pct=float(
                    payload.get("max_one_percent_low_fps_drop_pct", 10.0)
                ),
                max_p95_frame_ms_increase_pct=float(
                    payload.get("max_p95_frame_ms_increase_pct", 15.0)
                ),
                max_stutter_frame_increase_pct=float(
                    payload.get("max_stutter_frame_increase_pct", 20.0)
                ),
            )
        except (TypeError, ValueError) as exc:
            raise BenchmarkConfigurationError(
                "benchmark thresholds must contain numeric values"
            ) from exc

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "BenchmarkThresholds":
        return cls.from_mapping(load_json_file(path))

    def with_overrides(
        self,
        *,
        max_median_fps_drop_pct: float | None = None,
        max_one_percent_low_fps_drop_pct: float | None = None,
        max_p95_frame_ms_increase_pct: float | None = None,
        max_stutter_frame_increase_pct: float | None = None,
    ) -> "BenchmarkThresholds":
        """Return a copy with non-None command-line overrides applied."""
        return BenchmarkThresholds(
            max_median_fps_drop_pct=(
                self.max_median_fps_drop_pct
                if max_median_fps_drop_pct is None
                else float(max_median_fps_drop_pct)
            ),
            max_one_percent_low_fps_drop_pct=(
                self.max_one_percent_low_fps_drop_pct
                if max_one_percent_low_fps_drop_pct is None
                else float(max_one_percent_low_fps_drop_pct)
            ),
            max_p95_frame_ms_increase_pct=(
                self.max_p95_frame_ms_increase_pct
                if max_p95_frame_ms_increase_pct is None
                else float(max_p95_frame_ms_increase_pct)
            ),
            max_stutter_frame_increase_pct=(
                self.max_stutter_frame_increase_pct
                if max_stutter_frame_increase_pct is None
                else float(max_stutter_frame_increase_pct)
            ),
        )


class BenchmarkController:
    """Drive a benchmark camera route and persist measured frame metrics."""

    def __init__(
        self,
        *,
        scenario: BenchmarkScenario,
        output_dir: str | os.PathLike[str],
        logger,
        perf_counter,
        environment: Mapping[str, Any] | None = None,
    ) -> None:
        self.scenario = scenario
        self.output_dir = Path(output_dir)
        self.logger = logger
        self.perf_counter = perf_counter
        self.environment = dict(environment or {})
        self._started_at: float | None = None
        self._prepared_at: float | None = None
        self._frame_index = 0
        self._samples: list[BenchmarkFrameSample] = []
        self._frames_handle = None
        self._finished = False
        self._position_origin = np.zeros(3, dtype=np.float64)
        self._summary_path = self.output_dir / "summary.json"
        self._frames_path = self.output_dir / "frames.jsonl"
        self._environment_path = self.output_dir / "environment.json"

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def started(self) -> bool:
        return self._started_at is not None

    @property
    def summary_path(self) -> Path:
        return self._summary_path

    def prepare_output(self) -> None:
        """Create benchmark output files and write static environment data."""
        if self._prepared_at is None:
            self._prepared_at = self.perf_counter()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(self._environment_path, self.environment)
        if self._frames_handle is None:
            self._frames_handle = open(self._frames_path, "w", encoding="utf-8")

    def set_position_origin(self, origin: Iterable[float]) -> None:
        """Set the map-relative origin used by offset-based scenarios."""
        if self.scenario.position_mode == "first_chunk_center_offset":
            self._position_origin = np.asarray(tuple(origin), dtype=np.float64)

    def apply_initial_camera(self, camera) -> None:
        """Place the camera at the first scenario keyframe before streaming starts."""
        apply_pose_to_camera(
            camera,
            self.scenario.route[0],
            position_origin=self._position_origin,
        )

    def update_camera(self, camera, now: float | None = None) -> float:
        """Apply the current route pose to the viewer camera."""
        now = self.perf_counter() if now is None else float(now)
        if self._started_at is None:
            self.prepare_output()
            self._started_at = now
            self.logger.info(
                "Benchmark started: scenario=%s warmup=%.1fs measurement=%.1fs output=%s",
                self.scenario.name,
                self.scenario.warmup_seconds,
                self.scenario.measurement_seconds,
                self.output_dir,
            )
        elapsed_s = now - self._started_at
        apply_pose_to_camera(
            camera,
            self.scenario.pose_at(elapsed_s),
            position_origin=self._position_origin,
        )
        return elapsed_s

    def record_frame(
        self,
        *,
        now: float,
        frame_ms: float,
        streaming_ms: float,
        scene_setup_ms: float,
        mesh_draw_ms: float,
        mesh_cull_ms: float,
        mesh_submit_ms: float,
        overlay_ms: float,
        other_ms: float,
        drawn_chunks: int,
        resident_chunks: int,
        world_stats: Mapping[str, Any],
        streaming_timing: Mapping[str, Any],
    ) -> bool:
        """Record one full-scene frame. Return true when the run is complete."""
        if self._finished:
            return True
        if self._started_at is None:
            self._started_at = float(now)
        elapsed_s = float(now) - self._started_at
        self._frame_index += 1
        if elapsed_s < self.scenario.warmup_seconds:
            return False
        should_finish = self.should_finish(now)
        if self._frame_index % self.scenario.sample_every_n_frames != 0:
            return should_finish

        frame_ms = max(1e-9, float(frame_ms))
        sample = BenchmarkFrameSample(
            frame_index=self._frame_index,
            elapsed_s=elapsed_s,
            measured_elapsed_s=elapsed_s - self.scenario.warmup_seconds,
            frame_ms=frame_ms,
            fps=1000.0 / frame_ms,
            streaming_ms=float(streaming_ms),
            scene_setup_ms=float(scene_setup_ms),
            mesh_draw_ms=float(mesh_draw_ms),
            mesh_cull_ms=float(mesh_cull_ms),
            mesh_submit_ms=float(mesh_submit_ms),
            overlay_ms=float(overlay_ms),
            other_ms=float(other_ms),
            drawn_chunks=int(drawn_chunks),
            resident_chunks=int(resident_chunks),
            loaded_chunks=int(world_stats.get("loaded", 0)),
            pending_chunks=int(world_stats.get("pending", 0)),
            ready_chunks=int(world_stats.get("ready", 0)),
            unload_pending_chunks=int(world_stats.get("unload_pending", 0)),
            wanted_chunks=int(world_stats.get("wanted", 0)),
            chunks_uploaded=int(streaming_timing.get("chunks_uploaded", 0)),
            chunks_unloaded=int(streaming_timing.get("chunks_unloaded", 0)),
            bytes_uploaded=int(streaming_timing.get("bytes_uploaded", 0)),
            upload_stalls=int(streaming_timing.get("upload_stalls", 0)),
        )
        self._samples.append(sample)
        if self._frames_handle is None:
            self.prepare_output()
        assert self._frames_handle is not None
        self._frames_handle.write(json.dumps(sample.as_dict(), sort_keys=True) + "\n")
        self._frames_handle.flush()
        return should_finish

    def should_finish(self, now: float) -> bool:
        if self._started_at is None:
            return False
        elapsed_s = float(now) - self._started_at
        return elapsed_s >= self.scenario.total_duration_seconds

    def exceeded_max_runtime(self, now: float) -> bool:
        anchor = self._started_at if self._started_at is not None else self._prepared_at
        if anchor is None:
            return False
        elapsed_s = float(now) - anchor
        return elapsed_s >= self.scenario.resolved_max_runtime_seconds

    def finish(self, *, reason: str = "completed") -> dict[str, Any]:
        """Write summary output and close frame artifacts."""
        if self._finished:
            return load_json_file(self._summary_path)
        self._finished = True
        if self._frames_handle is not None:
            self._frames_handle.close()
            self._frames_handle = None
        summary = summarize_samples(
            self._samples,
            scenario=self.scenario,
            environment=self.environment,
            reason=reason,
        )
        self._write_json(self._summary_path, summary)
        self.logger.info(
            "Benchmark finished: scenario=%s reason=%s measured_frames=%d "
            "median_fps=%.2f one_percent_low_fps=%.2f p95_frame_ms=%.2f summary=%s",
            self.scenario.name,
            reason,
            summary["measured_frames"],
            summary["metrics"]["median_fps"],
            summary["metrics"]["one_percent_low_fps"],
            summary["metrics"]["p95_frame_ms"],
            self._summary_path,
        )
        return summary

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")


def apply_pose_to_camera(
    camera,
    pose: BenchmarkKeyframe,
    *,
    position_origin: Iterable[float] | None = None,
) -> None:
    """Apply one benchmark pose to a FlyCamera-like object."""
    origin = (
        np.zeros(3, dtype=np.float64)
        if position_origin is None
        else np.asarray(tuple(position_origin), dtype=np.float64)
    )
    camera.position = np.array(pose.position, dtype=np.float64) + origin
    camera.yaw = math.radians(pose.yaw_deg)
    camera.pitch = math.radians(pose.pitch_deg)
    camera.roll = math.radians(pose.roll_deg)


def summarize_samples(
    samples: Iterable[BenchmarkFrameSample],
    *,
    scenario: BenchmarkScenario,
    environment: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Return machine-readable benchmark summary metrics."""
    sample_list = list(samples)
    frame_ms_values = [sample.frame_ms for sample in sample_list]
    fps_values = [sample.fps for sample in sample_list]
    measured_seconds = sum(frame_ms_values) / 1000.0
    measured_frames = len(sample_list)
    stutter_counts = {
        f"over_{_threshold_label(threshold)}": sum(
            1 for value in frame_ms_values if value > threshold
        )
        for threshold in scenario.stutter_thresholds_ms
    }
    metrics = {
        "mean_fps": _round_metric((measured_frames / measured_seconds) if measured_seconds > 0 else 0.0),
        "median_fps": _round_metric(_median(fps_values)),
        "one_percent_low_fps": _round_metric(_fps_from_frame_percentile(frame_ms_values, 99.0)),
        "point_one_percent_low_fps": _round_metric(_fps_from_frame_percentile(frame_ms_values, 99.9)),
        "min_fps": _round_metric(min(fps_values) if fps_values else 0.0),
        "median_frame_ms": _round_metric(_median(frame_ms_values)),
        "p95_frame_ms": _round_metric(_percentile(frame_ms_values, 95.0)),
        "p99_frame_ms": _round_metric(_percentile(frame_ms_values, 99.0)),
        "max_frame_ms": _round_metric(max(frame_ms_values) if frame_ms_values else 0.0),
        "measured_seconds": _round_metric(measured_seconds),
        "stutter_counts": stutter_counts,
    }
    return {
        "schema_version": 1,
        "scenario": {
            "name": scenario.name,
            "fingerprint": scenario.fingerprint,
            "warmup_seconds": scenario.warmup_seconds,
            "measurement_seconds": scenario.measurement_seconds,
            "render_distance": scenario.render_distance,
            "window_size": list(scenario.window_size),
            "position_mode": scenario.position_mode,
            "metadata": scenario.metadata,
        },
        "reason": reason,
        "measured_frames": measured_frames,
        "metrics": metrics,
        "environment": dict(environment),
    }


def compare_summaries(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    thresholds: BenchmarkThresholds | None = None,
) -> dict[str, Any]:
    """Compare candidate benchmark metrics against a baseline summary."""
    thresholds = BenchmarkThresholds() if thresholds is None else thresholds
    baseline_metrics = baseline.get("metrics", {})
    candidate_metrics = candidate.get("metrics", {})
    checks = _compatibility_checks(baseline, candidate) + [
        _drop_check(
            "median_fps",
            baseline_metrics.get("median_fps", 0.0),
            candidate_metrics.get("median_fps", 0.0),
            thresholds.max_median_fps_drop_pct,
        ),
        _drop_check(
            "one_percent_low_fps",
            baseline_metrics.get("one_percent_low_fps", 0.0),
            candidate_metrics.get("one_percent_low_fps", 0.0),
            thresholds.max_one_percent_low_fps_drop_pct,
        ),
        _increase_check(
            "p95_frame_ms",
            baseline_metrics.get("p95_frame_ms", 0.0),
            candidate_metrics.get("p95_frame_ms", 0.0),
            thresholds.max_p95_frame_ms_increase_pct,
        ),
    ]

    baseline_stutters = baseline_metrics.get("stutter_counts", {})
    candidate_stutters = candidate_metrics.get("stutter_counts", {})
    for key in sorted(set(baseline_stutters) | set(candidate_stutters)):
        checks.append(
            _increase_check(
                f"stutter_counts.{key}",
                baseline_stutters.get(key, 0),
                candidate_stutters.get(key, 0),
                thresholds.max_stutter_frame_increase_pct,
            )
        )

    passed = all(check["passed"] for check in checks)
    return {
        "schema_version": 1,
        "passed": passed,
        "checks": checks,
        "thresholds": {
            "max_median_fps_drop_pct": thresholds.max_median_fps_drop_pct,
            "max_one_percent_low_fps_drop_pct": thresholds.max_one_percent_low_fps_drop_pct,
            "max_p95_frame_ms_increase_pct": thresholds.max_p95_frame_ms_increase_pct,
            "max_stutter_frame_increase_pct": thresholds.max_stutter_frame_increase_pct,
        },
        "baseline": {
            "scenario": baseline.get("scenario", {}).get("name"),
            "metrics": baseline_metrics,
        },
        "candidate": {
            "scenario": candidate.get("scenario", {}).get("name"),
            "metrics": candidate_metrics,
        },
    }


def load_json_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise BenchmarkConfigurationError(f"{path} must contain a JSON object")
    return payload


def _compatibility_checks(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> list[dict[str, Any]]:
    baseline_scenario = baseline.get("scenario", {})
    candidate_scenario = candidate.get("scenario", {})
    baseline_environment = baseline.get("environment", {})
    candidate_environment = candidate.get("environment", {})
    return [
        _matching_value_check(
            "scenario.name",
            baseline_scenario.get("name"),
            candidate_scenario.get("name"),
        ),
        _matching_value_check(
            "scenario.fingerprint",
            baseline_scenario.get("fingerprint"),
            candidate_scenario.get("fingerprint"),
        ),
        _matching_value_check(
            "environment.cache_manifest_sha256",
            baseline_environment.get("cache_manifest_sha256"),
            candidate_environment.get("cache_manifest_sha256"),
        ),
    ]


def _matching_value_check(name: str, baseline_value: Any, candidate_value: Any) -> dict[str, Any]:
    baseline_text = str(baseline_value or "")
    candidate_text = str(candidate_value or "")
    passed = bool(baseline_text) and baseline_text == candidate_text
    return {
        "metric": name,
        "kind": "compatibility",
        "baseline": baseline_text or "<missing>",
        "candidate": candidate_text or "<missing>",
        "passed": passed,
    }


def _drop_check(name: str, baseline_value: Any, candidate_value: Any, max_drop_pct: float) -> dict[str, Any]:
    baseline_float = float(baseline_value or 0.0)
    candidate_float = float(candidate_value or 0.0)
    delta_pct = _percent_delta(baseline_float, candidate_float)
    drop_pct = max(0.0, -delta_pct)
    return {
        "metric": name,
        "baseline": _round_metric(baseline_float),
        "candidate": _round_metric(candidate_float),
        "delta_pct": _round_metric(delta_pct),
        "allowed_drop_pct": _round_metric(max_drop_pct),
        "passed": drop_pct <= max_drop_pct,
    }


def _increase_check(name: str, baseline_value: Any, candidate_value: Any, max_increase_pct: float) -> dict[str, Any]:
    baseline_float = float(baseline_value or 0.0)
    candidate_float = float(candidate_value or 0.0)
    delta_pct = _percent_delta(baseline_float, candidate_float)
    increase_pct = max(0.0, delta_pct)
    return {
        "metric": name,
        "baseline": _round_metric(baseline_float),
        "candidate": _round_metric(candidate_float),
        "delta_pct": _round_metric(delta_pct),
        "allowed_increase_pct": _round_metric(max_increase_pct),
        "passed": increase_pct <= max_increase_pct,
    }


def _percent_delta(baseline: float, candidate: float) -> float:
    if baseline == 0:
        if candidate == 0:
            return 0.0
        return 100.0
    return ((candidate - baseline) / abs(baseline)) * 100.0


def _float_tuple(value: Any, *, length: int, field: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise BenchmarkConfigurationError(f"{field} must be a {length}-item array")
    try:
        return tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise BenchmarkConfigurationError(f"{field} must contain numbers") from exc


def _int_tuple(value: Any, *, length: int, field: str) -> tuple[int, ...]:
    values = _float_tuple(value, length=length, field=field)
    return tuple(int(item) for item in values)


def _non_negative_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BenchmarkConfigurationError(f"{field} must be a number") from exc
    if number < 0:
        raise BenchmarkConfigurationError(f"{field} must be non-negative")
    return number


def _positive_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BenchmarkConfigurationError(f"{field} must be a number") from exc
    if number <= 0:
        raise BenchmarkConfigurationError(f"{field} must be positive")
    return number


def _lerp(start: float, end: float, t: float) -> float:
    return float(start) + (float(end) - float(start)) * float(t)


def _lerp_angle_degrees(start: float, end: float, t: float) -> float:
    delta = ((float(end) - float(start) + 180.0) % 360.0) - 180.0
    return float(start) + delta * float(t)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (float(percentile) / 100.0)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return sorted_values[lower]
    return _lerp(sorted_values[lower], sorted_values[upper], rank - lower)


def _fps_from_frame_percentile(frame_ms_values: list[float], percentile: float) -> float:
    frame_ms = _percentile(frame_ms_values, percentile)
    if frame_ms <= 0:
        return 0.0
    return 1000.0 / frame_ms


def _threshold_label(threshold_ms: float) -> str:
    if float(threshold_ms).is_integer():
        return f"{int(threshold_ms)}ms"
    return f"{str(threshold_ms).replace('.', '_')}ms"


def _round_metric(value: float) -> float:
    return round(float(value), 6)


def _stable_json_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
