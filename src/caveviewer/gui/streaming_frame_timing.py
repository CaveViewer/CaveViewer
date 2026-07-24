"""Streaming-frame timing counters and diagnostic formatting policy."""

from __future__ import annotations

from typing import Any


TimingDict = dict[str, Any]


def new_streaming_frame_timing() -> TimingDict:
    """Return the per-frame streaming timing accumulator."""
    return {
        "update_ms": 0.0,
        "drain_ms": 0.0,
        "ready_drain_ms": 0.0,
        "failure_drain_ms": 0.0,
        "chunk_ready_ms": 0.0,
        "chunk_prepare_ms": 0.0,
        "vertex_pack_ms": 0.0,
        "buffer_ms": 0.0,
        "buffer_alloc_ms": 0.0,
        "buffer_write_ms": 0.0,
        "buffer_alloc_bytes": 0,
        "buffer_write_bytes": 0,
        "vao_ms": 0.0,
        "texture_ms": 0.0,
        "texture_decode_ms": 0.0,
        "texture_alloc_ms": 0.0,
        "texture_write_ms": 0.0,
        "texture_upload_ms": 0.0,
        "texture_mipmap_ms": 0.0,
        "texture_image_bytes": 0,
        "texture_material_cache_hits": 0,
        "texture_file_cache_hits": 0,
        "texture_decoded_cache_hits": 0,
        "texture_sync_decodes": 0,
        "texture_placeholders": 0,
        "texture_evictions": 0,
        "texture_evicted_bytes": 0,
        "worst_texture_ms": 0.0,
        "worst_texture_material": None,
        "worst_texture_size": None,
        "worst_texture_bytes": 0,
        "worst_texture_decode_ms": 0.0,
        "worst_texture_alloc_ms": 0.0,
        "worst_texture_write_ms": 0.0,
        "worst_texture_upload_ms": 0.0,
        "worst_texture_mipmap_ms": 0.0,
        "worst_texture_sync_decode": False,
        "worst_texture_decoded_cache_hit": False,
        "chunk_bookkeeping_ms": 0.0,
        "unload_ms": 0.0,
        "chunks_uploaded": 0,
        "chunks_unloaded": 0,
        "groups_uploaded": 0,
        "prepacked_groups": 0,
        "fallback_pack_groups": 0,
        "vertices_uploaded": 0,
        "bytes_uploaded": 0,
        "worst_chunk_ms": 0.0,
        "worst_chunk_cell": None,
        "worst_chunk_groups": 0,
        "worst_chunk_vertices": 0,
        "worst_chunk_bytes": 0,
        "worst_chunk_prepare_ms": 0.0,
        "worst_chunk_vertex_pack_ms": 0.0,
        "worst_chunk_buffer_ms": 0.0,
        "worst_chunk_buffer_alloc_ms": 0.0,
        "worst_chunk_buffer_write_ms": 0.0,
        "worst_chunk_vao_ms": 0.0,
        "worst_chunk_texture_ms": 0.0,
        "worst_chunk_bookkeeping_ms": 0.0,
        "upload_stalls": 0,
        "vbo_upload_slice_bytes": 0,
        "texture_upload_slice_bytes": 0,
    }


def format_optional_ms(value: float | None) -> str:
    """Format an optional millisecond value for diagnostics."""
    if value is None:
        return "n/a"
    return f"{value:.1f}ms"


