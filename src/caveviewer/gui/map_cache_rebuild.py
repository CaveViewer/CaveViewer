"""Map-local cache-rebuild capability and preflight policy for the splash.

This module owns facts that vary for each Map Library row.  It intentionally
does not use ``PlatformRuntime.feature_gates``: a source model, generated cache
destination, or competing cache builder can change after the splash starts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from caveviewer.core.capabilities import CapabilityResult
from caveviewer.core.chunking.staging import MANIFEST_NAME, ResumableObjImport
from caveviewer.core.map.cache_build_lock import cache_build_is_locked
from caveviewer.core.map.cache_paths import MapCacheLocator
from caveviewer.core.map.importer import probe_resumable_import
from caveviewer.core.map.source_model import find_model_file
from caveviewer.gui.features import (
    FeatureDecision,
    FeatureId,
    decide_map_library_cache_rebuild,
)


@dataclass(frozen=True, slots=True)
class CacheRebuildTarget:
    """One validated source and existing generated destination to replace."""

    map_path: Path
    model_descriptor: Mapping[str, str]
    textures_dir: Path
    cache_dir: Path

    def __post_init__(self) -> None:
        descriptor = {
            str(key): str(value)
            for key, value in self.model_descriptor.items()
        }
        object.__setattr__(self, "model_descriptor", MappingProxyType(descriptor))

    @property
    def source_path(self) -> Path:
        """Return the selected OBJ or GLB source path."""
        source = self.model_descriptor.get("obj_path") or self.model_descriptor.get(
            "glb_path"
        )
        if not source:
            raise ValueError("cache rebuild target is missing a source path")
        return Path(source)


@dataclass(frozen=True, slots=True)
class CacheRebuildPreflight:
    """Map-local cache-rebuild facts paired with a pure presentation decision."""

    capability: CapabilityResult[CacheRebuildTarget]
    decision: FeatureDecision
    resumable_import: ResumableObjImport | None = None

    def __post_init__(self) -> None:
        if self.decision.feature is not FeatureId.MAP_LIBRARY_CACHE_REBUILD:
            raise ValueError(
                "cache rebuild preflight must contain a Map Library rebuild decision"
            )


def _path_exists_without_following(path: Path) -> bool:
    """Return whether a path entry exists, including a broken symlink."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _adjacent_cache_candidate(map_path: Path) -> Path:
    """Return the only cache location inferable without a source descriptor."""
    if map_path.suffix.lower() in {".obj", ".glb"}:
        return map_path.parent / "_cache"
    return map_path / "_cache"


def _contains_precompiled_cache(map_path: Path) -> bool:
    """Return whether the selected entry itself looks like a cache directory."""
    return map_path.is_dir() and (map_path / MANIFEST_NAME).is_file()


def _descriptor_source_paths(descriptor: Mapping[str, str]) -> tuple[Path, ...]:
    if descriptor.get("obj_path"):
        return tuple(
            Path(value)
            for value in (descriptor.get("obj_path"), descriptor.get("mtl_path"))
            if value
        )
    if descriptor.get("glb_path"):
        return (Path(descriptor["glb_path"]),)
    return ()


def _sources_are_readable(descriptor: Mapping[str, str]) -> bool:
    """Verify source and required OBJ material files without parsing them."""
    source_paths = _descriptor_source_paths(descriptor)
    if not source_paths:
        return False
    for source_path in source_paths:
        if not source_path.is_file():
            return False
        with source_path.open("rb") as file_obj:
            file_obj.read(1)
    return True


def _cache_destination_is_safe(cache_dir: Path) -> bool:
    """Return whether an existing generated cache can be atomically replaced."""
    if cache_dir.is_symlink() or not cache_dir.is_dir():
        return False
    parent = cache_dir.parent
    if not parent.is_dir():
        return False
    return os.access(parent, os.W_OK | os.X_OK)


def _preflight_from_capability(
    capability: CapabilityResult[CacheRebuildTarget],
    *,
    resumable_import: ResumableObjImport | None = None,
) -> CacheRebuildPreflight:
    return CacheRebuildPreflight(
        capability=capability,
        decision=decide_map_library_cache_rebuild(capability),
        resumable_import=resumable_import,
    )


