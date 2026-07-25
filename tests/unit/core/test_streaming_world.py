"""Exercise streaming queues, worker lifecycle, prioritization, and shutdown."""

from __future__ import annotations

import logging
import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from caveviewer.core.hardware import system_memory
from caveviewer.core.streaming import scheduler as streaming_scheduler
from caveviewer.core.streaming import world as streaming_world
from caveviewer.core.workers.allocation import WorkerAllocation


def _chunk(cell):
    return SimpleNamespace(cell=cell)


class _LockCheckingSet(set):
    def __init__(self, lock: threading.Lock, values=()):
        super().__init__(values)
        self._lock = lock

    def __len__(self):
        assert self._lock.locked()
        return super().__len__()


def _world_with_ready_chunks(
    *cells,
    capacity=16,
    unload_chunks_per_frame=1,
    unload_time_budget_ms=1.0,
    unload_retire_frames=0,
):
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world._paused_event = threading.Event()
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity)
    world._lock = threading.Lock()
    world._pending = set(cells)
    world.loaded_cells = set()
    world.available_cells = set(cells)
    world._last_wanted_cells = set(cells)
    world._last_cam_cell_for_priority = (0, 0, 0)
    world._last_cell_priority_key = None
    world._cells_to_unload_next_drain = set()
    world.config = SimpleNamespace(
        max_loaded_chunks=capacity,
        unload_chunks_per_frame=unload_chunks_per_frame,
        unload_time_budget_ms=unload_time_budget_ms,
        unload_retire_frames=unload_retire_frames,
    )
    for cell in cells:
        world._ready_backlog.put(_chunk(cell), timeout=0.0)
    return world


def _drain(world, ready, *, max_per_frame=4, time_budget_ms=100.0):
    world.drain_ready_chunks(
        lambda data: ready.append(data.cell),
        lambda _cell: None,
        max_per_frame=max_per_frame,
        time_budget_ms=time_budget_ms,
    )


def _configure_worker_preprocess(
    world,
    *,
    on_decode_textures=None,
    prepack_smooth_shading: bool | None = None,
) -> None:
    world._on_decode_textures = on_decode_textures
    world._prepack_smooth_shading_lock = threading.Lock()
    world._prepack_smooth_shading = (
        streaming_world.StreamingWorld._normalize_prepack_smooth_shading(
            prepack_smooth_shading
        )
    )


def _streaming_world_with_cells(
    monkeypatch,
    cells,
    *,
    ram_available,
    workers=8,
    requested_workers: int | None = None,
    reserved_cpus=3,
    logical_cpus: int | None = None,
):
    manifest = {
        "chunks": {
            f"{cell[0]}_{cell[1]}_{cell[2]}": {}
            for cell in cells
        }
    }
    monkeypatch.setattr(
        streaming_world.chunker, "load_manifest", lambda _cache_dir: manifest
    )
    monkeypatch.setattr(
        streaming_world.StreamingWorld,
        "_estimate_chunk_ram_bytes",
        lambda _self, _keys: 1,
    )
    monkeypatch.setattr(
        streaming_world.StreamingWorld,
        "_estimate_chunk_gpu_bytes",
        lambda _self, _keys: 1,
    )
    monkeypatch.setattr(streaming_world, "_detect_total_ram_bytes", lambda: 1_000)
    monkeypatch.setattr(
        streaming_world, "_detect_total_gpu_memory_bytes", lambda _vendor=None: None
    )
    monkeypatch.setattr(
        streaming_world,
        "_detect_ram_snapshot",
        lambda: system_memory.RamSnapshot(100, ram_available),
    )
    monkeypatch.setattr(
        streaming_world,
        "resolve_worker_allocation",
        lambda *_args, **_kwargs: WorkerAllocation(
            workers if requested_workers is None else requested_workers,
            reserved_cpus,
            workers + reserved_cpus if logical_cpus is None else logical_cpus,
            workers,
        ),
    )
    return streaming_world.StreamingWorld(
        "unused",
        streaming_world.StreamingConfig(
            chunk_size=1.0,
            load_radius_cells=max(abs(cell[0]) for cell in cells),
        ),
    )


def test_ready_backlog_remains_bounded_until_an_item_is_consumed():
    backlog = streaming_scheduler.BoundedReadyBacklog(capacity=2)
    backlog.put(_chunk((0, 0, 0)), timeout=0.0)
    backlog.put(_chunk((1, 0, 0)), timeout=0.0)

    with pytest.raises(queue.Full):
        backlog.put(_chunk((2, 0, 0)), timeout=0.0)

    assert backlog.get_closest_nowait().cell == (0, 0, 0)
    backlog.put(_chunk((2, 0, 0)), timeout=0.0)
    assert backlog.qsize() == 2


def test_streaming_world_rejects_prepack_callback_policy():
    with pytest.raises(TypeError, match="not a callback"):
        streaming_world.StreamingWorld(
            "unused",
            streaming_world.StreamingConfig(chunk_size=1.0),
            prepack_smooth_shading=lambda: True,
        )


def test_streaming_worker_lowers_priority_at_thread_entry(monkeypatch):
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world._stop_event = threading.Event()
    world._paused_event = threading.Event()
    world._work_queue = queue.Queue()
    world._work_queue.put(None)
    calls = []

    monkeypatch.setattr(
        streaming_world,
        "lower_current_thread_priority",
        lambda: calls.append(True),
    )

    world._worker_loop()

    assert calls == [True]


