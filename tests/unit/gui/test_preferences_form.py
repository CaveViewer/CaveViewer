"""Exercise form focus, validation, locking, normalization, and apply behavior."""

from __future__ import annotations

from caveviewer.gui.preferences_form import (
    PreferencesFormController,
    MessageKind,
)


def test_valid_form_starts_unlocked_with_apply_enabled(valid_preferences):
    controller = PreferencesFormController(valid_preferences)

    assert controller.state.invalid_key is None
    assert controller.state.message_kind is MessageKind.NONE
    assert controller.state.apply_enabled
    assert not controller.state.form_locked


def test_invalid_initial_values_start_locked(valid_preferences):
    valid_preferences["io_workers"] = ""

    controller = PreferencesFormController(valid_preferences)
    state = controller.blur("io_workers")

    assert state.invalid_key == "io_workers"
    assert state.message_kind is MessageKind.ERROR
    assert not state.apply_enabled
    assert state.form_locked


def test_focused_required_field_can_be_temporarily_empty(valid_preferences):
    controller = PreferencesFormController(valid_preferences)
    controller.focus("io_workers")

    state = controller.change("io_workers", "")

    assert state.focused_key == "io_workers"
    assert state.invalid_key is None
    assert state.message == ""
    assert state.message_kind is MessageKind.NONE
    assert not state.apply_enabled
    assert not state.form_locked


def test_empty_required_field_locks_form_after_focus_leaves(valid_preferences):
    controller = PreferencesFormController(valid_preferences)
    controller.focus("io_workers")
    controller.change("io_workers", "")

    state = controller.blur("io_workers")

    assert state.focused_key is None
    assert state.invalid_key == "io_workers"
    assert state.message_kind is MessageKind.ERROR
    assert "required" in state.message
    assert not state.apply_enabled
    assert state.form_locked


def test_correcting_locked_field_unlocks_form(valid_preferences):
    controller = PreferencesFormController(valid_preferences)
    controller.focus("io_workers")
    controller.change("io_workers", "")
    controller.blur("io_workers")
    controller.focus("io_workers")

    state = controller.change("io_workers", "4")

    assert state.invalid_key is None
    assert state.apply_enabled
    assert not state.form_locked


def test_nonempty_out_of_range_value_is_rejected_immediately(
    valid_preferences,
):
    controller = PreferencesFormController(valid_preferences)
    controller.focus("io_workers")

    state = controller.change("io_workers", "33")

    assert state.invalid_key == "io_workers"
    assert state.message_kind is MessageKind.ERROR
    assert "no more than 32" in state.message
    assert state.form_locked


def test_high_worker_count_is_valid_without_bottom_warning(valid_preferences):
    controller = PreferencesFormController(valid_preferences)
    controller.focus("io_workers")

    state = controller.change("io_workers", "6")

    assert state.invalid_key is None
    assert state.message == ""
    assert state.message_kind is MessageKind.NONE
    assert state.apply_enabled
    assert not state.form_locked


def test_focus_loss_normalizes_valid_value(valid_preferences):
    controller = PreferencesFormController(valid_preferences)
    controller.focus("io_workers")
    controller.change("io_workers", "006")

    state = controller.blur("io_workers")

    assert state.values["io_workers"] == "6"
    assert state.message_kind is MessageKind.NONE


def test_optional_blank_value_keeps_apply_enabled(valid_preferences):
    controller = PreferencesFormController(valid_preferences)
    controller.focus("gpu_memory_gb")

    state = controller.change("gpu_memory_gb", "")

    assert state.apply_enabled
    assert not state.form_locked


def test_apply_returns_normalized_values(valid_preferences):
    controller = PreferencesFormController(valid_preferences)
    controller.focus("io_workers")
    controller.change("io_workers", "006")

    state, normalized = controller.attempt_apply()

    assert state.apply_enabled
    assert normalized is not None
    assert normalized["io_workers"] == "6"


def test_apply_rejects_missing_required_value_even_before_blur(
    valid_preferences,
):
    controller = PreferencesFormController(valid_preferences)
    controller.focus("io_workers")
    controller.change("io_workers", "")

    state, normalized = controller.attempt_apply()

    assert normalized is None
    assert state.invalid_key == "io_workers"
    assert state.message_kind is MessageKind.ERROR
    assert state.form_locked


def test_unknown_field_is_rejected(valid_preferences):
    controller = PreferencesFormController(valid_preferences)

    try:
        controller.change("not_a_setting", "1")
    except KeyError as exc:
        assert "Unknown Preferences field" in str(exc)
    else:
        raise AssertionError("Unknown field should have raised KeyError")
