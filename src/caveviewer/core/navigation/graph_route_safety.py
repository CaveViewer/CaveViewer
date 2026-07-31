"""Safety validation for routes produced by the prepared true-3D graph.

This module is intentionally independent from centerline planning.  A graph
route is accepted from graph topology and graph clearance evidence, then
checked against the persisted voxel atlas and the cached chunk mesh before it
can be executed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math

from caveviewer.core.navigation.mesh_collision import CachedChunkMeshCollisionGuard
from caveviewer.core.navigation.voxel_cache import NavigationVoxelAtlas
from caveviewer.core.navigation.voxel_graph_3d import (
    NavigationVoxel3DGraph,
    VoxelGraphKey,
)


Point = tuple[float, float, float]


@dataclass(frozen=True)
class GraphRouteSafetyPolicy:
    """Explicit clearance policy for graph-native motion validation."""

    minimum_clearance_m: float = 0.0
    sample_spacing_m: float | None = None

    def __post_init__(self) -> None:
        clearance = float(self.minimum_clearance_m)
        if not math.isfinite(clearance) or clearance < 0.0:
            raise ValueError("minimum graph clearance must be finite and non-negative")
        if self.sample_spacing_m is not None:
            spacing = float(self.sample_spacing_m)
            if not math.isfinite(spacing) or spacing <= 0.0:
                raise ValueError("graph safety sample spacing must be positive")


@dataclass(frozen=True)
class GraphRouteSafetyFailure:
    """First graph-native safety failure for a route candidate."""

    kind: str
    reason: str
    index: int | None = None
    segment_index: int | None = None
    point: Point | None = None
    first: Point | None = None
    second: Point | None = None
    node_key: VoxelGraphKey | None = None
    edge_source: VoxelGraphKey | None = None
    edge_target: VoxelGraphKey | None = None
    clearance_m: float | None = None
    required_clearance_m: float | None = None

    def diagnostic_payload(self) -> dict[str, object]:
        return {
            "kind": str(self.kind),
            "reason": str(self.reason),
            "index": self.index,
            "segment_index": self.segment_index,
            "point": _point_payload(self.point),
            "first": _point_payload(self.first),
            "second": _point_payload(self.second),
            "node_key": _key_payload(self.node_key),
            "edge_source": _key_payload(self.edge_source),
            "edge_target": _key_payload(self.edge_target),
            "clearance_m": (
                None if self.clearance_m is None else float(self.clearance_m)
            ),
            "required_clearance_m": (
                None
                if self.required_clearance_m is None
                else float(self.required_clearance_m)
            ),
        }


class GraphRouteSafetyValidator:
    """Validate a graph route without consulting centerline metadata."""

    def __init__(
        self,
        atlas: NavigationVoxelAtlas,
        graph: NavigationVoxel3DGraph,
        *,
        mesh_guard: CachedChunkMeshCollisionGuard | None = None,
        policy: GraphRouteSafetyPolicy | None = None,
    ) -> None:
        self.atlas = atlas
        self.graph = graph
        self.mesh_guard = mesh_guard
        self.policy = policy or GraphRouteSafetyPolicy()
        grid_sizes = [
            float(value)
            for value in graph.grid_size_m
            if math.isfinite(float(value)) and float(value) > 0.0
        ]
        fallback = max(0.25, float(atlas.voxel_size_m or 0.0))
        self._sample_spacing_m = max(
            0.25,
            float(
                self.policy.sample_spacing_m
                if self.policy.sample_spacing_m is not None
                else min(grid_sizes or [fallback]) * 0.5
            ),
        )

    @property
    def cell_size(self) -> float:
        """Return the graph scale used by consumers for replan distance."""
        grid_sizes = [
            float(value)
            for value in self.graph.grid_size_m
            if math.isfinite(float(value)) and float(value) > 0.0
        ]
        return max(grid_sizes or [float(self.atlas.voxel_size_m or 1.0)])

    @property
    def has_mesh_collision_guard(self) -> bool:
        return self.mesh_guard is not None

    def route_clearance_failure(
        self,
        route_points: Sequence[Sequence[float]],
        graph_keys: Sequence[VoxelGraphKey],
        *,
        start_graph_key: VoxelGraphKey | None = None,
    ) -> GraphRouteSafetyFailure | None:
        """Return the first graph, voxel, or mesh failure for one route."""
        points = tuple(_point(point) for point in route_points)
        keys = tuple(graph_keys)
        if len(points) < 2:
            return GraphRouteSafetyFailure(kind="route", reason="empty_or_short_route")
        if not keys:
            return GraphRouteSafetyFailure(kind="route", reason="graph_route_keys_missing")

        start_key = start_graph_key or keys[0]
        start_node = self.graph.nodes.get(start_key)
        if start_node is None:
            return GraphRouteSafetyFailure(
                kind="graph_node",
                reason="graph_start_node_missing",
                node_key=start_key,
                point=points[0],
            )

        camera_matches_start = _distance_squared(
            points[0],
            start_node.center,
        ) <= 1e-12
        if camera_matches_start:
            failure = self._node_failure(
                start_key,
                index=0,
                kind="point",
                point=points[0],
            )
        else:
            failure = self._point_failure(
                points[0],
                index=0,
                kind="camera_point",
                uncovered_reason="camera_point_uncovered",
            )
        if failure is not None:
            return failure

        if not camera_matches_start:
            failure = self._segment_failure(
                points[0],
                start_node.center,
                segment_index=0,
                kind="camera_connector",
                uncovered_reason="camera_to_graph_start_uncovered",
            )
            if failure is not None:
                return failure

        for index, key in enumerate(keys):
            node = self.graph.nodes.get(key)
            if node is None:
                return GraphRouteSafetyFailure(
                    kind="graph_node",
                    reason="graph_node_missing",
                    index=index,
                    node_key=key,
                )
            failure = self._node_failure(
                key,
                index=index + 1,
                kind="graph_node",
                point=tuple(float(value) for value in node.center),
            )
            if failure is not None:
                return failure

        for edge_index, (source, target) in enumerate(
            zip(keys, keys[1:], strict=False)
        ):
            failure = self.edge_clearance_failure(
                source,
                target,
                segment_index=edge_index,
            )
            if failure is not None:
                return failure

        for segment_index, (first, second) in enumerate(
            zip(points, points[1:], strict=False)
        ):
            failure = self._segment_failure(
                first,
                second,
                segment_index=segment_index,
                kind="segment",
                uncovered_reason="graph_route_uncovered",
            )
            if failure is not None:
                return failure
        return None

    def edge_clearance_failure(
        self,
        source: VoxelGraphKey,
        target: VoxelGraphKey,
        *,
        segment_index: int = 0,
    ) -> GraphRouteSafetyFailure | None:
        """Return the exact safety failure for one executable graph edge.

        Prepared graph line-of-sight is topology evidence, not mesh permission.
        Keeping this check public lets graph search reject a mesh-blocked edge
        before it becomes part of a preflight or runtime route.
        """
        failure = self._edge_failure(
            source,
            target,
            segment_index=segment_index,
        )
        if failure is not None:
            return failure
        source_node = self.graph.nodes.get(source)
        target_node = self.graph.nodes.get(target)
        if source_node is None or target_node is None:
            return GraphRouteSafetyFailure(
                kind="graph_edge",
                reason="graph_edge_node_missing",
                segment_index=segment_index,
                edge_source=source,
                edge_target=target,
            )
        failure = self._segment_failure(
            tuple(float(value) for value in source_node.center),
            tuple(float(value) for value in target_node.center),
            segment_index=segment_index,
            kind="graph_edge",
            uncovered_reason="graph_edge_uncovered",
        )
        if failure is None:
            return None
        return GraphRouteSafetyFailure(
            kind=failure.kind,
            reason=failure.reason,
            index=failure.index,
            segment_index=failure.segment_index,
            point=failure.point,
            first=failure.first,
            second=failure.second,
            node_key=failure.node_key,
            edge_source=source,
            edge_target=target,
            clearance_m=failure.clearance_m,
            required_clearance_m=failure.required_clearance_m,
        )

    def segment_clearance_failure(
        self,
        first: Sequence[float],
        second: Sequence[float],
        *,
        segment_index: int = 0,
        kind: str = "route_segment",
        uncovered_reason: str = "route_segment_uncovered",
    ) -> GraphRouteSafetyFailure | None:
        """Validate one executable geometric segment.

        Callers that already validated their graph topology can recheck a
        published camera segment against cached voxel evidence and the exact
        mesh without pretending that its endpoints belong to one graph.
        Fixed routes use this for seams between prepared and refined segments.
        """
        return self._segment_failure(
            _point(first),
            _point(second),
            segment_index=int(segment_index),
            kind=str(kind),
            uncovered_reason=str(uncovered_reason),
        )

    def _node_failure(
        self,
        key: VoxelGraphKey,
        *,
        index: int | None,
        kind: str,
        point: Point | None,
    ) -> GraphRouteSafetyFailure | None:
        node = self.graph.nodes.get(key)
        if node is None:
            return GraphRouteSafetyFailure(
                kind=kind,
                reason="graph_node_missing",
                index=index,
                node_key=key,
                point=point,
            )
        minimum = float(node.min_clearance_m)
        if not math.isfinite(minimum):
            return GraphRouteSafetyFailure(
                kind=kind,
                reason="graph_node_clearance_invalid",
                index=index,
                node_key=key,
                point=point,
            )
        required = float(self.policy.minimum_clearance_m)
        if minimum + 1e-9 < required:
            return GraphRouteSafetyFailure(
                kind=kind,
                reason="graph_node_clearance_below_policy",
                index=index,
                node_key=key,
                point=point,
                clearance_m=minimum,
                required_clearance_m=required,
            )
        return self._point_failure(
            point or tuple(float(value) for value in node.center),
            index=index,
            kind=kind,
            node_key=key,
            uncovered_reason="graph_node_uncovered",
            include_clearance=False,
        )

    def _edge_failure(
        self,
        source: VoxelGraphKey,
        target: VoxelGraphKey,
        *,
        segment_index: int,
    ) -> GraphRouteSafetyFailure | None:
        edge = next(
            (
                candidate
                for candidate in self.graph.outgoing(source)
                if candidate.target == target
            ),
            None,
        )
        if edge is None:
            return GraphRouteSafetyFailure(
                kind="graph_edge",
                reason="graph_edge_missing",
                segment_index=segment_index,
                edge_source=source,
                edge_target=target,
            )
        if not edge.line_of_sight:
            return GraphRouteSafetyFailure(
                kind="graph_edge",
                reason="graph_edge_not_line_of_sight",
                segment_index=segment_index,
                edge_source=source,
                edge_target=target,
            )
        if not math.isfinite(float(edge.distance_m)) or float(edge.distance_m) <= 0.0:
            return GraphRouteSafetyFailure(
                kind="graph_edge",
                reason="graph_edge_distance_invalid",
                segment_index=segment_index,
                edge_source=source,
                edge_target=target,
            )
        clearance = float(edge.min_clearance_m)
        required = float(self.policy.minimum_clearance_m)
        if not math.isfinite(clearance):
            return GraphRouteSafetyFailure(
                kind="graph_edge",
                reason="graph_edge_clearance_invalid",
                segment_index=segment_index,
                edge_source=source,
                edge_target=target,
            )
        if clearance + 1e-9 < required:
            return GraphRouteSafetyFailure(
                kind="graph_edge",
                reason="graph_edge_clearance_below_policy",
                segment_index=segment_index,
                edge_source=source,
                edge_target=target,
                clearance_m=clearance,
                required_clearance_m=required,
            )
        source_node = self.graph.nodes.get(source)
        target_node = self.graph.nodes.get(target)
        if source_node is None or target_node is None:
            return GraphRouteSafetyFailure(
                kind="graph_edge",
                reason="graph_edge_node_missing",
                segment_index=segment_index,
                edge_source=source,
                edge_target=target,
            )
        if int(source_node.component_id) != int(target_node.component_id):
            return GraphRouteSafetyFailure(
                kind="graph_edge",
                reason="graph_edge_crosses_component",
                segment_index=segment_index,
                edge_source=source,
                edge_target=target,
            )
        return None

    def _point_failure(
        self,
        point: Point,
        *,
        index: int | None,
        kind: str,
        node_key: VoxelGraphKey | None = None,
        uncovered_reason: str,
        include_clearance: bool = True,
    ) -> GraphRouteSafetyFailure | None:
        probe = self.atlas.probe_point(
            point,
            include_clearance=include_clearance,
        )
        if probe is None:
            # Unit-sized graph fixtures may intentionally contain only graph
            # evidence. Real persisted caches have tile/chunk coverage and
            # must prove every executable sample through the atlas.
            if node_key is not None or not self._has_persisted_coverage:
                return None
            return GraphRouteSafetyFailure(
                kind=kind,
                reason=uncovered_reason,
                index=index,
                point=point,
                node_key=node_key,
            )
        is_free, clearance = bool(probe[0]), float(probe[1])
        if not is_free:
            return GraphRouteSafetyFailure(
                kind=kind,
                reason="graph_point_blocked",
                index=index,
                point=point,
                node_key=node_key,
                clearance_m=max(0.0, clearance),
            )
        required = float(self.policy.minimum_clearance_m)
        if (
            include_clearance
            and (not math.isfinite(clearance) or clearance + 1e-9 < required)
        ):
            return GraphRouteSafetyFailure(
                kind=kind,
                reason="graph_point_clearance_below_policy",
                index=index,
                point=point,
                node_key=node_key,
                clearance_m=clearance,
                required_clearance_m=required,
            )
        return None

    def _segment_failure(
        self,
        first: Point,
        second: Point,
        *,
        segment_index: int,
        kind: str,
        uncovered_reason: str,
    ) -> GraphRouteSafetyFailure | None:
        distance = math.sqrt(_distance_squared(first, second))
        if distance <= 1e-9:
            return GraphRouteSafetyFailure(
                kind=kind,
                reason="zero_length_segment",
                segment_index=segment_index,
                first=first,
                second=second,
            )
        steps = max(1, int(math.ceil(distance / self._sample_spacing_m)))
        for step in range(steps + 1):
            fraction = float(step) / float(steps)
            point = tuple(
                float(first[axis])
                + (float(second[axis]) - float(first[axis])) * fraction
                for axis in range(3)
            )
            failure = self._point_failure(
                point,
                index=step,
                kind="segment_point",
                uncovered_reason=uncovered_reason,
            )
            if failure is not None:
                return GraphRouteSafetyFailure(
                    kind=failure.kind,
                    reason=failure.reason,
                    index=failure.index,
                    segment_index=segment_index,
                    point=failure.point,
                    first=first,
                    second=second,
                    node_key=failure.node_key,
                    clearance_m=failure.clearance_m,
                    required_clearance_m=failure.required_clearance_m,
                )
        if self.mesh_guard is not None:
            hit = self.mesh_guard.segment_collision(first, second)
            if hit is not None:
                return GraphRouteSafetyFailure(
                    kind="segment",
                    reason="mesh_intersection",
                    segment_index=segment_index,
                    point=tuple(float(value) for value in hit.point),
                    first=first,
                    second=second,
                )
        return None

    @property
    def _has_persisted_coverage(self) -> bool:
        return bool(
            self.atlas.tiles
            or self.atlas.fine_tiles
            or self.atlas.chunk_store is not None
        )


def _point(value: Sequence[float]) -> Point:
    if len(value) != 3:
        raise ValueError("graph route points must be 3D")
    point = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in point):
        raise ValueError("graph route points must be finite")
    return point  # type: ignore[return-value]


def _distance_squared(first: Sequence[float], second: Sequence[float]) -> float:
    return sum(
        (float(first[index]) - float(second[index])) ** 2
        for index in range(3)
    )


def _point_payload(point: Point | None) -> list[float] | None:
    return None if point is None else [float(value) for value in point]


def _key_payload(key: VoxelGraphKey | None) -> list[int] | None:
    return None if key is None else [int(value) for value in key]
