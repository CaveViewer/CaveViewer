"""Validated Recorded Dive traces and render-thread playback state.

Recorded Dive deliberately treats a completed manual trace as the camera
authority.  It performs no route planning, smoothing, collision rejection, or
navigation clamping.  The viewer supplies only chunk-readiness decisions; this
module owns deterministic time interpolation and camera-pose application.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np

from caveviewer.core.map.cache_identity import (
    GuidedDiveCacheIdentity,
    guided_dive_cache_identity_from_manifest,
    parse_guided_dive_cache_identity,
)
from caveviewer.gui.manual_dive_trace import (
    MANUAL_DIVE_TRACE_DIRECTORY,
    MANUAL_DIVE_TRACE_SCHEMA_VERSION,
)


RECORDED_DIVE_FILE_SUFFIX = ".jsonl"
MAX_RECORDED_DIVE_BYTES = 64 * 1024 * 1024
MAX_RECORDED_DIVE_LINE_BYTES = 256 * 1024
MAX_RECORDED_DIVE_SAMPLES = 100_000


class RecordedDiveError(ValueError):
    """Base error for an unusable Recorded Dive file or map association."""


class RecordedDiveFormatError(RecordedDiveError):
    """A trace does not satisfy the bounded schema accepted for playback."""


class RecordedDiveMapError(RecordedDiveError):
    """A trace cannot be associated with a compatible local source map."""


@dataclass(frozen=True)
class RecordedDiveMapReference:
    """Bounded source-map identity retained by a manual trace header."""

    source_name: str
    manifest_version: int | None
    chunk_size_m: float | None
    triangle_count: int | None
    cache_identity: GuidedDiveCacheIdentity


@dataclass(frozen=True)
class RecordedDivePose:
    """One recorded or interpolated camera pose in manifest XYZ space."""

    elapsed_s: float
    position: tuple[float, float, float]
    forward: tuple[float, float, float]
    up: tuple[float, float, float]
    right: tuple[float, float, float]
    yaw: float
    pitch: float
    roll: float
    move_speed_m_per_second: float
    record_kind: str = "sample"
    sample_index: int | None = None


@dataclass(frozen=True)
class RecordedDiveTrace:
    """A fully validated, immutable trace ready for deterministic playback."""

    path: Path
    schema_version: int
    session_id: str
    map_reference: RecordedDiveMapReference
    poses: tuple[RecordedDivePose, ...]
    elapsed_times: tuple[float, ...]
    duration_s: float

    @property
    def initial_pose(self) -> RecordedDivePose:
        return self.poses[0]

    @property
    def final_pose(self) -> RecordedDivePose:
        return self.poses[-1]

    def pose_at(self, elapsed_s: float) -> RecordedDivePose:
        """Interpolate the authoritative trace at one playback time.

        A declared discontinuity remains an instantaneous jump: the preceding
        pose is held until the discontinuity timestamp instead of inventing a
        path through the skipped space.
        """
        elapsed = max(0.0, min(self.duration_s, _finite_float(elapsed_s, "time")))
        if elapsed <= self.elapsed_times[0]:
            return self.poses[0]
        if elapsed >= self.elapsed_times[-1]:
            return self.poses[-1]

        right_index = bisect_right(self.elapsed_times, elapsed)
        left_index = max(0, right_index - 1)
        left = self.poses[left_index]
        if left.elapsed_s == elapsed or right_index >= len(self.poses):
            return left
        right = self.poses[right_index]
        if right.record_kind == "discontinuity":
            return left
        span = right.elapsed_s - left.elapsed_s
        if span <= 0.0:
            return right
        return _interpolate_pose(left, right, (elapsed - left.elapsed_s) / span, elapsed)

    def poses_between(
        self,
        start_s: float,
        end_s: float,
    ) -> tuple[RecordedDivePose, ...]:
        """Return boundary poses plus recorded poses in a bounded time range."""
        start = max(0.0, min(self.duration_s, _finite_float(start_s, "start time")))
        end = max(start, min(self.duration_s, _finite_float(end_s, "end time")))
        first_index = bisect_left(self.elapsed_times, start)
        final_index = bisect_right(self.elapsed_times, end)
        values: list[RecordedDivePose] = [self.pose_at(start)]
        values.extend(self.poses[first_index:final_index])
        values.append(self.pose_at(end))
        deduplicated: list[RecordedDivePose] = []
        for pose in values:
            if deduplicated and pose == deduplicated[-1]:
                continue
            deduplicated.append(pose)
        return tuple(deduplicated)


class RecordedDivePlaybackState(Enum):
    """Explicit render-thread playback lifecycle."""

    READY = "ready"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"
    FINISHED = "finished"
    STOPPED = "stopped"


class RecordedDivePlaybackController:
    """Advance a trace clock only while the viewer reports chunks ready."""

    def __init__(self, trace: RecordedDiveTrace) -> None:
        self.trace = trace
        self.state = RecordedDivePlaybackState.READY
        self.elapsed_s = 0.0
        self._last_wall_time: float | None = None

    @property
    def active(self) -> bool:
        return self.state in {
            RecordedDivePlaybackState.READY,
            RecordedDivePlaybackState.BUFFERING,
            RecordedDivePlaybackState.PLAYING,
            RecordedDivePlaybackState.PAUSED,
        }

    def start(self, camera: Any, *, now: float) -> RecordedDivePlaybackState:
        """Place the camera at the exact first pose and wait for local chunks."""
        if self.state not in {
            RecordedDivePlaybackState.READY,
            RecordedDivePlaybackState.STOPPED,
        }:
            raise RuntimeError("Recorded Dive playback has already started")
        self.elapsed_s = 0.0
        self._last_wall_time = _finite_float(now, "wall time")
        self.state = RecordedDivePlaybackState.BUFFERING
        apply_recorded_dive_pose(camera, self.trace.initial_pose)
        return self.state

    def candidate_elapsed(self, *, now: float) -> float:
        """Return the next elapsed time without mutating playback state."""
        wall_time = _finite_float(now, "wall time")
        if (
            self._last_wall_time is None
            or self.state
            in {
                RecordedDivePlaybackState.PAUSED,
                RecordedDivePlaybackState.FINISHED,
                RecordedDivePlaybackState.STOPPED,
            }
        ):
            return self.elapsed_s
        return min(
            self.trace.duration_s,
            self.elapsed_s + max(0.0, wall_time - self._last_wall_time),
        )

    def update(
        self,
        camera: Any,
        *,
        now: float,
        chunks_ready: bool,
    ) -> RecordedDivePlaybackState:
        """Apply one frame, freezing trace time whenever chunks are not ready."""
        wall_time = _finite_float(now, "wall time")
        if self.state is RecordedDivePlaybackState.READY:
            return self.start(camera, now=wall_time)
        if self.state in {
            RecordedDivePlaybackState.FINISHED,
            RecordedDivePlaybackState.STOPPED,
        }:
            return self.state
        if self.state is RecordedDivePlaybackState.PAUSED:
            self._last_wall_time = wall_time
            apply_recorded_dive_pose(camera, self.trace.pose_at(self.elapsed_s))
            return self.state

        delta_s = 0.0
        if self._last_wall_time is not None:
            delta_s = max(0.0, wall_time - self._last_wall_time)
        self._last_wall_time = wall_time
        if not chunks_ready:
            self.state = RecordedDivePlaybackState.BUFFERING
            apply_recorded_dive_pose(camera, self.trace.pose_at(self.elapsed_s))
            return self.state

        self.elapsed_s = min(self.trace.duration_s, self.elapsed_s + delta_s)
        apply_recorded_dive_pose(camera, self.trace.pose_at(self.elapsed_s))
        if self.elapsed_s >= self.trace.duration_s:
            self.elapsed_s = self.trace.duration_s
            apply_recorded_dive_pose(camera, self.trace.final_pose)
            self.state = RecordedDivePlaybackState.FINISHED
        else:
            self.state = RecordedDivePlaybackState.PLAYING
        return self.state

    def pause(self, *, now: float) -> bool:
        if self.state not in {
            RecordedDivePlaybackState.BUFFERING,
            RecordedDivePlaybackState.PLAYING,
        }:
            return False
        self._last_wall_time = _finite_float(now, "wall time")
        self.state = RecordedDivePlaybackState.PAUSED
        return True

    def resume(self, *, now: float) -> bool:
        if self.state is not RecordedDivePlaybackState.PAUSED:
            return False
        self._last_wall_time = _finite_float(now, "wall time")
        self.state = RecordedDivePlaybackState.BUFFERING
        return True

    def stop(self) -> bool:
        if not self.active:
            return False
        self.state = RecordedDivePlaybackState.STOPPED
        self._last_wall_time = None
        return True

    def lookahead_poses(self, horizon_s: float) -> tuple[RecordedDivePose, ...]:
        horizon = max(0.0, _finite_float(horizon_s, "lookahead horizon"))
        return self.trace.poses_between(
            self.elapsed_s,
            min(self.trace.duration_s, self.elapsed_s + horizon),
        )


def is_recorded_dive_path(path: str | os.PathLike[str]) -> bool:
    """Return whether a selected path has the Recorded Dive container suffix."""
    return Path(path).suffix.lower() == RECORDED_DIVE_FILE_SUFFIX


def has_recorded_dive_trace(map_path: str | os.PathLike[str]) -> bool:
    """Return whether a map-local Guided Dive trace is available for playback."""
    try:
        trace_dir = Path(map_path) / MANUAL_DIVE_TRACE_DIRECTORY
        return any(
            entry.is_file() and entry.name.lower().endswith(RECORDED_DIVE_FILE_SUFFIX)
            for entry in trace_dir.iterdir()
        )
    except OSError:
        return False


def load_recorded_dive_trace(
    path: str | os.PathLike[str],
) -> RecordedDiveTrace:
    """Read and validate a completed manual camera trace with bounded memory."""
    trace_path = Path(path).expanduser().resolve()
    try:
        file_size = trace_path.stat().st_size
    except OSError as exc:
        raise RecordedDiveFormatError(
            f"Recorded Dive file could not be opened: {trace_path}"
        ) from exc
    if not trace_path.is_file():
        raise RecordedDiveFormatError(
            f"Recorded Dive path is not a file: {trace_path}"
        )
    if file_size <= 0 or file_size > MAX_RECORDED_DIVE_BYTES:
        raise RecordedDiveFormatError(
            "Recorded Dive file must be between 1 byte and 64 MiB."
        )

    header: Mapping[str, Any] | None = None
    completion: Mapping[str, Any] | None = None
    session_id: str | None = None
    poses: list[RecordedDivePose] = []
    previous_elapsed = -math.inf
    previous_sample_index = -1

    try:
        with trace_path.open("r", encoding="utf-8") as file_obj:
            for line_number, raw_line in enumerate(file_obj, 1):
                if len(raw_line.encode("utf-8")) > MAX_RECORDED_DIVE_LINE_BYTES:
                    raise RecordedDiveFormatError(
                        f"Recorded Dive line {line_number} exceeds 256 KiB."
                    )
                stripped = raw_line.strip()
                if not stripped:
                    continue
                record = _load_json_record(stripped, line_number)
                _require_schema_version(record, line_number)
                record_session = record.get("session_id")
                if (
                    not isinstance(record_session, str)
                    or not record_session
                    or len(record_session) > 128
                ):
                    raise RecordedDiveFormatError(
                        f"Recorded Dive line {line_number} has an invalid session_id."
                    )
                if session_id is None:
                    session_id = record_session
                elif record_session != session_id:
                    raise RecordedDiveFormatError(
                        f"Recorded Dive line {line_number} belongs to another session."
                    )

                kind = record.get("record")
                if header is None:
                    if kind != "trace_started":
                        raise RecordedDiveFormatError(
                            "Recorded Dive must begin with trace_started."
                        )
                    header = record
                    continue
                if completion is not None:
                    raise RecordedDiveFormatError(
                        "Recorded Dive contains records after trace_completed."
                    )
                if kind == "trace_completed":
                    completion = record
                    continue
                if kind not in {"sample", "discontinuity"}:
                    raise RecordedDiveFormatError(
                        f"Recorded Dive line {line_number} has unsupported record type {kind!r}."
                    )
                if len(poses) >= MAX_RECORDED_DIVE_SAMPLES:
                    raise RecordedDiveFormatError(
                        "Recorded Dive exceeds the 100,000-pose playback limit."
                    )
                pose = _parse_pose(record, line_number, str(kind))
                if pose.elapsed_s < previous_elapsed:
                    raise RecordedDiveFormatError(
                        f"Recorded Dive time moves backward at line {line_number}."
                    )
                if pose.sample_index is None or pose.sample_index <= previous_sample_index:
                    raise RecordedDiveFormatError(
                        f"Recorded Dive sample indexes are not increasing at line {line_number}."
                    )
                poses.append(pose)
                previous_elapsed = pose.elapsed_s
                previous_sample_index = pose.sample_index
    except UnicodeError as exc:
        raise RecordedDiveFormatError(
            "Recorded Dive file is not valid UTF-8."
        ) from exc
    except OSError as exc:
        raise RecordedDiveFormatError(
            f"Recorded Dive file could not be read: {trace_path}"
        ) from exc

    if header is None or session_id is None:
        raise RecordedDiveFormatError("Recorded Dive is missing trace_started.")
    if completion is None:
        raise RecordedDiveFormatError(
            "Recorded Dive is incomplete; trace_completed is missing."
        )
    if not poses:
        raise RecordedDiveFormatError("Recorded Dive contains no camera poses.")

    map_reference = _parse_map_reference(header.get("map"))
    completion_duration = _finite_float(
        completion.get("duration_s"),
        "completion duration",
    )
    if completion_duration + 1e-6 < poses[-1].elapsed_s:
        raise RecordedDiveFormatError(
            "Recorded Dive completion time precedes its final pose."
        )
    duration_s = max(completion_duration, poses[-1].elapsed_s)
    return RecordedDiveTrace(
        path=trace_path,
        schema_version=MANUAL_DIVE_TRACE_SCHEMA_VERSION,
        session_id=session_id,
        map_reference=map_reference,
        poses=tuple(poses),
        elapsed_times=tuple(pose.elapsed_s for pose in poses),
        duration_s=duration_s,
    )


def resolve_recorded_dive_source_path(
    trace: RecordedDiveTrace,
    *,
    search_directories: Iterable[str | os.PathLike[str]] = (),
) -> Path:
    """Resolve an exact source basename beside the trace or in known map roots."""
    trace_parent = trace.path.parent
    preferred_directories = [trace_parent]
    if trace_parent.name == MANUAL_DIVE_TRACE_DIRECTORY:
        preferred_directories.append(trace_parent.parent)
    else:
        preferred_directories.append(trace_parent.parent)

    preferred_matches = _source_matches(
        trace.map_reference.source_name,
        preferred_directories,
    )
    if preferred_matches:
        return preferred_matches[0]

    fallback_matches = _source_matches(
        trace.map_reference.source_name,
        search_directories,
    )
    if len(fallback_matches) == 1:
        return fallback_matches[0]
    if len(fallback_matches) > 1:
        raise RecordedDiveMapError(
            "More than one recent map matches this Recorded Dive. Place the "
            "trace in the correct map's _guided_dives directory."
        )
    raise RecordedDiveMapError(
        f"Could not find {trace.map_reference.source_name!r} for this Recorded Dive. "
        "Place the trace in that map's _guided_dives directory."
    )


def validate_recorded_dive_manifest(
    trace: RecordedDiveTrace,
    manifest: Mapping[str, Any] | None,
) -> None:
    """Require the loaded/rebuilt cache to match the trace's retained identity."""
    if not isinstance(manifest, Mapping):
        raise RecordedDiveMapError("The map cache manifest could not be loaded.")
    reference = trace.map_reference
    manifest_source = os.path.basename(os.fspath(manifest.get("source_obj") or ""))
    if manifest_source != reference.source_name:
        raise RecordedDiveMapError(
            "Recorded Dive source map does not match the loaded cache."
        )
    if (
        reference.manifest_version is not None
        and manifest.get("version") != reference.manifest_version
    ):
        raise RecordedDiveMapError(
            "Recorded Dive cache version does not match this map cache; rebuild "
            "the map with a compatible CaveViewer version."
        )
    if reference.chunk_size_m is not None:
        manifest_chunk_size = _optional_finite_float(manifest.get("chunk_size"))
        if (
            manifest_chunk_size is None
            or not math.isclose(
                manifest_chunk_size,
                reference.chunk_size_m,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        ):
            raise RecordedDiveMapError(
                "Recorded Dive chunk size does not match this map cache."
            )
    if reference.triangle_count is not None:
        manifest_triangles = manifest.get("triangle_count")
        if manifest_triangles != reference.triangle_count:
            raise RecordedDiveMapError(
                "Recorded Dive geometry does not match this map cache."
            )
    cache_identity = guided_dive_cache_identity_from_manifest(manifest)
    if cache_identity is None:
        raise RecordedDiveMapError(
            "This map cache lacks a stable Guided Dive identity. Rebuild the map "
            "before playing a Guided Dive."
        )
    if cache_identity != reference.cache_identity:
        raise RecordedDiveMapError(
            "Recorded Dive cache identity does not match this map cache."
        )


def apply_recorded_dive_pose(camera: Any, pose: RecordedDivePose) -> None:
    """Apply a trace pose directly, bypassing free-fly navigation guards."""
    camera.position = np.asarray(pose.position, dtype=np.float64)
    set_basis = getattr(camera, "set_orientation_basis", None)
    if callable(set_basis):
        set_basis(right=pose.right, up=pose.up, forward=pose.forward)
    else:
        right, up, forward = _orthonormal_basis(
            pose.right,
            pose.up,
            pose.forward,
        )
        camera._orient = np.asarray([right, up, forward], dtype=np.float64)
    camera.move_speed = float(pose.move_speed_m_per_second)


def _load_json_record(text: str, line_number: int) -> Mapping[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value}")

    try:
        value = json.loads(text, parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecordedDiveFormatError(
            f"Recorded Dive line {line_number} is not valid JSON."
        ) from exc
    if not isinstance(value, Mapping):
        raise RecordedDiveFormatError(
            f"Recorded Dive line {line_number} must be a JSON object."
        )
    return value


def _require_schema_version(record: Mapping[str, Any], line_number: int) -> None:
    if record.get("schema_version") != MANUAL_DIVE_TRACE_SCHEMA_VERSION:
        raise RecordedDiveFormatError(
            f"Recorded Dive line {line_number} uses an unsupported schema version."
        )


def _parse_map_reference(value: Any) -> RecordedDiveMapReference:
    if not isinstance(value, Mapping):
        raise RecordedDiveFormatError("Recorded Dive header is missing map identity.")
    source_name = value.get("source_obj")
    if (
        not isinstance(source_name, str)
        or not source_name
        or len(source_name) > 255
        or source_name != source_name.replace("\\", "/").rsplit("/", 1)[-1]
    ):
        raise RecordedDiveFormatError(
            "Recorded Dive source_obj must be a plain source filename."
        )
    if value.get("coordinate_space") != "manifest_xyz":
        raise RecordedDiveFormatError(
            "Recorded Dive coordinate space is not supported."
        )
    if value.get("distance_unit") != "meter" or value.get("orientation_unit") != "radian":
        raise RecordedDiveFormatError("Recorded Dive units are not supported.")
    manifest_version = _optional_nonnegative_int(value.get("manifest_version"))
    chunk_size_m = _optional_finite_float(value.get("chunk_size_m"))
    if chunk_size_m is not None and chunk_size_m <= 0.0:
        raise RecordedDiveFormatError("Recorded Dive chunk size must be positive.")
    triangle_count = _optional_nonnegative_int(value.get("triangle_count"))
    cache_identity = parse_guided_dive_cache_identity(value.get("cache_identity"))
    if cache_identity is None:
        raise RecordedDiveFormatError(
            "Recorded Dive header is missing a stable cache identity."
        )
    return RecordedDiveMapReference(
        source_name=source_name,
        manifest_version=manifest_version,
        chunk_size_m=chunk_size_m,
        triangle_count=triangle_count,
        cache_identity=cache_identity,
    )


def _parse_pose(
    record: Mapping[str, Any],
    line_number: int,
    kind: str,
) -> RecordedDivePose:
    try:
        elapsed_s = _finite_float(record.get("elapsed_s"), "pose time")
        if elapsed_s < 0.0:
            raise ValueError("negative pose time")
        sample_index = record.get("sample_index")
        if isinstance(sample_index, bool) or not isinstance(sample_index, int) or sample_index < 0:
            raise ValueError("invalid sample index")
        position = _vector3(record.get("position"), "position")
        forward = _vector3(record.get("forward"), "forward")
        up = _vector3(record.get("up"), "up")
        right = _vector3(record.get("right"), "right")
        _orthonormal_basis(right, up, forward)
        speed = _finite_float(
            record.get("move_speed_m_per_second"),
            "move speed",
        )
        if speed < 0.0:
            raise ValueError("negative move speed")
        return RecordedDivePose(
            elapsed_s=elapsed_s,
            position=position,
            forward=forward,
            up=up,
            right=right,
            yaw=_finite_float(record.get("yaw"), "yaw"),
            pitch=_finite_float(record.get("pitch"), "pitch"),
            roll=_finite_float(record.get("roll"), "roll"),
            move_speed_m_per_second=speed,
            record_kind=kind,
            sample_index=sample_index,
        )
    except (TypeError, ValueError) as exc:
        raise RecordedDiveFormatError(
            f"Recorded Dive line {line_number} has an invalid camera pose."
        ) from exc


def _interpolate_pose(
    first: RecordedDivePose,
    second: RecordedDivePose,
    fraction: float,
    elapsed_s: float,
) -> RecordedDivePose:
    t = max(0.0, min(1.0, float(fraction)))
    position = tuple(
        float(a + (b - a) * t)
        for a, b in zip(first.position, second.position)
    )
    first_quaternion = _basis_quaternion(first.right, first.up, first.forward)
    second_quaternion = _basis_quaternion(second.right, second.up, second.forward)
    quaternion = _slerp_quaternion(first_quaternion, second_quaternion, t)
    right, up, forward = _quaternion_basis(quaternion)
    return RecordedDivePose(
        elapsed_s=elapsed_s,
        position=position,
        forward=forward,
        up=up,
        right=right,
        yaw=_interpolate_angle(first.yaw, second.yaw, t),
        pitch=_interpolate_angle(first.pitch, second.pitch, t),
        roll=_interpolate_angle(first.roll, second.roll, t),
        move_speed_m_per_second=(
            first.move_speed_m_per_second
            + (second.move_speed_m_per_second - first.move_speed_m_per_second) * t
        ),
        record_kind="interpolated",
        sample_index=None,
    )


def _interpolate_angle(first: float, second: float, fraction: float) -> float:
    difference = (second - first + math.pi) % (2.0 * math.pi) - math.pi
    return first + difference * fraction


def _basis_quaternion(
    right: Sequence[float],
    up: Sequence[float],
    forward: Sequence[float],
) -> np.ndarray:
    normalized_right, normalized_up, normalized_forward = _orthonormal_basis(
        right,
        up,
        forward,
    )
    matrix = np.column_stack(
        (normalized_right, normalized_up, -normalized_forward)
    )
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        quaternion = np.array(
            [
                0.25 * scale,
                (matrix[2, 1] - matrix[1, 2]) / scale,
                (matrix[0, 2] - matrix[2, 0]) / scale,
                (matrix[1, 0] - matrix[0, 1]) / scale,
            ],
            dtype=np.float64,
        )
    else:
        diagonal = np.diag(matrix)
        axis = int(np.argmax(diagonal))
        if axis == 0:
            scale = math.sqrt(max(0.0, 1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2])) * 2.0
            quaternion = np.array(
                [
                    (matrix[2, 1] - matrix[1, 2]) / scale,
                    0.25 * scale,
                    (matrix[0, 1] + matrix[1, 0]) / scale,
                    (matrix[0, 2] + matrix[2, 0]) / scale,
                ],
                dtype=np.float64,
            )
        elif axis == 1:
            scale = math.sqrt(max(0.0, 1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2])) * 2.0
            quaternion = np.array(
                [
                    (matrix[0, 2] - matrix[2, 0]) / scale,
                    (matrix[0, 1] + matrix[1, 0]) / scale,
                    0.25 * scale,
                    (matrix[1, 2] + matrix[2, 1]) / scale,
                ],
                dtype=np.float64,
            )
        else:
            scale = math.sqrt(max(0.0, 1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1])) * 2.0
            quaternion = np.array(
                [
                    (matrix[1, 0] - matrix[0, 1]) / scale,
                    (matrix[0, 2] + matrix[2, 0]) / scale,
                    (matrix[1, 2] + matrix[2, 1]) / scale,
                    0.25 * scale,
                ],
                dtype=np.float64,
            )
    norm = float(np.linalg.norm(quaternion))
    if norm <= 1e-12:
        raise ValueError("degenerate orientation quaternion")
    return quaternion / norm


