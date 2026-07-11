"""Verify import capacity checks and cleanup of partially built map caches."""

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


def test_space_check_uses_managed_cache_filesystem(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    managed_parent = tmp_path / "managed"
    source_dir.mkdir()
    managed_parent.mkdir()
    source = source_dir / "map.obj"
    source.write_bytes(b"x" * 100)
    observed = []
    monkeypatch.setattr(
        chunker.shutil,
        "disk_usage",
        lambda path: observed.append(path)
        or SimpleNamespace(total=1_000, used=0, free=1_000),
    )

    chunker.ensure_sufficient_disk_space(
        str(source), str(managed_parent / "map-key")
    )

    assert observed == [str(managed_parent)]


def test_capacity_check_includes_staged_texture_bytes(tmp_path, monkeypatch):
    source = tmp_path / "map.obj"
    source.write_bytes(b"x" * 100)
    texture = tmp_path / "rock.jpg"
    texture.write_bytes(b"t" * 50)
    _set_available_space(monkeypatch, 249)

    with pytest.raises(chunker.InsufficientDiskSpaceError) as raised:
        chunker.build_cache(
            str(source),
            _mesh_with_cells(),
            {},
            assets=[
                chunker.CacheAsset(
                    relative_path="rock.jpg", source_path=str(texture)
                )
            ],
        )

    assert raised.value.required_bytes == 250


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

    def write_until_disk_full(chunks_dir, cell_str, mesh, groups):
        nonlocal writes
        writes += 1
        if writes == 1:
            return original_write(chunks_dir, cell_str, mesh, groups)

        (Path(chunks_dir) / f"{cell_str}.bin").write_bytes(b"partial")
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
    assert {path.name for path in cache_path.iterdir()} == {
        chunker.CHUNKS_DIRNAME,
        chunker.MANIFEST_NAME,
    }
    assert not list(tmp_path.glob(f".{chunker.CACHE_DIRNAME}.tmp-*"))


def test_cache_assets_are_published_in_the_same_atomic_tree(tmp_path, monkeypatch):
    source = tmp_path / "map.obj"
    source.write_bytes(b"small map")
    texture = tmp_path / "textures" / "rock.jpg"
    texture.parent.mkdir()
    texture.write_bytes(b"rock texture")
    managed_cache = tmp_path / "managed" / "map-key"
    _set_available_space(monkeypatch, 10_000)

    result = chunker.build_cache(
        str(source),
        _mesh_with_cells(),
        {"rock": obj_parser.Material("rock", "tiles/rock.jpg")},
        cache_dir=str(managed_cache),
        assets=[
            chunker.CacheAsset(
                relative_path="tiles/rock.jpg", source_path=str(texture)
            ),
            chunker.CacheAsset(relative_path="embedded.png", data=b"png"),
        ],
    )

    assert result == str(managed_cache)
    assert (managed_cache / "tiles" / "rock.jpg").read_bytes() == b"rock texture"
    assert (managed_cache / "embedded.png").read_bytes() == b"png"
    assert (managed_cache / chunker.MANIFEST_NAME).is_file()
    assert not list(managed_cache.parent.glob(".map-key.tmp-*"))


def test_asset_failure_preserves_previous_cache_and_cleans_staging(
    tmp_path, monkeypatch
):
    source = tmp_path / "map.obj"
    source.write_bytes(b"small map")
    managed_cache = tmp_path / "managed" / "map-key"
    managed_cache.mkdir(parents=True)
    marker = managed_cache / "existing-cache"
    marker.write_text("keep", encoding="utf-8")
    _set_available_space(monkeypatch, 10_000)

    with pytest.raises(FileNotFoundError):
        chunker.build_cache(
            str(source),
            _mesh_with_cells(),
            {},
            cache_dir=str(managed_cache),
            assets=[
                chunker.CacheAsset(
                    relative_path="missing.jpg",
                    source_path=str(tmp_path / "missing.jpg"),
                )
            ],
        )

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not list(managed_cache.parent.glob(".map-key.tmp-*"))


def test_cache_asset_rejects_parent_traversal(tmp_path, monkeypatch):
    source = tmp_path / "map.obj"
    source.write_bytes(b"small map")
    managed_cache = tmp_path / "managed" / "map-key"
    _set_available_space(monkeypatch, 10_000)

    with pytest.raises(ValueError, match="Unsafe cache asset path"):
        chunker.build_cache(
            str(source),
            _mesh_with_cells(),
            {},
            cache_dir=str(managed_cache),
            assets=[chunker.CacheAsset(relative_path="../escape", data=b"bad")],
        )

    assert not (tmp_path / "escape").exists()
    assert not list(managed_cache.parent.glob(".map-key.tmp-*"))


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
