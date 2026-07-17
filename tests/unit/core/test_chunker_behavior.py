"""Behavior and failure-path tests for chunk cache construction and lookup."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

import numpy as np
import pytest

from caveviewer.core import chunker, hardware_memory, obj_parser
from caveviewer.core.worker_config import WorkerAllocation


def _attributed_mesh() -> obj_parser.RawMesh:
    positions = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [10.0, -1.0, 2.0],
            [12.0, -1.0, 2.0],
            [10.0, 2.0, 2.0],
        ],
        dtype=np.float32,
    )
    uvs = np.array(
        [
            [0.0, 0.0],
            [1.0, 0.0],
            [0.0, 1.0],
            [0.0, 0.0],
            [1.0, 0.0],
            [0.0, 1.0],
        ],
        dtype=np.float32,
    )
    normals = np.tile(
        np.array([[0.0, 0.0, 1.0]], dtype=np.float32), (len(positions), 1)
    )
    faces = np.array([[0, 1, 2], [3, 4, 5]], dtype=np.int32)
    return obj_parser.RawMesh(
        positions=positions,
        uvs=uvs,
        normals=normals,
        face_pos_idx=faces,
        face_uv_idx=faces.copy(),
        face_nrm_idx=faces.copy(),
        material_ranges=[obj_parser.MaterialRange("rock", 0, 2)],
    )


def _mesh_with_cells(cell_count: int) -> obj_parser.RawMesh:
    """Build one triangle per spatial cell for worker-pool lifecycle tests."""
    positions = []
    for cell_index in range(cell_count):
        x = float(cell_index * chunker.DEFAULT_CHUNK_SIZE)
        positions.extend(
            ((x, 0.0, 0.0), (x + 1.0, 0.0, 0.0), (x, 1.0, 0.0))
        )
    positions = np.asarray(positions, dtype=np.float32)
    faces = np.arange(cell_count * 3, dtype=np.int32).reshape(cell_count, 3)
    return obj_parser.RawMesh(
        positions=positions,
        uvs=np.zeros((cell_count * 3, 2), dtype=np.float32),
        normals=np.tile(
            np.array([[0.0, 0.0, 1.0]], dtype=np.float32),
            (cell_count * 3, 1),
        ),
        face_pos_idx=faces,
        face_uv_idx=faces.copy(),
        face_nrm_idx=faces.copy(),
        material_ranges=[obj_parser.MaterialRange("rock", 0, cell_count)],
    )


def _current_manifest() -> dict:
    return {
        "version": chunker._VERSION,
        "chunk_size": 8.0,
        "chunks": {},
    }


def _write_manifest(cache_dir: Path, payload: dict | str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / chunker.MANIFEST_NAME
    if isinstance(payload, str):
        manifest_path.write_text(payload, encoding="utf-8")
    else:
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


def _chunk_info(y_min: float, y_max: float) -> dict:
    return {
        "bounds_min": [0.0, y_min, 0.0],
        "bounds_max": [1.0, y_max, 1.0],
        "materials": [],
    }


def test_chunk_size_configuration_handles_default_valid_and_invalid_values(
    monkeypatch, caplog
):
    monkeypatch.delenv(chunker.CHUNK_SIZE_ENV_VAR, raising=False)
    assert chunker._DEFAULT_CHUNK_SIZE_FALLBACK == 50.0
    assert chunker._resolve_default_chunk_size() == chunker._DEFAULT_CHUNK_SIZE_FALLBACK

    monkeypatch.setenv(chunker.CHUNK_SIZE_ENV_VAR, "12.5")
    assert chunker._resolve_default_chunk_size() == 12.5

    with caplog.at_level(logging.WARNING):
        monkeypatch.setenv(chunker.CHUNK_SIZE_ENV_VAR, "0")
        assert (
            chunker._resolve_default_chunk_size()
            == chunker._DEFAULT_CHUNK_SIZE_FALLBACK
        )
        monkeypatch.setenv(chunker.CHUNK_SIZE_ENV_VAR, "not-a-number")
        assert (
            chunker._resolve_default_chunk_size()
            == chunker._DEFAULT_CHUNK_SIZE_FALLBACK
        )

    assert chunker.CHUNK_SIZE_ENV_VAR in caplog.text
    assert chunker.configured_chunk_size() == chunker.DEFAULT_CHUNK_SIZE


def test_import_memory_estimate_scales_with_geometry_size():
    small = chunker.estimate_import_memory_bytes(10, 10, 10, 10)
    large = chunker.estimate_import_memory_bytes(10_000, 10_000, 10_000, 10_000)

    assert small >= chunker.IMPORT_MEMORY_FIXED_OVERHEAD_BYTES
    assert large > small


def test_incremental_import_memory_estimate_avoids_face_count_scaling():
    traditional = chunker.estimate_import_memory_bytes(
        1_000_000,
        1_000_000,
        1_000_000,
        50_000_000,
    )
    incremental = chunker.estimate_incremental_import_memory_bytes(
        1_000_000,
        1_000_000,
        1_000_000,
        face_batch_size=100_000,
    )

    assert incremental < traditional


def test_incremental_import_memory_estimate_scales_with_bucket_workers():
    single_worker = chunker.estimate_incremental_import_memory_bytes(
        100_000,
        100_000,
        100_000,
        face_batch_size=50_000,
        bucket_workers=1,
    )
    multiple_workers = chunker.estimate_incremental_import_memory_bytes(
        100_000,
        100_000,
        100_000,
        face_batch_size=50_000,
        bucket_workers=4,
    )

    assert multiple_workers > single_worker


def test_configured_obj_bucket_workers_clamps_and_defaults(caplog):
    assert (
        chunker._configured_obj_bucket_workers({})
        == chunker._DEFAULT_OBJ_BUCKET_WORKERS
    )
    assert (
        chunker._configured_obj_bucket_workers(
            {chunker.OBJ_BUCKET_WORKERS_ENV_VAR: "4"}
        )
        == 4
    )
    assert (
        chunker._configured_obj_bucket_workers(
            {chunker.OBJ_BUCKET_WORKERS_ENV_VAR: "999"}
        )
        == chunker._MAX_OBJ_BUCKET_WORKERS
    )

    caplog.set_level(logging.WARNING, logger="caveviewer")
    assert (
        chunker._configured_obj_bucket_workers(
            {chunker.OBJ_BUCKET_WORKERS_ENV_VAR: "bad"}
        )
        == chunker._DEFAULT_OBJ_BUCKET_WORKERS
    )
    assert chunker.OBJ_BUCKET_WORKERS_ENV_VAR in caplog.text


def test_import_memory_preflight_warns_when_current_available_ram_is_low(
    monkeypatch, caplog
):
    monkeypatch.setattr(
        chunker.hardware_memory,
        "detect_ram_snapshot",
        lambda: hardware_memory.RamSnapshot(
            total_bytes=8 * 1024 ** 3,
            available_bytes=1 * 1024 ** 3,
        ),
    )
    caplog.set_level(logging.WARNING, logger="caveviewer")

    chunker.ensure_sufficient_import_memory(
        1_000_000,
        1_000_000,
        1_000_000,
        20_000_000,
        source_path="/maps/huge.obj",
    )

    assert "Import RAM preflight warning for huge.obj" in caplog.text
    assert "physical-memory overcommit allowance" in caplog.text


def test_import_memory_preflight_rejects_when_estimate_exceeds_physical_limit(
    monkeypatch,
):
    monkeypatch.setattr(
        chunker.hardware_memory,
        "detect_ram_snapshot",
        lambda: hardware_memory.RamSnapshot(
            total_bytes=8 * 1024 ** 3,
            available_bytes=7 * 1024 ** 3,
        ),
    )

    with pytest.raises(chunker.InsufficientImportMemoryError) as raised:
        chunker.ensure_sufficient_import_memory(
            1_000_000,
            1_000_000,
            1_000_000,
            200_000_000,
            source_path="/maps/huge.obj",
        )

    assert raised.value.physical_limit_bytes is not None
    assert raised.value.required_bytes > raised.value.physical_limit_bytes
    assert "huge.obj" in str(raised.value)
    assert "physical-memory overcommit allowance" in str(raised.value)


def test_import_memory_preflight_allows_import_when_ram_headroom_is_available(
    monkeypatch,
):
    monkeypatch.setattr(
        chunker.hardware_memory,
        "detect_ram_snapshot",
        lambda: hardware_memory.RamSnapshot(
            total_bytes=16 * 1024 ** 3,
            available_bytes=8 * 1024 ** 3,
        ),
    )

    chunker.ensure_sufficient_import_memory(
        10_000,
        10_000,
        10_000,
        10_000,
        source_path="/maps/small.obj",
    )


def test_build_cache_reports_progress_and_atomically_replaces_existing_cache(
    tmp_path
):
    source = tmp_path / "map.obj"
    source.write_bytes(b"small source map")
    old_cache = tmp_path / "managed" / "map-key"
    old_cache.mkdir(parents=True)
    (old_cache / "old-marker").write_text("old", encoding="utf-8")
    events: list[tuple[str, float]] = []

    result = chunker.build_cache(
        str(source),
        _attributed_mesh(),
        {},
        cache_dir=str(old_cache),
        progress_cb=lambda stage, fraction: events.append((stage, fraction)),
    )

    assert result == str(old_cache)
    assert not (old_cache / "old-marker").exists()
    assert (old_cache / chunker.MANIFEST_NAME).is_file()
    assert events[0] == ("computing face centroids", 0.0)
    assert events[-1] == ("done", 1.0)
    stages = {stage for stage, _fraction in events}
    assert {
        "grouping faces by cell",
        "grouping chunk faces",
        "writing chunk files",
        "writing manifest",
    } <= stages
    assert not list(old_cache.parent.glob(".map-key.tmp-*.previous"))


def test_incremental_obj_cache_build_writes_standard_chunks(tmp_path, monkeypatch):
    source = tmp_path / "map.obj"
    source.write_text(
        "\n".join(
            [
                "mtllib map.mtl",
                "v 0 0 0",
                "v 1 0 0",
                "v 0 1 0",
                "v 10 0 0",
                "v 11 0 0",
                "v 10 1 0",
                "vt 0 0",
                "vt 1 0",
                "vt 0 1",
                "vt 0 0",
                "vt 1 0",
                "vt 0 1",
                "vn 0 0 1",
                "usemtl rock",
                "f 1/1/1 2/2/1 3/3/1",
                "usemtl sand",
                "f 4/4/1 5/5/1 6/6/1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    cache_dir = tmp_path / "managed" / "map-key"
    monkeypatch.setattr(
        chunker.hardware_memory,
        "detect_ram_snapshot",
        lambda: hardware_memory.RamSnapshot(
            total_bytes=64 * 1024**3,
            available_bytes=48 * 1024**3,
        ),
    )

    result = chunker.build_cache_incremental_obj(
        str(source),
        {
            "rock": obj_parser.Material("rock", "rock.jpg"),
            "sand": obj_parser.Material("sand", "sand.jpg"),
        },
        cache_dir=str(cache_dir),
        chunk_size=8.0,
        face_batch_size=1,
        bucket_workers=2,
    )

    assert result == str(cache_dir)
    manifest = chunker.load_manifest(str(cache_dir))
    assert manifest["import_mode"] == "incremental_obj"
    assert manifest["triangle_count"] == 2
    assert manifest["mtl_materials"] == {"rock": "rock.jpg", "sand": "sand.jpg"}
    assert set(manifest["chunks"]) == {"0_0_0", "1_0_0"}
    assert not (cache_dir / ".chunk-buckets").exists()

    first = chunker.load_chunk_file(str(cache_dir), (0, 0, 0))
    assert set(first.groups) == {"rock"}
    np.testing.assert_allclose(
        first.groups["rock"].positions,
        np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            dtype=np.float32,
        ),
    )

    second = chunker.load_chunk_file(str(cache_dir), (1, 0, 0))
    assert set(second.groups) == {"sand"}
    np.testing.assert_allclose(second.groups["sand"].normals, [[0.0, 0.0, 1.0]] * 3)


def test_incremental_bucket_writer_uses_bounded_record_slices(tmp_path, monkeypatch):
    vertex_data = obj_parser.ObjVertexData(
        positions=np.array(
            [
                [0.0, 0.0, 0.0],
                [1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [2.0, 0.0, 0.0],
                [3.0, 0.0, 0.0],
                [2.0, 1.0, 0.0],
            ],
            dtype=np.float32,
        ),
        uvs=np.array(
            [
                [0.0, 0.0],
                [1.0, 0.0],
                [0.0, 1.0],
                [0.0, 0.0],
                [1.0, 0.0],
                [0.0, 1.0],
            ],
            dtype=np.float32,
        ),
        normals=np.array([[0.0, 0.0, 1.0]], dtype=np.float32),
        face_count=2,
    )
    batch = obj_parser.ObjFaceBatch(
        face_pos_idx=np.array([[0, 1, 2], [3, 4, 5]], dtype=np.int32),
        face_uv_idx=np.array([[0, 1, 2], [3, 4, 5]], dtype=np.int32),
        face_nrm_idx=np.zeros((2, 3), dtype=np.int32),
        material_names=["rock", "rock"],
    )
    original_empty = np.empty

    def guarded_empty(shape, *args, **kwargs):
        normalized_shape = (shape,) if isinstance(shape, int) else tuple(shape)
        if normalized_shape == (2, 3, 8):
            raise AssertionError("bucket writer allocated full-batch records")
        return original_empty(shape, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(chunker, "_OBJ_BUCKET_RECORD_SLICE_FACES", 1)
        patch.setattr(chunker.np, "empty", guarded_empty)
        face_count, bucket_paths = chunker._write_obj_face_batch_bucket_parts(
            vertex_data,
            batch,
            str(tmp_path / "buckets"),
            chunk_size=10.0,
        )

    assert face_count == 2
    assert set(bucket_paths) == {((0, 0, 0), "rock")}
    records = np.fromfile(next(iter(bucket_paths.values())), dtype=np.float32)
    records = records.reshape(-1, 8)
    assert records.shape == (6, 8)
    np.testing.assert_allclose(records[:, 0:3], vertex_data.positions)
    np.testing.assert_allclose(records[:, 3:5], vertex_data.uvs)
    np.testing.assert_allclose(records[:, 5:8], [[0.0, 0.0, 1.0]] * 6)


def test_incremental_bucket_finalizer_streams_parts_without_concatenate(
    tmp_path,
    monkeypatch,
):
    chunks_dir = tmp_path / chunker.CHUNKS_DIRNAME
    chunks_dir.mkdir()
    bucket_a = tmp_path / "bucket-a.bin"
    bucket_b = tmp_path / "bucket-b.bin"
    records_a = np.array(
        [
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
            [1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            [0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float32,
    )
    records_b = np.array(
        [
            [2.0, 0.0, 0.0, 0.2, 0.0, 0.0, 1.0, 0.0],
            [3.0, 0.0, 0.0, 0.3, 0.0, 0.0, 1.0, 0.0],
            [2.0, 1.0, 0.0, 0.2, 1.0, 0.0, 1.0, 0.0],
        ],
        dtype=np.float32,
    )
    records_a.tofile(bucket_a)
    records_b.tofile(bucket_b)

    def fail_concatenate(*_args, **_kwargs):
        raise AssertionError("bucket finalizer concatenated bucket parts")

    with monkeypatch.context() as patch:
        patch.setattr(chunker.np, "concatenate", fail_concatenate)
        bounds_min, bounds_max, used_materials = (
            chunker._write_chunk_file_from_buckets(
                str(chunks_dir),
                "0_0_0",
                [("rock", [str(bucket_a), str(bucket_b)])],
            )
        )

    assert used_materials == ["rock"]
    np.testing.assert_allclose(bounds_min, [0.0, 0.0, 0.0])
    np.testing.assert_allclose(bounds_max, [3.0, 1.0, 0.0])
    assert not bucket_a.exists()
    assert not bucket_b.exists()

    chunk = chunker.load_chunk_file(str(tmp_path), (0, 0, 0))
    group = chunk.groups["rock"]
    expected_records = np.concatenate((records_a, records_b), axis=0)
    np.testing.assert_allclose(group.positions, expected_records[:, 0:3])
    np.testing.assert_allclose(group.uvs, expected_records[:, 3:5])
    np.testing.assert_allclose(group.normals, expected_records[:, 5:8])


def test_incremental_obj_cache_progress_never_regresses(tmp_path, monkeypatch):
    source = tmp_path / "map.obj"
    source.write_text("unused", encoding="utf-8")
    cache_dir = tmp_path / "managed" / "map-key"
    progress: list[tuple[str, float]] = []
    vertex_data = obj_parser.ObjVertexData(
        positions=np.array([[0.0, 0.0, 0.0]], dtype=np.float32),
        uvs=np.zeros((0, 2), dtype=np.float32),
        normals=np.zeros((0, 3), dtype=np.float32),
        face_count=100,
    )
    batch = obj_parser.ObjFaceBatch(
        face_pos_idx=np.zeros((1, 3), dtype=np.int32),
        face_uv_idx=np.full((1, 3), -1, dtype=np.int32),
        face_nrm_idx=np.full((1, 3), -1, dtype=np.int32),
        material_names=["rock"],
    )

    def parse_vertices(_path, progress_cb=None, preflight_cb=None):
        if progress_cb:
            progress_cb("parsing vertices", 1.0)
        return vertex_data

    def iter_batches(_path, *, batch_size, progress_cb=None):
        del batch_size
        if progress_cb:
            progress_cb("bucketing faces", 1.0)
        yield batch

    def write_bucket_parts(_vertex_data, _batch, _bucket_root, *, chunk_size):
        del _vertex_data, _batch, _bucket_root, chunk_size
        return 1, {((0, 0, 0), "rock"): "fake-bucket-part"}

    def finalize_buckets(
        _chunks_dir,
        _bucket_parts,
        *,
        progress_cb=None,
        pause_requested=None,
        checkpoint_cb=None,
        initial_manifest_chunks=None,
        total_cell_count=None,
    ):
        del pause_requested, checkpoint_cb, initial_manifest_chunks, total_cell_count
        if progress_cb:
            progress_cb("writing chunk files", 0.66)
        return {
            "0_0_0": {
                "materials": ["rock"],
                "bounds_min": [0.0, 0.0, 0.0],
                "bounds_max": [1.0, 1.0, 1.0],
            }
        }

    monkeypatch.setattr(chunker, "parse_obj_vertices", parse_vertices)
    monkeypatch.setattr(chunker, "iter_obj_face_batches", iter_batches)
    monkeypatch.setattr(
        chunker, "_write_obj_face_batch_bucket_parts", write_bucket_parts
    )
    monkeypatch.setattr(chunker, "_finalize_incremental_buckets", finalize_buckets)

    chunker.build_cache_incremental_obj(
        str(source),
        {"rock": obj_parser.Material("rock", "rock.jpg")},
        cache_dir=str(cache_dir),
        chunk_size=8.0,
        face_batch_size=1,
        bucket_workers=1,
        progress_cb=lambda stage, fraction: progress.append((stage, fraction)),
    )

    fractions = [fraction for _stage, fraction in progress]
    assert fractions == sorted(fractions)
    assert ("bucketing faces", 0.65) in progress
    assert progress[-1] == ("done", 1.0)


def test_incremental_obj_import_pauses_and_resumes_from_checkpoint(
    tmp_path,
    monkeypatch,
    caplog,
):
    source = tmp_path / "map.obj"
    source.write_text(
        "\n".join(
            [
                "v 0 0 0",
                "v 1 0 0",
                "v 0 1 0",
                "v 10 0 0",
                "v 11 0 0",
                "v 10 1 0",
                "vn 0 0 1",
                "usemtl rock",
                "f 1//1 2//1 3//1",
                "usemtl sand",
                "f 4//1 5//1 6//1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    cache_dir = tmp_path / "managed" / "map-key"
    monkeypatch.setattr(
        chunker.hardware_memory,
        "detect_ram_snapshot",
        lambda: hardware_memory.RamSnapshot(
            total_bytes=64 * 1024**3,
            available_bytes=48 * 1024**3,
        ),
    )
    pause_checks = 0

    def pause_after_first_batch():
        nonlocal pause_checks
        pause_checks += 1
        return pause_checks >= 2

    with pytest.raises(chunker.ImportPaused) as paused:
        chunker.build_cache_incremental_obj(
            str(source),
            {
                "rock": obj_parser.Material("rock", "rock.jpg"),
                "sand": obj_parser.Material("sand", "sand.jpg"),
            },
            cache_dir=str(cache_dir),
            chunk_size=8.0,
            face_batch_size=1,
            bucket_workers=1,
            pause_requested=pause_after_first_batch,
        )

    resume_dir = Path(paused.value.resume_dir)
    assert resume_dir.is_dir()
    assert not cache_dir.exists()
    checkpoint = json.loads(
        (resume_dir / chunker.IMPORT_RESUME_MANIFEST_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint["stage"] == "bucketing"
    assert checkpoint["next_batch_index"] == 1
    assert checkpoint["bucketed_faces"] == 1

    caplog.set_level(logging.INFO, logger="caveviewer")
    result = chunker.build_cache_incremental_obj(
        str(source),
        {
            "rock": obj_parser.Material("rock", "rock.jpg"),
            "sand": obj_parser.Material("sand", "sand.jpg"),
        },
        cache_dir=str(cache_dir),
        chunk_size=8.0,
        face_batch_size=1,
        bucket_workers=1,
        pause_requested=lambda: False,
    )

    assert result == str(cache_dir)
    assert "Resuming previously paused OBJ import from checkpoint" in caplog.text
    assert not resume_dir.exists()
    assert not (cache_dir / chunker.IMPORT_RESUME_MANIFEST_NAME).exists()
    manifest = chunker.load_manifest(str(cache_dir))
    assert manifest["triangle_count"] == 2
    assert set(manifest["chunks"]) == {"0_0_0", "1_0_0"}


def test_publish_failure_restores_previous_cache(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    cache = tmp_path / "cache"
    staging.mkdir()
    cache.mkdir()
    (staging / "new").write_text("new", encoding="utf-8")
    (cache / "old").write_text("old", encoding="utf-8")
    real_replace = chunker.os.replace

    def fail_new_cache_publish(source, destination):
        if Path(source) == staging:
            raise OSError("publish failed")
        real_replace(source, destination)

    monkeypatch.setattr(chunker.os, "replace", fail_new_cache_publish)

    with pytest.raises(OSError, match="publish failed"):
        chunker._publish_cache_directory(str(staging), str(cache))

    assert (cache / "old").read_text(encoding="utf-8") == "old"
    assert (staging / "new").read_text(encoding="utf-8") == "new"
    assert not Path(f"{staging}.previous").exists()


def test_first_publish_failure_leaves_staging_available_for_outer_cleanup(
    tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    cache = tmp_path / "cache"
    staging.mkdir()
    (staging / "new").write_text("new", encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("first publish failed")

    monkeypatch.setattr(chunker.os, "replace", fail_replace)

    with pytest.raises(OSError, match="first publish failed"):
        chunker._publish_cache_directory(str(staging), str(cache))

    assert (staging / "new").read_text(encoding="utf-8") == "new"
    assert not cache.exists()


def test_publish_logs_when_previous_cache_cannot_be_restored(
    tmp_path, monkeypatch, caplog
):
    staging = tmp_path / "staging"
    cache = tmp_path / "cache"
    staging.mkdir()
    cache.mkdir()
    (cache / "old").write_text("old", encoding="utf-8")
    real_replace = chunker.os.replace

    def fail_publish_and_restore(source, destination):
        source_path = Path(source)
        if source_path == staging or source_path == Path(f"{staging}.previous"):
            raise OSError("replace failed")
        real_replace(source, destination)

    monkeypatch.setattr(chunker.os, "replace", fail_publish_and_restore)

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(OSError, match="replace failed"),
    ):
        chunker._publish_cache_directory(str(staging), str(cache))

    assert "Could not restore previous cache" in caplog.text
    assert Path(f"{staging}.previous").is_dir()
    assert not cache.exists()


def test_publish_keeps_new_cache_when_backup_cleanup_fails(
    tmp_path, monkeypatch, caplog
):
    staging = tmp_path / "staging"
    cache = tmp_path / "cache"
    staging.mkdir()
    cache.mkdir()
    (staging / "new").write_text("new", encoding="utf-8")
    (cache / "old").write_text("old", encoding="utf-8")
    backup = Path(f"{staging}.previous")
    real_rmtree = chunker.shutil.rmtree

    def fail_backup_cleanup(path, *args, **kwargs):
        if Path(path) == backup:
            raise OSError("cleanup failed")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(chunker.shutil, "rmtree", fail_backup_cleanup)

    with caplog.at_level(logging.WARNING):
        chunker._publish_cache_directory(str(staging), str(cache))

    assert (cache / "new").read_text(encoding="utf-8") == "new"
    assert (backup / "old").read_text(encoding="utf-8") == "old"
    assert "Could not remove replaced cache backup" in caplog.text


def test_parallel_write_failure_cancels_other_active_chunk_work(
    tmp_path, monkeypatch
):
    source = tmp_path / "map.obj"
    source.write_text("map", encoding="utf-8")
    cache_dir = tmp_path / "staging-cache"
    slow_write_started = threading.Event()
    release_slow_write = threading.Event()
    cancel_calls = 0
    real_cancel = chunker.concurrent.futures.Future.cancel

    def release_worker_then_cancel(future):
        nonlocal cancel_calls
        cancel_calls += 1
        release_slow_write.set()
        return real_cancel(future)

    def fail_one_write(_chunks_dir, cell_str, _mesh, _groups):
        if cell_str == "1_0_0":
            assert slow_write_started.wait(timeout=2.0)
            raise OSError("chunk write failed")
        if cell_str == "2_0_0":
            slow_write_started.set()
            assert release_slow_write.wait(timeout=2.0)
        bounds = np.zeros(3, dtype=np.float32)
        return bounds, bounds.copy(), ["rock"]

    monkeypatch.setattr(
        chunker.concurrent.futures.Future, "cancel", release_worker_then_cancel
    )
    monkeypatch.setattr(
        chunker,
        "resolve_worker_allocation",
        lambda *_args, **_kwargs: WorkerAllocation(2, 2, 8, 2),
    )
    monkeypatch.setattr(
        chunker.hardware_memory,
        "detect_ram_snapshot",
        lambda: hardware_memory.RamSnapshot(100, 100),
    )
    monkeypatch.setattr(chunker, "_write_chunk_file", fail_one_write)

    with pytest.raises(OSError, match="chunk write failed"):
        chunker._build_cache_in_directory(
            str(source), _mesh_with_cells(4), {}, str(cache_dir)
        )

    assert cancel_calls == 1


def test_cache_build_stays_at_one_worker_when_ram_is_at_limit(
    tmp_path, monkeypatch, caplog
):
    source = tmp_path / "map.obj"
    source.write_text("map", encoding="utf-8")
    cache_dir = tmp_path / "staging-cache"
    worker_threads = []
    written_cells = []
    probe_write_counts = []

    def write_cell(_chunks_dir, cell_str, _mesh, _groups):
        worker_threads.append(threading.get_ident())
        written_cells.append(cell_str)
        bounds = np.zeros(3, dtype=np.float32)
        return bounds, bounds.copy(), ["rock"]

    def probe_ram():
        probe_write_counts.append(len(written_cells))
        return hardware_memory.RamSnapshot(100, 20)

    monkeypatch.setattr(
        chunker,
        "resolve_worker_allocation",
        lambda *_args, **_kwargs: WorkerAllocation(8, 2, 10, 8),
    )
    monkeypatch.setattr(
        chunker.hardware_memory, "detect_ram_snapshot", probe_ram
    )
    monkeypatch.setattr(chunker, "_write_chunk_file", write_cell)

    with caplog.at_level(logging.INFO, logger="caveviewer"):
        chunker._build_cache_in_directory(
            str(source), _mesh_with_cells(6), {}, str(cache_dir)
        )

    assert len(written_cells) == 6
    assert len(set(worker_threads)) == 1
    assert probe_write_counts
    assert min(probe_write_counts) >= 1
    assert "Cache-build worker target resolved to 8 worker(s)" in caplog.text
    assert "(requested 8, reserved CPUs 2, logical CPUs 10)" in caplog.text
    assert "System RAM utilization is 80.0%" in caplog.text


def test_cache_build_logs_successful_ram_based_worker_admission(
    tmp_path, monkeypatch, caplog
):
    source = tmp_path / "map.obj"
    source.write_text("map", encoding="utf-8")
    cache_dir = tmp_path / "staging-cache"

    def write_cell(_chunks_dir, _cell_str, _mesh, _groups):
        bounds = np.zeros(3, dtype=np.float32)
        return bounds, bounds.copy(), ["rock"]

    monkeypatch.setattr(
        chunker,
        "resolve_worker_allocation",
        lambda *_args, **_kwargs: WorkerAllocation(2, 2, 8, 2),
    )
    monkeypatch.setattr(
        chunker.hardware_memory,
        "detect_ram_snapshot",
        lambda: hardware_memory.RamSnapshot(100, 100),
    )
    monkeypatch.setattr(chunker, "_write_chunk_file", write_cell)

    with caplog.at_level(logging.INFO, logger="caveviewer"):
        chunker._build_cache_in_directory(
            str(source), _mesh_with_cells(4), {}, str(cache_dir)
        )

    assert "Detected system RAM for cache-build worker admission" in caplog.text
    assert "increasing workers to 2 of 2" in caplog.text


def test_chunk_writer_preserves_attributes_and_bounds(tmp_path):
    chunks_dir = tmp_path / chunker.CHUNKS_DIRNAME
    chunks_dir.mkdir()
    mesh = _attributed_mesh()

    bounds_min, bounds_max, materials = chunker._write_chunk_file(
        str(chunks_dir),
        "0_0_0",
        mesh,
        [
            ("rock", np.array([0], dtype=np.int64)),
            ("sand", np.array([1], dtype=np.int64)),
        ],
    )

    np.testing.assert_array_equal(bounds_min, [0.0, -1.0, 0.0])
    np.testing.assert_array_equal(bounds_max, [12.0, 2.0, 2.0])
    assert materials == ["rock", "sand"]

    loaded = chunker.load_chunk_file(str(tmp_path), (0, 0, 0))
    assert set(loaded.groups) == {"rock", "sand"}
    np.testing.assert_array_equal(loaded.bounds_min, bounds_min)
    np.testing.assert_array_equal(loaded.bounds_max, bounds_max)


def test_empty_chunk_group_round_trips_with_zero_bounds(tmp_path):
    chunks_dir = tmp_path / chunker.CHUNKS_DIRNAME
    chunks_dir.mkdir()

    bounds_min, bounds_max, _materials = chunker._write_chunk_file(
        str(chunks_dir),
        "0_0_0",
        _attributed_mesh(),
        [("empty", np.array([], dtype=np.int64))],
    )
    loaded = chunker.load_chunk_file(str(tmp_path), (0, 0, 0))

    np.testing.assert_array_equal(bounds_min, np.zeros(3, dtype=np.float32))
    np.testing.assert_array_equal(bounds_max, np.zeros(3, dtype=np.float32))
    np.testing.assert_array_equal(loaded.bounds_min, bounds_min)
    np.testing.assert_array_equal(loaded.bounds_max, bounds_max)
    assert loaded.groups["empty"].positions.shape == (0, 3)


def test_flat_normals_and_upload_payloads_handle_degenerate_and_empty_groups():
    positions = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [2.0, 2.0, 2.0],
            [2.0, 2.0, 2.0],
            [2.0, 2.0, 2.0],
        ],
        dtype=np.float32,
    )
    uvs = np.zeros((6, 2), dtype=np.float32)
    smooth_normals = np.tile(
        np.array([[0.0, 1.0, 0.0]], dtype=np.float32), (6, 1)
    )
    flat_normals = chunker.compute_flat_normals(positions)
    np.testing.assert_array_equal(flat_normals[:3], [[0.0, 0.0, 1.0]] * 3)
    np.testing.assert_array_equal(flat_normals[3:], np.zeros((3, 3)))

    data = chunker.ChunkData(
        cell=(0, 0, 0),
        groups={
            "rock": chunker.ChunkMaterialGroup(
                "rock", positions, uvs, smooth_normals
            ),
            "empty": chunker.ChunkMaterialGroup(
                "empty",
                np.empty((0, 3), dtype=np.float32),
                np.empty((0, 2), dtype=np.float32),
                np.empty((0, 3), dtype=np.float32),
            ),
        },
        bounds_min=np.zeros(3, dtype=np.float32),
        bounds_max=np.ones(3, dtype=np.float32),
    )

    result = chunker.prepare_chunk_upload_groups(data)

    assert result is data
    assert result.upload_groups is not None
    assert [group.material_name for group in result.upload_groups] == ["rock"]
    expected_bytes = len(positions) * 8 * np.dtype(np.float32).itemsize
    assert len(result.upload_groups[0].smooth_vertex_bytes) == expected_bytes
    assert len(result.upload_groups[0].flat_vertex_bytes) == expected_bytes


def test_prepare_chunk_upload_groups_defers_flat_payload(monkeypatch):
    calls = []
    positions = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        dtype=np.float32,
    )
    uvs = np.zeros((3, 2), dtype=np.float32)
    smooth_normals = np.tile(
        np.array([[0.0, 1.0, 0.0]], dtype=np.float32), (3, 1)
    )

    def fake_flat_normals(flat_pos):
        calls.append(len(flat_pos))
        return smooth_normals.copy()

    monkeypatch.setattr(chunker, "compute_flat_normals", fake_flat_normals)
    data = chunker.ChunkData(
        cell=(0, 0, 0),
        groups={
            "rock": chunker.ChunkMaterialGroup(
                "rock", positions, uvs, smooth_normals
            ),
        },
        bounds_min=np.zeros(3, dtype=np.float32),
        bounds_max=np.ones(3, dtype=np.float32),
    )

    chunker.prepare_chunk_upload_groups(data)

    assert calls == []
    assert data.upload_groups is not None
    _flat_bytes = data.upload_groups[0].flat_vertex_bytes
    assert calls == [3]


def test_prepack_chunk_vertex_bytes_reuses_requested_payload(monkeypatch):
    calls = []
    positions = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        dtype=np.float32,
    )
    uvs = np.zeros((3, 2), dtype=np.float32)
    smooth_normals = np.tile(
        np.array([[0.0, 1.0, 0.0]], dtype=np.float32), (3, 1)
    )

    def fake_flat_normals(flat_pos):
        calls.append(len(flat_pos))
        return smooth_normals.copy()

    monkeypatch.setattr(chunker, "compute_flat_normals", fake_flat_normals)
    data = chunker.ChunkData(
        cell=(0, 0, 0),
        groups={
            "rock": chunker.ChunkMaterialGroup(
                "rock", positions, uvs, smooth_normals
            ),
        },
        bounds_min=np.zeros(3, dtype=np.float32),
        bounds_max=np.ones(3, dtype=np.float32),
    )

    chunker.prepare_chunk_upload_groups(data)
    group = data.upload_groups[0]
    assert not group.has_prepacked_vertex_bytes(smooth_shading=False)

    chunker.prepack_chunk_vertex_bytes(data, smooth_shading=False)

    assert calls == [3]
    assert group.has_prepacked_vertex_bytes(smooth_shading=False)
    calls.clear()
    _flat_bytes = group.flat_vertex_bytes
    assert calls == []


def test_footprint_from_positions_matches_dense_unique_across_blocks():
    base_positions = np.array(
        [
            [-4.0, 0.0, -2.0],
            [-1.0, 0.0, 3.0],
            [2.0, 0.0, -2.0],
            [5.0, 0.0, 7.0],
        ],
        dtype=np.float32,
    )
    positions = np.tile(base_positions, (70_000, 1))

    cell_size, footprint_flat = chunker._footprint_from_positions(positions)

    fine_cx = np.floor(positions[:, 0] / cell_size).astype(np.int32)
    fine_cz = np.floor(positions[:, 2] / cell_size).astype(np.int32)
    expected = np.unique(np.column_stack([fine_cx, fine_cz]), axis=0)
    assert footprint_flat == expected.flatten().tolist()


def test_manifest_chunk_size_helpers_reject_bad_values_and_io_failures(monkeypatch):
    assert chunker.load_manifest(None) is None
    assert chunker.manifest_chunk_size(None) is None
    assert chunker.manifest_chunk_size({"chunk_size": "invalid"}) is None
    assert chunker.manifest_chunk_size({"chunk_size": 0}) is None
    assert chunker.manifest_chunk_size({"chunk_size": "4.5"}) == 4.5

    monkeypatch.setattr(
        chunker, "load_manifest", lambda _cache_dir: (_ for _ in ()).throw(OSError())
    )
    assert chunker.cache_chunk_size("unreadable") is None


def test_chunk_cache_metadata_requires_current_version_chunks_and_directory(tmp_path):
    assert not chunker._has_current_chunk_cache(str(tmp_path), {})
    assert not chunker._has_current_chunk_cache(
        str(tmp_path), {"version": chunker._VERSION, "chunks": []}
    )
    assert not chunker._has_current_chunk_cache(
        str(tmp_path), {"version": chunker._VERSION + 1, "chunks": {}}
    )
    assert not chunker._has_current_chunk_cache(str(tmp_path), _current_manifest())

    (tmp_path / chunker.CHUNKS_DIRNAME).mkdir()
    assert chunker._has_current_chunk_cache(str(tmp_path), _current_manifest())


def test_cache_validity_rejects_stale_corrupt_and_incomplete_caches(
    tmp_path, monkeypatch
):
    source = tmp_path / "map.obj"
    source.write_text("map", encoding="utf-8")
    monkeypatch.setenv("CAVEVIEWER_MAP_CACHE_DIR", str(tmp_path / "managed"))
    assert not chunker.cache_is_valid(str(source))

    cache_dir = Path(chunker.get_cache_dir(str(source)))
    manifest_path = _write_manifest(cache_dir, _current_manifest())
    os.utime(manifest_path, (100, 100))
    os.utime(source, (200, 200))
    assert not chunker.cache_is_valid(str(source))

    manifest_path.write_text("{broken", encoding="utf-8")
    os.utime(manifest_path, (300, 300))
    assert not chunker.cache_is_valid(str(source))

    manifest_path.write_text(json.dumps(_current_manifest()), encoding="utf-8")
    assert not chunker.cache_is_valid(str(source))


def test_cache_validity_ignores_old_adjacent_cache_directories(tmp_path, monkeypatch):
    source = tmp_path / "map.obj"
    source.write_text("map", encoding="utf-8")
    managed_root = tmp_path / "managed"
    monkeypatch.setenv("CAVEVIEWER_MAP_CACHE_DIR", str(managed_root))
    old_adjacent = tmp_path / "_cache"
    old_legacy = tmp_path / ".caveviewer_cache"
    manifest_path = _write_manifest(old_adjacent, _current_manifest())
    (old_adjacent / chunker.CHUNKS_DIRNAME).mkdir()
    legacy_manifest_path = _write_manifest(old_legacy, _current_manifest())
    (old_legacy / chunker.CHUNKS_DIRNAME).mkdir()
    os.utime(source, (100, 100))
    os.utime(manifest_path, (200, 200))
    os.utime(legacy_manifest_path, (200, 200))

    assert not chunker.cache_is_valid(str(source))
    assert Path(chunker.get_cache_dir(str(source))).parent == managed_root


def test_get_cache_dir_uses_managed_manifest_only(tmp_path, monkeypatch):
    source = tmp_path / "map.obj"
    source.touch()
    managed_root = tmp_path / "managed"
    monkeypatch.setenv("CAVEVIEWER_MAP_CACHE_DIR", str(managed_root))
    managed = Path(chunker.get_cache_dir(str(source)))
    old_adjacent = tmp_path / "_cache"
    old_legacy = tmp_path / ".caveviewer_cache"

    assert managed.parent == managed_root
    _write_manifest(old_adjacent, {})
    _write_manifest(old_legacy, {})
    assert chunker.get_cache_dir(str(source)) == str(managed)
    _write_manifest(managed, {})
    assert chunker.get_cache_dir(str(source)) == str(managed)


def test_landing_position_uses_nearest_level_in_exact_column():
    manifest = {
        "chunk_size": 10.0,
        "chunks": {
            "0_0_0": _chunk_info(0.0, 4.0),
            "0_2_0": _chunk_info(20.0, 30.0),
        },
    }

    assert chunker.find_landing_position(manifest, 2.0, 3.0, 24.0) == (
        2.0,
        25.0,
        3.0,
    )


def test_landing_position_expands_to_nearest_occupied_ring_column():
    manifest = {
        "chunk_size": 10.0,
        "chunks": {
            "1_0_0": _chunk_info(4.0, 8.0),
            "1_0_1": _chunk_info(20.0, 24.0),
        },
    }

    assert chunker.find_landing_position(manifest, 1.0, 1.0, 5.0) == (
        15.0,
        6.0,
        5.0,
    )


def test_landing_position_falls_back_to_closest_column_in_entire_map():
    manifest = {
        "chunk_size": 10.0,
        "chunks": {
            "5_0_-2": _chunk_info(10.0, 14.0),
            "8_0_8": _chunk_info(30.0, 34.0),
        },
    }

    assert chunker.find_landing_position(
        manifest, 1.0, 1.0, 11.0, search_radius_cells=1
    ) == (55.0, 12.0, -15.0)


def test_landing_position_keeps_requested_point_for_empty_manifest():
    manifest = {"chunk_size": 8.0, "chunks": {}}

    assert chunker.find_landing_position(manifest, 3.0, -4.0, 7.0) == (
        3.0,
        7.0,
        -4.0,
    )