def test_streaming_world_can_skip_render_thread_manifest_and_texture_scans(monkeypatch):
    manifest = {
        "chunks": {"0_0_0": {}},
        "mtl_materials": {"wall": "wall.jpg"},
    }

    def fail_load_manifest(_cache_dir):
        raise AssertionError("manifest should be supplied by the caller")

    def fail_texture_estimate(_file_or_bytes, _textures_dir):
        raise AssertionError("texture headers should not be scanned")

    monkeypatch.setattr(streaming_world.chunker, "load_manifest", fail_load_manifest)
    monkeypatch.setattr(
        streaming_world.StreamingWorld,
        "_estimate_texture_gpu_bytes",
        staticmethod(fail_texture_estimate),
    )
    monkeypatch.setattr(
        streaming_world.StreamingWorld,
        "_estimate_chunk_ram_bytes",
        lambda _self, _keys: 1,
    )
    monkeypatch.setattr(
        streaming_world.StreamingWorld,
        "_estimate_chunk_gpu_bytes",
        lambda _self, _keys: 1,
    )
    monkeypatch.setattr(
        streaming_world,
        "_detect_ram_snapshot",
        lambda: system_memory.RamSnapshot(100, 100),
    )
    monkeypatch.setattr(
        streaming_world,
        "resolve_worker_allocation",
        lambda *_args, **_kwargs: WorkerAllocation(1, 3, 4, 1),
    )
    monkeypatch.setattr(
        streaming_world.StreamingWorld,
        "_start_worker_locked",
        lambda _self: None,
    )

    world = streaming_world.StreamingWorld(
        "unused",
        streaming_world.StreamingConfig(chunk_size=1.0),
        manifest=manifest,
        total_gpu_memory_bytes=1_000,
        texture_gpu_budget_bytes=100,
        gpu_geometry_budget_bytes=200,
        estimate_texture_gpu_bytes=False,
    )

    assert world.available_cells == {(0, 0, 0)}
    assert world._texture_gpu_bytes == {}


def test_prepack_policy_setter_publishes_bool_snapshot():
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    _configure_worker_preprocess(world)

    world.set_prepack_smooth_shading(True)
    assert world._prepack_smooth_shading_snapshot() is True

    world.set_prepack_smooth_shading(False)
    assert world._prepack_smooth_shading_snapshot() is False

    world.set_prepack_smooth_shading(None)
    assert world._prepack_smooth_shading_snapshot() is None


def test_ready_backlog_selector_callbacks_run_outside_internal_lock():
    backlog = streaming_scheduler.BoundedReadyBacklog(capacity=2)
    backlog.put(_chunk((2, 0, 0)), timeout=0.0)
    backlog.put(_chunk((1, 0, 0)), timeout=0.0)
    lock_owned_during_callback = []

    def distance_key(data):
        lock_owned_during_callback.append(backlog._condition._is_owned())
        return data.cell[0]

    assert backlog.get_closest_nowait(distance_key).cell == (1, 0, 0)
    assert lock_owned_during_callback == [False, False]


def test_ready_backlog_discard_predicate_runs_outside_internal_lock():
    backlog = streaming_scheduler.BoundedReadyBacklog(capacity=2)
    backlog.put(_chunk((1, 0, 0)), timeout=0.0)
    backlog.put(_chunk((2, 0, 0)), timeout=0.0)
    lock_owned_during_callback = []

    def predicate(data):
        lock_owned_during_callback.append(backlog._condition._is_owned())
        return data.cell[0] == 1

    discarded = backlog.discard_if(predicate)

    assert [data.cell for data in discarded] == [(1, 0, 0)]
    assert lock_owned_during_callback == [False, False]
    assert backlog.get_closest_nowait().cell == (2, 0, 0)


def test_drain_uploads_closest_chunk_first_and_retains_the_rest():
    world = _world_with_ready_chunks((5, 0, 0), (1, 0, 0), (3, 0, 0))
    ready = []

    _drain(world, ready, max_per_frame=1)

    assert ready == [(1, 0, 0)]
    assert world._ready_backlog.qsize() == 2
    assert world.loaded_cells == {(1, 0, 0)}
    assert (1, 0, 0) not in world._pending

    _drain(world, ready, max_per_frame=2)

    assert ready == [(1, 0, 0), (3, 0, 0), (5, 0, 0)]
    assert world._ready_backlog.qsize() == 0


def test_drain_uses_owner_supplied_cell_priority():
    world = _world_with_ready_chunks((1, 0, 0), (2, 0, 0), (3, 0, 0))
    world._last_cell_priority_key = lambda cell: {
        (3, 0, 0): 0,
        (1, 0, 0): 1,
        (2, 0, 0): 2,
    }[cell]
    ready = []

    _drain(world, ready, max_per_frame=1)

    assert ready == [(3, 0, 0)]
    assert world._ready_backlog.qsize() == 2


def test_drain_respects_time_budget_and_preserves_deferred_chunks(monkeypatch):
    world = _world_with_ready_chunks((1, 0, 0), (2, 0, 0))
    ready = []
    timestamps = iter((10.0, 10.0, 10.010))
    monkeypatch.setattr(streaming_world.time, "perf_counter", lambda: next(timestamps))

    _drain(world, ready, max_per_frame=4, time_budget_ms=5.0)

    assert ready == [(1, 0, 0)]
    assert world._ready_backlog.qsize() == 1


def test_worker_refill_during_upload_cannot_block_deferred_ready_chunks():
    world = _world_with_ready_chunks((1, 0, 0), (2, 0, 0), capacity=2)
    ready = []

    def upload(data):
        ready.append(data.cell)
        # Simulate a worker filling the slot freed when this upload started.
        world._ready_backlog.put(_chunk((3, 0, 0)), timeout=0.0)

    world.drain_ready_chunks(
        upload,
        lambda _cell: None,
        max_per_frame=1,
        time_budget_ms=100.0,
    )

    assert ready == [(1, 0, 0)]
    assert world._ready_backlog.qsize() == 2


def test_deferred_chunks_use_the_latest_camera_priority():
    world = _world_with_ready_chunks((1, 0, 0), (5, 0, 0), (9, 0, 0))
    ready = []

    _drain(world, ready, max_per_frame=1)
    world._last_cam_cell_for_priority = (10, 0, 0)
    _drain(world, ready, max_per_frame=1)

    assert ready == [(1, 0, 0), (9, 0, 0)]
    assert world._ready_backlog.qsize() == 1


