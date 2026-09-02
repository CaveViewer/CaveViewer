"""Cover preferences schema, validation, persistence, and environment use."""

from __future__ import annotations

import ast
import inspect
import json
import logging
import os
import sys
import textwrap
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from caveviewer.gui import preference_paths
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


def test_load_missing_settings_returns_and_persists_validated_defaults(
    tmp_path,
    caplog,
):
    path = tmp_path / "preferences.json"
    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        loaded = settings.load_preferences(path)

    assert isinstance(loaded, settings.Preferences)
    assert loaded == settings.preference_defaults()
    assert "preferences.json was not found; using and saving defaults" in caplog.text
    assert json.loads(path.read_text(encoding="utf-8")) == loaded.as_dict()

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="caveviewer"):
        reloaded = settings.load_preferences(path)
    assert reloaded == loaded
    assert "Loaded preferences from preferences.json." in caplog.text
    assert "was not found" not in caplog.text


def test_missing_preferences_save_failure_keeps_defaults_without_partial_file(
    tmp_path,
    caplog,
):
    path = tmp_path / "missing-parent" / "preferences.json"

    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        loaded = settings.load_preferences(path)

    assert loaded == settings.preference_defaults()
    assert "was not found; using and saving defaults" in caplog.text
    assert "Could not save preferences" in caplog.text
    assert not path.exists()
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("content", ["{broken", "[]", "null", '"text"'])
def test_load_malformed_or_non_object_settings_returns_defaults(
    tmp_path,
    content,
    caplog,
):
    path = tmp_path / "preferences.json"
    path.write_text(content, encoding="utf-8")
    original = path.read_bytes()

    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        assert settings.load_preferences(path) == settings.preference_defaults()

    assert "Could not load preferences file preferences.json" in caplog.text
    assert "using defaults" in caplog.text
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("document", "category"),
    [
        (b"\xff", "invalid UTF-8"),
        (
            b" " * (settings.MAX_PREFERENCES_FILE_BYTES + 1),
            "file is too large",
        ),
    ],
    ids=("invalid-utf8", "oversized"),
)
def test_unloadable_preferences_use_defaults_without_overwriting_source(
    tmp_path,
    caplog,
    document,
    category,
):
    path = tmp_path / "preferences.json"
    path.write_bytes(document)

    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        loaded = settings.load_preferences(path)

    assert loaded == settings.preference_defaults()
    assert f"preferences.json ({category})" in caplog.text
    assert path.read_bytes() == document
    assert not list(tmp_path.glob("*.tmp"))


def test_unreadable_preferences_use_defaults_and_log_safe_category(
    tmp_path,
    caplog,
    monkeypatch,
):
    path = tmp_path / "preferences.json"
    path.write_text('{"io_workers": "7"}', encoding="utf-8")
    original_open = Path.open

    def fail_preferences_open(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("private operating-system detail")
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_preferences_open)
    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        loaded = settings.load_preferences(path)

    assert loaded == settings.preference_defaults()
    assert "preferences.json (read error)" in caplog.text
    assert "private operating-system detail" not in caplog.text


