"""Tk-free interaction state for the Preferences form."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Mapping

from caveviewer.gui.preferences import (
    PREFERENCE_FIELDS,
    Preferences,
    PreferenceSpec,
    normalize_preferences,
    validate_preference,
    validate_preferences,
)


class MessageKind(str, Enum):
    NONE = "none"
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class PreferencesFormState:
    values: Mapping[str, str]
    focused_key: str | None
    invalid_key: str | None
    message: str
    message_kind: MessageKind
    apply_enabled: bool
    form_locked: bool


class PreferencesFormController:
    """Process form events and expose the resulting render state."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._field_specs = {
            field.key: field for field in PREFERENCE_FIELDS
        }
        self._values = normalize_preferences(dict(values))
        self._focused_key: str | None = None
        self._state = self._validate()

    @property
    def state(self) -> PreferencesFormState:
        return self._state

    def focus(self, key: str) -> PreferencesFormState:
        self._require_key(key)
        self._focused_key = key
        self._state = replace(
            self._state,
            values=dict(self._values),
            focused_key=key,
        )
        return self._state

    def change(self, key: str, raw_value: object) -> PreferencesFormState:
        field = self._require_key(key)
        value = str(raw_value) if raw_value is not None else ""
        self._values[key] = value

        if (
            self._focused_key == key
            and not field.optional
            and not value.strip()
        ):
            self._state = self._advisory_state()
        else:
            self._state = self._validate(preferred_key=key)
        return self._state

    def blur(self, key: str) -> PreferencesFormState:
        field = self._require_key(key)
        if self._focused_key == key:
            self._focused_key = None

        result = validate_preference(field, self._values[key])
        if result.is_valid:
            self._values[key] = result.normalized_value
        self._state = self._validate(preferred_key=key)
        return self._state

    def attempt_apply(
        self,
    ) -> tuple[PreferencesFormState, Preferences | None]:
        result = validate_preferences(self._values)
        if not result.is_valid or result.preferences is None:
            self._state = self._error_state(
                result.error_key, result.message or "Invalid preferences."
            )
            return self._state, None

        self._values = result.preferences.as_dict()
        self._state = self._advisory_state()
        return self._state, result.preferences

    def _require_key(self, key: str) -> PreferenceSpec:
        try:
            return self._field_specs[key]
        except KeyError as exc:
            raise KeyError(f"Unknown Preferences field: {key}") from exc

    def _has_missing_required_value(self) -> bool:
        return any(
            not field.optional
            and not self._values[field.key].strip()
            for field in PREFERENCE_FIELDS
        )

    def _advisory_state(self) -> PreferencesFormState:
        return PreferencesFormState(
            values=dict(self._values),
            focused_key=self._focused_key,
            invalid_key=None,
            message="",
            message_kind=MessageKind.NONE,
            apply_enabled=not self._has_missing_required_value(),
            form_locked=False,
        )

    def _error_state(
        self, invalid_key: str | None, message: str
    ) -> PreferencesFormState:
        return PreferencesFormState(
            values=dict(self._values),
            focused_key=self._focused_key,
            invalid_key=invalid_key,
            message=message,
            message_kind=MessageKind.ERROR,
            apply_enabled=False,
            form_locked=True,
        )

    def _validate(
        self, preferred_key: str | None = None
    ) -> PreferencesFormState:
        if preferred_key is not None:
            field = self._require_key(preferred_key)
            result = validate_preference(field, self._values[preferred_key])
            if not result.is_valid:
                return self._error_state(
                    preferred_key,
                    result.message or "Invalid preferences.",
                )

        result = validate_preferences(self._values)
        if not result.is_valid:
            return self._error_state(
                result.error_key,
                result.message or "Invalid preferences.",
            )
        return self._advisory_state()
