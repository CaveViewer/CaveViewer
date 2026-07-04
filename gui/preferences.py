"""
Helpers for CaveViewer user preference files.

Historically these lived as separate ~/.caveviewer_* files. New preference
files live under ~/.caveviewer/ so the app can grow without cluttering the
user's home directory.
"""

from __future__ import annotations

import os
import shutil


PREFERENCES_DIRNAME = ".caveviewer"


def preferences_dir() -> str:
    """Return the CaveViewer preferences directory, creating it if needed."""
    path = os.path.join(os.path.expanduser("~"), PREFERENCES_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def preference_file(filename: str) -> str:
    """Return a path inside the CaveViewer preferences directory."""
    return os.path.join(preferences_dir(), filename)


def legacy_preference_file(filename: str) -> str:
    """Return the old home-dotfile path for migration/fallback reads."""
    return os.path.join(os.path.expanduser("~"), filename)


def migrate_preference_file(filename: str, legacy_filename: str) -> str:
    """
    Return the new preference path and copy the legacy file there if needed.

    Migration is best-effort: callers should still handle read/write errors
    because the home directory or preference path can be unusual on some
    systems.
    """
    new_path = preference_file(filename)
    old_path = legacy_preference_file(legacy_filename)
    if not os.path.exists(new_path) and os.path.isfile(old_path):
        try:
            shutil.copy2(old_path, new_path)
        except Exception:
            pass
    return new_path
