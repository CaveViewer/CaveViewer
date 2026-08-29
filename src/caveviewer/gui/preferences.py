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
    PreferenceDefaultContext,
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
    default_preference_context,
    default_preferences,
    default_map_library_dir,
    default_recording_dir,
    normalize_preferences,
    require_validated_preferences,
    resolve_preferences,
    validate_preference,
    validate_preferences,
)
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.preference_paths import (
    migrate_preference_file,
    preference_file,
)


_LOG = get_logger("Preferences")
PREFERENCES_FILENAME = "preferences.json"
PREVIOUS_PREFERENCES_FILENAME = "advanced_settings.json"


class PreferencesSaveError(OSError):
    pass


def preferences_file() -> str:
    """Return the sole current application-preferences path."""

    return preference_file(PREFERENCES_FILENAME)


def preferences_load_file() -> str:
    """Return the current path or a readable failed-migration fallback."""

    target = preferences_file()
    readable_path = migrate_preference_file(
        PREFERENCES_FILENAME,
        PREVIOUS_PREFERENCES_FILENAME,
    )
    if os.path.abspath(readable_path) != os.path.abspath(target):
        _LOG.warning(
            "Could not rename preferences from %s to %s; using the old file "
            "for this run.",
            readable_path,
            target,
        )
    return readable_path


def load_preferences(
    preferences_path: str | os.PathLike[str] | None = None,
) -> Preferences:
    return resolve_preferences(load_saved_preference_values(preferences_path))


def load_saved_preference_values(
    preferences_path: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Return the un-resolved persisted preference payload.

    Application composition needs the raw payload so the runtime-settings
    resolver can preserve ``saved preference > environment > built-in``
    provenance.  The Preferences UI should continue to use
    :func:`load_preferences`, which supplies displayable defaults.
    """
    path = (
        Path(preferences_path)
        if preferences_path is not None
        else Path(preferences_load_file())
    )
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except Exception as exc:
        if not isinstance(exc, FileNotFoundError):
            _LOG.warning("Could not load preferences from %s: %s", path, exc)
        payload = None
    return dict(payload) if isinstance(payload, Mapping) else {}


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
