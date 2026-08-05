"""Cooperative ownership for one generated-cache build target.

The generated cache directory itself is atomically replaced at publication, so
the lock lives beside it rather than inside it.  This lets import callers keep
the current cache usable while a replacement is staged and prevents two
independent import processes from publishing to the same target concurrently.
"""

from __future__ import annotations

import os
from pathlib import Path

from caveviewer.core.diagnostics.logging import get_logger


_LOG = get_logger("CacheBuildLock")


class CacheBuildInProgressError(RuntimeError):
    """Raised when another cooperative builder already owns a cache target."""

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        self.cache_dir = os.path.abspath(os.fspath(cache_dir))
        self.lock_dir = cache_build_lock_path(self.cache_dir)
        super().__init__(
            "A cache build is already in progress for "
            f"{self.cache_dir}."
        )


def cache_build_lock_path(cache_dir: str | os.PathLike[str]) -> Path:
    """Return the private sibling directory used to lock ``cache_dir``."""
    target = Path(os.path.abspath(os.fspath(cache_dir)))
    return target.with_name(f".{target.name}.build-lock")


def cache_build_is_locked(cache_dir: str | os.PathLike[str]) -> bool:
    """Return whether a cooperative build currently owns ``cache_dir``.

    ``lstat`` deliberately treats a broken or unexpected symlink as occupied:
    callers must fail closed rather than risk racing an unknown filesystem
    object.
    """
    try:
        os.lstat(cache_build_lock_path(cache_dir))
    except FileNotFoundError:
        return False
    return True


class CacheBuildLock:
    """An atomic, process-safe, cooperative cache-build lock.

    The lock directory remains empty and is removed only by the process that
    created it.  A crash can leave it behind; that conservative outcome blocks
    a later rebuild rather than permitting two uncertain writers to race.
    """

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        self.cache_dir = os.path.abspath(os.fspath(cache_dir))
        self.lock_dir = cache_build_lock_path(self.cache_dir)
        self._owned = False

    def acquire(self) -> None:
        """Atomically acquire the lock or report the existing owner."""
        try:
            os.mkdir(self.lock_dir)
        except FileExistsError as exc:
            raise CacheBuildInProgressError(self.cache_dir) from exc
        self._owned = True

    def release(self) -> None:
        """Release a lock acquired by this instance without deleting strangers."""
        if not self._owned:
            return
        try:
            os.rmdir(self.lock_dir)
        except FileNotFoundError:
            self._owned = False
        except OSError as exc:
            _LOG.warning("Could not release cache build lock %s: %s", self.lock_dir, exc)
        else:
            self._owned = False

    def __enter__(self) -> "CacheBuildLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.release()
        return False
