"""Cover advanced-settings schema, validation, persistence, and environment use."""

from __future__ import annotations

import json
import inspect
import logging
import os
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
        ("upload_time_budget_ms", "0.49", "at least 0.5"),
        ("upload_time_budget_ms", "50.1", "no more than 50"),
        ("chunk_size_meters", "0", "at least 0.01"),
        ("chunk_size_meters", "512.1", "no more than 512"),
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
    ],
)
def test_invalid_setting_reports_field(
    valid_advanced_settings, key, value, message_fragment
):
    valid_advanced_settings[key] = value
    field = next(
        field for field in settings.ADVANCED_SETTING_FIELDS if field.key == key
    )

    result = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    field_result = settings.validate_advanced_setting(
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
        ("upload_time_budget_ms", "0.5", "0.5"),
        ("upload_time_budget_ms", "50", "50"),
        ("chunk_size_meters", "0.01", "0.01"),
        ("chunk_size_meters", "512", "512"),
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
    valid_advanced_settings, key, value, normalized_value
):
    valid_advanced_settings[key] = value
    field = next(
        field for field in settings.ADVANCED_SETTING_FIELDS if field.key == key
    )

    result = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    field_result = settings.validate_advanced_setting(
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
        for field in settings.ADVANCED_SETTING_FIELDS
        if field.key == "io_workers"
    )

    assert settings.validate_advanced_setting(
        io_workers, "006"
    ) == settings.FieldValidationResult(True, None, "6")


def test_schema_is_typed_and_has_unique_runtime_mappings():
    fields = settings.ADVANCED_SETTING_FIELDS

    assert all(isinstance(field, settings.SettingSpec) for field in fields)
    assert all(isinstance(field.value_type, settings.ValueType) for field in fields)
    assert len({field.key for field in fields}) == len(fields)
    assert len({field.env_var for field in fields}) == len(fields)
    assert set(settings.advanced_setting_defaults()) == {
        field.key for field in fields
    }
    assert settings.advanced_setting_defaults()["chunk_size_meters"] == "50"
    assert settings.advanced_setting_defaults()["obj_import_batch_thousands"] == "200"


def test_setting_spec_is_immutable():
    field = settings.ADVANCED_SETTING_FIELDS[0]

    with pytest.raises(FrozenInstanceError):
        field.minimum = 0


@pytest.mark.parametrize("key", ["io_workers", "chunk_build_workers"])
def test_high_worker_counts_are_valid_advisory_caps_without_warning(
    valid_advanced_settings, key
):
    valid_advanced_settings[key] = "32"

    result = settings.validate_advanced_settings(
        valid_advanced_settings
    )

    assert result.is_valid, result.message
    assert result.error_key is None


def test_invalid_worker_thread_value_defers_to_validation(valid_advanced_settings):
    valid_advanced_settings["io_workers"] = "many"

    result = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    assert not result.is_valid
    assert result.error_key == "io_workers"
    assert "whole number" in (result.message or "")


def test_optional_gpu_override_can_be_blank(valid_advanced_settings):
    valid_advanced_settings["gpu_memory_gb"] = ""
    result = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    assert result.is_valid, result.message
    assert result.error_key is None
    assert result.normalized_values["gpu_memory_gb"] == ""


def test_normalization_strips_values_and_ignores_unknown_keys():
    normalized = settings.normalize_advanced_settings(
        {"io_workers": " 4 ", "unknown_future_setting": "unsafe"}
    )
    assert normalized["io_workers"] == "4"
    assert "unknown_future_setting" not in normalized
    assert set(normalized) == {field.key for field in settings.ADVANCED_SETTING_FIELDS}


def test_recording_path_expands_home(valid_advanced_settings):
    valid_advanced_settings["recording_dir"] = "~/recordings"
    result = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    assert result.is_valid, result.message
    assert result.normalized_values["recording_dir"] == os.path.join(
        os.path.expanduser("~"), "recordings"
    )


def test_recording_path_rejects_existing_file(valid_advanced_settings, tmp_path):
    target = tmp_path / "movie.mp4"
    target.write_bytes(b"not a directory")
    valid_advanced_settings["recording_dir"] = str(target)
    result = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    assert not result.is_valid
    assert result.error_key == "recording_dir"
    assert "must be a folder" in (result.message or "")