def test_stationary_update_refreshes_ready_priority():
    world = _world_with_ready_chunks((1, 0, 0), (3, 0, 0))
    world.config.chunk_size = 1.0
    world.config.load_radius_cells = 3
    world._last_camera_cell = (0, 0, 0)
    world._last_load_radius = 3
    ready = []

    world.update(
        np.zeros(3, dtype=np.float32),
        cell_priority_key=lambda cell: 0 if cell == (3, 0, 0) else 1,
    )
    _drain(world, ready, max_per_frame=1)

    assert ready == [(3, 0, 0)]


def test_stationary_update_reprioritizes_queued_work():
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world._paused_event = threading.Event()
    world.available_cells = {(1, 0, 0), (2, 0, 0), (3, 0, 0)}
    world.config = streaming_world.StreamingConfig(
        chunk_size=1.0,
        load_radius_cells=3,
        max_loaded_chunks=16,
    )
    world._last_camera_cell = (0, 0, 0)
    world._last_load_radius = 3
    world.loaded_cells = set()
    world._pending = set(world.available_cells)
    world._failed_cells = {}
    world._lock = threading.Lock()
    world._work_queue = queue.Queue(maxsize=16)
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=16)
    world._last_wanted_cells = set(world.available_cells)
    world._last_cam_cell_for_priority = (0, 0, 0)
    world._last_cell_priority_key = None
    world._cells_to_unload_next_drain = set()
    for cell in [(3, 0, 0), (2, 0, 0), (1, 0, 0)]:
        world._work_queue.put_nowait(cell)

    world.update(
        np.zeros(3, dtype=np.float32),
        cell_priority_key=lambda cell: {
            (1, 0, 0): 0,
            (2, 0, 0): 1,
            (3, 0, 0): 2,
        }[cell],
    )

    assert [world._work_queue.get_nowait() for _ in range(3)] == [
        (1, 0, 0),
        (2, 0, 0),
        (3, 0, 0),
    ]
    assert world._pending == world.available_cells


def test_streaming_starts_one_worker_then_grows_after_completed_work(
    monkeypatch, caplog
):
    cells = {(index, 0, 0) for index in range(6)}
    loaded_count = 0
    loaded_lock = threading.Lock()
    all_loaded = threading.Event()

    def load_chunk(_cache_dir, cell):
        nonlocal loaded_count
        with loaded_lock:
            loaded_count += 1
            if loaded_count == len(cells):
                all_loaded.set()
        return _chunk(cell)

    monkeypatch.setattr(streaming_world.chunker, "load_chunk_file", load_chunk)
    monkeypatch.setattr(
        streaming_world.chunker,
        "prepare_chunk_upload_groups",
        lambda data: data,
    )
    with caplog.at_level(logging.INFO, logger="caveviewer"):
        world = _streaming_world_with_cells(
            monkeypatch,
            cells,
            ram_available=100,
            workers=5,
            requested_workers=8,
            reserved_cpus=3,
            logical_cpus=8,
        )
    try:
        assert len(world._workers) == 1
        assert all(not worker.daemon for worker in world._workers)
        with caplog.at_level(logging.INFO, logger="caveviewer"):
            world.update(np.zeros(3, dtype=np.float32))
            deadline = time.perf_counter() + 2.0
            ready = []
            while not all_loaded.is_set() and time.perf_counter() < deadline:
                world.drain_ready_chunks(
                    lambda data: ready.append(data.cell),
                    lambda _cell: None,
                    max_per_frame=6,
                    time_budget_ms=100.0,
                )
                time.sleep(0.01)
            assert all_loaded.is_set()
        assert len(world._workers) > 1
        assert len(world._workers) <= 5
        assert all(not worker.daemon for worker in world._workers)
        assert "Streaming worker target resolved to 5 worker(s)" in caplog.text
        assert "requested 8 capped by reserved CPU policy" in caplog.text
        assert "additional workers require system RAM" not in caplog.text
        assert "Detected system RAM for streaming worker admission" in caplog.text
    finally:
        world.shutdown()


def test_streaming_stays_at_one_worker_at_eighty_percent_ram(
    monkeypatch,
):
    cells = {(index, 0, 0) for index in range(4)}
    first_loaded = threading.Event()
    loaded_count = 0
    loaded_lock = threading.Lock()

    def load_chunk(_cache_dir, cell):
        nonlocal loaded_count
        with loaded_lock:
            loaded_count += 1
            first_loaded.set()
        return _chunk(cell)

    monkeypatch.setattr(streaming_world.chunker, "load_chunk_file", load_chunk)
    monkeypatch.setattr(
        streaming_world.chunker,
        "prepare_chunk_upload_groups",
        lambda data: data,
    )
    world = _streaming_world_with_cells(
        monkeypatch, cells, ram_available=20, workers=8
    )
    try:
        world.update(np.zeros(3, dtype=np.float32))
        assert first_loaded.wait(timeout=2.0)
        assert world.config.max_loaded_chunks == 1
        assert len(world._workers) == 1
    finally:
        world.shutdown()


def test_streaming_residency_budget_uses_available_ram_snapshot(monkeypatch):
    monkeypatch.delenv("CAVEVIEWER_MEMORY_UTILIZATION_TARGET", raising=False)
    cells = {(index, 0, 0) for index in range(20)}

    world = _streaming_world_with_cells(
        monkeypatch,
        cells,
        ram_available=50,
        workers=1,
    )
    try:
        assert world.config.max_loaded_chunks == 3
        assert world._ready_backlog.capacity == 1
        assert world.config.max_loaded_chunks + world._ready_backlog.capacity == 4
    finally:
        world.shutdown()


