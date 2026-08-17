"""Contracts for Windows manifest publishing and checkout safety."""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
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
