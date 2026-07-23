"""Tests for render-upload slice sizing and stall adaptation policy."""

from caveviewer.gui import upload_slice_policy


def test_render_upload_slice_vertices_is_triangle_aligned():
    """VBO slice vertex counts stay triangle-aligned and bounded."""
    vertices = upload_slice_policy.render_upload_slice_vertices(
        10 * upload_slice_policy.RENDER_UPLOAD_VERTEX_BYTES,
    )

    assert vertices == 9


def test_adapt_upload_slice_size_shrinks_texture_after_stall():
    """Texture upload slices shrink when measured upload time exceeds budget."""
    timing = {"upload_stalls": 0}
    state = upload_slice_policy.UploadSliceState(
        vbo_upload_slice_bytes=1024 * 1024,
        texture_upload_slice_bytes=1024 * 1024,
    )

    adjustment = upload_slice_policy.adapt_upload_slice_size(
        kind="texture",
        elapsed_ms=30.0,
        byte_count=1024 * 1024,
        target_ms=3.0,
        state=state,
        timing=timing,
    )

    assert adjustment.stalled
    assert adjustment.state.texture_upload_slice_bytes < 1024 * 1024
    assert adjustment.state.vbo_upload_slice_bytes == 1024 * 1024
    assert timing["upload_stalls"] == 1
    assert timing["texture_upload_slice_bytes"] == (
        adjustment.state.texture_upload_slice_bytes
    )


def test_adapt_upload_slice_size_uses_current_boosted_budget():
    """Operations inside the active budget do not shrink future slices."""
    state = upload_slice_policy.UploadSliceState(
        vbo_upload_slice_bytes=1024 * 1024,
        texture_upload_slice_bytes=1024 * 1024,
    )

    adjustment = upload_slice_policy.adapt_upload_slice_size(
        kind="vbo",
        elapsed_ms=5.0,
        byte_count=1024 * 1024,
        target_ms=8.0,
        state=state,
    )

    assert not adjustment.stalled
    assert adjustment.state == state


def test_record_upload_slice_sizes_writes_current_state_to_timing():
    """Timing diagnostics record current VBO and texture slice sizes."""
    timing = {}
    state = upload_slice_policy.UploadSliceState(
        vbo_upload_slice_bytes=256 * 1024,
        texture_upload_slice_bytes=128 * 1024,
    )

    upload_slice_policy.record_upload_slice_sizes(timing, state)

    assert timing == {
        "vbo_upload_slice_bytes": 256 * 1024,
        "texture_upload_slice_bytes": 128 * 1024,
    }