def test_worker_load_failure_does_not_stop_later_ready_work(monkeypatch):
    failed_cell = (1, 0, 0)
    ready_cell = (2, 0, 0)
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world.cache_dir = "unused"
    _configure_worker_preprocess(world)
    world._stop_event = threading.Event()
    world._paused_event = threading.Event()
    world._work_queue = queue.Queue()
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=2)
    world._lock = threading.Lock()
    world._pending = {failed_cell, ready_cell}
    world._last_wanted_cells = {failed_cell, ready_cell}
    world._work_queue.put(failed_cell)
    world._work_queue.put(ready_cell)
    world._work_queue.put(None)

    def load_chunk(_cache_dir, cell):
        if cell == failed_cell:
            raise FileNotFoundError
        return _chunk(cell)

    monkeypatch.setattr(streaming_world.chunker, "load_chunk_file", load_chunk)
    monkeypatch.setattr(
        streaming_world.chunker,
        "prepare_chunk_upload_groups",
        lambda data: data,
    )

    world._worker_loop()

    assert world._ready_backlog.get_closest_nowait().cell == ready_cell
    assert failed_cell not in world._pending
    assert ready_cell in world._pending
    failures = world.drain_worker_failures()
    assert len(failures) == 1
    assert failures[0].cell == failed_cell
    assert failures[0].stage == "load_chunk_file"
    assert failures[0].error_type == "FileNotFoundError"
    assert failures[0].fatal is True
    assert world.failed_cells()[failed_cell] == failures[0]


def test_nonfatal_decode_callback_failure_is_reported_without_failed_cell(
    monkeypatch,
):
    cell = (2, 0, 0)
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world.cache_dir = "unused"
    _configure_worker_preprocess(
        world,
        on_decode_textures=lambda _data: (_ for _ in ()).throw(
            RuntimeError("decode failed")
        ),
    )
    world._stop_event = threading.Event()
    world._paused_event = threading.Event()
    world._work_queue = queue.Queue()
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=1)
    world._lock = threading.Lock()
    world._pending = {cell}
    world._last_wanted_cells = {cell}
    world._work_queue.put(cell)
    world._work_queue.put(None)

    monkeypatch.setattr(
        streaming_world.chunker,
        "load_chunk_file",
        lambda _cache_dir, requested_cell: _chunk(requested_cell),
    )
    monkeypatch.setattr(
        streaming_world.chunker,
        "prepare_chunk_upload_groups",
        lambda data: data,
    )

    world._worker_loop()

    assert world._ready_backlog.get_closest_nowait().cell == cell
    assert world.failed_cells() == {}
    failures = world.drain_worker_failures()
    assert len(failures) == 1
    assert failures[0].cell == cell
    assert failures[0].stage == "on_decode_textures"
    assert failures[0].error_type == "RuntimeError"
    assert failures[0].fatal is False


def test_worker_prepacks_vertex_bytes_before_ready_handoff(monkeypatch):
    cell = (2, 0, 0)
    prepacked = []
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world.cache_dir = "unused"
    _configure_worker_preprocess(world, prepack_smooth_shading=True)
    world._stop_event = threading.Event()
    world._paused_event = threading.Event()
    world._work_queue = queue.Queue()
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=1)
    world._lock = threading.Lock()
    world._pending = {cell}
    world._last_wanted_cells = {cell}
    world._work_queue.put(cell)
    world._work_queue.put(None)

    monkeypatch.setattr(
        streaming_world.chunker,
        "load_chunk_file",
        lambda _cache_dir, requested_cell: _chunk(requested_cell),
    )
    monkeypatch.setattr(
        streaming_world.chunker,
        "prepare_chunk_upload_groups",
        lambda data: data,
    )

    def prepack(data, *, smooth_shading):
        prepacked.append((data.cell, smooth_shading))
        return data

    monkeypatch.setattr(
        streaming_world.chunker,
        "prepack_chunk_vertex_bytes",
        prepack,
    )

    world._worker_loop()

    assert prepacked == [(cell, True)]
    assert world._ready_backlog.get_closest_nowait().cell == cell


def test_ready_callback_failure_does_not_discard_unattempted_chunks():
    failed_cell = (1, 0, 0)
    deferred_cell = (2, 0, 0)
    world = _world_with_ready_chunks(failed_cell, deferred_cell)

    def fail_upload(_data):
        raise RuntimeError("GPU upload failed")

    with pytest.raises(RuntimeError, match="GPU upload failed"):
        world.drain_ready_chunks(
            fail_upload,
            lambda _cell: None,
            max_per_frame=2,
            time_budget_ms=100.0,
        )

    assert world._ready_backlog.get_closest_nowait().cell == deferred_cell
    assert failed_cell not in world.loaded_cells
    assert failed_cell not in world._pending


def test_ready_callback_observes_cell_as_uncommitted_until_it_succeeds():
    cell = (1, 0, 0)
    world = _world_with_ready_chunks(cell)
    loaded_during_callback = []

    def upload(data):
        loaded_during_callback.append(data.cell in world.loaded_cells)

    world.drain_ready_chunks(
        upload,
        lambda _cell: None,
        max_per_frame=1,
        time_budget_ms=100.0,
    )

    assert loaded_during_callback == [False]
    assert world.loaded_cells == {cell}
    assert cell not in world._pending


def test_partial_ready_callback_keeps_cell_pending_until_completed():
    cell = (1, 0, 0)
    world = _world_with_ready_chunks(cell)
    calls = 0

    def upload(data):
        nonlocal calls
        calls += 1
        assert data.cell == cell
        return calls >= 2

    world.drain_ready_chunks(
        upload,
        lambda _cell: None,
        max_per_frame=1,
        time_budget_ms=100.0,
    )

    assert calls == 1
    assert world.loaded_cells == set()
    assert cell in world._pending
    assert world.stats()["ready"] == 1

    world.drain_ready_chunks(
        upload,
        lambda _cell: None,
        max_per_frame=1,
        time_budget_ms=100.0,
    )

    assert calls == 2
    assert world.loaded_cells == {cell}
    assert cell not in world._pending
    assert world.stats()["ready"] == 0


