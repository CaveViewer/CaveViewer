"""Versioned cache identities used to associate Guided Dives with one map.

Cache construction owns source-file hashing while it is already performing
background import work.  GUI and playback code only consume the compact,
immutable payload retained in ``manifest.json``; they never hash a large map
from the render thread.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from typing import Any


GUIDED_DIVE_CACHE_IDENTITY_KEY = "guided_dive_identity"
GUIDED_DIVE_CACHE_IDENTITY_VERSION = 1
_SHA256_HEX_LENGTH = 64
_HASH_BLOCK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class GuidedDiveCacheIdentity:
    """Portable, versioned identity of one source map and generated cache."""

    version: int
    source_sha256: str
    cache_manifest_sha256: str

    def payload(self) -> dict[str, int | str]:
        """Return the JSON-safe payload stored in manifests and trace headers."""
        return {
            "version": self.version,
            "source_sha256": self.source_sha256,
            "cache_manifest_sha256": self.cache_manifest_sha256,
        }


def build_guided_dive_cache_identity(
    source_path: str | os.PathLike[str],
    manifest: Mapping[str, Any],
) -> GuidedDiveCacheIdentity:
    """Create the cache identity during construction, outside render-thread work."""
    raw_source_path = os.fspath(source_path).strip()
    if not raw_source_path:
        raise ValueError("Guided Dive cache identity requires a source path")
    return GuidedDiveCacheIdentity(
        version=GUIDED_DIVE_CACHE_IDENTITY_VERSION,
        source_sha256=_sha256_file(raw_source_path),
        cache_manifest_sha256=_canonical_manifest_sha256(manifest),
    )


def build_derived_guided_dive_cache_identity(
    parent_identity: GuidedDiveCacheIdentity,
    derivative: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> GuidedDiveCacheIdentity:
    """Build a distinct identity for a cache derived from another cache.

    A standalone slice has no original OBJ/GLB to hash.  Its source digest is
    therefore a canonical, domain-separated digest of the parent identity and
    derivative selection, while its manifest digest retains the normal cache
    identity semantics.  This keeps Guided Dive traces portable and prevents a
    trace for a parent map from matching one of its slices.
    """
    if parse_guided_dive_cache_identity(parent_identity.payload()) is None:
        raise ValueError("Derived Guided Dive identity requires a valid parent")
    if not isinstance(derivative, Mapping):
        raise TypeError("Derived Guided Dive identity requires mapping metadata")
    return GuidedDiveCacheIdentity(
        version=GUIDED_DIVE_CACHE_IDENTITY_VERSION,
        source_sha256=_canonical_payload_sha256(
            {
                "kind": "caveviewer.derived-cache",
                "version": 1,
                "parent_identity": parent_identity.payload(),
                "derivative": dict(derivative),
            }
        ),
        cache_manifest_sha256=_canonical_manifest_sha256(manifest),
    )


def guided_dive_cache_identity_from_manifest(
    manifest: Mapping[str, Any] | None,
) -> GuidedDiveCacheIdentity | None:
    """Return a validated stored identity without rehashing the manifest."""
    if not isinstance(manifest, Mapping):
        return None
    return parse_guided_dive_cache_identity(
        manifest.get(GUIDED_DIVE_CACHE_IDENTITY_KEY)
    )


def parse_guided_dive_cache_identity(
    value: Any,
) -> GuidedDiveCacheIdentity | None:
    """Parse one bounded JSON identity payload, returning ``None`` if invalid."""
    if not isinstance(value, Mapping):
        return None
    version = value.get("version")
    source_sha256 = value.get("source_sha256")
    cache_manifest_sha256 = value.get("cache_manifest_sha256")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != GUIDED_DIVE_CACHE_IDENTITY_VERSION
        or not _is_sha256(source_sha256)
        or not _is_sha256(cache_manifest_sha256)
    ):
        return None
    return GuidedDiveCacheIdentity(
        version=version,
        source_sha256=source_sha256,
        cache_manifest_sha256=cache_manifest_sha256,
    )


def _canonical_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash all cache metadata except the identity field being constructed."""
    payload = dict(manifest)
    payload.pop(GUIDED_DIVE_CACHE_IDENTITY_KEY, None)
    return _canonical_payload_sha256(payload)


def canonical_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Return the stable digest of cache metadata excluding its own identity."""
    return _canonical_manifest_sha256(manifest)


def _canonical_payload_sha256(payload: Mapping[str, Any]) -> str:
    encoder = json.JSONEncoder(
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256()
    for fragment in encoder.iterencode(payload):
        digest.update(fragment.encode("utf-8"))
    return digest.hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source_file:
        for block in iter(lambda: source_file.read(_HASH_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != _SHA256_HEX_LENGTH:
        return False
    if value.lower() != value:
        return False
    return all(character in "0123456789abcdef" for character in value)
