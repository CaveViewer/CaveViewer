"""
caveviewer.core.streaming_world

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
from typing import Callable, Optional

import numpy as np

from caveviewer.core import chunker, hardware_memory
from caveviewer.core.worker_config import (
    MAX_WORKER_RAM_UTILIZATION,
    can_start_additional_worker,
    resolve_worker_count,
)
from caveviewer.core.chunker import ChunkData
from caveviewer.core.logging_utils import get_logger
from caveviewer.core.streaming_budget import (
    calculate_residency_budget,
    estimate_chunk_bytes,
)
from caveviewer.core.streaming_scheduler import (
    BoundedReadyBacklog,
    cell_distance_sq,
    cell_in_cube_radius,
    cells_outside_cube_radius,
    select_evictions,
    select_wanted_cells,
)


_LOG = get_logger("StreamingWorld")

# Compatibility hooks for existing diagnostics/tests that patch these names
# through core.streaming_world rather than core.hardware_memory.
subprocess = hardware_memory.subprocess
sys = hardware_memory.sys

_AMD_PCI_VENDOR_ID = hardware_memory.AMD_PCI_VENDOR_ID
_LINUX_DRM_ROOT = hardware_memory.LINUX_DRM_ROOT


def _detect_total_ram_bytes() -> int:
    return hardware_memory.detect_total_ram_bytes()


def _detect_ram_snapshot() -> hardware_memory.RamSnapshot | None:
    return hardware_memory.detect_ram_snapshot()


def _parse_target_fraction(raw_value: str | None, conservative_default: float) -> float:
    return hardware_memory.parse_target_fraction(
        raw_value, conservative_default
    )


def _parse_memory_target_fraction(raw_value: str | None) -> float:
    return hardware_memory.parse_memory_target_fraction(raw_value)


def _parse_gpu_target_fraction(raw_value: str | None) -> float:
    return hardware_memory.parse_gpu_target_fraction(raw_value)


_read_positive_sysfs_int = hardware_memory.read_positive_sysfs_int


def _detect_linux_amd_gpu_memory_bytes(
    drm_root: str | os.PathLike[str] = _LINUX_DRM_ROOT,
) -> int | None:
    return hardware_memory.detect_linux_amd_gpu_memory_bytes(drm_root)


def _detect_nvidia_gpu_memory_bytes() -> int | None:
    return hardware_memory.detect_nvidia_gpu_memory_bytes()


def _detect_total_gpu_memory_bytes(gpu_vendor: str | None = None) -> int | None:
    return hardware_memory.detect_total_gpu_memory_bytes(
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
    """

    def __init__(self, cache_dir: str, config: StreamingConfig,
                 on_decode_textures: Optional[Callable[[ChunkData], None]] = None,
                 gpu_vendor: str | None = None,
                 textures_dir: str | None = None,
                 total_gpu_memory_bytes: int | None = None):
        """
        on_decode_textures, if given, is called from a background worker
        thread right after a chunk's geometry finishes loading, with the
        ChunkData as the argument. This is the hook used to pre-decode each
        chunk's textures (JPEG decode, pure CPU work, safe off-thread) so
        that by the time the chunk reaches the main thread for GPU upload,
        only the fast/predictable upload step remains there. Keeping this
        as an injected callback rather than importing TextureManager
        directly here preserves this module's GPU-API-agnostic design and
        keeps it unit-testable without any texture/GPU machinery at all.

        gpu_vendor is the active OpenGL context's GL_VENDOR string when the
        caller has one. It prevents a secondary adapter from supplying the
        memory budget on hybrid-GPU systems.

        total_gpu_memory_bytes may be supplied by a caller that already ran
        active-GPU detection for related setup, such as texture resolution
        selection.  When omitted, this class detects it itself.
        """
        self.cache_dir = cache_dir
        self.config = config
        self.on_decode_textures = on_decode_textures
        manifest = chunker.load_manifest(cache_dir) or {"chunks": {}}
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
        self._total_ram_bytes = _detect_total_ram_bytes()
        self._estimated_chunk_ram_bytes = self._estimate_chunk_ram_bytes(sampled_chunk_keys)
        gpu_target_env = os.environ.get("CAVEVIEWER_GPU_MEMORY_UTILIZATION_TARGET")
        self._gpu_target_fraction = _parse_gpu_target_fraction(gpu_target_env)
        self._total_gpu_memory_bytes = (
            total_gpu_memory_bytes
            if total_gpu_memory_bytes is not None
            else _detect_total_gpu_memory_bytes(gpu_vendor)
        )
        self._estimated_chunk_gpu_bytes = self._estimate_chunk_gpu_bytes(sampled_chunk_keys)
        self._gpu_residency_budget_bytes: int | None = None
        self._texture_gpu_bytes: dict[object, int] = {}
        self._configure_texture_gpu_estimates(manifest, textures_dir)
        self._configure_chunk_budget_from_memory_targets()

        self.loaded_cells: set[tuple[int, int, int]] = set()
        self._pending: set[tuple[int, int, int]] = set()
        # Cap how many fully-decoded chunk payloads can wait in RAM.
        # This bounds worst-case worker-ahead memory spikes when the
        # render thread is temporarily slower than background decoding.
        ready_backlog_capacity = max(16, min(256, self.config.max_loaded_chunks))
        self._ready_backlog = _BoundedReadyBacklog(ready_backlog_capacity)
        self._lock = threading.Lock()
        self._worker_pool_size = resolve_worker_count(
            os.environ.get("CAVEVIEWER_IO_WORKERS"),
            os.environ.get("CAVEVIEWER_IO_RESERVED_CPUS"),
            default_workers=2,
            default_reserved_cpus=3,
        )
        self._stop_event = threading.Event()
        self._paused_event = threading.Event()
        self._work_queue: "queue.Queue[tuple[int,int,int]]" = queue.Queue()
        self._worker_start_lock = threading.Lock()
        self._worker_admission_blocked = False
        self._workers: list[threading.Thread] = []
        # The configured count is a maximum. Start one worker unconditionally;
        # completed chunk work will make memory cost observable before each
        # additional worker is admitted.
        with self._worker_start_lock:
            self._start_worker_locked()

        self._last_camera_cell: Optional[tuple[int, int, int]] = None
        self._last_load_radius: Optional[int] = None

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

        self._gpu_residency_budget_bytes = int(
            self._total_gpu_memory_bytes * self._gpu_target_fraction
        )
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
                self._gpu_residency_budget_bytes / (1024 ** 2),
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
                image_context = Image.open(os.path.join(textures_dir, str(file_or_bytes)))
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
            ram_target_fraction=self._memory_target_fraction,
            estimated_chunk_ram_bytes=self._estimated_chunk_ram_bytes,
            total_gpu_memory_bytes=self._total_gpu_memory_bytes,
            gpu_target_fraction=self._gpu_target_fraction,
            estimated_chunk_gpu_bytes=self._estimated_chunk_gpu_bytes,
        )

        # Apply the memory-derived budget directly so env tuning can both
        # raise and lower residency as intended.
        self.config.max_loaded_chunks = budget.max_loaded_chunks
        _LOG.info(
            "Memory target %.0f%% of %.1f GB => max_loaded_chunks=%d (estimated %.1f MB/chunk)",
            self._memory_target_fraction * 100.0,
            self._total_ram_bytes / (1024 ** 3),
            budget.ram_budget_chunks,
            self._estimated_chunk_ram_bytes / (1024 ** 2),
        )
        if budget.gpu_budget_chunks is not None:
            _LOG.info(
                "GPU memory target %.0f%% of %.1f GB => max_loaded_chunks=%d (estimated %.1f MB/chunk)",
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
            daemon=True,
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
                        _LOG.warning(
                            "Could not measure available system RAM; keeping "
                            "streaming at %d worker(s).",
                            len(self._workers),
                        )
                    else:
                        _LOG.warning(
                            "System RAM utilization is %.1f%%; keeping streaming "
                            "at %d worker(s) because the limit is %.0f%%.",
                            snapshot.utilization_fraction * 100.0,
                            len(self._workers),
                            MAX_WORKER_RAM_UTILIZATION * 100.0,
                        )
                self._worker_admission_blocked = True
                return False

            self._start_worker_locked()
            if self._worker_admission_blocked:
                _LOG.info(
                    "System RAM pressure eased; increasing streaming workers "
                    "to %d of %d.",
                    len(self._workers),
                    self._worker_pool_size,
                )
            self._worker_admission_blocked = False
            return True

    def shutdown(self):
        self._stop_event.set()
        worker_start_lock = getattr(self, "_worker_start_lock", None)
        if worker_start_lock is None:
            workers = list(self._workers)
        else:
            with worker_start_lock:
                workers = list(self._workers)
        for _ in workers:
            self._work_queue.put(None)  # sentinel to unblock get()
        for w in workers:
            w.join(timeout=2.0)
        self._ready_backlog.clear()
        with self._lock:
            self._pending.clear()

    def pause(self):
        self._paused_event.set()

    def resume(self):
        self._paused_event.clear()

    def is_paused(self) -> bool:
        return self._paused_event.is_set()

    def _cell_is_wanted(self, cell: tuple[int, int, int]) -> bool:
        with self._lock:
            return cell in self._last_wanted_cells

    def _clear_pending_cell(self, cell: tuple[int, int, int]) -> None:
        with self._lock:
            self._pending.discard(cell)

    def _worker_loop(self):
        while not self._stop_event.is_set():
            if self._paused_event.is_set():
                time.sleep(0.1)
                continue
            try:
                cell = self._work_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if cell is None:
                break

            # Pause may have been requested while this worker was blocked
            # in queue.get(); put the item back so minimize mode really
            # stops disk/cache work.
            if self._paused_event.is_set():
                self._work_queue.put(cell)
                time.sleep(0.1)
                continue

            handed_off = False
            try:
                if not self._cell_is_wanted(cell):
                    continue
                data = chunker.load_chunk_file(self.cache_dir, cell)
                chunker.prepare_chunk_upload_groups(data)
                if self.on_decode_textures is not None:
                    try:
                        self.on_decode_textures(data)
                    except Exception as e:
                        # texture pre-decode is a best-effort optimization;
                        # a failure here should not block the chunk from
                        # becoming ready -- worst case, acquire() falls back
                        # to a synchronous decode on the main thread later.
                        _LOG.warning(f"texture pre-decode failed for {cell}: {e}")
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
            except FileNotFoundError:
                # cell vanished from manifest expectations; ignore safely
                pass
            except Exception as e:
                # don't crash the worker thread on a single bad chunk file;
                # surface via print so it's visible without killing render
                _LOG.warning(f"failed to load chunk {cell}: {e}")
            finally:
                if not handed_off:
                    self._clear_pending_cell(cell)
                else:
                    # The ready payload is still resident, so this probe sees
                    # the real geometry/texture memory cost before pool growth.
                    self._maybe_start_additional_worker()

    def cell_for_position(self, position: np.ndarray) -> tuple[int, int, int]:
        return chunker.world_to_cell(position, self.config.chunk_size)

    def update(self, camera_position: np.ndarray) -> None:
        """Call once per frame. Cheap if camera hasn't crossed a cell
        boundary AND the load radius hasn't changed since the last call
        (early-outs immediately in that case)."""
        if self._paused_event.is_set():
            return

        cam_cell = self.cell_for_position(camera_position)
        current_radius = self.config.load_radius_cells

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
            # cell asynchronously while the camera remains stationary. Only
            # early-out when every wanted cell is still loaded or in flight;
            # otherwise reconcile the same view and enqueue the missing work.
            with self._lock:
                unresolved = (
                    self._last_wanted_cells
                    - self.loaded_cells
                    - self._pending
                )
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
        with self._lock:
            self._last_wanted_cells = wanted
            to_request = wanted - self.loaded_cells - self._pending
            # Dispatch closest-to-camera first. Without this, chunks load in
            # whatever arbitrary order set-iteration and thread scheduling
            # happen to produce -- so a chunk directly ahead of a fast-moving
            # camera (which causes a visible hole if it's late) can finish
            # loading AFTER a chunk behind the camera that doesn't matter yet.
            # Sorting by distance means the chunks the camera will reach
            # soonest are always the ones uploaded soonest.
            ordered = sorted(
                to_request,
                key=lambda cell: self._cell_distance_sq(cell, cam_cell),
            )
            for cell in ordered:
                self._pending.add(cell)
                self._work_queue.put(cell)

        stale_ready = self._ready_backlog.discard_if(
            lambda data: data.cell not in wanted
        )
        if stale_ready:
            with self._lock:
                for data in stale_ready:
                    self._pending.discard(data.cell)

        # eviction uses a larger radius than load, so a chunk isn't dropped
        # the instant it's outside the tight load ring -- avoids reload
        # thrashing if the camera oscillates near a boundary.
        unload_r = self.config.unload_radius_cells
        cells_to_unload = cells_outside_cube_radius(
            self.loaded_cells, cam_cell, unload_r
        )
        self._cells_to_unload_next_drain = cells_to_unload
        self._last_cam_cell_for_priority = cam_cell

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

    def drain_ready_chunks(self, on_chunk_ready: Callable[[ChunkData], None],
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

        Each chunk is selected by distance to the camera's last known cell,
        so if multiple chunks are ready, the one most likely to cause a visible
        hole if delayed is uploaded first. Deferred chunks remain in the same
        bounded backlog for a later frame.
        """
        if self._paused_event.is_set():
            return

        cam_cell = getattr(self, "_last_cam_cell_for_priority", None)
        distance_key = None
        if cam_cell is not None:
            distance_key = lambda data: self._cell_distance_sq(data.cell, cam_cell)

        start = time.perf_counter()
        n = 0
        unload_now = getattr(self, "_cells_to_unload_next_drain", set())
        if unload_now:
            for cell in list(unload_now):
                with self._lock:
                    self.loaded_cells.discard(cell)
                on_chunk_unload(cell)
            self._cells_to_unload_next_drain = set()

        while n < max_per_frame:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if elapsed_ms >= time_budget_ms:
                break
            try:
                data = self._ready_backlog.get_closest_nowait(distance_key)
            except queue.Empty:
                break
            with self._lock:
                is_wanted = data.cell in self._last_wanted_cells
                if not is_wanted:
                    self._pending.discard(data.cell)
            if not is_wanted:
                continue
            try:
                on_chunk_ready(data)
            except BaseException:
                self._clear_pending_cell(data.cell)
                raise
            with self._lock:
                self._pending.discard(data.cell)
                self.loaded_cells.add(data.cell)
            n += 1

        # Hard memory residency cap: if loaded chunk count exceeds budget,
        # evict farthest chunks first (prefer those outside the immediate
        # wanted set) until within max_loaded_chunks.
        with self._lock:
            evicted_cells = select_evictions(
                self.loaded_cells,
                self._last_wanted_cells,
                getattr(self, "_last_cam_cell_for_priority", None),
                self.config.max_loaded_chunks,
            )
            for cell in evicted_cells:
                self.loaded_cells.discard(cell)

        for cell in evicted_cells:
            on_chunk_unload(cell)

    def stats(self) -> dict:
        return {
            "loaded": len(self.loaded_cells),
            "pending": len(self._pending),
            "ready": self._ready_backlog.qsize(),
            "wanted": len(self._last_wanted_cells),
            "total_available": len(self.available_cells),
        }
