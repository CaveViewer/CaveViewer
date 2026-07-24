"""Tests for streaming frame timing counters and diagnostics."""

from caveviewer.gui import streaming_frame_timing


def test_texture_timing_counters_track_cache_hits_and_worst_texture():
    """Texture upload timing updates chunk counters and frame worst-texture data."""
    counters = streaming_frame_timing.new_chunk_upload_counters()
    timing = streaming_frame_timing.new_streaming_frame_timing()

    streaming_frame_timing.add_texture_timing_counters(
        counters,
        {
            "total_ms": 12.0,
            "material": "limestone",
            "image_size": (1024, 512),
            "image_bytes": 100,
            "image_total_bytes": 200,
            "decode_ms": 1.5,
            "texture_alloc_ms": 2.0,
            "texture_write_ms": 3.0,
            "mipmap_ms": 4.0,
            "texture_evictions": 2,
            "texture_evicted_bytes": 4096,
            "material_cache_hit": True,
            "file_cache_hit": True,
            "decoded_cache_hit": True,
            "sync_decode": True,
        },
        timing,
    )

    assert counters["texture_decode_ms"] == 1.5
    assert counters["texture_alloc_ms"] == 2.0
    assert counters["texture_upload_ms"] == 3.0
    assert counters["texture_mipmap_ms"] == 4.0
    assert counters["texture_image_bytes"] == 100
    assert counters["texture_material_cache_hits"] == 1
    assert counters["texture_file_cache_hits"] == 1
    assert counters["texture_decoded_cache_hits"] == 1
    assert counters["texture_sync_decodes"] == 1
    assert counters["texture_evictions"] == 2
    assert counters["texture_evicted_bytes"] == 4096
    assert timing["worst_texture_material"] == "limestone"
    assert timing["worst_texture_size"] == (1024, 512)
    assert timing["worst_texture_bytes"] == 200
    assert timing["worst_texture_sync_decode"]


def test_record_chunk_upload_timing_accumulates_counts_and_worst_chunk():
    """Chunk upload counters accumulate into the current frame timing."""
    counters = streaming_frame_timing.new_chunk_upload_counters()
    counters.update(
        {
            "chunk_prepare_ms": 1.0,
            "vertex_pack_ms": 2.0,
            "buffer_ms": 3.0,
            "buffer_alloc_ms": 4.0,
            "buffer_write_ms": 5.0,
            "buffer_alloc_bytes": 6,
            "buffer_write_bytes": 7,
            "vao_ms": 8.0,
            "texture_ms": 9.0,
            "texture_decode_ms": 10.0,
            "texture_alloc_ms": 11.0,
            "texture_write_ms": 12.0,
            "texture_upload_ms": 13.0,
            "texture_mipmap_ms": 14.0,
            "texture_image_bytes": 15,
            "texture_material_cache_hits": 1,
            "texture_file_cache_hits": 2,
            "texture_decoded_cache_hits": 3,
            "texture_sync_decodes": 4,
            "texture_placeholders": 5,
            "texture_evictions": 6,
            "texture_evicted_bytes": 7,
            "chunk_bookkeeping_ms": 16.0,
            "groups": 17,
            "prepacked_groups": 18,
            "fallback_pack_groups": 19,
            "vertices": 20,
            "bytes": 21,
        }
    )
    timing = streaming_frame_timing.new_streaming_frame_timing()

    streaming_frame_timing.record_chunk_upload_timing(
        timing,
        counters,
        chunk_ms=30.0,
        cell=(1, 2, 3),
        completed=True,
    )

    assert timing["chunk_ready_ms"] == 30.0
    assert timing["chunks_uploaded"] == 1
    assert timing["groups_uploaded"] == 17
    assert timing["prepacked_groups"] == 18
    assert timing["fallback_pack_groups"] == 19
    assert timing["vertices_uploaded"] == 20
    assert timing["bytes_uploaded"] == 21
    assert timing["texture_evictions"] == 6
    assert timing["texture_evicted_bytes"] == 7
    assert timing["worst_chunk_cell"] == (1, 2, 3)
    assert timing["worst_chunk_ms"] == 30.0
    assert timing["worst_chunk_groups"] == 17
    assert timing["worst_chunk_bookkeeping_ms"] == 16.0


def test_format_streaming_frame_timing_includes_worst_records_and_slice_sizes():
    """Diagnostic formatting includes upload splits, stalls, and worst records."""
    timing = streaming_frame_timing.new_streaming_frame_timing()
    timing.update(
        {
            "drain_ms": 12.0,
            "ready_drain_ms": 9.0,
            "chunk_ready_ms": 5.0,
            "unload_ms": 1.0,
            "failure_drain_ms": 2.0,
            "buffer_alloc_ms": 1.5,
            "buffer_write_ms": 2.5,
            "texture_alloc_ms": 0.5,
            "texture_upload_ms": 3.5,
            "texture_evictions": 2,
            "texture_evicted_bytes": 3 * 1024 * 1024,
            "vbo_upload_slice_bytes": 256 * 1024,
            "texture_upload_slice_bytes": 128 * 1024,
            "upload_stalls": 1,
            "worst_texture_material": "sandstone",
            "worst_texture_ms": 6.0,
            "worst_texture_size": None,
            "worst_texture_bytes": 4 * 1024 * 1024,
            "worst_texture_decode_ms": 1.0,
            "worst_texture_alloc_ms": 2.0,
            "worst_texture_upload_ms": 3.0,
            "worst_texture_mipmap_ms": 4.0,
            "worst_chunk_cell": (4, 5, 6),
            "worst_chunk_ms": 7.0,
            "worst_chunk_groups": 8,
            "worst_chunk_vertices": 9,
            "worst_chunk_bytes": 2 * 1024 * 1024,
        }
    )

    detail = streaming_frame_timing.format_streaming_frame_timing(timing)

    assert "ready_other=3.0ms" in detail
    assert "drain_other=1.0ms" in detail
    assert "slices=vbo:256KB/tex:128KB" in detail
    assert "stalls=1" in detail
    assert "tex_evict=2" in detail
    assert "tex_evict_mb=3.0" in detail
    assert "worst_texture='sandstone' 6.0ms size=unknown bytes=4.0MB" in detail
    assert "worst_chunk=(4, 5, 6) 7.0ms groups=8 verts=9 vbo=2.0MB" in detail


def test_add_chunk_upload_counters_adds_matching_counter_sets():
    """Counter aggregation adds every source key into the target."""
    target = streaming_frame_timing.new_chunk_upload_counters()
    source = streaming_frame_timing.new_chunk_upload_counters()
    source["groups"] = 2
    source["bytes"] = 128

    streaming_frame_timing.add_chunk_upload_counters(target, source)

    assert target["groups"] == 2
    assert target["bytes"] == 128
