"""Portable, bounded import and export for preference snapshots."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from caveviewer.core.preferences.schema import (
    PREFERENCE_FIELDS,
    Preferences,
    preference_defaults,
    validate_preference,
)


PREFERENCES_EXPORT_FILENAME = "preferences.json"
MAX_PREFERENCES_FILE_BYTES = 256 * 1024


class PreferencesTransferError(ValueError):
    """A portable preferences file could not be read, validated, or written."""


@dataclass(frozen=True)
class PreferencesImportResult:
    """Resolved imported preferences and fields replaced with defaults."""

    preferences: Preferences
    defaulted_keys: tuple[str, ...]
    ignored_keys: tuple[str, ...]
    excluded_keys: tuple[str, ...] = ()


def encode_preferences(preferences: Preferences) -> bytes:
    """Return one deterministic UTF-8 portable preference document."""

    if not isinstance(preferences, Preferences):
        raise TypeError("encode_preferences requires a Preferences snapshot")
    payload = {
        field.key: preferences[field.key]
        for field in PREFERENCE_FIELDS
    }
    return (
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def decode_preferences(
    document: bytes,
    *,
    max_bytes: int = MAX_PREFERENCES_FILE_BYTES,
    current_preferences: Preferences | None = None,
) -> PreferencesImportResult:
    """Parse a bounded document and default invalid or missing fields."""

    limit = max(1, int(max_bytes))
    if len(document) > limit:
        raise PreferencesTransferError(
            f"Preferences file is larger than {limit} bytes."
        )
    try:
        payload = json.loads(document.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreferencesTransferError(
            "Preferences file is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(payload, Mapping):
        raise PreferencesTransferError("Preferences file must contain a JSON object.")

    defaults = preference_defaults()
    destination = (
        current_preferences.as_dict()
        if current_preferences is not None
        else defaults
    )
    resolved: dict[str, str] = {}
    defaulted_keys: list[str] = []
    excluded_keys: list[str] = []
    declared_keys = {field.key for field in PREFERENCE_FIELDS}
    for field in PREFERENCE_FIELDS:
        if not field.portable:
            resolved[field.key] = destination[field.key]
            if field.key in payload:
                excluded_keys.append(field.key)
            continue
        if field.key not in payload:
            resolved[field.key] = defaults[field.key]
            defaulted_keys.append(field.key)
            continue
        result = validate_preference(field, payload[field.key])
        if result.is_valid:
            resolved[field.key] = result.normalized_value
        else:
            resolved[field.key] = defaults[field.key]
            defaulted_keys.append(field.key)

    ignored_keys = tuple(
        sorted(str(key) for key in payload if key not in declared_keys)
    )
    return PreferencesImportResult(
        preferences=Preferences(resolved),
        defaulted_keys=tuple(defaulted_keys),
        ignored_keys=ignored_keys,
        excluded_keys=tuple(excluded_keys),
    )


def load_preferences_file(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = MAX_PREFERENCES_FILE_BYTES,
    current_preferences: Preferences | None = None,
) -> PreferencesImportResult:
    """Read no more than the configured bound from a portable file."""

    source = Path(path)
    limit = max(1, int(max_bytes))
    try:
        with source.open("rb") as input_file:
            document = input_file.read(limit + 1)
    except OSError as exc:
        raise PreferencesTransferError(
            f"Could not read preferences from {source}."
        ) from exc
    return decode_preferences(
        document,
        max_bytes=limit,
        current_preferences=current_preferences,
    )


def save_preferences_file(
    path: str | os.PathLike[str],
    preferences: Preferences,
) -> None:
    """Atomically write a portable preference document beside its target."""

    target = Path(path)
    document = encode_preferences(preferences)
    staging_path: Path | None = None
    try:
        descriptor, staging_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        staging_path = Path(staging_name)
        with os.fdopen(descriptor, "wb") as output_file:
            output_file.write(document)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(staging_path, target)
        staging_path = None
    except OSError as exc:
        raise PreferencesTransferError(
            f"Could not export preferences to {target}."
        ) from exc
    finally:
        if staging_path is not None:
            try:
                staging_path.unlink(missing_ok=True)
            except OSError:
                pass
