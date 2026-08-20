#!/usr/bin/env python3
"""Validate final Windows installer metadata before release publication."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify signed CaveViewer Windows installer metadata."
    )
    parser.add_argument("--artifact-file", type=Path, required=True)
    parser.add_argument("--metadata-file", type=Path, required=True)
    parser.add_argument("--update-metadata-file", type=Path, required=True)
    args = parser.parse_args()

    artifact = args.artifact_file
    if artifact.suffix.lower() != ".exe":
        raise SystemExit(f"Error: Windows installer must be an EXE: {artifact}")
    if not artifact.is_file() or artifact.stat().st_size <= 0:
        raise SystemExit(f"Error: Windows installer artifact is missing or empty: {artifact}")
    try:
        payload = json.loads(args.metadata_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(
            f"Error: unable to read Windows package metadata: {args.metadata_file}"
        ) from error
    if not isinstance(payload, dict):
        raise SystemExit(
            f"Error: Windows package metadata must be a JSON object: {args.metadata_file}"
        )
    artifact_sha256 = _sha256(artifact)

    expected = {
        "artifact_file": artifact.name,
        "package_type": "windows_signed_installer",
        "authenticode_required": True,
        "authenticode_status": "verified",
        "entrypoint": "CaveViewerSetup.exe",
        "size_bytes": artifact.stat().st_size,
        "sha256": artifact_sha256,
    }
    mismatches = [
        f"{key}={payload.get(key)!r} (expected {value!r})"
        for key, value in expected.items()
        if payload.get(key) != value
    ]
    if mismatches:
        raise SystemExit(
            "Error: Windows installer metadata is not release-ready: "
            + "; ".join(mismatches)
        )

    certificate_subject = payload.get("authenticode_certificate_subject")
    if not isinstance(certificate_subject, str) or not certificate_subject.strip():
        raise SystemExit(
            "Error: Windows installer metadata is not release-ready: "
            "authenticode_certificate_subject is missing"
        )
    certificate_subject = certificate_subject.strip()

    try:
        update_payload = json.loads(
            args.update_metadata_file.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(
            "Error: unable to read Windows update package metadata: "
            f"{args.update_metadata_file}"
        ) from error
    if not isinstance(update_payload, dict):
        raise SystemExit(
            "Error: Windows update package metadata must be a JSON object: "
            f"{args.update_metadata_file}"
        )
    expected_update = {
        "authenticode_required": True,
        "authenticode_status": "verified",
        "download_size_bytes": artifact.stat().st_size,
        "download_size_bytes_windows_exe": artifact.stat().st_size,
        "install_channel": "windows_installer",
        "latest_version": payload.get("version"),
        "sha256": artifact_sha256,
        "sha256_windows_exe": artifact_sha256,
    }
    update_mismatches = [
        f"{key}={update_payload.get(key)!r} (expected {value!r})"
        for key, value in expected_update.items()
        if update_payload.get(key) != value
    ]
    if update_payload.get("download_url") != update_payload.get(
        "download_url_windows_exe"
    ):
        update_mismatches.append("download URL aliases differ")
    if update_payload.get("authenticode_certificate_subject") != certificate_subject:
        update_mismatches.append("Authenticode certificate subjects differ")
    if update_mismatches:
        raise SystemExit(
            "Error: Windows update package metadata is not release-ready: "
            + "; ".join(update_mismatches)
        )
    print(f"Verified release-ready Windows installer metadata: {args.metadata_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
