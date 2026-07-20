from __future__ import annotations

import threading
from types import SimpleNamespace

from caveviewer.gui.import_controller import MapImportController


class FakeLogger:
    def __init__(self):
        self.info_messages = []

    def info(self, message, *args):
        self.info_messages.append(message % args if args else str(message))


class FakeThread:
    def __init__(self, *, alive: bool):
        self._alive = alive
        self.join_calls = []

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)
        self._alive = False


def _controller(logger=None, terminate_calls=None):
    active_logger = logger or FakeLogger()
    calls = [] if terminate_calls is None else terminate_calls

    def terminate(process, **kwargs):
        calls.append((process, kwargs))

    controller = MapImportController(
        SimpleNamespace(),
        logger=lambda: active_logger,
        chunker=lambda: SimpleNamespace(),
        start_import_process=lambda: None,
        terminate_import_process=lambda: terminate,
        acquire_inhibitor=lambda: None,
        release_inhibitor=lambda: None,
        perf_counter=lambda: 0.0,
        monotonic=lambda: 0.0,
    )
    return controller, active_logger, calls


def test_cancel_active_import_signals_live_relay_without_joining():
    controller, logger, calls = _controller()
    controller.stop_event = threading.Event()
    controller.thread = FakeThread(alive=True)
    controller.process = object()

    controller.cancel_active_import()

    assert controller.stop_event.is_set()
    assert controller.thread.join_calls == []
    assert calls == []
    assert logger.info_messages == [
        "Import cancellation requested; relay worker will terminate the child process."
    ]


def test_cancel_active_import_uses_zero_timeout_cleanup_without_relay():
    process = object()
    controller, _logger, calls = _controller()
    controller.stop_event = threading.Event()
    controller.thread = None
    controller.process = process
    controller.cache_dir = "/cache/cave"

    controller.cancel_active_import()

    assert controller.stop_event.is_set()
    assert calls == [(process, {"timeout": 0.0, "cache_dir": "/cache/cave"})]
