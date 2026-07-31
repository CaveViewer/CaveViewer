"""Storage backends for prepared navigation voxel chunks.

The navigation graph is intentionally not part of this module. It is loaded
once by the navigation cache and remains resident in memory. This module owns
only the dense voxel payloads used for clearance, refinement, and recovery.

Both backends expose the same small API:

* :class:`InMemoryNavigationVoxelChunkStore` keeps every tile resident.
* :class:`DiskNavigationVoxelChunkStore` loads individual persisted tiles and
  evicts them with a bounded LRU policy.

The interface is independent of render streaming so route planning can later
request navigation prefetches without affecting OpenGL chunk residency.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import binascii
import math
import os
import posixpath
import threading
import zlib

from caveviewer.core.json_io import load_bounded_json
from caveviewer.core.navigation.voxel_volume import LocalVoxelVolume


Point = tuple[float, float, float]
ChunkDecoder = Callable[[Mapping[str, object]], LocalVoxelVolume]

NAVIGATION_VOXEL_CHUNK_STORAGE_METHOD = "navigation_voxel_chunks_v1"
DEFAULT_NAVIGATION_VOXEL_CHUNK_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_NAVIGATION_VOXEL_CHUNK_MAX_RESIDENT = 8


def navigation_voxel_chunk_relative_path_parts(
    relative_path: str,
) -> tuple[str, ...] | None:
    """Parse one portable, cache-relative chunk path.

    Chunk paths are persisted in JSON with POSIX separators regardless of the
    host platform.  Native ``os.path`` normalization would rewrite those
    separators on Windows and incorrectly reject a valid cache descriptor.
    """
    if not relative_path or "\\" in relative_path:
        return None
    if (
        posixpath.isabs(relative_path)
        or posixpath.normpath(relative_path) != relative_path
    ):
        return None
    parts = tuple(relative_path.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


@dataclass(frozen=True)
class NavigationVoxelChunkDescriptor:
    """Small resident index entry for one persisted voxel tile."""

    chunk_id: str
    kind: str
    bounds_min: Point
    bounds_max: Point
    voxel_size_m: float
    voxel_count: int
    surface_cell_count: int
    relative_path: str | None = None
    available_volume_m3: float = 0.0

    def contains_point(self, point: Sequence[float]) -> bool:
        """Return whether the descriptor can contain a world-space point."""
        try:
            if len(point) != 3:
                return False
            return all(
                self.bounds_min[axis]
                <= float(point[axis])
                < self.bounds_max[axis]
                for axis in range(3)
            )
        except (IndexError, TypeError, ValueError):
            return False

    def payload(self) -> dict[str, object]:
        """Return a bounded JSON-safe descriptor."""
        return {
            "id": str(self.chunk_id),
            "kind": str(self.kind),
            "bounds_min": [float(value) for value in self.bounds_min],
            "bounds_max": [float(value) for value in self.bounds_max],
            "voxel_size_m": float(self.voxel_size_m),
            "voxel_count": int(self.voxel_count),
            "surface_cell_count": int(self.surface_cell_count),
            "relative_path": self.relative_path,
            "available_volume_m3": float(self.available_volume_m3),
        }

    @classmethod
    def from_payload(cls, value: object) -> "NavigationVoxelChunkDescriptor":
        """Validate and restore one persisted descriptor."""
        if not isinstance(value, Mapping):
            raise ValueError("navigation voxel chunk descriptor is malformed")
        try:
            chunk_id = str(value["id"])
            kind = str(value["kind"])
            bounds_min = _point(value["bounds_min"], "bounds_min")
            bounds_max = _point(value["bounds_max"], "bounds_max")
            voxel_size = float(value["voxel_size_m"])
            voxel_count = int(value["voxel_count"])
            surface_cell_count = int(value["surface_cell_count"])
            available_volume = float(value.get("available_volume_m3", 0.0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "navigation voxel chunk descriptor is malformed"
            ) from exc
        relative_path = value.get("relative_path")
        if relative_path is not None and not isinstance(relative_path, str):
            raise ValueError("navigation voxel chunk path is malformed")
        if (
            not chunk_id
            or kind not in {"coarse", "fine"}
            or not math.isfinite(voxel_size)
            or voxel_size <= 0.0
            or voxel_count < 0
            or surface_cell_count < 0
            or not math.isfinite(available_volume)
            or available_volume < 0.0
            or any(
                not math.isfinite(value)
                for value in (*bounds_min, *bounds_max)
            )
            or any(
                bounds_min[axis] >= bounds_max[axis]
                for axis in range(3)
            )
        ):
            raise ValueError("navigation voxel chunk descriptor is invalid")
        return cls(
            chunk_id=chunk_id,
            kind=kind,
            bounds_min=bounds_min,
            bounds_max=bounds_max,
            voxel_size_m=voxel_size,
            voxel_count=voxel_count,
            surface_cell_count=surface_cell_count,
            relative_path=relative_path,
            available_volume_m3=available_volume,
        )

    @classmethod
    def from_volume(
        cls,
        chunk_id: str,
        kind: str,
        volume: LocalVoxelVolume,
        *,
        relative_path: str | None = None,
        available_volume_m3: float = 0.0,
    ) -> "NavigationVoxelChunkDescriptor":
        """Describe an already-built local voxel volume."""
        return cls(
            chunk_id=str(chunk_id),
            kind=str(kind),
            bounds_min=tuple(float(value) for value in volume.bounds_min),
            bounds_max=tuple(float(value) for value in volume.bounds_max),
            voxel_size_m=float(volume.voxel_size_m),
            voxel_count=int(volume.voxel_count),
            surface_cell_count=len(volume.surface_cells),
            relative_path=relative_path,
            available_volume_m3=max(0.0, float(available_volume_m3)),
        )


class NavigationVoxelChunkStore:
    """Common bounded storage interface used by the navigation atlas."""

    def descriptors(
        self,
        *,
        fine_only: bool | None = None,
    ) -> tuple[NavigationVoxelChunkDescriptor, ...]:
        raise NotImplementedError

    def chunk_ids_for_point(
        self,
        point: Sequence[float],
        *,
        fine_only: bool | None = None,
    ) -> tuple[str, ...]:
        return tuple(
            descriptor.chunk_id
            for descriptor in self.descriptors(fine_only=fine_only)
            if descriptor.contains_point(point)
        )

    def get_chunk(self, chunk_id: str) -> LocalVoxelVolume | None:
        raise NotImplementedError

    def prefetch(self, chunk_ids: Sequence[str]) -> tuple[str, ...]:
        """Materialize requested chunks and return the resident IDs."""
        resident: list[str] = []
        for chunk_id in chunk_ids:
            if self.get_chunk(str(chunk_id)) is not None:
                resident.append(str(chunk_id))
        return tuple(resident)

    def release_unused(self, keep_chunk_ids: Sequence[str] = ()) -> None:
        """Release chunks not needed by the current navigation horizon."""
        del keep_chunk_ids

    def resident_chunk_ids(self) -> tuple[str, ...]:
        raise NotImplementedError

    def stats(self) -> dict[str, object]:
        raise NotImplementedError

    def close(self) -> None:
        """Release backend resources."""


class InMemoryNavigationVoxelChunkStore(NavigationVoxelChunkStore):
    """Full-memory backend used by small maps and cache compatibility paths."""

    def __init__(
        self,
        coarse_tiles: Sequence[LocalVoxelVolume] = (),
        fine_tiles: Sequence[LocalVoxelVolume] = (),
    ) -> None:
        self._chunks: dict[str, LocalVoxelVolume] = {}
        self._descriptors: dict[str, NavigationVoxelChunkDescriptor] = {}
        for index, volume in enumerate(coarse_tiles):
            chunk_id = f"coarse-{index:06d}"
            self._add(chunk_id, "coarse", volume)
        for index, volume in enumerate(fine_tiles):
            chunk_id = f"fine-{index:06d}"
            self._add(chunk_id, "fine", volume)
        self._prefetch_count = 0

    def _add(self, chunk_id: str, kind: str, volume: LocalVoxelVolume) -> None:
        self._chunks[chunk_id] = volume
        self._descriptors[chunk_id] = NavigationVoxelChunkDescriptor.from_volume(
            chunk_id,
            kind,
            volume,
        )

    def descriptors(
        self,
        *,
        fine_only: bool | None = None,
    ) -> tuple[NavigationVoxelChunkDescriptor, ...]:
        return tuple(
            descriptor
            for descriptor in self._descriptors.values()
            if fine_only is None or (descriptor.kind == "fine") == fine_only
        )

    def get_chunk(self, chunk_id: str) -> LocalVoxelVolume | None:
        return self._chunks.get(str(chunk_id))

    def prefetch(self, chunk_ids: Sequence[str]) -> tuple[str, ...]:
        requested = tuple(str(chunk_id) for chunk_id in chunk_ids)
        self._prefetch_count += len(requested)
        return super().prefetch(requested)

    def resident_chunk_ids(self) -> tuple[str, ...]:
        return tuple(self._chunks)

    def stats(self) -> dict[str, object]:
        return {
            "backend": "in_memory",
            "chunk_count": len(self._chunks),
            "resident_chunk_count": len(self._chunks),
            "prefetch_requests": int(self._prefetch_count),
            "cache_hits": len(self._chunks),
            "cache_misses": 0,
            "evictions": 0,
        }


class DiskNavigationVoxelChunkStore(NavigationVoxelChunkStore):
    """Bounded lazy backend for persisted navigation voxel chunks."""

    def __init__(
        self,
        root_dir: str | os.PathLike[str],
        descriptors: Sequence[NavigationVoxelChunkDescriptor],
        *,
        decoder: ChunkDecoder,
        max_resident_chunks: int = DEFAULT_NAVIGATION_VOXEL_CHUNK_MAX_RESIDENT,
        max_chunk_bytes: int = DEFAULT_NAVIGATION_VOXEL_CHUNK_MAX_BYTES,
    ) -> None:
        self._root_dir = os.path.abspath(os.fspath(root_dir))
        self._descriptors = {item.chunk_id: item for item in descriptors}
        if len(self._descriptors) != len(tuple(descriptors)):
            raise ValueError("navigation voxel chunk IDs must be unique")
        self._decoder = decoder
        self._max_resident_chunks = max(1, int(max_resident_chunks))
        self._max_chunk_bytes = max(1, int(max_chunk_bytes))
        self._resident: OrderedDict[str, LocalVoxelVolume] = OrderedDict()
        self._lock = threading.RLock()
        self._cache_hits = 0
        self._cache_misses = 0
        self._load_errors = 0
        self._prefetch_requests = 0
        self._evictions = 0

    def descriptors(
        self,
        *,
        fine_only: bool | None = None,
    ) -> tuple[NavigationVoxelChunkDescriptor, ...]:
        return tuple(
            descriptor
            for descriptor in self._descriptors.values()
            if fine_only is None or (descriptor.kind == "fine") == fine_only
        )

    def get_chunk(self, chunk_id: str) -> LocalVoxelVolume | None:
        key = str(chunk_id)
        with self._lock:
            cached = self._resident.get(key)
            if cached is not None:
                self._resident.move_to_end(key)
                self._cache_hits += 1
                return cached
            descriptor = self._descriptors.get(key)
            self._cache_misses += 1
        if descriptor is None or not descriptor.relative_path:
            with self._lock:
                self._load_errors += 1
            return None
        path = self._safe_path(descriptor.relative_path)
        if path is None:
            with self._lock:
                self._load_errors += 1
            return None
        try:
            payload = load_bounded_json(
                path,
                max_bytes=self._max_chunk_bytes,
                description="navigation voxel chunk",
            )
            if not isinstance(payload, Mapping):
                raise ValueError("navigation voxel chunk payload is malformed")
            volume = self._decoder(payload)
        except (OSError, TypeError, ValueError, binascii.Error, zlib.error):
            with self._lock:
                self._load_errors += 1
            return None
        with self._lock:
            self._resident[key] = volume
            self._resident.move_to_end(key)
            self._evict_locked()
        return volume

    def prefetch(self, chunk_ids: Sequence[str]) -> tuple[str, ...]:
        requested = tuple(dict.fromkeys(str(chunk_id) for chunk_id in chunk_ids))
        with self._lock:
            self._prefetch_requests += len(requested)
        return super().prefetch(requested)

    def release_unused(self, keep_chunk_ids: Sequence[str] = ()) -> None:
        keep = {str(chunk_id) for chunk_id in keep_chunk_ids}
        with self._lock:
            for chunk_id in tuple(self._resident):
                if chunk_id not in keep:
                    self._resident.pop(chunk_id, None)

    def resident_chunk_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._resident)

    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "backend": "disk_lru",
                "root_dir": self._root_dir,
                "chunk_count": len(self._descriptors),
                "resident_chunk_count": len(self._resident),
                "max_resident_chunks": self._max_resident_chunks,
                "prefetch_requests": int(self._prefetch_requests),
                "cache_hits": int(self._cache_hits),
                "cache_misses": int(self._cache_misses),
                "load_errors": int(self._load_errors),
                "evictions": int(self._evictions),
            }

    def close(self) -> None:
        with self._lock:
            self._resident.clear()

    def _safe_path(self, relative_path: str) -> str | None:
        path_parts = navigation_voxel_chunk_relative_path_parts(relative_path)
        if path_parts is None:
            return None
        # Join the persisted POSIX components with the host separator so the
        # same cache layout opens correctly on Linux, macOS, and Windows.
        candidate = os.path.abspath(os.path.join(self._root_dir, *path_parts))
        try:
            if os.path.commonpath((self._root_dir, candidate)) != self._root_dir:
                return None
        except ValueError:
            return None
        return candidate

    def _evict_locked(self) -> None:
        while len(self._resident) > self._max_resident_chunks:
            self._resident.popitem(last=False)
            self._evictions += 1


def _point(value: object, field_name: str) -> Point:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 3
    ):
        raise ValueError(f"navigation voxel chunk {field_name} is malformed")
    try:
        return tuple(float(item) for item in value)  # type: ignore[return-value]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"navigation voxel chunk {field_name} is malformed") from exc
