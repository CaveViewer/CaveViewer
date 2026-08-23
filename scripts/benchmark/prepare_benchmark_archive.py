#!/usr/bin/env python3
"""Download and safely extract a hash-pinned benchmark cache archive."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


MAX_DOWNLOAD_BYTES = 4 * 1024**3
MAX_EXPANDED_BYTES = 16 * 1024**3
MAX_MEMBERS = 100_000
MAX_COMPRESSION_RATIO = 200
COPY_CHUNK_BYTES = 1024 * 1024


def _validated_https_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("benchmark map URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("benchmark map URL must not contain credentials")
    return value


def _validated_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("benchmark map SHA-256 must contain exactly 64 hexadecimal characters")
    return normalized


def download_archive(url: str, expected_sha256: str, destination: Path) -> None:
    expected_sha256 = _validated_sha256(expected_sha256)
    request = urllib.request.Request(
        _validated_https_url(url),
        headers={"User-Agent": "CaveViewer-benchmark-archive/1"},
    )
    digest = hashlib.sha256()
    downloaded = 0
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - HTTPS is validated.
        _validated_https_url(response.geturl())
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > MAX_DOWNLOAD_BYTES:
            raise ValueError("benchmark map archive exceeds the compressed-size limit")
        with destination.open("xb") as output:
            while chunk := response.read(COPY_CHUNK_BYTES):
                downloaded += len(chunk)
                if downloaded > MAX_DOWNLOAD_BYTES:
                    raise ValueError("benchmark map archive exceeds the compressed-size limit")
                digest.update(chunk)
                output.write(chunk)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        destination.unlink(missing_ok=True)
        raise ValueError(
            f"benchmark map SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    print(f"benchmark_map_sha256={actual_sha256}")


def _member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    # On Windows, ZipInfo normalizes backslashes in ``filename`` to forward
    # slashes. ``orig_filename`` preserves the name encoded in the archive,
    # which is what must be validated before treating ZIP paths as POSIX paths.
    original_filename = info.orig_filename
    if "\\" in original_filename:
        raise ValueError(f"archive member uses a backslash path: {original_filename!r}")
    member = PurePosixPath(info.filename)
    if member.is_absolute() or not member.parts or any(part in {"", ".", ".."} for part in member.parts):
        raise ValueError(f"archive member has an unsafe path: {info.filename!r}")
    return member


def _validate_member_type(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x1:
        raise ValueError(f"encrypted archive member is not supported: {info.filename!r}")
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    allowed_types = {0, stat.S_IFREG, stat.S_IFDIR}
    if file_type not in allowed_types or stat.S_ISLNK(mode):
        raise ValueError(f"archive member is not a regular file or directory: {info.filename!r}")


def extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_MEMBERS:
            raise ValueError("benchmark map archive contains too many members")
        expanded_bytes = 0
        seen_paths: set[str] = set()
        validated: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        for info in members:
            member = _member_path(info)
            _validate_member_type(info)
            canonical = member.as_posix()
            if canonical in seen_paths:
                raise ValueError(f"duplicate archive member: {canonical!r}")
            seen_paths.add(canonical)
            expanded_bytes += info.file_size
            if expanded_bytes > MAX_EXPANDED_BYTES:
                raise ValueError("benchmark map archive exceeds the expanded-size limit")
            if info.file_size and (
                info.compress_size == 0
                or info.file_size > info.compress_size * MAX_COMPRESSION_RATIO
            ):
                raise ValueError(f"archive member exceeds the compression-ratio limit: {canonical!r}")
            validated.append((info, member))

        for info, member in validated:
            output_path = destination.joinpath(*member.parts)
            if info.is_dir():
                output_path.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with bundle.open(info) as source, output_path.open("xb") as output:
                shutil.copyfileobj(source, output, COPY_CHUNK_BYTES)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        download_archive(args.url, args.sha256, args.archive)
        extract_archive(args.archive, args.destination)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"Error: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
