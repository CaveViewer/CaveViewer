"""GUI persistence facade for core preferences."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from caveviewer.core.preferences.schema import (
    PREFERENCE_FIELDS,
    Preferences,
    PreferencesValidationError,
    PreferenceDefaultProvider,
    PreferenceFieldValidationResult,
    PreferenceEnvConverter,
    PreferenceSpec,
    PreferencesValidationResult,
    PreferenceValueType,
    preference_defaults,
    preference_placeholder_text,
    preference_range_text,
    preference_env_updates,
    default_preferences,
    default_recording_dir,
    effective_preferences,
    normalize_preferences,
    require_validated_preferences,
    resolve_preferences,
    validate_preference,
    validate_preferences,
)
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.preference_paths import migrate_preference_file


_LOG = get_logger("Preferences")


class PreferencesSaveError(OSError):
    pass


def preferences_file() -> str:
    # Keep the existing JSON filename for compatibility; only the Python API
    # has moved to "preferences" terminology.
    return migrate_preference_file(
        "advanced_settings.json", ".caveviewer_advanced_settings.json"
    )


def load_preferences(
    preferences_path: str | os.PathLike[str] | None = None,
) -> Preferences:
    path = (
        Path(preferences_path)
        if preferences_path is not None
        else Path(preferences_file())
    )
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except Exception as exc:
        if not isinstance(exc, FileNotFoundError):
            _LOG.warning("Could not load preferences from %s: %s", path, exc)
        payload = None
    return resolve_preferences(payload if isinstance(payload, Mapping) else None)


def save_preferences(
    preferences: Preferences,
    preferences_path: str | os.PathLike[str] | None = None,
) -> None:
    if not isinstance(preferences, Preferences):
        raise TypeError("save_preferences requires a Preferences snapshot")

    path = (
        Path(preferences_path)
        if preferences_path is not None
        else Path(preferences_file())
    )
    temp_path: Path | None = None
    try:
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(temp_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_obj:
            json.dump(preferences.as_dict(), file_obj, indent=2)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_path, path)
        temp_path = None
    except Exception as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        _LOG.warning("Could not save preferences to %s: %s", path, exc)
        raise PreferencesSaveError(
            f"Could not save preferences to {path}."
        ) from exc


def apply_preferences_to_env(preferences: Preferences) -> None:
    for env_var, value in preference_env_updates(preferences).items():
        os.environ[env_var] = value
