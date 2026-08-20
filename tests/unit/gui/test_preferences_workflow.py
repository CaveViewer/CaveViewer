"""Unit tests for the Tk-free Preferences dialog workflow."""

from caveviewer.gui import preferences as settings
from caveviewer.gui.preferences_workflow import (
    PreferencesApplyResult,
    PreferencesDialogWorkflow,
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
