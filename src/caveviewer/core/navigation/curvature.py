"""Pure curvature profiles for navigation polylines.

The profile is intentionally independent of the current centerline generator.
It can describe a cached centerline, a runtime candidate, or a future voxel
route without coupling those planners together.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math


Point = tuple[float, float, float]
CURVATURE_PROFILE_METHOD = "rolling_turn_density_v1"


@dataclass(frozen=True)
class CurvatureSample:
    """Curvature measured around one interior polyline point."""

    center_index: int
    start_index: int
    end_index: int
    start_distance_m: float
    end_distance_m: float
    turn_angle_rad: float
    curvature_rad: float
    curvature_density_rad_per_m: float
    rank_0_100: int


@dataclass(frozen=True)
class CurvatureRegion:
    """A contiguous high-curvature region in a navigation polyline."""

    start_index: int
    end_index: int
    start_distance_m: float
    end_distance_m: float
    max_rank_0_100: int
    max_curvature_density_rad_per_m: float
    total_curvature_rad: float


@dataclass(frozen=True)
class CurvatureProfile:
    """Windowed curvature measurements and merged high-curvature regions."""

    point_count: int
    cumulative_distances_m: tuple[float, ...]
    samples: tuple[CurvatureSample, ...]
    regions: tuple[CurvatureRegion, ...]


def analyze_polyline_curvature(
    points: Sequence[Sequence[float]],
    *,
    window_points: int = 3,
) -> CurvatureProfile:
    """Return a map-relative curvature profile for a 3D polyline.

    Curvature is represented as accumulated absolute turn angle per metre in
    a rolling window. The raw density is retained alongside a robust 0-100
    rank so callers can use either map-relative or cross-map thresholds.
    """
    normalized = tuple(_point(point) for point in points)
    cumulative = _cumulative_distances(normalized)
    if len(normalized) < 3:
        return CurvatureProfile(
            point_count=len(normalized),
            cumulative_distances_m=tuple(cumulative),
            samples=(),
            regions=(),
        )

    radius = max(1, int(window_points))
    turn_angles = [0.0] * len(normalized)
    for index in range(1, len(normalized) - 1):
        turn_angles[index] = _turn_angle(
            normalized[index - 1],
            normalized[index],
            normalized[index + 1],
        )

    raw_samples: list[dict[str, float | int]] = []
    for index in range(1, len(normalized) - 1):
        start_index = max(0, index - radius)
        end_index = min(len(normalized) - 1, index + radius)
        start_distance = cumulative[start_index]
        end_distance = cumulative[end_index]
        window_length = max(0.0, end_distance - start_distance)
        curvature = sum(turn_angles[start_index : end_index + 1])
        density = curvature / window_length if window_length > 1e-9 else 0.0
        raw_samples.append(
            {
                "center_index": index,
                "start_index": start_index,
                "end_index": end_index,
                "start_distance_m": start_distance,
                "end_distance_m": end_distance,
                "turn_angle_rad": turn_angles[index],
                "curvature_rad": curvature,
                "curvature_density_rad_per_m": density,
            }
        )

    densities = [
        float(sample["curvature_density_rad_per_m"])
        for sample in raw_samples
    ]
    lower = _percentile(densities, 10.0)
    upper = _percentile(densities, 90.0)
    samples = tuple(
        CurvatureSample(
            center_index=int(sample["center_index"]),
            start_index=int(sample["start_index"]),
            end_index=int(sample["end_index"]),
            start_distance_m=float(sample["start_distance_m"]),
            end_distance_m=float(sample["end_distance_m"]),
            turn_angle_rad=float(sample["turn_angle_rad"]),
            curvature_rad=float(sample["curvature_rad"]),
            curvature_density_rad_per_m=float(
                sample["curvature_density_rad_per_m"]
            ),
            rank_0_100=_rank_density(
                float(sample["curvature_density_rad_per_m"]),
                lower=lower,
                upper=upper,
            ),
        )
        for sample in raw_samples
    )
    return CurvatureProfile(
        point_count=len(normalized),
        cumulative_distances_m=tuple(cumulative),
        samples=samples,
        regions=_merge_curvature_regions(samples),
    )


def select_curvature_regions(
    profile: CurvatureProfile,
    *,
    minimum_rank: int = 65,
    max_regions: int = 2,
    max_start_distance_m: float | None = None,
) -> tuple[CurvatureRegion, ...]:
    """Select the earliest strongest regions for bounded local analysis."""
    threshold = max(0, min(100, int(minimum_rank)))
    limit = max(0, int(max_regions))
    if limit == 0:
        return ()
    selected = [
        region
        for region in profile.regions
        if region.max_rank_0_100 >= threshold
        and (
            max_start_distance_m is None
            or region.start_distance_m <= float(max_start_distance_m)
        )
    ]
    selected.sort(
        key=lambda region: (
            -region.max_rank_0_100,
            -region.max_curvature_density_rad_per_m,
            region.start_distance_m,
        )
    )
    return tuple(
        sorted(selected[:limit], key=lambda region: region.start_distance_m)
    )


def _point(value: Sequence[float]) -> Point:
    if len(value) != 3:
        raise ValueError("curvature points must be three-dimensional")
    point = (float(value[0]), float(value[1]), float(value[2]))
    if not all(math.isfinite(coordinate) for coordinate in point):
        raise ValueError("curvature points must be finite")
    return point


def _cumulative_distances(points: Sequence[Point]) -> list[float]:
    distances = [0.0]
    for first, second in zip(points, points[1:], strict=False):
        distances.append(distances[-1] + _distance(first, second))
    return distances


def _distance(first: Point, second: Point) -> float:
    return math.sqrt(
        sum((float(second[index]) - float(first[index])) ** 2 for index in range(3))
    )


def _turn_angle(previous: Point, current: Point, next_point: Point) -> float:
    first = tuple(current[index] - previous[index] for index in range(3))
    second = tuple(next_point[index] - current[index] for index in range(3))
    first_length = math.sqrt(sum(value * value for value in first))
    second_length = math.sqrt(sum(value * value for value in second))
    if first_length <= 1e-9 or second_length <= 1e-9:
        return 0.0
    cosine = sum(first[index] * second[index] for index in range(3))
    cosine /= first_length * second_length
    return math.acos(max(-1.0, min(1.0, cosine)))


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * max(0.0, min(100.0, percentile)) / 100.0
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] + (
        ordered[upper_index] - ordered[lower_index]
    ) * fraction


def _rank_density(value: float, *, lower: float, upper: float) -> int:
    if value <= 1e-9:
        return 0
    if upper <= lower + 1e-9:
        return 100
    normalized = (value - lower) / (upper - lower)
    return int(round(100.0 * max(0.0, min(1.0, normalized))))


def _merge_curvature_regions(
    samples: Sequence[CurvatureSample],
    *,
    minimum_rank: int = 65,
) -> tuple[CurvatureRegion, ...]:
    active = [
        sample
        for sample in samples
        if sample.rank_0_100 >= minimum_rank
        and sample.curvature_density_rad_per_m > 1e-9
    ]
    if not active:
        return ()

    groups: list[list[CurvatureSample]] = []
    current_group: list[CurvatureSample] = [active[0]]
    for sample in active[1:]:
        if sample.center_index <= current_group[-1].center_index + 1:
            current_group.append(sample)
        else:
            groups.append(current_group)
            current_group = [sample]
    groups.append(current_group)

    return tuple(
        CurvatureRegion(
            start_index=min(sample.start_index for sample in group),
            end_index=max(sample.end_index for sample in group),
            start_distance_m=min(sample.start_distance_m for sample in group),
            end_distance_m=max(sample.end_distance_m for sample in group),
            max_rank_0_100=max(sample.rank_0_100 for sample in group),
            max_curvature_density_rad_per_m=max(
                sample.curvature_density_rad_per_m for sample in group
            ),
            total_curvature_rad=sum(sample.turn_angle_rad for sample in group),
        )
        for group in groups
    )
