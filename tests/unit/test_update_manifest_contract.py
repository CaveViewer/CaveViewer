"""Cross-platform contracts for every signed update manifest in the repo."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from caveviewer.gui.update_signature import verify_update_manifest_signature


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
UPDATES_ROOT = REPOSITORY_ROOT / "updates"
VERSION_PATTERN = re.compile(r"\d+(?:\.\d+)+\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MANIFEST_PATHS = tuple(sorted(UPDATES_ROOT.glob("**/*.json")))


def test_update_manifests_and_signatures_are_complete_pairs():
    manifest_paths = set(UPDATES_ROOT.glob("**/*.json"))
    signed_manifest_paths = {
        signature.with_suffix("")
        for signature in UPDATES_ROOT.glob("**/*.json.sig")
    }

    assert manifest_paths == signed_manifest_paths


def _assert_common_contract(manifest: Path, payload: dict[str, object]) -> None:
    latest_version = payload.get("latest_version")
    assert isinstance(latest_version, str)
    assert VERSION_PATTERN.fullmatch(latest_version)

    download_url = payload.get("download_url")
    assert isinstance(download_url, str)
    parsed_url = urlparse(download_url)
    assert parsed_url.scheme == "https"
    assert parsed_url.netloc

    download_size_bytes = payload.get("download_size_bytes")
    assert type(download_size_bytes) is int
    assert download_size_bytes > 0

    sha256 = payload.get("sha256")
    assert isinstance(sha256, str)
    assert SHA256_PATTERN.fullmatch(sha256)
    assert isinstance(payload.get("release_notes"), str)

    signature = manifest.with_name(f"{manifest.name}.sig")
    assert signature.is_file()
    verify_update_manifest_signature(manifest.read_bytes(), signature.read_bytes())


@pytest.mark.parametrize("manifest", MANIFEST_PATHS, ids=lambda path: str(path.relative_to(REPOSITORY_ROOT)))
def test_signed_update_manifests_follow_the_release_contract(manifest: Path):
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    _assert_common_contract(manifest, payload)

    relative_path = manifest.relative_to(UPDATES_ROOT)
    # Existing signed manifests remain valid during the staged rollout. New
    # writers always add this field and its path is the expected channel.
    if "release_channel" in payload:
        assert payload["release_channel"] == relative_path.stem
    download_url = payload["download_url"]
    download_size_bytes = payload["download_size_bytes"]
    sha256 = payload["sha256"]

    if relative_path.parts[0] == "windows":
        if download_url.endswith("-windows.exe"):
            assert payload["download_url_windows_exe"] == download_url
            assert payload["download_size_bytes_windows_exe"] == download_size_bytes
            assert payload["sha256_windows_exe"] == sha256
            assert payload["install_channel"] == "windows_installer"
            authenticode_status = payload.get("authenticode_status", "verified")
            assert authenticode_status in {"verified", "unsigned-community"}
            certificate_subject = payload.get("authenticode_certificate_subject")
            if authenticode_status == "verified":
                assert isinstance(certificate_subject, str)
                assert certificate_subject.strip()
            else:
                assert certificate_subject is None
        else:
            # Existing signed manifests retain the ZIP aliases until their
            # source-bundle clients have completed the EXE migration.
            assert download_url.endswith("-windows.zip")
            assert payload["download_url_windows_zip"] == download_url
            assert payload["download_size_bytes_windows_zip"] == download_size_bytes
            assert payload["sha256_windows_zip"] == sha256
    elif relative_path.parts[0] == "linux":
        assert download_url.lower().endswith(".appimage")
        assert payload["download_url_linux_appimage"] == download_url
        assert payload["download_size_bytes_linux_appimage"] == download_size_bytes
        assert payload["sha256_linux_appimage"] == sha256
    elif relative_path.parts[0] == "macos":
        expected_architecture = (
            "arm64" if len(relative_path.parts) == 2 else relative_path.parts[1]
        )
        assert payload["platform"] == "macos"
        assert payload["architecture"] == expected_architecture
        assert download_url.endswith(".dmg")
        assert payload["download_url_macosx_dmg"] == download_url
        assert payload["download_size_bytes_macosx_dmg"] == download_size_bytes
        assert payload["sha256_macosx_dmg"] == sha256
    else:
        pytest.fail(f"Unexpected update manifest path: {relative_path}")
