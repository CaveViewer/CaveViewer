"""Exercise managed cache selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from caveviewer.core.map.cache_paths import (
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


def test_candidates_use_only_managed_cache_dir(tmp_path):
    source = tmp_path / "maps" / "survey.obj"
    locator = MapCacheLocator(paths=_paths(tmp_path), environ={}, platform_name="linux")

    candidates = locator.candidates(source)

    assert candidates == (locator.managed_cache_dir(source),)
    assert candidates[0].parent == tmp_path / "cache" / "maps"
    assert locator.build_cache_dir(source) == candidates[0]


def test_default_managed_root_uses_application_cache_dir(tmp_path):
    locator = MapCacheLocator(paths=_paths(tmp_path), environ={}, platform_name="linux")

    assert locator.managed_root == tmp_path / "cache" / "maps"


def test_managed_keys_are_stable_and_distinguish_same_named_sources(tmp_path):
    locator = MapCacheLocator(paths=_paths(tmp_path), environ={}, platform_name="linux")
    first = tmp_path / "one" / "map.obj"
    second = tmp_path / "two" / "map.obj"

    assert locator.managed_cache_dir(first) == locator.managed_cache_dir(first)
    assert locator.managed_cache_dir(first) != locator.managed_cache_dir(second)


def test_old_adjacent_cache_does_not_change_managed_cache_target(tmp_path):
    source = tmp_path / "map.obj"
    old_adjacent = tmp_path / "_cache"
    old_adjacent.mkdir()
    (old_adjacent / "manifest.json").write_text("{}", encoding="utf-8")
    locator = MapCacheLocator(paths=_paths(tmp_path), environ={}, platform_name="linux")

    assert locator.build_cache_dir(source) == locator.managed_cache_dir(source)


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
    external_cache = tmp_path / "external-cache"

    assert map_texture_dir("map.obj", str(managed_cache), "/source/textures") == str(
        managed_cache
    )
    assert map_texture_dir("map.obj", str(external_cache), "/source/textures") == (
        str(external_cache)
    )


def test_selected_external_cache_uses_its_own_assets(tmp_path):
    external_cache = tmp_path / "external-cache"
    external_cache.mkdir()
    (external_cache / "embedded.png").write_bytes(b"png")
    (external_cache / "manifest.json").write_text(
        '{"mtl_materials": {"rock": "embedded.png"}}', encoding="utf-8"
    )

    assert map_texture_dir(
        str(tmp_path / "map.glb"), str(external_cache), str(tmp_path)
    ) == str(external_cache)
