"""Resolve and migrate CaveViewer configuration and UI state files."""

from __future__ import annotations

import os
import shutil
import tempfile

from caveviewer.storage_paths import resolve_application_paths


PREFERENCES_DIRNAME = ".caveviewer"


def preferences_dir() -> str:
    """Return the configuration directory, creating it if needed."""
    path = resolve_application_paths().config_dir
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def state_dir() -> str:
    """Return the nonessential UI-state directory, creating it if needed."""
    path = resolve_application_paths().state_dir
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


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
    return _migrate_user_file(
        os.path.join(preferences_dir(), filename), filename, legacy_filename
    )


def migrate_state_file(filename: str, legacy_filename: str) -> str:
    """Move remembered UI state to XDG state while preserving old reads."""
    return _migrate_user_file(
        os.path.join(state_dir(), filename), filename, legacy_filename
    )


def write_text_atomic(path: str, value: str) -> None:
    """Atomically replace a small preference/state text file."""
    staging_path = None
    try:
        descriptor, staging_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            dir=os.path.dirname(path),
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staging_path, path)
        staging_path = None
    finally:
        if staging_path and os.path.exists(staging_path):
            os.remove(staging_path)


def _migrate_user_file(
    new_path: str, previous_filename: str, legacy_filename: str
) -> str:
    if os.path.exists(new_path):
        return new_path

    legacy_root = os.path.join(os.path.expanduser("~"), PREFERENCES_DIRNAME)
    candidates = (
        os.path.join(legacy_root, previous_filename),
        legacy_preference_file(legacy_filename),
    )
    for old_path in candidates:
        if os.path.abspath(old_path) == os.path.abspath(new_path):
            continue
        if not os.path.isfile(old_path):
            continue
        try:
            # Copy through a sibling and atomically replace so an interrupted
            # first launch never publishes a partial preference file.
            descriptor, staging_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(new_path)}.",
                suffix=".tmp",
                dir=os.path.dirname(new_path),
            )
            os.close(descriptor)
            try:
                shutil.copy2(old_path, staging_path)
                os.replace(staging_path, new_path)
            finally:
                if os.path.exists(staging_path):
                    os.remove(staging_path)
            break
        except Exception:
            pass
    return new_path
