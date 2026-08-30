"""Unit tests for the Tk-free Preferences dialog workflow."""

from pathlib import Path

from caveviewer.core.preferences.transfer import (
    PreferencesImportResult,
    PreferencesTransferError,
)

from caveviewer.gui import preferences as settings
from caveviewer.gui.preferences_workflow import (
    PreferencesApplyResult,
    PreferencesDialogWorkflow,
    PreferencesExportWorkflowResult,
    PreferencesImportWorkflowResult,
    PreferencesCloseAction,
    PreferencesCloseChoice,
    resolve_preferences_close,
)


def test_preferences_close_resolution_covers_clean_and_dirty_choices():
    assert resolve_preferences_close(False) is PreferencesCloseAction.LEAVE
    assert resolve_preferences_close(True) is PreferencesCloseAction.PROMPT
    assert (
        resolve_preferences_close(True, PreferencesCloseChoice.SAVE)
        is PreferencesCloseAction.SAVE
    )
    assert (
        resolve_preferences_close(True, PreferencesCloseChoice.DISCARD)
        is PreferencesCloseAction.DISCARD
    )
    assert (
        resolve_preferences_close(True, PreferencesCloseChoice.KEEP_EDITING)
        is PreferencesCloseAction.STAY
    )


def test_preferences_workflow_loads_initial_preferences_without_notifying_save():
    preferences = settings.preference_defaults()
    loaded = []
    applied = []
    workflow = PreferencesDialogWorkflow(
        load_preferences_fn=lambda: loaded.append(True) or preferences,
        save_preferences_fn=lambda _preferences: None,
        on_preferences_saved=applied.append,
    )

    assert workflow.load_initial() is preferences

    assert loaded == [True]
    assert applied == []


def test_preferences_workflow_saves_before_notifying_snapshot_owner():
    preferences = settings.preference_defaults()
    calls = []
    workflow = PreferencesDialogWorkflow(
        load_preferences_fn=lambda: preferences,
        save_preferences_fn=lambda value: calls.append(("save", value)),
        on_preferences_saved=lambda value: calls.append(("saved", value)),
    )

    result = workflow.apply(preferences)

    assert result == PreferencesApplyResult(preferences=preferences)
    assert result.succeeded
    assert calls == [("save", preferences), ("saved", preferences)]


def test_preferences_workflow_does_not_notify_after_save_failure():
    preferences = settings.preference_defaults()
    applied = []
    workflow = PreferencesDialogWorkflow(
        load_preferences_fn=lambda: preferences,
        save_preferences_fn=lambda _value: (_ for _ in ()).throw(
            settings.PreferencesSaveError("Could not save settings.")
        ),
        on_preferences_saved=applied.append,
    )

    result = workflow.apply(preferences)

    assert result == PreferencesApplyResult(
        preferences=None,
        error="Could not save settings.",
    )
    assert not result.succeeded
    assert applied == []


def test_preferences_workflow_import_is_transactional(caplog):
    preferences = settings.load_preferences()
    imported = []
    workflow = PreferencesDialogWorkflow(
        import_preferences_fn=lambda path, **kwargs: imported.append(
            (path, kwargs["current_preferences"])
        )
        or PreferencesImportResult(
            preferences=preferences,
            defaulted_keys=("io_workers",),
            ignored_keys=("future",),
            excluded_keys=("recording_dir", "map_library_dir"),
        )
    )

    with caplog.at_level("INFO", logger="caveviewer"):
        result = workflow.import_file("shared.json", preferences)

    assert result == PreferencesImportWorkflowResult(
        preferences=preferences,
        defaulted_keys=("io_workers",),
        ignored_keys=("future",),
        excluded_keys=("recording_dir", "map_library_dir"),
    )
    assert imported == [("shared.json", preferences)]
    assert "Importing preferences from shared.json." in caplog.text
    assert "platform-specific preference recording_dir" in caplog.text
    assert "platform-specific preference map_library_dir" in caplog.text
    assert result.succeeded


def test_preferences_workflow_import_failure_has_no_snapshot():
    workflow = PreferencesDialogWorkflow(
        import_preferences_fn=lambda _path, **_kwargs: (_ for _ in ()).throw(
            PreferencesTransferError("Malformed preferences file.")
        )
    )

    result = workflow.import_file("broken.json", settings.load_preferences())

    assert result == PreferencesImportWorkflowResult(
        preferences=None,
        error="Malformed preferences file.",
    )
    assert not result.succeeded


def test_preferences_workflow_exports_without_applying():
    preferences = settings.load_preferences()
    exported = []
    applied = []
    workflow = PreferencesDialogWorkflow(
        export_preferences_fn=lambda path, value: exported.append((path, value)),
        on_preferences_saved=applied.append,
    )

    result = workflow.export_file("shared.json", preferences)

    assert result == PreferencesExportWorkflowResult(path=Path("shared.json"))
    assert result.succeeded
    assert exported == [("shared.json", preferences)]
    assert applied == []


def test_preferences_workflow_reports_export_failure():
    workflow = PreferencesDialogWorkflow(
        export_preferences_fn=lambda _path, _value: (_ for _ in ()).throw(
            PreferencesTransferError("Could not export preferences.")
        )
    )

    result = workflow.export_file("shared.json", settings.load_preferences())

    assert result == PreferencesExportWorkflowResult(
        path=None,
        error="Could not export preferences.",
    )
    assert not result.succeeded
