"""Test splash lifecycle ownership without constructing Tk widgets."""

import pytest

from caveviewer.gui.splash_controller import SplashController, SplashScheduler


class _Scheduler:
    def __init__(self):
        self.callbacks = {}
        self.cancelled = []

    def after(self, delay_ms, callback):
        token = f"after-{len(self.callbacks)}"
        self.callbacks[token] = (delay_ms, callback)
        return token

    def after_cancel(self, token):
        self.cancelled.append(token)


def _controller():
    scheduler = _Scheduler()
    controller = SplashController(
        SplashScheduler(scheduler.after, scheduler.after_cancel)
    )
    return controller, scheduler


def test_controller_owns_selection_and_scheduled_callback():
    controller, scheduler = _controller()
    calls = []
    controller.start()
    controller.select_folder("/maps/cave")
    token = controller.schedule(25, lambda: calls.append("ran"))

    scheduler.callbacks[token][1]()

    assert calls == ["ran"]
    assert controller.selected_folder == "/maps/cave"


def test_close_is_idempotent_and_ignores_late_callback():
    controller, scheduler = _controller()
    calls = []
    controller.start()
    token = controller.schedule(25, lambda: calls.append("late"))

    controller.close()
    controller.close()
    scheduler.callbacks[token][1]()

    assert controller.closing is True
    assert scheduler.cancelled == [token]
    assert calls == []


def test_controller_cannot_schedule_before_start_or_after_close():
    controller, _scheduler = _controller()

    with pytest.raises(RuntimeError):
        controller.schedule(1, lambda: None)
    controller.start()
    controller.close()
    with pytest.raises(RuntimeError):
        controller.schedule(1, lambda: None)
