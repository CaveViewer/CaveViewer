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
from caveviewer.core.preferences.transfer import MAX_PREFERENCES_FILE_BYTES
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
    path = (
        Path(preferences_path)
        if preferences_path is not None
        else Path(preferences_load_file())
    )
    if not path.exists():
        defaults = resolve_preferences()
        _LOG.warning(
            "Preferences file %s was not found; using and saving defaults.",
            path.name,
        )
        try:
            save_preferences(defaults, path)
        except PreferencesSaveError:
            # Saving reports its own warning; startup must still have a valid
            # in-memory snapshot when the configuration location is unwritable.
            pass
        return defaults
    return resolve_preferences(load_saved_preference_values(path))


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
        with path.open("rb") as file_obj:
            document = file_obj.read(MAX_PREFERENCES_FILE_BYTES + 1)
    except OSError:
        _LOG.warning(
            "Could not load preferences file %s (read error); using defaults.",
            path.name,
        )
        return {}
    if len(document) > MAX_PREFERENCES_FILE_BYTES:
        _LOG.warning(
            "Could not load preferences file %s (file is too large); using defaults.",
            path.name,
        )
        return {}
    try:
        payload = json.loads(document.decode("utf-8"))
    except UnicodeDecodeError:
        _LOG.warning(
            "Could not load preferences file %s (invalid UTF-8); using defaults.",
            path.name,
        )
        return {}
    except json.JSONDecodeError:
        _LOG.warning(
            "Could not load preferences file %s (malformed JSON); using defaults.",
            path.name,
        )
        return {}
    if isinstance(payload, Mapping):
        _LOG.info("Loaded preferences from %s.", path.name)
        return dict(payload)
    _LOG.warning(
        "Could not load preferences file %s (JSON root is not an object); "
        "using defaults.",
        path.name,
    )
    return {}


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
