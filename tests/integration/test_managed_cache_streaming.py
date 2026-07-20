"""Build and stream a real chunk from a self-contained managed cache."""

from __future__ import annotations

import time

import numpy as np
import pytest

from caveviewer.core.mesh import obj as obj_parser
from caveviewer.core.chunking import builder as chunker
from caveviewer.core.streaming import world as streaming_world


@pytest.mark.integration
def test_managed_cache_build_is_consumed_by_runtime_streaming(tmp_path, monkeypatch):
    source = tmp_path / "source" / "map.obj"
    source.parent.mkdir()
    source.write_bytes(b"small map")
    cache_dir = tmp_path / "xdg-cache" / "map-key"
    mesh = obj_parser.RawMesh(
        positions=np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            dtype=np.float32,
        ),
        uvs=np.empty((0, 2), dtype=np.float32),
        normals=np.empty((0, 3), dtype=np.float32),
        face_pos_idx=np.array([[0, 1, 2]], dtype=np.int32),
        face_uv_idx=np.full((1, 3), -1, dtype=np.int32),
        face_nrm_idx=np.full((1, 3), -1, dtype=np.int32),
        material_ranges=[obj_parser.MaterialRange("rock", 0, 1)],
    )
    texture_bytes = b"texture"
    chunker.build_cache(
        str(source),
        mesh,
        {"rock": obj_parser.Material("rock", "rock.jpg")},
        cache_dir=str(cache_dir),
        assets=[chunker.CacheAsset(relative_path="rock.jpg", data=texture_bytes)],
    )

    monkeypatch.setenv("CAVEVIEWER_IO_WORKERS", "1")
    monkeypatch.setattr(streaming_world, "_detect_total_ram_bytes", lambda: 8 * 1024**3)
    monkeypatch.setattr(
        streaming_world, "_detect_total_gpu_memory_bytes", lambda _vendor=None: None
    )
    world = streaming_world.StreamingWorld(
        str(cache_dir),
        streaming_world.StreamingConfig(chunk_size=8.0, load_radius_cells=0),
    )
    ready = []
    try:
        world.update(np.array([0.1, 0.1, 0.1], dtype=np.float32))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not ready:
            world.drain_ready_chunks(
                lambda data: ready.append(data),
                lambda _cell: None,
                max_per_frame=1,
                time_budget_ms=100.0,
            )
            if not ready:
                time.sleep(0.01)
    finally:
        world.shutdown()

    assert [data.cell for data in ready] == [(0, 0, 0)]
    assert ready[0].groups["rock"].positions.shape == (3, 3)
    assert (cache_dir / "rock.jpg").read_bytes() == texture_bytes
