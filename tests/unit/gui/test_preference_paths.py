"""Exercise XDG preference/state migration and failure cleanup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from caveviewer.gui import preference_paths as preferences


def test_previous_preferences_file_is_renamed_once(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / "config"
    old_path = config_home / "caveviewer" / "advanced_settings.json"
    old_path.parent.mkdir(parents=True)
    old_path.write_text("legacy", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    migrated = Path(
        preferences.migrate_preference_file(
            "preferences.json", "advanced_settings.json"
        )
    )

    assert migrated == config_home / "caveviewer" / "preferences.json"
    assert migrated.read_text(encoding="utf-8") == "legacy"
    assert not old_path.exists()

    old_path.write_text("stale", encoding="utf-8")
    assert preferences.migrate_preference_file(
        "preferences.json", "advanced_settings.json"
    ) == str(migrated)
    assert migrated.read_text(encoding="utf-8") == "legacy"
    assert old_path.read_text(encoding="utf-8") == "stale"


def test_failed_preference_rename_returns_readable_previous_file(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / "config"
    old_path = config_home / "caveviewer" / "advanced_settings.json"
    old_path.parent.mkdir(parents=True)
    old_path.write_text("legacy", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(
        preferences.os,
        "rename",
        lambda *_args: (_ for _ in ()).throw(PermissionError("blocked")),
    )

    readable = Path(
        preferences.migrate_preference_file(
            "preferences.json", "advanced_settings.json"
        )
    )

    assert readable == old_path
    assert readable.read_text(encoding="utf-8") == "legacy"
    assert not (config_home / "caveviewer" / "preferences.json").exists()


@pytest.mark.parametrize("platform_name", ["win32", "darwin", "linux"])
def test_preference_rename_uses_config_directory_on_supported_platforms(
    platform_name,
    tmp_path,
    monkeypatch,
):
    application_home = tmp_path / "application-home"
    config_dir = application_home / "config"
    config_dir.mkdir(parents=True)
    previous = config_dir / "advanced_settings.json"
    previous.write_text("portable", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setenv("CAVEVIEWER_HOME", str(application_home))

    migrated = Path(
        preferences.migrate_preference_file(
            "preferences.json", "advanced_settings.json"
        )
    )

    assert migrated == config_dir / "preferences.json"
    assert migrated.read_text(encoding="utf-8") == "portable"
    assert not previous.exists()


def test_ui_history_uses_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    path = Path(
        preferences.migrate_state_file(
            "last_browse_path", ".caveviewer_last_browse_path"
        )
    )

    assert path == state_home / "caveviewer" / "last_browse_path"


def test_current_legacy_state_is_copied_to_xdg_state_without_deletion(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    home = Path.home()
    old_path = home / ".caveviewer" / "last_browse_path"
    old_path.parent.mkdir()
    old_path.write_text("/maps/legacy", encoding="utf-8")
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    migrated = Path(
        preferences.migrate_state_file(
            "last_browse_path", ".caveviewer_last_browse_path"
        )
    )

    assert migrated == state_home / "caveviewer" / "last_browse_path"
    assert migrated.read_text(encoding="utf-8") == "/maps/legacy"
    assert old_path.read_text(encoding="utf-8") == "/maps/legacy"


def test_failed_migration_leaves_no_partial_target(tmp_path, monkeypatch):
    legacy = Path.home() / ".caveviewer_old_state"
    legacy.write_text("legacy", encoding="utf-8")
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setattr(
        preferences.shutil,
        "copy2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("copy failed")),
    )

    target = Path(
        preferences.migrate_state_file("old_state", ".caveviewer_old_state")
    )

    assert not target.exists()
    assert not list(target.parent.glob(".old_state.*.tmp"))


def test_atomic_state_write_preserves_previous_value_on_replace_failure(
    tmp_path, monkeypatch
):
    path = tmp_path / "last_browse_path"
    path.write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        preferences.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    try:
        preferences.write_text_atomic(str(path), "new")
    except OSError as error:
        assert str(error) == "replace failed"
    else:
        raise AssertionError("replace failure should propagate")

    assert path.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".last_browse_path.*.tmp"))