def format_streaming_frame_timing(timing: TimingDict) -> str:
    """Return the detailed streaming timing diagnostic line."""
    uploaded_mb = timing["bytes_uploaded"] / (1024 ** 2)
    allocated_mb = timing.get("buffer_alloc_bytes", 0) / (1024 ** 2)
    written_mb = timing.get("buffer_write_bytes", 0) / (1024 ** 2)
    texture_mb = timing["texture_image_bytes"] / (1024 ** 2)
    ready_other_ms = max(
        0.0,
        timing.get("ready_drain_ms", 0.0)
        - timing["chunk_ready_ms"]
        - timing["unload_ms"],
    )
    drain_other_ms = max(
        0.0,
        timing["drain_ms"]
        - timing.get("ready_drain_ms", 0.0)
        - timing.get("failure_drain_ms", 0.0),
    )
    vbo_slice_kb = timing.get("vbo_upload_slice_bytes", 0) / 1024
    texture_slice_kb = timing.get("texture_upload_slice_bytes", 0) / 1024
    detail = (
        f"update={timing['update_ms']:.1f}ms "
        f"drain={timing['drain_ms']:.1f}ms "
        f"ready_drain={timing.get('ready_drain_ms', 0.0):.1f}ms "
        f"upload={timing['chunk_ready_ms']:.1f}ms "
        f"unload={timing['unload_ms']:.1f}ms "
        f"ready_other={ready_other_ms:.1f}ms "
        f"failures={timing.get('failure_drain_ms', 0.0):.1f}ms "
        f"drain_other={drain_other_ms:.1f}ms | "
        f"chunks_up={timing['chunks_uploaded']} "
        f"chunks_down={timing['chunks_unloaded']} "
        f"groups={timing['groups_uploaded']} "
        f"prepacked={timing['prepacked_groups']} "
        f"fallback_pack={timing['fallback_pack_groups']} "
        f"verts={timing['vertices_uploaded']} "
        f"vbo={uploaded_mb:.1f}MB "
        f"vbo_alloc={allocated_mb:.1f}MB "
        f"vbo_write={written_mb:.1f}MB | "
        f"prepare={timing['chunk_prepare_ms']:.1f}ms "
        f"pack={timing['vertex_pack_ms']:.1f}ms "
        f"buffer={timing['buffer_ms']:.1f}ms "
        f"buffer_alloc={timing.get('buffer_alloc_ms', 0.0):.1f}ms "
        f"buffer_write={timing.get('buffer_write_ms', 0.0):.1f}ms "
        f"vao={timing['vao_ms']:.1f}ms "
        f"texture={timing['texture_ms']:.1f}ms "
        f"tex_decode={timing['texture_decode_ms']:.1f}ms "
        f"tex_alloc={timing.get('texture_alloc_ms', 0.0):.1f}ms "
        f"tex_upload={timing['texture_upload_ms']:.1f}ms "
        f"tex_mipmap={timing['texture_mipmap_ms']:.1f}ms "
        f"tex_mb={texture_mb:.1f} "
        f"slices=vbo:{vbo_slice_kb:.0f}KB/tex:{texture_slice_kb:.0f}KB "
        f"stalls={timing.get('upload_stalls', 0)} "
        f"tex_mat_reuse={timing['texture_material_cache_hits']} "
        f"tex_file_reuse={timing['texture_file_cache_hits']} "
        f"tex_decoded={timing['texture_decoded_cache_hits']} "
        f"tex_sync_decode={timing['texture_sync_decodes']} "
        f"tex_placeholder={timing['texture_placeholders']} "
        f"tex_evict={timing.get('texture_evictions', 0)} "
        f"tex_evict_mb={timing.get('texture_evicted_bytes', 0) / (1024**2):.1f} "
        f"book={timing['chunk_bookkeeping_ms']:.1f}ms"
    )

    worst_texture_material = timing["worst_texture_material"]
    if worst_texture_material is not None:
        worst_texture_size = timing["worst_texture_size"]
        worst_texture_size_text = (
            "unknown"
            if worst_texture_size is None
            else f"{worst_texture_size[0]}x{worst_texture_size[1]}"
        )
        worst_texture_mb = timing["worst_texture_bytes"] / (1024 ** 2)
        detail += (
            f" | worst_texture={worst_texture_material!r} "
            f"{timing['worst_texture_ms']:.1f}ms "
            f"size={worst_texture_size_text} "
            f"bytes={worst_texture_mb:.1f}MB "
            f"decode={timing['worst_texture_decode_ms']:.1f}ms "
            f"alloc={timing.get('worst_texture_alloc_ms', 0.0):.1f}ms "
            f"upload={timing['worst_texture_upload_ms']:.1f}ms "
            f"mipmap={timing['worst_texture_mipmap_ms']:.1f}ms "
            f"sync_decode={timing['worst_texture_sync_decode']} "
            f"decoded_cache={timing['worst_texture_decoded_cache_hit']}"
        )

    worst_cell = timing["worst_chunk_cell"]
    if worst_cell is None:
        return detail + " | worst_chunk=none"

    worst_mb = timing["worst_chunk_bytes"] / (1024 ** 2)
    return (
        detail
        + f" | worst_chunk={worst_cell} "
        f"{timing['worst_chunk_ms']:.1f}ms "
        f"groups={timing['worst_chunk_groups']} "
        f"verts={timing['worst_chunk_vertices']} "
        f"vbo={worst_mb:.1f}MB "
        f"prepare={timing['worst_chunk_prepare_ms']:.1f}ms "
        f"pack={timing['worst_chunk_vertex_pack_ms']:.1f}ms "
        f"buffer={timing['worst_chunk_buffer_ms']:.1f}ms "
        f"buffer_alloc={timing.get('worst_chunk_buffer_alloc_ms', 0.0):.1f}ms "
        f"buffer_write={timing.get('worst_chunk_buffer_write_ms', 0.0):.1f}ms "
        f"vao={timing['worst_chunk_vao_ms']:.1f}ms "
        f"texture={timing['worst_chunk_texture_ms']:.1f}ms "
        f"book={timing['worst_chunk_bookkeeping_ms']:.1f}ms"
    )