def test_recording_path_rejects_unwritable_directory(
    valid_advanced_settings, tmp_path, monkeypatch
):
    target = tmp_path / "recordings"
    target.mkdir()
    valid_advanced_settings["recording_dir"] = str(target)
    monkeypatch.setattr(settings.os, "access", lambda path, mode: False)
    result = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    assert not result.is_valid
    assert result.error_key == "recording_dir"
    assert "must be writable" in (result.message or "")


def test_recording_path_rejects_creation_under_unwritable_parent(
    valid_advanced_settings, tmp_path, monkeypatch
):
    target = tmp_path / "parent" / "new" / "recordings"
    target.parent.parent.mkdir()
    valid_advanced_settings["recording_dir"] = str(target)
    monkeypatch.setattr(settings.os, "access", lambda path, mode: False)
    result = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    assert not result.is_valid
    assert result.error_key == "recording_dir"
    assert "inside a writable folder" in (result.message or "")


def test_load_missing_settings_returns_validated_defaults(tmp_path):
    loaded = settings.load_advanced_settings(tmp_path / "missing.json")
    assert isinstance(loaded, settings.AdvancedSettings)
    assert loaded == settings.advanced_setting_defaults()


@pytest.mark.parametrize("content", ["{broken", "[]", "null", '"text"'])
def test_load_malformed_or_non_object_settings_returns_defaults(tmp_path, content):
    path = tmp_path / "advanced_settings.json"
    path.write_text(content, encoding="utf-8")
    assert settings.load_advanced_settings(path) == settings.advanced_setting_defaults()


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
        loaded = settings.load_advanced_settings(path)

    assert loaded["io_workers"] == "2"
    assert loaded["upload_chunks_per_frame"] == "3"
    assert "Ignoring invalid saved io_workers" in caplog.text


def test_loaded_settings_snapshot_is_immutable(tmp_path):
    loaded = settings.load_advanced_settings(tmp_path / "missing.json")
    mutable_copy = loaded.as_dict()

    with pytest.raises(TypeError):
        loaded["io_workers"] = "8"
    mutable_copy["io_workers"] = "8"

    assert loaded["io_workers"] == "2"


def test_snapshot_constructor_rejects_values_outside_the_schema_boundary(
    valid_advanced_settings
):
    valid_advanced_settings["io_workers"] = "999"

    with pytest.raises(ValueError, match="no more than 32"):
        settings.AdvancedSettings(valid_advanced_settings)

    valid_advanced_settings["io_workers"] = "2"
    del valid_advanced_settings["upload_time_budget_ms"]
    with pytest.raises(ValueError, match="exactly the declared schema keys"):
        settings.AdvancedSettings(valid_advanced_settings)


def test_settings_save_and_load_round_trip(valid_advanced_settings, tmp_path):
    path = tmp_path / "advanced_settings.json"
    valid_advanced_settings["io_workers"] = " 7 "
    snapshot = settings.require_validated_advanced_settings(valid_advanced_settings)
    settings.save_advanced_settings(snapshot, path)
    loaded = settings.load_advanced_settings(path)
    assert loaded["io_workers"] == "7"
    assert json.loads(path.read_text(encoding="utf-8"))["io_workers"] == "7"


def test_default_settings_path_uses_xdg_config_not_state(
    valid_advanced_settings, tmp_path, monkeypatch
):
    config_home = tmp_path / "config"
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    snapshot = settings.require_validated_advanced_settings(valid_advanced_settings)
    settings.save_advanced_settings(snapshot)
    path = Path(settings.advanced_settings_file())
    assert path == config_home / "caveviewer" / "advanced_settings.json"
    assert not (state_home / "caveviewer" / "advanced_settings.json").exists()
    assert path.is_file()
    assert settings.load_advanced_settings()["io_workers"] == valid_advanced_settings[
        "io_workers"
    ]


def test_legacy_settings_file_is_migrated_into_preferences_directory():
    legacy = Path(os.path.expanduser("~")) / ".caveviewer_advanced_settings.json"
    legacy.write_text('{"io_workers": "6"}', encoding="utf-8")

    migrated = Path(settings.advanced_settings_file())

    assert migrated.is_file()
    assert settings.load_advanced_settings(migrated)["io_workers"] == "6"


def test_settings_save_failure_is_reported(valid_advanced_settings, tmp_path, caplog):
    path = tmp_path / "missing-parent" / "advanced_settings.json"
    snapshot = settings.require_validated_advanced_settings(valid_advanced_settings)
    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        with pytest.raises(settings.AdvancedSettingsSaveError):
            settings.save_advanced_settings(snapshot, path)
    assert "Could not save advanced settings" in caplog.text


