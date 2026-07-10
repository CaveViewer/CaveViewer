"""Shared worker-pool sizing rules for streaming and cache building."""

from __future__ import annotations

import os


MIN_WORKERS = 1
MAX_WORKERS = 32
MIN_RESERVED_CPUS = 2
MAX_RESERVED_CPUS = 32


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
        return requested_workers
    available_workers = max(MIN_WORKERS, cpu_count - reserved_cpus)
    return min(requested_workers, available_workers)
