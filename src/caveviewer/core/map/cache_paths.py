"""Select map-cache locations."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Mapping

from caveviewer.storage_paths import (
    ApplicationPaths,
    StoragePathError,
    resolve_application_paths,
)


MANAGED_CACHE_ENV_VAR = "CAVEVIEWER_MAP_CACHE_DIR"
ADJACENT_CACHE_DIRNAME = "_cache"


class MapCacheLocator:
    """Resolve generated cache locations without depending on cache-format policy."""

    def __init__(
        self,
        *,
        paths: ApplicationPaths | None = None,
        environ: Mapping[str, str] | None = None,
        platform_name: str | None = None,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._platform_name = sys.platform if platform_name is None else platform_name
        self._paths = paths or resolve_application_paths(
            environ=self._environ, platform_name=self._platform_name
        )

    @property
    def managed_root(self) -> Path:
        """Return the explicit hashed-cache root, or the legacy app-cache root.

        New default GUI/import behavior uses an adjacent ``_cache`` directory.
        This property remains for explicit ``CAVEVIEWER_MAP_CACHE_DIR`` and CLI
        ``--cache-root`` callers that still need one parent containing hashed
        cache directories.
        """
        configured = self._environ.get(MANAGED_CACHE_ENV_VAR, "").strip()
        if configured:
            expanded = os.path.expanduser(configured)
            if not os.path.isabs(expanded):
                raise StoragePathError(
                    f"{MANAGED_CACHE_ENV_VAR} must be an absolute path: "
                    f"{configured!r}"
                )
            return Path(expanded)
        return self._paths.cache_dir / "maps"

    def configured_managed_root(self) -> Path | None:
        """Return the configured managed root, if the user explicitly set one."""
        configured = self._environ.get(MANAGED_CACHE_ENV_VAR, "").strip()
        if not configured:
            return None
        return self.managed_root

    def managed_cache_dir(self, source_path: str | os.PathLike[str]) -> Path:
        canonical_source = os.path.realpath(os.path.abspath(source_path))
        digest = hashlib.sha256(os.fsencode(canonical_source)).hexdigest()[:16]
        readable_name = re.sub(
            r"[^A-Za-z0-9._-]+", "-", Path(canonical_source).stem
        ).strip("-._")
        readable_name = (readable_name or "map")[:48]
        return self.managed_root / f"{readable_name}-{digest}"

    def adjacent_cache_dir(self, source_path: str | os.PathLike[str]) -> Path:
        """Return the simple map-local cache directory for ``source_path``."""
        canonical_source = Path(os.path.abspath(source_path))
        return canonical_source.parent / ADJACENT_CACHE_DIRNAME

    def candidates(self, source_path: str | os.PathLike[str]) -> tuple[Path, ...]:
        return (self.build_cache_dir(source_path),)

    def build_cache_dir(self, source_path: str | os.PathLike[str]) -> Path:
        if self.configured_managed_root() is not None:
            return self.managed_cache_dir(source_path)
        return self.adjacent_cache_dir(source_path)

    def cache_root_for_source(self, source_path: str | os.PathLike[str]) -> Path:
        """Return the parent location reported as the cache root for a source."""
        if self.configured_managed_root() is not None:
            return self.managed_root
        return self.adjacent_cache_dir(source_path).parent

    def generated_cache_dir(self, source_path: str | os.PathLike[str]) -> Path:
        """Return the generated cache directory CaveViewer may manage."""
        return self.build_cache_dir(source_path)

    def is_managed(self, cache_dir: str | os.PathLike[str]) -> bool:
        managed_root = os.path.realpath(self.managed_root)
        candidate = os.path.realpath(cache_dir)
        try:
            return os.path.commonpath((managed_root, candidate)) == managed_root
        except ValueError:
            return False


def map_cache_candidates(
    source_path: str,
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> tuple[str, ...]:
    """Return cache candidates from explicit storage inputs when supplied."""

    return tuple(
        str(path)
        for path in MapCacheLocator(
            environ=environ,
            platform_name=platform_name,
        ).candidates(source_path)
    )


def map_cache_build_dir(
    source_path: str,
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> str:
    """Return the generated build location from explicit storage inputs."""

    return str(
        MapCacheLocator(
            environ=environ,
            platform_name=platform_name,
        ).build_cache_dir(source_path)
    )


def map_texture_dir(
    source_path: str, cache_dir: str, source_textures_dir: str
) -> str:
    """Return the self-contained cache asset directory."""
    del source_path, source_textures_dir
    return cache_dir
