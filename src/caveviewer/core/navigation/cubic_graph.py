"""Compact implicit graph over isotropic cubic free-space voxels.

The prepared V10 graph may coarsen horizontal buckets to keep an explicit
Python node/edge graph bounded.  This module preserves the original cubic
voxel resolution instead: free cells are stored as packed integer keys and
their local adjacency is computed on demand.  Exact cached-mesh validation
remains a separate authority for any route selected from this evidence.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
import heapq
import math

from caveviewer.core.navigation.centerline import Point
from caveviewer.core.navigation.voxel_volume import LocalVoxelVolume


CUBIC_VOXEL_GRAPH_METHOD = "implicit_sparse_cubic_free_space_v1"

CubicVoxelKey = tuple[int, int, int]
PackedCubicVoxelKey = int
PointFilter = Callable[[Point], bool]

_COORDINATE_BITS = 21
_COORDINATE_BIAS = 1 << (_COORDINATE_BITS - 1)
_COORDINATE_LIMIT = 1 << _COORDINATE_BITS
_COORDINATE_MASK = _COORDINATE_LIMIT - 1

_CARDINAL_OFFSETS: tuple[CubicVoxelKey, ...] = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
    (0, 0, 1),
    (0, 0, -1),
)
_OFFSETS_26: tuple[CubicVoxelKey, ...] = tuple(
    (delta_x, delta_y, delta_z)
    for delta_x in (-1, 0, 1)
    for delta_y in (-1, 0, 1)
    for delta_z in (-1, 0, 1)
    if (delta_x, delta_y, delta_z) != (0, 0, 0)
)


@dataclass(frozen=True)
class CubicVoxelPath:
    """One deterministic shortest path through implicit cubic adjacency."""

    keys: tuple[CubicVoxelKey, ...]
    points: tuple[Point, ...]
    distance_m: float
    expanded_voxel_count: int

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "voxel_count": len(self.keys),
            "point_count": len(self.points),
            "distance_m": float(self.distance_m),
            "expanded_voxel_count": int(self.expanded_voxel_count),
            "start_key": _key_payload(self.keys[0]) if self.keys else None,
            "terminal_key": _key_payload(self.keys[-1]) if self.keys else None,
        }


@dataclass(frozen=True)
class CubicVoxelGraphBuildResult:
    """A cubic graph plus bounded construction diagnostics."""

    graph: "SparseCubicVoxelGraph"
    details: dict[str, object]


@dataclass(frozen=True)
class SparseCubicVoxelGraph:
    """Free cubic voxels with adjacency derived directly from coordinates.

    Packing three signed coordinates into one Python integer avoids retaining
    a tuple object and an explicit edge collection for every free voxel.  Six-
    connected component analysis and corner-safe 26-neighbor A* are computed
    only when requested.
    """

    voxel_size_m: float
    packed_free_keys: frozenset[PackedCubicVoxelKey]

    def __post_init__(self) -> None:
        size = float(self.voxel_size_m)
        if not math.isfinite(size) or size <= 0.0:
            raise ValueError("cubic voxel size must be positive and finite")

    @classmethod
    def from_keys(
        cls,
        keys: Sequence[CubicVoxelKey],
        *,
        voxel_size_m: float = 1.0,
    ) -> "SparseCubicVoxelGraph":
        """Construct a graph from deterministic world-grid voxel keys."""
        return cls(
            voxel_size_m=float(voxel_size_m),
            packed_free_keys=frozenset(pack_cubic_voxel_key(key) for key in keys),
        )

    @property
    def free_voxel_count(self) -> int:
        return len(self.packed_free_keys)

    @property
    def compact_key_bytes(self) -> int:
        """Return the serialized lower bound for packed 64-bit keys."""
        return len(self.packed_free_keys) * 8

    def contains_key(self, key: CubicVoxelKey) -> bool:
        return pack_cubic_voxel_key(key) in self.packed_free_keys

    def world_key(self, point: Sequence[float]) -> CubicVoxelKey:
        """Map one finite world point to this graph's global cubic grid."""
        if len(point) != 3:
            raise ValueError("cubic graph points must be three-dimensional")
        size = float(self.voxel_size_m)
        try:
            values = tuple(float(value) for value in point)
        except (TypeError, ValueError) as exc:
            raise ValueError("cubic graph point is malformed") from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError("cubic graph point must be finite")
        return tuple(  # type: ignore[return-value]
            math.floor(value / size) for value in values
        )

    def voxel_center(self, key: CubicVoxelKey) -> Point:
        size = float(self.voxel_size_m)
        return tuple(
            (int(key[axis]) + 0.5) * size
            for axis in range(3)
        )  # type: ignore[return-value]

    def nearest_key(
        self,
        point: Sequence[float],
        *,
        max_distance_m: float,
    ) -> tuple[CubicVoxelKey | None, float]:
        """Return the nearest free voxel in a bounded cubic neighborhood."""
        try:
            if len(point) != 3:
                return None, math.inf
            target = tuple(float(value) for value in point)
        except (TypeError, ValueError):
            return None, math.inf
        if not all(math.isfinite(value) for value in target):
            return None, math.inf
        maximum = float(max_distance_m)
        if not math.isfinite(maximum) or maximum < 0.0:
            return None, math.inf
        base = self.world_key(target)
        radius = max(0, int(math.ceil(maximum / self.voxel_size_m)))
        best_key: CubicVoxelKey | None = None
        best_distance = math.inf
        for delta_x in range(-radius, radius + 1):
            for delta_y in range(-radius, radius + 1):
                for delta_z in range(-radius, radius + 1):
                    key = (
                        base[0] + delta_x,
                        base[1] + delta_y,
                        base[2] + delta_z,
                    )
                    if not self.contains_key(key):
                        continue
                    distance_m = math.dist(target, self.voxel_center(key))
                    if distance_m > maximum + 1e-9:
                        continue
                    rank = (float(distance_m), key)
                    current_rank = (
                        float(best_distance),
                        best_key if best_key is not None else key,
                    )
                    if best_key is None or rank < current_rank:
                        best_key = key
                        best_distance = float(distance_m)
        return best_key, best_distance

    def component_sizes(self) -> tuple[int, ...]:
        """Return six-connected free-space component sizes, largest first."""
        remaining = set(self.packed_free_keys)
        sizes: list[int] = []
        while remaining:
            start = min(remaining)
            remaining.remove(start)
            queue = deque((start,))
            size = 0
            while queue:
                packed = queue.popleft()
                size += 1
                key = unpack_cubic_voxel_key(packed)
                for offset in _CARDINAL_OFFSETS:
                    neighbor = (
                        key[0] + offset[0],
                        key[1] + offset[1],
                        key[2] + offset[2],
                    )
                    neighbor_packed = pack_cubic_voxel_key(neighbor)
                    if neighbor_packed not in remaining:
                        continue
                    remaining.remove(neighbor_packed)
                    queue.append(neighbor_packed)
            sizes.append(size)
        return tuple(sorted(sizes, reverse=True))

    def shortest_path(
        self,
        start_key: CubicVoxelKey,
        terminal_key: CubicVoxelKey,
        *,
        allow_diagonal: bool = True,
        max_expansions: int | None = None,
        blocked_edges: Collection[
            tuple[CubicVoxelKey, CubicVoxelKey]
        ] = (),
    ) -> CubicVoxelPath | None:
        """Find a shortest physical route without materializing graph edges."""
        start = pack_cubic_voxel_key(start_key)
        terminal = pack_cubic_voxel_key(terminal_key)
        if start not in self.packed_free_keys or terminal not in self.packed_free_keys:
            return None
        if start == terminal:
            point = self.voxel_center(start_key)
            return CubicVoxelPath(
                keys=(start_key,),
                points=(point,),
                distance_m=0.0,
                expanded_voxel_count=1,
            )
        expansion_limit = (
            len(self.packed_free_keys)
            if max_expansions is None
            else max(1, int(max_expansions))
        )
        blocked = frozenset(
            _ordered_packed_edge(
                pack_cubic_voxel_key(first),
                pack_cubic_voxel_key(second),
            )
            for first, second in blocked_edges
        )
        terminal_center = self.voxel_center(terminal_key)
        distances: dict[PackedCubicVoxelKey, float] = {start: 0.0}
        previous: dict[PackedCubicVoxelKey, PackedCubicVoxelKey] = {}
        queue: list[tuple[float, float, PackedCubicVoxelKey]] = [
            (math.dist(self.voxel_center(start_key), terminal_center), 0.0, start)
        ]
        closed: set[PackedCubicVoxelKey] = set()
        offsets = _OFFSETS_26 if allow_diagonal else _CARDINAL_OFFSETS
        expanded_count = 0
        while queue and expanded_count < expansion_limit:
            _priority, distance_m, packed = heapq.heappop(queue)
            if packed in closed:
                continue
            if distance_m > distances.get(packed, math.inf) + 1e-12:
                continue
            closed.add(packed)
            expanded_count += 1
            if packed == terminal:
                path_packed = [packed]
                while path_packed[-1] != start:
                    predecessor = previous.get(path_packed[-1])
                    if predecessor is None:
                        return None
                    path_packed.append(predecessor)
                path_packed.reverse()
                keys = tuple(
                    unpack_cubic_voxel_key(value) for value in path_packed
                )
                return CubicVoxelPath(
                    keys=keys,
                    points=tuple(self.voxel_center(key) for key in keys),
                    distance_m=float(distance_m),
                    expanded_voxel_count=int(expanded_count),
                )
            key = unpack_cubic_voxel_key(packed)
            for offset in offsets:
                neighbor_key = (
                    key[0] + offset[0],
                    key[1] + offset[1],
                    key[2] + offset[2],
                )
                neighbor = pack_cubic_voxel_key(neighbor_key)
                if neighbor in closed or neighbor not in self.packed_free_keys:
                    continue
                if _ordered_packed_edge(packed, neighbor) in blocked:
                    continue
                if allow_diagonal and not self._diagonal_step_is_clear(
                    key,
                    offset,
                ):
                    continue
                step_distance = float(self.voxel_size_m) * math.sqrt(
                    sum(value * value for value in offset)
                )
                candidate_distance = distance_m + step_distance
                existing = distances.get(neighbor)
                if existing is not None and candidate_distance >= existing - 1e-12:
                    continue
                distances[neighbor] = candidate_distance
                previous[neighbor] = packed
                heuristic = math.dist(
                    self.voxel_center(neighbor_key),
                    terminal_center,
                )
                heapq.heappush(
                    queue,
                    (
                        candidate_distance + heuristic,
                        candidate_distance,
                        neighbor,
                    ),
                )
        return None

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "method": CUBIC_VOXEL_GRAPH_METHOD,
            "voxel_size_m": float(self.voxel_size_m),
            "free_voxel_count": len(self.packed_free_keys),
            "compact_key_bytes": int(self.compact_key_bytes),
            "explicit_edge_count": 0,
            "adjacency": "implicit_cardinal_or_corner_safe_26_neighbor",
        }

    def _diagonal_step_is_clear(
        self,
        source: CubicVoxelKey,
        offset: CubicVoxelKey,
    ) -> bool:
        axes = tuple(axis for axis, value in enumerate(offset) if value)
        if len(axes) <= 1:
            return True
        full_mask = (1 << len(axes)) - 1
        for mask in range(1, full_mask):
            intermediate = list(source)
            for bit, axis in enumerate(axes):
                if mask & (1 << bit):
                    intermediate[axis] += offset[axis]
            if pack_cubic_voxel_key(tuple(intermediate)) not in self.packed_free_keys:
                return False
        return True


