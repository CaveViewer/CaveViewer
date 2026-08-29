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


def migrate_preference_file(filename: str, previous_filename: str) -> str:
    """Rename one sibling preference file, returning a readable fallback."""

    directory = preferences_dir()
    target = os.path.join(directory, filename)
    if os.path.exists(target):
        return target

    previous = os.path.join(directory, previous_filename)
    if not os.path.isfile(previous):
        return target
    try:
        # The source remains readable if the same-directory rename fails, so
        # startup can retain user settings and retry on the next launch.
        os.rename(previous, target)
    except OSError:
        return previous
    return target


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
        os.path.join(os.path.expanduser("~"), legacy_filename),
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
