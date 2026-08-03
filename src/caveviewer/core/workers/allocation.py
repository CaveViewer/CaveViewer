"""Shared worker-pool sizing rules for streaming and cache building."""

from __future__ import annotations

from dataclasses import dataclass
import os

from caveviewer.core.capabilities import RamAvailability
from caveviewer.core.hardware.system_memory import RamSnapshot


MIN_WORKERS = 1
MAX_WORKERS = 32
MIN_RESERVED_CPUS = 2
MAX_RESERVED_CPUS = 32
MAX_WORKER_RAM_UTILIZATION = 0.80


@dataclass(frozen=True)
class WorkerAllocation:
    """Resolved worker-pool limits after requested and reserved-CPU bounds."""

    requested_workers: int
    reserved_cpus: int
    logical_cpu_count: int | None
    effective_workers: int


def can_start_additional_worker(
    snapshot: RamSnapshot | RamAvailability | None,
) -> bool:
    """Allow pool growth only while current system RAM use is below 80%.

    The first worker is always admitted by each pool. If current RAM cannot be
    measured, callers conservatively keep that single worker instead of
    honoring an aggressive configured maximum blindly.
    """
    return (
        snapshot is not None
        and snapshot.utilization_fraction < MAX_WORKER_RAM_UTILIZATION
    )


def _bounded_int(raw_value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int((raw_value or "").strip())
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def resolve_worker_count(
    worker_value: str | None,
    reserved_cpu_value: str | None,
    *,
    default_workers: int,
    default_reserved_cpus: int,
    logical_cpu_count: int | None = None,
) -> int:
    """Resolve a safe pool size while leaving the requested CPUs available.

    The configured worker count is a requested maximum. On systems where a
    logical CPU count is available, the pool is reduced as needed so no more
    than ``logical CPUs - reserved CPUs`` workers are created.
    """
    return resolve_worker_allocation(
        worker_value,
        reserved_cpu_value,
        default_workers=default_workers,
        default_reserved_cpus=default_reserved_cpus,
        logical_cpu_count=logical_cpu_count,
    ).effective_workers


def resolve_worker_allocation(
    worker_value: str | None,
    reserved_cpu_value: str | None,
    *,
    default_workers: int,
    default_reserved_cpus: int,
    logical_cpu_count: int | None = None,
) -> WorkerAllocation:
    """Resolve worker count and keep the inputs that explain runtime clamping."""
    requested_workers = _bounded_int(
        worker_value, default_workers, MIN_WORKERS, MAX_WORKERS
    )
    reserved_cpus = _bounded_int(
        reserved_cpu_value,
        default_reserved_cpus,
        MIN_RESERVED_CPUS,
        MAX_RESERVED_CPUS,
    )
    cpu_count = os.cpu_count() if logical_cpu_count is None else logical_cpu_count
    if cpu_count is None or cpu_count < 1:
        return WorkerAllocation(
            requested_workers=requested_workers,
            reserved_cpus=reserved_cpus,
            logical_cpu_count=None,
            effective_workers=requested_workers,
        )
    available_workers = max(MIN_WORKERS, cpu_count - reserved_cpus)
    return WorkerAllocation(
        requested_workers=requested_workers,
        reserved_cpus=reserved_cpus,
        logical_cpu_count=cpu_count,
        effective_workers=min(requested_workers, available_workers),
    )


def describe_worker_target(pool_name: str, allocation: WorkerAllocation) -> str:
    """Return a concise log message explaining the effective worker target."""
    if (
        allocation.logical_cpu_count is not None
        and allocation.effective_workers < allocation.requested_workers
    ):
        return (
            f"{pool_name} worker target resolved to "
            f"{allocation.effective_workers} worker(s): requested "
            f"{allocation.requested_workers} capped by reserved CPU policy "
            f"({allocation.logical_cpu_count} logical CPUs - "
            f"{allocation.reserved_cpus} reserved)."
        )

    logical_cpus = (
        str(allocation.logical_cpu_count)
        if allocation.logical_cpu_count is not None
        else "unknown"
    )
    return (
        f"{pool_name} worker target resolved to "
        f"{allocation.effective_workers} worker(s) "
        f"(requested {allocation.requested_workers}, reserved CPUs "
        f"{allocation.reserved_cpus}, logical CPUs {logical_cpus})."
    )
