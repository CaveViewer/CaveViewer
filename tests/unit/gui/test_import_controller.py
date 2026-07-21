"""Tests for map import controller lifecycle behavior."""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

from caveviewer.gui.import_controller import MapImportController


class FakeLogger:
    def __init__(self):
        self.info_messages = []
        self.warning_messages = []

    def info(self, message, *args):
        self.info_messages.append(message % args if args else str(message))

    def warning(self, message, *args):
        self.warning_messages.append(message % args if args else str(message))


class FakeThread:
    def __init__(self, *, alive: bool, finish_on_join: bool = True):
        self._alive = alive
        self._finish_on_join = finish_on_join
        self.join_calls = []

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)
        if self._finish_on_join:
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


def test_shutdown_joins_live_relay_and_clears_import_references():
    process = object()
    controller, _logger, calls = _controller()
    stop_event = threading.Event()
    thread = FakeThread(alive=True)
    controller.active = True
    controller.stop_event = stop_event
    controller.thread = thread
    controller.process = process
    controller.cache_dir = "/cache/cave"
    controller.event_queue = queue.Queue()
    controller.event_queue.put(("progress", "building cache", 0.5))

    controller.shutdown(timeout=1.25)

    assert stop_event.is_set()
    assert thread.join_calls == [1.25]
    assert calls == []
    assert controller.active is False
    assert controller.thread is None
    assert controller.process is None
    assert controller.event_queue is None
    assert controller.cache_dir is None


def test_shutdown_terminates_child_directly_when_relay_is_gone():
    process = object()
    controller, _logger, calls = _controller()
    stop_event = threading.Event()
    controller.active = True
    controller.stop_event = stop_event
    controller.thread = None
    controller.process = process
    controller.cache_dir = "/cache/cave"

    controller.shutdown(timeout=1.5)

    assert stop_event.is_set()
    assert calls == [(process, {"timeout": 1.5, "cache_dir": "/cache/cave"})]
    assert controller.active is False
    assert controller.process is None
    assert controller.cache_dir is None


def test_shutdown_detaches_relay_that_does_not_stop_within_timeout():
    process = object()
    controller, logger, calls = _controller()
    stop_event = threading.Event()
    thread = FakeThread(alive=True, finish_on_join=False)
    controller.active = True
    controller.stop_event = stop_event
    controller.thread = thread
    controller.process = process
    controller.cache_dir = "/cache/cave"

    controller.shutdown(timeout=0.3)

    assert stop_event.is_set()
    assert thread.join_calls == [0.3]
    assert calls == [(process, {"timeout": 0.0, "cache_dir": "/cache/cave"})]
    assert logger.warning_messages == [
        "Import shutdown timed out after 0.3s; relay worker will remain "
        "detached while the application exits."
    ]
    assert controller.active is False
    assert controller.thread is None
    assert controller.process is None


def test_shutdown_does_not_join_current_thread():
    controller, _logger, calls = _controller()
    stop_event = threading.Event()
    controller.active = True
    controller.stop_event = stop_event
    controller.thread = threading.current_thread()
    controller.process = object()

    controller.shutdown(timeout=1.0)

    assert stop_event.is_set()
    assert calls == []
    assert controller.active is False


def test_drain_queue_ignores_late_messages_after_shutdown():
    loaded = []
    owner = SimpleNamespace(load_new_map=lambda *args: loaded.append(args))
    controller, _logger, _calls = _controller()
    controller._owner = owner
    controller.active = True
    controller._shutdown_requested = True
    controller.event_queue = queue.Queue()
    controller.event_queue.put(("done", "/cache/cave", "/textures/cave"))

    controller.drain_queue()

    assert loaded == []
    assert controller.active is False
    assert controller.event_queue is None
