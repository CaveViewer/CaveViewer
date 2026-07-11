from __future__ import annotations

import errno
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from caveviewer import app as caveviewer
from caveviewer.core import chunker, obj_parser


def _mesh_with_cells(cell_count: int = 2) -> obj_parser.RawMesh:
    positions = []
    faces = []
    for cell_index in range(cell_count):
        x = float(cell_index * 20)
        first_vertex = len(positions)
        positions.extend(
            [
                [x, 0.0, 0.0],
                [x + 1.0, 0.0, 0.0],
                [x, 1.0, 0.0],
            ]
        )
        faces.append([first_vertex, first_vertex + 1, first_vertex + 2])

    return obj_parser.RawMesh(
        positions=np.array(positions, dtype=np.float32),
        uvs=np.empty((0, 2), dtype=np.float32),
        normals=np.empty((0, 3), dtype=np.float32),
        face_pos_idx=np.array(faces, dtype=np.int32),
        face_uv_idx=np.full((cell_count, 3), -1, dtype=np.int32),
        face_nrm_idx=np.full((cell_count, 3), -1, dtype=np.int32),
        material_ranges=[obj_parser.MaterialRange("rock", 0, cell_count)],
    )


def _set_available_space(monkeypatch, free_bytes: int) -> None:
    monkeypatch.setattr(
        chunker.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=free_bytes, used=0, free=free_bytes),
    )


def test_import_is_rejected_when_free_space_is_below_twice_map_size(
    tmp_path, monkeypatch
):
    source = tmp_path / "map.obj"
    source.write_bytes(b"x" * 100)
    _set_available_space(monkeypatch, 199)

    with pytest.raises(chunker.InsufficientDiskSpaceError) as raised:
        chunker.build_cache(str(source), _mesh_with_cells(), {})

    assert raised.value.errno == errno.ENOSPC
    assert raised.value.required_bytes == 200
    assert raised.value.available_bytes == 199
    assert not (tmp_path / chunker.CACHE_DIRNAME).exists()
    assert not list(tmp_path.glob(f".{chunker.CACHE_DIRNAME}.tmp-*"))


def test_exactly_twice_map_size_passes_disk_space_check(tmp_path, monkeypatch):
    source = tmp_path / "map.obj"
    source.write_bytes(b"x" * 100)
    _set_available_space(monkeypatch, 200)

    chunker.ensure_sufficient_disk_space(str(source))


def test_obj_import_checks_space_before_parsing(tmp_path, monkeypatch):
    source = tmp_path / "map.obj"
    material_file = tmp_path / "map.mtl"
    source.write_bytes(b"x" * 100)
    material_file.write_text("newmtl rock", encoding="utf-8")
    _set_available_space(monkeypatch, 199)
    monkeypatch.setattr(
        obj_parser,
        "parse_obj",
        lambda *_args, **_kwargs: pytest.fail("parsing must not start"),
    )

    with pytest.raises(chunker.InsufficientDiskSpaceError):
        caveviewer.import_and_cache(str(source), str(material_file))


def test_glb_import_checks_space_before_parsing(tmp_path, monkeypatch):
    source = tmp_path / "map.glb"
    source.write_bytes(b"x" * 100)
    _set_available_space(monkeypatch, 199)

    with pytest.raises(chunker.InsufficientDiskSpaceError):
        caveviewer.import_and_cache_any(
            {"format": "glb", "glb_path": str(source)}, str(tmp_path)
        )


def test_disk_full_mid_build_removes_every_partial_cache_file(
    tmp_path, monkeypatch
):
    source = tmp_path / "map.obj"
    source.write_bytes(b"small map")
    _set_available_space(monkeypatch, 10_000)
    original_write = chunker._write_chunk_file
    writes = 0

    def write_until_disk_full(chunks_dir, cross_section_dir, cell_str, mesh, groups):
        nonlocal writes
        writes += 1
        if writes == 1:
            return original_write(
                chunks_dir, cross_section_dir, cell_str, mesh, groups
            )

        (Path(chunks_dir) / f"{cell_str}.bin").write_bytes(b"partial")
        (Path(cross_section_dir) / f"{cell_str}.bin").write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(chunker, "_write_chunk_file", write_until_disk_full)

    with pytest.raises(OSError) as raised:
        chunker.build_cache(str(source), _mesh_with_cells(3), {})

    assert raised.value.errno == errno.ENOSPC
    assert writes == 2
    assert not (tmp_path / chunker.CACHE_DIRNAME).exists()
    assert not list(tmp_path.glob(f".{chunker.CACHE_DIRNAME}.tmp-*"))


def test_successful_build_publishes_complete_cache_without_staging_files(
    tmp_path, monkeypatch
):
    source = tmp_path / "map.obj"
    source.write_bytes(b"small map")
    _set_available_space(monkeypatch, 10_000)

    cache_dir = chunker.build_cache(str(source), _mesh_with_cells(), {})

    cache_path = tmp_path / chunker.CACHE_DIRNAME
    assert cache_dir == str(cache_path)
    assert (cache_path / chunker.MANIFEST_NAME).is_file()
    assert len(list((cache_path / chunker.CHUNKS_DIRNAME).glob("*.bin"))) == 2
    assert len(
        list((cache_path / chunker.CROSS_SECTION_DIRNAME).glob("*.bin"))
    ) == 2
    assert not list(tmp_path.glob(f".{chunker.CACHE_DIRNAME}.tmp-*"))


def test_disk_full_during_rebuild_preserves_previous_cache(tmp_path, monkeypatch):
    source = tmp_path / "map.obj"
    source.write_bytes(b"small map")
    _set_available_space(monkeypatch, 10_000)
    cache_path = tmp_path / chunker.CACHE_DIRNAME
    cache_path.mkdir()
    marker = cache_path / "existing-cache"
    marker.write_text("keep me", encoding="utf-8")

    def fail_first_write(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(chunker, "_write_chunk_file", fail_first_write)

    with pytest.raises(OSError) as raised:
        chunker.build_cache(str(source), _mesh_with_cells(), {})

    assert raised.value.errno == errno.ENOSPC
    assert marker.read_text(encoding="utf-8") == "keep me"
    assert not list(tmp_path.glob(f".{chunker.CACHE_DIRNAME}.tmp-*"))