def build_cubic_graph_from_local_volumes(
    volumes: Sequence[tuple[LocalVoxelVolume, Sequence[Point]]],
    *,
    voxel_size_m: float = 1.0,
    minimum_clearance_m: float = 0.0,
    point_filter: PointFilter | None = None,
    allow_truncated_surface_evidence: bool = False,
) -> CubicVoxelGraphBuildResult:
    """Merge aligned local fields into one conservative implicit graph.

    Every source volume must already use the requested isotropic resolution
    and align to the same global grid. Surface occupancy from any overlapping
    tile wins over free-space evidence from another tile. This is intentionally
    fail-closed; truncated evidence is rejected unless a diagnostic caller
    explicitly opts in, and exact mesh checks are still required before route
    execution.
    """
    size = float(voxel_size_m)
    clearance = float(minimum_clearance_m)
    if not math.isfinite(size) or size <= 0.0:
        raise ValueError("cubic graph voxel size must be positive and finite")
    if not math.isfinite(clearance) or clearance < 0.0:
        raise ValueError("cubic graph clearance must be non-negative and finite")
    free: set[PackedCubicVoxelKey] = set()
    blocked: set[PackedCubicVoxelKey] = set()
    source_free_count = 0
    clearance_rejection_count = 0
    point_filter_rejection_count = 0
    duplicate_free_count = 0
    truncated_source_volume_count = 0
    for volume, seed_points in volumes:
        _validate_aligned_volume(
            volume,
            size,
            allow_truncated_surface_evidence=bool(
                allow_truncated_surface_evidence
            ),
        )
        if volume.sampling_truncated:
            truncated_source_volume_count += 1
        for local_key in volume.surface_cells:
            blocked.add(
                pack_cubic_voxel_key(
                    _world_key(volume.voxel_center(local_key), size)
                )
            )
        filled = volume.filled_free_cell_clearance_m(seed_points)
        source_free_count += len(filled)
        for local_key, clearance_m in filled.items():
            if float(clearance_m) + 1e-9 < clearance:
                clearance_rejection_count += 1
                continue
            center = volume.voxel_center(local_key)
            if point_filter is not None and not point_filter(center):
                point_filter_rejection_count += 1
                continue
            packed = pack_cubic_voxel_key(_world_key(center, size))
            if packed in free:
                duplicate_free_count += 1
            free.add(packed)
    blocked_free_count = len(free & blocked)
    free.difference_update(blocked)
    graph = SparseCubicVoxelGraph(
        voxel_size_m=size,
        packed_free_keys=frozenset(free),
    )
    return CubicVoxelGraphBuildResult(
        graph=graph,
        details={
            **graph.diagnostic_payload(),
            "source_volume_count": len(volumes),
            "source_filled_free_voxel_count": int(source_free_count),
            "surface_blocked_voxel_count": len(blocked),
            "blocked_free_conflict_count": int(blocked_free_count),
            "clearance_rejection_count": int(clearance_rejection_count),
            "point_filter_rejection_count": int(point_filter_rejection_count),
            "duplicate_free_voxel_count": int(duplicate_free_count),
            "truncated_source_volume_count": int(
                truncated_source_volume_count
            ),
        },
    )


