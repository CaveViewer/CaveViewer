"""Tk-free Preferences dialog persistence workflow."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from caveviewer.core.preferences.transfer import (
    PreferencesImportResult,
    PreferencesTransferError,
    load_preferences_file,
    save_preferences_file,
)
from caveviewer.gui.preferences import (
    Preferences,
    PreferencesSaveError,
    load_preferences,
    save_preferences,
)


class PreferencesCloseChoice(str, Enum):
    """Explicit user choice when leaving Preferences with staged changes."""

    SAVE = "save"
    DISCARD = "discard"
    KEEP_EDITING = "keep_editing"


class PreferencesCloseAction(str, Enum):
    """Side effect requested by one Preferences close interaction."""

    LEAVE = "leave"
    PROMPT = "prompt"
    SAVE = "save"
    DISCARD = "discard"
    STAY = "stay"


def resolve_preferences_close(
    has_unsaved_changes: bool,
    choice: PreferencesCloseChoice | None = None,
) -> PreferencesCloseAction:
    """Resolve close intent without performing persistence or Tk operations."""
    if not has_unsaved_changes:
        return PreferencesCloseAction.LEAVE
    if choice is None:
        return PreferencesCloseAction.PROMPT
    return {
        PreferencesCloseChoice.SAVE: PreferencesCloseAction.SAVE,
        PreferencesCloseChoice.DISCARD: PreferencesCloseAction.DISCARD,
        PreferencesCloseChoice.KEEP_EDITING: PreferencesCloseAction.STAY,
    }[choice]


@dataclass(frozen=True)
class PreferencesApplyResult:
    """Outcome of saving a Preferences snapshot from the dialog."""

    preferences: Preferences | None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.preferences is not None and self.error is None


@dataclass(frozen=True)
class PreferencesImportWorkflowResult:
    """Transactional result of reading one portable preferences file."""

    preferences: Preferences | None
    defaulted_keys: tuple[str, ...] = ()
    ignored_keys: tuple[str, ...] = ()
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.preferences is not None and self.error is None


@dataclass(frozen=True)
class PreferencesExportWorkflowResult:
    """Result of atomically exporting one preference snapshot."""

    path: Path | None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.path is not None and self.error is None


class PreferencesDialogWorkflow:
    """Coordinate Preferences load/save side effects outside Tk presentation."""

    def __init__(
        self,
        *,
        load_preferences_fn: Callable[[], Preferences] = load_preferences,
        save_preferences_fn: Callable[[Preferences], None] = save_preferences,
        import_preferences_fn: Callable[
            [str], PreferencesImportResult
        ] = load_preferences_file,
        export_preferences_fn: Callable[[str, Preferences], None] = (
            save_preferences_file
        ),
        on_preferences_saved: Callable[[Preferences], None] | None = None,
    ) -> None:
        self._load_preferences = load_preferences_fn
        self._save_preferences = save_preferences_fn
        self._import_preferences = import_preferences_fn
        self._export_preferences = export_preferences_fn
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

    def import_file(self, path: str) -> PreferencesImportWorkflowResult:
        """Read and resolve an import without changing persisted preferences."""

        try:
            result = self._import_preferences(path)
        except PreferencesTransferError as exc:
            return PreferencesImportWorkflowResult(
                preferences=None,
                error=str(exc),
            )
        return PreferencesImportWorkflowResult(
            preferences=result.preferences,
            defaulted_keys=result.defaulted_keys,
            ignored_keys=result.ignored_keys,
        )

    def export_file(
        self,
        path: str,
        preferences: Preferences,
    ) -> PreferencesExportWorkflowResult:
        """Write a portable snapshot without changing application state."""

        try:
            self._export_preferences(path, preferences)
        except PreferencesTransferError as exc:
            return PreferencesExportWorkflowResult(path=None, error=str(exc))
        return PreferencesExportWorkflowResult(path=Path(path))
