"""Exercise streaming queues, worker lifecycle, prioritization, and shutdown."""

from __future__ import annotations

import logging
import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from caveviewer.core import hardware_memory, streaming_scheduler, streaming_world
from caveviewer.core.worker_config import WorkerAllocation


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
        lambda: hardware_memory.RamSnapshot(100, ram_available),
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
    world.on_decode_textures = None
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


def test_worker_prepacks_vertex_bytes_before_ready_handoff(monkeypatch):
    cell = (2, 0, 0)
    prepacked = []
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world.cache_dir = "unused"
    world.on_decode_textures = None
    world.prepack_smooth_shading = lambda: True
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


def test_texture_gpu_estimate_includes_mipmap_and_driver_alignment(tmp_path):
    from PIL import Image

    texture_path = tmp_path / "rock.png"
    Image.new("RGB", (16, 8)).save(texture_path)

    assert streaming_world.StreamingWorld._estimate_texture_gpu_bytes(
        "rock.png", str(tmp_path)
    ) == int(16 * 8 * 4 * (4.0 / 3.0))


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


def test_stationary_view_requeues_wanted_cell_after_terminal_failure():
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


def test_worker_skips_stale_queued_chunk_before_disk_io(monkeypatch):
    stale_cell = (1, 0, 0)
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world.cache_dir = "unused"
    world.on_decode_textures = None
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


def test_worker_pause_requeue_does_not_block_if_queue_refills():
    cell = (1, 0, 0)
    filler_cell = (2, 0, 0)
    world = streaming_world.StreamingWorld.__new__(streaming_world.StreamingWorld)
    world.cache_dir = "unused"
    world.on_decode_textures = None
    world.prepack_smooth_shading = None
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
    assert stats["pending"] == 1
    assert stats["wanted"] == 2
