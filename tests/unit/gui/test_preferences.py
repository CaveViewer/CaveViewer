"""Cover preferences schema, validation, persistence, and environment use."""

from __future__ import annotations

import json
import inspect
import logging
import os
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from caveviewer.gui import preferences as settings


@pytest.mark.parametrize(
    ("key", "value", "message_fragment"),
    [
        ("memory_target_percent", "", "required"),
        ("memory_target_percent", "-1", "cannot be negative"),
        ("memory_target_percent", "0.9", "at least 1"),
        ("memory_target_percent", "80.1", "no more than 80"),
        ("memory_target_percent", "abc", "must be a number"),
        ("memory_target_percent", "nan", "finite number"),
        ("memory_target_percent", "inf", "finite number"),
        ("gpu_memory_target_percent", "0.9", "at least 1"),
        ("gpu_memory_target_percent", "81", "no more than 80"),
        ("gpu_memory_gb", "0.49", "at least 0.5"),
        ("gpu_memory_gb", "-1", "cannot be negative"),
        ("gpu_memory_gb", "50.1", "no more than 50"),
        ("io_workers", "0", "at least 1"),
        ("io_workers", "33", "no more than 32"),
        ("io_workers", "1.5", "whole number"),
        ("io_workers", "many", "whole number"),
        ("io_reserved_cpus", "-1", "cannot be negative"),
        ("io_reserved_cpus", "0", "at least 2"),
        ("io_reserved_cpus", "1", "at least 2"),
        ("io_reserved_cpus", "33", "no more than 32"),
        ("upload_chunks_per_frame", "0", "at least 1"),
        ("upload_chunks_per_frame", "17", "no more than 16"),
        ("upload_chunks_per_frame", "2.5", "whole number"),
        ("upload_groups_per_frame", "0", "at least 1"),
        ("upload_groups_per_frame", "65", "no more than 64"),
        ("upload_groups_per_frame", "2.5", "whole number"),
        ("upload_time_budget_ms", "0.49", "at least 0.5"),
        ("upload_time_budget_ms", "50.1", "no more than 50"),
        ("chunk_size_meters", "0", "at least 0.01"),
        ("chunk_size_meters", "512.1", "no more than 512"),
        ("max_upload_group_mb", "", "required"),
        ("max_upload_group_mb", "0.9", "at least 1"),
        ("max_upload_group_mb", "512.1", "no more than 512"),
        ("obj_scan_throttle_ms", "-0.1", "cannot be negative"),
        ("obj_scan_throttle_ms", "50.1", "no more than 50"),
        ("obj_import_batch_thousands", "0", "at least 1"),
        ("obj_import_batch_thousands", "2001", "no more than 2000"),
        ("obj_import_batch_thousands", "1.5", "whole number"),
        ("chunk_build_workers", "0", "at least 1"),
        ("chunk_build_workers", "33", "no more than 32"),
        ("chunk_build_reserved_cpus", "-1", "cannot be negative"),
        ("chunk_build_reserved_cpus", "0", "at least 2"),
        ("chunk_build_reserved_cpus", "1", "at least 2"),
        ("chunk_build_reserved_cpus", "33", "no more than 32"),
        ("recording_dir", "", "required"),
        ("map_library_dir", "", "required"),
    ],
)
def test_invalid_setting_reports_field(
    valid_preferences, key, value, message_fragment
):
    valid_preferences[key] = value
    field = next(
        field for field in settings.PREFERENCE_FIELDS if field.key == key
    )

    result = settings.validate_preferences(
        valid_preferences
    )
    field_result = settings.validate_preference(
        field, value
    )

    assert not result.is_valid
    assert not field_result.is_valid
    assert result.error_key == key
    assert message_fragment in (result.message or "")
    assert field_result.message == result.message


