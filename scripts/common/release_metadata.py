"""Helpers for comparing release-specific AppStream metadata."""

from __future__ import annotations

import xml.etree.ElementTree as ET


def appstream_release_entries(text: str) -> tuple[str, ...]:
    """Return serialized entries from an AppStream ``releases`` element."""
    releases = ET.fromstring(text).find("releases")
    if releases is None:
        raise ValueError("AppStream metadata has no releases element.")
    return tuple(ET.tostring(entry, encoding="unicode") for entry in releases)


def appstream_releases_changed(base_text: str, head_text: str) -> bool:
    """Report whether the AppStream release history changed."""
    return appstream_release_entries(base_text) != appstream_release_entries(head_text)