def test_atomic_save_preserves_existing_file_when_replace_fails(
    valid_advanced_settings, tmp_path, monkeypatch
):
    path = tmp_path / "advanced_settings.json"
    path.write_text('{"io_workers": "2"}', encoding="utf-8")
    snapshot = settings.require_validated_advanced_settings(valid_advanced_settings)

    def fail_replace(*_args):
        raise OSError("failed")

    monkeypatch.setattr(settings.os, "replace", fail_replace)

    with pytest.raises(settings.AdvancedSettingsSaveError):
        settings.save_advanced_settings(snapshot, path)

    assert path.read_text(encoding="utf-8") == '{"io_workers": "2"}'
    assert list(tmp_path.glob("*.tmp")) == []


def test_environment_overrides_are_used_as_defaults(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_IO_WORKERS", "9")
    monkeypatch.setenv("CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME", "3")
    monkeypatch.setenv("CAVEVIEWER_OBJ_IMPORT_BATCH_FACES", "300000")
    defaults = settings.advanced_setting_defaults()
    assert defaults["io_workers"] == "9"
    assert defaults["upload_chunks_per_frame"] == "3"
    assert defaults["obj_import_batch_thousands"] == "300"


def test_invalid_environment_override_falls_back_to_built_in(monkeypatch, caplog):
    monkeypatch.setenv("CAVEVIEWER_IO_WORKERS", "999")

    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        defaults = settings.advanced_setting_defaults()

    assert defaults["io_workers"] == "2"
    assert "Ignoring invalid CAVEVIEWER_IO_WORKERS" in caplog.text


def test_invalid_values_cannot_cross_validated_boundary(valid_advanced_settings):
    valid_advanced_settings["io_workers"] = "999"

    with pytest.raises(settings.AdvancedSettingsValidationError) as exc_info:
        settings.require_validated_advanced_settings(valid_advanced_settings)

    assert exc_info.value.result.error_key == "io_workers"


def test_runtime_consumers_reject_unvalidated_mappings(
    valid_advanced_settings, tmp_path
):
    with pytest.raises(TypeError, match="AdvancedSettings snapshot"):
        settings.save_advanced_settings(
            valid_advanced_settings, tmp_path / "advanced_settings.json"
        )
    with pytest.raises(TypeError, match="AdvancedSettings snapshot"):
        settings.apply_advanced_settings_to_env(valid_advanced_settings)


def test_every_numeric_setting_has_a_display_range():
    expected_ranges = {
        "memory_target_percent": "1-80%",
        "gpu_memory_target_percent": "1-80%",
        "gpu_memory_gb": "0.5-50 GB",
        "io_workers": "1-32 workers",
        "io_reserved_cpus": "2-32 logical CPUs",
        "upload_chunks_per_frame": "1-16 chunks",
        "upload_time_budget_ms": "0.5-50 ms",
        "chunk_size_meters": "0.01-512",
        "obj_scan_throttle_ms": "0-50 ms",
        "obj_import_batch_thousands": "1-2000 thousand faces",
        "chunk_build_workers": "1-32 workers",
        "chunk_build_reserved_cpus": "2-32 logical CPUs",
    }

    numeric_fields = {
        field.key: field
        for field in settings.ADVANCED_SETTING_FIELDS
        if field.value_type in {settings.ValueType.INT, settings.ValueType.FLOAT}
    }
    assert set(numeric_fields) == set(expected_ranges)
    assert {
        key: settings.advanced_setting_range_text(field)
        for key, field in numeric_fields.items()
    } == expected_ranges


def test_every_numeric_setting_has_an_in_field_placeholder():
    numeric_fields = [
        field
        for field in settings.ADVANCED_SETTING_FIELDS
        if field.value_type in {settings.ValueType.INT, settings.ValueType.FLOAT}
    ]
    placeholders = {
        field.key: settings.advanced_setting_placeholder_text(field)
        for field in numeric_fields
    }
    assert placeholders == {
        "memory_target_percent": "1-80",
        "gpu_memory_target_percent": "1-80",
        "gpu_memory_gb": "0.5-50",
        "io_workers": "1-32",
        "io_reserved_cpus": "2-32",
        "upload_chunks_per_frame": "1-16",
        "upload_time_budget_ms": "0.5-50",
        "chunk_size_meters": "0.01-512",
        "obj_scan_throttle_ms": "0-50",
        "obj_import_batch_thousands": "1-2000",
        "chunk_build_workers": "1-32",
        "chunk_build_reserved_cpus": "2-32",
    }


def test_every_numeric_setting_has_finite_bounds():
    numeric_fields = [
        field
        for field in settings.ADVANCED_SETTING_FIELDS
        if field.value_type in {settings.ValueType.INT, settings.ValueType.FLOAT}
    ]

    assert all(field.minimum is not None for field in numeric_fields)
    assert all(field.maximum is not None for field in numeric_fields)


def test_required_numeric_settings_open_with_defaults():
    defaults = settings.advanced_setting_defaults()
    required_numeric_keys = {
        field.key
        for field in settings.ADVANCED_SETTING_FIELDS
        if field.value_type in {settings.ValueType.INT, settings.ValueType.FLOAT}
        and not field.optional
    }
    assert all(defaults[key] for key in required_numeric_keys)


def test_non_numeric_setting_has_no_display_range():
    recording_dir = next(
        field
        for field in settings.ADVANCED_SETTING_FIELDS
        if field.key == "recording_dir"
    )
    assert settings.advanced_setting_range_text(recording_dir) is None
    assert settings.advanced_setting_placeholder_text(recording_dir) is None


def test_apply_maps_every_setting_to_its_declared_environment_variable(
    valid_advanced_settings
):
    for index, field in enumerate(settings.ADVANCED_SETTING_FIELDS, start=1):
        key = field.key
        if field.value_type is settings.ValueType.PATH_CREATE:
            continue
        if field.value_type is settings.ValueType.INT:
            minimum = int(field.minimum or 0)
            maximum = field.maximum
            value = max(minimum, index)
            if maximum is not None:
                value = min(value, int(maximum))
            valid_advanced_settings[key] = str(value)

    expected = settings.require_validated_advanced_settings(valid_advanced_settings)
    settings.apply_advanced_settings_to_env(expected)

    for field in settings.ADVANCED_SETTING_FIELDS:
        assert os.environ[field.env_var] == field.value_to_env(expected[field.key])


def test_obj_import_batch_preference_maps_thousands_to_faces_env(
    valid_advanced_settings,
):
    valid_advanced_settings["obj_import_batch_thousands"] = "250"

    snapshot = settings.require_validated_advanced_settings(valid_advanced_settings)
    settings.apply_advanced_settings_to_env(snapshot)

    assert snapshot["obj_import_batch_thousands"] == "250"
    assert os.environ["CAVEVIEWER_OBJ_IMPORT_BATCH_FACES"] == "250000"


def test_advanced_settings_dialog_uses_extracted_settings_logic():
    from caveviewer.gui import advanced_settings_dialog, advanced_settings_form, splash_screen

    assert advanced_settings_dialog._NUMERIC_ENTRY_WIDTH == 8
    assert advanced_settings_dialog.ADVANCED_SETTING_FIELDS is settings.ADVANCED_SETTING_FIELDS
    assert (
        advanced_settings_dialog.advanced_setting_placeholder_text
        is settings.advanced_setting_placeholder_text
    )
    assert advanced_settings_dialog.save_advanced_settings is settings.save_advanced_settings
    assert (
        advanced_settings_dialog.AdvancedSettingsFormController
        is advanced_settings_form.AdvancedSettingsFormController
    )
    assert (
        splash_screen._show_advanced_settings_dialog
        is advanced_settings_dialog.show_advanced_settings_dialog
    )


def test_advanced_settings_dialog_uses_compact_tabbed_pages():
    from caveviewer.gui import advanced_settings_dialog

    source = inspect.getsource(advanced_settings_dialog.AdvancedSettingsDialog._build)
    module_source = inspect.getsource(advanced_settings_dialog)
    settings_source = inspect.getsource(settings)
    show_page_source = inspect.getsource(
        advanced_settings_dialog.AdvancedSettingsDialog._show_page
    )
    render_field_source = inspect.getsource(
        advanced_settings_dialog.AdvancedSettingsDialog._render_field
    )
    page_keys = [page[0] for page in advanced_settings_dialog._PREFERENCE_PAGES]
    page_labels = [page[1] for page in advanced_settings_dialog._PREFERENCE_PAGES]
    field_sections = {
        field.section for field in advanced_settings_dialog.ADVANCED_SETTING_FIELDS
    }
    fields_by_key = {
        field.key: field for field in advanced_settings_dialog.ADVANCED_SETTING_FIELDS
    }

    assert page_keys == ["streaming", "parsing", "storage"]
    assert page_labels == ["Streaming", "Import", "Storage"]
    assert all(len(page) == 2 for page in advanced_settings_dialog._PREFERENCE_PAGES)
    assert set(page_keys) == field_sections
    assert advanced_settings_dialog._WINDOWS_LAYOUT == (
        advanced_settings_dialog.sys.platform == "win32"
    )
    assert advanced_settings_dialog._MACOS_LAYOUT == (
        advanced_settings_dialog.sys.platform == "darwin"
    )
    assert fields_by_key["io_workers"].label == "Loading worker limit"
    assert (
        fields_by_key["chunk_build_workers"].label
        == "Cache-building worker limit"
    )
    assert (
        fields_by_key["obj_import_batch_thousands"].label
        == "Faces per .obj batch"
    )
    assert (
        fields_by_key["io_workers"].hint
        == "Max chunk-loading worker threads."
    )
    assert (
        fields_by_key["chunk_build_workers"].hint
        == "Max cache-building worker threads."
    )
    assert (
        fields_by_key["upload_time_budget_ms"].hint
        == "Target milliseconds spent uploading chunks each frame."
    )
    assert (
        fields_by_key["obj_import_batch_thousands"].hint
        == "Thousands of triangulated faces per batch."
    )
    if (
        advanced_settings_dialog._LINUX_LAYOUT
        or advanced_settings_dialog.sys.platform == "win32"
    ):
        assert advanced_settings_dialog._MIN_WIDTH >= 860
    elif advanced_settings_dialog._MACOS_LAYOUT:
        assert advanced_settings_dialog._MIN_WIDTH == 680
        assert advanced_settings_dialog._TEXT_ENTRY_WIDTH == 34
        assert advanced_settings_dialog._ROW_PAD_Y == 8
        assert advanced_settings_dialog._CONTROL_ROW_TOP_PAD_Y == 8
        assert advanced_settings_dialog._TAB_PAD_Y == 8
        assert advanced_settings_dialog._TAB_HIGHLIGHT_THICKNESS == 0
        assert advanced_settings_dialog._TAB_BOTTOM_PAD_Y == 12
        assert advanced_settings_dialog._BUTTON_ROW_TOP_PAD_Y == 12
    else:
        assert advanced_settings_dialog._MIN_WIDTH >= 760
    assert advanced_settings_dialog._ROW_PAD_X == 18
    if not advanced_settings_dialog._MACOS_LAYOUT:
        assert advanced_settings_dialog._ROW_PAD_Y == 12
        assert advanced_settings_dialog._CONTROL_ROW_TOP_PAD_Y == 14
        assert advanced_settings_dialog._TAB_PAD_Y == 7
        assert advanced_settings_dialog._TAB_HIGHLIGHT_THICKNESS == 1
        assert advanced_settings_dialog._TAB_BOTTOM_PAD_Y == 18
        assert advanced_settings_dialog._BUTTON_ROW_TOP_PAD_Y == 18
    assert advanced_settings_dialog._NOTICE_WRAP_LENGTH == 720
    assert fields_by_key["recording_dir"].label == "Recordings folder"
    assert fields_by_key["recording_dir"].hint == "Where saved recordings are stored."
    assert "compact_path = key == \"recording_dir\"" in render_field_source
    assert "row=1" in render_field_source
    assert "pady=(_CONTROL_ROW_TOP_PAD_Y, 0)" in render_field_source
    assert "entry.grid(row=0, column=0, sticky=\"ew\")" in render_field_source
    assert "padx=(_CONTROL_GAP_X, 0)" in render_field_source
    assert "if not single_line_hint:" in render_field_source
    assert "self._resize_hint(event, label)" in render_field_source
    assert "if _LINUX_LAYOUT and not single_line_hint:" not in render_field_source
    assert "entry_parent.grid(row=0, column=1" not in render_field_source
    assert "row.grid_columnconfigure(1" not in render_field_source
    assert "_compact_directory_path(path: str, max_chars: int = 80)" in module_source
    assert "Streaming Performance" not in module_source
    assert "Map Parsing" not in module_source
    assert "Maximum threads used while viewing a cave" not in settings_source
    assert "Maximum threads used to build a new cache" not in settings_source
    assert "MP4 flight recordings" not in settings_source
    assert "Movie recording directory" not in settings_source
    assert "_COMPACT_WORKER_WARNING" not in module_source
    assert "_warning_keys_for_values" not in module_source
    assert "Scrollbar(" not in source
    assert "yscrollcommand=self._set_page_scrollbar" in source
    assert "create_line(" in module_source
    assert "capstyle=\"round\"" in module_source
    assert "content_canvas" not in source
    assert "resizable(False, _WINDOWS_LAYOUT)" in module_source
    assert "highlightthickness=_TAB_HIGHLIGHT_THICKNESS" in module_source
    assert "self.button_row.pack(" in source
    assert "side=\"bottom\"" in source
    assert "self.page_scroll_shell.pack(side=\"top\", fill=\"both\", expand=True)" in source
    assert "self.page_scrollbar.pack(side=\"right\", fill=\"y\")" in module_source
    assert "self.page_scrollbar.pack_forget()" in module_source
    assert "_SCROLL_THUMB_COLOR" in module_source
    assert "before=self.page_scroll_shell" not in module_source
    assert "self.feedback_frame = tk.Frame(self.button_row" in source
    assert "self.feedback_frame.pack(side=\"left\", fill=\"x\", expand=True)" in source
    assert "_INLINE_FEEDBACK_PAD_X" in module_source
    assert "self.dialog.minsize(dialog_w, min(dialog_h, 360))" in module_source
    assert "_bind_page_mousewheel" in module_source
    assert "grid_remove()" not in show_page_source
    assert "candidate_page.tkraise()" in show_page_source
    assert "self.page_canvas.yview_moveto(0)" in show_page_source
    assert "self.dialog.after_idle(self._sync_page_scrollbar)" in show_page_source
    assert "active_page.winfo_reqheight()" in module_source
    assert "_apply_geometry" not in show_page_source
    assert "grid_propagate(False)" in source
    assert "create_dialog_notice(" not in module_source
    assert "create_dialog_action_button(" in module_source
    assert "set_dialog_action_button(" in module_source
    assert "class _LabelButton" not in module_source


def test_advanced_settings_invalid_field_switches_to_containing_page():
    from caveviewer.gui import advanced_settings_dialog

    shown_pages = []
    focused = []

    dialog = advanced_settings_dialog.AdvancedSettingsDialog.__new__(
        advanced_settings_dialog.AdvancedSettingsDialog
    )
    dialog.field_page_keys = {"chunk_size_meters": "parsing"}
    dialog.field_entries = {
        "chunk_size_meters": SimpleNamespace(
            winfo_exists=lambda: True,
            focus_set=lambda: focused.append("chunk_size_meters"),
            selection_range=lambda *_args: None,
        )
    }
    dialog.field_entry_states = {"chunk_size_meters": "normal"}
    dialog.numeric_placeholder_keys = set()
    dialog._show_page = lambda page_key: shown_pages.append(page_key)
    dialog.dialog = SimpleNamespace(after_idle=lambda callback: callback())

    dialog._focus_invalid_field("chunk_size_meters")

    assert shown_pages == ["parsing"]
    assert focused == ["chunk_size_meters"]


def test_dialog_stays_open_and_reports_atomic_save_failure(
    valid_advanced_settings, monkeypatch
):
    from caveviewer.gui import advanced_settings_dialog
    from caveviewer.gui.advanced_settings_form import MessageKind

    snapshot = settings.require_validated_advanced_settings(valid_advanced_settings)
    original_settings = settings.load_advanced_settings()
    destroyed = []
    feedback = []
    applied = []
    dialog = advanced_settings_dialog.AdvancedSettingsDialog.__new__(
        advanced_settings_dialog.AdvancedSettingsDialog
    )
    dialog.form = SimpleNamespace(
        attempt_apply=lambda: (SimpleNamespace(), snapshot)
    )
    dialog._render_form_state = lambda *_args, **_kwargs: None
    dialog.numeric_entry_states = {}
    dialog._set_feedback = lambda message, kind: feedback.append((message, kind))
    dialog.dialog = SimpleNamespace(destroy=lambda: destroyed.append(True))
    dialog.settings = original_settings

    def fail_save(_settings):
        raise settings.AdvancedSettingsSaveError("Could not save settings.")

    monkeypatch.setattr(advanced_settings_dialog, "save_advanced_settings", fail_save)
    monkeypatch.setattr(
        advanced_settings_dialog,
        "apply_advanced_settings_to_env",
        lambda value: applied.append(value),
    )

    dialog.apply()

    assert feedback == [("Could not save settings.", MessageKind.ERROR)]
    assert dialog.settings is original_settings
    assert applied == []
    assert destroyed == []