def test_load_falls_back_only_invalid_saved_fields(tmp_path, caplog):
    path = tmp_path / "preferences.json"
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
    assert "999" not in caplog.text

    settings.save_preferences(loaded, path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["io_workers"] == settings.preference_defaults()["io_workers"]
    assert saved["upload_chunks_per_frame"] == "3"


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
    path = tmp_path / "preferences.json"
    valid_preferences["io_workers"] = " 7 "
    snapshot = settings.require_validated_preferences(valid_preferences)
    settings.save_preferences(snapshot, path)
    loaded = settings.load_preferences(path)
    assert loaded["io_workers"] == "7"
    assert json.loads(path.read_text(encoding="utf-8"))["io_workers"] == "7"


def test_valid_preferences_file_loads_every_supported_setting_and_logs_filename(
    valid_preferences,
    tmp_path,
    caplog,
):
    path = tmp_path / "preferences.json"
    snapshot = settings.require_validated_preferences(valid_preferences)
    path.write_text(json.dumps(snapshot.as_dict()), encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="caveviewer"):
        loaded = settings.load_preferences(path)

    assert loaded == snapshot
    assert set(loaded.as_dict()) == {
        field.key for field in settings.PREFERENCE_FIELDS
    }
    assert "Loaded preferences from preferences.json." in caplog.text
    assert str(tmp_path) not in caplog.text


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
    assert path == config_home / "caveviewer" / "preferences.json"
    assert not (state_home / "caveviewer" / "preferences.json").exists()
    assert path.is_file()
    assert settings.load_preferences()["io_workers"] == valid_preferences[
        "io_workers"
    ]


def test_previous_settings_file_is_renamed_before_loading(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / "config"
    preferences_dir = config_home / "caveviewer"
    preferences_dir.mkdir(parents=True)
    previous = preferences_dir / "advanced_settings.json"
    previous.write_text('{"io_workers": "6"}', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    loaded = settings.load_preferences()

    current = preferences_dir / "preferences.json"
    assert loaded["io_workers"] == "6"
    assert current.is_file()
    assert not previous.exists()


def test_current_settings_take_precedence_without_touching_previous_file(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / "config"
    preferences_dir = config_home / "caveviewer"
    preferences_dir.mkdir(parents=True)
    current = preferences_dir / "preferences.json"
    previous = preferences_dir / "advanced_settings.json"
    current.write_text('{"io_workers": "7"}', encoding="utf-8")
    previous.write_text('{"io_workers": "6"}', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    loaded = settings.load_preferences()

    assert loaded["io_workers"] == "7"
    assert previous.read_text(encoding="utf-8") == '{"io_workers": "6"}'


def test_failed_settings_rename_loads_previous_file_and_reports_retry(
    tmp_path,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / "config"
    preferences_dir = config_home / "caveviewer"
    preferences_dir.mkdir(parents=True)
    previous = preferences_dir / "advanced_settings.json"
    previous.write_text('{"io_workers": "6"}', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(
        preference_paths.os,
        "rename",
        lambda *_args: (_ for _ in ()).throw(PermissionError("blocked")),
    )

    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        loaded = settings.load_preferences()

    assert loaded["io_workers"] == "6"
    assert previous.is_file()
    assert not (preferences_dir / "preferences.json").exists()
    assert "Could not rename preferences" in caplog.text


def test_malformed_previous_settings_are_renamed_then_resolve_to_defaults(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / "config"
    preferences_dir = config_home / "caveviewer"
    preferences_dir.mkdir(parents=True)
    previous = preferences_dir / "advanced_settings.json"
    previous.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    loaded = settings.load_preferences()

    assert loaded == settings.preference_defaults()
    assert (preferences_dir / "preferences.json").is_file()
    assert not previous.exists()


def test_older_dotfile_settings_are_not_discovered():
    older = Path(os.path.expanduser("~")) / ".caveviewer_advanced_settings.json"
    older.write_text('{"io_workers": "6"}', encoding="utf-8")

    assert settings.load_preferences()["io_workers"] == "2"
    assert Path(settings.preferences_file()).is_file()


def test_settings_save_failure_is_reported(valid_preferences, tmp_path, caplog):
    path = tmp_path / "missing-parent" / "preferences.json"
    snapshot = settings.require_validated_preferences(valid_preferences)
    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        with pytest.raises(settings.PreferencesSaveError):
            settings.save_preferences(snapshot, path)
    assert "Could not save preferences" in caplog.text


def test_atomic_save_preserves_existing_file_when_replace_fails(
    valid_preferences, tmp_path, monkeypatch
):
    path = tmp_path / "preferences.json"
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
            valid_preferences, tmp_path / "preferences.json"
        )
    with pytest.raises(TypeError, match="Preferences snapshot"):
        settings.preference_env_updates(valid_preferences)


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
    updates = settings.preference_env_updates(expected)

    for field in settings.PREFERENCE_FIELDS:
        assert updates[field.env_var] == field.value_to_env(expected[field.key])


def test_obj_import_batch_preference_maps_thousands_to_faces_env(
    valid_preferences,
):
    valid_preferences["obj_import_batch_thousands"] = "250"

    snapshot = settings.require_validated_preferences(valid_preferences)
    updates = settings.preference_env_updates(snapshot)

    assert snapshot["obj_import_batch_thousands"] == "250"
    assert updates["CAVEVIEWER_OBJ_IMPORT_BATCH_FACES"] == "250000"


def test_preferences_panel_uses_extracted_settings_logic():
    from caveviewer.gui import preferences_dialog, preferences_form, splash_screen

    assert preferences_dialog._NUMERIC_ENTRY_WIDTH == 6
    assert preferences_dialog._SCROLLBAR_GUTTER_X == 18
    assert preferences_dialog._CONTROL_GAP_X == 10
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
    splash_source = inspect.getsource(splash_screen._show_splash_composition)
    assert "PreferencesPanel(" in splash_source
    assert "platform_runtime=platform_runtime" in splash_source
    assert "on_cancel=_show_map_library_surface" in splash_source
    assert "_show_preferences_dialog(" not in splash_source


def test_preferences_panel_snapshot_keeps_unsaved_values_and_navigation():
    from caveviewer.gui import preferences_dialog

    panel = object.__new__(preferences_dialog.PreferencesPanel)
    panel.form = SimpleNamespace(
        state=SimpleNamespace(values={"memory_target_percent": "63"})
    )
    panel.active_page_key = "streaming"
    panel.page_canvas = SimpleNamespace(yview=lambda: (0.35, 0.8))

    snapshot = panel.snapshot()

    assert snapshot.values == {"memory_target_percent": "63"}
    assert snapshot.active_page_key == "streaming"
    assert snapshot.scroll_fraction == 0.35


def test_preferences_panel_exposes_backup_and_restore_as_a_separate_tab():
    from caveviewer.gui import preferences_dialog

    assert ("backup", "Backup") in preferences_dialog._PREFERENCE_PAGES
    ensure_source = inspect.getsource(
        preferences_dialog.PreferencesPanel._ensure_page
    )
    backup_source = inspect.getsource(
        preferences_dialog.PreferencesPanel._render_backup_restore
    )
    assert 'page_key == "backup"' in ensure_source
    assert 'title="Save preferences"' in backup_source
    assert 'description="Save preferences to a file."' in backup_source
    assert 'button_text="Save"' in backup_source
    assert 'title="Load preferences"' in backup_source
    assert 'description="Load preferences from a file."' in backup_source
    assert 'button_text="Load"' in backup_source
    assert 'title="Restore defaults"' in backup_source
    assert (
        'description="Restore default import and streaming settings."'
        in backup_source
    )
    assert 'button_text="Restore"' in backup_source
    assert "width=_BACKUP_ACTION_BUTTON_WIDTH" in inspect.getsource(
        preferences_dialog.PreferencesPanel._render_backup_action
    )


def test_preferences_panel_uses_dirty_controls_without_generic_status_message():
    from caveviewer.gui import preferences_dialog

    build_source = inspect.getsource(preferences_dialog.PreferencesPanel._build)
    dirty_source = inspect.getsource(
        preferences_dialog.PreferencesPanel._render_dirty_state
    )
    feedback_source = inspect.getsource(
        preferences_dialog.PreferencesPanel._sync_feedback_to_current_state
    )

    assert '"Save changes"' in build_source
    assert '"Discard changes"' in build_source
    assert "state.dirty_sections" in dirty_source
    assert "state.dirty_keys" in dirty_source
    assert 'suffix = " •"' in dirty_source
    assert "enabled=has_changes and state.apply_enabled" in dirty_source
    assert "enabled=has_changes" in dirty_source
    assert "You have unsaved changes." not in feedback_source
    assert "_save_confirmation" not in build_source


def test_preferences_panel_exports_validated_form_to_selected_file(
    valid_preferences,
    tmp_path,
):
    from caveviewer.gui import preferences_dialog
    from caveviewer.gui.preferences_workflow import PreferencesExportWorkflowResult

    snapshot = settings.require_validated_preferences(valid_preferences)
    destination = tmp_path / "preferences.json"
    chooser_calls = []
    export_calls = []
    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.form = SimpleNamespace(
        attempt_apply=lambda: (SimpleNamespace(), snapshot)
    )
    panel._render_form_state = lambda *_args, **_kwargs: None
    panel.desktop_services = SimpleNamespace(
        save_file=lambda **options: chooser_calls.append(options)
        or SimpleNamespace(path=str(destination))
    )
    panel.workflow = SimpleNamespace(
        export_file=lambda path, preferences: export_calls.append(
            (path, preferences)
        )
        or PreferencesExportWorkflowResult(path=Path(path))
    )
    panel.dialog = object()
    panel.rendered_state = None
    panel.error_label = None

    panel.export_preferences()

    assert chooser_calls[0]["initial_name"] == "preferences.json"
    assert export_calls == [(str(destination), snapshot)]
    assert "Preferences saved to" in panel._feedback_override[0]


def test_preferences_panel_import_stages_values_without_saving(
    valid_preferences,
    tmp_path,
):
    from caveviewer.gui import preferences_dialog
    from caveviewer.gui.preferences_workflow import PreferencesImportWorkflowResult

    snapshot = settings.require_validated_preferences(valid_preferences)
    source = tmp_path / "preferences.json"
    staged = []
    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.desktop_services = SimpleNamespace(
        choose_file=lambda **_options: SimpleNamespace(path=str(source))
    )
    panel.workflow = SimpleNamespace(
        import_file=lambda path, current: PreferencesImportWorkflowResult(
            preferences=snapshot,
            defaulted_keys=("io_workers",),
        )
    )
    panel.form = SimpleNamespace(
        attempt_apply=lambda: (SimpleNamespace(), snapshot)
    )
    panel._render_form_state = lambda *_args, **_kwargs: None
    panel.dialog = object()
    panel._stage_preferences = lambda preferences, message: staged.append(
        (preferences, message)
    )

    panel.import_preferences()

    assert staged[0][0] is snapshot
    assert "1 invalid or missing value was replaced with its default" in staged[0][1]


def test_preferences_panel_malformed_import_leaves_form_unchanged(tmp_path):
    from caveviewer.gui import preferences_dialog
    from caveviewer.gui.preferences_form import MessageKind
    from caveviewer.gui.preferences_workflow import PreferencesImportWorkflowResult

    current = settings.require_validated_preferences(settings.preference_defaults())
    original_form = SimpleNamespace(
        attempt_apply=lambda: (SimpleNamespace(), current)
    )
    feedback = []
    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.form = original_form
    panel.desktop_services = SimpleNamespace(
        choose_file=lambda **_options: SimpleNamespace(
            path=str(tmp_path / "broken.json")
        )
    )
    panel.workflow = SimpleNamespace(
        import_file=lambda _path, _current: PreferencesImportWorkflowResult(
            preferences=None,
            error="Preferences file is not valid UTF-8 JSON.",
        )
    )
    panel.dialog = object()
    panel._render_form_state = lambda *_args, **_kwargs: None
    panel._set_feedback = lambda message, kind: feedback.append((message, kind))

    panel.import_preferences()

    assert panel.form is original_form
    assert feedback == [
        ("Preferences file is not valid UTF-8 JSON.", MessageKind.ERROR)
    ]


def test_preferences_panel_restore_defaults_requires_confirmation():
    from caveviewer.gui import preferences_dialog

    staged = []
    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.confirm_restore = lambda: False
    panel._stage_preferences = lambda preferences, message: staged.append(
        (preferences, message)
    )

    panel.restore_defaults()
    assert staged == []

    panel.confirm_restore = lambda: True
    panel.restore_defaults()
    assert staged[0][0] == settings.preference_defaults()
    assert "not saved until" not in staged[0][1]


def test_preferences_restore_defaults_uses_app_styled_confirmation(monkeypatch):
    from caveviewer.gui import preferences_dialog

    calls = []
    monkeypatch.setattr(
        preferences_dialog,
        "ask_confirmation",
        lambda parent, **options: calls.append((parent, options)) or True,
    )
    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.confirm_restore = None
    panel.dialog = object()

    assert panel._confirm_restore_defaults() is True
    assert calls[0][0] is panel.dialog
    assert calls[0][1]["confirm_text"] == "Restore"
    assert calls[0][1]["cancel_text"] == "Cancel"


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


def test_preferences_directory_browse_keeps_compatible_service_feedback_quiet(
    tmp_path,
):
    from caveviewer.gui import preferences_dialog
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
    assert feedback == []


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
    ensure_page_source = inspect.getsource(
        preferences_dialog.PreferencesPanel._ensure_page
    )
    render_field_source = inspect.getsource(
        preferences_dialog.PreferencesPanel._render_field
    )
    section_pack_source = inspect.getsource(
        preferences_dialog.PreferenceSectionContainer.pack
    )
    page_keys = [page[0] for page in preferences_dialog._PREFERENCE_PAGES]
    page_labels = [page[1] for page in preferences_dialog._PREFERENCE_PAGES]
    field_sections = {
        field.section for field in preferences_dialog.PREFERENCE_FIELDS
    }
    fields_by_key = {
        field.key: field for field in preferences_dialog.PREFERENCE_FIELDS
    }
    assert page_keys == ["streaming", "parsing", "storage", "backup"]
    assert page_labels == ["Streaming", "Import", "Storage", "Backup"]
    assert all(len(page) == 2 for page in preferences_dialog._PREFERENCE_PAGES)
    assert set(page_keys) - {"backup"} == field_sections
    assert fields_by_key["io_workers"].label == "Loading worker limit"
    assert fields_by_key["chunk_build_workers"].label == "Cache-building worker limit"
    assert fields_by_key["recording_dir"].label == "Recordings folder"
    assert fields_by_key["map_library_dir"].label == "Downloaded maps folder"
    assert "Guided Dive" not in module_source
    assert "_render_guided_dive_disclaimer" not in module_source
    assert "compact_path = value_type in {" in render_field_source
    assert "ipady=_COMPACT_PATH_CONTROL_PAD_Y" in render_field_source
    assert "self.field_compound_controls[key] = entry_parent" in render_field_source
    assert "browse_button.configure(borderwidth=0, highlightthickness=0)" in render_field_source
    assert 'padx=(1, 1)' in render_field_source
    assert 'pady=1' in render_field_source
    assert "grid_remove()" in show_page_source
    assert "candidate_page.tkraise()" not in show_page_source
    assert "self._ensure_page(page_key)" in show_page_source
    assert "self._render_section(page, page_key)" in ensure_page_source
    assert "self._render_backup_restore(page)" in ensure_page_source
    assert "self.page_canvas.yview_moveto(0)" in show_page_source
    assert "self.button_row.pack(" in source
    assert "self.page_scroll_shell.pack(side=\"top\", fill=\"both\", expand=True)" in source
    assert "TopTabbedContentSurface(" in source
    assert "padx=(self._px(TABBED_CONTENT_ALIGNMENT_INSET), 0)" in section_pack_source
    assert "on_selected=self._show_page" in source
    assert "self.tab_strip.select(page_key, notify=False)" in show_page_source
    assert "CanvasVerticalScrollbar(" in source
    assert "self.page_scrollbar.bind_mousewheel(page)" in ensure_page_source
    assert "for page_key, _tab_label in _PREFERENCE_PAGES" not in source
    assert "text_column.bind(" not in render_field_source
    assert "self.feedback_frame.bind(" not in source
    assert "self._schedule_page_layout_sync()" in show_page_source
    assert "self.dialog.update_idletasks()" not in source
    assert "self.page_stack.grid_propagate(False)" not in source
    assert "_draw_page_scrollbar_thumb" not in module_source
    assert "class PreferencesDialog" not in module_source
    assert "show_preferences_dialog" not in module_source
    assert "tk.Toplevel" not in module_source
    assert "resizable_vertical" not in module_source


def test_preferences_pages_are_constructed_once_on_first_selection(monkeypatch):
    from caveviewer.gui import preferences_dialog

    created = []
    rendered = []
    mousewheel_bound = []

    class _FakePage:
        def __init__(self, parent, **options) -> None:
            self.parent = parent
            self.options = options
            self.bindings = []
            created.append(self)

        def bind(self, event, callback, *, add=None) -> None:
            self.bindings.append((event, callback, add))

    panel = object.__new__(preferences_dialog.PreferencesPanel)
    panel.pages = {}
    panel.page_stack = object()
    panel.page_scrollbar = SimpleNamespace(
        bind_mousewheel=lambda page: mousewheel_bound.append(page)
    )
    panel._render_section = lambda page, key: rendered.append((page, key))
    monkeypatch.setattr(preferences_dialog.tk, "Frame", _FakePage)

    assert panel._ensure_page("unknown") is None
    page = panel._ensure_page("parsing")

    assert panel._ensure_page("parsing") is page
    assert created == [page]
    assert rendered == [(page, "parsing")]
    assert mousewheel_bound == [page]


def test_preferences_page_switch_maps_only_the_selected_page():
    from caveviewer.gui import preferences_dialog

    class _FakePage:
        def __init__(self) -> None:
            self.grid_calls = []
            self.remove_calls = 0

        def grid(self, **options) -> None:
            self.grid_calls.append(options)

        def grid_remove(self) -> None:
            self.remove_calls += 1

    streaming_page = _FakePage()
    parsing_page = _FakePage()
    tab_selections = []
    scroll_positions = []
    layout_requests = []
    panel = object.__new__(preferences_dialog.PreferencesPanel)
    panel.pages = {
        "streaming": streaming_page,
        "parsing": parsing_page,
    }
    panel._ensure_page = lambda key: panel.pages.get(key)
    panel.active_page_key = "streaming"
    panel._page_configured_sizes = {}
    panel.field_entries = {}
    panel.field_page_keys = {}
    panel.tab_strip = SimpleNamespace(
        select=lambda key, notify: tab_selections.append((key, notify))
    )
    panel.form_ready = False
    panel.rendered_state = None
    panel._sync_feedback_to_current_state = lambda: None
    panel.page_canvas = SimpleNamespace(
        yview_moveto=lambda position: scroll_positions.append(position)
    )
    panel._page_scroll_region = (0, 0, 10, 10)
    panel._scrollbar_layout_state = (10, 10)
    panel._schedule_page_layout_sync = lambda: layout_requests.append(True)

    panel._show_page("parsing")

    assert panel.active_page_key == "parsing"
    assert streaming_page.remove_calls == 1
    assert streaming_page.grid_calls == []
    assert parsing_page.grid_calls == [
        {"row": 0, "column": 0, "sticky": "nsew"}
    ]
    assert tab_selections == [("parsing", False)]
    assert scroll_positions == [0]
    assert panel._page_scroll_region is None
    assert panel._scrollbar_layout_state is None
    assert layout_requests == [True]


def test_preferences_compound_path_control_preserves_focus_and_invalid_borders():
    from caveviewer.gui import preferences_dialog

    colors = []
    shell = SimpleNamespace(configure=lambda **options: colors.append(options["bg"]))
    panel = object.__new__(preferences_dialog.PreferencesPanel)
    panel.field_compound_controls = {"recording_dir": shell}
    panel.rendered_invalid_key = None

    panel._set_compound_focus("recording_dir", focused=True)
    panel._set_compound_focus("recording_dir", focused=False)
    panel.rendered_invalid_key = "recording_dir"
    panel._set_compound_focus("recording_dir", focused=False)

    assert colors == [
        preferences_dialog.DARK_THEME.entry_focus_border,
        preferences_dialog.DARK_THEME.entry_border,
        preferences_dialog.DARK_THEME.invalid_border,
    ]


def test_preferences_field_lock_updates_compound_path_border():
    from caveviewer.gui import preferences_dialog

    entry_updates = []
    shell_updates = []
    panel = object.__new__(preferences_dialog.PreferencesPanel)
    panel.field_entries = {
        "recording_dir": SimpleNamespace(
            config=lambda **options: entry_updates.append(options)
        )
    }
    panel.field_entry_states = {"recording_dir": "readonly"}
    panel.field_compound_controls = {
        "recording_dir": SimpleNamespace(
            configure=lambda **options: shell_updates.append(options)
        )
    }
    panel.field_browse_buttons = {}

    panel._set_field_lock("recording_dir")
    panel._set_field_lock(None)

    assert entry_updates[0]["state"] == "readonly"
    assert shell_updates == [
        {"bg": preferences_dialog.DARK_THEME.invalid_border},
        {"bg": preferences_dialog.DARK_THEME.entry_border},
    ]


def test_preferences_layout_requests_are_coalesced_and_cancelled_on_destroy():
    from caveviewer.gui import preferences_dialog

    callbacks = []
    cancelled = []
    panel = object.__new__(preferences_dialog.PreferencesPanel)
    panel._destroyed = False
    panel._page_layout_after_id = None
    panel._pending_page_canvas_width = None
    panel.dialog = SimpleNamespace(
        after_idle=lambda callback: callbacks.append(callback) or "layout-1",
        after_cancel=lambda after_id: cancelled.append(after_id),
    )
    panel.container = object()

    panel._schedule_page_layout_sync(viewport_width=320)
    panel._schedule_page_layout_sync(viewport_width=640)

    assert len(callbacks) == 1
    assert panel._pending_page_canvas_width == 640
    assert panel._page_layout_after_id == "layout-1"

    panel._on_container_destroy(SimpleNamespace(widget=panel.container))

    assert panel._destroyed is True
    assert panel._page_layout_after_id is None
    assert cancelled == ["layout-1"]


def test_preferences_layout_waits_for_canvas_width_before_measuring_hints():
    from caveviewer.gui import preferences_dialog

    class _FakeCanvas:
        def __init__(self) -> None:
            self.itemconfigure_calls = []

        def itemconfigure(self, item, **options) -> None:
            self.itemconfigure_calls.append((item, options))

        def winfo_width(self) -> int:
            return 480

    canvas = _FakeCanvas()
    page_window = object()
    scheduled = []
    scrollbar_syncs = []
    panel = object.__new__(preferences_dialog.PreferencesPanel)
    panel.page_canvas = canvas
    panel.page_canvas_window = page_window
    panel._pending_page_canvas_width = 480
    panel._page_canvas_window_width = None
    panel._schedule_page_layout_sync = lambda: scheduled.append(True)

    panel._sync_page_layout()

    assert canvas.itemconfigure_calls == [(page_window, {"width": 480})]
    assert scheduled == [True]

    scheduled.clear()
    panel._sync_active_page_hint_wraplengths = lambda: True
    panel.feedback_frame = None
    panel._sync_page_scrollbar = lambda: scrollbar_syncs.append(True)
    panel._sync_page_layout()

    assert scrollbar_syncs == [True]
    assert scheduled == [True]


def test_preferences_hint_wrapping_only_updates_the_active_page():
    from caveviewer.gui import preferences_dialog

    class _FakeLabel:
        def __init__(self, width: int, wraplength: int) -> None:
            self.master = SimpleNamespace(winfo_width=lambda: width)
            self.wraplength = wraplength
            self.configure_calls = []

        def cget(self, option: str) -> str:
            assert option == "wraplength"
            return str(self.wraplength)

        def configure(self, **options) -> None:
            self.configure_calls.append(options)
            self.wraplength = options["wraplength"]

    active_label = _FakeLabel(width=360, wraplength=520)
    hidden_label = _FakeLabel(width=700, wraplength=520)
    panel = object.__new__(preferences_dialog.PreferencesPanel)
    panel.active_page_key = "streaming"
    panel.page_hint_labels = {
        "streaming": [active_label],
        "parsing": [hidden_label],
    }

    assert panel._sync_active_page_hint_wraplengths() is True
    assert panel._sync_active_page_hint_wraplengths() is False

    assert active_label.configure_calls == [{"wraplength": 356}]
    assert hidden_label.configure_calls == []


def test_preferences_hint_wrap_tracks_the_rendered_text_column_width():
    from caveviewer.gui import preferences_dialog

    class _FakeLabel:
        def __init__(self) -> None:
            self.wraplength = 520
            self.configure_calls = []

        def cget(self, option: str) -> str:
            assert option == "wraplength"
            return str(self.wraplength)

        def configure(self, **options) -> None:
            self.configure_calls.append(options)
            self.wraplength = options["wraplength"]

    label = _FakeLabel()

    assert preferences_dialog.PreferencesPanel._sync_hint_wraplength(label, 1) is False
    assert preferences_dialog.PreferencesPanel._sync_hint_wraplength(label, 804) is True
    assert preferences_dialog.PreferencesPanel._sync_hint_wraplength(label, 804) is False
    assert label.configure_calls == [{"wraplength": 800}]


def test_preferences_invalidates_hidden_geometry_when_shown():
    from caveviewer.gui import preferences_dialog

    scheduled = []
    panel = object.__new__(preferences_dialog.PreferencesPanel)
    panel._pending_page_canvas_width = 1
    panel._page_canvas_window_width = 480
    panel._page_scroll_region = (0, 0, 480, 600)
    panel._scrollbar_layout_state = (600, 500)
    panel._page_configured_sizes = {"streaming": (480, 600)}
    panel._page_layout_after_id = "hidden-layout"
    cancelled = []
    panel.dialog = SimpleNamespace(after_cancel=cancelled.append)
    panel._schedule_page_layout_sync = lambda: scheduled.append(True)

    panel.on_shown()

    assert panel._pending_page_canvas_width is None
    assert panel._page_canvas_window_width is None
    assert panel._page_scroll_region is None
    assert panel._scrollbar_layout_state is None
    assert panel._page_configured_sizes == {}
    assert panel._page_layout_after_id is None
    assert cancelled == ["hidden-layout"]
    assert scheduled == [True]


def test_preferences_mapped_event_refreshes_only_the_panel_container():
    from caveviewer.gui import preferences_dialog

    panel = object.__new__(preferences_dialog.PreferencesPanel)
    panel.container = object()
    refreshed = []
    panel.on_shown = lambda: refreshed.append(True)

    panel._on_container_mapped(SimpleNamespace(widget=object()))
    panel._on_container_mapped(SimpleNamespace(widget=panel.container))

    assert refreshed == [True]


def test_preferences_rewraps_when_the_active_page_reaches_its_final_width():
    from caveviewer.gui import preferences_dialog

    class _FakeEntry:
        def winfo_reqwidth(self) -> int:
            return 100

    class _FakeLabel:
        def __init__(self) -> None:
            self.wraplength = 520
            self.configure_calls = []

        def cget(self, _option: str) -> str:
            return str(self.wraplength)

        def configure(self, **options) -> None:
            self.configure_calls.append(options)
            self.wraplength = options["wraplength"]

    scheduled = []
    label = _FakeLabel()
    panel = object.__new__(preferences_dialog.PreferencesPanel)
    panel.active_page_key = "streaming"
    panel._page_configured_sizes = {}
    panel.page_hint_labels = {"streaming": [label]}
    panel.field_entries = {"io_workers": _FakeEntry()}
    panel.field_page_keys = {"io_workers": "streaming"}
    panel._layout_policy = SimpleNamespace(row_pad_x=18)
    panel._surface_px = int
    panel._schedule_page_layout_sync = lambda: scheduled.append(True)

    panel._on_page_configured("storage", 800, 500)
    panel._on_page_configured("streaming", 800, 600)
    panel._on_page_configured("streaming", 800, 600)
    panel._on_page_configured("streaming", 800, 580)

    assert scheduled == [True, True]
    assert label.configure_calls == [{"wraplength": 678}]


def test_preferences_feedback_wraplength_avoids_repeating_identical_geometry_work():
    from caveviewer.gui import preferences_dialog

    class _FakeLabel:
        def __init__(self, wraplength: int) -> None:
            self.wraplength = wraplength
            self.configure_calls = []

        def cget(self, option: str) -> str:
            assert option == "wraplength"
            return str(self.wraplength)

        def configure(self, **options) -> None:
            self.configure_calls.append(options)
            self.wraplength = options["wraplength"]

    panel = object.__new__(preferences_dialog.PreferencesPanel)
    label = _FakeLabel(wraplength=120)
    panel.error_label = label

    panel._sync_feedback_wraplength(280)
    panel._sync_feedback_wraplength(280)
    panel._sync_feedback_wraplength(420)

    assert label.configure_calls == [
        {"wraplength": 260},
        {"wraplength": 400},
    ]


def test_preferences_visual_groups_cover_each_schema_field_once():
    """Presentation-only grouping must not hide or duplicate a setting."""
    from caveviewer.gui import preferences_dialog

    grouped_fields = [
        field
        for page_key, _page_label in preferences_dialog._PREFERENCE_PAGES
        for _group_title, fields in preferences_dialog._preference_field_groups(
            page_key
        )
        for field in fields
    ]

    assert grouped_fields == list(preferences_dialog.PREFERENCE_FIELDS)


def test_preferences_groups_use_the_standard_section_container():
    from caveviewer.gui import preferences_dialog
    from caveviewer.gui.section_spacing import (
        PRIMARY_SURFACE_VERTICAL_MARGIN,
        STANDARD_CONTENT_SECTION_SPACING,
    )

    container_source = inspect.getsource(
        preferences_dialog.PreferenceSectionContainer
    )
    render_section_source = inspect.getsource(
        preferences_dialog.PreferencesPanel._render_section
    )

    assert STANDARD_CONTENT_SECTION_SPACING.heading_to_content_y == 13
    assert STANDARD_CONTENT_SECTION_SPACING.between_sections_y == 26
    assert PRIMARY_SURFACE_VERTICAL_MARGIN == 14
    assert "text=title.upper()" in container_source
    assert "DARK_THEME.entry_border" not in container_source
    assert "STANDARD_CONTENT_SECTION_SPACING.heading_to_content_y" in container_source
    assert "STANDARD_CONTENT_SECTION_SPACING.between_sections_y" in container_source
    assert "PreferenceSectionContainer(" in render_section_source
    assert "group.content" in render_section_source


def test_preferences_uses_the_shared_primary_surface_origin():
    from caveviewer.gui import preferences_dialog

    source = inspect.getsource(preferences_dialog.PreferencesPanel._build)

    assert "content_pad_left_x=0" in source
    assert "content_pad_right_x=self._layout_policy.body_pad_x" in source
    assert "pady=self._surface_px(PRIMARY_SURFACE_VERTICAL_MARGIN)" in source


def test_preferences_panel_uses_sidebar_context_and_full_width_forms():
    """The selected splash navigation item already identifies Preferences."""
    from caveviewer.gui import preferences_dialog

    build_source = inspect.getsource(preferences_dialog.PreferencesPanel._build)
    section_source = inspect.getsource(
        preferences_dialog.PreferencesPanel._render_section
    )

    assert 'text="Preferences"' not in build_source
    assert 'section.pack(fill="x")' in section_source


def test_preferences_panel_aligns_entry_controls_without_inline_units():
    from caveviewer.gui import preferences_dialog

    render_field_source = inspect.getsource(
        preferences_dialog.PreferencesPanel._render_field
    )
    panel_source = inspect.getsource(preferences_dialog.PreferencesPanel)

    assert "self._form_row_gap()" in render_field_source
    assert "_inline_unit_text" not in panel_source


def test_preferences_panel_uses_the_shared_tabbed_content_surface():
    from caveviewer.gui import preferences_dialog

    build_source = inspect.getsource(preferences_dialog.PreferencesPanel._build)
    panel_source = inspect.getsource(preferences_dialog.PreferencesPanel)

    assert "TopTabbedContentSurface(" in build_source
    assert "TopTabbedContentSurfaceStyle(" in build_source
    assert "_tab_to_content_gap" not in panel_source


def test_preferences_panel_does_not_pass_tuple_padding_to_tk_frames():
    """Tk Frame accepts one screen distance; asymmetric margins belong to pack."""
    from caveviewer.gui import preferences_dialog

    source = textwrap.dedent(inspect.getsource(preferences_dialog.PreferencesPanel))
    tree = ast.parse(source)
    frame_calls = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "tk"
        and call.func.attr == "Frame"
    ]

    assert frame_calls
    assert all(
        not (
            keyword.arg == "pady" and isinstance(keyword.value, ast.Tuple)
        )
        for call in frame_calls
        for keyword in call.keywords
    )


def test_preferences_page_uses_the_shared_canvas_scrollbar():
    from caveviewer.gui import preferences_dialog

    source = inspect.getsource(preferences_dialog.PreferencesPanel)

    assert "CanvasScrollbarStyle(background_color=_BG_COLOR)" in source
    assert "self.page_scrollbar.sync_overflow(content_height)" in source
    assert "vertical_scroll_units" not in source


def test_preferences_tabs_use_the_shared_text_navigation_pattern():
    from caveviewer.gui import preferences_dialog

    source = inspect.getsource(preferences_dialog.PreferencesPanel)

    assert "TopTabStripStyle(" in source
    assert "TopTab(page_key, tab_label)" in source
    assert "_new_page_tab" not in source


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
    panel._feedback_override = ("Preferences exported.", "#ffffff")
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


def test_preferences_transient_feedback_times_out_and_replaces_prior_timer():
    from caveviewer.gui import preferences_dialog

    callbacks = {}
    cancelled = []
    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.dialog = SimpleNamespace(
        after=lambda duration, callback: callbacks.setdefault(duration, callback)
        or "timer",
        after_cancel=lambda after_id: cancelled.append(after_id),
    )
    panel._feedback_after_id = "old-timer"
    panel._feedback_override = ("Old", "#fff")
    panel._feedback_override_is_transient = True
    panel._destroyed = False
    synchronized = []
    panel._sync_feedback_to_current_state = lambda: synchronized.append(
        panel._feedback_override
    )

    panel._show_transient_feedback("Preferences exported.", "#0f0", duration_ms=4000)

    assert cancelled == ["old-timer"]
    assert panel._feedback_override == ("Preferences exported.", "#0f0")
    assert synchronized[-1] == panel._feedback_override
    callbacks[4000]()
    assert panel._feedback_override is None
    assert synchronized[-1] is None


def test_preferences_hidden_clears_only_transient_feedback():
    from caveviewer.gui import preferences_dialog

    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.dialog = SimpleNamespace(after_cancel=lambda _after_id: None)
    panel._feedback_after_id = "timer"
    panel._feedback_override = ("Preferences exported.", "#0f0")
    panel._feedback_override_is_transient = True
    panel._destroyed = False
    panel._sync_feedback_to_current_state = lambda: None

    panel.on_hidden()

    assert panel._feedback_override is None
    assert panel._feedback_after_id is None

    panel._feedback_override = ("Could not save preferences.", "#f00")
    panel._feedback_override_is_transient = False
    panel.on_hidden()
    assert panel._feedback_override == ("Could not save preferences.", "#f00")


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
    rendered = []
    dirty_state = SimpleNamespace(has_unsaved_changes=True)
    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.form = SimpleNamespace(
        attempt_apply=lambda: (dirty_state, snapshot)
    )
    panel._render_form_state = lambda state, **_kwargs: rendered.append(state)
    panel.numeric_entry_states = {}
    panel._set_feedback = lambda message, kind: feedback.append((message, kind))
    panel.preferences = original_preferences

    def fail_save(_settings):
        raise settings.PreferencesSaveError("Could not save settings.")

    monkeypatch.setattr(preferences_dialog, "save_preferences", fail_save)

    panel.apply()

    assert feedback == [("Could not save settings.", MessageKind.ERROR)]
    assert panel.preferences is original_preferences
    assert rendered == [dirty_state]


def test_preferences_panel_calls_apply_callback_after_success(valid_preferences):
    from caveviewer.gui import preferences_dialog
    from caveviewer.gui.preferences_workflow import PreferencesApplyResult

    snapshot = settings.require_validated_preferences(valid_preferences)
    applied = []
    marked_saved = []
    rendered = []
    dirty_state = SimpleNamespace(has_unsaved_changes=True)
    clean_state = SimpleNamespace(has_unsaved_changes=False)
    panel = preferences_dialog.PreferencesPanel.__new__(
        preferences_dialog.PreferencesPanel
    )
    panel.form = SimpleNamespace(
        attempt_apply=lambda: (dirty_state, snapshot),
        mark_saved=lambda preferences: marked_saved.append(preferences)
        or clean_state,
    )
    panel._render_form_state = lambda state, **_kwargs: rendered.append(state)
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
    assert marked_saved == [snapshot]
    assert rendered == [dirty_state, clean_state]
    assert rendered[-1] is clean_state
    assert panel.preferences is snapshot
    apply_source = inspect.getsource(preferences_dialog.PreferencesPanel.apply)
    assert "_show_save_confirmation" not in apply_source
    assert "Preferences saved" not in apply_source
