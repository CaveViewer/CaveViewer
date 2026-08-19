"""Characterization tests for core runtime-settings composition."""

from __future__ import annotations

import pytest

from caveviewer.core.preferences import runtime_settings as settings
from caveviewer.core.preferences.schema import (
    PREFERENCE_FIELDS,
    PreferenceDefaultContext,
    preference_defaults,
)


def _platform(tmp_path, *, platform_name: str = "linux"):
    return settings.RuntimePlatformFacts(
        platform_name=platform_name,
        os_name="nt" if platform_name.startswith("win") else "posix",
        home=tmp_path,
    )


def _resolve(
    tmp_path,
    *,
    preferences=None,
    environ=None,
    cli_overrides=None,
    platform_name="linux",
):
    return settings.resolve_runtime_settings(
        preferences=preferences,
        environ={} if environ is None else environ,
        cli_overrides=cli_overrides,
        platform=_platform(tmp_path, platform_name=platform_name),
    )


def test_registry_inventories_runtime_settings_and_excludes_tooling_variables():
    preference_specs = [
        spec
        for spec in settings.RUNTIME_SETTING_SPECS
        if spec.category is settings.RuntimeSettingCategory.PERSISTED_PREFERENCE
    ]

    assert {spec.key for spec in preference_specs} == {
        field.key for field in PREFERENCE_FIELDS
    }
    assert {spec.environment_variable for spec in preference_specs} == {
        field.env_var for field in PREFERENCE_FIELDS
    }
    assert all(spec.preference in PREFERENCE_FIELDS for spec in preference_specs)
    assert all(spec.description for spec in settings.RUNTIME_SETTING_SPECS)
    assert all(callable(spec.parser) for spec in settings.RUNTIME_SETTING_SPECS)

    expected_environment_only = {
        "CAVEVIEWER_APP_ICON",
        "CAVEVIEWER_COMMIT",
        "CAVEVIEWER_FFMPEG",
        "CAVEVIEWER_FORCE_STARTUP_FOCUS",
        "CAVEVIEWER_FORCE_UPDATE",
        "CAVEVIEWER_GITHUB_REPO",
        "CAVEVIEWER_GPU_DRAW_TIMER",
        "CAVEVIEWER_HOME",
        "CAVEVIEWER_IMPORT_NICE",
        "CAVEVIEWER_IO_NICE",
        "CAVEVIEWER_LOG_LEVEL",
        "CAVEVIEWER_MAP_CACHE_DIR",
        "CAVEVIEWER_MAP_LIBRARY_API_URL",
        "CAVEVIEWER_MAP_LIBRARY_CATALOG_ASSET_NAME",
        "CAVEVIEWER_MAP_LIBRARY_RELEASE_TAG",
        "CAVEVIEWER_MAP_LIBRARY_REPO",
        "CAVEVIEWER_MAX_TEXTURE_SIZE",
        "CAVEVIEWER_NAVIGATION_GUARD",
        "CAVEVIEWER_NAVIGATION_GUARD_RADIUS_CELLS",
        "CAVEVIEWER_OBJ_BUCKET_WORKERS",
        "CAVEVIEWER_RECORDING_CRF",
        "CAVEVIEWER_RECORDING_FPS",
        "CAVEVIEWER_RECORDING_MAX_HEIGHT",
        "CAVEVIEWER_SAMPLE_DATA_TAG",
        "CAVEVIEWER_SAMPLE_MAPS_API_URL",
        "CAVEVIEWER_SAMPLE_MAPS_REPO",
        "CAVEVIEWER_TEXTURE_RESIDENT_CACHE_MB",
        "CAVEVIEWER_TEXT_AA_MODE",
        "CAVEVIEWER_TK_SCALE",
        "CAVEVIEWER_UI_FONT",
        "CAVEVIEWER_UI_TEXT_SCALE",
        "CAVEVIEWER_UPDATE_BRANCH",
        "CAVEVIEWER_UPDATE_CHANNEL",
        "CAVEVIEWER_UPDATE_MANIFEST_SIGNATURE_URL",
        "CAVEVIEWER_UPDATE_MANIFEST_URL",
        "CAVEVIEWER_VIEWER_UI_SCALE",
        "CAVEVIEWER_VSYNC",
        "CAVEVIEWER_WINDOW_SYSTEM",
    }

    assert settings.runtime_environment_variable_names() == (
        {field.env_var for field in PREFERENCE_FIELDS} | expected_environment_only
    )
    assert not (
        settings.runtime_environment_variable_names()
        & settings.PACKAGING_OR_DEVELOPMENT_ENVIRONMENT_VARIABLES
    )
    assert settings.runtime_setting_spec("CAVEVIEWER_SAMPLE_MAPS_REPO").key == (
        "map_library_repository"
    )


