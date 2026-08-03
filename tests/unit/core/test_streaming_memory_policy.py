"""Test pure typed-hardware decisions for streaming residency limits."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    GpuMemoryBudget,
    RamAvailability,
)
from caveviewer.core.streaming.budget import (
    CONSERVATIVE_UNKNOWN_GPU_MEMORY_BYTES,
    CONSERVATIVE_UNKNOWN_RAM_BYTES,
    CONSERVATIVE_UNKNOWN_RAM_TARGET_FRACTION,
    StreamingMemoryMode,
    decide_streaming_memory,
)


GIB = 1024 ** 3


def test_streaming_memory_policy_uses_measured_ram_and_gpu_budgets():
    decision = decide_streaming_memory(
        available_cell_count=100,
        ram_capability=CapabilityResult.available(
            RamAvailability(total_bytes=1_000, available_bytes=1_000),
            reason_code="ram_availability_detected",
        ),
        gpu_capability=CapabilityResult.available(
            GpuMemoryBudget(1_000),
            reason_code="gpu_memory_detected",
        ),
        ram_target_fraction=0.5,
        gpu_target_fraction=0.2,
        estimated_chunk_ram_bytes=100,
        estimated_chunk_gpu_bytes=100,
    )

    assert decision.mode is StreamingMemoryMode.NORMAL
    assert decision.uses_conservative_limits is False
    assert decision.total_ram_bytes == 1_000
    assert decision.available_ram_bytes == 1_000
    assert decision.total_gpu_memory_bytes == 1_000
    assert decision.ram_target_fraction == pytest.approx(0.5)
    assert decision.residency_budget.ram_budget_chunks == 5
    assert decision.residency_budget.gpu_budget_chunks == 2
    assert decision.residency_budget.max_loaded_chunks == 2


def test_streaming_memory_policy_fails_closed_for_unknown_ram():
    decision = decide_streaming_memory(
        available_cell_count=100,
        ram_capability=CapabilityResult.unknown(
            reason_code="ram_availability_unknown",
        ),
        gpu_capability=CapabilityResult.available(
            GpuMemoryBudget(4 * GIB),
            reason_code="gpu_memory_detected",
        ),
        ram_target_fraction=0.80,
        gpu_target_fraction=0.70,
        estimated_chunk_ram_bytes=GIB,
        estimated_chunk_gpu_bytes=GIB,
    )

    assert decision.mode is StreamingMemoryMode.CONSERVATIVE
    assert decision.ram_reason_code == "ram_availability_unknown"
    assert decision.total_ram_bytes == CONSERVATIVE_UNKNOWN_RAM_BYTES
    assert decision.available_ram_bytes == CONSERVATIVE_UNKNOWN_RAM_BYTES
    assert decision.ram_target_fraction == pytest.approx(
        CONSERVATIVE_UNKNOWN_RAM_TARGET_FRACTION
    )
    assert decision.residency_budget.max_loaded_chunks == 1


def test_streaming_memory_policy_fails_closed_for_unknown_gpu():
    decision = decide_streaming_memory(
        available_cell_count=100,
        ram_capability=CapabilityResult.available(
            RamAvailability(total_bytes=4 * GIB, available_bytes=3 * GIB),
            reason_code="ram_availability_detected",
        ),
        gpu_capability=CapabilityResult.unknown(
            reason_code="gpu_memory_probe_failed",
        ),
        ram_target_fraction=0.50,
        gpu_target_fraction=0.70,
        estimated_chunk_ram_bytes=GIB,
        estimated_chunk_gpu_bytes=GIB,
    )

    assert decision.mode is StreamingMemoryMode.CONSERVATIVE
    assert decision.gpu_reason_code == "gpu_memory_probe_failed"
    assert decision.total_gpu_memory_bytes == CONSERVATIVE_UNKNOWN_GPU_MEMORY_BYTES
    assert decision.residency_budget.gpu_budget_chunks == 1
    assert decision.residency_budget.max_loaded_chunks == 1


def test_conservative_gpu_probe_result_is_preserved_as_a_degraded_policy():
    decision = decide_streaming_memory(
        available_cell_count=100,
        ram_capability=CapabilityResult.available(
            RamAvailability(total_bytes=4 * GIB, available_bytes=3 * GIB),
            reason_code="ram_availability_detected",
        ),
        gpu_capability=CapabilityResult.available(
            GpuMemoryBudget(2 * GIB),
            reason_code="gpu_memory_conservative_fallback",
            source=CapabilitySource.CONSERVATIVE_FALLBACK,
        ),
        ram_target_fraction=0.50,
        gpu_target_fraction=0.50,
        estimated_chunk_ram_bytes=GIB,
        estimated_chunk_gpu_bytes=GIB,
    )

    assert decision.mode is StreamingMemoryMode.CONSERVATIVE
    assert decision.gpu_source is CapabilitySource.CONSERVATIVE_FALLBACK
    assert decision.total_gpu_memory_bytes == 2 * GIB
    assert decision.residency_budget.gpu_budget_chunks == 1
