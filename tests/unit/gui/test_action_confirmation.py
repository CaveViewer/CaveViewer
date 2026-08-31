"""Tests for shared transient confirmations beside completed actions."""

from __future__ import annotations

from caveviewer.gui.action_confirmation import (
    ACTION_CONFIRMATION_MS,
    TransientActionConfirmation,
)


class _Scheduler:
    def __init__(self) -> None:
        self.callbacks = {}
        self.cancelled = []

    def after(self, duration_ms, callback):
        after_id = f"timer-{len(self.callbacks) + 1}"
        self.callbacks[after_id] = (duration_ms, callback)
        return after_id

    def after_cancel(self, after_id):
        self.cancelled.append(after_id)


def test_confirmation_uses_three_seconds_and_hides_on_timeout():
    scheduler = _Scheduler()
    visibility = []
    confirmation = TransientActionConfirmation(
        scheduler,
        on_visibility_changed=visibility.append,
    )

    confirmation.show()

    assert confirmation.visible is True
    assert visibility == [True]
    duration_ms, callback = scheduler.callbacks["timer-1"]
    assert duration_ms == ACTION_CONFIRMATION_MS == 3_000

    callback()

    assert confirmation.visible is False
    assert visibility == [True, False]


def test_repeated_confirmation_restarts_timer_without_repainting_mark():
    scheduler = _Scheduler()
    visibility = []
    confirmation = TransientActionConfirmation(
        scheduler,
        on_visibility_changed=visibility.append,
    )

    confirmation.show()
    confirmation.show()

    assert scheduler.cancelled == ["timer-1"]
    assert visibility == [True]
    assert scheduler.callbacks["timer-2"][0] == ACTION_CONFIRMATION_MS


def test_clear_cancels_timer_and_hides_immediately():
    scheduler = _Scheduler()
    visibility = []
    confirmation = TransientActionConfirmation(
        scheduler,
        on_visibility_changed=visibility.append,
    )
    confirmation.show()

    confirmation.clear()

    assert scheduler.cancelled == ["timer-1"]
    assert confirmation.visible is False
    assert visibility == [True, False]
