"""Tests for post-upload GitHub release asset verification."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.common.verify_release_asset import (
    ReleaseAssetVerificationError,
    verify_release_asset,
)


def _release_payload(artifact: Path) -> dict:
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return {
        "assets": [
            {
                "browser_download_url": (
                    "https://github.com/example/CaveViewer/releases/download/"
                    f"v2.0.0/{artifact.name}"
                ),
                "digest": f"sha256:{digest}",
                "name": artifact.name,
                "size": artifact.stat().st_size,
                "state": "uploaded",
            }
        ],
        "draft": False,
        "prerelease": True,
        "tag_name": "v2.0.0",
    }


def test_release_asset_verifier_returns_the_api_browser_url(tmp_path: Path):
    artifact = tmp_path / "CaveViewer-2.0.0-x86_64.AppImage"
    artifact.write_bytes(b"verified payload")
    release = _release_payload(artifact)

    verified_url = verify_release_asset(
        release,
        artifact_path=artifact,
        expected_tag="v2.0.0",
    )

    assert verified_url == release["assets"][0]["browser_download_url"]


@pytest.mark.parametrize(
    ("release_mutation", "expected_error"),
    [
        (lambda release: release.update(tag_name="v1.9.0"), "tag"),
        (lambda release: release.update(draft=True), "draft"),
        (lambda release: release["assets"][0].update(state="new"), "uploaded"),
        (lambda release: release["assets"][0].update(size=1), "size"),
        (
            lambda release: release["assets"][0].update(digest="sha256:" + "0" * 64),
            "digest",
        ),
        (
            lambda release: release["assets"][0].update(
                browser_download_url="http://example.invalid/payload"
            ),
            "download URL",
        ),
    ],
)
def test_release_asset_verifier_rejects_unpublishable_metadata(
    tmp_path: Path,
    release_mutation,
    expected_error: str,
):
    artifact = tmp_path / "CaveViewer-2.0.0-x86_64.AppImage"
    artifact.write_bytes(b"verified payload")
    release = deepcopy(_release_payload(artifact))
    release_mutation(release)

    with pytest.raises(ReleaseAssetVerificationError, match=expected_error):
        verify_release_asset(
            release,
            artifact_path=artifact,
            expected_tag="v2.0.0",
        )


def test_release_asset_verifier_requires_one_exact_asset_name(tmp_path: Path):
    artifact = tmp_path / "CaveViewer-2.0.0-x86_64.AppImage"
    artifact.write_bytes(b"verified payload")
    release = _release_payload(artifact)
    release["assets"].append(deepcopy(release["assets"][0]))

    with pytest.raises(ReleaseAssetVerificationError, match="exactly one"):
        verify_release_asset(
            release,
            artifact_path=artifact,
            expected_tag="v2.0.0",
        )
