"""Cover persisted recently opened map folders for the splash library."""

from __future__ import annotations

import json
import sys

from caveviewer.gui import map_history, standard_library_maps


def test_recent_map_history_uses_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    first = tmp_path / "first-map"
    second = tmp_path / "second-map"
    first.mkdir()
    second.mkdir()

    map_history.remember_recent_map_path(str(first))
    map_history.remember_recent_map_path(str(second))
    map_history.remember_recent_map_path(str(first))

    state_file = state_home / "caveviewer" / "recent_map_paths"
    assert json.loads(state_file.read_text(encoding="utf-8")) == [
        str(first),
        str(second),
    ]
    assert map_history.load_recent_map_paths() == [str(first), str(second)]


def test_recent_map_history_ignores_missing_and_invalid_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    existing = tmp_path / "existing-map"
    missing = tmp_path / "missing-map"
    existing.mkdir()
    state_file = state_home / "caveviewer" / "recent_map_paths"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps([str(missing), str(existing), str(existing), 42]),
        encoding="utf-8",
    )

    assert map_history.load_recent_map_paths() == [str(existing)]


def test_recent_map_history_excludes_app_supplied_standard_library_maps(
    tmp_path, monkeypatch
):
    caveviewer_home = tmp_path / "caveviewer-home"
    monkeypatch.setenv("CAVEVIEWER_HOME", str(caveviewer_home))
    sample = standard_library_maps.bundled_standard_library_catalog()[0]
    library_map_path = (
        caveviewer_home
        / "data"
        / standard_library_maps.MAP_LIBRARY_DIRNAME
        / sample.display_name
    )
    user_map = tmp_path / "user-map"
    library_map_path.mkdir(parents=True)
    user_map.mkdir()

    map_history.remember_recent_map_path(str(library_map_path))
    map_history.remember_recent_map_path(str(user_map))

    state_file = caveviewer_home / "state" / "recent_map_paths"
    assert json.loads(state_file.read_text(encoding="utf-8")) == [str(user_map)]
    assert map_history.load_recent_map_paths() == [str(user_map)]


def test_recent_map_history_filters_existing_standard_library_entries_on_load(
    tmp_path, monkeypatch
):
    caveviewer_home = tmp_path / "caveviewer-home"
    monkeypatch.setenv("CAVEVIEWER_HOME", str(caveviewer_home))
    sample = standard_library_maps.bundled_standard_library_catalog()[0]
    library_map_path = (
        caveviewer_home
        / "data"
        / standard_library_maps.MAP_LIBRARY_DIRNAME
        / sample.display_name
    )
    user_map = tmp_path / "user-map"
    library_map_path.mkdir(parents=True)
    user_map.mkdir()
    state_file = caveviewer_home / "state" / "recent_map_paths"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps([str(library_map_path), str(user_map)]),
        encoding="utf-8",
    )

    assert map_history.load_recent_map_paths() == [str(user_map)]


def test_recent_map_history_can_remove_and_readd_user_map(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    first = tmp_path / "first-map"
    second = tmp_path / "second-map"
    first.mkdir()
    second.mkdir()

    map_history.remember_recent_map_path(str(first))
    map_history.remember_recent_map_path(str(second))
    map_history.remove_recent_map_path(str(first))

    assert map_history.load_recent_map_paths() == [str(second)]

    map_history.remember_recent_map_path(str(first))

    assert map_history.load_recent_map_paths() == [str(first), str(second)]


def test_recent_map_history_preserves_path_casing_for_display(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    mixed_case_map = tmp_path / "Mixed-Case-Map"
    mixed_case_map.mkdir()
    monkeypatch.setattr(map_history.os.path, "normcase", lambda path: path.lower())

    map_history.remember_recent_map_path(str(mixed_case_map))

    state_file = state_home / "caveviewer" / "recent_map_paths"
    assert json.loads(state_file.read_text(encoding="utf-8")) == [
        str(mixed_case_map)
    ]
    assert map_history.load_recent_map_paths() == [str(mixed_case_map)]


def test_recent_map_history_ignores_malformed_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    state_file = state_home / "caveviewer" / "recent_map_paths"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("not json", encoding="utf-8")

    assert map_history.load_recent_map_paths() == []
