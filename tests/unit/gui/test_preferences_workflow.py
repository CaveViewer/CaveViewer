"""Unit tests for the Tk-free Preferences dialog workflow."""

from caveviewer.gui import preferences as settings
from caveviewer.gui.preferences_workflow import (
    PreferencesApplyResult,
    PreferencesDialogWorkflow,
)


def test_preferences_workflow_loads_and_applies_initial_preferences():
    preferences = settings.preference_defaults()
    loaded = []
    applied = []
    workflow = PreferencesDialogWorkflow(
        load_preferences_fn=lambda: loaded.append(True) or preferences,
        save_preferences_fn=lambda _preferences: None,
        apply_preferences_to_env_fn=applied.append,
    )

    assert workflow.load_initial() is preferences

    assert loaded == [True]
    assert applied == [preferences]


def test_preferences_workflow_saves_before_applying_preferences():
    preferences = settings.preference_defaults()
    calls = []
    workflow = PreferencesDialogWorkflow(
        load_preferences_fn=lambda: preferences,
        save_preferences_fn=lambda value: calls.append(("save", value)),
        apply_preferences_to_env_fn=lambda value: calls.append(("apply", value)),
    )

    result = workflow.apply(preferences)

    assert result == PreferencesApplyResult(preferences=preferences)
    assert result.succeeded
    assert calls == [("save", preferences), ("apply", preferences)]


def test_preferences_workflow_does_not_apply_after_save_failure():
    preferences = settings.preference_defaults()
    applied = []
    workflow = PreferencesDialogWorkflow(
        load_preferences_fn=lambda: preferences,
        save_preferences_fn=lambda _value: (_ for _ in ()).throw(
            settings.PreferencesSaveError("Could not save settings.")
        ),
        apply_preferences_to_env_fn=applied.append,
    )

    result = workflow.apply(preferences)

    assert result == PreferencesApplyResult(
        preferences=None,
        error="Could not save settings.",
    )
    assert not result.succeeded
    assert applied == []
