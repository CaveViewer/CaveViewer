"""Unit tests for render-thread upload bookkeeping policy."""

from types import SimpleNamespace

from caveviewer.gui import render_upload


def test_render_upload_slice_vertices_stays_triangle_aligned():
    assert render_upload.render_upload_slice_vertices(1) == 3
    assert render_upload.render_upload_slice_vertices(
        render_upload.RENDER_UPLOAD_VERTEX_BYTES * 5
    ) == 3
    assert render_upload.render_upload_slice_vertices(
        render_upload.RENDER_UPLOAD_VERTEX_BYTES * 6
    ) == 6


def test_adapt_upload_slice_size_records_non_stall_state():
    timing = render_upload.new_streaming_frame_timing()
    state = render_upload.UploadSliceState(
        vbo_upload_slice_bytes=1024 * 1024,
        texture_upload_slice_bytes=1024 * 1024,
    )

    decision = render_upload.adapt_upload_slice_size(
        kind="vbo",
        elapsed_ms=2.0,
        byte_count=1024 * 1024,
        target_ms=3.0,
        state=state,
        timing=timing,
    )

    assert decision == render_upload.UploadSliceDecision(
        state=state,
        stalled=False,
    )
    assert timing["upload_stalls"] == 0
    assert timing["vbo_upload_slice_bytes"] == 1024 * 1024
    assert timing["texture_upload_slice_bytes"] == 1024 * 1024


def test_adapt_upload_slice_size_shrinks_only_measured_kind():
    timing = render_upload.new_streaming_frame_timing()
    state = render_upload.UploadSliceState(
        vbo_upload_slice_bytes=1024 * 1024,
        texture_upload_slice_bytes=1024 * 1024,
    )

    texture_decision = render_upload.adapt_upload_slice_size(
        kind="texture",
        elapsed_ms=30.0,
        byte_count=1024 * 1024,
        target_ms=3.0,
        state=state,
        timing=timing,
    )
    vbo_decision = render_upload.adapt_upload_slice_size(
        kind="vbo",
        elapsed_ms=30.0,
        byte_count=1024 * 1024,
        target_ms=3.0,
        state=texture_decision.state,
        timing=timing,
    )

    assert texture_decision.state.texture_upload_slice_bytes < 1024 * 1024
    assert texture_decision.state.vbo_upload_slice_bytes == 1024 * 1024
    assert vbo_decision.state.vbo_upload_slice_bytes < 1024 * 1024
    assert timing["upload_stalls"] == 2
    assert timing["texture_upload_slice_bytes"] == (
        vbo_decision.state.texture_upload_slice_bytes
    )
    assert timing["vbo_upload_slice_bytes"] == (
        vbo_decision.state.vbo_upload_slice_bytes
    )


def test_texture_timing_updates_worst_texture_frame_summary():
    counters = render_upload.new_chunk_upload_counters()
    frame_timing = render_upload.new_streaming_frame_timing()

    render_upload.add_texture_timing_counters(
        counters,
        {
            "material": "limestone",
            "total_ms": 9.0,
            "decode_ms": 1.0,
            "texture_alloc_ms": 2.0,
            "texture_write_ms": 3.0,
            "mipmap_ms": 4.0,
            "image_bytes": 1024,
            "image_total_bytes": 2048,
            "image_size": (32, 16),
            "material_cache_hit": True,
            "decoded_cache_hit": True,
        },
        frame_timing,
    )

    assert counters["texture_material_cache_hits"] == 1
    assert counters["texture_decoded_cache_hits"] == 1
    assert counters["texture_upload_ms"] == 3.0
    assert frame_timing["worst_texture_material"] == "limestone"
    assert frame_timing["worst_texture_size"] == (32, 16)
    assert frame_timing["worst_texture_bytes"] == 2048


def test_new_chunk_group_upload_job_normalizes_state():
    group = SimpleNamespace(material_name="silt")

    job = render_upload.new_chunk_group_upload_job(group, smooth_shading=1)

    assert job["group"] is group
    assert job["smooth_shading"] is True
    assert job["next_vertex_index"] == 0
    assert job["texture_task"] is None
    assert job["pending_vbo"] is None