def test_saved_preference_wins_over_environment_and_keeps_typed_provenance(tmp_path):
    snapshot = _resolve(
        tmp_path,
        preferences={"io_workers": " 5 ", "gpu_memory_gb": "3.5"},
        environ={
            "CAVEVIEWER_IO_WORKERS": "9",
            "CAVEVIEWER_GPU_MEMORY_GB": "7.5",
        },
    )

    assert snapshot["io_workers"] == 5
    assert snapshot["gpu_memory_gb"] == 3.5
    assert snapshot.source("io_workers") is settings.SettingSource.PREFERENCES
    assert snapshot.source("gpu_memory_gb") is settings.SettingSource.PREFERENCES


def test_invalid_saved_preference_falls_back_to_valid_environment_value(tmp_path):
    snapshot = _resolve(
        tmp_path,
        preferences={"io_workers": "many"},
        environ={"CAVEVIEWER_IO_WORKERS": "7"},
    )

    assert snapshot["io_workers"] == 7
    assert snapshot.source("io_workers") is settings.SettingSource.ENVIRONMENT
    assert snapshot.issues == (
        settings.RuntimeSettingIssue(
            key="io_workers",
            source=settings.SettingSource.PREFERENCES,
            raw_value="many",
            message="Loading worker limit must be a whole number.",
        ),
    )


def test_invalid_environment_value_falls_back_to_preference_built_in_default(tmp_path):
    snapshot = _resolve(
        tmp_path,
        environ={"CAVEVIEWER_IO_WORKERS": "999"},
    )

    assert snapshot["io_workers"] == 2
    assert snapshot.source("io_workers") is settings.SettingSource.BUILT_IN
    assert snapshot.issues[0].key == "io_workers"
    assert snapshot.issues[0].source is settings.SettingSource.ENVIRONMENT


def test_preference_environment_conversion_is_owned_by_preference_spec(tmp_path):
    snapshot = _resolve(
        tmp_path,
        environ={"CAVEVIEWER_OBJ_IMPORT_BATCH_FACES": "300000"},
    )

    assert snapshot["obj_import_batch_thousands"] == 300
    assert snapshot.source("obj_import_batch_thousands") is settings.SettingSource.ENVIRONMENT


def test_command_line_override_has_highest_precedence_without_mutating_inputs(tmp_path):
    environment = {"CAVEVIEWER_UPDATE_BRANCH": "environment-branch"}
    command_line = {"update_branch": "cli-branch"}

    snapshot = _resolve(
        tmp_path,
        environ=environment,
        cli_overrides=command_line,
    )

    assert snapshot["update_branch"] == "cli-branch"
    assert snapshot.source("update_branch") is settings.SettingSource.CLI
    assert environment == {"CAVEVIEWER_UPDATE_BRANCH": "environment-branch"}
    assert command_line == {"update_branch": "cli-branch"}


def test_map_library_aliases_and_derived_default_keep_current_precedence(tmp_path):
    legacy_snapshot = _resolve(
        tmp_path,
        environ={
            "CAVEVIEWER_SAMPLE_MAPS_REPO": "Example/Maps",
            "CAVEVIEWER_SAMPLE_DATA_TAG": "testing",
        },
    )
    primary_snapshot = _resolve(
        tmp_path,
        environ={
            "CAVEVIEWER_MAP_LIBRARY_REPO": "Primary/Maps",
            "CAVEVIEWER_SAMPLE_MAPS_REPO": "Example/Maps",
        },
    )

    assert legacy_snapshot["map_library_repository"] == "Example/Maps"
    assert legacy_snapshot["map_library_release_tag"] == "testing"
    assert legacy_snapshot["map_library_api_url"] == (
        "https://api.github.com/repos/Example/Maps/releases/tags/testing"
    )
    assert primary_snapshot["map_library_repository"] == "Primary/Maps"


