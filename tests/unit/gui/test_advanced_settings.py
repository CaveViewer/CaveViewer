from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from gui import advanced_settings as settings


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
        ("gpu_memory_gb", "0", "greater than 0"),
        ("gpu_memory_gb", "-1", "cannot be negative"),
        ("gpu_memory_gb", "1025", "no more than 1024"),
        ("io_workers", "0", "at least 1"),
        ("io_workers", "1.5", "whole number"),
        ("io_workers", "many", "whole number"),
        ("io_reserved_cpus", "-1", "cannot be negative"),
        ("upload_chunks_per_frame", "0", "at least 1"),
        ("upload_chunks_per_frame", "17", "no more than 16"),
        ("upload_chunks_per_frame", "2.5", "whole number"),
        ("upload_time_budget_ms", "0.49", "at least 0.5"),
        ("upload_time_budget_ms", "50.1", "no more than 50"),
        ("chunk_size_meters", "0", "greater than 0"),
        ("chunk_size_meters", "512.1", "no more than 512"),
        ("obj_scan_throttle_ms", "-0.1", "cannot be negative"),
        ("obj_scan_throttle_ms", "50.1", "no more than 50"),
        ("chunk_build_workers", "0", "at least 1"),
        ("chunk_build_reserved_cpus", "-1", "cannot be negative"),
        ("recording_dir", "", "required"),
    ],
)
def test_invalid_setting_reports_field(
    valid_advanced_settings, key, value, message_fragment
):
    valid_advanced_settings[key] = value

    ok, message, _normalized, error_key = settings.validate_advanced_settings(
        valid_advanced_settings
    )

    assert not ok
    assert error_key == key
    assert message_fragment in (message or "")


@pytest.mark.parametrize(
    ("key", "value", "normalized_value"),
    [
        ("memory_target_percent", "1", "1"),
        ("memory_target_percent", "80", "80"),
        ("gpu_memory_target_percent", "1", "1"),
        ("gpu_memory_target_percent", "80", "80"),
        ("gpu_memory_gb", "0.01", "0.01"),
        ("gpu_memory_gb", "1024", "1024"),
        ("io_workers", "1", "1"),
        ("io_reserved_cpus", "0", "0"),
        ("upload_chunks_per_frame", "1", "1"),
        ("upload_chunks_per_frame", "16", "16"),
        ("upload_time_budget_ms", "0.5", "0.5"),
        ("upload_time_budget_ms", "50", "50"),
        ("chunk_size_meters", "0.01", "0.01"),
        ("chunk_size_meters", "512", "512"),
        ("obj_scan_throttle_ms", "0", "0"),
        ("obj_scan_throttle_ms", "50", "50"),
        ("chunk_build_workers", "1", "1"),
        ("chunk_build_reserved_cpus", "0", "0"),
    ],
)
def test_setting_boundaries_are_accepted(
    valid_advanced_settings, key, value, normalized_value
):
    valid_advanced_settings[key] = value

    ok, message, normalized, error_key = settings.validate_advanced_settings(
        valid_advanced_settings
    )

    assert ok, message
    assert error_key is None
    assert normalized[key] == normalized_value


def test_optional_gpu_override_can_be_blank(valid_advanced_settings):
    valid_advanced_settings["gpu_memory_gb"] = ""
    ok, message, normalized, error_key = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    assert ok, message
    assert error_key is None
    assert normalized["gpu_memory_gb"] == ""


def test_normalization_strips_values_and_ignores_unknown_keys():
    normalized = settings.normalize_advanced_settings(
        {"io_workers": " 4 ", "unknown_future_setting": "unsafe"}
    )
    assert normalized["io_workers"] == "4"
    assert "unknown_future_setting" not in normalized
    assert set(normalized) == {field["key"] for field in settings.ADVANCED_SETTING_FIELDS}


def test_recording_path_expands_home(valid_advanced_settings):
    valid_advanced_settings["recording_dir"] = "~/recordings"
    ok, message, normalized, _error_key = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    assert ok, message
    assert normalized["recording_dir"] == os.path.join(
        os.path.expanduser("~"), "recordings"
    )


def test_recording_path_rejects_existing_file(valid_advanced_settings, tmp_path):
    target = tmp_path / "movie.mp4"
    target.write_bytes(b"not a directory")
    valid_advanced_settings["recording_dir"] = str(target)
    ok, message, _normalized, error_key = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    assert not ok
    assert error_key == "recording_dir"
    assert "must be a folder" in (message or "")


