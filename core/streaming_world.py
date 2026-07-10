"""
core/streaming_world.py

Runtime chunk streaming: watches the camera's world position and keeps
only a radius of chunks around it loaded into GPU memory, uploading newly
needed chunks and evicting ones that fall out of range. This is the actual
mechanism that prevents lag on big maps -- the renderer never sees more
geometry/textures than fit within `load_radius_cells` of the camera,
regardless of how large the full cave map is.

This module is GPU-API-agnostic: it deals in ChunkData (CPU-side numpy
arrays) and calls back into caller-supplied upload/evict functions so the
moderngl-specific VBO/texture code lives in gui/viewer_window.py, not here.
This keeps the streaming *logic* unit-testable without an OpenGL context
(see the test suite -- we verify load/unload behavior with fake GPU hooks).
"""

from __future__ import annotations

import heapq
import os
from pathlib import Path
import subprocess
import sys
import threading
import queue
import time
import ctypes
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from core import chunker
from core.worker_config import resolve_worker_count
from core.chunker import ChunkData
from core.logging_utils import get_logger


_LOG = get_logger("StreamingWorld")

_AMD_PCI_VENDOR_ID = 0x1002
_LINUX_DRM_ROOT = "/sys/class/drm"


def _detect_total_ram_bytes() -> int:
    """Best-effort total physical RAM detection without extra dependencies."""
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys)
            return 8 * 1024 * 1024 * 1024

        if hasattr(os, "sysconf"):
            page_size = os.sysconf("SC_PAGE_SIZE")
            pages = os.sysconf("SC_PHYS_PAGES")
            if isinstance(page_size, int) and isinstance(pages, int) and page_size > 0 and pages > 0:
                return int(page_size * pages)
    except Exception:
        return 8 * 1024 * 1024 * 1024

    return 8 * 1024 * 1024 * 1024


def _parse_target_fraction(raw_value: str | None, conservative_default: float) -> float:
    """
    Parse memory target from env.

    Accepts either fraction (0.10) or percent-style (10, 25, 40).
    Returns a conservative default when unset/invalid.

    This value is interpreted as a share of TOTAL physical RAM
    (best-effort detected), not currently free RAM.
    """
    if raw_value is None:
        return conservative_default

    text = raw_value.strip()
    if not text:
        return conservative_default

    try:
        value = float(text)
    except ValueError:
        return conservative_default

    if value > 1.0:
        value = value / 100.0

    # Guardrails: allow explicit tuning but keep pathological values out.
    return max(0.01, min(0.80, value))


def _parse_memory_target_fraction(raw_value: str | None) -> float:
    """
    Parse system-RAM residency target from env.

    Accepts either fraction (0.10) or percent-style (10, 25, 40).
    Returns a conservative default when unset/invalid.
    """
    return _parse_target_fraction(raw_value, conservative_default=0.08)


def _parse_gpu_target_fraction(raw_value: str | None) -> float:
    """
    Parse GPU-memory residency target from env.

    Defaults higher than the RAM target because this only applies when a
    dedicated GPU memory size can be detected or explicitly configured.
    """
    return _parse_target_fraction(raw_value, conservative_default=0.70)


def _read_positive_sysfs_int(path: Path) -> int | None:
    """Read a positive decimal/hex integer from sysfs, or return None."""
    try:
        value = int(path.read_text(encoding="ascii").strip(), 0)
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def _detect_linux_amd_gpu_memory_bytes(
    drm_root: str | os.PathLike[str] = _LINUX_DRM_ROOT,
) -> int | None:
    """Detect AMD VRAM through the Linux DRM/amdgpu sysfs interface.

    amdgpu exposes ``mem_info_vram_total`` in bytes for each DRM card.  Prefer
    the boot/primary adapter when multiple AMD cards are present; if none is
    marked primary, use the largest valid AMD VRAM value.  Connector entries
    such as ``card1-eDP-1`` are deliberately ignored.
    """
    root = Path(drm_root)
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return None

    candidates: list[tuple[bool, int]] = []
    for card_path in entries:
        if not card_path.name.startswith("card"):
            continue
        card_index = card_path.name.removeprefix("card")
        if not card_index.isdigit():
            continue

        device_path = card_path / "device"
        vendor_id = _read_positive_sysfs_int(device_path / "vendor")
        if vendor_id != _AMD_PCI_VENDOR_ID:
            continue

        vram_bytes = _read_positive_sysfs_int(
            device_path / "mem_info_vram_total"
        )
        if vram_bytes is None:
            continue

        is_boot_gpu = _read_positive_sysfs_int(device_path / "boot_vga") == 1
        candidates.append((is_boot_gpu, vram_bytes))

    if not candidates:
        return None

    # A primary adapter is more likely to own the OpenGL context.  The memory
    # size breaks ties and provides a useful fallback on systems whose driver
    # does not expose boot_vga.
    _is_boot_gpu, vram_bytes = max(
        candidates, key=lambda candidate: (candidate[0], candidate[1])
    )
    return vram_bytes


