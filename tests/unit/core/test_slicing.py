"""Exercise portable, lossless precompiled-map slice exports."""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

from caveviewer.core.chunking.io import (
    ChunkFileWriter,
    load_chunk_file,
)
from caveviewer.core.chunking.metadata import load_manifest
from caveviewer.core.map.cache_identity import (
    GUIDED_DIVE_CACHE_IDENTITY_KEY,
    GuidedDiveCacheIdentity,
    guided_dive_cache_identity_from_manifest,
)
from caveviewer.core.map.slicing import (
    SliceBounds,
    SliceExportCancelled,
    SliceExportRequest,
    export_slice,
    next_slice_display_name,
    unique_slice_output_dir,
)
from caveviewer.gui.map_opening import resolve_selected_map_folder


def _source_cache(tmp_path, *, texture_present: bool = True):
    source = tmp_path / "parent-cache"
    positions = np.array(
        [
            [-1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ],
        dtype=np.float32,
    )
    uvs = np.array([[0.0, 0.0], [1.0, 0.0], [0.5, 1.0]], dtype=np.float32)
    normals = np.tile(np.array([[0.0, 0.0, 1.0]], dtype=np.float32), (3, 1))
    writer = ChunkFileWriter(str(source / "chunks"), (0, 0, 0))
    writer.write_group("rock", positions, uvs, normals)
    written = writer.finish()
    assert written is not None

    texture = source / "textures" / "rock.png"
    texture.parent.mkdir(parents=True, exist_ok=True)
    if texture_present:
        texture.write_bytes(b"small texture fixture")
    parent_identity = GuidedDiveCacheIdentity(1, "a" * 64, "b" * 64)
    manifest = {
        "version": 1,
        "chunk_size": 50.0,
        "max_upload_group_mb": 16.0,
        "source_obj": "parent.obj",
        "mtl_materials": {"rock": "textures/rock.png"},
        "chunks": {
            "0_0_0": {
                "materials": ["rock"],
                "bounds_min": written.bounds_min.tolist(),
                "bounds_max": written.bounds_max.tolist(),
            }
        },
        "triangle_count": 1,
        GUIDED_DIVE_CACHE_IDENTITY_KEY: parent_identity.payload(),
    }
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return source, parent_identity


def _request(source, output):
    bounds = SliceBounds(
        minimum=(0.0, -0.5, -0.5),
        maximum=(0.75, 1.5, 0.5),
    )
    return SliceExportRequest(
        source_cache_dir=str(source),
        output_dir=str(output),
        bounds=bounds,
        entry_position=(0.25, 0.0, 0.0),
        display_name="Interesting passage",
        root_cave_name="Parent Cave",
    )


def test_export_slice_preserves_complete_chunk_and_is_portable_without_parent(
    tmp_path,
):
    source, parent_identity = _source_cache(tmp_path)
    output = tmp_path / "maps" / "Interesting passage"
    source_chunk_bytes = (source / "chunks" / "0_0_0.bin").read_bytes()

    result = export_slice(_request(source, output))

    assert result.output_dir == str(output)
    assert result.chunk_count == 1
    assert result.triangle_count == 1
    manifest = load_manifest(str(output))
    assert manifest is not None
    assert manifest["source_obj"].endswith(".cvslice")
    assert (output / manifest["source_obj"]).is_file()
    marker = json.loads((output / manifest["source_obj"]).read_text(encoding="utf-8"))
    assert marker["root_cave_name"] == "Parent Cave"
    assert marker["exporter_version"] == 2
    assert marker["geometry_mode"] == "whole_source_chunks"
    assert (output / "textures" / "rock.png").read_bytes() == b"small texture fixture"
    assert manifest["slice"]["entry_position"] == [0.25, 0.0, 0.0]
    assert manifest["slice"]["root_cave_name"] == "Parent Cave"
    assert manifest["slice"]["exporter_version"] == 2
    assert manifest["slice"]["geometry_mode"] == "whole_source_chunks"
    assert manifest["footprint_cell_size"] == 2.0
    assert manifest["footprint_cells"] == [-1, 0, 0, 0]
    assert (output / "chunks" / "0_0_0.bin").read_bytes() == source_chunk_bytes
    identity = guided_dive_cache_identity_from_manifest(manifest)
    assert identity is not None
    assert identity != parent_identity

    shutil.rmtree(source)
    copied = load_chunk_file(str(output), (0, 0, 0))
    positions = next(iter(copied.groups.values())).positions
    assert np.array_equal(
        positions,
        np.array(
            [[-1.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            dtype=np.float32,
        ),
    )
    opened = resolve_selected_map_folder(str(output))
    assert opened.is_prebuilt_cache
    assert opened.cache_dir == str(output)
    assert opened.map_name == manifest["source_obj"]


def test_export_slice_cleans_private_staging_when_canceled(tmp_path):
    source, _identity = _source_cache(tmp_path)
    output = tmp_path / "maps" / "Canceled"
    cancellation = {"requested": False}

    def progress(stage, _fraction):
        if stage == "copying slice chunks":
            cancellation["requested"] = True

    with pytest.raises(SliceExportCancelled):
        export_slice(
            _request(source, output),
            progress_cb=progress,
            cancel_requested=lambda: cancellation["requested"],
        )

    assert not output.exists()
    assert not list((tmp_path / "maps").glob(".Canceled.tmp-*"))


def test_export_slice_rejects_missing_required_texture_without_output(tmp_path):
    source, _identity = _source_cache(tmp_path, texture_present=False)
    output = tmp_path / "maps" / "Missing texture"

    with pytest.raises(ValueError, match="texture.*missing"):
        export_slice(_request(source, output))

    assert not output.exists()


def test_export_slice_rejects_unsafe_texture_path_without_output(tmp_path):
    source, _identity = _source_cache(tmp_path)
    output = tmp_path / "maps" / "Unsafe texture"
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["mtl_materials"]["rock"] = "../outside.png"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsafe texture path"):
        export_slice(_request(source, output))

    assert not output.exists()


def test_export_slice_keeps_whole_chunk_when_box_crosses_no_triangle(tmp_path):
    source, _identity = _source_cache(tmp_path)
    output = tmp_path / "maps" / "Whole chunk"
    request = SliceExportRequest(
        source_cache_dir=str(source),
        output_dir=str(output),
        bounds=SliceBounds((0.8, 0.8, -0.1), (1.0, 1.0, 0.1)),
        entry_position=(0.9, 0.9, 0.0),
    )

    result = export_slice(request)

    assert result.triangle_count == 1
    chunk = load_chunk_file(str(output), (0, 0, 0))
    positions = next(iter(chunk.groups.values())).positions
    assert np.any(positions[:, 0] < request.bounds.minimum[0])


def test_export_slice_removes_staging_when_no_chunk_overlaps(tmp_path):
    source, _identity = _source_cache(tmp_path)
    output = tmp_path / "maps" / "Empty"
    request = SliceExportRequest(
        source_cache_dir=str(source),
        output_dir=str(output),
        bounds=SliceBounds((10.0, 10.0, 10.0), (11.0, 11.0, 11.0)),
        entry_position=(10.5, 10.5, 10.5),
    )

    with pytest.raises(ValueError, match="does not overlap any map chunk"):
        export_slice(request)

    assert not output.exists()
    assert not list((tmp_path / "maps").glob(".Empty.tmp-*"))


def test_export_slice_rejects_a_parent_manifest_change_before_publication(tmp_path):
    source, _identity = _source_cache(tmp_path)
    output = tmp_path / "maps" / "Changed parent"
    manifest_path = source / "manifest.json"
    changed = False

    def progress(stage, _fraction):
        nonlocal changed
        if stage != "copying slice chunks" or changed:
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["triangle_count"] = 2
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        changed = True

    with pytest.raises(RuntimeError, match="changed while the slice was exporting"):
        export_slice(_request(source, output), progress_cb=progress)

    assert changed
    assert not output.exists()
    assert not list((tmp_path / "maps").glob(".Changed parent.tmp-*"))


def test_slice_bounds_add_configured_padding_to_camera_anchors():
    bounds = SliceBounds.from_anchors((2.0, 3.0, 4.0), (-1.0, 5.0, 0.0), padding=2.5)

    assert bounds.minimum == (-3.5, 0.5, -2.5)
    assert bounds.maximum == (4.5, 7.5, 6.5)


def test_unique_slice_output_dir_uses_collision_safe_child_name(tmp_path):
    (tmp_path / "Cave slice").mkdir()

    assert unique_slice_output_dir(tmp_path, "Cave slice") == str(
        tmp_path / "Cave slice 2"
    )


def test_next_slice_display_name_preserves_the_cave_name_and_starts_at_one(tmp_path):
    assert next_slice_display_name(tmp_path, "Ginnie Springs") == (
        "Ginnie Springs - Segment 1"
    )


def test_next_slice_display_name_uses_the_highest_matching_segment(tmp_path):
    (tmp_path / "Ginnie Springs - Segment 1").mkdir()
    (tmp_path / "ginnie springs - segment 3").mkdir()
    (tmp_path / "Ginnie Springs - Segment 4 backup").mkdir()
    (tmp_path / "Other Cave - Segment 99").mkdir()

    assert next_slice_display_name(tmp_path, "Ginnie Springs") == (
        "Ginnie Springs - Segment 4"
    )


def test_next_slice_display_name_reserves_space_for_the_segment_suffix(tmp_path):
    cave_name = "G" * 100

    first_name = next_slice_display_name(tmp_path, cave_name)
    (tmp_path / first_name).mkdir()
    second_name = next_slice_display_name(tmp_path, cave_name)

    assert first_name.endswith(" - Segment 1")
    assert second_name.endswith(" - Segment 2")
    assert len(first_name) <= 80
    assert len(second_name) <= 80


@pytest.mark.parametrize("output_suffix", ("", "nested-slice"))
def test_slice_request_rejects_an_output_that_overlaps_source_cache(
    tmp_path,
    output_suffix,
):
    source = tmp_path / "cache"
    source.mkdir()
    output = source.parent if not output_suffix else source / output_suffix

    with pytest.raises(ValueError, match="must not contain or replace"):
        SliceExportRequest(
            source_cache_dir=str(source),
            output_dir=str(output),
            bounds=SliceBounds((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),
            entry_position=(0.5, 0.5, 0.5),
        )
