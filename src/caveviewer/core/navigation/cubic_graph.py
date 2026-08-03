"""Compact implicit graph over orthogonal free-space voxels.

The prepared compatibility graph may coarsen horizontal buckets to keep an
explicit Python node/edge graph bounded.  This module preserves the cubic
voxel resolution instead: free cells are stored as packed integer keys and
their local adjacency is computed on demand.  Exact cached-mesh validation
remains a separate authority for any route selected from this evidence.
"""

from __future__ import annotations

from collections import deque
from collections.abc import (
    Callable,
    Collection,
    Iterator,
    Mapping,
    Sequence,
    Set,
)
from dataclasses import dataclass
import heapq
import math

from caveviewer.core.navigation.centerline import Point
from caveviewer.core.navigation.voxel_volume import LocalVoxelVolume


LEGACY_CUBIC_VOXEL_GRAPH_METHOD = "implicit_sparse_cubic_free_space_v1"
CUBIC_VOXEL_GRAPH_METHOD = "implicit_sparse_orthogonal_free_space_v2"

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


class CubicVoxelLimitExceededError(ValueError):
    """Raised when a bounded cubic merge exceeds its resident key budget."""

    def __init__(self, max_free_voxels: int) -> None:
        self.max_free_voxels = max(1, int(max_free_voxels))
        super().__init__("cubic voxel free-space limit exceeded")


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
class CubicVoxelPathSearchResult:
    """One bounded implicit-graph search and its capacity outcome."""

    path: CubicVoxelPath | None
    reason: str
    expanded_voxel_count: int
    discovered_voxel_count: int
    node_limit_reached: bool

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "reason": str(self.reason),
            "expanded_voxel_count": int(self.expanded_voxel_count),
            "discovered_voxel_count": int(self.discovered_voxel_count),
            "node_limit_reached": bool(self.node_limit_reached),
            "path": (
                None
                if self.path is None
                else self.path.diagnostic_payload()
            ),
        }


