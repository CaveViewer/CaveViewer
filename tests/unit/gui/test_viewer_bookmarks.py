"""Unit tests for viewer camera bookmark persistence and hotkey policy."""

import json

from caveviewer.gui import viewer_bookmarks


def test_load_bookmarks_filters_invalid_slots_and_payloads(tmp_path):
    path = tmp_path / "camera_bookmarks.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "slots": {
                    "1": {"position": [1, 2, 3], "yaw": "1.5", "pitch": "0.25"},
                    "0": {"position": [9, 9, 9], "yaw": 0, "pitch": 0},
                    "10": {"position": [9, 9, 9], "yaw": 0, "pitch": 0},
                    "bad": {"position": [9, 9, 9], "yaw": 0, "pitch": 0},
                    "2": {"position": [1, 2], "yaw": 0, "pitch": 0},
                    "3": {"position": ["x", 2, 3], "yaw": 0, "pitch": 0},
                    "4": {"position": [4, 5, 6], "yaw": 0, "pitch": None},
                },
            }
        ),
        encoding="utf-8",
    )

    assert viewer_bookmarks.load_bookmarks(path) == {
        1: {"position": [1.0, 2.0, 3.0], "yaw": 1.5, "pitch": 0.25}
    }


def test_save_bookmarks_writes_stable_payload(tmp_path):
    path = tmp_path / "camera_bookmarks.json"
    viewer_bookmarks.save_bookmarks(
        path,
        {
            2: {"position": [2.0, 3.0, 4.0], "yaw": 1.0, "pitch": 0.5},
            1: {"position": [1.0, 2.0, 3.0], "yaw": 0.0, "pitch": -0.5},
            10: {"position": [10.0, 0.0, 0.0], "yaw": 0.0, "pitch": 0.0},
        },
    )

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "slots": {
            "1": {"position": [1.0, 2.0, 3.0], "yaw": 0.0, "pitch": -0.5},
            "2": {"position": [2.0, 3.0, 4.0], "yaw": 1.0, "pitch": 0.5},
        },
    }


def test_bookmark_from_camera_normalizes_numeric_values():
    assert viewer_bookmarks.bookmark_from_camera(
        (1, 2, 3),
        yaw="1.25",
        pitch="0.5",
    ) == {"position": [1.0, 2.0, 3.0], "yaw": 1.25, "pitch": 0.5}


def test_bookmark_hotkey_action_prefers_delete_over_save():
    action = viewer_bookmarks.bookmark_hotkey_action(
        3,
        save_modifier_down=True,
        shift_down=True,
        ctrl_down=True,
        backspace_down=False,
        shift_digit_save_fallback=True,
    )

    assert action is viewer_bookmarks.BookmarkHotkeyAction.DELETE


def test_bookmark_hotkey_action_supports_save_fallback_and_recall():
    assert (
        viewer_bookmarks.bookmark_hotkey_action(
            3,
            save_modifier_down=False,
            shift_down=True,
            ctrl_down=False,
            backspace_down=False,
            shift_digit_save_fallback=True,
        )
        is viewer_bookmarks.BookmarkHotkeyAction.SAVE
    )
    assert (
        viewer_bookmarks.bookmark_hotkey_action(
            3,
            save_modifier_down=False,
            shift_down=True,
            ctrl_down=False,
            backspace_down=False,
            shift_digit_save_fallback=False,
        )
        is viewer_bookmarks.BookmarkHotkeyAction.RECALL
    )
    assert (
        viewer_bookmarks.bookmark_hotkey_action(
            None,
            save_modifier_down=True,
            shift_down=True,
            ctrl_down=True,
            backspace_down=True,
            shift_digit_save_fallback=True,
        )
        is viewer_bookmarks.BookmarkHotkeyAction.NONE
    )
