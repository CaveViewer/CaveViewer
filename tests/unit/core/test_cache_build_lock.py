"""Exercise cooperative ownership of generated-cache build targets."""

from __future__ import annotations

import pytest

from caveviewer.core.chunking import builder as chunker
from caveviewer.core.map.cache_build_lock import (
    CacheBuildInProgressError,
    CacheBuildLock,
    cache_build_is_locked,
    cache_build_lock_path,
)


def test_cache_build_lock_excludes_a_second_owner_and_releases_cleanly(tmp_path):
    cache_dir = tmp_path / "_cache"
    cache_dir.mkdir()
    first = CacheBuildLock(cache_dir)

    first.acquire()

    assert cache_build_is_locked(cache_dir)
    assert cache_build_lock_path(cache_dir).is_dir()
    with pytest.raises(CacheBuildInProgressError):
        CacheBuildLock(cache_dir).acquire()

    first.release()

    assert not cache_build_is_locked(cache_dir)
    with CacheBuildLock(cache_dir):
        assert cache_build_is_locked(cache_dir)
    assert not cache_build_is_locked(cache_dir)


def test_builder_refuses_a_competing_cache_build_without_touching_current_cache(
    tmp_path,
):
    source = tmp_path / "map.obj"
    source.write_text("v 0 0 0\n", encoding="utf-8")
    cache_dir = tmp_path / "_cache"
    cache_dir.mkdir()
    marker = cache_dir / "current-cache-marker"
    marker.write_text("keep", encoding="utf-8")
    lock = CacheBuildLock(cache_dir)
    lock.acquire()

    try:
        with pytest.raises(CacheBuildInProgressError):
            chunker.build_cache(
                str(source),
                None,
                {},
                cache_dir=str(cache_dir),
            )
    finally:
        lock.release()

    assert marker.read_text(encoding="utf-8") == "keep"