@pytest.mark.parametrize(
    ("key", "value", "normalized_value"),
    [
        ("memory_target_percent", "1", "1"),
        ("memory_target_percent", "80", "80"),
        ("gpu_memory_target_percent", "1", "1"),
        ("gpu_memory_target_percent", "80", "80"),
        ("gpu_memory_gb", "0.5", "0.5"),
        ("gpu_memory_gb", "50", "50"),
        ("io_workers", "1", "1"),
        ("io_workers", "32", "32"),
        ("io_reserved_cpus", "2", "2"),
        ("io_reserved_cpus", "32", "32"),
        ("upload_chunks_per_frame", "1", "1"),
        ("upload_chunks_per_frame", "16", "16"),
        ("upload_groups_per_frame", "1", "1"),
        ("upload_groups_per_frame", "64", "64"),
        ("upload_time_budget_ms", "0.5", "0.5"),
        ("upload_time_budget_ms", "50", "50"),
        ("chunk_size_meters", "0.01", "0.01"),
        ("chunk_size_meters", "512", "512"),
        ("max_upload_group_mb", "1", "1"),
        ("max_upload_group_mb", "512", "512"),
        ("obj_scan_throttle_ms", "0", "0"),
        ("obj_scan_throttle_ms", "50", "50"),
        ("obj_import_batch_thousands", "1", "1"),
        ("obj_import_batch_thousands", "2000", "2000"),
        ("chunk_build_workers", "1", "1"),
        ("chunk_build_workers", "32", "32"),
        ("chunk_build_reserved_cpus", "2", "2"),
        ("chunk_build_reserved_cpus", "32", "32"),
    ],
)
def test_setting_boundaries_are_accepted(
    valid_preferences, key, value, normalized_value
):
    valid_preferences[key] = value
    field = next(
        field for field in settings.PREFERENCE_FIELDS if field.key == key
    )

    result = settings.validate_preferences(
        valid_preferences
    )
    field_result = settings.validate_preference(
        field, value
    )

    assert result.is_valid, result.message
    assert field_result.is_valid, field_result.message
    assert result.error_key is None
    assert result.normalized_values[key] == normalized_value
    assert field_result.normalized_value == normalized_value


def test_single_field_validation_returns_canonical_value():
    io_workers = next(
        field
        for field in settings.PREFERENCE_FIELDS
        if field.key == "io_workers"
    )

    assert settings.validate_preference(
        io_workers, "006"
    ) == settings.PreferenceFieldValidationResult(True, None, "6")


def test_schema_is_typed_and_has_unique_runtime_mappings():
    fields = settings.PREFERENCE_FIELDS

    assert all(isinstance(field, settings.PreferenceSpec) for field in fields)
    assert all(isinstance(field.value_type, settings.PreferenceValueType) for field in fields)
    assert len({field.key for field in fields}) == len(fields)
    assert len({field.env_var for field in fields}) == len(fields)
    assert set(settings.preference_defaults()) == {
        field.key for field in fields
    }
    assert settings.preference_defaults()["chunk_size_meters"] == "50"
    assert settings.preference_defaults()["max_upload_group_mb"] == "16"
    assert settings.preference_defaults()["obj_import_batch_thousands"] == "200"
    assert settings.preference_defaults()["map_library_dir"].endswith(
        os.path.join("Downloads")
    )


def test_setting_spec_is_immutable():
    field = settings.PREFERENCE_FIELDS[0]

    with pytest.raises(FrozenInstanceError):
        field.minimum = 0


@pytest.mark.parametrize("key", ["io_workers", "chunk_build_workers"])
def test_high_worker_counts_are_valid_advisory_caps_without_warning(
    valid_preferences, key
):
    valid_preferences[key] = "32"

    result = settings.validate_preferences(
        valid_preferences
    )

    assert result.is_valid, result.message
    assert result.error_key is None


def test_invalid_worker_thread_value_defers_to_validation(valid_preferences):
    valid_preferences["io_workers"] = "many"

    result = settings.validate_preferences(
        valid_preferences
    )
    assert not result.is_valid
    assert result.error_key == "io_workers"
    assert "whole number" in (result.message or "")


def test_optional_gpu_override_can_be_blank(valid_preferences):
    valid_preferences["gpu_memory_gb"] = ""
    result = settings.validate_preferences(
        valid_preferences
    )
    assert result.is_valid, result.message
    assert result.error_key is None
    assert result.normalized_values["gpu_memory_gb"] == ""


def test_normalization_strips_values_and_ignores_unknown_keys():
    normalized = settings.normalize_preferences(
        {"io_workers": " 4 ", "unknown_future_setting": "unsafe"}
    )
    assert normalized["io_workers"] == "4"
    assert "unknown_future_setting" not in normalized
    assert set(normalized) == {field.key for field in settings.PREFERENCE_FIELDS}




def test_recording_path_expands_home(valid_preferences):
    valid_preferences["recording_dir"] = "~/recordings"
    result = settings.validate_preferences(
        valid_preferences
    )
    assert result.is_valid, result.message
    assert result.normalized_values["recording_dir"] == os.path.join(
        os.path.expanduser("~"), "recordings"
    )


