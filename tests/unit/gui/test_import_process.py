"""Validate the child-process import orchestration boundary."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from caveviewer.core.chunking import builder as chunker
from caveviewer.core.map.cache_build_lock import CacheBuildInProgressError
from caveviewer.core.map import importer
from caveviewer.core.preferences.runtime_settings import ImportRuntimeSettings
from caveviewer.gui import import_process


class FakeEventQueue:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class FakeProcess:
    def __init__(self, target, args, name, pid=None):
        self.target = target
        self.args = args
        self.name = name
        self.pid = pid
        self.daemon = False
        self.started = False
        self.exitcode = None
        self.terminated = False
        self.killed = False
        self.join_calls = []

    def start(self):
        self.started = True

    def is_alive(self):
        return self.exitcode is None and not self.terminated

    def terminate(self):
        self.terminated = True
        self.exitcode = -15

    def kill(self):
        self.killed = True
        self.exitcode = -9

    def join(self, timeout=None):
        self.join_calls.append(timeout)


class FakeLogger:
    def __init__(self):
        self.info_messages = []
        self.error_messages = []

    @staticmethod
    def _format(message, args):
        return message % args if args else str(message)

    def info(self, message, *args):
        self.info_messages.append(self._format(message, args))

    def error(self, message, *args):
        self.error_messages.append(self._format(message, args))


class FakeSpawnContext:
    def __init__(self):
        self.queues = []
        self.processes = []

    def Queue(self):
        queue = FakeEventQueue()
        self.queues.append(queue)
        return queue

    def Process(self, *, target, args, name):
        process = FakeProcess(target, args, name)
        self.processes.append(process)
        return process


def _runtime_settings(*, map_cache_dir: str = "/cache-root") -> ImportRuntimeSettings:
    return ImportRuntimeSettings(
        map_cache_dir=map_cache_dir,
        chunk_size_meters=12.5,
        max_upload_group_mb=24.0,
        obj_scan_throttle_seconds=0.003,
        obj_import_batch_faces=42_000,
        obj_bucket_workers=3,
        chunk_build_workers=4,
        chunk_build_reserved_cpus=1,
        import_nice_increment=7,
    )


def test_source_path_from_descriptor_accepts_supported_formats():
    assert (
        import_process.source_path_from_descriptor({"obj_path": "/maps/cave.obj"})
        == "/maps/cave.obj"
    )
    assert (
        import_process.source_path_from_descriptor({"glb_path": "/maps/cave.glb"})
        == "/maps/cave.glb"
    )

    with pytest.raises(ValueError, match="missing obj_path or glb_path"):
        import_process.source_path_from_descriptor({})


def test_start_import_process_uses_spawn_context_and_event_queue():
    context = FakeSpawnContext()

    handle = import_process.start_import_process(
        {"glb_path": "/maps/cave.glb"}, "/maps", context=context
    )

    assert handle.events is context.queues[0]
    assert handle.commands is context.queues[1]
    assert handle.process is context.processes[0]
    assert handle.cache_dir
    assert handle.process.name == "CaveViewer-import"
    assert handle.process.daemon is True
    assert handle.process.started is True
    assert handle.process.args == (
        {"glb_path": "/maps/cave.glb"},
        "/maps",
        context.queues[0],
        context.queues[1],
        False,
    )


def test_start_import_process_passes_forced_rebuild_to_child():
    context = FakeSpawnContext()

    handle = import_process.start_import_process(
        {"glb_path": "/maps/cave.glb"},
        "/maps",
        force_rebuild=True,
        context=context,
    )

    assert handle.process.args[-1] is True


def test_start_import_process_passes_resume_requirement_to_child():
    context = FakeSpawnContext()

    handle = import_process.start_import_process(
        {"obj_path": "/maps/cave.obj"},
        "/maps",
        resume_required=True,
        context=context,
    )

    assert handle.process.args[-2:] == (False, True)


def test_start_import_process_allows_a_splash_owned_child_to_outlive_tk_close():
    context = FakeSpawnContext()

    handle = import_process.start_import_process(
        {"glb_path": "/maps/cave.glb"},
        "/maps",
        daemon=False,
        context=context,
    )

    assert handle.process.daemon is False


def test_start_import_process_serializes_explicit_runtime_settings_to_child():
    context = FakeSpawnContext()
    settings = _runtime_settings()

    handle = import_process.start_import_process(
        {"glb_path": "/maps/cave.glb"},
        "/maps",
        context=context,
        runtime_settings=settings,
    )

    assert handle.process.args == (
        {"glb_path": "/maps/cave.glb"},
        "/maps",
        context.queues[0],
        context.queues[1],
        False,
        False,
        settings,
    )


def test_import_event_log_handler_sends_log_event_to_parent_queue():
    events = FakeEventQueue()
    handler = import_process._ImportEventLogHandler(events)
    record = logging.LogRecord(
        "caveviewer",
        logging.WARNING,
        __file__,
        1,
        "slow import at %s%%",
        (50,),
        None,
    )
    record.component = "ImportProcess"

    handler.emit(record)

    assert events.events == [
        ("log", logging.WARNING, "ImportProcess", "slow import at 50%")
    ]


def test_import_process_reports_progress_and_done(monkeypatch):
    events = FakeEventQueue()

    def fake_import(model_descriptor, textures_dir, **options):
        assert model_descriptor == {"glb_path": "/maps/cave.glb"}
        assert textures_dir == "/maps"
        assert options["force_rebuild"] is False
        options["progress_cb"]("building cache", 0.5)
        return "/cache/cave"

    monkeypatch.setattr(importer, "import_and_cache_any", fake_import)
    monkeypatch.setattr(
        import_process,
        "_configure_import_child_logging",
        lambda _events: None,
    )
    monkeypatch.setattr(import_process, "configure_import_child_runtime", lambda: None)
    monkeypatch.setattr(
        import_process,
        "_start_heartbeat_thread",
        lambda *_args, **_kwargs: SimpleNamespace(join=lambda timeout=None: None),
    )

    import_process._run_import_process(
        {"glb_path": "/maps/cave.glb"}, "/maps", events
    )

    assert events.events == [
        ("progress", "starting import", 0.0),
        ("progress", "building cache", 0.5),
        ("done", "/cache/cave", "/cache/cave"),
    ]


def test_import_process_uses_explicit_runtime_settings_without_environment(
    monkeypatch, tmp_path,
):
    events = FakeEventQueue()
    map_root = tmp_path / "maps"
    source_path = map_root / "cave.obj"
    cache_root = tmp_path / "cache-root"
    expected_cache_dir = cache_root / "cave"
    settings = _runtime_settings(map_cache_dir=str(cache_root))
    child_runtime_calls = []

    def fake_import(model_descriptor, textures_dir, **options):
        assert model_descriptor == {"obj_path": str(source_path)}
        assert textures_dir == str(map_root)
        assert options["chunk_size"] == 12.5
        assert options["max_upload_group_mb"] == 24.0
        assert options["obj_scan_throttle_seconds"] == 0.003
        assert options["obj_import_batch_faces"] == 42_000
        assert options["obj_bucket_workers"] == 3
        assert options["chunk_build_workers"] == 4
        assert options["chunk_build_reserved_cpus"] == 1
        assert Path(options["cache_dir"]).is_relative_to(cache_root)
        return str(expected_cache_dir)

    monkeypatch.setattr(importer, "import_and_cache_any", fake_import)
    monkeypatch.setattr(
        import_process,
        "_configure_import_child_logging",
        lambda _events: None,
    )
    monkeypatch.setattr(
        import_process,
        "configure_import_child_runtime",
        lambda *, nice_increment: child_runtime_calls.append(nice_increment),
    )
    monkeypatch.setattr(
        import_process,
        "_start_heartbeat_thread",
        lambda *_args, **_kwargs: SimpleNamespace(join=lambda timeout=None: None),
    )

    import_process._run_import_process(
        {"obj_path": str(source_path)},
        str(map_root),
        events,
        runtime_settings=settings,
    )

    assert child_runtime_calls == [7]
    assert events.events[-1] == (
        "done",
        str(expected_cache_dir),
        str(expected_cache_dir),
    )


def test_import_process_forces_rebuild_when_requested(monkeypatch):
    events = FakeEventQueue()

    def fake_import(_model_descriptor, _textures_dir, **options):
        assert options["force_rebuild"] is True
        return "/cache/cave"

    monkeypatch.setattr(importer, "import_and_cache_any", fake_import)
    monkeypatch.setattr(
        import_process,
        "_configure_import_child_logging",
        lambda _events: None,
    )
    monkeypatch.setattr(import_process, "configure_import_child_runtime", lambda: None)
    monkeypatch.setattr(
        import_process,
        "_start_heartbeat_thread",
        lambda *_args, **_kwargs: SimpleNamespace(join=lambda timeout=None: None),
    )

    import_process._run_import_process(
        {"glb_path": "/maps/cave.glb"},
        "/maps",
        events,
        force_rebuild=True,
    )

    assert events.events[-1] == ("done", "/cache/cave", "/cache/cave")


def test_import_process_requires_resume_checkpoint_when_requested(monkeypatch):
    events = FakeEventQueue()

    def fake_import(_model_descriptor, _textures_dir, **options):
        assert options["resume_required"] is True
        return "/cache/cave"

    monkeypatch.setattr(importer, "import_and_cache_any", fake_import)
    monkeypatch.setattr(
        import_process,
        "_configure_import_child_logging",
        lambda _events: None,
    )
    monkeypatch.setattr(import_process, "configure_import_child_runtime", lambda: None)
    monkeypatch.setattr(
        import_process,
        "_start_heartbeat_thread",
        lambda *_args, **_kwargs: SimpleNamespace(join=lambda timeout=None: None),
    )

    import_process._run_import_process(
        {"obj_path": "/maps/cave.obj"},
        "/maps",
        events,
        resume_required=True,
    )

    assert events.events[-1] == ("done", "/cache/cave", "/cache/cave")


def test_import_process_reports_paused_checkpoint(monkeypatch):
    events = FakeEventQueue()
    logger = FakeLogger()

    def fake_import(_model_descriptor, _textures_dir, **options):
        assert options["pause_requested"]() is True
        raise chunker.ImportPaused("/cache/.map.resume-123")

    def start_command_thread(_commands, pause_event, _stop_event):
        pause_event.set()
        return SimpleNamespace(join=lambda timeout=None: None)

    monkeypatch.setattr(importer, "import_and_cache_any", fake_import)
    monkeypatch.setattr(import_process, "_LOG", logger)
    monkeypatch.setattr(
        import_process,
        "_configure_import_child_logging",
        lambda _events: None,
    )
    monkeypatch.setattr(import_process, "configure_import_child_runtime", lambda: None)
    monkeypatch.setattr(
        import_process,
        "_start_heartbeat_thread",
        lambda *_args, **_kwargs: SimpleNamespace(join=lambda timeout=None: None),
    )
    monkeypatch.setattr(import_process, "_start_command_thread", start_command_thread)

    import_process._run_import_process(
        {"obj_path": "/maps/cave.obj"}, "/maps", events, object()
    )

    assert events.events == [
        ("progress", "starting import", 0.0),
        ("paused", "/cache/.map.resume-123"),
    ]
    assert any("Import paused" in message for message in logger.info_messages)


def test_import_process_reports_error_with_traceback(monkeypatch):
    events = FakeEventQueue()

    def fail_import(*_args, **_kwargs):
        raise RuntimeError("parse failed")

    monkeypatch.setattr(importer, "import_and_cache_any", fail_import)
    monkeypatch.setattr(
        import_process,
        "_configure_import_child_logging",
        lambda _events: None,
    )
    monkeypatch.setattr(import_process, "configure_import_child_runtime", lambda: None)
    monkeypatch.setattr(
        import_process,
        "_start_heartbeat_thread",
        lambda *_args, **_kwargs: SimpleNamespace(join=lambda timeout=None: None),
    )

    import_process._run_import_process(
        {"glb_path": "/maps/broken.glb"}, "/maps", events
    )

    assert events.events[0] == ("progress", "starting import", 0.0)
    kind, message, trace = events.events[1]
    assert kind == "error"
    assert message == "parse failed"
    assert "RuntimeError: parse failed" in trace


def test_import_process_reports_actionable_chunker_error_without_traceback(
    monkeypatch,
):
    events = FakeEventQueue()
    logger = FakeLogger()

    def fail_import(*_args, **kwargs):
        assert "console_progress" not in kwargs
        raise chunker.InsufficientImportMemoryError(
            20 * 1024**3,
            9 * 1024**3,
            8 * 1024**3,
            source_path="/maps/DevilsEye Start.obj",
            physical_limit_bytes=18 * 1024**3,
        )

    monkeypatch.setattr(importer, "import_and_cache_any", fail_import)
    monkeypatch.setattr(import_process, "_LOG", logger)
    monkeypatch.setattr(
        import_process,
        "_configure_import_child_logging",
        lambda _events: None,
    )
    monkeypatch.setattr(import_process, "configure_import_child_runtime", lambda: None)
    monkeypatch.setattr(
        import_process,
        "_start_heartbeat_thread",
        lambda *_args, **_kwargs: SimpleNamespace(join=lambda timeout=None: None),
    )

    import_process._run_import_process(
        {"obj_path": "/maps/DevilsEye Start.obj"}, "/maps", events
    )

    assert events.events[0] == ("progress", "starting import", 0.0)
    kind, message, trace, suggestion = events.events[1]
    assert kind == "error"
    assert "Not enough available system RAM" in message
    assert trace == ""
    assert "machine with more RAM" in suggestion
    assert any(message in logged for logged in logger.error_messages)
    assert any("Suggestion:" in logged for logged in logger.error_messages)
    assert not any("Traceback" in logged for logged in logger.error_messages)


def test_import_process_reports_competing_cache_build_without_traceback(monkeypatch):
    events = FakeEventQueue()

    def fail_import(*_args, **_kwargs):
        raise CacheBuildInProgressError("/maps/_cache")

    monkeypatch.setattr(importer, "import_and_cache_any", fail_import)
    monkeypatch.setattr(
        import_process,
        "_configure_import_child_logging",
        lambda _events: None,
    )
    monkeypatch.setattr(import_process, "configure_import_child_runtime", lambda: None)
    monkeypatch.setattr(
        import_process,
        "_start_heartbeat_thread",
        lambda *_args, **_kwargs: SimpleNamespace(join=lambda timeout=None: None),
    )

    import_process._run_import_process(
        {"glb_path": "/maps/cave.glb"},
        "/maps",
        events,
    )

    kind, message, trace, suggestion = events.events[-1]
    assert kind == "error"
    assert "already in progress" in message
    assert trace == ""
    assert "Wait for the other cache build" in suggestion


def test_import_process_reports_lost_resume_checkpoint_without_traceback(monkeypatch):
    events = FakeEventQueue()

    monkeypatch.setattr(
        importer,
        "import_and_cache_any",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            chunker.ResumeCheckpointUnavailableError()
        ),
    )
    monkeypatch.setattr(
        import_process,
        "_configure_import_child_logging",
        lambda _events: None,
    )
    monkeypatch.setattr(import_process, "configure_import_child_runtime", lambda: None)
    monkeypatch.setattr(
        import_process,
        "_start_heartbeat_thread",
        lambda *_args, **_kwargs: SimpleNamespace(join=lambda timeout=None: None),
    )

    import_process._run_import_process(
        {"obj_path": "/maps/cave.obj"},
        "/maps",
        events,
        resume_required=True,
    )

    kind, message, trace, suggestion = events.events[-1]
    assert kind == "error"
    assert "checkpoint is no longer usable" in message.lower()
    assert trace == ""
    assert "Rebuild cache" in suggestion


def test_import_process_reports_cancelled_without_traceback_on_keyboard_interrupt(
    monkeypatch,
):
    events = FakeEventQueue()
    logger = FakeLogger()

    def interrupt_import(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(importer, "import_and_cache_any", interrupt_import)
    monkeypatch.setattr(import_process, "_LOG", logger)
    monkeypatch.setattr(
        import_process,
        "_configure_import_child_logging",
        lambda _events: None,
    )
    monkeypatch.setattr(import_process, "configure_import_child_runtime", lambda: None)
    monkeypatch.setattr(
        import_process,
        "_start_heartbeat_thread",
        lambda *_args, **_kwargs: SimpleNamespace(join=lambda timeout=None: None),
    )

    import_process._run_import_process(
        {"glb_path": "/maps/cancelled.glb"}, "/maps", events
    )

    assert events.events == [
        ("progress", "starting import", 0.0),
        ("cancelled",),
    ]
    assert logger.info_messages[-1] == "Import process interrupted by user."
    assert logger.error_messages == []


def test_terminate_import_process_terminates_alive_process():
    process = FakeProcess(target=object(), args=(), name="import")

    import_process.terminate_import_process(process, timeout=0.25)

    assert process.terminated is True
    assert process.join_calls == [0.25]


def test_terminate_import_process_cleans_abandoned_staging(tmp_path):
    cache_dir = tmp_path / "managed" / "map-key"
    cache_dir.parent.mkdir()
    staging_dir = cache_dir.parent / ".map-key.tmp-123-abandoned"
    backup_dir = cache_dir.parent / ".map-key.tmp-123-abandoned.previous"
    active_other_process = cache_dir.parent / ".map-key.tmp-999-active"
    unrelated_dir = cache_dir.parent / ".other.tmp-abandoned"
    staging_dir.mkdir()
    backup_dir.mkdir()
    active_other_process.mkdir()
    unrelated_dir.mkdir()
    (staging_dir / "partial.bin").write_bytes(b"partial")
    process = FakeProcess(target=object(), args=(), name="import", pid=123)

    import_process.terminate_import_process(
        process,
        timeout=0.25,
        cache_dir=str(cache_dir),
    )

    assert not staging_dir.exists()
    assert backup_dir.is_dir()
    assert active_other_process.is_dir()
    assert unrelated_dir.is_dir()


def test_terminate_import_process_ignores_finished_process():
    process = SimpleNamespace(
        exitcode=0,
        is_alive=lambda: False,
        terminate=lambda: pytest.fail("finished process must not terminate"),
    )

    import_process.terminate_import_process(process)


def test_limit_native_threads_sets_missing_values_and_preserves_existing():
    environ = {"OPENBLAS_NUM_THREADS": "4"}

    capped = import_process._limit_native_threads(environ)

    assert "OPENBLAS_NUM_THREADS" not in capped
    assert environ["OPENBLAS_NUM_THREADS"] == "4"
    for name in import_process.IMPORT_NATIVE_THREAD_ENV_VARS:
        assert environ[name]
