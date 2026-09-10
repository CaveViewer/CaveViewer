"""Verify portable persistence and scaling for the primary Tk shell."""

from __future__ import annotations

import json

import pytest

from caveviewer.gui import shell_window_state
from caveviewer.gui.shell_window_state import ShellWindowState


def test_shell_window_state_round_trips_versioned_json(tmp_path):
    path = tmp_path / "shell_window_state.json"
    expected = ShellWindowState(
        normal_width=1040.5,
        normal_height=780.25,
        maximized=True,
    )

    assert shell_window_state.save_shell_window_state(expected, path)

    assert shell_window_state.load_shell_window_state(path) == expected
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "layout_revision": shell_window_state.SHELL_LAYOUT_REVISION,
        "normal_width": 1040.5,
        "normal_height": 780.25,
        "maximized": True,
    }


@pytest.mark.parametrize(
    "payload",
    (
        "not json",
        "[]",
        '{"version": 2, "layout_revision": 1, "normal_width": 1040, '
        '"normal_height": 780, "maximized": false}',
        '{"version": 1, "layout_revision": 2, "normal_width": 1040, '
        '"normal_height": 780, "maximized": false}',
        '{"version": 1, "layout_revision": 1, "normal_width": 0, '
        '"normal_height": 780, "maximized": false}',
        '{"version": 1, "layout_revision": 1, "normal_width": 1040, '
        '"normal_height": "780", "maximized": false}',
        '{"version": 1, "layout_revision": 1, "normal_width": 1040, '
        '"normal_height": 780, "maximized": 1}',
    ),
)
def test_shell_window_state_ignores_invalid_or_incompatible_data(tmp_path, payload):
    path = tmp_path / "shell_window_state.json"
    path.write_text(payload, encoding="utf-8")

    assert shell_window_state.load_shell_window_state(path) is None


def test_missing_shell_window_state_is_a_quiet_first_launch(tmp_path):
    assert shell_window_state.load_shell_window_state(tmp_path / "missing.json") is None


def test_shell_window_state_rejects_a_stale_layout_revision_on_save(tmp_path):
    path = tmp_path / "shell_window_state.json"

    assert not shell_window_state.save_shell_window_state(
        ShellWindowState(1040, 780, maximized=False, layout_revision=2),
        path,
    )
    assert not path.exists()


def test_remembered_logical_size_scales_once_and_clamps_to_work_area():
    state = ShellWindowState(1040, 780, maximized=False)

    assert shell_window_state.native_size_from_shell_state(
        state,
        layout_scale=1.5,
        minimum_size=(1260, 900),
        available_size=(3840, 2088),
    ) == (1560, 1170)
    assert shell_window_state.native_size_from_shell_state(
        state,
        layout_scale=1.5,
        minimum_size=(1260, 900),
        available_size=(1400, 1000),
    ) == (1400, 1000)


def test_intentionally_small_remembered_size_is_not_raised_to_default():
    state = ShellWindowState(900, 620, maximized=False)

    assert shell_window_state.native_size_from_shell_state(
        state,
        layout_scale=1.0,
        minimum_size=(840, 600),
        available_size=(1920, 1040),
    ) == (900, 620)


def test_settled_native_size_round_trips_through_logical_units():
    state = shell_window_state.shell_state_from_native_size(
        width=1482,
        height=1112,
        layout_scale=1.425,
        maximized=False,
    )

    assert shell_window_state.native_size_from_shell_state(
        state,
        layout_scale=1.425,
        minimum_size=(1, 1),
        available_size=(3840, 2088),
    ) == (1482, 1112)


def test_shell_window_state_write_failure_preserves_previous_file(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "shell_window_state.json"
    path.write_text("previous", encoding="utf-8")
    monkeypatch.setattr(
        "caveviewer.gui.shell_window_state.write_text_atomic",
        lambda *_args: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    assert not shell_window_state.save_shell_window_state(
        ShellWindowState(1040, 780, maximized=False),
        path,
    )
    assert path.read_text(encoding="utf-8") == "previous"