def _slerp_quaternion(first: np.ndarray, second: np.ndarray, fraction: float) -> np.ndarray:
    start = np.asarray(first, dtype=np.float64)
    end = np.asarray(second, dtype=np.float64)
    dot = float(np.dot(start, end))
    if dot < 0.0:
        end = -end
        dot = -dot
    dot = max(-1.0, min(1.0, dot))
    if dot > 0.9995:
        result = start + fraction * (end - start)
        return result / np.linalg.norm(result)
    angle = math.acos(dot)
    sine = math.sin(angle)
    start_weight = math.sin((1.0 - fraction) * angle) / sine
    end_weight = math.sin(fraction * angle) / sine
    return start * start_weight + end * end_weight


def _quaternion_basis(
    quaternion: Sequence[float],
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    w, x, y, z = (float(value) for value in quaternion)
    matrix = np.array(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )
    return _tuple3(matrix[:, 0]), _tuple3(matrix[:, 1]), _tuple3(-matrix[:, 2])


def _orthonormal_basis(
    right: Sequence[float],
    up: Sequence[float],
    forward: Sequence[float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    forward_array = np.asarray(forward, dtype=np.float64).reshape(3)
    right_array = np.asarray(right, dtype=np.float64).reshape(3)
    up_array = np.asarray(up, dtype=np.float64).reshape(3)
    if not (
        np.all(np.isfinite(forward_array))
        and np.all(np.isfinite(right_array))
        and np.all(np.isfinite(up_array))
    ):
        raise ValueError("orientation basis must be finite")
    forward_norm = float(np.linalg.norm(forward_array))
    if forward_norm <= 1e-9:
        raise ValueError("forward orientation is degenerate")
    normalized_forward = forward_array / forward_norm
    projected_right = right_array - np.dot(right_array, normalized_forward) * normalized_forward
    right_norm = float(np.linalg.norm(projected_right))
    if right_norm <= 1e-9:
        projected_right = np.cross(normalized_forward, up_array)
        right_norm = float(np.linalg.norm(projected_right))
    if right_norm <= 1e-9:
        raise ValueError("right/up orientation is degenerate")
    normalized_right = projected_right / right_norm
    normalized_up = np.cross(normalized_right, normalized_forward)
    if float(np.dot(normalized_up, up_array)) < 0.0:
        normalized_right = -normalized_right
        normalized_up = -normalized_up
    return normalized_right, normalized_up, normalized_forward


def _source_matches(
    source_name: str,
    directories: Iterable[str | os.PathLike[str]],
) -> list[Path]:
    matches: list[Path] = []
    seen: set[str] = set()
    for directory in directories:
        try:
            candidate = (Path(directory).expanduser().resolve() / source_name).resolve()
            key = os.path.normcase(os.fspath(candidate))
            if key in seen or not candidate.is_file():
                continue
        except (OSError, TypeError, ValueError):
            continue
        seen.add(key)
        matches.append(candidate)
    return matches


def _vector3(value: Any, label: str) -> tuple[float, float, float]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be a vector")
    values = tuple(value)
    if len(values) != 3:
        raise ValueError(f"{label} must have three components")
    return (
        _finite_float(values[0], label),
        _finite_float(values[1], label),
        _finite_float(values[2], label),
    )


def _tuple3(value: Sequence[float]) -> tuple[float, float, float]:
    return float(value[0]), float(value[1]), float(value[2])


def _finite_float(value: Any, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _optional_finite_float(value: Any) -> float | None:
    if value is None:
        return None
    return _finite_float(value, "optional numeric value")


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecordedDiveFormatError("Recorded Dive map identity has an invalid integer.")
    return value
