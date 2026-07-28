"""Bounded 3D forward-hemisphere probe generation for navigation recovery.

This module owns only probe geometry. Collision, voxel, and route policy stay
in the caller so the scan can be replaced without changing Guided Dive's
navigation seam.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
import math


Point = tuple[float, float, float]


@dataclass(frozen=True)
class HemisphereProbe:
    """One virtual camera pose and short 3D probe ray."""

    index: int
    direction_index: int
    roll_index: int
    offset_index: int
    direction: Point
    origin: Point
    target: Point
    origin_offset: Point
    roll_deg: float
    forward_alignment: float
    offset_label: str


def forward_hemisphere_directions(
    forward: Sequence[float],
    *,
    up_hint: Sequence[float] = (0.0, 1.0, 0.0),
    count: int = 48,
) -> tuple[Point, ...]:
    """Return approximately equal-area directions on the forward hemisphere.

    The hemisphere is defined by ``dot(direction, forward) >= 0``. A
    Fibonacci distribution avoids the pole clustering produced by a regular
    yaw/pitch grid while retaining deterministic, inexpensive sampling.
    """
    forward_unit = _unit(forward)
    right, up = _orthonormal_basis(forward_unit, up_hint=up_hint)
    sample_count = max(8, int(count))
    golden_angle = math.pi * (3.0 - math.sqrt(5.0))
    directions: list[Point] = []
    for index in range(sample_count):
        forward_alignment = (float(index) + 0.5) / float(sample_count)
        radial = math.sqrt(max(0.0, 1.0 - forward_alignment**2))
        azimuth = golden_angle * float(index)
        direction = _add(
            _scale(forward_unit, forward_alignment),
            _add(
                _scale(right, radial * math.cos(azimuth)),
                _scale(up, radial * math.sin(azimuth)),
            ),
        )
        directions.append(_unit(direction))
    return tuple(directions)


def iter_hemisphere_probes(
    current: Sequence[float],
    *,
    forward: Sequence[float],
    distance_m: float,
    cell_size_m: float,
    voxel_size_m: float = 1.0,
    current_roll_deg: float = 0.0,
    direction_count: int = 48,
    roll_count: int = 4,
    offset_radii_m: Sequence[float] | None = None,
) -> Iterator[HemisphereProbe]:
    """Yield bounded 3D probes with side/up origin offsets and roll samples.

    Origin offsets are intentionally virtual. The planner evaluates the move
    from the current camera to each offset and then the probe ray; it only
    executes the selected pose. This gives the navigation policy the same
    lateral and vertical visibility choices as a diver without moving the
    render camera through every sample.
    """
    current_point = _point(current)
    forward_unit = _unit(forward)
    scan_distance = max(
        max(1e-3, float(cell_size_m)),
        float(distance_m),
    )
    radii = _resolved_offset_radii(
        cell_size_m=float(cell_size_m),
        voxel_size_m=float(voxel_size_m),
        requested=offset_radii_m,
    )
    directions = forward_hemisphere_directions(
        forward_unit,
        count=direction_count,
    )
    roll_samples = max(1, int(roll_count))
    index = 0
    for direction_index, direction in enumerate(directions):
        right, up = _orthonormal_basis(direction)
        for roll_index in range(roll_samples):
            roll_offset_deg = 360.0 * float(roll_index) / float(roll_samples)
            roll_deg = _normalise_degrees(
                float(current_roll_deg) + roll_offset_deg
            )
            rolled_right, rolled_up = _rolled_basis(
                direction,
                right,
                up,
                math.radians(roll_offset_deg),
            )
            for offset_index, (right_factor, up_factor, label) in enumerate(
                _offset_patterns(radii)
            ):
                origin_offset = _add(
                    _scale(rolled_right, right_factor),
                    _scale(rolled_up, up_factor),
                )
                origin = _add(current_point, origin_offset)
                target = _add(origin, _scale(direction, scan_distance))
                yield HemisphereProbe(
                    index=index,
                    direction_index=direction_index,
                    roll_index=roll_index,
                    offset_index=offset_index,
                    direction=direction,
                    origin=origin,
                    target=target,
                    origin_offset=origin_offset,
                    roll_deg=roll_deg,
                    forward_alignment=_dot(direction, forward_unit),
                    offset_label=label,
                )
                index += 1


def _resolved_offset_radii(
    *,
    cell_size_m: float,
    voxel_size_m: float,
    requested: Sequence[float] | None,
) -> tuple[float, ...]:
    if requested is not None:
        values = [0.0]
        for value in requested:
            try:
                radius = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(radius) and radius > 1e-6:
                values.append(radius)
        return tuple(sorted(set(values)))
    step = max(1.0, float(voxel_size_m))
    step = min(step * 1.5, max(step, float(cell_size_m) * 0.25))
    outer = min(float(cell_size_m) * 0.5, step * 2.0)
    return tuple(sorted({0.0, step, outer}))


def _offset_patterns(
    radii: Sequence[float],
) -> tuple[tuple[float, float, str], ...]:
    patterns: list[tuple[float, float, str]] = [(0.0, 0.0, "center")]
    for radius in radii:
        if float(radius) <= 1e-6:
            continue
        value = float(radius)
        patterns.extend(
            (
                (value, 0.0, "right"),
                (-value, 0.0, "left"),
                (0.0, value, "up"),
                (0.0, -value, "down"),
            )
        )
    return tuple(patterns)


def _orthonormal_basis(
    forward: Sequence[float],
    *,
    up_hint: Sequence[float] = (0.0, 1.0, 0.0),
) -> tuple[Point, Point]:
    forward_unit = _unit(forward)
    hint = _unit(up_hint)
    right = _cross(forward_unit, hint)
    if _length(right) <= 1e-8:
        right = _cross(forward_unit, (1.0, 0.0, 0.0))
    right = _unit(right)
    up = _unit(_cross(right, forward_unit))
    return right, up


def _rolled_basis(
    forward: Sequence[float],
    right: Sequence[float],
    up: Sequence[float],
    roll_rad: float,
) -> tuple[Point, Point]:
    cosine = math.cos(float(roll_rad))
    sine = math.sin(float(roll_rad))
    axis = _unit(forward)
    rolled_right = _add(
        _scale(right, cosine),
        _scale(_cross(axis, right), sine),
    )
    rolled_up = _add(
        _scale(up, cosine),
        _scale(_cross(axis, up), sine),
    )
    return _unit(rolled_right), _unit(rolled_up)


def _normalise_degrees(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def _point(value: Sequence[float]) -> Point:
    if len(value) != 3:
        raise ValueError("recovery scan points must be three-dimensional")
    return tuple(float(item) for item in value)  # type: ignore[return-value]


def _unit(value: Sequence[float]) -> Point:
    point = _point(value)
    norm = _length(point)
    if norm <= 1e-9:
        raise ValueError("recovery scan vectors must be non-zero")
    return _scale(point, 1.0 / norm)


def _length(value: Sequence[float]) -> float:
    return math.sqrt(sum(float(item) ** 2 for item in value))


def _dot(first: Sequence[float], second: Sequence[float]) -> float:
    return sum(float(first[index]) * float(second[index]) for index in range(3))


def _cross(first: Sequence[float], second: Sequence[float]) -> Point:
    return (
        float(first[1]) * float(second[2])
        - float(first[2]) * float(second[1]),
        float(first[2]) * float(second[0])
        - float(first[0]) * float(second[2]),
        float(first[0]) * float(second[1])
        - float(first[1]) * float(second[0]),
    )


def _scale(value: Sequence[float], factor: float) -> Point:
    return tuple(float(item) * float(factor) for item in value)  # type: ignore[return-value]


def _add(first: Sequence[float], second: Sequence[float]) -> Point:
    return tuple(
        float(first[index]) + float(second[index])
        for index in range(3)
    )  # type: ignore[return-value]
