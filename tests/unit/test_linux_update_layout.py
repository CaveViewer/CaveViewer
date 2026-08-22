"""Repository contracts for Linux update-manifest layout."""

from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LINUX_UPDATES = REPOSITORY_ROOT / "updates" / "linux"


def test_linux_update_layout_is_x86_64_only():
    assert (LINUX_UPDATES / "x86_64" / "stable.json").exists()
    assert (LINUX_UPDATES / "x86_64" / "stable.json.sig").exists()

    preview_manifest = LINUX_UPDATES / "x86_64" / "preview.json"
    preview_signature = preview_manifest.with_name("preview.json.sig")
    assert preview_manifest.exists() is preview_signature.exists()

    assert not any((LINUX_UPDATES / "arm64").glob("*"))
