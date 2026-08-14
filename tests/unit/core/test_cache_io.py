"""Validate binary chunk-cache serialization boundaries."""

from __future__ import annotations

import json
import struct

import numpy as np
import pytest

from caveviewer.core.chunking import builder as chunker
from caveviewer.core.chunking import metadata as chunk_metadata
from caveviewer.core.chunking.io import ChunkFileWriter, iter_chunk_file_groups


CELL = (1, -2, 3)


def _chunk_path(cache_dir, cell=CELL):
    path = cache_dir / chunker.CHUNKS_DIRNAME / f"{cell[0]}_{cell[1]}_{cell[2]}.bin"
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


def test_load_manifest_rejects_oversized_json(tmp_path, monkeypatch):
    monkeypatch.setattr(chunk_metadata, "MAX_CACHE_MANIFEST_BYTES", 8)
    (tmp_path / chunker.MANIFEST_NAME).write_text(
        json.dumps({"chunk_size": 8.0, "chunks": {}}),
        encoding="utf-8",
    )

    assert chunker.load_manifest(str(tmp_path)) is None
    assert chunker.cache_chunk_size(str(tmp_path)) is None


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


def test_streamed_chunk_groups_stay_within_requested_triangle_blocks(tmp_path):
    positions = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
            [1.0, 0.0, 1.0],
            [0.0, 1.0, 1.0],
        ],
        dtype=np.float32,
    )
    writer = ChunkFileWriter(str(tmp_path / chunker.CHUNKS_DIRNAME), CELL)
    writer.write_group(
        "rock",
        positions,
        np.zeros((6, 2), dtype=np.float32),
        np.tile(np.array([[0.0, 0.0, 1.0]], dtype=np.float32), (6, 1)),
    )
    assert writer.finish() is not None

    blocks = list(iter_chunk_file_groups(str(tmp_path), CELL, block_vertices=3))

    assert [block.material_name for block in blocks] == ["rock", "rock"]
    assert [len(block.positions) for block in blocks] == [3, 3]
    assert all(len(block.positions) % 3 == 0 for block in blocks)