def test_empty_update_url_is_an_explicit_environment_override(tmp_path):
    snapshot = _resolve(
        tmp_path,
        environ={"CAVEVIEWER_UPDATE_MANIFEST_URL": ""},
    )

    assert snapshot["update_manifest_url"] == ""
    assert snapshot.source("update_manifest_url") is settings.SettingSource.ENVIRONMENT


def test_platform_facts_control_dynamic_defaults_without_reading_process_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAVEVIEWER_IO_WORKERS", "15")

    linux_snapshot = _resolve(tmp_path, environ={}, platform_name="linux")
    windows_snapshot = _resolve(tmp_path, environ={}, platform_name="win32")

    assert linux_snapshot["io_workers"] == 2
    assert linux_snapshot["obj_scan_throttle_ms"] == 0.0
    assert windows_snapshot["obj_scan_throttle_ms"] == 1.0
    assert linux_snapshot["text_antialiasing_mode"] == "light"
    assert windows_snapshot["text_antialiasing_mode"] == "normal"
    assert linux_snapshot["recording_dir"] == str(tmp_path / "Movies" / "CaveViewer")
    assert linux_snapshot["map_library_dir"] == str(tmp_path / "Downloads")


def test_preference_defaults_accept_an_injected_default_context(tmp_path):
    context = PreferenceDefaultContext(
        environ={},
        platform_name="win32",
        home=tmp_path,
    )

    defaults = preference_defaults(environ={}, default_context=context)

    assert defaults["obj_scan_throttle_ms"] == "1"
    assert defaults["recording_dir"] == str(tmp_path / "Movies" / "CaveViewer")
    assert defaults["map_library_dir"] == str(tmp_path / "Downloads")


def test_range_clamping_and_fail_fast_window_system_validation(tmp_path):
    snapshot = _resolve(
        tmp_path,
        environ={
            "CAVEVIEWER_RECORDING_FPS": "999",
            "CAVEVIEWER_NAVIGATION_GUARD_RADIUS_CELLS": "-3",
        },
    )

    assert snapshot["recording_fps"] == 60
    assert snapshot["navigation_guard_radius_cells"] == 0

    with pytest.raises(settings.RuntimeSettingsResolutionError) as exc_info:
        _resolve(
            tmp_path,
            environ={"CAVEVIEWER_WINDOW_SYSTEM": "unsupported"},
        )

    assert exc_info.value.issue.key == "window_system"
    assert exc_info.value.issue.source is settings.SettingSource.ENVIRONMENT


def test_registry_preserves_existing_special_numeric_parser_behavior(tmp_path):
    bounded_snapshot = _resolve(
        tmp_path,
        environ={
            "CAVEVIEWER_OBJ_BUCKET_WORKERS": "99",
            "CAVEVIEWER_MAX_TEXTURE_SIZE": "1024.9",
            "CAVEVIEWER_IO_NICE": "-2",
            "CAVEVIEWER_IMPORT_NICE": "-2",
        },
    )
    invalid_snapshot = _resolve(
        tmp_path,
        environ={"CAVEVIEWER_OBJ_BUCKET_WORKERS": "0"},
    )

    assert bounded_snapshot["obj_bucket_workers"] == 32
    assert bounded_snapshot["max_texture_size"] == 1024
    assert bounded_snapshot["io_nice_increment"] == 0
    assert bounded_snapshot["import_nice_increment"] == -2
    assert invalid_snapshot["obj_bucket_workers"] == 2
    assert invalid_snapshot.source("obj_bucket_workers") is settings.SettingSource.BUILT_IN


def test_runtime_snapshot_is_immutable_and_exposes_mutable_value_copy(tmp_path):
    snapshot = _resolve(tmp_path, environ={"CAVEVIEWER_RECORDING_FPS": "24"})
    copy = snapshot.as_dict()
    copy["recording_fps"] = 12

    with pytest.raises(TypeError):
        snapshot["recording_fps"] = 12

    assert snapshot["recording_fps"] == 24
    assert snapshot.entry("recording_fps") == settings.ResolvedRuntimeSetting(
        value=24,
        source=settings.SettingSource.ENVIRONMENT,
    )
