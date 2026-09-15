"""Verify durable, isolated viewer-window size persistence."""

from __future__ import annotations

import json

from caveviewer.gui import viewer_window_state
from caveviewer.gui.viewer_window_state import ViewerWindowState


def test_viewer_window_state_round_trips_versioned_json(tmp_path):
    path = tmp_path / "viewer_window_state.json"
    expected = ViewerWindowState(width=1680, height=960)

    assert viewer_window_state.save_viewer_window_state(expected, path)
    assert viewer_window_state.load_viewer_window_state(path) == expected
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "height": 960,
        "version": 1,
        "width": 1680,
    }


def test_viewer_window_state_ignores_invalid_or_missing_data(tmp_path):
    path = tmp_path / "viewer_window_state.json"

    assert viewer_window_state.load_viewer_window_state(path) is None

    path.write_text(
        '{"version": 1, "width": 0, "height": 960}',
        encoding="utf-8",
    )
    assert viewer_window_state.load_viewer_window_state(path) is None

    path.write_text(
        '{"version": 1, "width": true, "height": 960}',
        encoding="utf-8",
    )
    assert viewer_window_state.load_viewer_window_state(path) is None


def test_viewer_window_state_write_failure_preserves_previous_file(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "viewer_window_state.json"
    path.write_text("previous", encoding="utf-8")
    monkeypatch.setattr(
        viewer_window_state,
        "write_text_atomic",
        lambda *_args: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    assert not viewer_window_state.save_viewer_window_state(
        ViewerWindowState(width=1680, height=960),
        path,
    )
    assert path.read_text(encoding="utf-8") == "previous"
