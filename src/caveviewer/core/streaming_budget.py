"""Pure chunk-memory estimation and streaming residency policy."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ResidencyBudget:
    max_loaded_chunks: int
    ram_budget_chunks: int
    ready_backlog_chunks: int
    gpu_budget_chunks: int | None
    gpu_budget_bytes: int | None


def estimate_chunk_bytes(
    cache_dir: str,
    chunk_keys: list[str],
    *,
    chunks_dirname: str,
    overhead_multiplier: float,
) -> int:
    """Estimate one chunk's resident bytes from the median cache-file size."""
    if not chunk_keys:
        return 2 * 1024 * 1024

    chunks_dir = os.path.join(cache_dir, chunks_dirname)
    sampled_sizes: list[int] = []
    for cell_str in chunk_keys:
        path = os.path.join(chunks_dir, f"{cell_str}.bin")
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > 0:
            sampled_sizes.append(size)

    if not sampled_sizes:
        return 2 * 1024 * 1024

    sampled_sizes.sort()
    median_size = sampled_sizes[len(sampled_sizes) // 2]
    return max(int(median_size * overhead_multiplier), 512 * 1024)


def calculate_residency_budget(
    *,
    available_cell_count: int,
    total_ram_bytes: int,
    available_ram_bytes: int | None = None,
    ram_target_fraction: float,
    estimated_chunk_ram_bytes: int,
    total_gpu_memory_bytes: int | None,
    gpu_target_fraction: float,
    estimated_chunk_gpu_bytes: int,
    gpu_budget_bytes: int | None = None,
    ready_backlog_target_chunks: int = 16,
) -> ResidencyBudget:
    """Calculate RAM/GPU chunk caps without mutating runtime configuration."""
    if estimated_chunk_ram_bytes <= 0:
        raise ValueError("estimated chunk RAM bytes must be positive")

    ram_base_bytes = total_ram_bytes
    if available_ram_bytes is not None and available_ram_bytes > 0:
        ram_base_bytes = min(total_ram_bytes, available_ram_bytes)
    ram_budget_bytes = int(ram_base_bytes * ram_target_fraction)
    ram_budget_chunks = max(1, ram_budget_bytes // estimated_chunk_ram_bytes)
    ready_backlog_chunks = _calculate_ready_backlog_chunks(
        ram_budget_chunks,
        ready_backlog_target_chunks,
    )
    ram_loaded_chunks = _loaded_chunks_after_ready_reservation(
        ram_budget_chunks,
        ready_backlog_chunks,
    )
    max_loaded_chunks = ram_loaded_chunks

    gpu_budget_chunks = None
    resolved_gpu_budget_bytes = gpu_budget_bytes
    if (
        resolved_gpu_budget_bytes is None
        and total_gpu_memory_bytes is not None
        and estimated_chunk_gpu_bytes > 0
    ):
        resolved_gpu_budget_bytes = int(total_gpu_memory_bytes * gpu_target_fraction)
    if resolved_gpu_budget_bytes is not None and estimated_chunk_gpu_bytes > 0:
        gpu_budget_chunks = max(1, resolved_gpu_budget_bytes // estimated_chunk_gpu_bytes)
        max_loaded_chunks = min(max_loaded_chunks, gpu_budget_chunks)

    max_loaded_chunks = min(max_loaded_chunks, max(0, available_cell_count))
    return ResidencyBudget(
        max_loaded_chunks=int(max_loaded_chunks),
        ram_budget_chunks=int(ram_budget_chunks),
        ready_backlog_chunks=int(ready_backlog_chunks),
        gpu_budget_chunks=(
            int(gpu_budget_chunks) if gpu_budget_chunks is not None else None
        ),
        gpu_budget_bytes=(
            int(resolved_gpu_budget_bytes)
            if resolved_gpu_budget_bytes is not None
            else None
        ),
    )


def _calculate_ready_backlog_chunks(
    ram_budget_chunks: int,
    ready_backlog_target_chunks: int,
) -> int:
    """Reserve RAM-budgeted chunk slots for decoded worker-to-render handoff."""
    ram_budget_chunks = max(1, int(ram_budget_chunks))
    if ram_budget_chunks <= 1:
        return 1
    ready_target = max(1, int(ready_backlog_target_chunks))
    return min(
        ready_target,
        max(1, ram_budget_chunks // 8),
        ram_budget_chunks - 1,
    )


def _loaded_chunks_after_ready_reservation(
    ram_budget_chunks: int,
    ready_backlog_chunks: int,
) -> int:
    """Keep loaded residency and ready backlog from double-using RAM slots."""
    ram_budget_chunks = max(1, int(ram_budget_chunks))
    ready_backlog_chunks = max(0, int(ready_backlog_chunks))
    if ram_budget_chunks <= 1:
        # One visible chunk and one staging slot is the irreducible streaming
        # minimum.  Larger budgets reserve the staging slot from the RAM cap.
        return 1
    return max(1, ram_budget_chunks - ready_backlog_chunks)