def new_chunk_upload_counters() -> TimingDict:
    """Return counters for one ready chunk's render-thread upload work."""
    return {
        "chunk_prepare_ms": 0.0,
        "vertex_pack_ms": 0.0,
        "buffer_ms": 0.0,
        "buffer_alloc_ms": 0.0,
        "buffer_write_ms": 0.0,
        "buffer_alloc_bytes": 0,
        "buffer_write_bytes": 0,
        "vao_ms": 0.0,
        "texture_ms": 0.0,
        "texture_decode_ms": 0.0,
        "texture_alloc_ms": 0.0,
        "texture_write_ms": 0.0,
        "texture_upload_ms": 0.0,
        "texture_mipmap_ms": 0.0,
        "texture_image_bytes": 0,
        "texture_material_cache_hits": 0,
        "texture_file_cache_hits": 0,
        "texture_decoded_cache_hits": 0,
        "texture_sync_decodes": 0,
        "texture_placeholders": 0,
        "texture_evictions": 0,
        "texture_evicted_bytes": 0,
        "chunk_bookkeeping_ms": 0.0,
        "groups": 0,
        "prepacked_groups": 0,
        "fallback_pack_groups": 0,
        "vertices": 0,
        "bytes": 0,
        "upload_stalls": 0,
    }


def add_chunk_upload_counters(target: TimingDict, source: TimingDict) -> None:
    """Add one chunk/group counter set into another."""
    for key, value in source.items():
        target[key] += value


def add_texture_timing_counters(
    counters: TimingDict,
    texture_timing: TimingDict,
    frame_timing: TimingDict | None,
) -> None:
    """Accumulate one texture acquire/upload result into chunk and frame timing."""
    texture_alloc_ms = texture_timing.get("texture_alloc_ms", 0.0)
    texture_write_ms = texture_timing.get("texture_write_ms")
    if texture_write_ms is None:
        texture_write_ms = texture_timing.get("texture_ms", 0.0)
    counters["texture_decode_ms"] += texture_timing.get("decode_ms", 0.0)
    counters["texture_alloc_ms"] += texture_alloc_ms
    counters["texture_write_ms"] += texture_write_ms
    counters["texture_upload_ms"] += texture_write_ms
    counters["texture_mipmap_ms"] += texture_timing.get("mipmap_ms", 0.0)
    counters["texture_image_bytes"] += texture_timing.get("image_bytes", 0)
    counters["texture_evictions"] += int(texture_timing.get("texture_evictions", 0))
    counters["texture_evicted_bytes"] += int(
        texture_timing.get("texture_evicted_bytes", 0)
    )
    if texture_timing.get("material_cache_hit"):
        counters["texture_material_cache_hits"] += 1
    if texture_timing.get("file_cache_hit"):
        counters["texture_file_cache_hits"] += 1
    if texture_timing.get("decoded_cache_hit"):
        counters["texture_decoded_cache_hits"] += 1
    if texture_timing.get("sync_decode"):
        counters["texture_sync_decodes"] += 1
    if texture_timing.get("placeholder"):
        counters["texture_placeholders"] += 1
    if (
        frame_timing is not None
        and texture_timing.get("total_ms", 0.0) > frame_timing["worst_texture_ms"]
    ):
        frame_timing["worst_texture_ms"] = texture_timing.get("total_ms", 0.0)
        frame_timing["worst_texture_material"] = texture_timing.get("material")
        frame_timing["worst_texture_size"] = texture_timing.get("image_size")
        frame_timing["worst_texture_bytes"] = texture_timing.get(
            "image_total_bytes",
            texture_timing.get("image_bytes", 0),
        )
        frame_timing["worst_texture_decode_ms"] = texture_timing.get(
            "decode_ms", 0.0
        )
        frame_timing["worst_texture_alloc_ms"] = texture_alloc_ms
        frame_timing["worst_texture_write_ms"] = texture_write_ms
        frame_timing["worst_texture_upload_ms"] = texture_write_ms
        frame_timing["worst_texture_mipmap_ms"] = texture_timing.get(
            "mipmap_ms", 0.0
        )
        frame_timing["worst_texture_sync_decode"] = texture_timing.get(
            "sync_decode", False
        )
        frame_timing["worst_texture_decoded_cache_hit"] = texture_timing.get(
            "decoded_cache_hit", False
        )


