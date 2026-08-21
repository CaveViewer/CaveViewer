"""Tests for the core-only embedded release-channel contract."""

from __future__ import annotations

import json

import pytest

from caveviewer.core.release_metadata import (
    ReleaseMetadata,
    ReleaseMetadataSource,
    default_release_metadata,
    load_embedded_release_metadata,
)


def test_default_release_metadata_is_the_stable_source_checkout_default():
    metadata = default_release_metadata()

    assert metadata.release_channel == "stable"
    assert metadata.source is ReleaseMetadataSource.SOURCE_DEFAULT
    assert metadata.diagnostic is None


def test_loader_reads_and_normalizes_a_valid_embedded_resource(tmp_path):
    metadata_path = tmp_path / "release_metadata.v1.json"
    metadata_path.write_text(
        json.dumps({"schema_version": 1, "release_channel": " Prerelease "}),
        encoding="utf-8",
    )

    metadata = load_embedded_release_metadata(metadata_path)

    assert metadata == ReleaseMetadata(
        release_channel="prerelease",
        source=ReleaseMetadataSource.BUNDLED,
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"schema_version": 2, "release_channel": "stable"},
        {"schema_version": True, "release_channel": "stable"},
        {"schema_version": 1, "release_channel": "preview"},
        {"schema_version": 1, "release_channel": 7},
    ],
)
def test_loader_safely_falls_back_for_invalid_embedded_metadata(tmp_path, payload):
    metadata_path = tmp_path / "release_metadata.v1.json"
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    metadata = load_embedded_release_metadata(metadata_path)

    assert metadata.release_channel == "stable"
    assert metadata.source is ReleaseMetadataSource.FALLBACK_INVALID
    assert metadata.diagnostic


def test_loader_safely_falls_back_for_missing_or_unreadable_metadata(tmp_path):
    missing = load_embedded_release_metadata(tmp_path / "missing.json")
    unreadable = load_embedded_release_metadata(tmp_path)

    assert missing.release_channel == "stable"
    assert missing.source is ReleaseMetadataSource.FALLBACK_MISSING
    assert missing.diagnostic
    assert unreadable.release_channel == "stable"
    assert unreadable.source is ReleaseMetadataSource.FALLBACK_UNREADABLE
    assert unreadable.diagnostic


def test_release_metadata_rejects_an_unknown_channel():
    with pytest.raises(ValueError, match="release channel"):
        ReleaseMetadata("preview", ReleaseMetadataSource.BUNDLED)
