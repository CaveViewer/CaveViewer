"""Test typed hardware capability probes without changing numeric callers."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import (
    CapabilitySource,
    CapabilityStatus,
    GpuMemoryBudget,
    RamAvailability,
)
from caveviewer.core.hardware import gpu_memory, system_memory


GIB = 1024 ** 3


class _LogRecorder:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


def test_gpu_memory_capability_preserves_detected_budget_provenance():
    result = gpu_memory.probe_gpu_memory_budget(
        "NVIDIA",
        nvidia_detector=lambda: 8 * GIB,
        environment={},
        logger=_LogRecorder(),
    )

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.source is CapabilitySource.DETECTED
    assert result.reason_code == "gpu_memory_detected"
    assert result.value == GpuMemoryBudget(8 * GIB)
    assert result.evidence == {"detector": "nvidia_smi"}


def test_gpu_memory_capability_marks_a_lower_configured_ceiling_as_user_override():
    result = gpu_memory.probe_gpu_memory_budget(
        "NVIDIA",
        nvidia_detector=lambda: 8 * GIB,
        environment={"CAVEVIEWER_GPU_MEMORY_GB": "3.5"},
        logger=_LogRecorder(),
    )

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.source is CapabilitySource.USER_OVERRIDE
    assert result.reason_code == "gpu_memory_user_cap"
    assert result.value == GpuMemoryBudget(int(3.5 * GIB))


def test_gpu_memory_capability_keeps_detected_budget_when_override_is_too_large():
    result = gpu_memory.probe_gpu_memory_budget(
        "NVIDIA",
        nvidia_detector=lambda: 4 * GIB,
        environment={"CAVEVIEWER_GPU_MEMORY_GB": "16"},
        logger=_LogRecorder(),
    )

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.source is CapabilitySource.DETECTED
    assert result.reason_code == "gpu_memory_detected"
    assert result.value == GpuMemoryBudget(4 * GIB)
    assert result.evidence["override"] == "capped_to_detected"


def test_gpu_memory_capability_distinguishes_unverified_override_and_fallback():
    override = gpu_memory.probe_gpu_memory_budget(
        "Intel",
        environment={"CAVEVIEWER_GPU_MEMORY_GB": "2"},
        logger=_LogRecorder(),
    )
    fallback = gpu_memory.probe_gpu_memory_budget(
        "Intel",
        environment={},
        logger=_LogRecorder(),
    )

    assert override.source is CapabilitySource.USER_OVERRIDE
    assert override.reason_code == "gpu_memory_user_override_unverified"
    assert override.value == GpuMemoryBudget(2 * GIB)
    assert fallback.source is CapabilitySource.CONSERVATIVE_FALLBACK
    assert fallback.reason_code == "gpu_memory_conservative_fallback"
    assert fallback.value == GpuMemoryBudget(
        gpu_memory.UNKNOWN_GPU_MEMORY_FALLBACK_BYTES
    )


@pytest.mark.parametrize("override", ["nan", "inf", "-inf", "1e-12"])
def test_gpu_memory_capability_ignores_non_operational_override_values(override):
    result = gpu_memory.probe_gpu_memory_budget(
        "Intel",
        environment={"CAVEVIEWER_GPU_MEMORY_GB": override},
        logger=_LogRecorder(),
    )

    assert result.source is CapabilitySource.CONSERVATIVE_FALLBACK
    assert result.reason_code == "gpu_memory_conservative_fallback"
    assert result.value == GpuMemoryBudget(
        gpu_memory.UNKNOWN_GPU_MEMORY_FALLBACK_BYTES
    )


def test_ram_availability_capability_wraps_a_current_snapshot():
    result = system_memory.probe_ram_availability(
        snapshot_detector=lambda: system_memory.RamSnapshot(
            total_bytes=100,
            available_bytes=25,
        )
    )

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.source is CapabilitySource.DETECTED
    assert result.reason_code == "ram_availability_detected"
    assert result.value == RamAvailability(total_bytes=100, available_bytes=25)


@pytest.mark.parametrize(
    "snapshot_detector",
    [
        lambda: None,
        lambda: (_ for _ in ()).throw(OSError("probe failed")),
        lambda: system_memory.RamSnapshot(total_bytes=0, available_bytes=0),
    ],
)
def test_ram_availability_capability_fails_closed_for_unknown_measurements(
    snapshot_detector,
):
    result = system_memory.probe_ram_availability(
        snapshot_detector=snapshot_detector,
    )

    assert result.status is CapabilityStatus.UNKNOWN
    assert result.value is None


def test_hardware_capability_values_reject_invalid_budgets():
    with pytest.raises(ValueError, match="GPU memory budget"):
        GpuMemoryBudget(0)
    with pytest.raises(ValueError, match="RAM availability"):
        RamAvailability(total_bytes=100, available_bytes=101)
