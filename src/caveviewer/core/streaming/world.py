"""
caveviewer.core.streaming.world

Runtime chunk streaming: watches the camera's world position and keeps
only a radius of chunks around it loaded into GPU memory, uploading newly
needed chunks and evicting ones that fall out of range. This is the actual
mechanism that prevents lag on big maps -- the renderer never sees more
geometry/textures than fit within `load_radius_cells` of the camera,
regardless of how large the full cave map is.

This module is GPU-API-agnostic: it deals in ChunkData (CPU-side numpy
arrays) and calls back into caller-supplied upload/evict functions so the
moderngl-specific VBO/texture code lives in caveviewer.gui.viewer_window, not here.
This keeps the streaming *logic* unit-testable without an OpenGL context
(see the test suite -- we verify load/unload behavior with fake GPU hooks).
"""

from __future__ import annotations

import os
import threading
import queue
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import numpy as np

from caveviewer.core.chunking import builder as chunker
from caveviewer.core.hardware import gpu_memory, memory_targets, system_memory
from caveviewer.core.workers.allocation import (
    MAX_WORKER_RAM_UTILIZATION,
    can_start_additional_worker,
    describe_worker_target,
    resolve_worker_allocation,
)
from caveviewer.core.chunking.io import ChunkData
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.streaming.budget import (
    calculate_residency_budget,
    estimate_chunk_bytes,
)
from caveviewer.core.streaming.scheduler import (
    BoundedReadyBacklog,
    Cell,
    cell_distance_sq,
    cell_in_cube_radius,
    cells_outside_cube_radius,
    select_evictions,
    select_wanted_cells,
)
from caveviewer.core.textures.decoding import resolve_texture_path


_LOG = get_logger("StreamingWorld")
_SHUTDOWN_WORKER_JOIN_POLL_SECONDS = 0.25
_SHUTDOWN_WORKER_JOIN_TIMEOUT_SECONDS = 2.0
_SHUTDOWN_WORKER_JOIN_LOG_SECONDS = 5.0
_READY_BACKLOG_TARGET_CHUNKS = 16
_WORKER_FAILURE_BACKLOG_CAPACITY = 256
CellPriorityKey = Callable[[Cell], object]

# Compatibility hooks for existing diagnostics/tests that patch these names
# through core.streaming.world rather than the lower-level memory modules.
subprocess = gpu_memory.subprocess
sys = gpu_memory.sys

_AMD_PCI_VENDOR_ID = gpu_memory.AMD_PCI_VENDOR_ID
_LINUX_DRM_ROOT = gpu_memory.LINUX_DRM_ROOT


def _detect_total_ram_bytes() -> int:
    return system_memory.detect_total_ram_bytes()


def _detect_ram_snapshot() -> system_memory.RamSnapshot | None:
    return system_memory.detect_ram_snapshot()


def _parse_target_fraction(raw_value: str | None, conservative_default: float) -> float:
    return memory_targets.parse_target_fraction(
        raw_value, conservative_default
    )


def _parse_memory_target_fraction(raw_value: str | None) -> float:
    return memory_targets.parse_memory_target_fraction(raw_value)


def _parse_gpu_target_fraction(raw_value: str | None) -> float:
    return memory_targets.parse_gpu_target_fraction(raw_value)


_read_positive_sysfs_int = gpu_memory.read_positive_sysfs_int


def _detect_linux_amd_gpu_memory_bytes(
    drm_root: str | os.PathLike[str] = _LINUX_DRM_ROOT,
) -> int | None:
    return gpu_memory.detect_linux_amd_gpu_memory_bytes(drm_root)


def _detect_nvidia_gpu_memory_bytes() -> int | None:
    return gpu_memory.detect_nvidia_gpu_memory_bytes()


def _detect_total_gpu_memory_bytes(gpu_vendor: str | None = None) -> int | None:
    return gpu_memory.detect_total_gpu_memory_bytes(
        gpu_vendor,
        nvidia_detector=_detect_nvidia_gpu_memory_bytes,
        amd_detector=_detect_linux_amd_gpu_memory_bytes,
        logger=_LOG,
    )


@dataclass
class StreamingConfig:
    chunk_size: float
    load_radius_cells: int = 3     # ring radius kept loaded around camera
    # (unload_radius > load_radius prevents thrashing when camera sits
    #  near a cell boundary and jitters back and forth)
    unload_radius_margin: int = 1  # how many cells beyond load_radius before eviction
    max_loaded_chunks: int = 400   # hard cap as a safety valve regardless of radius
    unload_chunks_per_frame: int = 1
    unload_time_budget_ms: float = 1.0
    unload_retire_frames: int = 2

    @property
    def unload_radius_cells(self) -> int:
        """
        Derived from load_radius_cells rather than stored as an
        independent fixed value, so the hysteresis gap between "keep
        loaded" and "evict" stays correct automatically even when
        load_radius_cells changes at runtime (see the render-distance
        slider in viewer_window.py) -- a fixed unload_radius_cells set
        once at construction would otherwise need to be kept in sync by
        hand every time the load radius changes, and a bug there would
        be the kind of thing that's easy to miss (the gap silently
        shrinking or inverting) until someone actually notices chunks
        thrashing load/unload near a boundary.
        """
        return self.load_radius_cells + self.unload_radius_margin


@dataclass(frozen=True)
class StreamingWorkerFailure:
    """Structured notification for a background streaming worker exception.

    Workers publish these records to a bounded queue so the owning
    application/render thread can drain and report failures without polling
    worker internals.  Fatal failures also move the cell into
    StreamingWorld's failed-cell state; non-fatal failures report failed
    best-effort preprocessing while still allowing the chunk to become ready.
    """

    cell: tuple[int, int, int]
    stage: str
    error_type: str
    message: str
    thread_name: str
    fatal: bool


_BoundedReadyBacklog = BoundedReadyBacklog


