"""Persist recently opened map folders for the splash map library."""

from __future__ import annotations

import json
import os

from caveviewer.gui.preference_paths import migrate_state_file, write_text_atomic
from caveviewer.gui.sample_maps import is_app_supplied_sample_map_path


def _recent_map_paths_file() -> str:
    return migrate_state_file("recent_map_paths", ".caveviewer_recent_map_paths")


def _normalized_existing_directory(path: str) -> str | None:
    try:
        if not path:
            return None
        normalized = os.path.abspath(os.path.expanduser(os.fspath(path)))
        if os.path.isdir(normalized):
            return normalized
    except (OSError, TypeError):
        return None
    return None


def load_recent_map_paths() -> list[str]:
    """Return existing recently opened map folders, newest first."""
    try:
        with open(_recent_map_paths_file(), "r", encoding="utf-8") as f:
            payload = json.loads(f.read() or "[]")
    except Exception:
        return []
    if not isinstance(payload, list):
        return []

    paths: list[str] = []
    seen: set[str] = set()
    for raw_path in payload:
        if not isinstance(raw_path, str):
            continue
        normalized = _normalized_existing_directory(raw_path)
        if (
            normalized is None
            or normalized in seen
            or is_app_supplied_sample_map_path(normalized)
        ):
            continue
        paths.append(normalized)
        seen.add(normalized)
    return paths


def remember_recent_map_path(path: str) -> None:
    """Persist a successfully opened map folder as the newest recent map."""
    normalized = _normalized_existing_directory(path)
    if normalized is None or is_app_supplied_sample_map_path(normalized):
        return

    paths = [
        normalized,
        *[
            recent_path
            for recent_path in load_recent_map_paths()
            if recent_path != normalized
        ],
    ]
    try:
        write_text_atomic(_recent_map_paths_file(), json.dumps(paths, indent=2) + "\n")
    except Exception:
        pass
