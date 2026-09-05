"""Tests for map import controller lifecycle behavior."""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

from caveviewer.gui.import_controller import MapImportController
from caveviewer.gui.map_opening_progress import MapOpeningProgressSession


class FakeLogger:
    def __init__(self):
        self.info_messages = []
        self.warning_messages = []
        self.error_messages = []

    def info(self, message, *args):
        self.info_messages.append(message % args if args else str(message))

    def warning(self, message, *args):
        self.warning_messages.append(message % args if args else str(message))

    def error(self, message, *args):
        self.error_messages.append(message % args if args else str(message))


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


def test_close_requests_obj_checkpoint_with_a_bounded_deadline():
    controller, logger, _calls = _controller()
    controller.active = True
    controller.model_format = "obj"
    controller.command_queue = queue.Queue()
    controller._perf_counter = lambda: 10.0

    assert controller.request_pause_for_close() is True
    assert controller.command_queue.get_nowait() == ("pause",)
    assert controller._close_pause_deadline == 13.0
    assert controller.progress_title == ""
    assert controller.progress_note == "Saving a resume point."
    assert controller.transient_progress_note() == controller.progress_note
    assert logger.info_messages == [
        "Import pause requested; waiting for the current safe checkpoint."
    ]


def test_obj_pause_support_normalizes_the_descriptor_format():
    assert MapImportController.import_model_format_from_descriptor(
        {"format": "OBJ"}
    ) == "obj"


def test_resuming_import_message_returns_to_normal_progress_after_three_seconds():
    controller, _logger, _calls = _controller()
    clock = [10.0]
    controller._perf_counter = lambda: clock[0]

    controller.update_progress_message_for_stage("resuming import")

    assert controller.progress_title == ""
    assert controller.progress_note == "Using saved work from the previous session."
    assert controller.transient_progress_note() == controller.progress_note

    clock[0] = 13.0
    controller.update_progress_message_for_stage("resuming import")

    assert controller.progress_title == ""
    assert controller.progress_note == controller.default_progress_note()
    assert controller.transient_progress_note() is None


def test_pause_notice_renders_at_the_supplied_framebuffer_size():
    rendered = []

    class FakePanel:
        def render(self, *args, **kwargs):
            rendered.append((args, kwargs))

    controller, _logger, _calls = _controller()
    controller.pause_notice_until = 1.0
    controller.pause_notice_map_name = "cave.obj"
    controller._perf_counter = lambda: 0.0
    window = SimpleNamespace(size=(800, 600))

    assert controller.render_pause_notice_if_active(
        FakePanel(), window, (820, 600)
    ) is True
    assert rendered[0][0][0] == (820, 600)


def test_pending_import_splash_uses_the_shared_opening_presentation_session_without_a_duplicate_title():
    rendered = []

    class FakePanel:
        def render(self, *args, **kwargs):
            rendered.append((args, kwargs))

    controller, _logger, _calls = _controller()
    session = MapOpeningProgressSession()

    controller.render_pending_import_splash(
        {"model_descriptor": {"obj_path": "/maps/cave.obj"}},
        FakePanel(),
        (820, 600),
        opening_session=session,
    )

    args, kwargs = rendered[0]
    assert args == ((820, 600), "cave.obj", "starting import", 0.0)
    assert kwargs["title"] == ""
    assert kwargs["note"] == (
        "First-time setup in progress. Next time, this map will open faster."
    )
    assert kwargs["progress_session_id"] == session.session_id


def test_close_after_paused_import_releases_the_viewer_without_a_notice():
    calls = []
    owner = SimpleNamespace(
        _has_map_loaded=False,
        _hide_window_before_close=lambda: calls.append("hide_viewer"),
        _complete_window_close=lambda: calls.append("close_viewer"),
    )
    controller, logger, _terminate_calls = _controller()
    controller._owner = owner
    controller.active = True
    controller.is_startup = True
    controller.map_name = "cave.obj"
    controller._close_pause_deadline = 13.0
    controller.event_queue = queue.Queue()
    controller.event_queue.put(("paused", "/cache/.cave.resume-123"))

    controller.drain_queue()

    assert calls == ["hide_viewer", "close_viewer"]
    assert controller.pause_notice_until is None
    assert "Resume checkpoint saved; closing viewer." in logger.info_messages


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