class StreamingWorld:
    """
    Call `update(camera_position)` once per frame (or every N frames -- it's
    cheap, but you can throttle further if desired). It will:
      - compute which cells *should* be loaded given the camera position
      - kick off background loads (disk I/O + numpy unpacking) for missing
        ones via a worker thread, so disk reads never block the render
        thread / cause a frame hitch
      - call `on_chunk_ready(ChunkData)` on the main thread (via
        `drain_ready_chunks()`, which you call once per frame) for any
        chunks that finished loading
      - call `on_chunk_unload(cell)` for chunks that should be evicted

    Thread ownership:
      - The owning application/render thread calls lifecycle and frame methods
        such as `update()`, `drain_ready_chunks()`, `pause()`, `resume()`,
        `stats()`, `drain_worker_failures()`, and `shutdown()`.
      - Streaming worker threads are owned by this object and do only CPU-side
        chunk loading/preparation work. They never issue OpenGL commands.
      - `on_chunk_ready` and `on_chunk_unload` run synchronously inside
        `drain_ready_chunks()` on the owner thread. Internal loaded/unloaded
        state is committed only after those callbacks reach their documented
        transaction point.
      - `on_decode_textures` runs in worker threads and must be a stable,
        CPU-only, worker-safe callable.
      - Smooth-shading prepack policy is not read through a callback. The
        owner thread publishes that immutable boolean state with
        `set_prepack_smooth_shading()`, and workers read the latest published
        value.
    """

    def __init__(self, cache_dir: str, config: StreamingConfig,
                 on_decode_textures: Optional[Callable[[ChunkData], None]] = None,
                 prepack_smooth_shading: bool | None = None,
                 gpu_vendor: str | None = None,
                 textures_dir: str | None = None,
                 total_gpu_memory_bytes: int | None = None,
                 texture_gpu_budget_bytes: int | None = None,
                 gpu_geometry_budget_bytes: int | None = None):
        """
        on_decode_textures, if given, is called from a background worker
        thread right after a chunk's geometry finishes loading, with the
        ChunkData as the argument. The callable must be CPU-only, worker-safe,
        and stable for this StreamingWorld instance; it must not read GUI,
        OpenGL, camera, scene, or other render-thread-owned mutable state.
        This hook lets the GUI pre-decode textures without importing
        TextureManager here, preserving core's GPU-API-agnostic design.

        prepack_smooth_shading, if not None, is a published owner-thread
        snapshot of which shade mode's interleaved vertex bytes workers should
        pack before the chunk reaches the render thread. It is deliberately a
        bool instead of a callback so workers never query GUI/render state
        directly. The owner thread may update it later with
        set_prepack_smooth_shading(). If the user toggles SHADE after a chunk is
        prepacked, the renderer can still compute the other mode from retained
        source arrays.

        gpu_vendor is the active OpenGL context's GL_VENDOR string when the
        caller has one. It prevents a secondary adapter from supplying the
        memory budget on hybrid-GPU systems.

        total_gpu_memory_bytes may be supplied by a caller that already ran
        active-GPU detection for related setup, such as texture resolution
        selection.  When omitted, this class detects it itself.

        texture_gpu_budget_bytes and gpu_geometry_budget_bytes let the caller
        split one detected GPU target between texture and geometry residency.
        When omitted, geometry falls back to the legacy full-GPU-target cap.
        """
        self.cache_dir = cache_dir
        self.config = config
        self._on_decode_textures = on_decode_textures
        self._prepack_smooth_shading_lock = threading.Lock()
        self._prepack_smooth_shading = self._normalize_prepack_smooth_shading(
            prepack_smooth_shading
        )
        manifest = chunker.load_manifest(cache_dir) or {"chunks": {}}
        manifest_max_upload_group_mb = chunker.manifest_max_upload_group_mb(manifest)
        self._chunk_file_max_group_bytes = (
            int(manifest_max_upload_group_mb * 1024 ** 2)
            if manifest_max_upload_group_mb is not None
            else None
        )
        chunks = manifest.get("chunks", {})
        sampled_chunk_keys: list[str] = []
        self.available_cells: set[tuple[int, int, int]] = set()
        for i, cell_str in enumerate(chunks.keys()):
            self.available_cells.add(tuple(int(x) for x in cell_str.split("_")))
            if i < 128:
                sampled_chunk_keys.append(cell_str)
        self._last_wanted_cells: set[tuple[int, int, int]] = set()

        target_env = os.environ.get("CAVEVIEWER_MEMORY_UTILIZATION_TARGET")
        self._memory_target_fraction = _parse_memory_target_fraction(target_env)
        ram_snapshot = _detect_ram_snapshot()
        if ram_snapshot is not None:
            self._total_ram_bytes = ram_snapshot.total_bytes
            self._available_ram_bytes = ram_snapshot.available_bytes
        else:
            self._total_ram_bytes = _detect_total_ram_bytes()
            self._available_ram_bytes = self._total_ram_bytes
        self._estimated_chunk_ram_bytes = self._estimate_chunk_ram_bytes(sampled_chunk_keys)
        gpu_target_env = os.environ.get("CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET")
        self._gpu_target_fraction = _parse_gpu_target_fraction(gpu_target_env)
        self._total_gpu_memory_bytes = (
            total_gpu_memory_bytes
            if total_gpu_memory_bytes is not None
            else _detect_total_gpu_memory_bytes(gpu_vendor)
        )
        self._texture_gpu_budget_bytes = (
            max(0, int(texture_gpu_budget_bytes))
            if texture_gpu_budget_bytes is not None
            else None
        )
        self._gpu_geometry_budget_bytes = (
            max(0, int(gpu_geometry_budget_bytes))
            if gpu_geometry_budget_bytes is not None
            else None
        )
        self._estimated_chunk_gpu_bytes = self._estimate_chunk_gpu_bytes(sampled_chunk_keys)
        self._total_gpu_residency_budget_bytes: int | None = None
        self._texture_gpu_bytes: dict[object, int] = {}
        self._configure_texture_gpu_estimates(manifest, textures_dir)
        self._configure_chunk_budget_from_memory_targets()

        self.loaded_cells: set[tuple[int, int, int]] = set()
        self._pending: set[tuple[int, int, int]] = set()
        self._failed_cells: dict[tuple[int, int, int], StreamingWorkerFailure] = {}
        self._partial_ready: list[ChunkData] = []
        # Cells leave loaded_cells only when their caller-provided unload
        # callback actually runs.  Evictions are queued here first so the
        # render thread never has to delete many VAO/VBO/texture objects in
        # one frame; OpenGL deletion can otherwise synchronize with recent
        # GPU use and create visible hitches.
        self._unload_backlog: list[tuple[tuple[int, int, int], int]] = []
        self._unload_backlog_cells: set[tuple[int, int, int]] = set()
        # Cap how many fully-decoded chunk payloads can wait in RAM.
        # This bounds worst-case worker-ahead memory spikes when the
        # render thread is temporarily slower than background decoding.
        ready_backlog_capacity = max(
            1,
            int(
                getattr(
                    self,
                    "_ready_backlog_capacity",
                    min(_READY_BACKLOG_TARGET_CHUNKS, max(1, self.config.max_loaded_chunks)),
                )
            ),
        )
        self._ready_backlog = _BoundedReadyBacklog(ready_backlog_capacity)
        self._lock = threading.Lock()
        worker_allocation = resolve_worker_allocation(
            os.environ.get("CAVEVIEWER_IO_WORKERS"),
            os.environ.get("CAVEVIEWER_IO_RESERVED_CPUS"),
            default_workers=2,
            default_reserved_cpus=3,
        )
        self._worker_pool_size = worker_allocation.effective_workers
        _LOG.info(describe_worker_target("Streaming", worker_allocation))
        self._stop_event = threading.Event()
        self._paused_event = threading.Event()
        self._worker_wakeup_event = threading.Event()
        # Keep queued-but-not-yet-decoded work bounded.  The ready backlog
        # already caps decoded payloads; this caps the earlier scheduling
        # stage so a dense start cell or high render radius cannot enqueue the
        # whole desired view at once and force the startup overlay to wait for
        # a large pending set before the user can begin.
        self._work_queue_capacity = max(16, min(512, self.config.max_loaded_chunks * 2))
        self._work_queue: "queue.Queue[tuple[int, int, int] | None]" = queue.Queue(
            maxsize=self._work_queue_capacity
        )
        # Failure notifications are bounded so a broken cache cannot let
        # workers accumulate unbounded diagnostic records.  The persistent
        # failed-cell map remains authoritative even if old notifications are
        # dropped before the render thread drains them.
        self._worker_failure_queue: "queue.Queue[StreamingWorkerFailure]" = (
            queue.Queue(maxsize=_WORKER_FAILURE_BACKLOG_CAPACITY)
        )
        self._worker_start_lock = threading.Lock()
        self._worker_admission_blocked = False
        self._workers: list[threading.Thread] = []
        self._shutdown_unjoined_workers: list[threading.Thread] = []
        # The configured count is a maximum. Start one worker unconditionally;
        # completed chunk work will make memory cost observable before each
        # additional worker is admitted.
        with self._worker_start_lock:
            self._start_worker_locked()

        self._last_camera_cell: Optional[tuple[int, int, int]] = None
        self._last_load_radius: Optional[int] = None
        self._last_cell_priority_key: CellPriorityKey | None = None

    @staticmethod
    def _normalize_prepack_smooth_shading(value: bool | None) -> bool | None:
        if value is None:
            return None
        if callable(value):
            raise TypeError(
                "prepack_smooth_shading must be a bool or None, not a callback; "
                "publish owner-thread state with set_prepack_smooth_shading()"
            )
        return bool(value)

    def set_prepack_smooth_shading(self, smooth_shading: bool | None) -> None:
        """Publish owner-thread shading state for worker CPU prepack.

        ``None`` disables worker prepack. ``True`` and ``False`` ask workers to
        prepack the corresponding CPU-side vertex-byte layout. Workers read this
        value through StreamingWorld's synchronization boundary and never call
        back into GUI/render-owned state.
        """
        normalized = self._normalize_prepack_smooth_shading(smooth_shading)
        with self._prepack_smooth_shading_lock:
            self._prepack_smooth_shading = normalized

    def _prepack_smooth_shading_snapshot(self) -> bool | None:
        with self._prepack_smooth_shading_lock:
            return self._prepack_smooth_shading

    def _estimate_chunk_ram_bytes(self, chunk_keys: list[str]) -> int:
        """Estimate in-RAM cost per loaded chunk from cache chunk file sizes."""
        return estimate_chunk_bytes(
            self.cache_dir,
            chunk_keys,
            chunks_dirname=chunker.CHUNKS_DIRNAME,
            overhead_multiplier=6.0,
        )

    def _estimate_chunk_gpu_bytes(self, chunk_keys: list[str]) -> int:
        """Estimate GPU-resident cost per loaded chunk from cache chunk sizes."""
        return estimate_chunk_bytes(
            self.cache_dir,
            chunk_keys,
            chunks_dirname=chunker.CHUNKS_DIRNAME,
            overhead_multiplier=2.5,
        )

    def _configure_texture_gpu_estimates(
        self, manifest: dict, textures_dir: str | None
    ) -> None:
        """Estimate full-resolution texture cost for diagnostics and cap sizing."""
        if self._total_gpu_memory_bytes is None:
            return

        self._total_gpu_residency_budget_bytes = int(
            self._total_gpu_memory_bytes * self._gpu_target_fraction
        )
        if self._texture_gpu_budget_bytes is None:
            self._texture_gpu_budget_bytes = self._total_gpu_residency_budget_bytes
        material_textures = manifest.get("mtl_materials", {})
        texture_root = textures_dir or self.cache_dir

        for file_or_bytes in material_textures.values():
            texture_key = self._texture_cache_key(file_or_bytes)
            if texture_key is None or texture_key in self._texture_gpu_bytes:
                continue
            self._texture_gpu_bytes[texture_key] = self._estimate_texture_gpu_bytes(
                file_or_bytes, texture_root
            )

        estimated_texture_bytes = sum(self._texture_gpu_bytes.values())
        if estimated_texture_bytes > 0:
            _LOG.info(
                "Texture GPU estimate before any decode-time downscaling: %.1f MB "
                "across %d unique texture(s); texture upload target is %.1f MB.",
                estimated_texture_bytes / (1024 ** 2),
                len(self._texture_gpu_bytes),
                self._texture_gpu_budget_bytes / (1024 ** 2),
            )

    @staticmethod
    def _texture_cache_key(file_or_bytes) -> object | None:
        if not file_or_bytes:
            return None
        if isinstance(file_or_bytes, bytes):
            return ("embedded", len(file_or_bytes), hash(file_or_bytes))
        return ("file", str(file_or_bytes))

    @staticmethod
    def _estimate_texture_gpu_bytes(file_or_bytes, textures_dir: str) -> int:
        """Conservatively estimate GPU texture storage including mipmaps."""
        if not file_or_bytes:
            return 0
        try:
            from PIL import Image

            if isinstance(file_or_bytes, bytes):
                import io

                image_context = Image.open(io.BytesIO(file_or_bytes))
            else:
                image_context = Image.open(
                    resolve_texture_path(textures_dir, str(file_or_bytes))
                )
            with image_context as image:
                width, height = image.size
        except Exception as exc:
            display_name = "<embedded texture>" if isinstance(file_or_bytes, bytes) else file_or_bytes
            _LOG.warning("Could not estimate texture GPU size for %r: %s", display_name, exc)
            return 0

        # Drivers often store RGB textures with 4-byte alignment internally;
        # mipmaps add another ~1/3.  Use the conservative value so low-memory
        # GPUs do not accept a resident set that later fails in the driver.
        return int(width * height * 4 * (4.0 / 3.0))

    def _configure_chunk_budget_from_memory_targets(self) -> None:
        """Derive max_loaded_chunks from system RAM and GPU memory targets.

        Important: this is a policy cap for chunk residency, not a strict
        memory reservation. Actual process memory can differ due to Python
        object overhead, decode/transient buffers, GPU buffers/textures,
        driver usage, and whatever else is running on the machine.
        """
        if self._estimated_chunk_ram_bytes <= 0:
            return

        budget = calculate_residency_budget(
            available_cell_count=len(self.available_cells),
            total_ram_bytes=self._total_ram_bytes,
            available_ram_bytes=self._available_ram_bytes,
            ram_target_fraction=self._memory_target_fraction,
            estimated_chunk_ram_bytes=self._estimated_chunk_ram_bytes,
            total_gpu_memory_bytes=self._total_gpu_memory_bytes,
            gpu_target_fraction=self._gpu_target_fraction,
            estimated_chunk_gpu_bytes=self._estimated_chunk_gpu_bytes,
            gpu_budget_bytes=self._gpu_geometry_budget_bytes,
            ready_backlog_target_chunks=_READY_BACKLOG_TARGET_CHUNKS,
        )

        # Apply the memory-derived budget directly so env tuning can both
        # raise and lower residency as intended.
        self.config.max_loaded_chunks = budget.max_loaded_chunks
        self._ready_backlog_capacity = max(1, int(budget.ready_backlog_chunks))
        _LOG.info(
            "Memory target %.0f%% of %.1f GB currently available "
            "(%.1f GB total) => max_loaded_chunks=%d, ready_backlog=%d "
            "(estimated %.1f MB/chunk)",
            self._memory_target_fraction * 100.0,
            self._available_ram_bytes / (1024 ** 3),
            self._total_ram_bytes / (1024 ** 3),
            budget.max_loaded_chunks,
            budget.ready_backlog_chunks,
            self._estimated_chunk_ram_bytes / (1024 ** 2),
        )
        if budget.gpu_budget_chunks is not None:
            if self._gpu_geometry_budget_bytes is not None:
                _LOG.info(
                    "GPU geometry residency budget %.1f MB from shared GPU split "
                    "=> max_loaded_chunks=%d (estimated %.1f MB/chunk)",
                    budget.gpu_budget_bytes / (1024 ** 2),
                    budget.gpu_budget_chunks,
                    self._estimated_chunk_gpu_bytes / (1024 ** 2),
                )
            else:
                _LOG.info(
                    "GPU memory target %.0f%% of %.1f GB => max_loaded_chunks=%d "
                    "(estimated %.1f MB/chunk)",
                    self._gpu_target_fraction * 100.0,
                    self._total_gpu_memory_bytes / (1024 ** 3),
                    budget.gpu_budget_chunks,
                    self._estimated_chunk_gpu_bytes / (1024 ** 2),
                )
            _LOG.info("Effective max_loaded_chunks=%d after RAM/GPU limits.", self.config.max_loaded_chunks)
        else:
            _LOG.info(
                "GPU memory limit not applied; automatic detection was unavailable. "
                "Set CAVEVIEWER_GPU_MEMORY_GB to provide an explicit value."
            )

    def _start_worker_locked(self) -> None:
        """Create one worker while the caller owns _worker_start_lock."""
        worker_number = len(self._workers) + 1
        worker = threading.Thread(
            target=self._worker_loop,
            name=f"CaveViewer-stream-{worker_number}",
            daemon=False,
        )
        self._workers.append(worker)
        try:
            worker.start()
        except BaseException:
            self._workers.pop()
            raise

    def _maybe_start_additional_worker(self) -> bool:
        """Grow streaming concurrency by one after measuring current RAM."""
        worker_start_lock = getattr(self, "_worker_start_lock", None)
        if worker_start_lock is None:
            return False

        log_event = None
        with worker_start_lock:
            if (
                self._stop_event.is_set()
                or self._paused_event.is_set()
                or len(self._workers) >= self._worker_pool_size
                or self._work_queue.empty()
            ):
                return False

            snapshot = _detect_ram_snapshot()
            if not can_start_additional_worker(snapshot):
                if not self._worker_admission_blocked:
                    if snapshot is None:
                        log_event = ("missing_snapshot", len(self._workers))
                    else:
                        log_event = (
                            "ram_pressure",
                            snapshot.utilization_fraction,
                            len(self._workers),
                        )
                self._worker_admission_blocked = True
                started = False
            else:
                self._start_worker_locked()
                log_event = (
                    "started",
                    self._worker_admission_blocked,
                    snapshot.available_bytes,
                    snapshot.total_bytes,
                    snapshot.utilization_fraction,
                    len(self._workers),
                    self._worker_pool_size,
                )
                self._worker_admission_blocked = False
                started = True

        if log_event is None:
            return started
        if log_event[0] == "missing_snapshot":
            _LOG.warning(
                "Could not measure available system RAM; keeping "
                "streaming at %d worker(s).",
                log_event[1],
            )
        elif log_event[0] == "ram_pressure":
            _event, utilization_fraction, worker_count = log_event
            _LOG.warning(
                "System RAM utilization is %.1f%%; keeping streaming "
                "at %d worker(s) because the limit is %.0f%%.",
                utilization_fraction * 100.0,
                worker_count,
                MAX_WORKER_RAM_UTILIZATION * 100.0,
            )
        else:
            (
                _event,
                was_admission_blocked,
                available_bytes,
                total_bytes,
                utilization_fraction,
                worker_count,
                worker_pool_size,
            ) = log_event
            pressure_note = (
                "System RAM pressure eased; "
                if was_admission_blocked
                else ""
            )
            _LOG.info(
                "%sDetected system RAM for streaming worker admission: %.1f GB "
                "available of %.1f GB (%.1f%% used); increasing workers "
                "to %d of %d.",
                pressure_note,
                available_bytes / (1024 ** 3),
                total_bytes / (1024 ** 3),
                utilization_fraction * 100.0,
                worker_count,
                worker_pool_size,
            )
        return started

    def _drain_work_queue(self) -> None:
        while True:
            try:
                self._work_queue.get_nowait()
            except queue.Empty:
                return

    def shutdown(
        self,
        *,
        timeout: float | None = _SHUTDOWN_WORKER_JOIN_TIMEOUT_SECONDS,
    ):
        """Request worker shutdown, wake blocked workers, and join them.

        A finite ``timeout`` returns control to the owner if a worker is stuck
        in external I/O or callback code; unjoined non-daemon workers remain in
        ``_shutdown_unjoined_workers`` so the owner can report or retry before
        interpreter termination. Passing ``timeout=None`` waits until every
        worker has joined.
        """
        if timeout is not None and timeout < 0.0:
            raise ValueError("shutdown timeout must be non-negative")
        self._stop_event.set()
        self._wake_workers()
        worker_start_lock = getattr(self, "_worker_start_lock", None)
        if worker_start_lock is None:
            workers = list(self._workers)
        else:
            with worker_start_lock:
                workers = list(self._workers)
        for _ in workers:
            try:
                self._work_queue.put_nowait(None)  # sentinel to unblock get()
            except queue.Full:
                # stop_event is already set. A full queue means workers will
                # notice shutdown when they finish their current item or when
                # their queue.get timeout wakes. Blocking here could deadlock
                # teardown before we reach the join barrier below.
                pass
        wait_started_at = time.perf_counter()
        last_wait_log_at = wait_started_at
        deadline = None if timeout is None else wait_started_at + timeout
        unjoined_workers: list[threading.Thread] = []
        for w in workers:
            while w.is_alive():
                join_timeout = _SHUTDOWN_WORKER_JOIN_POLL_SECONDS
                if deadline is not None:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0.0:
                        break
                    join_timeout = min(join_timeout, remaining)
                w.join(timeout=join_timeout)
                if not w.is_alive():
                    break
                now = time.perf_counter()
                if now - last_wait_log_at >= _SHUTDOWN_WORKER_JOIN_LOG_SECONDS:
                    _LOG.info(
                        "Waiting for streaming worker %s to stop during shutdown (%.1fs).",
                        getattr(w, "name", "<unnamed>"),
                        now - wait_started_at,
                    )
                    last_wait_log_at = now
            if w.is_alive():
                unjoined_workers.append(w)
        elapsed = time.perf_counter() - wait_started_at
        if unjoined_workers:
            names = ", ".join(
                getattr(worker, "name", "<unnamed>")
                for worker in unjoined_workers
            )
            _LOG.warning(
                "Streaming worker shutdown timed out after %.2fs; "
                "%d worker(s) still running: %s",
                elapsed,
                len(unjoined_workers),
                names,
            )
        self._drain_work_queue()
        if worker_start_lock is None:
            self._workers = unjoined_workers
        else:
            with worker_start_lock:
                self._workers = unjoined_workers
        self._shutdown_unjoined_workers = unjoined_workers
        self._ready_backlog.clear()
        partial_ready = getattr(self, "_partial_ready", None)
        if partial_ready is not None:
            partial_ready.clear()
        with self._lock:
            self._pending.clear()

    def pause(self):
        self._paused_event.set()
        self._wake_workers()

    def resume(self):
        self._paused_event.clear()
        self._wake_workers()

    def is_paused(self) -> bool:
        return self._paused_event.is_set()

    def _cell_is_wanted(self, cell: tuple[int, int, int]) -> bool:
        with self._lock:
            return cell in self._last_wanted_cells

    def _clear_pending_cell(self, cell: tuple[int, int, int]) -> None:
        with self._lock:
            self._pending.discard(cell)

    def _wake_workers(self) -> None:
        worker_wakeup_event = getattr(self, "_worker_wakeup_event", None)
        if worker_wakeup_event is not None:
            worker_wakeup_event.set()

    def _wait_while_paused(self) -> None:
        worker_wakeup_event = getattr(self, "_worker_wakeup_event", None)
        while self._paused_event.is_set() and not self._stop_event.is_set():
            if worker_wakeup_event is None:
                self._stop_event.wait(timeout=0.1)
                continue
            worker_wakeup_event.clear()
            if not self._paused_event.is_set() or self._stop_event.is_set():
                return
            worker_wakeup_event.wait()

    def _ensure_worker_failure_queue(self) -> "queue.Queue[StreamingWorkerFailure]":
        if not hasattr(self, "_worker_failure_queue"):
            self._worker_failure_queue = queue.Queue(
                maxsize=_WORKER_FAILURE_BACKLOG_CAPACITY
            )
        return self._worker_failure_queue

    def _publish_worker_failure(self, failure: StreamingWorkerFailure) -> None:
        failure_queue = self._ensure_worker_failure_queue()
        try:
            failure_queue.put_nowait(failure)
            return
        except queue.Full:
            pass

        # The failed-cell map keeps authoritative state.  If the notification
        # queue is full, keep the newest event and drop the oldest undrained
        # notification rather than blocking a worker during error handling.
        try:
            failure_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            failure_queue.put_nowait(failure)
        except queue.Full:
            _LOG.warning(
                "Streaming worker failure queue is full; dropped failure "
                "notification for %s during %s.",
                failure.cell,
                failure.stage,
            )

    def _record_worker_failure(
        self,
        cell: tuple[int, int, int],
        stage: str,
        exc: Exception,
        *,
        fatal: bool,
    ) -> StreamingWorkerFailure:
        failure = StreamingWorkerFailure(
            cell=cell,
            stage=stage,
            error_type=type(exc).__name__,
            message=str(exc) or repr(exc),
            thread_name=threading.current_thread().name,
            fatal=fatal,
        )
        if fatal:
            with self._lock:
                if not hasattr(self, "_failed_cells"):
                    self._failed_cells = {}
                self._failed_cells[cell] = failure
                self._pending.discard(cell)
        self._publish_worker_failure(failure)
        _LOG.warning(
            "Streaming worker %s failure for cell %s during %s on %s: %s: %s",
            "fatal" if fatal else "non-fatal",
            cell,
            stage,
            failure.thread_name,
            failure.error_type,
            failure.message,
        )
        return failure

    def drain_worker_failures(
        self,
        max_items: int | None = None,
    ) -> list[StreamingWorkerFailure]:
        """Return worker failure notifications without blocking.

        This is the owner-thread propagation point.  Draining notifications
        does not clear failed-cell state; fatal failed cells remain suppressed
        from future load scheduling so a bad chunk cannot churn forever.
        """
        if max_items is not None and max_items < 0:
            raise ValueError("max_items must be non-negative")
        failure_queue = self._ensure_worker_failure_queue()
        failures: list[StreamingWorkerFailure] = []
        while max_items is None or len(failures) < max_items:
            try:
                failures.append(failure_queue.get_nowait())
            except queue.Empty:
                break
        return failures

    def failed_cells(self) -> dict[tuple[int, int, int], StreamingWorkerFailure]:
        """Return a snapshot of cells that hit fatal worker failures."""
        with self._lock:
            return dict(getattr(self, "_failed_cells", {}))

    def _worker_loop(self):
        while not self._stop_event.is_set():
            if self._paused_event.is_set():
                self._wait_while_paused()
                continue
            try:
                cell = self._work_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if cell is None:
                break
            if self._stop_event.is_set():
                self._clear_pending_cell(cell)
                break

            # Pause may have been requested while this worker was blocked
            # in queue.get(); put the item back so minimize mode really
            # stops disk/cache work.
            if self._paused_event.is_set():
                try:
                    self._work_queue.put_nowait(cell)
                except queue.Full:
                    # Another thread filled the queue after this worker took
                    # the item.  Do not block while paused/shutting down; make
                    # the cell schedulable again when update() reconciles.
                    self._clear_pending_cell(cell)
                self._wait_while_paused()
                continue

            handed_off = False
            stage = "load_chunk_file"
            try:
                if not self._cell_is_wanted(cell):
                    continue
                chunk_file_max_group_bytes = getattr(
                    self,
                    "_chunk_file_max_group_bytes",
                    None,
                )
                if chunk_file_max_group_bytes is None:
                    data = chunker.load_chunk_file(self.cache_dir, cell)
                else:
                    data = chunker.load_chunk_file(
                        self.cache_dir,
                        cell,
                        max_group_bytes=chunk_file_max_group_bytes,
                    )
                if self._stop_event.is_set() or not self._cell_is_wanted(cell):
                    continue
                stage = "prepare_chunk_upload_groups"
                chunker.prepare_chunk_upload_groups(data)
                if self._stop_event.is_set() or not self._cell_is_wanted(cell):
                    continue
                prepack_smooth_shading = self._prepack_smooth_shading_snapshot()
                if prepack_smooth_shading is not None:
                    try:
                        stage = "prepack_chunk_vertex_bytes"
                        chunker.prepack_chunk_vertex_bytes(
                            data,
                            smooth_shading=prepack_smooth_shading,
                        )
                    except Exception as e:
                        # Prepacking is an optimization. If it fails, keep the
                        # chunk stream correct and let the render thread fall
                        # back to the existing on-demand pack path.
                        self._record_worker_failure(
                            cell,
                            "prepack_chunk_vertex_bytes",
                            e,
                            fatal=False,
                        )
                if self._stop_event.is_set() or not self._cell_is_wanted(cell):
                    continue
                on_decode_textures = getattr(self, "_on_decode_textures", None)
                if on_decode_textures is not None:
                    try:
                        stage = "on_decode_textures"
                        on_decode_textures(data)
                    except Exception as e:
                        # texture pre-decode is a best-effort optimization;
                        # a failure here should not block the chunk from
                        # becoming ready -- worst case, acquire() falls back
                        # to a synchronous decode on the main thread later.
                        self._record_worker_failure(
                            cell,
                            "on_decode_textures",
                            e,
                            fatal=False,
                        )
                stage = "ready_backlog"
                while (
                    not self._stop_event.is_set()
                    and self._cell_is_wanted(cell)
                ):
                    try:
                        self._ready_backlog.put(data, timeout=0.2)
                        handed_off = True
                        break
                    except queue.Full:
                        continue
            except FileNotFoundError as e:
                self._record_worker_failure(cell, stage, e, fatal=True)
            except Exception as e:
                # don't crash the worker thread on a single bad chunk file;
                # publish a structured failure and mark the cell terminal.
                self._record_worker_failure(cell, stage, e, fatal=True)
            finally:
                if not handed_off:
                    self._clear_pending_cell(cell)
                else:
                    # The ready payload is still resident, so this probe sees
                    # the real geometry/texture memory cost before pool growth.
                    self._maybe_start_additional_worker()

    def cell_for_position(self, position: np.ndarray) -> tuple[int, int, int]:
        return chunker.world_to_cell(position, self.config.chunk_size)

    def _reprioritize_queued_work(
        self,
        priority_key: CellPriorityKey,
        wanted: set[Cell],
    ) -> None:
        """Reorder queued, not-yet-started worker cells by current priority.

        Pending cells include both queue-resident work and work a worker thread
        has already taken.  Only queue-resident work can be reprioritized safely;
        in-flight work is left alone.
        """
        work_queue = getattr(self, "_work_queue", None)
        if work_queue is None:
            return

        queued: list[Cell] = []
        stale: list[Cell] = []
        sentinels = 0
        while True:
            try:
                cell = work_queue.get_nowait()
            except queue.Empty:
                break
            if cell is None:
                sentinels += 1
            elif cell in wanted:
                queued.append(cell)
            else:
                stale.append(cell)

        if not queued and not stale and sentinels == 0:
            return

        requeued: set[Cell] = set()
        for cell in sorted(queued, key=priority_key):
            try:
                work_queue.put_nowait(cell)
            except queue.Full:
                break
            requeued.add(cell)

        dropped = set(stale) | (set(queued) - requeued)
        if dropped:
            with self._lock:
                self._pending.difference_update(dropped)

        for _ in range(sentinels):
            try:
                work_queue.put_nowait(None)
            except queue.Full:
                break

    def update(
        self,
        camera_position: np.ndarray,
        cell_priority_key: CellPriorityKey | None = None,
    ) -> None:
        """Call once per frame. Cheap if camera hasn't crossed a cell
        boundary AND the load radius hasn't changed since the last call
        (early-outs immediately in that case).

        `cell_priority_key`, when supplied by the owner/render thread, ranks
        candidate cells for dispatch and ready uploads.  This keeps the core
        scheduler independent of camera/rendering APIs while allowing the GUI
        to prioritize cells in the current view over cells that are merely
        closer to the camera but off-screen or behind it.
        """
        if self._paused_event.is_set():
            return

        cam_cell = self.cell_for_position(camera_position)
        current_radius = self.config.load_radius_cells
        priority_key = (
            cell_priority_key
            if cell_priority_key is not None
            else lambda cell: self._cell_distance_sq(cell, cam_cell)
        )
        # Store this before the stationary-view early-out so turning the
        # camera without crossing a cell still changes which ready/deferred
        # chunks get uploaded first.
        self._last_cell_priority_key = priority_key

        # Recompute if the camera moved to a new cell OR the radius itself
        # changed (e.g. the person just dragged a render-distance slider
        # while standing still). Without the radius check, adjusting the
        # slider at a standstill would silently do nothing until the
        # camera happened to cross a cell boundary on its own -- the
        # slider would feel completely broken on first try.
        same_view = (
            cam_cell == self._last_camera_cell
            and current_radius == self._last_load_radius
        )
        if same_view:
            # Worker failures and stale-result cleanup can remove a pending
            # cell asynchronously while the camera remains stationary.
            # Fatal failures are terminal for this StreamingWorld instance,
            # so they count as resolved for scheduling: keeping them outside
            # pending prevents a bad chunk file from being retried every frame.
            with self._lock:
                wanted_snapshot = set(self._last_wanted_cells)
                unresolved = (
                    wanted_snapshot
                    - self.loaded_cells
                    - self._pending
                    - set(getattr(self, "_failed_cells", {}))
                )
            self._reprioritize_queued_work(priority_key, wanted_snapshot)
            if not unresolved:
                return
        self._last_camera_cell = cam_cell
        self._last_load_radius = current_radius

        load_r = self.config.load_radius_cells
        wanted = select_wanted_cells(
            self.available_cells,
            cam_cell,
            load_r,
            self.config.max_loaded_chunks,
        )
        self._cancel_queued_unloads(wanted)
        with self._lock:
            self._last_wanted_cells = wanted
            to_request = tuple(
                wanted
                - self.loaded_cells
                - self._pending
                - set(getattr(self, "_failed_cells", {}))
            )
        # Sort outside StreamingWorld's internal lock.  The priority key is
        # supplied by the owner layer and must not run while internal state is
        # locked.
        ordered = sorted(to_request, key=priority_key)
        with self._lock:
            for cell in ordered:
                if cell not in self._last_wanted_cells:
                    continue
                if (
                    cell in self.loaded_cells
                    or cell in self._pending
                    or cell in getattr(self, "_failed_cells", {})
                ):
                    continue
                try:
                    self._work_queue.put_nowait(cell)
                except queue.Full:
                    break
                self._pending.add(cell)
        self._reprioritize_queued_work(priority_key, wanted)

        stale_ready = self._ready_backlog.discard_if(
            lambda data: data.cell not in wanted
        )
        partial_ready = getattr(self, "_partial_ready", [])
        stale_partial_ready = [
            data
            for data in partial_ready
            if data.cell not in wanted
        ]
        if stale_partial_ready:
            self._partial_ready = [
                data
                for data in partial_ready
                if data.cell in wanted
            ]

        stale_partial_cells = set()
        if stale_ready or stale_partial_ready:
            with self._lock:
                for data in stale_ready + stale_partial_ready:
                    self._pending.discard(data.cell)
            if stale_partial_ready:
                stale_partial_cells = {
                    data.cell
                    for data in stale_partial_ready
                }

        # eviction uses a larger radius than load, so a chunk isn't dropped
        # the instant it's outside the tight load ring -- avoids reload
        # thrashing if the camera oscillates near a boundary.
        unload_r = self.config.unload_radius_cells
        cells_to_unload = cells_outside_cube_radius(
            self.loaded_cells, cam_cell, unload_r
        )
        self._cells_to_unload_next_drain = cells_to_unload | stale_partial_cells
        self._last_cam_cell_for_priority = cam_cell

    def _ensure_unload_backlog(
        self,
    ) -> tuple[list[tuple[tuple[int, int, int], int]], set[tuple[int, int, int]]]:
        if not hasattr(self, "_unload_backlog"):
            self._unload_backlog = []
        if not hasattr(self, "_unload_backlog_cells"):
            self._unload_backlog_cells = {
                cell for cell, _remaining_frames in self._unload_backlog
            }
        return self._unload_backlog, self._unload_backlog_cells

    def _cancel_queued_unloads(self, wanted: set[tuple[int, int, int]]) -> None:
        backlog, backlog_cells = self._ensure_unload_backlog()
        if not backlog:
            return
        with self._lock:
            loaded_wanted = self.loaded_cells & wanted
        if not loaded_wanted:
            return
        retained = []
        for cell, remaining_frames in backlog:
            if cell in loaded_wanted:
                backlog_cells.discard(cell)
            else:
                retained.append((cell, remaining_frames))
        backlog[:] = retained

    def _queue_cells_for_unload(
        self,
        cells: Iterable[tuple[int, int, int]],
        *,
        retire_frames: int | None = None,
    ) -> None:
        cells_to_queue = tuple(cells)
        if not cells_to_queue:
            return
        backlog, backlog_cells = self._ensure_unload_backlog()
        if retire_frames is None:
            retire_frames = max(
                0,
                int(getattr(self.config, "unload_retire_frames", 2)),
            )
        with self._lock:
            loaded_wanted = self.loaded_cells & self._last_wanted_cells
        for cell in sorted(cells_to_queue):
            if cell in backlog_cells:
                continue
            if cell in loaded_wanted:
                continue
            backlog.append((cell, retire_frames))
            backlog_cells.add(cell)

    def _drain_unload_backlog(
        self,
        on_chunk_unload: Callable[[tuple], None],
    ) -> None:
        backlog, backlog_cells = self._ensure_unload_backlog()
        if not backlog:
            return

        max_unloads = max(
            1,
            int(getattr(self.config, "unload_chunks_per_frame", 1)),
        )
        time_budget_ms = max(
            0.0,
            float(getattr(self.config, "unload_time_budget_ms", 1.0)),
        )

        aged_backlog = []
        for cell, remaining_frames in backlog:
            aged_backlog.append((cell, max(0, remaining_frames - 1)))
        backlog[:] = aged_backlog

        start = time.perf_counter()
        released = 0
        retained = []
        for cell, remaining_frames in backlog:
            if remaining_frames > 0:
                retained.append((cell, remaining_frames))
                continue
            if released >= max_unloads:
                retained.append((cell, remaining_frames))
                continue
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if released > 0 and elapsed_ms >= time_budget_ms:
                retained.append((cell, remaining_frames))
                continue

            with self._lock:
                if cell in self.loaded_cells and cell in self._last_wanted_cells:
                    backlog_cells.discard(cell)
                    continue
                was_loaded = cell in self.loaded_cells
            on_chunk_unload(cell)
            if was_loaded:
                with self._lock:
                    self.loaded_cells.discard(cell)
            backlog_cells.discard(cell)
            released += 1
        backlog[:] = retained

    def _cell_distance_sq(self, cell: tuple[int, int, int], center: tuple[int, int, int]) -> int:
        return cell_distance_sq(cell, center)

    def _available_cells_in_radius(self, center: tuple[int, int, int], radius: int) -> set[tuple[int, int, int]]:
        return select_wanted_cells(
            self.available_cells,
            center,
            radius,
            max_loaded_chunks=len(self.available_cells),
        )

    @staticmethod
    def _cell_in_cube_radius(cell: tuple[int, int, int], center: tuple[int, int, int], radius: int) -> bool:
        return cell_in_cube_radius(cell, center, radius)

    def drain_ready_chunks(self, on_chunk_ready: Callable[[ChunkData], object],
                             on_chunk_unload: Callable[[tuple], None],
                             max_per_frame: int = 4,
                             time_budget_ms: float = 4.0) -> None:
        """
        Call once per frame on the render/main thread. Pulls finished
        background loads and hands them to `on_chunk_ready` (where the
        caller uploads to GPU -- the only part of this that has to happen
        on the main thread, since OpenGL calls aren't valid off-thread).

        Two throttles apply together:
          - `max_per_frame`: hard cap on number of chunks uploaded this call,
            as a simple worst-case backstop.
          - `time_budget_ms`: stops uploading once this many milliseconds
            have been spent in this call, even if under max_per_frame and
            even if more chunks are ready. This matters because individual
            chunks vary a lot in cost (a chunk needing a fresh texture
            decode is much more expensive than one reusing an already-
            resident texture) -- a fixed *count* can still spike frame time
            if several expensive chunks land in the same frame. The time
            budget catches that case; the count cap is just a backstop.

        Each chunk is selected by the latest owner-supplied cell priority when
        available, falling back to distance from the camera's last known cell.
        This lets the render owner upload camera-visible chunks before
        off-screen chunks without putting camera or OpenGL policy in core.
        Deferred chunks remain in the same bounded backlog for a later frame.

        `on_chunk_ready` may return False when it performed only a partial
        render-thread upload. In that case the chunk remains pending and is
        resumed on a later drain call instead of being marked loaded/drawable.
        """
        if self._paused_event.is_set():
            return
        if not hasattr(self, "_partial_ready"):
            self._partial_ready = []

        cell_priority_key = getattr(self, "_last_cell_priority_key", None)
        if cell_priority_key is None:
            cam_cell = getattr(self, "_last_cam_cell_for_priority", None)
            if cam_cell is not None:
                cell_priority_key = lambda cell: self._cell_distance_sq(cell, cam_cell)
        ready_priority_key = (
            None
            if cell_priority_key is None
            else lambda data: cell_priority_key(data.cell)
        )

        start = time.perf_counter()
        n = 0
        unload_now = getattr(self, "_cells_to_unload_next_drain", set())
        if unload_now:
            self._queue_cells_for_unload(unload_now)
            self._cells_to_unload_next_drain = set()
        self._drain_unload_backlog(on_chunk_unload)

        while n < max_per_frame:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if elapsed_ms >= time_budget_ms:
                break
            partial_ready = getattr(self, "_partial_ready", [])
            if partial_ready:
                if ready_priority_key is None:
                    data = partial_ready.pop(0)
                else:
                    item_index = min(
                        range(len(partial_ready)),
                        key=lambda index: ready_priority_key(partial_ready[index]),
                    )
                    data = partial_ready.pop(item_index)
            else:
                try:
                    data = self._ready_backlog.get_closest_nowait(ready_priority_key)
                except queue.Empty:
                    break
            with self._lock:
                is_wanted = data.cell in self._last_wanted_cells
                if not is_wanted:
                    self._pending.discard(data.cell)
            if not is_wanted:
                continue
            try:
                upload_complete = on_chunk_ready(data)
            except BaseException:
                self._clear_pending_cell(data.cell)
                raise
            if upload_complete is False:
                self._partial_ready.append(data)
                n += 1
                continue
            with self._lock:
                self._pending.discard(data.cell)
                self.loaded_cells.add(data.cell)
            n += 1

        # Memory residency cap: if loaded chunk count exceeds budget, evict
        # farthest chunks first (prefer those outside the immediate wanted set).
        # Actual GL release is queued so many deletions cannot bunch into one
        # render frame; loaded_cells is updated at the callback transaction.
        with self._lock:
            evicted_cells = select_evictions(
                self.loaded_cells,
                self._last_wanted_cells,
                getattr(self, "_last_cam_cell_for_priority", None),
                self.config.max_loaded_chunks,
            )
        self._queue_cells_for_unload(evicted_cells)

    def stats(self) -> dict:
        with self._lock:
            loaded_count = len(self.loaded_cells)
            loaded_wanted_count = len(self.loaded_cells & self._last_wanted_cells)
            pending_count = len(self._pending)
            ready_count = (
                self._ready_backlog.qsize()
                + len(getattr(self, "_partial_ready", []))
            )
            unload_pending_count = len(getattr(self, "_unload_backlog", []))
            wanted_count = len(self._last_wanted_cells)
            failed_cells = getattr(self, "_failed_cells", {})
            failed_count = len(failed_cells)
            failed_wanted_count = len(self._last_wanted_cells & set(failed_cells))
        return {
            "loaded": loaded_count,
            "loaded_wanted": loaded_wanted_count,
            "pending": pending_count,
            "ready": ready_count,
            "unload_pending": unload_pending_count,
            "wanted": wanted_count,
            "failed": failed_count,
            "failed_wanted": failed_wanted_count,
            "total_available": len(self.available_cells),
        }
