"""Tk-free Preferences dialog persistence workflow."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from caveviewer.gui.preferences import (
    Preferences,
    PreferencesSaveError,
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
        on_preferences_saved: Callable[[Preferences], None] | None = None,
    ) -> None:
        self._load_preferences = load_preferences_fn
        self._save_preferences = save_preferences_fn
        self._on_preferences_saved = on_preferences_saved

    def load_initial(self) -> Preferences:
        return self._load_preferences()

    def apply(self, preferences: Preferences) -> PreferencesApplyResult:
        try:
            self._save_preferences(preferences)
        except PreferencesSaveError as exc:
            return PreferencesApplyResult(preferences=None, error=str(exc))
        if self._on_preferences_saved is not None:
            self._on_preferences_saved(preferences)
        return PreferencesApplyResult(preferences=preferences)