def test_scheduled_unloads_run_before_new_uploads():
    old_cell = (9, 0, 0)
    new_cell = (1, 0, 0)
    world = _world_with_ready_chunks(new_cell)
    world.loaded_cells = {old_cell}
    world._cells_to_unload_next_drain = {old_cell}
    events = []

    def upload(data):
        events.append(("upload", data.cell, old_cell in world.loaded_cells))

    world.drain_ready_chunks(
        upload,
        lambda cell: events.append(("unload", cell)),
        max_per_frame=1,
        time_budget_ms=100.0,
    )

    assert events == [("unload", old_cell), ("upload", new_cell, False)]


def test_unload_callback_observes_cell_loaded_until_release_succeeds():
    old_cell = (9, 0, 0)
    world = _world_with_ready_chunks(unload_retire_frames=0)
    world.loaded_cells = {old_cell}
    world._last_wanted_cells = set()
    world._cells_to_unload_next_drain = {old_cell}
    loaded_during_callback = []

    world.drain_ready_chunks(
        lambda _data: None,
        lambda cell: loaded_during_callback.append(cell in world.loaded_cells),
        max_per_frame=1,
        time_budget_ms=100.0,
    )

    assert loaded_during_callback == [True]
    assert old_cell not in world.loaded_cells


def test_unload_callback_failure_keeps_loaded_state_and_backlog():
    old_cell = (9, 0, 0)
    world = _world_with_ready_chunks(unload_retire_frames=0)
    world.loaded_cells = {old_cell}
    world._last_wanted_cells = set()
    world._cells_to_unload_next_drain = {old_cell}

    def fail_unload(_cell):
        raise RuntimeError("release failed")

    with pytest.raises(RuntimeError, match="release failed"):
        world.drain_ready_chunks(
            lambda _data: None,
            fail_unload,
            max_per_frame=1,
            time_budget_ms=100.0,
        )

    assert old_cell in world.loaded_cells
    assert old_cell in {cell for cell, _frames in world._unload_backlog}


def test_scheduled_unloads_can_retire_before_release():
    old_cell = (9, 0, 0)
    new_cell = (1, 0, 0)
    world = _world_with_ready_chunks(new_cell, unload_retire_frames=2)
    world.loaded_cells = {old_cell}
    world._cells_to_unload_next_drain = {old_cell}
    events = []

    world.drain_ready_chunks(
        lambda data: events.append(("upload", data.cell)),
        lambda cell: events.append(("unload", cell)),
        max_per_frame=1,
        time_budget_ms=100.0,
    )

    assert events == [("upload", new_cell)]
    assert old_cell in world.loaded_cells
    assert world.stats()["unload_pending"] == 1

    world.drain_ready_chunks(
        lambda data: events.append(("upload", data.cell)),
        lambda cell: events.append(("unload", cell)),
        max_per_frame=1,
        time_budget_ms=100.0,
    )

    assert events == [("upload", new_cell), ("unload", old_cell)]
    assert old_cell not in world.loaded_cells
    assert world.stats()["unload_pending"] == 0


def test_unload_queue_limits_releases_per_frame():
    cells = {(3, 0, 0), (4, 0, 0), (5, 0, 0)}
    world = _world_with_ready_chunks(
        capacity=16,
        unload_chunks_per_frame=1,
        unload_retire_frames=0,
    )
    world.loaded_cells = set(cells)
    world._last_wanted_cells = set()
    world._cells_to_unload_next_drain = set(cells)
    unloaded = []

    world.drain_ready_chunks(
        lambda _data: None,
        lambda cell: unloaded.append(cell),
        max_per_frame=4,
        time_budget_ms=100.0,
    )

    assert len(unloaded) == 1
    assert len(world.loaded_cells) == 2
    assert world.stats()["unload_pending"] == 2

    world.drain_ready_chunks(
        lambda _data: None,
        lambda cell: unloaded.append(cell),
        max_per_frame=4,
        time_budget_ms=100.0,
    )

    assert len(unloaded) == 2
    assert len(world.loaded_cells) == 1
    assert world.stats()["unload_pending"] == 1


def test_queued_unload_is_cancelled_when_loaded_cell_is_wanted_again():
    cell = (3, 0, 0)
    world = _world_with_ready_chunks(unload_retire_frames=2)
    world.loaded_cells = {cell}
    world._last_wanted_cells = set()
    world._cells_to_unload_next_drain = {cell}
    unloaded = []

    world.drain_ready_chunks(
        lambda _data: None,
        lambda unload_cell: unloaded.append(unload_cell),
        max_per_frame=4,
        time_budget_ms=100.0,
    )
    assert world.stats()["unload_pending"] == 1

    world._cancel_queued_unloads({cell})
    world._last_wanted_cells = {cell}

    world.drain_ready_chunks(
        lambda _data: None,
        lambda unload_cell: unloaded.append(unload_cell),
        max_per_frame=4,
        time_budget_ms=100.0,
    )

    assert unloaded == []
    assert world.loaded_cells == {cell}
    assert world.stats()["unload_pending"] == 0


def test_stale_partial_ready_chunk_is_unloaded_without_becoming_loaded():
    cell = (1, 0, 0)
    world = _world_with_ready_chunks(cell)
    world._ready_backlog.clear()
    world._partial_ready = [_chunk(cell)]
    world.available_cells = set()
    world.config = streaming_world.StreamingConfig(
        chunk_size=1.0,
        load_radius_cells=0,
        max_loaded_chunks=16,
        unload_retire_frames=0,
    )
    world._last_camera_cell = None
    world._last_load_radius = None
    world._work_queue = queue.Queue()

    world.update(np.zeros(3, dtype=np.float32))

    events = []
    world.drain_ready_chunks(
        lambda _data: events.append("upload"),
        lambda unload_cell: events.append(("unload", unload_cell)),
        max_per_frame=1,
        time_budget_ms=100.0,
    )

    assert events == [("unload", cell)]
    assert world.loaded_cells == set()
    assert cell not in world._pending