def probe_map_library_cache_rebuild(
    map_path: str | os.PathLike[str],
) -> CacheRebuildPreflight:
    """Validate whether one Map Library entry may force-rebuild its cache.

    A valid cache manifest is not required: rebuilding a stale or malformed
    generated cache is an intentional repair path.  The target must instead
    have a discoverable readable source, an existing non-symlink destination,
    and no active cooperative build lock.
    """
    try:
        selected_path = Path(map_path).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError):
        return _preflight_from_capability(
            CapabilityResult.unknown(
                reason_code="map_cache_rebuild_probe_failed",
                evidence={"map_path": "invalid"},
            )
        )

    try:
        descriptor = find_model_file(os.fspath(selected_path))
    except FileNotFoundError:
        try:
            if _contains_precompiled_cache(selected_path):
                return _preflight_from_capability(
                    CapabilityResult.unavailable(
                        reason_code="map_cache_rebuild_precompiled_map",
                        evidence={"map": "precompiled_cache"},
                    )
                )
            if _path_exists_without_following(
                _adjacent_cache_candidate(selected_path)
            ):
                return _preflight_from_capability(
                    CapabilityResult.unavailable(
                        reason_code="map_cache_rebuild_source_unavailable",
                        evidence={"source": "missing", "cache": "present"},
                    )
                )
        except OSError:
            return _preflight_from_capability(
                CapabilityResult.unknown(
                    reason_code="map_cache_rebuild_probe_failed",
                    evidence={"filesystem": "unreadable"},
                )
            )
        return _preflight_from_capability(
            CapabilityResult.unavailable(
                reason_code="map_cache_rebuild_no_generated_cache",
                evidence={"cache": "missing"},
            )
        )
    except (OSError, TypeError, ValueError):
        return _preflight_from_capability(
            CapabilityResult.unknown(
                reason_code="map_cache_rebuild_probe_failed",
                evidence={"source": "discovery_failed"},
            )
        )

    source_path = descriptor.get("obj_path") or descriptor.get("glb_path")
    if not source_path:
        return _preflight_from_capability(
            CapabilityResult.unknown(
                reason_code="map_cache_rebuild_probe_failed",
                evidence={"source": "descriptor_missing"},
            )
        )

    try:
        cache_dir = MapCacheLocator().generated_cache_dir(source_path)
        if not _path_exists_without_following(cache_dir):
            return _preflight_from_capability(
                CapabilityResult.unavailable(
                    reason_code="map_cache_rebuild_no_generated_cache",
                    evidence={"cache": "missing"},
                )
            )
        if not _cache_destination_is_safe(cache_dir):
            return _preflight_from_capability(
                CapabilityResult.unavailable(
                    reason_code="map_cache_rebuild_destination_unsafe",
                    evidence={"cache": "unsafe_destination"},
                )
            )
        if not _sources_are_readable(descriptor):
            return _preflight_from_capability(
                CapabilityResult.unavailable(
                    reason_code="map_cache_rebuild_source_unreadable",
                    evidence={"source": "unreadable"},
                )
            )
    except (OSError, TypeError, ValueError):
        return _preflight_from_capability(
            CapabilityResult.unknown(
                reason_code="map_cache_rebuild_probe_failed",
                evidence={"filesystem": "probe_failed"},
            )
        )

    target = CacheRebuildTarget(
        map_path=selected_path,
        model_descriptor=descriptor,
        textures_dir=Path(source_path).parent,
        cache_dir=cache_dir,
    )
    try:
        resumable_import = probe_resumable_import(
            dict(descriptor),
            cache_dir=os.fspath(cache_dir),
        )
    except (OSError, TypeError, ValueError):
        return _preflight_from_capability(
            CapabilityResult.unknown(
                reason_code="map_cache_rebuild_probe_failed",
                evidence={"checkpoint": "probe_failed"},
            )
        )
    if cache_build_is_locked(cache_dir):
        return _preflight_from_capability(
            CapabilityResult.unavailable(
                reason_code="map_cache_rebuild_already_in_progress",
                evidence={"cache": "build_locked"},
            ),
            resumable_import=resumable_import,
        )
    return _preflight_from_capability(
        CapabilityResult.available(
            target,
            reason_code="map_cache_rebuild_target_available",
            evidence={"cache": "generated_destination", "source": "readable"},
        ),
        resumable_import=resumable_import,
    )
