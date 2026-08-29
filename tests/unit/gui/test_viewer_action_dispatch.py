"""Unit coverage for the viewer's non-OpenGL key-action priority."""

from __future__ import annotations

from caveviewer.gui.viewer_action_dispatch import (
    ViewerActionDispatcher,
    ViewerKeyPressActions,
)


def _action(calls: list[str], name: str, result: bool):
    def run() -> bool:
        calls.append(name)
        return result

    return run


def test_key_press_stops_at_the_first_consuming_action():
    calls: list[str] = []
    actions = ViewerKeyPressActions(
        window_shortcut=_action(calls, "window", False),
        recorded_dive=_action(calls, "recorded_dive", False),
        begin_screen=_action(calls, "begin", True),
        capture_escape=_action(calls, "escape", False),
        fly_speed=_action(calls, "speed", True),
        bookmark=_action(calls, "bookmark", True),
        manual_dive_trace=_action(calls, "trace", True),
        slice=_action(calls, "slice", True),
        recording=_action(calls, "recording", True),
        reset_view=_action(calls, "reset", True),
    )

    assert ViewerActionDispatcher().dispatch_key_press(actions)
    assert calls == ["window", "escape", "recorded_dive", "begin"]


def test_capture_escape_precedes_other_viewer_actions():
    calls: list[str] = []
    actions = ViewerKeyPressActions(
        **{
            name: _action(
                calls,
                name,
                name in {"capture_escape", "fly_speed"},
            )
            for name in ViewerKeyPressActions.__dataclass_fields__
        }
    )

    assert ViewerActionDispatcher().dispatch_key_press(actions)
    assert calls == [
        "window_shortcut",
        "capture_escape",
    ]


def test_key_press_reports_unhandled_when_every_action_declines():
    calls: list[str] = []
    actions = ViewerKeyPressActions(
        **{
            name: _action(calls, name, False)
            for name in ViewerKeyPressActions.__dataclass_fields__
        }
    )

    assert not ViewerActionDispatcher().dispatch_key_press(actions)
    assert calls == [
        "window_shortcut",
        "capture_escape",
        "recorded_dive",
        "begin_screen",
        "fly_speed",
        "bookmark",
        "manual_dive_trace",
        "slice",
        "recording",
        "reset_view",
    ]


def test_repeat_skips_speed_actions_while_the_begin_screen_owns_input():
    calls: list[str] = []

    assert not ViewerActionDispatcher.dispatch_key_repeat(
        waiting_for_begin=True,
        fly_speed=_action(calls, "speed", True),
    )
    assert calls == []

    assert ViewerActionDispatcher.dispatch_key_repeat(
        waiting_for_begin=False,
        fly_speed=_action(calls, "speed", True),
    )
    assert calls == ["speed"]
