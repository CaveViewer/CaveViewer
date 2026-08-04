"""Tests for versioned Guided Dive cache identities."""

from __future__ import annotations

from caveviewer.core.map.cache_identity import (
    GUIDED_DIVE_CACHE_IDENTITY_KEY,
    build_guided_dive_cache_identity,
    guided_dive_cache_identity_from_manifest,
    parse_guided_dive_cache_identity,
)


def _manifest() -> dict:
    return {
        "version": 1,
        "source_obj": "cave.obj",
        "chunk_size": 50.0,
        "triangle_count": 12,
        "chunks": {
            "0_0_0": {
                "materials": ["rock"],
                "bounds_min": [0.0, 0.0, 0.0],
                "bounds_max": [50.0, 50.0, 50.0],
            }
        },
    }


def test_cache_identity_is_portable_and_independent_of_mapping_order(tmp_path):
    source = tmp_path / "cave.obj"
    source.write_bytes(b"v 0 0 0\n")
    first_manifest = _manifest()
    reordered_manifest = {
        "chunks": first_manifest["chunks"],
        "triangle_count": 12,
        "chunk_size": 50.0,
        "source_obj": "cave.obj",
        "version": 1,
    }

    first = build_guided_dive_cache_identity(source, first_manifest)
    second = build_guided_dive_cache_identity(source, reordered_manifest)

    assert first == second
    assert guided_dive_cache_identity_from_manifest(
        {**first_manifest, GUIDED_DIVE_CACHE_IDENTITY_KEY: first.payload()}
    ) == first


def test_cache_identity_changes_for_source_or_cache_metadata(tmp_path):
    source = tmp_path / "cave.obj"
    source.write_bytes(b"v 0 0 0\n")
    manifest = _manifest()
    original = build_guided_dive_cache_identity(source, manifest)

    source.write_bytes(b"v 1 0 0\n")
    changed_source = build_guided_dive_cache_identity(source, manifest)
    changed_manifest = build_guided_dive_cache_identity(
        source,
        {**manifest, "triangle_count": 13},
    )

    assert changed_source.source_sha256 != original.source_sha256
    assert changed_source.cache_manifest_sha256 == original.cache_manifest_sha256
    assert changed_manifest.cache_manifest_sha256 != original.cache_manifest_sha256


def test_cache_identity_parser_rejects_unsupported_or_malformed_payloads():
    assert parse_guided_dive_cache_identity(None) is None
    assert parse_guided_dive_cache_identity(
        {
            "version": 2,
            "source_sha256": "a" * 64,
            "cache_manifest_sha256": "b" * 64,
        }
    ) is None
    assert parse_guided_dive_cache_identity(
        {
            "version": 1,
            "source_sha256": "A" * 64,
            "cache_manifest_sha256": "b" * 64,
        }
    ) is None
