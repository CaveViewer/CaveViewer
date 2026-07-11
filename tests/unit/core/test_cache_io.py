from __future__ import annotations

import json
import struct

import numpy as np
import pytest

from caveviewer.core import chunker


CELL = (1, -2, 3)


def _chunk_path(cache_dir, cell=CELL):
    path = cache_dir / chunker.CHUNKS_DIRNAME / f"{cell[0]}_{cell[1]}_{cell[2]}.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _cross_section_path(cache_dir, cell=CELL):
    path = (
        cache_dir
        / chunker.CROSS_SECTION_DIRNAME
        / f"{cell[0]}_{cell[1]}_{cell[2]}.bin"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _valid_chunk_blob() -> bytes:
    name = b"rock"
    positions = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        dtype=np.float32,
    )
    uvs = np.zeros((3, 2), dtype=np.float32)
    normals = np.tile(np.array([[0.0, 0.0, 1.0]], dtype=np.float32), (3, 1))
    return b"".join(
        [
            chunker._MAGIC,
            struct.pack("<I", chunker._VERSION),
            struct.pack("<I", 1),
            struct.pack("<I", len(name)),
            name,
            struct.pack("<I", 3),
            positions.tobytes(),
            uvs.tobytes(),
            normals.tobytes(),
        ]
    )


def _valid_cross_section_blob() -> bytes:
    positions = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        dtype=np.float32,
    )
    return b"".join(
        [
            chunker._CROSS_SECTION_MAGIC,
            struct.pack("<I", chunker._CROSS_SECTION_VERSION),
            struct.pack("<I", 3),
            positions.tobytes(),
        ]
    )


def test_load_manifest_returns_none_when_missing(tmp_path):
    assert chunker.load_manifest(str(tmp_path)) is None


@pytest.mark.parametrize("content", ["{broken", "[]", "null"])
def test_load_manifest_rejects_corrupt_or_non_object_json(tmp_path, content):
    (tmp_path / chunker.MANIFEST_NAME).write_text(content, encoding="utf-8")
    assert chunker.load_manifest(str(tmp_path)) is None
    assert chunker.cache_chunk_size(str(tmp_path)) is None


def test_load_manifest_reads_valid_object(tmp_path):
    expected = {"chunk_size": 8.0, "chunks": {}}
    (tmp_path / chunker.MANIFEST_NAME).write_text(
        json.dumps(expected), encoding="utf-8"
    )
    assert chunker.load_manifest(str(tmp_path)) == expected
    assert chunker.cache_chunk_size(str(tmp_path)) == 8.0


def test_load_chunk_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        chunker.load_chunk_file(str(tmp_path), CELL)


def test_load_chunk_rejects_bad_magic(tmp_path):
    _chunk_path(tmp_path).write_bytes(
        b"BAD!" + struct.pack("<II", chunker._VERSION, 0)
    )
    with pytest.raises(ValueError, match="Bad chunk file magic"):
        chunker.load_chunk_file(str(tmp_path), CELL)


def test_load_chunk_rejects_unsupported_version(tmp_path):
    _chunk_path(tmp_path).write_bytes(
        chunker._MAGIC + struct.pack("<II", chunker._VERSION + 1, 0)
    )
    with pytest.raises(ValueError, match="Unsupported chunk version"):
        chunker.load_chunk_file(str(tmp_path), CELL)


@pytest.mark.parametrize("blob", [b"", chunker._MAGIC, _valid_chunk_blob()[:-1]])
def test_load_chunk_rejects_truncated_data(tmp_path, blob):
    _chunk_path(tmp_path).write_bytes(blob)
    with pytest.raises(ValueError, match="Truncated chunk file"):
        chunker.load_chunk_file(str(tmp_path), CELL)


def test_load_chunk_reads_valid_binary(tmp_path):
    _chunk_path(tmp_path).write_bytes(_valid_chunk_blob())
    data = chunker.load_chunk_file(str(tmp_path), CELL)
    assert data.cell == CELL
    assert list(data.groups) == ["rock"]
    assert data.groups["rock"].positions.shape == (3, 3)


def test_cross_section_loader_returns_none_when_file_missing(tmp_path):
    assert chunker.load_cross_section_triangles(str(tmp_path), CELL) is None


def test_cross_section_loader_rejects_truncated_header(tmp_path):
    _cross_section_path(tmp_path).write_bytes(chunker._CROSS_SECTION_MAGIC)
    with pytest.raises(ValueError, match="Truncated cross-section triangle header"):
        chunker.load_cross_section_triangles(str(tmp_path), CELL)


def test_cross_section_loader_rejects_bad_magic(tmp_path):
    _cross_section_path(tmp_path).write_bytes(
        b"BAD!" + struct.pack("<II", chunker._CROSS_SECTION_VERSION, 0)
    )
    with pytest.raises(ValueError, match="Bad cross-section file magic"):
        chunker.load_cross_section_triangles(str(tmp_path), CELL)


def test_cross_section_loader_rejects_unsupported_version(tmp_path):
    _cross_section_path(tmp_path).write_bytes(
        chunker._CROSS_SECTION_MAGIC
        + struct.pack("<II", chunker._CROSS_SECTION_VERSION + 1, 0)
    )
    with pytest.raises(ValueError, match="Unsupported cross-section version"):
        chunker.load_cross_section_triangles(str(tmp_path), CELL)


def test_cross_section_loader_rejects_non_triangle_vertex_count(tmp_path):
    _cross_section_path(tmp_path).write_bytes(
        chunker._CROSS_SECTION_MAGIC
        + struct.pack("<II", chunker._CROSS_SECTION_VERSION, 2)
        + np.zeros((2, 3), dtype=np.float32).tobytes()
    )
    with pytest.raises(ValueError, match="non-triangle vertex count"):
        chunker.load_cross_section_triangles(str(tmp_path), CELL)


def test_cross_section_loader_rejects_truncated_payload(tmp_path):
    _cross_section_path(tmp_path).write_bytes(_valid_cross_section_blob()[:-1])
    with pytest.raises(ValueError, match="Truncated cross-section triangle file"):
        chunker.load_cross_section_triangles(str(tmp_path), CELL)


def test_cross_section_loader_reads_valid_binary(tmp_path):
    _cross_section_path(tmp_path).write_bytes(_valid_cross_section_blob())
    triangles = chunker.load_cross_section_triangles(str(tmp_path), CELL)
    assert triangles is not None
    assert triangles.shape == (1, 3, 3)