def test_recording_path_rejects_unwritable_directory(
    valid_advanced_settings, tmp_path, monkeypatch
):
    target = tmp_path / "recordings"
    target.mkdir()
    valid_advanced_settings["recording_dir"] = str(target)
    monkeypatch.setattr(settings.os, "access", lambda path, mode: False)
    ok, message, _normalized, error_key = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    assert not ok
    assert error_key == "recording_dir"
    assert "must be writable" in (message or "")


def test_recording_path_rejects_creation_under_unwritable_parent(
    valid_advanced_settings, tmp_path, monkeypatch
):
    target = tmp_path / "parent" / "new" / "recordings"
    target.parent.parent.mkdir()
    valid_advanced_settings["recording_dir"] = str(target)
    monkeypatch.setattr(settings.os, "access", lambda path, mode: False)
    ok, message, _normalized, error_key = settings.validate_advanced_settings(
        valid_advanced_settings
    )
    assert not ok
    assert error_key == "recording_dir"
    assert "inside a writable folder" in (message or "")


def test_load_missing_settings_returns_blank_schema(tmp_path):
    loaded = settings.load_advanced_settings(tmp_path / "missing.json")
    assert loaded == settings.default_advanced_settings()


@pytest.mark.parametrize("content", ["{broken", "[]", "null", '"text"'])
def test_load_malformed_or_non_object_settings_returns_blank_schema(tmp_path, content):
    path = tmp_path / "advanced_settings.json"
    path.write_text(content, encoding="utf-8")
    assert settings.load_advanced_settings(path) == settings.default_advanced_settings()


def test_settings_save_and_load_round_trip(valid_advanced_settings, tmp_path):
    path = tmp_path / "advanced_settings.json"
    valid_advanced_settings["io_workers"] = " 7 "
    settings.save_advanced_settings(valid_advanced_settings, path)
    loaded = settings.load_advanced_settings(path)
    assert loaded["io_workers"] == "7"
    assert json.loads(path.read_text(encoding="utf-8"))["io_workers"] == "7"


def test_default_settings_path_stays_inside_isolated_home(valid_advanced_settings):
    settings.save_advanced_settings(valid_advanced_settings)
    path = Path(settings.advanced_settings_file())
    assert path.parent == Path(os.path.expanduser("~")) / ".caveviewer"
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


def test_settings_save_failure_is_logged(valid_advanced_settings, tmp_path, caplog):
    path = tmp_path / "missing-parent" / "advanced_settings.json"
    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        settings.save_advanced_settings(valid_advanced_settings, path)
    assert "could not save advanced settings" in caplog.text


def test_environment_overrides_are_used_as_defaults(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_IO_WORKERS", "9")
    monkeypatch.setenv("CAVEVIEWER_UPLOAD_CHUNKS_PER_FRAME", "3")
    defaults = settings.advanced_setting_defaults()
    assert defaults["io_workers"] == "9"
    assert defaults["upload_chunks_per_frame"] == "3"


def test_apply_maps_every_setting_to_its_declared_environment_variable(
    valid_advanced_settings
):
    for index, field in enumerate(settings.ADVANCED_SETTING_FIELDS, start=1):
        key = field["key"]
        if field["value_type"] == "path_create":
            continue
        if field["value_type"] == "int":
            minimum = int(field.get("min", 0))
            maximum = field.get("max")
            value = max(minimum, index)
            if maximum is not None:
                value = min(value, int(maximum))
            valid_advanced_settings[key] = str(value)

    expected = settings.effective_advanced_settings(valid_advanced_settings)
    settings.apply_advanced_settings_to_env(valid_advanced_settings)

    for field in settings.ADVANCED_SETTING_FIELDS:
        assert os.environ[field["env_var"]] == expected[field["key"]]


def test_splash_dialog_uses_extracted_settings_logic():
    from gui import splash_screen

    assert splash_screen._ADVANCED_SETTING_FIELDS is settings.ADVANCED_SETTING_FIELDS
    assert splash_screen._validate_advanced_settings is settings.validate_advanced_settings
    assert splash_screen._save_advanced_settings is settings.save_advanced_settings
