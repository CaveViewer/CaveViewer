"""Contracts for architecture-specific macOS update manifests and scripts."""

import subprocess
from pathlib import Path

from caveviewer.gui.update_signature import verify_update_manifest_signature


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MACOS_UPDATES = REPOSITORY_ROOT / "updates" / "macos"


def test_arm64_manifests_match_signed_legacy_compatibility_aliases():
    for channel in ("stable", "prerelease"):
        legacy_manifest = MACOS_UPDATES / f"{channel}.json"
        legacy_signature = MACOS_UPDATES / f"{channel}.json.sig"
        arm_manifest = MACOS_UPDATES / "arm64" / f"{channel}.json"
        arm_signature = MACOS_UPDATES / "arm64" / f"{channel}.json.sig"

        assert arm_manifest.read_bytes() == legacy_manifest.read_bytes()
        assert arm_signature.read_bytes() == legacy_signature.read_bytes()
        verify_update_manifest_signature(
            arm_manifest.read_bytes(), arm_signature.read_bytes()
        )


def test_x86_64_directory_does_not_offer_an_arm64_manifest():
    intel_dir = MACOS_UPDATES / "x86_64"

    assert (intel_dir / "README.md").is_file()
    assert not list(intel_dir.glob("*.json"))
    assert not list(intel_dir.glob("*.json.sig"))


def test_macos_script_architecture_helper_normalizes_supported_names():
    helper = REPOSITORY_ROOT / "scripts" / "macos" / "architecture.sh"
    command = (
        'source "$1"\n'
        "cv_normalize_macos_arch aarch64\n"
        "cv_normalize_macos_arch amd64\n"
        "if cv_normalize_macos_arch unsupported; then exit 9; fi\n"
    )

    completed = subprocess.run(
        ["bash", "-c", command, "bash", str(helper)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.splitlines() == ["arm64", "x86_64"]


def test_macos_release_scripts_use_architecture_specific_contracts():
    packager = (
        REPOSITORY_ROOT / "scripts" / "macos" / "package_macos_dmg.sh"
    ).read_text(encoding="utf-8")
    publisher = (
        REPOSITORY_ROOT / "scripts" / "macos" / "publish.sh"
    ).read_text(encoding="utf-8")
    manifest_writer = (
        REPOSITORY_ROOT / "scripts" / "macos" / "update_manifest.sh"
    ).read_text(encoding="utf-8")

    assert "CaveViewer-${version}-macos-${macos_arch}.dmg" in packager
    assert '"architecture": "$macos_arch"' in packager
    assert "updates/macos/$macos_arch/$manifest_channel.json" in publisher
    assert 'if [ "$macos_arch" = "arm64" ]; then' in publisher
    assert "updates/macos/$macos_arch/$channel.json" in manifest_writer
    assert '"architecture": "$macos_arch"' in manifest_writer