def test_texture_budget_does_not_limit_geometry_wanted_set():
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world._paused_event = threading.Event()
    world.available_cells = {(1, 0, 0), (2, 0, 0), (3, 0, 0)}
    world.config = streaming_world.StreamingConfig(
        chunk_size=1.0,
        load_radius_cells=3,
        max_loaded_chunks=16,
    )
    world._last_camera_cell = None
    world._last_load_radius = None
    world.loaded_cells = set()
    world._pending = set()
    world._lock = threading.Lock()
    world._work_queue = queue.Queue()
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=16)
    world._last_wanted_cells = set()
    world._last_cam_cell_for_priority = None
    world._cells_to_unload_next_drain = set()

    world.update(np.array([0.0, 0.0, 0.0], dtype=np.float32))

    assert world._last_wanted_cells == world.available_cells
    assert world._work_queue.qsize() == 3


def test_update_includes_prefetch_wanted_cells_outside_camera_radius():
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world._paused_event = threading.Event()
    near_cell = (0, 0, 0)
    route_cell = (5, 0, 0)
    world.available_cells = {near_cell, route_cell}
    world.config = streaming_world.StreamingConfig(
        chunk_size=1.0,
        load_radius_cells=0,
        max_loaded_chunks=16,
    )
    world._last_camera_cell = None
    world._last_load_radius = None
    world.loaded_cells = set()
    world._pending = set()
    world._failed_cells = {}
    world._lock = threading.Lock()
    world._work_queue = queue.Queue(maxsize=16)
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=16)
    world._last_wanted_cells = set()
    world._last_cam_cell_for_priority = None
    world._last_cell_priority_key = None
    world._cells_to_unload_next_drain = set()

    world.set_prefetch_wanted_cells({route_cell})
    world.update(np.zeros(3, dtype=np.float32))

    assert near_cell in world._last_wanted_cells
    assert route_cell in world._last_wanted_cells
    assert route_cell in set(world._work_queue.queue)
    assert world.prefetch_wanted_cells_snapshot() == frozenset({route_cell})


def test_update_dispatch_is_bounded_by_work_queue_capacity():
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world._paused_event = threading.Event()
    world.available_cells = {(index, 0, 0) for index in range(10)}
    world.config = streaming_world.StreamingConfig(
        chunk_size=1.0,
        load_radius_cells=9,
        max_loaded_chunks=10,
    )
    world._last_camera_cell = None
    world._last_load_radius = None
    world.loaded_cells = set()
    world._pending = set()
    world._lock = threading.Lock()
    world._work_queue = queue.Queue(maxsize=2)
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=16)
    world._last_wanted_cells = set()
    world._last_cam_cell_for_priority = None
    world._cells_to_unload_next_drain = set()

    world.update(np.zeros(3, dtype=np.float32))

    assert len(world._last_wanted_cells) == 10
    assert world._work_queue.qsize() == 2
    assert len(world._pending) == 2


def test_update_dispatch_uses_owner_priority_outside_internal_lock():
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world._paused_event = threading.Event()
    world.available_cells = {(1, 0, 0), (2, 0, 0), (3, 0, 0)}
    world.config = streaming_world.StreamingConfig(
        chunk_size=1.0,
        load_radius_cells=3,
        max_loaded_chunks=16,
    )
    world._last_camera_cell = None
    world._last_load_radius = None
    world.loaded_cells = set()
    world._pending = set()
    world._failed_cells = {}
    world._lock = threading.Lock()
    world._work_queue = queue.Queue(maxsize=16)
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=16)
    world._last_wanted_cells = set()
    world._last_cam_cell_for_priority = None
    world._last_cell_priority_key = None
    world._cells_to_unload_next_drain = set()
    lock_owned_during_priority = []

    def priority(cell):
        lock_owned_during_priority.append(world._lock.locked())
        return {
            (3, 0, 0): 0,
            (1, 0, 0): 1,
            (2, 0, 0): 2,
        }[cell]

    world.update(np.zeros(3, dtype=np.float32), cell_priority_key=priority)

    assert [world._work_queue.get_nowait() for _ in range(3)] == [
        (3, 0, 0),
        (1, 0, 0),
        (2, 0, 0),
    ]
    assert lock_owned_during_priority
    assert all(owned is False for owned in lock_owned_during_priority)


def test_render_distance_wanted_set_is_not_trimmed_by_residency_budget():
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world._paused_event = threading.Event()
    world.available_cells = {(index, 0, 0) for index in range(1, 6)}
    world.config = streaming_world.StreamingConfig(
        chunk_size=1.0,
        load_radius_cells=5,
        max_loaded_chunks=2,
    )
    world._last_camera_cell = None
    world._last_load_radius = None
    world.loaded_cells = set()
    world._pending = set()
    world._lock = threading.Lock()
    world._work_queue = queue.Queue(maxsize=16)
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=16)
    world._last_wanted_cells = set()
    world._last_cam_cell_for_priority = None
    world._cells_to_unload_next_drain = set()

    world.update(np.zeros(3, dtype=np.float32))

    assert world._last_wanted_cells == world.available_cells
    assert world._work_queue.qsize() == 5
    assert len(world._pending) == 5


def test_texture_gpu_estimate_includes_mipmap_and_driver_alignment(tmp_path):
    from PIL import Image

    texture_path = tmp_path / "rock.png"
    Image.new("RGB", (16, 8)).save(texture_path)

    assert streaming_world.StreamingWorld._estimate_texture_gpu_bytes(
        "rock.png", str(tmp_path)
    ) == int(16 * 8 * 4 * (4.0 / 3.0))


def test_texture_gpu_estimate_rejects_unsafe_texture_path(tmp_path, caplog):
    outside = tmp_path.parent / "outside.png"
    from PIL import Image

    Image.new("RGB", (16, 8)).save(outside)

    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        assert streaming_world.StreamingWorld._estimate_texture_gpu_bytes(
            "../outside.png", str(tmp_path)
        ) == 0

    assert "Unsafe texture path" in caplog.text


