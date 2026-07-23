"""Render-upload slice sizing and stall-adaptation policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


RENDER_UPLOAD_VERTEX_BYTES = 8 * 4
RENDER_UPLOAD_MAX_SLICE_BYTES = 1 * 1024 ** 2
RENDER_UPLOAD_INITIAL_SLICE_BYTES = 512 * 1024
RENDER_UPLOAD_MIN_SLICE_BYTES = 256 * 1024
RENDER_UPLOAD_STALL_SHRINK_FACTOR = 0.5


@dataclass(frozen=True)
class UploadSliceState:
    """Current render-thread VBO and texture upload slice sizes."""

    vbo_upload_slice_bytes: int
    texture_upload_slice_bytes: int


@dataclass(frozen=True)
class UploadSliceAdjustment:
    """Result of adapting one upload slice size after a measured operation."""

    state: UploadSliceState
    stalled: bool


def min_vbo_upload_slice_bytes(
    *,
    min_slice_bytes: int = RENDER_UPLOAD_MIN_SLICE_BYTES,
    vertex_bytes: int = RENDER_UPLOAD_VERTEX_BYTES,
) -> int:
    """Return the minimum legal VBO slice size."""
    return max(min_slice_bytes, 3 * vertex_bytes)


def render_upload_slice_vertices(
    vbo_upload_slice_bytes: int,
    *,
    vertex_bytes: int = RENDER_UPLOAD_VERTEX_BYTES,
    max_slice_bytes: int = RENDER_UPLOAD_MAX_SLICE_BYTES,
) -> int:
    """Return a triangle-aligned vertex count for the current VBO slice size."""
    slice_bytes = max(
        3 * vertex_bytes,
        min(max_slice_bytes, int(vbo_upload_slice_bytes)),
    )
    vertices = max(3, slice_bytes // vertex_bytes)
    vertices -= vertices % 3
    return max(3, int(vertices))


def record_upload_slice_sizes(
    timing: dict[str, Any] | None,
    state: UploadSliceState,
) -> None:
    """Record current upload slice sizes in a streaming timing accumulator."""
    if timing is None:
        return
    timing["vbo_upload_slice_bytes"] = int(state.vbo_upload_slice_bytes)
    timing["texture_upload_slice_bytes"] = int(state.texture_upload_slice_bytes)


def adapt_upload_slice_size(
    *,
    kind: str,
    elapsed_ms: float,
    byte_count: int,
    target_ms: float,
    state: UploadSliceState,
    min_slice_bytes: int = RENDER_UPLOAD_MIN_SLICE_BYTES,
    max_slice_bytes: int = RENDER_UPLOAD_MAX_SLICE_BYTES,
    shrink_factor: float = RENDER_UPLOAD_STALL_SHRINK_FACTOR,
) -> UploadSliceAdjustment:
    """
    Shrink future upload slices when one measured operation exceeds budget.

    The policy is pure: callers keep ownership of the mutable render-thread
    state and decide where to record the stall in their diagnostics.
    """
    target_ms = max(0.5, float(target_ms))
    if elapsed_ms <= target_ms:
        return UploadSliceAdjustment(state=state, stalled=False)

    if kind == "texture":
        minimum = min_slice_bytes
        current = state.texture_upload_slice_bytes
    elif kind == "vbo":
        minimum = min_vbo_upload_slice_bytes(min_slice_bytes=min_slice_bytes)
        current = state.vbo_upload_slice_bytes
    else:
        return UploadSliceAdjustment(state=state, stalled=True)

    current = max(minimum, min(max_slice_bytes, int(current)))
    next_size = current
    if byte_count > 0:
        throughput_limited = int(byte_count * target_ms / max(elapsed_ms, 0.001))
        conservative_target = int(throughput_limited * 0.75)
        halved = int(current * shrink_factor)
        next_size = max(minimum, min(halved, conservative_target))

    if kind == "texture":
        state = UploadSliceState(
            vbo_upload_slice_bytes=state.vbo_upload_slice_bytes,
            texture_upload_slice_bytes=next_size,
        )
    else:
        state = UploadSliceState(
            vbo_upload_slice_bytes=next_size,
            texture_upload_slice_bytes=state.texture_upload_slice_bytes,
        )

    return UploadSliceAdjustment(state=state, stalled=True)
