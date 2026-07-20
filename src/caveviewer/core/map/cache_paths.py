"""Select managed map-cache locations."""

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


class MapCacheLocator:
    """Resolve managed cache locations without depending on cache-format policy."""

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

    def managed_cache_dir(self, source_path: str | os.PathLike[str]) -> Path:
        canonical_source = os.path.realpath(os.path.abspath(source_path))
        digest = hashlib.sha256(os.fsencode(canonical_source)).hexdigest()[:16]
        readable_name = re.sub(
            r"[^A-Za-z0-9._-]+", "-", Path(canonical_source).stem
        ).strip("-._")
        readable_name = (readable_name or "map")[:48]
        return self.managed_root / f"{readable_name}-{digest}"

    def candidates(self, source_path: str | os.PathLike[str]) -> tuple[Path, ...]:
        return (self.managed_cache_dir(source_path),)

    def build_cache_dir(self, source_path: str | os.PathLike[str]) -> Path:
        return self.managed_cache_dir(source_path)

    def is_managed(self, cache_dir: str | os.PathLike[str]) -> bool:
        managed_root = os.path.realpath(self.managed_root)
        candidate = os.path.realpath(cache_dir)
        try:
            return os.path.commonpath((managed_root, candidate)) == managed_root
        except ValueError:
            return False


def map_cache_candidates(source_path: str) -> tuple[str, ...]:
    return tuple(str(path) for path in MapCacheLocator().candidates(source_path))


def map_cache_build_dir(source_path: str) -> str:
    return str(MapCacheLocator().build_cache_dir(source_path))


def map_texture_dir(
    source_path: str, cache_dir: str, source_textures_dir: str
) -> str:
    """Return the self-contained cache asset directory."""
    del source_path, source_textures_dir
    return cache_dir
