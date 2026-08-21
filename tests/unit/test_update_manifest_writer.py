"""Direct contract coverage for the shared update-manifest writer."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WRITER = REPOSITORY_ROOT / "scripts" / "write_update_manifest.py"


def _run_writer(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WRITER), *(str(arg) for arg in args)],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    (
        "target",
        "architecture",
        "artifact_name",
        "download_url",
        "aliases",
        "authenticode_certificate_subject",
    ),
    [
        (
            "windows",
            None,
            "CaveViewer-1.2.3-windows.exe",
            "https://downloads.example/CaveViewer-1.2.3-windows.exe",
            (
                "download_url_windows_exe",
                "download_size_bytes_windows_exe",
                "sha256_windows_exe",
            ),
            "CN=CaveViewer Update Publisher",
        ),
        (
            "windows",
            None,
            "CaveViewer-1.2.3-windows.zip",
            "https://downloads.example/CaveViewer-1.2.3-windows.zip",
            (
                "download_url_windows_zip",
                "download_size_bytes_windows_zip",
                "sha256_windows_zip",
            ),
            None,
        ),
        (
            "linux",
            None,
            "CaveViewer-1.2.3-x86_64.AppImage",
            "https://downloads.example/CaveViewer-1.2.3-x86_64.AppImage",
            (
                "download_url_linux_appimage",
                "download_size_bytes_linux_appimage",
                "sha256_linux_appimage",
            ),
            None,
        ),
        (
            "macos",
            "arm64",
            "CaveViewer-1.2.3-macos-arm64.dmg",
            "https://downloads.example/CaveViewer-1.2.3-macos-arm64.dmg",
            (
                "download_url_macosx_dmg",
                "download_size_bytes_macosx_dmg",
                "sha256_macosx_dmg",
            ),
            None,
        ),
    ],
)
def test_writer_generates_platform_aliases_and_escaped_json(
    tmp_path: Path,
    target: str,
    architecture: str | None,
    artifact_name: str,
    download_url: str,
    aliases: tuple[str, str, str],
    authenticode_certificate_subject: str | None,
):
    artifact = tmp_path / artifact_name
    artifact_contents = b"CaveViewer artifact\n"
    artifact.write_bytes(artifact_contents)
    output = tmp_path / "updates" / f"{target}.json"
    notes = 'Quoted "release" notes with a \\backslash\nSecond line: cafe\u0301'
    command: list[str | Path] = [
        "--target",
        target,
        "--version",
        "v1.2.3",
        "--download-url",
        download_url,
        "--artifact-file",
        artifact,
        "--notes",
        notes,
        "--channel",
        "stable",
        "--output",
        output,
    ]
    if architecture is not None:
        command.extend(("--architecture", architecture))
    if authenticode_certificate_subject is not None:
        command.extend(
            (
                "--authenticode-certificate-subject",
                authenticode_certificate_subject,
            )
        )

    completed = _run_writer(*command)

    assert completed.returncode == 0, completed.stderr
    raw_manifest = output.read_bytes()
    assert b"\r\n" not in raw_manifest
    payload = json.loads(raw_manifest)
    expected_sha256 = hashlib.sha256(artifact_contents).hexdigest()

    assert payload["latest_version"] == "1.2.3"
    assert payload["download_url"] == download_url
    assert payload["download_size_bytes"] == len(artifact_contents)
    assert payload["sha256"] == expected_sha256
    assert payload["release_notes"] == notes
    assert payload[aliases[0]] == download_url
    assert payload[aliases[1]] == len(artifact_contents)
    assert payload[aliases[2]] == expected_sha256
    if target == "macos":
        assert payload["platform"] == "macos"
        assert payload["architecture"] == architecture
    if authenticode_certificate_subject is not None:
        assert payload["install_channel"] == "windows_installer"
        assert payload["authenticode_status"] == "verified"
        assert (
            payload["authenticode_certificate_subject"]
            == authenticode_certificate_subject
        )


@pytest.mark.parametrize(
    ("version", "download_url", "artifact_contents", "expected_error"),
    [
        (
            "1.2.3-rc1",
            "https://downloads.example/CaveViewer.zip",
            b"artifact",
            "bare dot-separated numeric",
        ),
        (
            "1.2.3",
            "http://downloads.example/CaveViewer.zip",
            b"artifact",
            "absolute HTTPS URL",
        ),
        (
            "1.2.3",
            "https://[invalid/CaveViewer.zip",
            b"artifact",
            "absolute HTTPS URL",
        ),
        (
            "1.2.3",
            "https://downloads.example/CaveViewer.dmg",
            b"artifact",
            "must end with one of: .zip, .exe",
        ),
        (
            "1.2.3",
            "https://downloads.example/CaveViewer.zip",
            b"",
            "must not be empty",
        ),
    ],
)
def test_writer_rejects_invalid_unsigned_manifest_inputs(
    tmp_path: Path,
    version: str,
    download_url: str,
    artifact_contents: bytes,
    expected_error: str,
):
    artifact = tmp_path / "CaveViewer.zip"
    artifact.write_bytes(artifact_contents)
    output = tmp_path / "updates" / "stable.json"

    completed = _run_writer(
        "--target",
        "windows",
        "--version",
        version,
        "--download-url",
        download_url,
        "--artifact-file",
        artifact,
        "--notes",
        "Release notes",
        "--channel",
        "stable",
        "--output",
        output,
    )

    assert completed.returncode == 2
    assert expected_error in completed.stderr
    assert not output.exists()


def test_writer_rejects_a_windows_exe_without_its_authenticode_subject(tmp_path: Path):
    artifact = tmp_path / "CaveViewer-1.2.3-windows.exe"
    artifact.write_bytes(b"signed installer fixture")
    output = tmp_path / "updates" / "stable.json"

    completed = _run_writer(
        "--target",
        "windows",
        "--version",
        "1.2.3",
        "--download-url",
        "https://downloads.example/CaveViewer-1.2.3-windows.exe",
        "--artifact-file",
        artifact,
        "--notes",
        "Release notes",
        "--channel",
        "stable",
        "--output",
        output,
    )

    assert completed.returncode == 2
    assert "--authenticode-certificate-subject" in completed.stderr
    assert not output.exists()


def test_writer_generates_an_explicit_unsigned_community_windows_manifest(
    tmp_path: Path,
):
    artifact = tmp_path / "CaveViewer-1.2.3-windows.exe"
    artifact.write_bytes(b"community installer fixture")
    output = tmp_path / "updates" / "stable.json"

    completed = _run_writer(
        "--target",
        "windows",
        "--version",
        "1.2.3",
        "--download-url",
        "https://downloads.example/CaveViewer-1.2.3-windows.exe",
        "--artifact-file",
        artifact,
        "--notes",
        "Release notes",
        "--channel",
        "stable",
        "--authenticode-status",
        "unsigned-community",
        "--output",
        output,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["authenticode_status"] == "unsigned-community"
    assert "authenticode_certificate_subject" not in payload


def test_writer_rejects_a_community_manifest_that_declares_a_publisher(tmp_path: Path):
    artifact = tmp_path / "CaveViewer-1.2.3-windows.exe"
    artifact.write_bytes(b"community installer fixture")
    output = tmp_path / "updates" / "stable.json"

    completed = _run_writer(
        "--target",
        "windows",
        "--version",
        "1.2.3",
        "--download-url",
        "https://downloads.example/CaveViewer-1.2.3-windows.exe",
        "--artifact-file",
        artifact,
        "--notes",
        "Release notes",
        "--channel",
        "stable",
        "--authenticode-status",
        "unsigned-community",
        "--authenticode-certificate-subject",
        "CN=Unexpected Publisher",
        "--output",
        output,
    )

    assert completed.returncode == 2
    assert "must not declare" in completed.stderr
    assert not output.exists()
