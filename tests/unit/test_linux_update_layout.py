"""Repository contracts for Linux update-manifest layout."""

from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LINUX_UPDATES = REPOSITORY_ROOT / "updates" / "linux"


def test_linux_update_layout_is_x86_64_only():
    assert (LINUX_UPDATES / "x86_64" / "stable.json").exists()
    assert (LINUX_UPDATES / "x86_64" / "stable.json.sig").exists()
    assert (LINUX_UPDATES / "x86_64" / "prerelease.json").exists()
    assert (LINUX_UPDATES / "x86_64" / "prerelease.json.sig").exists()

    assert not any((LINUX_UPDATES / "arm64").glob("*"))
