"""Validate the child-process import orchestration boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caveviewer import app
from caveviewer.core import chunker
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


class FakeSpawnContext:
    def __init__(self):
        self.queue = FakeEventQueue()
        self.processes = []

    def Queue(self):
        return self.queue

    def Process(self, *, target, args, name):
        process = FakeProcess(target, args, name)
        self.processes.append(process)
        return process


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

    assert handle.events is context.queue
    assert handle.process is context.processes[0]
    assert handle.cache_dir
    assert handle.process.name == "CaveViewer-import"
    assert handle.process.daemon is True
    assert handle.process.started is True
    assert handle.process.args == (
        {"glb_path": "/maps/cave.glb"},
        "/maps",
        context.queue,
    )


def test_import_process_reports_progress_and_done(monkeypatch):
    events = FakeEventQueue()

    def fake_import(model_descriptor, textures_dir, **options):
        assert model_descriptor == {"glb_path": "/maps/cave.glb"}
        assert textures_dir == "/maps"
        assert options["force_rebuild"] is False
        options["extra_progress_cb"]("building cache", 0.5)
        return "/cache/cave"

    monkeypatch.setattr(app, "import_and_cache_any", fake_import)
    monkeypatch.setattr(chunker, "load_manifest", lambda _path: {"chunks": {}})
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
        ("done", "/cache/cave", "/cache/cave", {"chunks": {}}),
    ]


def test_import_process_reports_error_with_traceback(monkeypatch):
    events = FakeEventQueue()

    def fail_import(*_args, **_kwargs):
        raise RuntimeError("parse failed")

    monkeypatch.setattr(app, "import_and_cache_any", fail_import)
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
