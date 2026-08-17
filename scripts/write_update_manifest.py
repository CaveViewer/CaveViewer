#!/usr/bin/env python3
"""Write a release update manifest from a locally built artifact.

The platform shell wrappers deliberately delegate serialization here so every
manifest follows one JSON and integrity-metadata contract before it is signed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


_VERSION_PATTERN = re.compile(r"\d+(?:\.\d+)+\Z")
_TARGET_URL_SUFFIXES = {
    "windows": (".zip",),
    "linux": (".appimage",),
    "macos": (".dmg",),
}
_MACOS_ARCHITECTURES = ("arm64", "x86_64")


class ManifestInputError(ValueError):
    """Raised when an unsigned manifest cannot meet the release contract."""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write a validated CaveViewer update manifest."
    )
    parser.add_argument("--target", choices=sorted(_TARGET_URL_SUFFIXES), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--download-url", required=True)
    parser.add_argument("--artifact-file", type=Path, required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--channel", choices=("stable", "prerelease"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--architecture", choices=_MACOS_ARCHITECTURES)
    args = parser.parse_args(argv)

    if args.target == "macos" and args.architecture is None:
        parser.error("--architecture is required when --target is macos")
    if args.target != "macos" and args.architecture is not None:
        parser.error("--architecture is only supported when --target is macos")
    return args


def _canonical_version(version: str) -> str:
    canonical_version = version.strip()
    if canonical_version.lower().startswith("v"):
        canonical_version = canonical_version[1:]
    if not _VERSION_PATTERN.fullmatch(canonical_version):
        raise ManifestInputError(
            "--version must be a bare dot-separated numeric release version, "
            f"got {version!r}"
        )
    return canonical_version


def _validate_download_url(target: str, download_url: str) -> None:
    try:
        parsed_url = urlparse(download_url)
    except ValueError as error:
        raise ManifestInputError("--download-url must be an absolute HTTPS URL") from error
    if parsed_url.scheme.lower() != "https" or not parsed_url.netloc:
        raise ManifestInputError("--download-url must be an absolute HTTPS URL")

    allowed_suffixes = _TARGET_URL_SUFFIXES[target]
    if not parsed_url.path.lower().endswith(allowed_suffixes):
        suffixes = ", ".join(allowed_suffixes)
        raise ManifestInputError(
            f"--download-url for {target} must end with one of: {suffixes}"
        )


def _artifact_integrity(artifact_file: Path) -> tuple[int, str]:
    if not artifact_file.is_file():
        raise ManifestInputError(f"--artifact-file does not exist: {artifact_file}")

    size_bytes = artifact_file.stat().st_size
    if size_bytes <= 0:
        raise ManifestInputError("--artifact-file must not be empty")

    digest = hashlib.sha256()
    with artifact_file.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return size_bytes, digest.hexdigest()


def _manifest_payload(args: argparse.Namespace, size_bytes: int, sha256: str) -> dict[str, object]:
    """Build the signed payload using a single, explicit schema for each target."""

    payload: dict[str, object] = {
        "latest_version": args.version,
        "download_url": args.download_url,
        "download_size_bytes": size_bytes,
        "release_notes": args.notes,
        "sha256": sha256,
    }
    if args.target == "windows":
        payload.update(
            {
                "download_url_windows_zip": args.download_url,
                "download_size_bytes_windows_zip": size_bytes,
                "sha256_windows_zip": sha256,
            }
        )
    elif args.target == "linux":
        payload.update(
            {
                "download_url_linux_appimage": args.download_url,
                "download_size_bytes_linux_appimage": size_bytes,
                "sha256_linux_appimage": sha256,
            }
        )
    else:
        payload.update(
            {
                "platform": "macos",
                "architecture": args.architecture,
                "download_url_macosx_dmg": args.download_url,
                "download_size_bytes_macosx_dmg": size_bytes,
                "sha256_macosx_dmg": sha256,
            }
        )
    return payload


def _write_json(path: Path, payload: dict[str, object]) -> None:
    """Write atomically with LF line endings so signing is byte-stable."""

    path.parent.mkdir(parents=True, exist_ok=True)
    # Lexicographic key order is the canonical signed-manifest representation.
    contents = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as temporary_file:
            temporary_file.write(contents)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    args.version = _canonical_version(args.version)
    _validate_download_url(args.target, args.download_url)
    size_bytes, sha256 = _artifact_integrity(args.artifact_file)
    _write_json(args.output, _manifest_payload(args, size_bytes, sha256))
    print(f"Wrote update manifest: {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ManifestInputError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
