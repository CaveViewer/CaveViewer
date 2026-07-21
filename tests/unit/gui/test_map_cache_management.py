"""Cover splash-library managed cache removal helpers."""

from __future__ import annotations

from caveviewer.core.map.cache_paths import MapCacheLocator
from caveviewer.core.chunking import builder as chunker
from caveviewer.gui import map_cache_management


def test_remove_managed_map_cache_deletes_generated_cache_only(tmp_path, monkeypatch):
    cache_root = tmp_path / "cache-root"
    monkeypatch.setenv("CAVEVIEWER_MAP_CACHE_DIR", str(cache_root))

    map_dir = tmp_path / "maps" / "cave"
    map_dir.mkdir(parents=True)
    source = map_dir / "cave.glb"
    source.write_bytes(b"glTF")

    cache_dir = MapCacheLocator().managed_cache_dir(source)
    (cache_dir / "chunks").mkdir(parents=True)
    (cache_dir / chunker.MANIFEST_NAME).write_text("{}", encoding="utf-8")

    result = map_cache_management.remove_managed_map_cache(str(map_dir))

    assert result.removed
    assert result.cache_dir == str(cache_dir)
    assert result.error is None
    assert not cache_dir.exists()
    assert source.exists()
    assert map_dir.exists()


def test_remove_managed_map_cache_ignores_precompiled_cache_folder(tmp_path):
    precompiled = tmp_path / "precompiled-cache"
    precompiled.mkdir()
    (precompiled / chunker.MANIFEST_NAME).write_text("{}", encoding="utf-8")

    result = map_cache_management.remove_managed_map_cache(str(precompiled))

    assert not result.removed
    assert result.cache_dir is None
    assert result.error is None
    assert precompiled.exists()
    assert (precompiled / chunker.MANIFEST_NAME).exists()


def test_has_managed_map_cache_reports_existing_cache(tmp_path, monkeypatch):
    cache_root = tmp_path / "cache-root"
    monkeypatch.setenv("CAVEVIEWER_MAP_CACHE_DIR", str(cache_root))

    map_dir = tmp_path / "maps" / "cave"
    map_dir.mkdir(parents=True)
    source = map_dir / "cave.glb"
    source.write_bytes(b"glTF")

    assert not map_cache_management.has_managed_map_cache(str(map_dir))

    cache_dir = MapCacheLocator().managed_cache_dir(source)
    cache_dir.mkdir(parents=True)

    assert map_cache_management.has_managed_map_cache(str(map_dir))
