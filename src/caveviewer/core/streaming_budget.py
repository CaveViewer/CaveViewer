"""Pure chunk-memory estimation and streaming residency policy."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ResidencyBudget:
    max_loaded_chunks: int
    ram_budget_chunks: int
    gpu_budget_chunks: int | None


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
    ram_target_fraction: float,
    estimated_chunk_ram_bytes: int,
    total_gpu_memory_bytes: int | None,
    gpu_target_fraction: float,
    estimated_chunk_gpu_bytes: int,
) -> ResidencyBudget:
    """Calculate RAM/GPU chunk caps without mutating runtime configuration."""
    if estimated_chunk_ram_bytes <= 0:
        raise ValueError("estimated chunk RAM bytes must be positive")

    ram_budget_bytes = int(total_ram_bytes * ram_target_fraction)
    ram_budget_chunks = max(1, ram_budget_bytes // estimated_chunk_ram_bytes)
    max_loaded_chunks = ram_budget_chunks

    gpu_budget_chunks = None
    if total_gpu_memory_bytes is not None and estimated_chunk_gpu_bytes > 0:
        gpu_budget_bytes = int(total_gpu_memory_bytes * gpu_target_fraction)
        gpu_budget_chunks = max(
            1, gpu_budget_bytes // estimated_chunk_gpu_bytes
        )
        max_loaded_chunks = min(max_loaded_chunks, gpu_budget_chunks)

    max_loaded_chunks = min(max_loaded_chunks, max(0, available_cell_count))
    return ResidencyBudget(
        max_loaded_chunks=int(max_loaded_chunks),
        ram_budget_chunks=int(ram_budget_chunks),
        gpu_budget_chunks=(
            int(gpu_budget_chunks) if gpu_budget_chunks is not None else None
        ),
    )