def test_stale_ready_chunk_is_discarded_without_gpu_upload():
    stale_cell = (1, 0, 0)
    world = _world_with_ready_chunks(stale_cell)
    world._last_wanted_cells = set()
    ready = []

    _drain(world, ready)

    assert ready == []
    assert world._ready_backlog.qsize() == 0
    assert stale_cell not in world.loaded_cells
    assert stale_cell not in world._pending


def test_update_discards_ready_chunks_that_are_no_longer_wanted():
    stale_cell = (0, 0, 0)
    world = _world_with_ready_chunks(stale_cell)
    world.available_cells = {stale_cell}
    world.config = streaming_world.StreamingConfig(
        chunk_size=1.0,
        load_radius_cells=0,
        max_loaded_chunks=16,
    )
    world._last_camera_cell = None
    world._last_load_radius = None
    world._work_queue = queue.Queue()

    world.update(np.array([10.0, 0.0, 0.0], dtype=np.float32))

    assert world._ready_backlog.qsize() == 0
    assert stale_cell not in world._pending


def test_stationary_view_requeues_wanted_cell_after_pending_cleanup():
    cell = (0, 0, 0)
    world = _world_with_ready_chunks(cell)
    world._ready_backlog.clear()
    world._pending.clear()
    world.available_cells = {cell}
    world.config = streaming_world.StreamingConfig(
        chunk_size=1.0,
        load_radius_cells=0,
        max_loaded_chunks=16,
    )
    world._last_camera_cell = cell
    world._last_load_radius = 0
    world._work_queue = queue.Queue()

    world.update(np.zeros(3, dtype=np.float32))

    assert cell in world._pending
    assert world._work_queue.get_nowait() == cell


def test_update_does_not_requeue_failed_wanted_cell():
    cell = (0, 0, 0)
    world = _world_with_ready_chunks(cell)
    failure = streaming_world.StreamingWorkerFailure(
        cell=cell,
        stage="load_chunk_file",
        error_type="ValueError",
        message="bad chunk",
        thread_name="test-worker",
        fatal=True,
    )
    world._ready_backlog.clear()
    world._pending.clear()
    world._failed_cells = {cell: failure}
    world.available_cells = {cell}
    world.config = streaming_world.StreamingConfig(
        chunk_size=1.0,
        load_radius_cells=0,
        max_loaded_chunks=16,
    )
    world._last_camera_cell = cell
    world._last_load_radius = 0
    world._work_queue = queue.Queue()

    world.update(np.zeros(3, dtype=np.float32))

    assert world._pending == set()
    assert world._work_queue.empty()
    assert world.stats()["failed_wanted"] == 1


def test_worker_failure_queue_drops_oldest_notification_when_full():
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world._lock = threading.Lock()
    world._pending = {(1, 0, 0), (2, 0, 0)}
    world._failed_cells = {}
    world._worker_failure_queue = queue.Queue(maxsize=1)

    world._record_worker_failure(
        (1, 0, 0),
        "load_chunk_file",
        ValueError("first"),
        fatal=True,
    )
    world._record_worker_failure(
        (2, 0, 0),
        "load_chunk_file",
        ValueError("second"),
        fatal=True,
    )

    failures = world.drain_worker_failures()
    assert [failure.cell for failure in failures] == [(2, 0, 0)]
    assert set(world.failed_cells()) == {(1, 0, 0), (2, 0, 0)}


def test_worker_skips_stale_queued_chunk_before_disk_io(monkeypatch):
    stale_cell = (1, 0, 0)
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world.cache_dir = "unused"
    _configure_worker_preprocess(world)
    world._stop_event = threading.Event()
    world._paused_event = threading.Event()
    world._work_queue = queue.Queue()
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=1)
    world._lock = threading.Lock()
    world._pending = {stale_cell}
    world._last_wanted_cells = set()
    world._work_queue.put(stale_cell)
    world._work_queue.put(None)

    monkeypatch.setattr(
        streaming_world.chunker,
        "load_chunk_file",
        lambda *_args: pytest.fail("stale chunk must not be read"),
    )

    world._worker_loop()

    assert stale_cell not in world._pending


def test_shutdown_clears_pending_and_ready_cpu_payloads():
    cell = (1, 0, 0)
    world = _world_with_ready_chunks(cell)
    world._stop_event = threading.Event()
    world._work_queue = queue.Queue()
    world._workers = []

    world.shutdown()

    assert world._pending == set()
    assert world._ready_backlog.qsize() == 0


def test_shutdown_does_not_block_on_full_work_queue():
    world = _world_with_ready_chunks((1, 0, 0))
    world._stop_event = threading.Event()
    world._work_queue = queue.Queue(maxsize=1)
    world._work_queue.put_nowait((99, 0, 0))
    worker_started = threading.Event()

    def worker_loop():
        worker_started.set()
        while not world._stop_event.is_set():
            time.sleep(0.01)

    worker = threading.Thread(target=worker_loop, name="test-stream-worker")
    worker.start()
    assert worker_started.wait(timeout=1.0)
    world._workers = [worker]

    world.shutdown()

    assert not worker.is_alive()
    assert world._pending == set()


