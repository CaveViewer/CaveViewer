"""Estimate chunk-size tradeoffs before building a map cache."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import repeat
import math
from typing import Iterable, Sequence

import numpy as np

from caveviewer.core.chunking.capacity import (
    ensure_sufficient_import_memory,
    ensure_sufficient_source_file_read_memory,
)


DEFAULT_CANDIDATE_SIZES = (24.0, 32.0, 40.0, 50.0, 64.0, 80.0, 100.0, 128.0)
MAX_DIRECTION_SAMPLES = 50_000
ESTIMATED_BYTES_PER_FACE = 96
P95_STUTTER_WARNING_BYTES = 128 * 1024 * 1024
MAX_STUTTER_WARNING_BYTES = 256 * 1024 * 1024
ProgressCallback = Callable[[str, float], None]


@dataclass(frozen=True)
class ChunkSizeCandidate:
    """Score and diagnostics for one tested chunk size."""

    chunk_size: float
    score: float
    chunk_count: int
    median_chunk_faces: int
    p95_chunk_faces: int
    max_chunk_faces: int
    median_chunk_bytes_estimate: int
    p95_chunk_bytes_estimate: int
    max_chunk_bytes_estimate: int
    median_material_count: int
    p95_material_count: int
    occupancy_sparsity: float
    direction_change_score: float
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "chunk_size": self.chunk_size,
            "score": self.score,
            "chunk_count": self.chunk_count,
            "median_chunk_faces": self.median_chunk_faces,
            "p95_chunk_faces": self.p95_chunk_faces,
            "max_chunk_faces": self.max_chunk_faces,
            "median_chunk_bytes_estimate": self.median_chunk_bytes_estimate,
            "p95_chunk_bytes_estimate": self.p95_chunk_bytes_estimate,
            "max_chunk_bytes_estimate": self.max_chunk_bytes_estimate,
            "median_material_count": self.median_material_count,
            "p95_material_count": self.p95_material_count,
            "occupancy_sparsity": self.occupancy_sparsity,
            "direction_change_score": self.direction_change_score,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ChunkSizeRecommendation:
    """Recommended chunk size and the candidate scores that justify it."""

    recommended_size: float
    candidates: tuple[ChunkSizeCandidate, ...]
    explanation: str
    advisor_version: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "advisor_version": self.advisor_version,
            "recommended_size": self.recommended_size,
            "explanation": self.explanation,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


@dataclass
class _CellStats:
    face_count: int = 0
    materials: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _RawCandidate:
    chunk_size: float
    chunk_count: int
    median_chunk_faces: int
    p95_chunk_faces: int
    max_chunk_faces: int
    median_material_count: int
    p95_material_count: int
    occupancy_sparsity: float
    direction_change_score: float


class _AdvisorAccumulator:
    def __init__(
        self,
        candidate_sizes: Sequence[float],
        *,
        expected_face_count: int | None = None,
        worker_count: int = 1,
    ) -> None:
        self.candidate_sizes = tuple(_normalize_candidate_sizes(candidate_sizes))
        self.cell_stats: dict[float, dict[tuple[int, int, int], _CellStats]] = {
            size: {} for size in self.candidate_sizes
        }
        self.worker_count = min(
            _normalize_worker_count(worker_count),
            max(1, len(self.candidate_sizes)),
        )
        self._executor = (
            ThreadPoolExecutor(max_workers=self.worker_count)
            if self.worker_count > 1
            else None
        )
        self.expected_face_count = expected_face_count
        self.processed_faces = 0
        face_count = max(0, int(expected_face_count or 0))
        self.direction_sample_stride = max(1, face_count // MAX_DIRECTION_SAMPLES)
        self.direction_samples: list[np.ndarray] = []

    def __enter__(self) -> "_AdvisorAccumulator":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    def process(
        self,
        face_centroids: np.ndarray,
        material_names: Sequence[str | None] | None = None,
    ) -> None:
        centroids = np.asarray(face_centroids, dtype=np.float32)
        if centroids.ndim != 2 or centroids.shape[1] != 3:
            raise ValueError("face_centroids must have shape (N, 3)")
        if len(centroids) == 0:
            return

        materials = (
            [None] * len(centroids)
            if material_names is None
            else list(material_names)
        )
        if len(materials) != len(centroids):
            raise ValueError("material_names must match face_centroids length")

        self._sample_direction_centroids(centroids)

        if self._executor is None:
            candidate_results = (
                _candidate_cell_stats(chunk_size, centroids, materials)
                for chunk_size in self.candidate_sizes
            )
        else:
            candidate_results = self._executor.map(
                _candidate_cell_stats,
                self.candidate_sizes,
                repeat(centroids),
                repeat(materials),
            )

        for chunk_size, partial_stats in candidate_results:
            _merge_cell_stats(self.cell_stats[chunk_size], partial_stats)

        self.processed_faces += len(centroids)

    def finalize(self) -> ChunkSizeRecommendation:
        if self.processed_faces <= 0:
            raise ValueError("Cannot recommend a chunk size without faces")

        direction_complexity = _direction_complexity(self._direction_sample_array())
        raw_candidates = tuple(
            self._raw_candidate(size, direction_complexity)
            for size in self.candidate_sizes
        )
        final_candidates = _score_candidates(raw_candidates)
        recommended = min(final_candidates, key=lambda candidate: candidate.score)
        explanation = (
            f"Selected {recommended.chunk_size:g} because it had the lowest "
            f"estimated streaming score among {len(final_candidates)} candidates."
        )
        return ChunkSizeRecommendation(
            recommended_size=recommended.chunk_size,
            candidates=tuple(sorted(final_candidates, key=lambda item: item.chunk_size)),
            explanation=explanation,
        )

    def _sample_direction_centroids(self, centroids: np.ndarray) -> None:
        remaining = MAX_DIRECTION_SAMPLES - sum(len(chunk) for chunk in self.direction_samples)
        if remaining <= 0:
            return
        indices = np.arange(len(centroids), dtype=np.int64) + self.processed_faces
        selected = centroids[(indices % self.direction_sample_stride) == 0]
        if len(selected) > remaining:
            selected = selected[:remaining]
        if len(selected):
            self.direction_samples.append(selected.astype(np.float32, copy=True))

    def _direction_sample_array(self) -> np.ndarray:
        if not self.direction_samples:
            return np.empty((0, 3), dtype=np.float32)
        return np.concatenate(self.direction_samples, axis=0)

    def _raw_candidate(
        self,
        chunk_size: float,
        direction_complexity: float,
    ) -> _RawCandidate:
        per_cell = self.cell_stats[chunk_size]
        if not per_cell:
            raise ValueError("Cannot score a candidate without occupied cells")
        face_counts = np.asarray(
            [stats.face_count for stats in per_cell.values()],
            dtype=np.int64,
        )
        material_counts = np.asarray(
            [len(stats.materials) for stats in per_cell.values()],
            dtype=np.int64,
        )
        cell_coords = np.asarray(list(per_cell.keys()), dtype=np.int64)
        cell_min = cell_coords.min(axis=0)
        cell_max = cell_coords.max(axis=0)
        bbox_cell_count = int(np.prod((cell_max - cell_min) + 1))
        occupancy = len(per_cell) / max(1, bbox_cell_count)
        occupancy_sparsity = 1.0 - max(0.0, min(1.0, occupancy))

        max_candidate_size = max(self.candidate_sizes)
        direction_change_score = max(0.0, min(
            1.0,
            float(direction_complexity) * (float(chunk_size) / max_candidate_size),
        ))

        return _RawCandidate(
            chunk_size=float(chunk_size),
            chunk_count=len(per_cell),
            median_chunk_faces=int(round(float(np.median(face_counts)))),
            p95_chunk_faces=int(round(float(np.percentile(face_counts, 95)))),
            max_chunk_faces=int(face_counts.max()),
            median_material_count=int(round(float(np.median(material_counts)))),
            p95_material_count=int(round(float(np.percentile(material_counts, 95)))),
            occupancy_sparsity=occupancy_sparsity,
            direction_change_score=direction_change_score,
        )


def recommend_chunk_size_from_faces(
    face_centroids: np.ndarray,
    *,
    material_names: Sequence[str | None] | None = None,
    candidate_sizes: Sequence[float] | None = None,
    worker_count: int = 1,
    progress_cb: ProgressCallback | None = None,
) -> ChunkSizeRecommendation:
    """Recommend a chunk size from already-computed face centroids."""
    progress_cb = _monotonic_progress(progress_cb)
    centroids = np.asarray(face_centroids, dtype=np.float32)
    _emit_progress(progress_cb, "scoring candidates", 0.0)
    with _AdvisorAccumulator(
        candidate_sizes or DEFAULT_CANDIDATE_SIZES,
        expected_face_count=len(centroids),
        worker_count=worker_count,
    ) as accumulator:
        accumulator.process(centroids, material_names)
        _emit_progress(progress_cb, "scoring candidates", 0.8)
        result = accumulator.finalize()
    _emit_progress(progress_cb, "done", 1.0)
    return result


def recommend_chunk_size_for_obj(
    obj_path: str,
    *,
    candidate_sizes: Sequence[float] | None = None,
    face_batch_size: int = 200_000,
    worker_count: int = 1,
    progress_cb: ProgressCallback | None = None,
) -> ChunkSizeRecommendation:
    """Analyze an OBJ source without retaining whole-model face arrays."""
    from caveviewer.core.mesh.obj import iter_obj_face_batches, parse_obj_vertices

    progress_cb = _monotonic_progress(progress_cb)
    _emit_progress(progress_cb, "reading vertices", 0.0)
    vertex_data = parse_obj_vertices(
        obj_path,
        progress_cb=_scaled_progress(
            progress_cb,
            0.0,
            0.35,
            stage_override="reading vertices",
        ),
    )
    with _AdvisorAccumulator(
        candidate_sizes or DEFAULT_CANDIDATE_SIZES,
        expected_face_count=vertex_data.face_count,
        worker_count=worker_count,
    ) as accumulator:

        def face_progress(_stage: str, fraction: float) -> None:
            _emit_progress(progress_cb, "reading faces", 0.35 + 0.25 * fraction)

        for batch in iter_obj_face_batches(
            obj_path,
            batch_size=face_batch_size,
            progress_cb=face_progress,
        ):
            centroids = vertex_data.positions[batch.face_pos_idx].mean(axis=1)
            accumulator.process(centroids, batch.material_names)
            if vertex_data.face_count:
                fraction = min(1.0, accumulator.processed_faces / vertex_data.face_count)
                _emit_progress(progress_cb, "analyzing faces", 0.60 + 0.30 * fraction)
        _emit_progress(progress_cb, "scoring candidates", 0.95)
        result = accumulator.finalize()
    _emit_progress(progress_cb, "done", 1.0)
    return result


def recommend_chunk_size_for_glb(
    glb_path: str,
    *,
    candidate_sizes: Sequence[float] | None = None,
    face_batch_size: int = 200_000,
    worker_count: int = 1,
    progress_cb: ProgressCallback | None = None,
) -> ChunkSizeRecommendation:
    """Analyze a GLB source using its parsed mesh."""
    from caveviewer.core.mesh.glb import parse_glb

    progress_cb = _monotonic_progress(progress_cb)
    _emit_progress(progress_cb, "reading GLB", 0.0)
    ensure_sufficient_source_file_read_memory(glb_path)

    def glb_preflight(
        vertex_count: int,
        uv_count: int,
        normal_count: int,
        face_count: int,
    ) -> None:
        ensure_sufficient_import_memory(
            vertex_count,
            uv_count,
            normal_count,
            face_count,
            source_path=glb_path,
        )

    mesh, _embedded_textures = parse_glb(
        glb_path,
        progress_cb=_scaled_progress(
            progress_cb,
            0.0,
            0.55,
            stage_override="reading GLB",
        ),
        preflight_cb=glb_preflight,
    )
    face_indices = mesh.face_pos_idx
    face_count = len(face_indices)
    material_names = _material_names_for_mesh(mesh)
    with _AdvisorAccumulator(
        candidate_sizes or DEFAULT_CANDIDATE_SIZES,
        expected_face_count=face_count,
        worker_count=worker_count,
    ) as accumulator:
        batch_size = max(1, int(face_batch_size))
        for start in range(0, face_count, batch_size):
            end = min(face_count, start + batch_size)
            _emit_progress(progress_cb, "computing face centroids", 0.55)
            centroids = mesh.positions[face_indices[start:end]].mean(axis=1)
            accumulator.process(centroids, material_names[start:end])
            fraction = end / max(1, face_count)
            _emit_progress(progress_cb, "analyzing faces", 0.60 + 0.35 * fraction)
        _emit_progress(progress_cb, "scoring candidates", 0.97)
        result = accumulator.finalize()
    _emit_progress(progress_cb, "done", 1.0)
    return result


def recommend_chunk_size_for_descriptor(
    model_descriptor: dict,
    *,
    candidate_sizes: Sequence[float] | None = None,
    face_batch_size: int = 200_000,
    worker_count: int = 1,
    progress_cb: ProgressCallback | None = None,
) -> ChunkSizeRecommendation:
    """Analyze a model descriptor returned by core.map.source_model.find_model_file."""
    fmt = model_descriptor.get("format")
    if fmt == "obj":
        return recommend_chunk_size_for_obj(
            str(model_descriptor["obj_path"]),
            candidate_sizes=candidate_sizes,
            face_batch_size=face_batch_size,
            worker_count=worker_count,
            progress_cb=progress_cb,
        )
    if fmt == "glb":
        return recommend_chunk_size_for_glb(
            str(model_descriptor["glb_path"]),
            candidate_sizes=candidate_sizes,
            face_batch_size=face_batch_size,
            worker_count=worker_count,
            progress_cb=progress_cb,
        )
    raise ValueError(f"Unknown model format: {fmt!r}")


def _candidate_cell_stats(
    chunk_size: float,
    centroids: np.ndarray,
    materials: Sequence[str | None],
) -> tuple[float, dict[tuple[int, int, int], _CellStats]]:
    cells = np.floor(centroids / chunk_size).astype(np.int64)
    unique_cells, inverse, counts = np.unique(
        cells,
        axis=0,
        return_inverse=True,
        return_counts=True,
    )
    material_buckets: list[set[str]] = [set() for _ in range(len(unique_cells))]
    for inverse_index, material_name in zip(inverse, materials):
        if material_name is not None:
            material_buckets[int(inverse_index)].add(str(material_name))

    partial_stats: dict[tuple[int, int, int], _CellStats] = {}
    for index, cell in enumerate(unique_cells):
        key = (int(cell[0]), int(cell[1]), int(cell[2]))
        partial_stats[key] = _CellStats(
            face_count=int(counts[index]),
            materials=material_buckets[index],
        )
    return float(chunk_size), partial_stats


def _merge_cell_stats(
    target: dict[tuple[int, int, int], _CellStats],
    partial: dict[tuple[int, int, int], _CellStats],
) -> None:
    for key, source_stats in partial.items():
        target_stats = target.setdefault(key, _CellStats())
        target_stats.face_count += source_stats.face_count
        target_stats.materials.update(source_stats.materials)


def _emit_progress(
    progress_cb: ProgressCallback | None,
    stage: str,
    fraction: float,
) -> None:
    if progress_cb is None:
        return
    progress_cb(str(stage), max(0.0, min(1.0, float(fraction))))


def _scaled_progress(
    progress_cb: ProgressCallback | None,
    start: float,
    end: float,
    *,
    stage_override: str | None = None,
) -> ProgressCallback | None:
    if progress_cb is None:
        return None
    start = max(0.0, min(1.0, float(start)))
    end = max(0.0, min(1.0, float(end)))

    def emit(stage: str, fraction: float) -> None:
        clamped = max(0.0, min(1.0, float(fraction)))
        _emit_progress(
            progress_cb,
            stage_override or stage,
            start + (end - start) * clamped,
        )

    return emit


def _monotonic_progress(
    progress_cb: ProgressCallback | None,
) -> ProgressCallback | None:
    if progress_cb is None:
        return None
    last_fraction = 0.0

    def emit(stage: str, fraction: float) -> None:
        nonlocal last_fraction
        clamped = max(0.0, min(1.0, float(fraction)))
        if clamped < last_fraction:
            clamped = last_fraction
        else:
            last_fraction = clamped
        progress_cb(stage, clamped)

    return emit


def _material_names_for_mesh(mesh) -> list[str | None]:
    face_count = len(getattr(mesh, "face_pos_idx", ()))
    names: list[str | None] = [None] * face_count
    for material_range in getattr(mesh, "material_ranges", ()):
        start = max(0, int(material_range.start_face))
        end = min(face_count, int(material_range.end_face))
        for index in range(start, end):
            names[index] = str(material_range.material_name)
    return names


def _normalize_candidate_sizes(candidate_sizes: Iterable[float]) -> tuple[float, ...]:
    normalized = sorted({float(size) for size in candidate_sizes})
    if not normalized:
        raise ValueError("At least one candidate chunk size is required")
    for size in normalized:
        if not math.isfinite(size) or size <= 0.0:
            raise ValueError("Candidate chunk sizes must be positive finite numbers")
    return tuple(normalized)


def _normalize_worker_count(worker_count: int) -> int:
    try:
        value = int(worker_count)
    except Exception as exc:
        raise ValueError("worker_count must be a whole number") from exc
    if value < 1:
        raise ValueError("worker_count must be at least 1")
    return value


def _direction_complexity(samples: np.ndarray) -> float:
    samples = np.asarray(samples, dtype=np.float64)
    if samples.ndim != 2 or samples.shape[1] != 3 or len(samples) < 8:
        return 0.0
    centered = samples - samples.mean(axis=0)
    global_direction = _principal_direction(centered)
    projections = centered @ global_direction
    order = np.argsort(projections)
    ordered = centered[order]
    window_count = max(4, min(16, len(ordered) // 12))
    if window_count < 2:
        return 0.0
    windows = np.array_split(ordered, window_count)
    directions = []
    for window in windows:
        if len(window) < 4:
            continue
        directions.append(_principal_direction(window - window.mean(axis=0)))
    if len(directions) < 2:
        return 0.0

    changes = []
    for first, second in zip(directions, directions[1:]):
        dot = abs(float(np.dot(first, second)))
        dot = max(0.0, min(1.0, dot))
        changes.append(math.acos(dot) / (math.pi / 2.0))
    if not changes:
        return 0.0
    return max(0.0, min(1.0, float(np.mean(changes))))


def _principal_direction(points: np.ndarray) -> np.ndarray:
    if len(points) == 0:
        return np.asarray([1.0, 0.0, 0.0], dtype=np.float64)
    covariance = np.cov(points, rowvar=False)
    if covariance.shape == ():
        return np.asarray([1.0, 0.0, 0.0], dtype=np.float64)
    values, vectors = np.linalg.eigh(covariance)
    direction = vectors[:, int(np.argmax(values))]
    norm = float(np.linalg.norm(direction))
    if norm <= 0.0:
        return np.asarray([1.0, 0.0, 0.0], dtype=np.float64)
    return direction / norm


def _score_candidates(raw_candidates: Sequence[_RawCandidate]) -> tuple[ChunkSizeCandidate, ...]:
    chunk_counts = [candidate.chunk_count for candidate in raw_candidates]
    p95_faces = [candidate.p95_chunk_faces for candidate in raw_candidates]
    max_faces = [candidate.max_chunk_faces for candidate in raw_candidates]
    p95_materials = [candidate.p95_material_count for candidate in raw_candidates]
    direction_scores = [candidate.direction_change_score for candidate in raw_candidates]

    final_candidates = []
    for raw in raw_candidates:
        median_bytes = raw.median_chunk_faces * ESTIMATED_BYTES_PER_FACE
        p95_bytes = raw.p95_chunk_faces * ESTIMATED_BYTES_PER_FACE
        max_bytes = raw.max_chunk_faces * ESTIMATED_BYTES_PER_FACE
        score = (
            0.22 * _normalized(raw.chunk_count, chunk_counts)
            + 0.28 * _normalized(raw.p95_chunk_faces, p95_faces)
            + 0.16 * _normalized(raw.max_chunk_faces, max_faces)
            + 0.10 * _normalized(raw.p95_material_count, p95_materials)
            + 0.08 * raw.occupancy_sparsity
            + 0.16 * _normalized(raw.direction_change_score, direction_scores)
        )
        warnings = []
        if p95_bytes >= P95_STUTTER_WARNING_BYTES:
            warnings.append("p95 chunk payload may be expensive to stream")
        if max_bytes >= MAX_STUTTER_WARNING_BYTES:
            warnings.append("largest chunk payload may cause visible stalls")
        final_candidates.append(
            ChunkSizeCandidate(
                chunk_size=raw.chunk_size,
                score=round(float(score), 4),
                chunk_count=raw.chunk_count,
                median_chunk_faces=raw.median_chunk_faces,
                p95_chunk_faces=raw.p95_chunk_faces,
                max_chunk_faces=raw.max_chunk_faces,
                median_chunk_bytes_estimate=median_bytes,
                p95_chunk_bytes_estimate=p95_bytes,
                max_chunk_bytes_estimate=max_bytes,
                median_material_count=raw.median_material_count,
                p95_material_count=raw.p95_material_count,
                occupancy_sparsity=round(float(raw.occupancy_sparsity), 4),
                direction_change_score=round(float(raw.direction_change_score), 4),
                warnings=tuple(warnings),
            )
        )
    return tuple(final_candidates)


def _normalized(value: float, values: Sequence[float]) -> float:
    minimum = float(min(values))
    maximum = float(max(values))
    if maximum <= minimum:
        return 0.0
    return (float(value) - minimum) / (maximum - minimum)
