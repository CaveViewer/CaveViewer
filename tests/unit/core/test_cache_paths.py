"""Exercise managed cache selection and compatibility candidate ordering."""

from __future__ import annotations

from pathlib import Path

import pytest

from caveviewer.core.cache_paths import (
    CACHE_DIRNAME,
    LEGACY_CACHE_DIRNAME,
    MapCacheLocator,
    map_texture_dir,
)
from caveviewer.storage_paths import ApplicationPaths, StoragePathError


def _paths(root: Path) -> ApplicationPaths:
    return ApplicationPaths(
        config_dir=root / "config",
        data_dir=root / "data",
        cache_dir=root / "cache",
        state_dir=root / "state",
        runtime_dir=root / "runtime",
    )


def test_candidates_preserve_adjacent_and_legacy_before_managed(tmp_path):
    source = tmp_path / "maps" / "survey.obj"
    locator = MapCacheLocator(paths=_paths(tmp_path), environ={}, platform_name="linux")

    candidates = locator.candidates(source)

    assert candidates[0] == source.parent / CACHE_DIRNAME
    assert candidates[1] == source.parent / LEGACY_CACHE_DIRNAME
    assert candidates[2].parent == tmp_path / "cache" / "maps"
    assert locator.build_cache_dir(source) == candidates[2]


def test_managed_keys_are_stable_and_distinguish_same_named_sources(tmp_path):
    locator = MapCacheLocator(paths=_paths(tmp_path), environ={}, platform_name="linux")
    first = tmp_path / "one" / "map.obj"
    second = tmp_path / "two" / "map.obj"

    assert locator.managed_cache_dir(first) == locator.managed_cache_dir(first)
    assert locator.managed_cache_dir(first) != locator.managed_cache_dir(second)


def test_force_rebuild_can_replace_an_existing_adjacent_cache(tmp_path):
    source = tmp_path / "map.obj"
    adjacent = tmp_path / CACHE_DIRNAME
    adjacent.mkdir()
    (adjacent / "manifest.json").write_text("{}", encoding="utf-8")
    locator = MapCacheLocator(paths=_paths(tmp_path), environ={}, platform_name="linux")

    assert locator.build_cache_dir(source, prefer_existing=True) == adjacent


def test_explicit_managed_root_must_be_absolute(tmp_path):
    locator = MapCacheLocator(
        paths=_paths(tmp_path),
        environ={"CAVEVIEWER_MAP_CACHE_DIR": "relative"},
        platform_name="linux",
    )

    with pytest.raises(StoragePathError, match="must be an absolute path"):
        _ = locator.managed_root


def test_managed_cache_uses_self_contained_textures(tmp_path, monkeypatch):
    managed_root = tmp_path / "managed"
    monkeypatch.setenv("CAVEVIEWER_MAP_CACHE_DIR", str(managed_root))
    managed_cache = managed_root / "map-key"
    adjacent_cache = tmp_path / CACHE_DIRNAME

    assert map_texture_dir("map.obj", str(managed_cache), "/source/textures") == str(
        managed_cache
    )
    assert map_texture_dir("map.obj", str(adjacent_cache), "/source/textures") == (
        "/source/textures"
    )


def test_self_contained_adjacent_cache_uses_published_assets(tmp_path):
    adjacent_cache = tmp_path / CACHE_DIRNAME
    adjacent_cache.mkdir()
    (adjacent_cache / "embedded.png").write_bytes(b"png")
    (adjacent_cache / "manifest.json").write_text(
        '{"mtl_materials": {"rock": "embedded.png"}}', encoding="utf-8"
    )

    assert map_texture_dir(
        str(tmp_path / "map.glb"), str(adjacent_cache), str(tmp_path)
    ) == str(adjacent_cache)