def pack_cubic_voxel_key(key: CubicVoxelKey) -> PackedCubicVoxelKey:
    """Pack three bounded signed grid coordinates into one positive integer."""
    if len(key) != 3:
        raise ValueError("cubic voxel key must contain three coordinates")
    shifted: list[int] = []
    for value in key:
        coordinate = int(value)
        encoded = coordinate + _COORDINATE_BIAS
        if encoded < 0 or encoded >= _COORDINATE_LIMIT:
            raise ValueError("cubic voxel coordinate exceeds packed range")
        shifted.append(encoded)
    return (
        (shifted[0] << (_COORDINATE_BITS * 2))
        | (shifted[1] << _COORDINATE_BITS)
        | shifted[2]
    )


def unpack_cubic_voxel_key(packed: PackedCubicVoxelKey) -> CubicVoxelKey:
    """Restore one key produced by :func:`pack_cubic_voxel_key`."""
    value = int(packed)
    if value < 0 or value >= 1 << (_COORDINATE_BITS * 3):
        raise ValueError("packed cubic voxel key is outside range")
    return (
        ((value >> (_COORDINATE_BITS * 2)) & _COORDINATE_MASK)
        - _COORDINATE_BIAS,
        ((value >> _COORDINATE_BITS) & _COORDINATE_MASK)
        - _COORDINATE_BIAS,
        (value & _COORDINATE_MASK) - _COORDINATE_BIAS,
    )


def _validate_aligned_volume(
    volume: LocalVoxelVolume,
    size: float,
    *,
    allow_truncated_surface_evidence: bool,
) -> None:
    if not math.isclose(
        float(volume.voxel_size_m),
        size,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError("local volume does not use the requested cubic resolution")
    if volume.sampling_truncated and not allow_truncated_surface_evidence:
        raise ValueError("local volume has truncated surface evidence")
    for origin in volume.origin:
        grid_coordinate = float(origin) / size
        if not math.isclose(
            grid_coordinate,
            round(grid_coordinate),
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise ValueError("local volume is not aligned to the global cubic grid")


def _world_key(point: Point, size: float) -> CubicVoxelKey:
    return tuple(
        int(math.floor(float(point[axis]) / size))
        for axis in range(3)
    )  # type: ignore[return-value]


def _key_payload(key: CubicVoxelKey) -> list[int]:
    return [int(value) for value in key]


def _ordered_packed_edge(
    first: PackedCubicVoxelKey,
    second: PackedCubicVoxelKey,
) -> tuple[PackedCubicVoxelKey, PackedCubicVoxelKey]:
    return (first, second) if first < second else (second, first)
