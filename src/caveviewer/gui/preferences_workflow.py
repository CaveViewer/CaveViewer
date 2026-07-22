"""Tk-free Preferences dialog persistence workflow."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from caveviewer.gui.preferences import (
    Preferences,
    PreferencesSaveError,
    apply_preferences_to_env,
    load_preferences,
    save_preferences,
)


@dataclass(frozen=True)
class PreferencesApplyResult:
    """Outcome of saving a Preferences snapshot from the dialog."""

    preferences: Preferences | None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.preferences is not None and self.error is None


class PreferencesDialogWorkflow:
    """Coordinate Preferences load/save side effects outside Tk presentation."""

    def __init__(
        self,
        *,
        load_preferences_fn: Callable[[], Preferences] = load_preferences,
        save_preferences_fn: Callable[[Preferences], None] = save_preferences,
        apply_preferences_to_env_fn: Callable[
            [Preferences], None
        ] = apply_preferences_to_env,
    ) -> None:
        self._load_preferences = load_preferences_fn
        self._save_preferences = save_preferences_fn
        self._apply_preferences_to_env = apply_preferences_to_env_fn

    def load_initial(self) -> Preferences:
        preferences = self._load_preferences()
        self._apply_preferences_to_env(preferences)
        return preferences

    def apply(self, preferences: Preferences) -> PreferencesApplyResult:
        try:
            self._save_preferences(preferences)
        except PreferencesSaveError as exc:
            return PreferencesApplyResult(preferences=None, error=str(exc))
        self._apply_preferences_to_env(preferences)
        return PreferencesApplyResult(preferences=preferences)