def _detect_nvidia_gpu_memory_bytes() -> int | None:
    """Detect NVIDIA VRAM with nvidia-smi when it is available."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        first_line = result.stdout.strip().splitlines()[0].strip()
        total_mb = int(first_line.split()[0])
        if total_mb > 0:
            return total_mb * 1024 * 1024
    except Exception:
        pass
    return None


def _detect_total_gpu_memory_bytes(gpu_vendor: str | None = None) -> int | None:
    """Best-effort dedicated GPU memory detection.

    CAVEVIEWER_GPU_MEMORY_GB is an explicit override for systems where
    automatic detection is unavailable. NVIDIA uses nvidia-smi when present;
    AMD GPUs on Linux use the amdgpu DRM sysfs interface.  When the active
    OpenGL vendor is known, only its matching detector runs so a secondary
    GPU cannot accidentally determine the primary GPU's memory budget.
    """
    override_gb = os.environ.get("CAVEVIEWER_GPU_MEMORY_GB", "").strip()
    if override_gb:
        try:
            value = float(override_gb)
            if value > 0.0:
                memory_bytes = int(value * (1024 ** 3))
                _LOG.info(
                    "Using configured GPU memory override: %.1f GB.",
                    memory_bytes / (1024 ** 3),
                )
                return memory_bytes
        except ValueError:
            pass

    normalized_vendor = (gpu_vendor or "").strip().casefold()
    vendor_is_nvidia = "nvidia" in normalized_vendor
    vendor_is_amd = (
        "amd" in normalized_vendor
        or "advanced micro devices" in normalized_vendor
        or normalized_vendor.startswith("ati ")
    )

    # Preserve the historical NVIDIA-first fallback when no OpenGL vendor is
    # available.  If the context identified its vendor, avoid consulting a
    # different adapter that merely happens to be installed in the machine.
    if vendor_is_nvidia or not normalized_vendor:
        memory_bytes = _detect_nvidia_gpu_memory_bytes()
        if memory_bytes is not None:
            _LOG.info(
                "Detected NVIDIA GPU memory via nvidia-smi: %.1f GB.",
                memory_bytes / (1024 ** 3),
            )
            return memory_bytes

    if sys.platform.startswith("linux") and (vendor_is_amd or not normalized_vendor):
        memory_bytes = _detect_linux_amd_gpu_memory_bytes()
        if memory_bytes is not None:
            _LOG.info(
                "Detected AMD GPU memory via Linux DRM sysfs: %.1f GB.",
                memory_bytes / (1024 ** 3),
            )
            return memory_bytes

    return None


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
                 gpu_vendor: str | None = None):
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
        self._total_gpu_memory_bytes = _detect_total_gpu_memory_bytes(gpu_vendor)
        self._estimated_chunk_gpu_bytes = self._estimate_chunk_gpu_bytes(sampled_chunk_keys)
        self._configure_chunk_budget_from_memory_targets()

        self.loaded_cells: set[tuple[int, int, int]] = set()
        self._pending: set[tuple[int, int, int]] = set()
        # Cap how many fully-decoded chunk payloads can wait in RAM.
        # This bounds worst-case worker-ahead memory spikes when the
        # render thread is temporarily slower than background decoding.
        ready_queue_capacity = max(16, min(256, self.config.max_loaded_chunks))
        self._ready_queue: "queue.Queue[ChunkData]" = queue.Queue(maxsize=ready_queue_capacity)
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
        self._workers = [
            threading.Thread(target=self._worker_loop, daemon=True)
            for _ in range(self._worker_pool_size)
        ]
        for w in self._workers:
            w.start()

        self._last_camera_cell: Optional[tuple[int, int, int]] = None
        self._last_load_radius: Optional[int] = None

    def _estimate_chunk_ram_bytes(self, chunk_keys: list[str]) -> int:
        """Estimate in-RAM cost per loaded chunk from cache chunk file sizes."""
        chunks_dir = os.path.join(self.cache_dir, chunker.CHUNKS_DIRNAME)
        if not chunk_keys:
            return 2 * 1024 * 1024

        sampled_sizes: list[int] = []
        for cell_str in chunk_keys:
            path = os.path.join(chunks_dir, f"{cell_str}.bin")
            try:
                size = os.path.getsize(path)
                if size > 0:
                    sampled_sizes.append(size)
            except OSError:
                continue

        if not sampled_sizes:
            return 2 * 1024 * 1024

        sampled_sizes.sort()
        median_size = sampled_sizes[len(sampled_sizes) // 2]
        # Keep conservative headroom for numpy arrays, Python object overhead,
        # and GPU-side residency associated with a loaded chunk.
        overhead_multiplier = 6.0
        return max(int(median_size * overhead_multiplier), 512 * 1024)

    def _estimate_chunk_gpu_bytes(self, chunk_keys: list[str]) -> int:
        """Estimate GPU-resident cost per loaded chunk from cache chunk sizes."""
        chunks_dir = os.path.join(self.cache_dir, chunker.CHUNKS_DIRNAME)
        if not chunk_keys:
            return 2 * 1024 * 1024

        sampled_sizes: list[int] = []
        for cell_str in chunk_keys:
            path = os.path.join(chunks_dir, f"{cell_str}.bin")
            try:
                size = os.path.getsize(path)
                if size > 0:
                    sampled_sizes.append(size)
            except OSError:
                continue

        if not sampled_sizes:
            return 2 * 1024 * 1024

        sampled_sizes.sort()
        median_size = sampled_sizes[len(sampled_sizes) // 2]
        # A chunk's VBO is roughly the position/uv/normal payload size.
        # Use headroom for driver allocation overhead and texture residency
        # shared across loaded chunks.
        overhead_multiplier = 2.5
        return max(int(median_size * overhead_multiplier), 512 * 1024)

    def _configure_chunk_budget_from_memory_targets(self) -> None:
        """Derive max_loaded_chunks from system RAM and GPU memory targets.

        Important: this is a policy cap for chunk residency, not a strict
        memory reservation. Actual process memory can differ due to Python
        object overhead, decode/transient buffers, GPU buffers/textures,
        driver usage, and whatever else is running on the machine.
        """
        if self._estimated_chunk_ram_bytes <= 0:
            return

        ram_budget_bytes = int(self._total_ram_bytes * self._memory_target_fraction)
        ram_budget_chunks = max(1, ram_budget_bytes // self._estimated_chunk_ram_bytes)
        budget_chunks = ram_budget_chunks

        gpu_budget_chunks = None
        if self._total_gpu_memory_bytes is not None and self._estimated_chunk_gpu_bytes > 0:
            gpu_budget_bytes = int(self._total_gpu_memory_bytes * self._gpu_target_fraction)
            gpu_budget_chunks = max(1, gpu_budget_bytes // self._estimated_chunk_gpu_bytes)
            budget_chunks = min(budget_chunks, gpu_budget_chunks)

        budget_chunks = min(budget_chunks, len(self.available_cells))

        # Apply the memory-derived budget directly so env tuning can both
        # raise and lower residency as intended.
        self.config.max_loaded_chunks = int(budget_chunks)
        _LOG.info(
            "Memory target %.0f%% of %.1f GB => max_loaded_chunks=%d (estimated %.1f MB/chunk)",
            self._memory_target_fraction * 100.0,
            self._total_ram_bytes / (1024 ** 3),
            ram_budget_chunks,
            self._estimated_chunk_ram_bytes / (1024 ** 2),
        )
        if gpu_budget_chunks is not None:
            _LOG.info(
                "GPU memory target %.0f%% of %.1f GB => max_loaded_chunks=%d (estimated %.1f MB/chunk)",
                self._gpu_target_fraction * 100.0,
                self._total_gpu_memory_bytes / (1024 ** 3),
                gpu_budget_chunks,
                self._estimated_chunk_gpu_bytes / (1024 ** 2),
            )
            _LOG.info("Effective max_loaded_chunks=%d after RAM/GPU limits.", self.config.max_loaded_chunks)
        else:
            _LOG.info(
                "GPU memory limit not applied; automatic detection was unavailable. "
                "Set CAVEVIEWER_GPU_MEMORY_GB to provide an explicit value."
            )

    def shutdown(self):
        self._stop_event.set()
        for _ in self._workers:
            self._work_queue.put(None)  # sentinel to unblock get()
        for w in self._workers:
            w.join(timeout=2.0)

    def pause(self):
        self._paused_event.set()

    def resume(self):
        self._paused_event.clear()

    def is_paused(self) -> bool:
        return self._paused_event.is_set()

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

            try:
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
                while not self._stop_event.is_set():
                    try:
                        self._ready_queue.put(data, timeout=0.2)
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
        if cam_cell == self._last_camera_cell and current_radius == self._last_load_radius:
            return
        self._last_camera_cell = cam_cell
        self._last_load_radius = current_radius

        load_r = self.config.load_radius_cells
        wanted = self._available_cells_in_radius(cam_cell, load_r)
        if len(wanted) > self.config.max_loaded_chunks:
            ordered_wanted = sorted(
                wanted,
                key=lambda cell: self._cell_distance_sq(cell, cam_cell),
            )
            wanted = set(ordered_wanted[:max(1, self.config.max_loaded_chunks)])
        self._last_wanted_cells = wanted

        with self._lock:
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

        # eviction uses a larger radius than load, so a chunk isn't dropped
        # the instant it's outside the tight load ring -- avoids reload
        # thrashing if the camera oscillates near a boundary.
        unload_r = self.config.unload_radius_cells
        self._cells_to_unload_next_drain = {
            cell
            for cell in self.loaded_cells
            if not self._cell_in_cube_radius(cell, cam_cell, unload_r)
        }
        self._last_cam_cell_for_priority = cam_cell

    def _cell_distance_sq(self, cell: tuple[int, int, int], center: tuple[int, int, int]) -> int:
        return (cell[0] - center[0]) ** 2 + (cell[1] - center[1]) ** 2 + (cell[2] - center[2]) ** 2

    def _available_cells_in_radius(self, center: tuple[int, int, int], radius: int) -> set[tuple[int, int, int]]:
        cx, cy, cz = center
        available = self.available_cells
        wanted: set[tuple[int, int, int]] = set()
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    cell = (cx + dx, cy + dy, cz + dz)
                    if cell in available:
                        wanted.add(cell)
        return wanted

    @staticmethod
    def _cell_in_cube_radius(cell: tuple[int, int, int], center: tuple[int, int, int], radius: int) -> bool:
        return (
            abs(cell[0] - center[0]) <= radius
            and abs(cell[1] - center[1]) <= radius
            and abs(cell[2] - center[2]) <= radius
        )

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

        Ready chunks are also re-sorted by distance to the camera's last
        known cell before draining, so if multiple chunks became ready
        between frames, the ones closest to the camera (most likely to
        cause a visible hole if delayed) are uploaded first.
        """
        if self._paused_event.is_set():
            return

        # Drain everything currently sitting in the ready queue into a list
        # so we can sort by distance before uploading any of it -- this is
        # cheap (CPU-side list ops on ChunkData references, no GPU work yet).
        pending_ready = []
        while True:
            try:
                pending_ready.append(self._ready_queue.get_nowait())
            except queue.Empty:
                break

        cam_cell = getattr(self, "_last_cam_cell_for_priority", None)
        if cam_cell is not None and pending_ready:
            pending_ready.sort(key=lambda d: self._cell_distance_sq(d.cell, cam_cell))

        start = time.perf_counter()
        n = 0
        leftover = []
        for data in pending_ready:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if n >= max_per_frame or elapsed_ms >= time_budget_ms:
                leftover.append(data)
                continue
            with self._lock:
                self._pending.discard(data.cell)
                self.loaded_cells.add(data.cell)
            on_chunk_ready(data)
            n += 1

        # anything we didn't get to this frame goes back in the queue,
        # still in priority order, so the next frame picks up where this
        # one left off rather than re-sorting from scratch each time
        for data in leftover:
            self._ready_queue.put(data)

        unload_now = getattr(self, "_cells_to_unload_next_drain", set())
        if unload_now:
            for cell in list(unload_now):
                with self._lock:
                    self.loaded_cells.discard(cell)
                on_chunk_unload(cell)
            self._cells_to_unload_next_drain = set()

        # Hard memory residency cap: if loaded chunk count exceeds budget,
        # evict farthest chunks first (prefer those outside the immediate
        # wanted set) until within max_loaded_chunks.
        with self._lock:
            loaded_count = len(self.loaded_cells)
            # Never evict below the current wanted set; that causes chunk
            # thrash and repeated reloading while the camera is stationary.
            effective_cap = max(self.config.max_loaded_chunks, len(self._last_wanted_cells))
            over_budget = loaded_count - effective_cap
            if over_budget > 0:
                cam_cell = getattr(self, "_last_cam_cell_for_priority", None)
                if cam_cell is not None:
                    preferred = heapq.nlargest(
                        over_budget,
                        (c for c in self.loaded_cells if c not in self._last_wanted_cells),
                        key=lambda c: self._cell_distance_sq(c, cam_cell),
                    )
                    remaining = over_budget - len(preferred)
                    if remaining > 0:
                        fallback = heapq.nlargest(
                            remaining,
                            (c for c in self.loaded_cells if c in self._last_wanted_cells),
                            key=lambda c: self._cell_distance_sq(c, cam_cell),
                        )
                        to_evict = preferred + fallback
                    else:
                        to_evict = preferred
                else:
                    to_evict = list(self.loaded_cells)[:over_budget]
                for cell in to_evict:
                    self.loaded_cells.discard(cell)

                evicted_cells = to_evict
            else:
                evicted_cells = []

        for cell in evicted_cells:
            on_chunk_unload(cell)

    def stats(self) -> dict:
        return {
            "loaded": len(self.loaded_cells),
            "pending": len(self._pending),
            "ready": self._ready_queue.qsize(),
            "wanted": len(self._last_wanted_cells),
            "total_available": len(self.available_cells),
        }
