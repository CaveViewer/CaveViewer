"""GUI-facing helpers for removing managed map caches."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from caveviewer.core.map.cache_paths import MapCacheLocator
from caveviewer.core.map.source_model import find_model_file


@dataclass(frozen=True)
class CacheRemovalResult:
    """Result of a scoped managed-cache removal request."""

    cache_dir: str | None
    removed: bool
    error: str | None = None


def managed_cache_dir_for_map_path(path: str | os.PathLike[str]) -> Path | None:
    """Return the generated managed-cache path for a map folder or source file."""
    try:
        descriptor = find_model_file(os.fspath(path))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None

    source_path = descriptor.get("obj_path") or descriptor.get("glb_path")
    if not source_path:
        return None

    locator = MapCacheLocator()
    cache_dir = locator.managed_cache_dir(source_path)
    if not locator.is_managed(cache_dir):
        return None
    return cache_dir


def existing_managed_cache_dir_for_map_path(
    path: str | os.PathLike[str],
) -> Path | None:
    """Return an existing generated cache directory for ``path``, if present."""
    cache_dir = managed_cache_dir_for_map_path(path)
    if cache_dir is None:
        return None
    try:
        if cache_dir.is_dir() and not cache_dir.is_symlink():
            return cache_dir
    except OSError:
        return None
    return None


def has_managed_map_cache(path: str | os.PathLike[str]) -> bool:
    """Return whether ``path`` currently has a removable generated cache."""
    return existing_managed_cache_dir_for_map_path(path) is not None


def remove_managed_map_cache(path: str | os.PathLike[str]) -> CacheRemovalResult:
    """Remove only CaveViewer's generated managed cache for ``path``."""
    cache_dir = managed_cache_dir_for_map_path(path)
    if cache_dir is None:
        return CacheRemovalResult(cache_dir=None, removed=False)

    cache_dir_text = str(cache_dir)
    try:
        if not cache_dir.exists():
            return CacheRemovalResult(cache_dir=cache_dir_text, removed=False)
        if cache_dir.is_symlink() or not cache_dir.is_dir():
            return CacheRemovalResult(
                cache_dir=cache_dir_text,
                removed=False,
                error="The cache path is not a removable directory.",
            )
        shutil.rmtree(cache_dir)
    except FileNotFoundError:
        return CacheRemovalResult(cache_dir=cache_dir_text, removed=False)
    except OSError as exc:
        return CacheRemovalResult(
            cache_dir=cache_dir_text,
            removed=False,
            error=str(exc),
        )
    return CacheRemovalResult(cache_dir=cache_dir_text, removed=True)
