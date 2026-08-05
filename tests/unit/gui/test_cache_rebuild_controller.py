"""Cover the splash-owned forced-cache-rebuild process lifecycle."""

from __future__ import annotations

import queue
from pathlib import Path

from caveviewer.gui.cache_rebuild_controller import (
    CacheRebuildFailed,
    CacheRebuildJobController,
    CacheRebuildJobState,
    CacheRebuildPaused,
    CacheRebuildProgress,
    CacheRebuildStarted,
    CacheRebuildSucceeded,
)
from caveviewer.gui.map_cache_rebuild import CacheRebuildTarget


class _FakeProcess:
    def __init__(self):
        self.exitcode = None
        self.join_calls = []

    def join(self, timeout=None):
        self.join_calls.append(timeout)


class _FakeHandle:
    def __init__(self):
        self.events = queue.Queue()
        self.commands = queue.Queue()
        self.process = _FakeProcess()


def _target() -> CacheRebuildTarget:
    return CacheRebuildTarget(
        map_path=Path("/maps/cave"),
        model_descriptor={
            "format": "obj",
            "obj_path": "/maps/cave/cave.obj",
            "mtl_path": "/maps/cave/cave.mtl",
        },
        textures_dir=Path("/maps/cave"),
        cache_dir=Path("/maps/cave/_cache"),
    )


def test_controller_starts_forced_import_and_reports_progress_then_success():
    handle = _FakeHandle()
    calls = []
    controller = CacheRebuildJobController(
        start_process=lambda descriptor, textures_dir, **options: (
            calls.append((descriptor, textures_dir, options)) or handle
        )
    )

    started = controller.start(_target())

    assert isinstance(started, CacheRebuildStarted)
    assert controller.active
    assert calls == [
        (
            {
                "format": "obj",
                "obj_path": "/maps/cave/cave.obj",
                "mtl_path": "/maps/cave/cave.mtl",
            },
            "/maps/cave",
            {"force_rebuild": True, "daemon": False},
        )
    ]

    handle.events.put(("progress", "building chunks", 0.625))
    updates = controller.poll()

    assert updates == (
        CacheRebuildProgress(
            target=_target(),
            stage="building chunks",
            fraction=0.625,
        ),
    )

    handle.events.put(("done", "/maps/cave/_cache", "/maps/cave/_cache"))
    updates = controller.poll()

    assert updates == (
        CacheRebuildSucceeded(target=_target(), cache_dir="/maps/cave/_cache"),
    )
    assert controller.state is CacheRebuildJobState.SUCCEEDED
    assert not controller.active
    assert handle.process.join_calls == [0.0]


def test_controller_requests_obj_pause_and_reports_checkpoint():
    handle = _FakeHandle()
    controller = CacheRebuildJobController(
        start_process=lambda *_args, **_kwargs: handle
    )
    controller.start(_target())

    assert controller.request_pause()
    assert handle.commands.get_nowait() == ("pause",)
    assert controller.state is CacheRebuildJobState.PAUSING

    handle.events.put(("paused", "/maps/cave/.cache.resume-123"))
    updates = controller.poll()

    assert updates == (
        CacheRebuildPaused(
            target=_target(),
            resume_dir="/maps/cave/.cache.resume-123",
        ),
    )
    assert controller.state is CacheRebuildJobState.PAUSED
    assert not controller.active


def test_controller_reports_abnormal_child_exit_as_failure():
    handle = _FakeHandle()
    controller = CacheRebuildJobController(
        start_process=lambda *_args, **_kwargs: handle
    )
    controller.start(_target())
    handle.process.exitcode = 3

    assert controller.poll() == ()
    assert controller.poll() == ()
    updates = controller.poll()

    assert len(updates) == 1
    assert isinstance(updates[0], CacheRebuildFailed)
    assert "exit code 3" in updates[0].error
    assert controller.state is CacheRebuildJobState.FAILED
