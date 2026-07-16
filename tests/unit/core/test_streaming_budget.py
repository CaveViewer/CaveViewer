"""Check chunk-memory estimates and RAM/GPU residency budget calculations."""

from __future__ import annotations

import pytest

from caveviewer.core.streaming_budget import (
    calculate_residency_budget,
    estimate_chunk_bytes,
)


def test_chunk_estimate_uses_median_file_size_and_overhead(tmp_path):
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    sizes = {"0_0_0": 100_000, "1_0_0": 200_000, "2_0_0": 300_000}
    for key, size in sizes.items():
        (chunks_dir / f"{key}.bin").write_bytes(b"x" * size)

    estimate = estimate_chunk_bytes(
        str(tmp_path),
        list(sizes),
        chunks_dirname="chunks",
        overhead_multiplier=6.0,
    )

    assert estimate == 1_200_000


def test_chunk_estimate_uses_conservative_fallback_without_samples(tmp_path):
    assert estimate_chunk_bytes(
        str(tmp_path),
        [],
        chunks_dirname="chunks",
        overhead_multiplier=6.0,
    ) == 2 * 1024 * 1024


def test_gpu_budget_limits_ram_budget():
    budget = calculate_residency_budget(
        available_cell_count=100,
        total_ram_bytes=1_000,
        ram_target_fraction=0.5,
        estimated_chunk_ram_bytes=100,
        total_gpu_memory_bytes=1_000,
        gpu_target_fraction=0.2,
        estimated_chunk_gpu_bytes=100,
    )

    assert budget.ram_budget_chunks == 5
    assert budget.gpu_budget_chunks == 2
    assert budget.max_loaded_chunks == 2


def test_available_cells_cap_ram_only_budget():
    budget = calculate_residency_budget(
        available_cell_count=3,
        total_ram_bytes=1_000,
        ram_target_fraction=0.5,
        estimated_chunk_ram_bytes=100,
        total_gpu_memory_bytes=None,
        gpu_target_fraction=0.7,
        estimated_chunk_gpu_bytes=100,
    )

    assert budget.gpu_budget_chunks is None
    assert budget.max_loaded_chunks == 3


def test_ram_budget_uses_current_available_ram_when_lower():
    budget = calculate_residency_budget(
        available_cell_count=100,
        total_ram_bytes=10_000,
        available_ram_bytes=1_000,
        ram_target_fraction=0.5,
        estimated_chunk_ram_bytes=100,
        total_gpu_memory_bytes=None,
        gpu_target_fraction=0.7,
        estimated_chunk_gpu_bytes=100,
    )

    assert budget.ram_budget_chunks == 5
    assert budget.max_loaded_chunks == 5


def test_budget_rejects_non_positive_ram_estimate():
    with pytest.raises(ValueError, match="must be positive"):
        calculate_residency_budget(
            available_cell_count=3,
            total_ram_bytes=1_000,
            ram_target_fraction=0.5,
            estimated_chunk_ram_bytes=0,
            total_gpu_memory_bytes=None,
            gpu_target_fraction=0.7,
            estimated_chunk_gpu_bytes=100,
        )
