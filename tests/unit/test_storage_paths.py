"""Exercise XDG path resolution and the explicit portable-home override."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from caveviewer import storage_paths
from caveviewer.storage_paths import (
    STORAGE_HOME_ENV_VAR,
    StoragePathError,
    resolve_application_paths,
)


def _expected_runtime_fallback_dir() -> Path:
    getuid = getattr(os, "getuid", None)
    suffix = os.getpid() if getuid is None else getuid()
    return Path(tempfile.gettempdir()) / f"caveviewer-{suffix}" / "caveviewer"


def test_linux_paths_follow_absolute_xdg_roots(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    runtime_root.chmod(0o700)
    monkeypatch.setattr(
        storage_paths,
        "_is_valid_xdg_runtime_dir",
        lambda path: path == runtime_root,
    )
    environment = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(runtime_root),
    }

    paths = resolve_application_paths(
        environ=environment, home=tmp_path / "home", platform_name="linux"
    )

    assert paths.config_dir == tmp_path / "config" / "caveviewer"
    assert paths.data_dir == tmp_path / "data" / "caveviewer"
    assert paths.cache_dir == tmp_path / "cache" / "caveviewer"
    assert paths.state_dir == tmp_path / "state" / "caveviewer"
    assert paths.runtime_dir == runtime_root / "caveviewer"


def test_relative_xdg_values_are_ignored(tmp_path):
    paths = resolve_application_paths(
        environ={
            "XDG_CONFIG_HOME": "relative-config",
            "XDG_DATA_HOME": "relative-data",
            "XDG_CACHE_HOME": "relative-cache",
            "XDG_STATE_HOME": "relative-state",
            "XDG_RUNTIME_DIR": "relative-runtime",
        },
        home=tmp_path / "home",
        platform_name="linux",
    )

    assert paths.config_dir == tmp_path / "home" / ".config" / "caveviewer"
    assert paths.data_dir == tmp_path / "home" / ".local" / "share" / "caveviewer"
    assert paths.cache_dir == tmp_path / "home" / ".cache" / "caveviewer"
    assert paths.state_dir == tmp_path / "home" / ".local" / "state" / "caveviewer"
    assert paths.runtime_dir == _expected_runtime_fallback_dir()


def test_invalid_xdg_runtime_dir_is_ignored(tmp_path):
    missing_runtime = tmp_path / "missing-runtime"

    paths = resolve_application_paths(
        environ={"XDG_RUNTIME_DIR": str(missing_runtime)},
        home=tmp_path / "home",
        platform_name="linux",
    )

    assert paths.runtime_dir == _expected_runtime_fallback_dir()


def test_group_or_world_accessible_xdg_runtime_dir_is_ignored(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    runtime_root.chmod(0o755)

    paths = resolve_application_paths(
        environ={"XDG_RUNTIME_DIR": str(runtime_root)},
        home=tmp_path / "home",
        platform_name="linux",
    )

    assert paths.runtime_dir == _expected_runtime_fallback_dir()


def test_caveviewer_home_derives_isolated_category_directories(tmp_path):
    storage_root = tmp_path / "portable"

    paths = resolve_application_paths(
        environ={STORAGE_HOME_ENV_VAR: str(storage_root)},
        home="relative-home-is-ignored-when-portable-home-is-set",
        platform_name="linux",
    )
    paths.ensure_persistent_directories()

    assert paths.config_dir == storage_root / "config"
    assert paths.data_dir == storage_root / "data"
    assert paths.cache_dir == storage_root / "cache"
    assert paths.state_dir == storage_root / "state"
    assert all(path.is_dir() for path in (
        paths.config_dir, paths.data_dir, paths.cache_dir, paths.state_dir
    ))


def test_caveviewer_home_must_be_absolute(tmp_path):
    with pytest.raises(StoragePathError, match="must be an absolute path"):
        resolve_application_paths(
            environ={STORAGE_HOME_ENV_VAR: "relative"},
            home=tmp_path / "home",
            platform_name="linux",
        )


def test_home_fallback_must_resolve_to_absolute_path():
    with pytest.raises(StoragePathError, match="home directory"):
        resolve_application_paths(environ={}, home="relative-home", platform_name="linux")


def test_non_linux_platforms_preserve_legacy_root(tmp_path):
    paths = resolve_application_paths(
        environ={}, home=tmp_path / "home", platform_name="darwin"
    )

    legacy_root = tmp_path / "home" / ".caveviewer"
    assert paths.config_dir == legacy_root
    assert paths.data_dir == legacy_root
    assert paths.cache_dir == legacy_root
    assert paths.state_dir == legacy_root