def test_recording_path_rejects_existing_file(valid_preferences, tmp_path):
    target = tmp_path / "movie.mp4"
    target.write_bytes(b"not a directory")
    valid_preferences["recording_dir"] = str(target)
    result = settings.validate_preferences(
        valid_preferences
    )
    assert not result.is_valid
    assert result.error_key == "recording_dir"
    assert "must be a folder" in (result.message or "")


def test_recording_path_rejects_unwritable_directory(
    valid_preferences, tmp_path, monkeypatch
):
    target = tmp_path / "recordings"
    target.mkdir()
    valid_preferences["recording_dir"] = str(target)
    monkeypatch.setattr(settings.os, "access", lambda path, mode: False)
    result = settings.validate_preferences(
        valid_preferences
    )
    assert not result.is_valid
    assert result.error_key == "recording_dir"
    assert "must be writable" in (result.message or "")


def test_recording_path_rejects_creation_under_unwritable_parent(
    valid_preferences, tmp_path, monkeypatch
):
    target = tmp_path / "parent" / "new" / "recordings"
    target.parent.parent.mkdir()
    valid_preferences["recording_dir"] = str(target)
    monkeypatch.setattr(settings.os, "access", lambda path, mode: False)
    result = settings.validate_preferences(
        valid_preferences
    )
    assert not result.is_valid
    assert result.error_key == "recording_dir"
    assert "inside a writable folder" in (result.message or "")


def test_map_library_path_expands_home(valid_preferences):
    valid_preferences["map_library_dir"] = "~/map-library-parent"
    result = settings.validate_preferences(
        valid_preferences
    )
    assert result.is_valid, result.message
    assert result.normalized_values["map_library_dir"] == os.path.join(
        os.path.expanduser("~"), "map-library-parent"
    )


def test_load_missing_settings_returns_validated_defaults(tmp_path):
    loaded = settings.load_preferences(tmp_path / "missing.json")
    assert isinstance(loaded, settings.Preferences)
    assert loaded == settings.preference_defaults()


@pytest.mark.parametrize("content", ["{broken", "[]", "null", '"text"'])
def test_load_malformed_or_non_object_settings_returns_defaults(tmp_path, content):
    path = tmp_path / "advanced_settings.json"
    path.write_text(content, encoding="utf-8")
    assert settings.load_preferences(path) == settings.preference_defaults()