def test_done_message_loads_map_with_original_source_dir():
    manifest = {"chunks": {}}
    loaded = []
    owner = SimpleNamespace(
        load_new_map=lambda *args, **kwargs: loaded.append((args, kwargs))
    )
    controller, _logger, _calls = _controller()
    controller._owner = owner
    controller._chunker = lambda: SimpleNamespace(
        load_manifest=lambda _cache_dir: manifest
    )
    controller.active = True
    controller.source_dir = "/maps/Original Cave"
    controller.event_queue = queue.Queue()
    controller.event_queue.put(("done", "/cache/cave", "/cache/cave"))

    controller.drain_queue()

    assert loaded == [
        (
            ("/cache/cave", "/cache/cave", manifest),
            {"source_dir": "/maps/Original Cave"},
        )
    ]
    assert controller.active is False
    assert controller.source_dir is None


def test_done_message_keeps_the_opening_session_for_initial_streaming():
    manifest = {"chunks": {}}
    calls = []
    owner = SimpleNamespace(
        load_new_map=lambda *args, **kwargs: calls.append((args, kwargs)),
        _abandon_map_opening_progress=lambda: calls.append("abandon"),
    )
    controller, _logger, _calls = _controller()
    controller._owner = owner
    controller._chunker = lambda: SimpleNamespace(
        load_manifest=lambda _cache_dir: manifest
    )
    controller.active = True
    controller.event_queue = queue.Queue()
    controller.event_queue.put(("done", "/cache/cave", "/cache/cave"))

    controller.drain_queue()

    assert calls == [
        (("/cache/cave", "/cache/cave", manifest), {"source_dir": None})
    ]


def test_cancelled_import_abandons_only_the_presentation_session():
    abandoned = []
    owner = SimpleNamespace(
        _abandon_map_opening_progress=lambda: abandoned.append(True),
    )
    controller, _logger, _calls = _controller()
    controller._owner = owner
    controller.active = True
    controller.event_queue = queue.Queue()
    controller.event_queue.put(("cancelled",))

    controller.drain_queue()

    assert controller.active is False
    assert abandoned == [True]


def test_failed_or_paused_import_abandons_the_presentation_session():
    failed = []
    failed_owner = SimpleNamespace(
        _abandon_map_opening_progress=lambda: failed.append(True),
    )
    controller, _logger, _calls = _controller()
    controller._owner = failed_owner
    controller.active = True
    controller.event_queue = queue.Queue()
    controller.event_queue.put(("error", "could not build cache"))

    controller.drain_queue()

    paused = []
    paused_owner = SimpleNamespace(
        _has_map_loaded=False,
        _abandon_map_opening_progress=lambda: paused.append(True),
    )
    pause_controller, _logger, _calls = _controller()
    pause_controller._owner = paused_owner
    pause_controller.active = True
    pause_controller.map_name = "cave.obj"
    pause_controller.event_queue = queue.Queue()
    pause_controller.event_queue.put(("paused", "/cache/.cave.resume"))

    pause_controller.drain_queue()

    assert failed == [True]
    assert paused == [True]


def test_startup_import_failure_is_reported_before_the_empty_viewer_closes():
    calls = []
    owner = SimpleNamespace(
        wnd=SimpleNamespace(close=lambda: calls.append("close")),
    )
    controller, logger, _terminate_calls = _controller()
    controller._owner = owner
    controller._report_startup_failure = (
        lambda message, suggestion: calls.append((message, suggestion))
    )
    controller.is_startup = True
    controller.active = True
    controller.event_queue = queue.Queue()
    controller.event_queue.put(
        ("error", "cache build already active", "", "wait, then retry")
    )

    controller.drain_queue()

    assert calls == [
        ("cache build already active", "wait, then retry"),
        "close",
    ]
    assert logger.error_messages[-1] == (
        "Closing -- no map to show without a successful import."
    )
