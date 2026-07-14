"""Exercise XDG preference/state migration and failure cleanup."""

from __future__ import annotations

from pathlib import Path

from caveviewer.gui import preference_paths as preferences


def test_current_legacy_config_is_copied_once_without_deletion(tmp_path, monkeypatch):
    home = Path.home()
    old_path = home / ".caveviewer" / "advanced_settings.json"
    old_path.parent.mkdir()
    old_path.write_text("legacy", encoding="utf-8")
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    migrated = Path(
        preferences.migrate_preference_file(
            "advanced_settings.json", ".caveviewer_advanced_settings.json"
        )
    )

    assert migrated == config_home / "caveviewer" / "advanced_settings.json"
    assert migrated.read_text(encoding="utf-8") == "legacy"
    assert old_path.read_text(encoding="utf-8") == "legacy"

    old_path.write_text("changed", encoding="utf-8")
    assert preferences.migrate_preference_file(
        "advanced_settings.json", ".caveviewer_advanced_settings.json"
    ) == str(migrated)
    assert migrated.read_text(encoding="utf-8") == "legacy"


def test_ui_history_uses_xdg_state_home(tmp_path, monkeypatch):
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
