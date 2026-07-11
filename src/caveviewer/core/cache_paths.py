"""Select backward-compatible adjacent or managed map-cache locations."""

from __future__ import annotations

import hashlib
import json
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


CACHE_DIRNAME = "_cache"
LEGACY_CACHE_DIRNAME = ".caveviewer_cache"
MANAGED_CACHE_ENV_VAR = "CAVEVIEWER_MAP_CACHE_DIR"
_MANIFEST_NAME = "manifest.json"


class MapCacheLocator:
    """Resolve cache candidates without depending on cache-format policy."""

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
        source_dir = Path(os.path.abspath(source_path)).parent
        return (
            source_dir / CACHE_DIRNAME,
            source_dir / LEGACY_CACHE_DIRNAME,
            self.managed_cache_dir(source_path),
        )

    def build_cache_dir(
        self,
        source_path: str | os.PathLike[str],
        *,
        prefer_existing: bool = False,
    ) -> Path:
        candidates = self.candidates(source_path)
        if prefer_existing:
            for candidate in candidates:
                if (candidate / _MANIFEST_NAME).is_file():
                    return candidate
        if self._platform_name.startswith("linux") or self._environ.get(
            MANAGED_CACHE_ENV_VAR, ""
        ).strip():
            return candidates[-1]
        return candidates[0]

    def is_managed(self, cache_dir: str | os.PathLike[str]) -> bool:
        managed_root = os.path.realpath(self.managed_root)
        candidate = os.path.realpath(cache_dir)
        try:
            return os.path.commonpath((managed_root, candidate)) == managed_root
        except ValueError:
            return False


def map_cache_candidates(source_path: str) -> tuple[str, ...]:
    return tuple(str(path) for path in MapCacheLocator().candidates(source_path))


def map_cache_build_dir(source_path: str, *, prefer_existing: bool = False) -> str:
    return str(
        MapCacheLocator().build_cache_dir(
            source_path, prefer_existing=prefer_existing
        )
    )


def map_texture_dir(
    source_path: str, cache_dir: str, source_textures_dir: str
) -> str:
    """Use self-contained managed assets while preserving legacy cache reads."""
    del source_path
    if MapCacheLocator().is_managed(cache_dir):
        return cache_dir
    # New caches on every platform contain their available texture assets, but
    # older/precompiled adjacent caches may still rely on the source folder.
    try:
        manifest_path = os.path.join(cache_dir, _MANIFEST_NAME)
        with open(manifest_path, "r", encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
        texture_names = [
            name
            for name in manifest.get("mtl_materials", {}).values()
            if isinstance(name, str) and name
        ]
        if texture_names and all(
            os.path.isfile(os.path.join(cache_dir, name)) for name in texture_names
        ):
            return cache_dir
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return source_textures_dir