def test_shutdown_times_out_and_accounts_for_noncooperating_worker(caplog):
    world = _world_with_ready_chunks((1, 0, 0))
    world._stop_event = threading.Event()
    world._work_queue = queue.Queue()
    worker_started = threading.Event()
    release_worker = threading.Event()

    def worker_loop():
        worker_started.set()
        release_worker.wait(timeout=5.0)

    # Keep this synthetic noncooperating thread daemonized so a failed test
    # cannot strand the test process; real StreamingWorld workers are asserted
    # non-daemon in the startup/growth test above.
    worker = threading.Thread(
        target=worker_loop,
        name="test-stuck-stream-worker",
        daemon=True,
    )
    worker.start()
    assert worker_started.wait(timeout=1.0)
    world._workers = [worker]
    started_at = time.perf_counter()

    with caplog.at_level(logging.WARNING, logger="caveviewer"):
        world.shutdown(timeout=0.05)

    elapsed = time.perf_counter() - started_at
    try:
        assert elapsed < 0.5
        assert worker.is_alive()
        assert world._shutdown_unjoined_workers == [worker]
        assert world._workers == [worker]
        assert world._pending == set()
        assert "1 worker(s) still running" in caplog.text
        assert "daemon worker" not in caplog.text
    finally:
        release_worker.set()
        worker.join(timeout=1.0)


def test_shutdown_wakes_paused_worker_without_sleep(monkeypatch):
    class TrackingEvent:
        def __init__(self):
            self._event = threading.Event()
            self.wait_started = threading.Event()

        def set(self):
            self._event.set()

        def clear(self):
            self._event.clear()

        def wait(self, timeout=None):
            self.wait_started.set()
            return self._event.wait(timeout)

    monkeypatch.setattr(
        streaming_world.time,
        "sleep",
        lambda _seconds: pytest.fail("paused worker must wait on an event"),
    )

    world = _world_with_ready_chunks((1, 0, 0))
    world._stop_event = threading.Event()
    world._paused_event = threading.Event()
    world._paused_event.set()
    wakeup_event = TrackingEvent()
    world._worker_wakeup_event = wakeup_event
    world._work_queue = queue.Queue()

    worker = threading.Thread(target=world._worker_loop, name="test-paused-worker")
    worker.start()
    assert wakeup_event.wait_started.wait(timeout=1.0)
    world._workers = [worker]

    world.shutdown(timeout=1.0)

    assert not worker.is_alive()


def test_worker_does_not_start_queued_cell_after_shutdown_race(monkeypatch):
    cell = (1, 0, 0)
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world.cache_dir = "unused"
    _configure_worker_preprocess(world)
    world._stop_event = threading.Event()
    world._paused_event = threading.Event()
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=1)
    world._lock = threading.Lock()
    world._pending = {cell}
    world._last_wanted_cells = {cell}

    class StopDuringGetQueue(queue.Queue):
        def get(self, *args, **kwargs):
            item = super().get(*args, **kwargs)
            world._stop_event.set()
            return item

    world._work_queue = StopDuringGetQueue()
    world._work_queue.put(cell)
    monkeypatch.setattr(
        streaming_world.chunker,
        "load_chunk_file",
        lambda *_args, **_kwargs: pytest.fail(
            "worker must not process a real cell after shutdown is requested"
        ),
    )

    world._worker_loop()

    assert cell not in world._pending
    assert world._ready_backlog.qsize() == 0


def test_worker_skips_preprocess_callbacks_when_shutdown_follows_chunk_load(
    monkeypatch,
):
    cell = (1, 0, 0)
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world.cache_dir = "unused"
    _configure_worker_preprocess(
        world,
        on_decode_textures=lambda _data: pytest.fail(
            "texture predecode callback must not run after shutdown"
        ),
        prepack_smooth_shading=True,
    )
    world._stop_event = threading.Event()
    world._paused_event = threading.Event()
    world._work_queue = queue.Queue()
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=1)
    world._lock = threading.Lock()
    world._pending = {cell}
    world._last_wanted_cells = {cell}
    world._work_queue.put(cell)

    def load_chunk(_cache_dir, requested_cell):
        world._stop_event.set()
        return _chunk(requested_cell)

    monkeypatch.setattr(streaming_world.chunker, "load_chunk_file", load_chunk)
    monkeypatch.setattr(
        streaming_world.chunker,
        "prepare_chunk_upload_groups",
        lambda _data: pytest.fail("chunk preparation must not run after shutdown"),
    )

    world._worker_loop()

    assert cell not in world._pending
    assert world._ready_backlog.qsize() == 0


def test_worker_pause_requeue_does_not_block_if_queue_refills():
    cell = (1, 0, 0)
    filler_cell = (2, 0, 0)
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world.cache_dir = "unused"
    _configure_worker_preprocess(world)
    world._stop_event = threading.Event()
    world._paused_event = threading.Event()
    world._ready_backlog = streaming_scheduler.BoundedReadyBacklog(capacity=1)
    world._lock = threading.Lock()
    world._pending = {cell}
    world._last_wanted_cells = {cell}

    class RefillingQueue(queue.Queue):
        def get(self, *args, **kwargs):
            item = super().get(*args, **kwargs)
            world._paused_event.set()
            super().put_nowait(filler_cell)
            world._stop_event.set()
            return item

    world._work_queue = RefillingQueue(maxsize=1)
    world._work_queue.put_nowait(cell)

    worker = threading.Thread(target=world._worker_loop)
    worker.start()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert cell not in world._pending


def test_stats_reads_mutable_streaming_sets_under_lock():
    cell = (1, 0, 0)
    pending_cell = (2, 0, 0)
    world = _world_with_ready_chunks(cell)
    lock = threading.Lock()
    world._lock = lock
    world.loaded_cells = _LockCheckingSet(lock, {cell})
    world._pending = _LockCheckingSet(lock, {pending_cell})
    world._last_wanted_cells = _LockCheckingSet(lock, {cell, pending_cell})

    stats = world.stats()

    assert stats["loaded"] == 1
    assert stats["loaded_wanted"] == 1
    assert stats["pending"] == 1
    assert stats["wanted"] == 2


def test_wanted_cells_snapshot_returns_thread_safe_copy():
    cell = (1, 0, 0)
    world = _world_with_ready_chunks(cell)
    world._last_wanted_cells = {cell}

    snapshot = world.wanted_cells_snapshot()
    world._last_wanted_cells.clear()

    assert snapshot == frozenset({cell})
