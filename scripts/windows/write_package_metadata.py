#!/usr/bin/env python3
"""Write Windows installer metadata from the final package artifact.

The release finalizer consumes the regular metadata file and the in-app update
path consumes the update metadata file. Both are derived from the same final
installer so their URL, size, and digest cannot diverge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write CaveViewer Windows installer metadata."
    )
    parser.add_argument("--artifact-file", type=Path, required=True)
    parser.add_argument("--metadata-output", type=Path, required=True)
    parser.add_argument("--update-output", type=Path, required=True)
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--created-at-utc", required=True)
    parser.add_argument("--download-url", default="")
    parser.add_argument(
        "--authenticode-status",
        choices=("verified", "unsigned-test-only"),
        required=True,
    )
    parser.add_argument("--authenticode-certificate-subject", default="")
    return parser.parse_args()


def _integrity(path: Path) -> tuple[int, str]:
    if not path.is_file():
        raise ValueError(f"artifact does not exist: {path}")
    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        raise ValueError(f"artifact must not be empty: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(block)
    return size_bytes, digest.hexdigest()


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    args = _parse_args()
    artifact = args.artifact_file.resolve()
    if artifact.suffix.lower() != ".exe":
        raise ValueError(f"Windows installer artifact must be an EXE: {artifact}")
    size_bytes, sha256 = _integrity(artifact)
    artifact_name = artifact.name
    download_url = args.download_url
    authenticode_certificate_subject = str(
        args.authenticode_certificate_subject
    ).strip()
    if args.authenticode_status == "verified" and not authenticode_certificate_subject:
        raise ValueError(
            "verified Windows installer metadata requires an Authenticode "
            "certificate subject"
        )
    if (
        args.authenticode_status == "unsigned-test-only"
        and authenticode_certificate_subject
    ):
        raise ValueError(
            "unsigned test-only Windows installer metadata must not declare an "
            "Authenticode certificate subject"
        )
    if download_url:
        parsed_url = urlparse(download_url)
        if (
            parsed_url.scheme.lower() != "https"
            or not parsed_url.netloc
            or not parsed_url.path.lower().endswith(".exe")
        ):
            raise ValueError(
                "download URL must be an absolute HTTPS URL ending in .exe"
            )

    package_payload: dict[str, object] = {
        "app_name": args.app_name,
        "artifact_file": artifact_name,
        "artifact_path": f"dist/windows/packages/{artifact_name}",
        "authenticode_required": True,
        "authenticode_status": args.authenticode_status,
        "created_at_utc": args.created_at_utc,
        "entrypoint": "CaveViewerSetup.exe",
        "package_type": "windows_signed_installer",
        "sha256": sha256,
        "size_bytes": size_bytes,
        "version": args.version,
    }
    if download_url:
        package_payload["download_url"] = download_url
    if authenticode_certificate_subject:
        package_payload["authenticode_certificate_subject"] = (
            authenticode_certificate_subject
        )

    update_payload: dict[str, object] = {
        "app_name": args.app_name,
        "authenticode_required": True,
        "authenticode_status": args.authenticode_status,
        "download_size_bytes": size_bytes,
        "download_size_bytes_windows_exe": size_bytes,
        "download_url": download_url,
        "download_url_windows_exe": download_url,
        "install_channel": "windows_installer",
        "latest_version": args.version,
        "release_notes": "",
        "sha256": sha256,
        "sha256_windows_exe": sha256,
    }
    if authenticode_certificate_subject:
        update_payload["authenticode_certificate_subject"] = (
            authenticode_certificate_subject
        )
    _write(args.metadata_output, package_payload)
    _write(args.update_output, update_payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
