"""Camera bookmark persistence and hotkey policy for the viewer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
import json
import logging
from os import PathLike
from pathlib import Path
from typing import Any, TypedDict


class CameraBookmark(TypedDict):
    position: list[float]
    yaw: float
    pitch: float


BookmarkSlots = dict[int, CameraBookmark]


class BookmarkHotkeyAction(Enum):
    NONE = "none"
    SAVE = "save"
    RECALL = "recall"
    DELETE = "delete"


_VALID_SLOTS = range(1, 10)


def bookmark_from_camera(
    position: Sequence[float],
    *,
    yaw: float,
    pitch: float,
) -> CameraBookmark:
    return {
        "position": [
            float(position[0]),
            float(position[1]),
            float(position[2]),
        ],
        "yaw": float(yaw),
        "pitch": float(pitch),
    }


def load_bookmarks(
    bookmarks_path: str | PathLike[str] | None,
    *,
    logger: logging.Logger | None = None,
) -> BookmarkSlots:
    if not bookmarks_path:
        return {}

    path = Path(bookmarks_path)
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as file_obj:
            raw = json.load(file_obj)
    except Exception as exc:
        if logger is not None:
            logger.warning("Failed to load bookmarks: %s", exc)
        return {}
    return bookmarks_from_payload(raw)


def save_bookmarks(
    bookmarks_path: str | PathLike[str] | None,
    bookmarks: Mapping[int, CameraBookmark],
    *,
    logger: logging.Logger | None = None,
) -> None:
    if not bookmarks_path:
        return

    path = Path(bookmarks_path)
    try:
        with path.open("w", encoding="utf-8") as file_obj:
            json.dump(bookmarks_payload(bookmarks), file_obj, indent=2)
    except Exception as exc:
        if logger is not None:
            logger.warning("Failed to save bookmarks: %s", exc)


def bookmarks_from_payload(raw: object) -> BookmarkSlots:
    slots = raw.get("slots", {}) if isinstance(raw, Mapping) else {}
    bookmarks: BookmarkSlots = {}
    if not isinstance(slots, Mapping):
        return bookmarks

    for slot_str, payload in slots.items():
        try:
            slot = int(slot_str)
        except (TypeError, ValueError):
            continue
        if slot not in _VALID_SLOTS:
            continue
        bookmark = bookmark_from_payload(payload)
        if bookmark is not None:
            bookmarks[slot] = bookmark
    return bookmarks


def bookmarks_payload(bookmarks: Mapping[int, CameraBookmark]) -> dict[str, Any]:
    return {
        "version": 1,
        "slots": {
            str(slot): bookmark
            for slot, bookmark in sorted(bookmarks.items())
            if slot in _VALID_SLOTS
        },
    }


def bookmark_from_payload(payload: object) -> CameraBookmark | None:
    if not isinstance(payload, Mapping):
        return None

    position = payload.get("position")
    yaw = payload.get("yaw")
    pitch = payload.get("pitch")
    if (
        isinstance(position, (str, bytes))
        or not isinstance(position, Sequence)
        or len(position) != 3
        or yaw is None
        or pitch is None
    ):
        return None

    try:
        return {
            "position": [
                float(position[0]),
                float(position[1]),
                float(position[2]),
            ],
            "yaw": float(yaw),
            "pitch": float(pitch),
        }
    except (TypeError, ValueError):
        return None


def bookmark_hotkey_action(
    slot: int | None,
    *,
    save_modifier_down: bool,
    shift_down: bool,
    ctrl_down: bool,
    backspace_down: bool,
    shift_digit_save_fallback: bool,
) -> BookmarkHotkeyAction:
    if slot not in _VALID_SLOTS:
        return BookmarkHotkeyAction.NONE

    if backspace_down:
        return BookmarkHotkeyAction.DELETE

    if ctrl_down and shift_down:
        return BookmarkHotkeyAction.DELETE

    if save_modifier_down:
        return BookmarkHotkeyAction.SAVE

    if shift_digit_save_fallback and shift_down:
        return BookmarkHotkeyAction.SAVE

    return BookmarkHotkeyAction.RECALL
