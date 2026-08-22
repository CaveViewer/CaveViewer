"""Test cache-rebuild orchestration without Tk widgets or child processes."""

from caveviewer.gui.map_library_cache_rebuild_workflow import (
    MapLibraryCacheRebuildWorkflow,
)


class _Controller:
    active = True

    def __init__(self):
        self.pause_calls = []
        self.updates = ("progress",)

    def request_pause(self):
        self.pause_calls.append(False)
        return True

    def request_pause_for_close(self):
        self.pause_calls.append(True)
        return True

    def poll(self):
        self.active = False
        return self.updates


class _Scheduler:
    def __init__(self):
        self.callbacks = {}
        self.cancelled = []

    def after(self, _delay, callback):
        token = f"after-{len(self.callbacks)}"
        self.callbacks[token] = callback
        return token

    def after_cancel(self, token):
        self.cancelled.append(token)


def test_poll_token_and_update_delivery_have_one_owner():
    controller = _Controller()
    scheduler = _Scheduler()
    delivered = []
    workflow = MapLibraryCacheRebuildWorkflow(
        controller=controller,
        scheduler=scheduler,
        splash_exists=lambda: True,
        apply_updates=delivered.append,
    )

    workflow.schedule_poll()
    workflow.schedule_poll()
    assert tuple(scheduler.callbacks) == ("after-0",)

    scheduler.callbacks["after-0"]()
    assert delivered == [("progress",)]
    assert workflow.poll_scheduled is False


def test_close_pause_and_poll_cancellation_are_explicit():
    controller = _Controller()
    scheduler = _Scheduler()
    workflow = MapLibraryCacheRebuildWorkflow(
        controller=controller,
        scheduler=scheduler,
        splash_exists=lambda: True,
        apply_updates=lambda _updates: None,
    )
    workflow.schedule_poll()

    assert workflow.request_pause(for_close=True) is True
    workflow.cancel_poll()

    assert controller.pause_calls == [True]
    assert scheduler.cancelled == ["after-0"]