def record_chunk_upload_timing(
    timing: TimingDict | None,
    counters: TimingDict,
    *,
    chunk_ms: float,
    cell,
    completed: bool,
) -> None:
    """Accumulate one ready chunk's upload counters into the frame timing."""
    if timing is None:
        return

    timing["chunk_ready_ms"] += chunk_ms
    timing["chunk_prepare_ms"] += counters["chunk_prepare_ms"]
    timing["vertex_pack_ms"] += counters["vertex_pack_ms"]
    timing["buffer_ms"] += counters["buffer_ms"]
    timing["buffer_alloc_ms"] += counters["buffer_alloc_ms"]
    timing["buffer_write_ms"] += counters["buffer_write_ms"]
    timing["buffer_alloc_bytes"] += counters["buffer_alloc_bytes"]
    timing["buffer_write_bytes"] += counters["buffer_write_bytes"]
    timing["vao_ms"] += counters["vao_ms"]
    timing["texture_ms"] += counters["texture_ms"]
    timing["texture_decode_ms"] += counters["texture_decode_ms"]
    timing["texture_alloc_ms"] += counters["texture_alloc_ms"]
    timing["texture_write_ms"] += counters["texture_write_ms"]
    timing["texture_upload_ms"] += counters["texture_upload_ms"]
    timing["texture_mipmap_ms"] += counters["texture_mipmap_ms"]
    timing["texture_image_bytes"] += counters["texture_image_bytes"]
    timing["texture_material_cache_hits"] += counters["texture_material_cache_hits"]
    timing["texture_file_cache_hits"] += counters["texture_file_cache_hits"]
    timing["texture_decoded_cache_hits"] += counters["texture_decoded_cache_hits"]
    timing["texture_sync_decodes"] += counters["texture_sync_decodes"]
    timing["texture_placeholders"] += counters["texture_placeholders"]
    timing["texture_evictions"] += counters["texture_evictions"]
    timing["texture_evicted_bytes"] += counters["texture_evicted_bytes"]
    timing["chunk_bookkeeping_ms"] += counters["chunk_bookkeeping_ms"]
    if completed:
        timing["chunks_uploaded"] += 1
    timing["groups_uploaded"] += counters["groups"]
    timing["prepacked_groups"] += counters["prepacked_groups"]
    timing["fallback_pack_groups"] += counters["fallback_pack_groups"]
    timing["vertices_uploaded"] += counters["vertices"]
    timing["bytes_uploaded"] += counters["bytes"]
    if chunk_ms > timing["worst_chunk_ms"]:
        timing["worst_chunk_ms"] = chunk_ms
        timing["worst_chunk_cell"] = cell
        timing["worst_chunk_groups"] = counters["groups"]
        timing["worst_chunk_vertices"] = counters["vertices"]
        timing["worst_chunk_bytes"] = counters["bytes"]
        timing["worst_chunk_prepare_ms"] = counters["chunk_prepare_ms"]
        timing["worst_chunk_vertex_pack_ms"] = counters["vertex_pack_ms"]
        timing["worst_chunk_buffer_ms"] = counters["buffer_ms"]
        timing["worst_chunk_buffer_alloc_ms"] = counters["buffer_alloc_ms"]
        timing["worst_chunk_buffer_write_ms"] = counters["buffer_write_ms"]
        timing["worst_chunk_vao_ms"] = counters["vao_ms"]
        timing["worst_chunk_texture_ms"] = counters["texture_ms"]
        timing["worst_chunk_bookkeeping_ms"] = counters["chunk_bookkeeping_ms"]