@dataclass(frozen=True)
class CubicVoxelRoutePathBuildResult:
    """A route-ordered cubic spine plus bounded construction evidence."""

    path: CubicVoxelPath | None
    details: Mapping[str, object]


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
    # Construction owns this set and never mutates it after publication. A
    # plain set avoids a second full-size allocation while freezing millions
    # of packed keys; callers receive no mutating graph API.
    packed_free_keys: Set[PackedCubicVoxelKey]
    vertical_voxel_size_m: float | None = None

    def __post_init__(self) -> None:
        size = float(self.voxel_size_m)
        vertical_size = float(
            size
            if self.vertical_voxel_size_m is None
            else self.vertical_voxel_size_m
        )
        if (
            not math.isfinite(size)
            or size <= 0.0
            or not math.isfinite(vertical_size)
            or vertical_size <= 0.0
        ):
            raise ValueError("orthogonal voxel sizes must be positive and finite")
        object.__setattr__(self, "voxel_size_m", size)
        object.__setattr__(self, "vertical_voxel_size_m", vertical_size)

    @classmethod
    def from_keys(
        cls,
        keys: Sequence[CubicVoxelKey],
        *,
        voxel_size_m: float = 1.0,
        vertical_voxel_size_m: float | None = None,
    ) -> "SparseCubicVoxelGraph":
        """Construct a graph from deterministic world-grid voxel keys."""
        return cls(
            voxel_size_m=float(voxel_size_m),
            packed_free_keys={pack_cubic_voxel_key(key) for key in keys},
            vertical_voxel_size_m=vertical_voxel_size_m,
        )

    @property
    def cell_size_m(self) -> tuple[float, float, float]:
        """Return the physical X/Y/Z key scale."""
        return (
            float(self.voxel_size_m),
            float(self.vertical_voxel_size_m),
            float(self.voxel_size_m),
        )

    @property
    def cell_volume_m3(self) -> float:
        x_size, y_size, z_size = self.cell_size_m
        return float(x_size * y_size * z_size)

    @property
    def cell_diagonal_m(self) -> float:
        return float(math.sqrt(sum(size * size for size in self.cell_size_m)))

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
        try:
            values = tuple(float(value) for value in point)
        except (TypeError, ValueError) as exc:
            raise ValueError("cubic graph point is malformed") from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError("cubic graph point must be finite")
        return tuple(  # type: ignore[return-value]
            math.floor(value / self.cell_size_m[axis])
            for axis, value in enumerate(values)
        )

    def voxel_center(self, key: CubicVoxelKey) -> Point:
        return tuple(
            (int(key[axis]) + 0.5) * self.cell_size_m[axis]
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
        radii = tuple(
            max(0, int(math.ceil(maximum / size)))
            for size in self.cell_size_m
        )
        best_key: CubicVoxelKey | None = None
        best_distance = math.inf
        for delta_x in range(-radii[0], radii[0] + 1):
            for delta_y in range(-radii[1], radii[1] + 1):
                for delta_z in range(-radii[2], radii[2] + 1):
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

    def keys_within_distance(
        self,
        point: Sequence[float],
        *,
        max_distance_m: float,
    ) -> tuple[tuple[CubicVoxelKey, float], ...]:
        """Return all nearby free keys ordered by physical distance."""
        try:
            if len(point) != 3:
                return ()
            target = tuple(float(value) for value in point)
        except (TypeError, ValueError):
            return ()
        if not all(math.isfinite(value) for value in target):
            return ()
        maximum = float(max_distance_m)
        if not math.isfinite(maximum) or maximum < 0.0:
            return ()
        base = self.world_key(target)
        radii = tuple(
            max(0, int(math.ceil(maximum / size)))
            for size in self.cell_size_m
        )
        candidates: list[tuple[CubicVoxelKey, float]] = []
        for delta_x in range(-radii[0], radii[0] + 1):
            for delta_y in range(-radii[1], radii[1] + 1):
                for delta_z in range(-radii[2], radii[2] + 1):
                    key = (
                        base[0] + delta_x,
                        base[1] + delta_y,
                        base[2] + delta_z,
                    )
                    if not self.contains_key(key):
                        continue
                    distance_m = math.dist(target, self.voxel_center(key))
                    if distance_m <= maximum + 1e-9:
                        candidates.append((key, float(distance_m)))
        return tuple(
            sorted(candidates, key=lambda item: (item[1], item[0]))
        )

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

    def connected_component(
        self,
        start_key: CubicVoxelKey,
        *,
        max_voxels: int | None = None,
    ) -> "SparseCubicVoxelGraph":
        """Return the six-connected component containing ``start_key``.

        Fixed cache chunks overlap, so their local flood fills are only
        candidate evidence.  Selecting one component after all occupied
        observations have been merged proves that the retained free voxels
        connect across every chunk seam without materializing explicit edges.
        """
        start = pack_cubic_voxel_key(start_key)
        if start not in self.packed_free_keys:
            return SparseCubicVoxelGraph(
                voxel_size_m=self.voxel_size_m,
                packed_free_keys=frozenset(),
                vertical_voxel_size_m=self.vertical_voxel_size_m,
            )
        limit = (
            len(self.packed_free_keys)
            if max_voxels is None
            else max(1, int(max_voxels))
        )
        selected: set[PackedCubicVoxelKey] = {start}
        queue = deque((start,))
        while queue:
            packed = queue.popleft()
            key = unpack_cubic_voxel_key(packed)
            for offset in _CARDINAL_OFFSETS:
                neighbor = pack_cubic_voxel_key(
                    (
                        key[0] + offset[0],
                        key[1] + offset[1],
                        key[2] + offset[2],
                    )
                )
                if (
                    neighbor in selected
                    or neighbor not in self.packed_free_keys
                ):
                    continue
                if len(selected) >= limit:
                    raise ValueError("cubic voxel component limit exceeded")
                selected.add(neighbor)
                queue.append(neighbor)
        return SparseCubicVoxelGraph(
            voxel_size_m=self.voxel_size_m,
            packed_free_keys=selected,
            vertical_voxel_size_m=self.vertical_voxel_size_m,
        )

    def keys(self) -> tuple[CubicVoxelKey, ...]:
        """Return deterministic unpacked keys for bounded cache-time work."""
        return tuple(
            unpack_cubic_voxel_key(value)
            for value in sorted(self.packed_free_keys)
        )

    def iter_keys(self) -> Iterator[CubicVoxelKey]:
        """Iterate unpacked keys without allocating another component copy."""
        for value in self.packed_free_keys:
            yield unpack_cubic_voxel_key(value)

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
        return self.find_path_to_any(
            start_key,
            (terminal_key,),
            allow_diagonal=allow_diagonal,
            max_expansions=max_expansions,
            blocked_edges=blocked_edges,
        ).path

    def find_path_to_any(
        self,
        start_key: CubicVoxelKey,
        terminal_keys: Collection[CubicVoxelKey],
        *,
        allow_diagonal: bool = True,
        max_expansions: int | None = None,
        blocked_edges: Collection[
            tuple[CubicVoxelKey, CubicVoxelKey]
        ] = (),
        blocked_keys: Collection[CubicVoxelKey] = (),
    ) -> CubicVoxelPathSearchResult:
        """Find a bounded path to any explicit terminal voxel."""
        terminals = frozenset(
            pack_cubic_voxel_key(key)
            for key in terminal_keys
            if self.contains_key(key)
        )
        if not terminals:
            return CubicVoxelPathSearchResult(
                path=None,
                reason="cubic_path_terminal_missing",
                expanded_voxel_count=0,
                discovered_voxel_count=0,
                node_limit_reached=False,
            )
        terminal_centers = tuple(
            self.voxel_center(unpack_cubic_voxel_key(value))
            for value in sorted(terminals)
        )
        return self._find_path(
            start_key,
            is_target=lambda packed, _point: packed in terminals,
            heuristic=lambda point: min(
                math.dist(point, terminal_center)
                for terminal_center in terminal_centers
            ),
            allow_diagonal=allow_diagonal,
            max_expansions=max_expansions,
            blocked_edges=blocked_edges,
            blocked_keys=blocked_keys,
        )

    def find_path_to_horizontal_gate(
        self,
        start_key: CubicVoxelKey,
        target_point: Sequence[float],
        *,
        max_horizontal_distance_m: float,
        allow_diagonal: bool = False,
        max_expansions: int | None = None,
        blocked_edges: Collection[
            tuple[CubicVoxelKey, CubicVoxelKey]
        ] = (),
        blocked_keys: Collection[CubicVoxelKey] = (),
    ) -> CubicVoxelPathSearchResult:
        """Find any start-connected Y layer at one ordered X/Z gate.

        Imported route Y is deliberately ignored. The fixed component and
        its six-connected topology select the executable cave layer.
        """
        try:
            if len(target_point) != 3:
                raise ValueError
            target_x = float(target_point[0])
            target_z = float(target_point[2])
            radius = float(max_horizontal_distance_m)
        except (TypeError, ValueError):
            return CubicVoxelPathSearchResult(
                path=None,
                reason="cubic_path_horizontal_gate_malformed",
                expanded_voxel_count=0,
                discovered_voxel_count=0,
                node_limit_reached=False,
            )
        if (
            not math.isfinite(target_x)
            or not math.isfinite(target_z)
            or not math.isfinite(radius)
            or radius < 0.0
        ):
            return CubicVoxelPathSearchResult(
                path=None,
                reason="cubic_path_horizontal_gate_malformed",
                expanded_voxel_count=0,
                discovered_voxel_count=0,
                node_limit_reached=False,
            )

        def horizontal_distance(point: Point) -> float:
            return math.hypot(point[0] - target_x, point[2] - target_z)

        return self._find_path(
            start_key,
            is_target=lambda _packed, point: (
                horizontal_distance(point) <= radius + 1e-9
            ),
            heuristic=lambda point: max(
                0.0,
                horizontal_distance(point) - radius,
            ),
            allow_diagonal=allow_diagonal,
            max_expansions=max_expansions,
            blocked_edges=blocked_edges,
            blocked_keys=blocked_keys,
        )

    def _find_path(
        self,
        start_key: CubicVoxelKey,
        *,
        is_target: Callable[[PackedCubicVoxelKey, Point], bool],
        heuristic: Callable[[Point], float],
        allow_diagonal: bool,
        max_expansions: int | None,
        blocked_edges: Collection[
            tuple[CubicVoxelKey, CubicVoxelKey]
        ],
        blocked_keys: Collection[CubicVoxelKey],
    ) -> CubicVoxelPathSearchResult:
        """Run one bounded implicit A* search for an internal target rule."""
        start = pack_cubic_voxel_key(start_key)
        if start not in self.packed_free_keys:
            return CubicVoxelPathSearchResult(
                path=None,
                reason="cubic_path_start_missing",
                expanded_voxel_count=0,
                discovered_voxel_count=0,
                node_limit_reached=False,
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
        blocked_voxels = frozenset(
            pack_cubic_voxel_key(key)
            for key in blocked_keys
            if key != start_key
        )
        start_point = self.voxel_center(start_key)
        distances: dict[PackedCubicVoxelKey, float] = {start: 0.0}
        previous: dict[PackedCubicVoxelKey, PackedCubicVoxelKey] = {}
        queue: list[tuple[float, float, PackedCubicVoxelKey]] = [
            (max(0.0, float(heuristic(start_point))), 0.0, start)
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
            key = unpack_cubic_voxel_key(packed)
            point = self.voxel_center(key)
            if is_target(packed, point):
                path_packed = [packed]
                while path_packed[-1] != start:
                    predecessor = previous.get(path_packed[-1])
                    if predecessor is None:
                        return CubicVoxelPathSearchResult(
                            path=None,
                            reason="cubic_path_predecessor_missing",
                            expanded_voxel_count=int(expanded_count),
                            discovered_voxel_count=len(distances),
                            node_limit_reached=False,
                        )
                    path_packed.append(predecessor)
                path_packed.reverse()
                keys = tuple(
                    unpack_cubic_voxel_key(value) for value in path_packed
                )
                return CubicVoxelPathSearchResult(
                    path=CubicVoxelPath(
                        keys=keys,
                        points=tuple(self.voxel_center(value) for value in keys),
                        distance_m=float(distance_m),
                        expanded_voxel_count=int(expanded_count),
                    ),
                    reason="cubic_path_built",
                    expanded_voxel_count=int(expanded_count),
                    discovered_voxel_count=len(distances),
                    node_limit_reached=False,
                )
            for offset in offsets:
                neighbor_key = (
                    key[0] + offset[0],
                    key[1] + offset[1],
                    key[2] + offset[2],
                )
                neighbor = pack_cubic_voxel_key(neighbor_key)
                if (
                    neighbor in closed
                    or neighbor in blocked_voxels
                    or neighbor not in self.packed_free_keys
                ):
                    continue
                if _ordered_packed_edge(packed, neighbor) in blocked:
                    continue
                if allow_diagonal and not self._diagonal_step_is_clear(
                    key,
                    offset,
                ):
                    continue
                step_distance = math.sqrt(
                    sum(
                        (offset[axis] * self.cell_size_m[axis]) ** 2
                        for axis in range(3)
                    )
                )
                candidate_distance = distance_m + step_distance
                existing = distances.get(neighbor)
                if existing is not None and candidate_distance >= existing - 1e-12:
                    continue
                neighbor_point = self.voxel_center(neighbor_key)
                distances[neighbor] = candidate_distance
                previous[neighbor] = packed
                heapq.heappush(
                    queue,
                    (
                        candidate_distance
                        + max(0.0, float(heuristic(neighbor_point))),
                        candidate_distance,
                        neighbor,
                    ),
                )
        limit_reached = bool(queue)
        return CubicVoxelPathSearchResult(
            path=None,
            reason=(
                "cubic_path_expansion_limit_reached"
                if limit_reached
                else "cubic_path_target_unreachable"
            ),
            expanded_voxel_count=int(expanded_count),
            discovered_voxel_count=len(distances),
            node_limit_reached=limit_reached,
        )

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "method": CUBIC_VOXEL_GRAPH_METHOD,
            "voxel_size_m": float(self.voxel_size_m),
            "vertical_voxel_size_m": float(self.vertical_voxel_size_m),
            "cell_size_m": [float(value) for value in self.cell_size_m],
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


def build_route_ordered_cubic_path(
    graph: SparseCubicVoxelGraph,
    route_guide_points: Sequence[Point],
    *,
    start_key: CubicVoxelKey,
    terminal_keys: Collection[CubicVoxelKey],
    horizontal_gate_radius_m: float,
    max_expansions: int,
    waypoint_key_groups: Sequence[Collection[CubicVoxelKey]] = (),
    require_waypoint_key_groups: bool = False,
    blocked_edges: Collection[
        tuple[CubicVoxelKey, CubicVoxelKey]
    ] = (),
    edge_is_clear: Callable[[CubicVoxelKey, CubicVoxelKey], bool] | None = None,
    max_blocked_edges: int = 4_096,
) -> CubicVoxelRoutePathBuildResult:
    """Build one bounded six-connected path through ordered X/Z gates.

    Intermediate route samples are ordering hints only. Their Y values never
    select a layer, and no intermediate gate is accepted as the terminal.
    Every leg consumes one shared expansion ledger so adding metadata samples
    cannot multiply the cache-time memory or work allowance.
    """
    guides = tuple(
        tuple(float(value) for value in point)
        for point in route_guide_points
        if len(point) == 3
        and all(math.isfinite(float(value)) for value in point)
    )
    limit = max(1, int(max_expansions))
    radius = max(float(graph.voxel_size_m), float(horizontal_gate_radius_m))
    terminal_set = tuple(
        sorted({key for key in terminal_keys if graph.contains_key(key)})
    )
    waypoint_groups = tuple(
        tuple(sorted({key for key in group if graph.contains_key(key)}))
        for group in waypoint_key_groups
    )
    waypoint_groups_supplied = bool(waypoint_key_groups)
    expected_intermediate_gate_count = max(0, len(guides) - 2)
    use_waypoint_targets = bool(
        require_waypoint_key_groups or waypoint_groups_supplied
    )
    base_details: dict[str, object] = {
        "method": "route_ordered_cubic_spine_v1",
        "adjacency": "six_connected",
        "route_guide_point_count": len(guides),
        "horizontal_gate_radius_m": float(radius),
        "max_expansions": int(limit),
        "terminal_candidate_count": len(terminal_set),
        "intermediate_gate_count": (
            len(waypoint_groups)
            if use_waypoint_targets
            else expected_intermediate_gate_count
        ),
        "expected_intermediate_gate_count": int(
            expected_intermediate_gate_count
        ),
        "intermediate_gate_source": (
            "surface_gap_free_voxel_candidates"
            if use_waypoint_targets
            else "horizontal_route_guide"
        ),
        "surface_gap_waypoints_required": bool(
            require_waypoint_key_groups
        ),
        "raw_route_y_used": False,
    }
    if len(guides) < 2 or not graph.contains_key(start_key) or not terminal_set:
        return CubicVoxelRoutePathBuildResult(
            path=None,
            details={
                **base_details,
                "reason": "route_ordered_cubic_spine_inputs_missing",
                "known_terminal_reached": False,
                "node_limit_reached": False,
            },
        )
    if require_waypoint_key_groups and (
        len(waypoint_groups) != expected_intermediate_gate_count
        or any(not group for group in waypoint_groups)
    ):
        return CubicVoxelRoutePathBuildResult(
            path=None,
            details={
                **base_details,
                "reason": "route_ordered_cubic_spine_waypoint_evidence_missing",
                "known_terminal_reached": False,
                "node_limit_reached": False,
            },
        )

    retained_keys: list[CubicVoxelKey] = [start_key]
    retained_key_set: set[CubicVoxelKey] = {start_key}
    current_key = start_key
    expanded_total = 0
    discovered_peak = 1
    reached_gate_count = 0
    exact_reroute_count = 0
    blocked_limit = max(1, int(max_blocked_edges))

    def ordered_edge(
        first: CubicVoxelKey,
        second: CubicVoxelKey,
    ) -> tuple[CubicVoxelKey, CubicVoxelKey]:
        return (first, second) if first < second else (second, first)

    working_blocked_edges = {
        ordered_edge(first, second) for first, second in blocked_edges
    }

    def rejected_path_edges(
        keys: Sequence[CubicVoxelKey],
    ) -> set[tuple[CubicVoxelKey, CubicVoxelKey]]:
        if edge_is_clear is None:
            return set()
        rejected: set[tuple[CubicVoxelKey, CubicVoxelKey]] = set()
        for first, second in zip(keys, keys[1:], strict=False):
            try:
                clear = bool(edge_is_clear(first, second))
            except Exception:
                clear = False
            if not clear:
                rejected.add(ordered_edge(first, second))
        return rejected

    def append_non_revisiting(keys: Sequence[CubicVoxelKey]) -> bool:
        for key in keys[1:]:
            if key in retained_key_set:
                return False
            retained_key_set.add(key)
            retained_keys.append(key)
        return True

    intermediate_targets: Sequence[object] = (
        waypoint_groups if use_waypoint_targets else guides[1:-1]
    )
    skipped_empty_gate_count = 0
    for guide_index, target in enumerate(intermediate_targets, start=1):
        if use_waypoint_targets and not target:
            return CubicVoxelRoutePathBuildResult(
                path=None,
                details={
                    **base_details,
                    "reason": "route_ordered_cubic_spine_waypoint_evidence_missing",
                    "expanded_voxel_count": int(expanded_total),
                    "discovered_voxel_peak": int(discovered_peak),
                    "reached_intermediate_gate_count": int(reached_gate_count),
                    "failed_route_guide_index": int(guide_index),
                    "blocked_edge_count": len(working_blocked_edges),
                    "known_terminal_reached": False,
                    "node_limit_reached": False,
                },
            )
        while True:
            remaining = limit - expanded_total
            if remaining <= 0:
                return CubicVoxelRoutePathBuildResult(
                    path=None,
                    details={
                        **base_details,
                        "reason": "route_ordered_cubic_spine_expansion_limit_reached",
                        "expanded_voxel_count": int(expanded_total),
                        "discovered_voxel_peak": int(discovered_peak),
                        "reached_intermediate_gate_count": int(reached_gate_count),
                        "skipped_empty_gate_count": int(skipped_empty_gate_count),
                        "failed_route_guide_index": int(guide_index),
                        "blocked_edge_count": len(working_blocked_edges),
                        "known_terminal_reached": False,
                        "node_limit_reached": True,
                    },
                )
            if use_waypoint_targets:
                search = graph.find_path_to_any(
                    current_key,
                    target,  # type: ignore[arg-type]
                    allow_diagonal=False,
                    max_expansions=remaining,
                    blocked_edges=working_blocked_edges,
                    blocked_keys=retained_key_set - {current_key},
                )
            else:
                search = graph.find_path_to_horizontal_gate(
                    current_key,
                    target,  # type: ignore[arg-type]
                    max_horizontal_distance_m=radius,
                    allow_diagonal=False,
                    max_expansions=remaining,
                    blocked_edges=working_blocked_edges,
                    blocked_keys=retained_key_set - {current_key},
                )
            expanded_total += int(search.expanded_voxel_count)
            discovered_peak = max(
                discovered_peak,
                int(search.discovered_voxel_count),
            )
            if search.path is None:
                return CubicVoxelRoutePathBuildResult(
                    path=None,
                    details={
                        **base_details,
                        "reason": (
                            "route_ordered_cubic_spine_expansion_limit_reached"
                            if search.node_limit_reached
                            else "route_ordered_cubic_spine_gate_unreachable"
                        ),
                        "search_reason": str(search.reason),
                        "expanded_voxel_count": int(expanded_total),
                        "discovered_voxel_peak": int(discovered_peak),
                        "reached_intermediate_gate_count": int(reached_gate_count),
                        "skipped_empty_gate_count": int(skipped_empty_gate_count),
                        "failed_route_guide_index": int(guide_index),
                        "blocked_edge_count": len(working_blocked_edges),
                        "known_terminal_reached": False,
                        "node_limit_reached": bool(search.node_limit_reached),
                    },
                )
            rejected = rejected_path_edges(search.path.keys)
            new_rejections = rejected - working_blocked_edges
            working_blocked_edges.update(rejected)
            if len(working_blocked_edges) > blocked_limit:
                return CubicVoxelRoutePathBuildResult(
                    path=None,
                    details={
                        **base_details,
                        "reason": "route_ordered_cubic_spine_edge_rejection_limit_reached",
                        "expanded_voxel_count": int(expanded_total),
                        "discovered_voxel_peak": int(discovered_peak),
                        "reached_intermediate_gate_count": int(reached_gate_count),
                        "failed_route_guide_index": int(guide_index),
                        "blocked_edge_count": len(working_blocked_edges),
                        "known_terminal_reached": False,
                        "node_limit_reached": False,
                    },
                )
            if rejected and new_rejections:
                exact_reroute_count += 1
                continue
            if rejected:
                return CubicVoxelRoutePathBuildResult(
                    path=None,
                    details={
                        **base_details,
                        "reason": "route_ordered_cubic_spine_exact_edge_unreachable",
                        "expanded_voxel_count": int(expanded_total),
                        "discovered_voxel_peak": int(discovered_peak),
                        "reached_intermediate_gate_count": int(reached_gate_count),
                        "failed_route_guide_index": int(guide_index),
                        "blocked_edge_count": len(working_blocked_edges),
                        "known_terminal_reached": False,
                        "node_limit_reached": False,
                    },
                )
            break
        if not append_non_revisiting(search.path.keys):
            return CubicVoxelRoutePathBuildResult(
                path=None,
                details={
                    **base_details,
                    "reason": "route_ordered_cubic_spine_revisit_detected",
                    "expanded_voxel_count": int(expanded_total),
                    "discovered_voxel_peak": int(discovered_peak),
                    "reached_intermediate_gate_count": int(reached_gate_count),
                    "failed_route_guide_index": int(guide_index),
                    "blocked_edge_count": len(working_blocked_edges),
                    "known_terminal_reached": False,
                    "node_limit_reached": False,
                },
            )
        current_key = retained_keys[-1]
        reached_gate_count += 1

    final_search: CubicVoxelPathSearchResult | None = None
    while True:
        remaining = limit - expanded_total
        if remaining <= 0:
            final_search = None
            break
        final_search = graph.find_path_to_any(
            current_key,
            terminal_set,
            allow_diagonal=False,
            max_expansions=remaining,
            blocked_edges=working_blocked_edges,
            blocked_keys=retained_key_set - {current_key},
        )
        expanded_total += int(final_search.expanded_voxel_count)
        discovered_peak = max(
            discovered_peak,
            int(final_search.discovered_voxel_count),
        )
        if final_search.path is None:
            break
        rejected = rejected_path_edges(final_search.path.keys)
        new_rejections = rejected - working_blocked_edges
        working_blocked_edges.update(rejected)
        if len(working_blocked_edges) > blocked_limit:
            return CubicVoxelRoutePathBuildResult(
                path=None,
                details={
                    **base_details,
                    "reason": "route_ordered_cubic_spine_edge_rejection_limit_reached",
                    "expanded_voxel_count": int(expanded_total),
                    "discovered_voxel_peak": int(discovered_peak),
                    "reached_intermediate_gate_count": int(reached_gate_count),
                    "blocked_edge_count": len(working_blocked_edges),
                    "known_terminal_reached": False,
                    "node_limit_reached": False,
                },
            )
        if rejected and new_rejections:
            exact_reroute_count += 1
            continue
        if rejected:
            final_search = None
        break
    if final_search is None or final_search.path is None:
        limit_reached = bool(
            final_search is None or final_search.node_limit_reached
        )
        return CubicVoxelRoutePathBuildResult(
            path=None,
            details={
                **base_details,
                "reason": (
                    "route_ordered_cubic_spine_expansion_limit_reached"
                    if limit_reached
                    else "route_ordered_cubic_spine_terminal_unreachable"
                ),
                "search_reason": (
                    "cubic_path_expansion_limit_reached"
                    if final_search is None
                    else str(final_search.reason)
                ),
                "expanded_voxel_count": int(expanded_total),
                "discovered_voxel_peak": int(discovered_peak),
                "reached_intermediate_gate_count": int(reached_gate_count),
                "skipped_empty_gate_count": int(skipped_empty_gate_count),
                "blocked_edge_count": len(working_blocked_edges),
                "known_terminal_reached": False,
                "node_limit_reached": limit_reached,
            },
        )
    if not append_non_revisiting(final_search.path.keys):
        return CubicVoxelRoutePathBuildResult(
            path=None,
            details={
                **base_details,
                "reason": "route_ordered_cubic_spine_revisit_detected",
                "expanded_voxel_count": int(expanded_total),
                "discovered_voxel_peak": int(discovered_peak),
                "reached_intermediate_gate_count": int(reached_gate_count),
                "blocked_edge_count": len(working_blocked_edges),
                "known_terminal_reached": False,
                "node_limit_reached": False,
            },
        )
    points = tuple(graph.voxel_center(key) for key in retained_keys)
    distance_m = sum(
        math.dist(first, second)
        for first, second in zip(points, points[1:], strict=False)
    )
    path = CubicVoxelPath(
        keys=tuple(retained_keys),
        points=points,
        distance_m=float(distance_m),
        expanded_voxel_count=int(expanded_total),
    )
    return CubicVoxelRoutePathBuildResult(
        path=path,
        details={
            **base_details,
            "reason": "route_ordered_cubic_spine_built",
            "expanded_voxel_count": int(expanded_total),
            "discovered_voxel_peak": int(discovered_peak),
            "reached_intermediate_gate_count": int(reached_gate_count),
            "skipped_empty_gate_count": int(skipped_empty_gate_count),
            "blocked_edge_count": len(working_blocked_edges),
            "exact_reroute_count": int(exact_reroute_count),
            "known_terminal_reached": True,
            "node_limit_reached": False,
            "non_circular_path": True,
            "path": path.diagnostic_payload(),
        },
    )


def build_cubic_graph_from_local_volumes(
    volumes: Sequence[tuple[LocalVoxelVolume, Sequence[Point]]],
    *,
    voxel_size_m: float = 1.0,
    vertical_voxel_size_m: float | None = None,
    minimum_clearance_m: float = 0.0,
    point_filter: PointFilter | None = None,
    include_all_filtered_free_cells: bool = False,
    allow_truncated_surface_evidence: bool = False,
    max_free_voxels: int | None = None,
) -> CubicVoxelGraphBuildResult:
    """Merge aligned local fields into one conservative implicit graph.

    Every source volume must already use the requested orthogonal resolution
    and align to the same global grid. Surface occupancy from any overlapping
    tile wins over free-space evidence from another tile. This is intentionally
    fail-closed; truncated evidence is rejected unless a diagnostic caller
    explicitly opts in, and exact mesh checks are still required before route
    execution.
    """
    size = float(voxel_size_m)
    vertical_size = float(
        size if vertical_voxel_size_m is None else vertical_voxel_size_m
    )
    clearance = float(minimum_clearance_m)
    if (
        not math.isfinite(size)
        or size <= 0.0
        or not math.isfinite(vertical_size)
        or vertical_size <= 0.0
    ):
        raise ValueError("orthogonal graph voxel sizes must be positive and finite")
    cell_size = (size, vertical_size, size)
    if not math.isfinite(clearance) or clearance < 0.0:
        raise ValueError("cubic graph clearance must be non-negative and finite")
    free_limit = (
        None
        if max_free_voxels is None
        else max(1, int(max_free_voxels))
    )
    free: set[PackedCubicVoxelKey] = set()
    blocked: set[PackedCubicVoxelKey] = set()
    source_free_count = 0
    clearance_rejection_count = 0
    point_filter_rejection_count = 0
    duplicate_free_count = 0
    truncated_source_volume_count = 0
    validated_volumes: list[
        tuple[LocalVoxelVolume, Sequence[Point]]
    ] = []
    for volume, seed_points in volumes:
        _validate_aligned_volume(
            volume,
            cell_size,
            allow_truncated_surface_evidence=bool(
                allow_truncated_surface_evidence
            ),
        )
        if volume.sampling_truncated:
            truncated_source_volume_count += 1
        validated_volumes.append((volume, seed_points))
        for local_key in volume.surface_cells:
            blocked.add(
                pack_cubic_voxel_key(
                    _world_key(volume.voxel_center(local_key), cell_size)
                )
            )

    # Occupancy from every overlapping tile must be known before any free
    # cell is retained.  Besides making occupied-wins literal, this prevents
    # globally blocked keys from consuming the bounded free-key budget.
    blocked_free: set[PackedCubicVoxelKey] = set()
    for volume, seed_points in validated_volumes:
        if include_all_filtered_free_cells:
            free_cells = volume.iter_all_free_cell_clearance_m()
        else:
            free_cells = iter(
                volume.filled_free_cell_clearance_m(seed_points).items()
            )
        for local_key, clearance_m in free_cells:
            source_free_count += 1
            if float(clearance_m) + 1e-9 < clearance:
                clearance_rejection_count += 1
                continue
            center = volume.voxel_center(local_key)
            if point_filter is not None and not point_filter(center):
                point_filter_rejection_count += 1
                continue
            packed = pack_cubic_voxel_key(_world_key(center, cell_size))
            if packed in blocked:
                blocked_free.add(packed)
                continue
            if packed in free:
                duplicate_free_count += 1
            free.add(packed)
            if free_limit is not None and len(free) > free_limit:
                raise CubicVoxelLimitExceededError(free_limit)
    graph = SparseCubicVoxelGraph(
        voxel_size_m=size,
        packed_free_keys=free,
        vertical_voxel_size_m=vertical_size,
    )
    return CubicVoxelGraphBuildResult(
        graph=graph,
        details={
            **graph.diagnostic_payload(),
            "source_volume_count": len(volumes),
            "source_free_space_method": (
                "all_filtered_non_surface_cells_v1"
                if include_all_filtered_free_cells
                else "seeded_local_flood_fill_v1"
            ),
            "source_filled_free_voxel_count": int(source_free_count),
            "surface_blocked_voxel_count": len(blocked),
            "blocked_free_conflict_count": len(blocked_free),
            "clearance_rejection_count": int(clearance_rejection_count),
            "point_filter_rejection_count": int(point_filter_rejection_count),
            "duplicate_free_voxel_count": int(duplicate_free_count),
            "truncated_source_volume_count": int(
                truncated_source_volume_count
            ),
            "max_free_voxels": free_limit,
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
    cell_size: tuple[float, float, float],
    *,
    allow_truncated_surface_evidence: bool,
) -> None:
    if not math.isclose(
        float(volume.voxel_size_m),
        cell_size[0],
        rel_tol=0.0,
        abs_tol=1e-6,
    ) or not math.isclose(
        float(volume.vertical_voxel_size_m),
        cell_size[1],
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError(
            "local volume does not use the requested orthogonal resolution"
        )
    if volume.sampling_truncated and not allow_truncated_surface_evidence:
        raise ValueError("local volume has truncated surface evidence")
    for axis, origin in enumerate(volume.origin):
        grid_coordinate = float(origin) / cell_size[axis]
        if not math.isclose(
            grid_coordinate,
            round(grid_coordinate),
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise ValueError(
                "local volume is not aligned to the global orthogonal grid"
            )


def _world_key(
    point: Point,
    cell_size: Sequence[float],
) -> CubicVoxelKey:
    return tuple(
        int(math.floor(float(point[axis]) / float(cell_size[axis])))
        for axis in range(3)
    )  # type: ignore[return-value]


def _key_payload(key: CubicVoxelKey) -> list[int]:
    return [int(value) for value in key]


def _ordered_packed_edge(
    first: PackedCubicVoxelKey,
    second: PackedCubicVoxelKey,
) -> tuple[PackedCubicVoxelKey, PackedCubicVoxelKey]:
    return (first, second) if first < second else (second, first)
