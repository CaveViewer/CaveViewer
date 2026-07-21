"""Cover persisted recently opened map folders for the splash library."""

from __future__ import annotations

import json
import sys

from caveviewer.gui import map_history


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


def test_recent_map_history_ignores_malformed_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    state_file = state_home / "caveviewer" / "recent_map_paths"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("not json", encoding="utf-8")

    assert map_history.load_recent_map_paths() == []
