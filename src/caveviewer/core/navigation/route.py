"""Generic camera route and route-following primitives."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any

import numpy as np


class NavigationConfigurationError(ValueError):
    """Raised when navigation route inputs are missing or invalid."""


@dataclass(frozen=True)
class RouteKeyframe:
    """One camera pose in a generated or user-authored route."""

    time_s: float
    position: tuple[float, float, float]
    yaw_deg: float
    pitch_deg: float
    roll_deg: float = 0.0

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, index: int) -> "RouteKeyframe":
        try:
            time_s = float(payload["time_s"])
            position = _float_tuple(
                payload["position"],
                length=3,
                field=f"route[{index}].position",
            )
            yaw_deg = float(payload.get("yaw_deg", payload.get("yaw", 0.0)))
            pitch_deg = float(payload.get("pitch_deg", payload.get("pitch", 0.0)))
            roll_deg = float(payload.get("roll_deg", payload.get("roll", 0.0)))
        except KeyError as exc:
            raise NavigationConfigurationError(
                f"route[{index}] is missing required field {exc.args[0]!r}"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise NavigationConfigurationError(
                f"route[{index}] contains invalid values"
            ) from exc
        if time_s < 0:
            raise NavigationConfigurationError(
                f"route[{index}].time_s must be non-negative"
            )
        return cls(
            time_s=time_s,
            position=position,
            yaw_deg=yaw_deg,
            pitch_deg=pitch_deg,
            roll_deg=roll_deg,
        )

    def identity_payload(self) -> dict[str, Any]:
        """Return route content that affects deterministic identity."""
        return {
            "time_s": self.time_s,
            "position": list(self.position),
            "yaw_deg": self.yaw_deg,
            "pitch_deg": self.pitch_deg,
            "roll_deg": self.roll_deg,
        }


@dataclass(frozen=True)
class CameraRoute:
    """A time-indexed camera path independent of benchmark measurement."""

    keyframes: tuple[RouteKeyframe, ...]
    position_mode: str = "absolute"

    @classmethod
    def from_keyframes(
        cls,
        keyframes: Sequence[RouteKeyframe],
        *,
        position_mode: str = "absolute",
    ) -> "CameraRoute":
        route = tuple(keyframes)
        if not route:
            raise NavigationConfigurationError(
                "camera route requires at least one keyframe"
            )
        for previous, current in zip(route, route[1:]):
            if current.time_s <= previous.time_s:
                raise NavigationConfigurationError(
                    "route keyframe time_s values must be strictly increasing"
                )
        normalized_position_mode = str(position_mode).strip().lower()
        if normalized_position_mode not in {"absolute", "first_chunk_center_offset"}:
            raise NavigationConfigurationError(
                "position_mode must be 'absolute' or 'first_chunk_center_offset'"
            )
        return cls(route, normalized_position_mode)

    def pose_at(self, elapsed_s: float) -> RouteKeyframe:
        """Return the interpolated camera pose for route elapsed time."""
        return interpolated_route_pose(self.keyframes, elapsed_s)

    @property
    def duration_s(self) -> float:
        return self.keyframes[-1].time_s


class RouteFollower:
    """Play a camera route against a FlyCamera-like object."""

    def __init__(
        self,
        route: CameraRoute,
        *,
        perf_counter: Callable[[], float],
    ) -> None:
        self.route = route
        self.perf_counter = perf_counter
        self._started_at: float | None = None
        self._position_origin = np.zeros(3, dtype=np.float64)

    @property
    def started(self) -> bool:
        return self._started_at is not None

    @property
    def started_at(self) -> float | None:
        return self._started_at

    def set_position_origin(self, origin: Iterable[float]) -> None:
        """Set the map-relative origin used by offset-based routes."""
        if self.route.position_mode == "first_chunk_center_offset":
            self._position_origin = np.asarray(tuple(origin), dtype=np.float64)

    def apply_initial_camera(self, camera) -> None:
        """Place the camera at the first route keyframe."""
        apply_pose_to_camera(
            camera,
            self.route.keyframes[0],
            position_origin=self._position_origin,
        )

    def update_camera(self, camera, now: float | None = None) -> float:
        """Apply the current route pose and return elapsed route seconds."""
        now = self.perf_counter() if now is None else float(now)
        if self._started_at is None:
            self._started_at = now
        elapsed_s = now - self._started_at
        apply_pose_to_camera(
            camera,
            self.route.pose_at(elapsed_s),
            position_origin=self._position_origin,
        )
        return elapsed_s


def interpolated_route_pose(
    keyframes: Sequence[RouteKeyframe],
    elapsed_s: float,
) -> RouteKeyframe:
    """Return the interpolated route pose for elapsed time."""
    route = tuple(keyframes)
    if not route:
        raise NavigationConfigurationError("camera route has no keyframes")
    elapsed_s = max(0.0, float(elapsed_s))
    if elapsed_s <= route[0].time_s or len(route) == 1:
        return route[0]
    if elapsed_s >= route[-1].time_s:
        return route[-1]

    for previous, current in zip(route, route[1:]):
        if previous.time_s <= elapsed_s <= current.time_s:
            span = max(1e-9, current.time_s - previous.time_s)
            t = (elapsed_s - previous.time_s) / span
            position = tuple(
                lerp(previous.position[index], current.position[index], t)
                for index in range(3)
            )
            return RouteKeyframe(
                time_s=elapsed_s,
                position=position,
                yaw_deg=lerp_angle_degrees(previous.yaw_deg, current.yaw_deg, t),
                pitch_deg=lerp(previous.pitch_deg, current.pitch_deg, t),
                roll_deg=lerp_angle_degrees(previous.roll_deg, current.roll_deg, t),
            )

    return route[-1]


def apply_pose_to_camera(
    camera,
    pose: RouteKeyframe,
    *,
    position_origin: Iterable[float] | None = None,
) -> None:
    """Apply one route pose to a FlyCamera-like object."""
    origin = (
        np.zeros(3, dtype=np.float64)
        if position_origin is None
        else np.asarray(tuple(position_origin), dtype=np.float64)
    )
    camera.position = np.array(pose.position, dtype=np.float64) + origin
    camera.yaw = math.radians(pose.yaw_deg)
    camera.pitch = math.radians(pose.pitch_deg)
    camera.roll = math.radians(pose.roll_deg)


def route_keyframes_for_points(
    points: tuple[tuple[float, float, float], ...],
    *,
    duration_s: float,
    start_time_s: float = 0.0,
    hold_start: bool = False,
) -> list[dict[str, Any]]:
    """Create route keyframe dictionaries from 3D points."""
    if not points:
        raise NavigationConfigurationError("generated route has no points")
    distances = cumulative_distances(points)
    total_distance = distances[-1]
    keyframes: list[dict[str, Any]] = []
    for index, point in enumerate(points):
        if total_distance > 0:
            time_s = (
                float(start_time_s)
                + float(duration_s) * distances[index] / total_distance
            )
        else:
            time_s = (
                float(start_time_s)
                + float(duration_s) * index / max(1, len(points) - 1)
            )
        yaw_deg, pitch_deg = look_angles(points, index)
        keyframes.append(
            {
                "time_s": round(time_s, 6),
                "position": [round(float(value), 6) for value in point],
                "yaw_deg": round(yaw_deg, 6),
                "pitch_deg": round(pitch_deg, 6),
            }
        )
    if hold_start and start_time_s > 0.0:
        first = dict(keyframes[0])
        first["time_s"] = 0.0
        keyframes.insert(0, first)
        keyframes[1]["time_s"] = round(float(start_time_s), 6)
    else:
        keyframes[0]["time_s"] = round(float(start_time_s), 6)
    if len(keyframes) > 1:
        keyframes[-1]["time_s"] = round(
            float(start_time_s) + float(duration_s),
            6,
        )
    return keyframes


def look_angles(
    points: tuple[tuple[float, float, float], ...],
    index: int,
) -> tuple[float, float]:
    if len(points) == 1:
        return 0.0, 0.0
    if index < len(points) - 1:
        source = points[index]
        target = points[index + 1]
    else:
        source = points[index - 1]
        target = points[index]
    dx = target[0] - source[0]
    dy = target[1] - source[1]
    dz = target[2] - source[2]
    horizontal = math.hypot(dx, dz)
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    if distance < 1e-9:
        return 0.0, 0.0
    yaw = math.degrees(math.atan2(dz, dx))
    pitch = math.degrees(math.atan2(dy, horizontal))
    return yaw, pitch


def cumulative_distances(points: tuple[tuple[float, float, float], ...]) -> list[float]:
    distances = [0.0]
    for first, second in zip(points, points[1:]):
        distances.append(distances[-1] + point_distance(first, second))
    return distances


def path_length(points: tuple[tuple[float, float, float], ...]) -> float:
    return sum(
        point_distance(first, second)
        for first, second in zip(points, points[1:])
    )


def point_distance(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def lerp(start: float, end: float, t: float) -> float:
    return float(start) + (float(end) - float(start)) * float(t)


def lerp_angle_degrees(start: float, end: float, t: float) -> float:
    delta = ((float(end) - float(start) + 180.0) % 360.0) - 180.0
    return float(start) + delta * float(t)


def _float_tuple(
    value: Any,
    *,
    length: int,
    field: str,
) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise NavigationConfigurationError(f"{field} must be a {length}-item array")
    try:
        return tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise NavigationConfigurationError(f"{field} must contain numbers") from exc
