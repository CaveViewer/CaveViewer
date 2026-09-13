"""Verify persisted Help choices, restart behavior, and retained Preferences state."""

import logging

import pytest

from caveviewer.gui.log_information import LogInformationController
from caveviewer.gui.preferences import (
    Preferences, PreferencesSaveError, load_preferences, save_preferences,
)
from caveviewer.gui.preferences_dialog import PreferencesPanel
from caveviewer.gui.preferences_form import PreferencesFormController
from caveviewer.core.preferences.runtime_settings import RuntimePlatformFacts, resolve_runtime_settings


def test_default_and_saved_choice_survive_restart_and_preserve_latest_preferences(tmp_path):
    path = tmp_path / "preferences.json"
    load = lambda: load_preferences(path)
    save = lambda preferences: save_preferences(preferences, path)
    notifications = []
    controller = LogInformationController(load=load, save=save, on_saved=notifications.append)
    assert controller.refresh().value == "essential"
    latest = Preferences({**load(), "io_workers": "7"})
    save(latest)
    current_log_level = logging.getLogger().level
    assert controller.select("all").error == ""
    assert load()["io_workers"] == "7"
    assert notifications[-1]["log_information"] == "all"
    assert logging.getLogger().level == current_log_level
    restarted = LogInformationController(load=load, save=save)
    assert restarted.refresh().value == "all"
    runtime = resolve_runtime_settings(
        preferences=load(), environ={},
        platform=RuntimePlatformFacts(platform_name="win32", os_name="nt", home=tmp_path),
    )
    assert runtime["log_level"] == "DEBUG"
    assert restarted.select("essential").value == "essential"
    assert load()["log_information"] == "essential"


def test_failed_save_keeps_previous_choice_and_does_not_notify(tmp_path):
    path = tmp_path / "preferences.json"
    original = load_preferences(path)
    notifications = []

    def fail(_preferences):
        raise PreferencesSaveError("disk unavailable")

    controller = LogInformationController(
        load=lambda: load_preferences(path), save=fail, on_saved=notifications.append,
    )
    controller.refresh()
    state = controller.select("all")
    assert state.value == "essential"
    assert "Couldn’t save" in state.error
    assert load_preferences(path) == original
    assert notifications == []
    with pytest.raises(ValueError):
        controller.select("verbose")


def test_help_save_updates_retained_preferences_without_losing_other_staged_edits(valid_preferences):
    panel = PreferencesPanel.__new__(PreferencesPanel)
    panel.preferences = Preferences(valid_preferences)
    panel.form = PreferencesFormController(valid_preferences)
    panel.form.change("io_workers", "7")
    panel._render_form_state = lambda _state: None
    panel.accept_log_information("all")
    assert panel.form.state.dirty_keys == frozenset({"io_workers"})
    _, to_save = panel.form.attempt_apply()
    assert to_save["log_information"] == "all"
    panel.form.discard()
    assert panel.form.state.values["log_information"] == "all"
    assert panel.preferences["log_information"] == "all"
