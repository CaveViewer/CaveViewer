#!/usr/bin/env python3
"""Verify one local artifact against GitHub's post-upload release metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse


class ReleaseAssetVerificationError(ValueError):
    """Raised when a GitHub release does not contain the expected asset."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact_file:
        for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_release_asset(
    release: Mapping[str, Any],
    *,
    artifact_path: Path,
    expected_tag: str,
) -> str:
    """Return the verified browser URL for one exact local release artifact."""
    if release.get("tag_name") != expected_tag:
        raise ReleaseAssetVerificationError(
            f"GitHub release tag does not match {expected_tag!r}."
        )
    if release.get("draft") is not False:
        raise ReleaseAssetVerificationError(
            f"GitHub release {expected_tag} is still a draft."
        )
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ReleaseAssetVerificationError(
            f"GitHub release {expected_tag} has no asset list."
        )

    matching_assets = [
        asset
        for asset in assets
        if isinstance(asset, Mapping) and asset.get("name") == artifact_path.name
    ]
    if len(matching_assets) != 1:
        raise ReleaseAssetVerificationError(
            f"Expected exactly one uploaded asset named {artifact_path.name!r}; "
            f"found {len(matching_assets)}."
        )

    asset = matching_assets[0]
    if asset.get("state") != "uploaded":
        raise ReleaseAssetVerificationError(
            f"GitHub asset {artifact_path.name!r} is not in the uploaded state."
        )

    local_size = artifact_path.stat().st_size
    remote_size = asset.get("size")
    if type(remote_size) is not int or remote_size != local_size:
        raise ReleaseAssetVerificationError(
            f"GitHub asset {artifact_path.name!r} has size {remote_size!r}; "
            f"expected {local_size}."
        )

    local_digest = _sha256(artifact_path)
    remote_digest = asset.get("digest")
    expected_digest = f"sha256:{local_digest}"
    if not isinstance(remote_digest, str) or remote_digest.lower() != expected_digest:
        raise ReleaseAssetVerificationError(
            f"GitHub asset {artifact_path.name!r} has digest {remote_digest!r}; "
            f"expected {expected_digest!r}."
        )

    browser_url = asset.get("browser_download_url")
    if not isinstance(browser_url, str):
        raise ReleaseAssetVerificationError(
            f"GitHub asset {artifact_path.name!r} has no browser download URL."
        )
    parsed_url = urlparse(browser_url)
    if (
        parsed_url.scheme.lower() != "https"
        or not parsed_url.netloc
        or Path(unquote(parsed_url.path)).name != artifact_path.name
    ):
        raise ReleaseAssetVerificationError(
            f"GitHub asset {artifact_path.name!r} has an invalid browser download URL."
        )

    return browser_url


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-json", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--expected-tag", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        release = json.loads(args.release_json.read_text(encoding="utf-8"))
        if not isinstance(release, dict):
            raise ReleaseAssetVerificationError(
                "GitHub release response must be a JSON object."
            )
        browser_url = verify_release_asset(
            release,
            artifact_path=args.artifact,
            expected_tag=args.expected_tag,
        )
    except (OSError, json.JSONDecodeError, ReleaseAssetVerificationError) as error:
        raise SystemExit(f"Error: release asset verification failed: {error}") from error

    print(browser_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
