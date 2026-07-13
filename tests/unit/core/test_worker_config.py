"""Validate worker-count limits, defaults, and reserved-CPU policy."""

from __future__ import annotations

import pytest

from caveviewer.core.worker_config import (
    WorkerAllocation,
    describe_worker_target,
    resolve_worker_allocation,
    resolve_worker_count,
)


@pytest.mark.parametrize(
    ("workers", "reserved", "cpu_count", "expected"),
    [
        ("2", "3", 8, 2),
        ("8", "3", 8, 5),
        ("32", "2", 16, 14),
        ("6", "2", 64, 6),
        ("6", "32", 4, 1),
    ],
)
def test_reserved_cpus_cap_requested_workers(workers, reserved, cpu_count, expected):
    assert resolve_worker_count(
        workers,
        reserved,
        default_workers=2,
        default_reserved_cpus=3,
        logical_cpu_count=cpu_count,
    ) == expected


def test_worker_allocation_explains_reserved_cpu_clamp():
    assert resolve_worker_allocation(
        "8",
        "3",
        default_workers=2,
        default_reserved_cpus=3,
        logical_cpu_count=8,
    ) == WorkerAllocation(
        requested_workers=8,
        reserved_cpus=3,
        logical_cpu_count=8,
        effective_workers=5,
    )


def test_worker_target_description_names_reserved_cpu_clamp():
    allocation = WorkerAllocation(
        requested_workers=8,
        reserved_cpus=3,
        logical_cpu_count=8,
        effective_workers=5,
    )

    assert describe_worker_target("Streaming", allocation) == (
        "Streaming worker target resolved to 5 worker(s): requested 8 capped "
        "by reserved CPU policy (8 logical CPUs - 3 reserved)."
    )


def test_invalid_values_use_defaults():
    assert resolve_worker_count(
        "many",
        "unknown",
        default_workers=4,
        default_reserved_cpus=2,
        logical_cpu_count=8,
    ) == 4


def test_runtime_values_are_bounded_even_outside_advanced_settings():
    assert resolve_worker_count(
        "999",
        "0",
        default_workers=2,
        default_reserved_cpus=3,
        logical_cpu_count=128,
    ) == 32


def test_unknown_cpu_count_keeps_bounded_requested_worker_count(monkeypatch):
    monkeypatch.setattr("caveviewer.core.worker_config.os.cpu_count", lambda: None)

    assert resolve_worker_count(
        "7",
        "3",
        default_workers=2,
        default_reserved_cpus=3,
    ) == 7
