"""Contracts for signed Windows update manifests and publishing."""

import json
from pathlib import Path

from caveviewer.gui.update_signature import verify_update_manifest_signature


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_UPDATES = REPOSITORY_ROOT / "updates" / "windows"


def test_windows_manifests_are_signed_for_each_release_channel():
    for channel in ("stable", "prerelease"):
        manifest = WINDOWS_UPDATES / f"{channel}.json"
        signature = WINDOWS_UPDATES / f"{channel}.json.sig"
        payload = json.loads(manifest.read_text(encoding="utf-8"))

        assert payload["download_url"].endswith("-windows.zip")
        assert payload["download_url_windows_zip"] == payload["download_url"]
        assert signature.is_file()
        verify_update_manifest_signature(manifest.read_bytes(), signature.read_bytes())


def test_windows_publisher_signs_and_commits_the_selected_channel():
    publisher = (REPOSITORY_ROOT / "scripts" / "windows" / "publish.sh").read_text(
        encoding="utf-8"
    )

    assert "CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY must be set" in publisher
    assert (
        'update_manifest_path="$repo_root/updates/windows/$manifest_channel.json"'
        in publisher
    )
    assert 'update_manifest_signature_path="$update_manifest_path.sig"' in publisher
    assert '"$repo_root/scripts/sign_update_manifest.py"' in publisher
    assert '"updates/windows/$manifest_channel.json.sig"' in publisher


def test_signed_update_manifests_have_a_stable_line_ending_contract():
    attributes = (REPOSITORY_ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "updates/**/*.json text eol=lf" in attributes