def test_load_falls_back_only_invalid_saved_fields(tmp_path, caplog):
    path = tmp_path / "advanced_settings.json"
    path.write_text(
        json.dumps(
            {
                "io_workers": "999",
                "upload_chunks_per_frame": "3",
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        loaded = settings.load_preferences(path)

    assert loaded["io_workers"] == "2"
    assert loaded["upload_chunks_per_frame"] == "3"
    assert "Ignoring invalid saved io_workers" in caplog.text


def test_loaded_settings_snapshot_is_immutable(tmp_path):
    loaded = settings.load_preferences(tmp_path / "missing.json")
    mutable_copy = loaded.as_dict()

    with pytest.raises(TypeError):
        loaded["io_workers"] = "8"
    mutable_copy["io_workers"] = "8"

    assert loaded["io_workers"] == "2"


def test_snapshot_constructor_rejects_values_outside_the_schema_boundary(
    valid_preferences
):
    valid_preferences["io_workers"] = "999"

    with pytest.raises(ValueError, match="no more than 32"):
        settings.Preferences(valid_preferences)

    valid_preferences["io_workers"] = "2"
    del valid_preferences["upload_time_budget_ms"]
    with pytest.raises(ValueError, match="exactly the declared schema keys"):
        settings.Preferences(valid_preferences)


def test_settings_save_and_load_round_trip(valid_preferences, tmp_path):
    path = tmp_path / "advanced_settings.json"
    valid_preferences["io_workers"] = " 7 "
    snapshot = settings.require_validated_preferences(valid_preferences)
    settings.save_preferences(snapshot, path)
    loaded = settings.load_preferences(path)
    assert loaded["io_workers"] == "7"
    assert json.loads(path.read_text(encoding="utf-8"))["io_workers"] == "7"


def test_default_settings_path_uses_xdg_config_not_state(
    valid_preferences, tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / "config"
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    snapshot = settings.require_validated_preferences(valid_preferences)
    settings.save_preferences(snapshot)
    path = Path(settings.preferences_file())
    assert path == config_home / "caveviewer" / "advanced_settings.json"
    assert not (state_home / "caveviewer" / "advanced_settings.json").exists()
    assert path.is_file()
    assert settings.load_preferences()["io_workers"] == valid_preferences[
        "io_workers"
    ]


def test_legacy_settings_file_is_migrated_into_preferences_directory():
    legacy = Path(os.path.expanduser("~")) / ".caveviewer_advanced_settings.json"
    legacy.write_text('{"io_workers": "6"}', encoding="utf-8")

    migrated = Path(settings.preferences_file())

    assert migrated.is_file()
    assert settings.load_preferences(migrated)["io_workers"] == "6"


def test_settings_save_failure_is_reported(valid_preferences, tmp_path, caplog):
    path = tmp_path / "missing-parent" / "advanced_settings.json"
    snapshot = settings.require_validated_preferences(valid_preferences)
    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        with pytest.raises(settings.PreferencesSaveError):
            settings.save_preferences(snapshot, path)
    assert "Could not save preferences" in caplog.text


def test_atomic_save_preserves_existing_file_when_replace_fails(
    valid_preferences, tmp_path, monkeypatch
):
    path = tmp_path / "advanced_settings.json"
    path.write_text('{"io_workers": "2"}', encoding="utf-8")
    snapshot = settings.require_validated_preferences(valid_preferences)

    def fail_replace(*_args):
        raise OSError("failed")

    monkeypatch.setattr(settings.os, "replace", fail_replace)

    with pytest.raises(settings.PreferencesSaveError):
        settings.save_preferences(snapshot, path)

    assert path.read_text(encoding="utf-8") == '{"io_workers": "2"}'
    assert list(tmp_path.glob("*.tmp")) == []


def test_environment_overrides_are_used_as_defaults(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_IO_WORKERS", "9")
    monkeypatch.setenv("CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME", "3")
    monkeypatch.setenv("CAVEVIEWER_UPLOAD_GROUPS_PER_FRAME", "4")
    monkeypatch.setenv("CAVEVIEWER_OBJ_IMPORT_BATCH_FACES", "300000")
    defaults = settings.preference_defaults()
    assert defaults["io_workers"] == "9"
    assert defaults["upload_chunks_per_frame"] == "3"
    assert defaults["upload_groups_per_frame"] == "4"
    assert defaults["obj_import_batch_thousands"] == "300"




def test_invalid_environment_override_falls_back_to_built_in(monkeypatch, caplog):
    monkeypatch.setenv("CAVEVIEWER_IO_WORKERS", "999")

    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        defaults = settings.preference_defaults()

    assert defaults["io_workers"] == "2"
    assert "Ignoring invalid CAVEVIEWER_IO_WORKERS" in caplog.text


def test_invalid_values_cannot_cross_validated_boundary(valid_preferences):
    valid_preferences["io_workers"] = "999"

    with pytest.raises(settings.PreferencesValidationError) as exc_info:
        settings.require_validated_preferences(valid_preferences)

    assert exc_info.value.result.error_key == "io_workers"


def test_runtime_consumers_reject_unvalidated_mappings(
    valid_preferences, tmp_path
):
    with pytest.raises(TypeError, match="Preferences snapshot"):
        settings.save_preferences(
            valid_preferences, tmp_path / "advanced_settings.json"
        )
    with pytest.raises(TypeError, match="Preferences snapshot"):
        settings.apply_preferences_to_env(valid_preferences)


def test_every_numeric_setting_has_a_display_range():
    expected_ranges = {
        "memory_target_percent": "1-80%",
        "gpu_memory_target_percent": "1-80%",
        "gpu_memory_gb": "0.5-50 GB",
        "io_workers": "1-32 workers",
        "io_reserved_cpus": "2-32 logical CPUs",
        "upload_chunks_per_frame": "1-16 chunks",
        "upload_groups_per_frame": "1-64 operations",
        "upload_time_budget_ms": "0.5-50 ms",
        "chunk_size_meters": "0.01-512",
        "max_upload_group_mb": "1-512 MB",
        "obj_scan_throttle_ms": "0-50 ms",
        "obj_import_batch_thousands": "1-2000 thousand faces",
        "chunk_build_workers": "1-32 workers",
        "chunk_build_reserved_cpus": "2-32 logical CPUs",
    }

    numeric_fields = {
        field.key: field
        for field in settings.PREFERENCE_FIELDS
        if field.value_type in {settings.PreferenceValueType.INT, settings.PreferenceValueType.FLOAT}
    }
    assert set(numeric_fields) == set(expected_ranges)
    assert {
        key: settings.preference_range_text(field)
        for key, field in numeric_fields.items()
    } == expected_ranges


def test_every_numeric_setting_has_an_in_field_placeholder():
    numeric_fields = [
        field
        for field in settings.PREFERENCE_FIELDS
        if field.value_type in {settings.PreferenceValueType.INT, settings.PreferenceValueType.FLOAT}
    ]
    placeholders = {
        field.key: settings.preference_placeholder_text(field)
        for field in numeric_fields
    }
    assert placeholders == {
        "memory_target_percent": "1-80",
        "gpu_memory_target_percent": "1-80",
        "gpu_memory_gb": "0.5-50",
        "io_workers": "1-32",
        "io_reserved_cpus": "2-32",
        "upload_chunks_per_frame": "1-16",
        "upload_groups_per_frame": "1-64",
        "upload_time_budget_ms": "0.5-50",
        "chunk_size_meters": "0.01-512",
        "max_upload_group_mb": "1-512",
        "obj_scan_throttle_ms": "0-50",
        "obj_import_batch_thousands": "1-2000",
        "chunk_build_workers": "1-32",
        "chunk_build_reserved_cpus": "2-32",
    }


def test_every_numeric_setting_has_finite_bounds():
    numeric_fields = [
        field
        for field in settings.PREFERENCE_FIELDS
        if field.value_type in {settings.PreferenceValueType.INT, settings.PreferenceValueType.FLOAT}
    ]

    assert all(field.minimum is not None for field in numeric_fields)
    assert all(field.maximum is not None for field in numeric_fields)


def test_required_numeric_settings_open_with_defaults():
    defaults = settings.preference_defaults()
    required_numeric_keys = {
        field.key
        for field in settings.PREFERENCE_FIELDS
        if field.value_type in {settings.PreferenceValueType.INT, settings.PreferenceValueType.FLOAT}
        and not field.optional
    }
    assert all(defaults[key] for key in required_numeric_keys)


def test_non_numeric_settings_have_no_display_range():
    non_numeric_fields = [
        field
        for field in settings.PREFERENCE_FIELDS
        if field.value_type not in {
            settings.PreferenceValueType.INT,
            settings.PreferenceValueType.FLOAT,
        }
    ]
    assert {field.key for field in non_numeric_fields} == {
        "recording_dir",
        "map_library_dir",
    }
    assert all(
        settings.preference_range_text(field) is None
        for field in non_numeric_fields
    )
    assert all(
        settings.preference_placeholder_text(field) is None
        for field in non_numeric_fields
    )


def test_apply_maps_every_setting_to_its_declared_environment_variable(
    valid_preferences
):
    for index, field in enumerate(settings.PREFERENCE_FIELDS, start=1):
        key = field.key
        if field.value_type is settings.PreferenceValueType.PATH_CREATE:
            continue
        if field.value_type is settings.PreferenceValueType.INT:
            minimum = int(field.minimum or 0)
            maximum = field.maximum
            value = max(minimum, index)
            if maximum is not None:
                value = min(value, int(maximum))
            valid_preferences[key] = str(value)

    expected = settings.require_validated_preferences(valid_preferences)
    settings.apply_preferences_to_env(expected)

    for field in settings.PREFERENCE_FIELDS:
        assert os.environ[field.env_var] == field.value_to_env(expected[field.key])


def test_obj_import_batch_preference_maps_thousands_to_faces_env(
    valid_preferences,
):
    valid_preferences["obj_import_batch_thousands"] = "250"

    snapshot = settings.require_validated_preferences(valid_preferences)
    settings.apply_preferences_to_env(snapshot)

    assert snapshot["obj_import_batch_thousands"] == "250"
    assert os.environ["CAVEVIEWER_OBJ_IMPORT_BATCH_FACES"] == "250000"


def test_preferences_panel_uses_extracted_settings_logic():
    from caveviewer.gui import preferences_dialog, preferences_form, splash_screen

    assert preferences_dialog._NUMERIC_ENTRY_WIDTH == 8
    assert preferences_dialog.PREFERENCE_FIELDS is settings.PREFERENCE_FIELDS
    assert (
        preferences_dialog.preference_placeholder_text
        is settings.preference_placeholder_text
    )
    assert preferences_dialog.save_preferences is settings.save_preferences
    assert (
        preferences_dialog.PreferencesFormController
        is preferences_form.PreferencesFormController
    )
    assert splash_screen.PreferencesPanel is preferences_dialog.PreferencesPanel
    splash_source = inspect.getsource(splash_screen.show_splash_screen)
    assert "PreferencesPanel(" in splash_source
    assert "platform_runtime=platform_runtime" in splash_source
    assert "on_cancel=_show_map_library_surface" in splash_source
    assert "_show_preferences_dialog(" not in splash_source


def _directory_picker_panel(
    preferences_dialog,
    *,
    desktop_services,
    platform_runtime,
    initial_dir,
):
    values = SimpleNamespace(value=str(initial_dir))
    values.get = lambda: values.value
    values.set = lambda value: setattr(values, "value", value)
    feedback = []
    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.desktop_services = desktop_services
    panel.platform_runtime = platform_runtime
    panel.field_vars = {"map_library_dir": values}
    panel.dialog = object()
    panel._set_feedback = lambda message, kind: feedback.append((message, kind))
    return panel, values, feedback


def test_preferences_directory_browse_rechecks_the_injected_runtime(tmp_path):
    from caveviewer.gui import preferences_dialog
    from caveviewer.core.capabilities import (
        CapabilityResult,
        DirectorySelectionRoute,
        DirectorySelectionTarget,
    )
    from caveviewer.gui.features import FeatureDecision, FeatureId, FeatureState
    from caveviewer.gui.platform.runtime import DirectorySelectionPreflight

    selected_dir = tmp_path / "selected"
    selected_dir.mkdir()
    chooser_calls = []
    preflight_calls = []
    target = DirectorySelectionTarget(
        primary_route=DirectorySelectionRoute.PORTAL,
        fallback_route=DirectorySelectionRoute.TK,
    )

    class FakeDesktopServices:
        def directory_selection_target(self):
            return target

        def choose_directory(self, **options):
            chooser_calls.append(options)
            return SimpleNamespace(path=str(selected_dir))

    desktop_services = FakeDesktopServices()

    class FakeRuntime:
        def __init__(self):
            self.desktop_services = desktop_services

        def directory_selection_preflight(self):
            preflight_calls.append(True)
            return DirectorySelectionPreflight(
                capability=CapabilityResult.available(
                    target,
                    reason_code="directory_selection_portal_route_available",
                ),
                decision=FeatureDecision(
                    feature=FeatureId.DIRECTORY_SELECTION,
                    state=FeatureState.ENABLED,
                    reason_code="directory_selection_available",
                    explanation="Directory selection is available.",
                    route="portal_then_tk",
                )
            )

    panel, values, feedback = _directory_picker_panel(
        preferences_dialog,
        desktop_services=desktop_services,
        platform_runtime=FakeRuntime(),
        initial_dir=tmp_path,
    )

    panel._choose_directory("map_library_dir", "Downloaded maps folder")
    panel._choose_directory("map_library_dir", "Downloaded maps folder")

    assert preflight_calls == [True, True]
    assert len(chooser_calls) == 2
    assert all(call["title"] == "Downloaded maps folder" for call in chooser_calls)
    assert all(call["parent"] is panel.dialog for call in chooser_calls)
    assert values.value == str(selected_dir)
    assert feedback == []


def test_preferences_directory_browse_blocks_disabled_route_before_chooser(tmp_path):
    from caveviewer.gui import preferences_dialog
    from caveviewer.gui.features import FeatureDecision, FeatureId, FeatureState
    from caveviewer.gui.preferences_form import MessageKind

    preflight_calls = []

    class FakeDesktopServices:
        def choose_directory(self, **_options):
            pytest.fail("disabled directory selection must not open a chooser")

    desktop_services = FakeDesktopServices()

    class FakeRuntime:
        def __init__(self):
            self.desktop_services = desktop_services

        def directory_selection_preflight(self):
            preflight_calls.append(True)
            return SimpleNamespace(
                decision=FeatureDecision(
                    feature=FeatureId.DIRECTORY_SELECTION,
                    state=FeatureState.DISABLED,
                    reason_code="directory_selection_service_unavailable",
                    explanation="Directory selection is unavailable in this environment.",
                )
            )

    panel, _values, feedback = _directory_picker_panel(
        preferences_dialog,
        desktop_services=desktop_services,
        platform_runtime=FakeRuntime(),
        initial_dir=tmp_path,
    )

    panel._choose_directory("map_library_dir", "Downloaded maps folder")

    assert preflight_calls == [True]
    assert feedback == [
        (
            "Directory selection is unavailable in this environment.",
            MessageKind.WARNING,
        )
    ]


def test_preferences_directory_browse_uses_legacy_compatible_service(tmp_path):
    from caveviewer.gui import preferences_dialog
    from caveviewer.gui.preferences_form import MessageKind

    selected_dir = tmp_path / "selected"
    selected_dir.mkdir()
    chooser_calls = []

    class LegacyDesktopServices:
        def choose_directory(self, **options):
            chooser_calls.append(options)
            return SimpleNamespace(path=str(selected_dir))

    panel, values, feedback = _directory_picker_panel(
        preferences_dialog,
        desktop_services=LegacyDesktopServices(),
        platform_runtime=None,
        initial_dir=tmp_path,
    )

    panel._choose_directory("map_library_dir", "Downloaded maps folder")

    assert len(chooser_calls) == 1
    assert values.value == str(selected_dir)
    assert feedback == [
        (
            "Directory selection is available through this desktop service.",
            MessageKind.WARNING,
        )
    ]


def test_preferences_directory_browse_reports_desktop_action_failure(tmp_path):
    from caveviewer.gui import preferences_dialog
    from caveviewer.core.capabilities import (
        CapabilityResult,
        DirectorySelectionRoute,
        DirectorySelectionTarget,
    )
    from caveviewer.gui.features import FeatureDecision, FeatureId, FeatureState
    from caveviewer.gui.preferences_form import MessageKind
    from caveviewer.gui.platform.runtime import DirectorySelectionPreflight

    target = DirectorySelectionTarget(
        primary_route=DirectorySelectionRoute.PORTAL,
        fallback_route=DirectorySelectionRoute.TK,
    )

    class FakeDesktopServices:
        def directory_selection_target(self):
            return target

        def choose_directory(self, **_options):
            raise preferences_dialog.DesktopServiceError("Desktop picker failed.")

    desktop_services = FakeDesktopServices()

    class FakeRuntime:
        def __init__(self):
            self.desktop_services = desktop_services

        def directory_selection_preflight(self):
            return DirectorySelectionPreflight(
                capability=CapabilityResult.available(
                    target,
                    reason_code="directory_selection_portal_route_available",
                ),
                decision=FeatureDecision(
                    feature=FeatureId.DIRECTORY_SELECTION,
                    state=FeatureState.ENABLED,
                    reason_code="directory_selection_available",
                    explanation="Directory selection is available.",
                    route="portal_then_tk",
                )
            )

    panel, _values, feedback = _directory_picker_panel(
        preferences_dialog,
        desktop_services=desktop_services,
        platform_runtime=FakeRuntime(),
        initial_dir=tmp_path,
    )

    panel._choose_directory("map_library_dir", "Downloaded maps folder")

    assert feedback == [("Desktop picker failed.", MessageKind.ERROR)]


def test_preferences_panel_uses_compact_tabbed_pages():
    from caveviewer.gui import preferences_dialog

    source = inspect.getsource(preferences_dialog.PreferencesPanel._build)
    module_source = inspect.getsource(preferences_dialog)
    show_page_source = inspect.getsource(
        preferences_dialog.PreferencesPanel._show_page
    )
    render_field_source = inspect.getsource(
        preferences_dialog.PreferencesPanel._render_field
    )
    page_keys = [page[0] for page in preferences_dialog._PREFERENCE_PAGES]
    page_labels = [page[1] for page in preferences_dialog._PREFERENCE_PAGES]
    field_sections = {
        field.section for field in preferences_dialog.PREFERENCE_FIELDS
    }
    fields_by_key = {
        field.key: field for field in preferences_dialog.PREFERENCE_FIELDS
    }
    assert page_keys == ["streaming", "parsing", "storage"]
    assert page_labels == ["Streaming", "Import", "Storage"]
    assert all(len(page) == 2 for page in preferences_dialog._PREFERENCE_PAGES)
    assert set(page_keys) == field_sections
    assert fields_by_key["io_workers"].label == "Loading worker limit"
    assert fields_by_key["chunk_build_workers"].label == "Cache-building worker limit"
    assert fields_by_key["recording_dir"].label == "Recordings folder"
    assert fields_by_key["map_library_dir"].label == "Downloaded maps folder"
    assert "Guided Dive" not in module_source
    assert "_render_guided_dive_disclaimer" not in module_source
    assert "compact_path = value_type in {" in render_field_source
    assert "entry.grid(row=0, column=0, sticky=\"ew\")" in render_field_source
    assert "grid_remove()" not in show_page_source
    assert "candidate_page.tkraise()" in show_page_source
    assert "self.page_canvas.yview_moveto(0)" in show_page_source
    assert "self.button_row.pack(" in source
    assert "self.page_scroll_shell.pack(side=\"top\", fill=\"both\", expand=True)" in source
    assert "class PreferencesDialog" not in module_source
    assert "show_preferences_dialog" not in module_source
    assert "tk.Toplevel" not in module_source
    assert "resizable_vertical" not in module_source


@pytest.mark.parametrize("delta", (-120, -1))
def test_preferences_page_scrolls_for_windows_and_macos_wheel_deltas(delta):
    from caveviewer.gui import preferences_dialog

    class _FakeCanvas:
        def __init__(self) -> None:
            self.scroll_calls = []

        def yview_scroll(self, amount, units) -> None:
            self.scroll_calls.append((amount, units))

    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.page_canvas = _FakeCanvas()
    panel.page_scrollbar = SimpleNamespace(winfo_manager=lambda: "pack")

    assert panel._scroll_page_content(SimpleNamespace(delta=delta)) == "break"
    assert panel.page_canvas.scroll_calls == [(1, "units")]


def test_preferences_panel_tracks_and_discards_unsaved_values(valid_preferences):
    from caveviewer.gui import preferences_dialog

    snapshot = settings.require_validated_preferences(valid_preferences)
    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.preferences = snapshot
    panel.form = preferences_dialog.PreferencesFormController(snapshot)
    panel.rendering_state = False
    panel.rendered_invalid_key = None
    panel._feedback_override = ("Preferences saved.", "#ffffff")
    synchronized = []
    panel._sync_field_value = lambda key, value: synchronized.append((key, value))
    panel._render_form_state = lambda *_args, **_kwargs: None

    assert panel.has_unsaved_changes is False
    panel.form.change("io_workers", "5")
    assert panel.has_unsaved_changes is True

    panel.discard_changes()

    assert panel.has_unsaved_changes is False
    assert panel.form.state.values == snapshot.as_dict()
    assert set(synchronized) == set(snapshot.items())
    assert panel._feedback_override is None


def test_preferences_invalid_field_switches_to_containing_page():
    from caveviewer.gui import preferences_dialog

    shown_pages = []
    focused = []

    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.field_page_keys = {"chunk_size_meters": "parsing"}
    panel.field_entries = {
        "chunk_size_meters": SimpleNamespace(
            winfo_exists=lambda: True,
            focus_set=lambda: focused.append("chunk_size_meters"),
            selection_range=lambda *_args: None,
        )
    }
    panel.field_entry_states = {"chunk_size_meters": "normal"}
    panel.numeric_placeholder_keys = set()
    panel._show_page = lambda page_key: shown_pages.append(page_key)
    panel.dialog = SimpleNamespace(after_idle=lambda callback: callback())

    panel._focus_invalid_field("chunk_size_meters")

    assert shown_pages == ["parsing"]
    assert focused == ["chunk_size_meters"]


def test_preferences_panel_reports_atomic_save_failure(
    valid_preferences, monkeypatch
):
    from caveviewer.gui import preferences_dialog
    from caveviewer.gui.preferences_form import MessageKind

    snapshot = settings.require_validated_preferences(valid_preferences)
    original_preferences = settings.load_preferences()
    feedback = []
    applied = []
    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.form = SimpleNamespace(
        attempt_apply=lambda: (SimpleNamespace(), snapshot)
    )
    panel._render_form_state = lambda *_args, **_kwargs: None
    panel.numeric_entry_states = {}
    panel._set_feedback = lambda message, kind: feedback.append((message, kind))
    panel.preferences = original_preferences

    def fail_save(_settings):
        raise settings.PreferencesSaveError("Could not save settings.")

    monkeypatch.setattr(preferences_dialog, "save_preferences", fail_save)
    monkeypatch.setattr(
        preferences_dialog,
        "apply_preferences_to_env",
        lambda value: applied.append(value),
    )

    panel.apply()

    assert feedback == [("Could not save settings.", MessageKind.ERROR)]
    assert panel.preferences is original_preferences
    assert applied == []


def test_preferences_panel_calls_apply_callback_after_success(valid_preferences):
    from caveviewer.gui import preferences_dialog
    from caveviewer.gui.preferences_workflow import PreferencesApplyResult

    snapshot = settings.require_validated_preferences(valid_preferences)
    applied = []
    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.form = SimpleNamespace(
        attempt_apply=lambda: (SimpleNamespace(), snapshot)
    )
    panel._render_form_state = lambda *_args, **_kwargs: None
    panel.numeric_entry_states = {}
    panel.workflow = SimpleNamespace(
        apply=lambda preferences: PreferencesApplyResult(
            preferences=preferences
        )
    )
    panel.on_applied = applied.append
    panel.preferences = None

    panel.apply()

    assert applied == [snapshot]
    assert panel.preferences is snapshot
