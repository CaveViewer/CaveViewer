"""Validate current-RAM probing and additional-worker admission policy."""

from __future__ import annotations

import pytest

from caveviewer.core import hardware_memory
from caveviewer.core.worker_config import can_start_additional_worker


def test_linux_ram_snapshot_uses_reclaim_aware_available_memory(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       1000 kB\n"
        "MemFree:         100 kB\n"
        "MemAvailable:    250 kB\n",
        encoding="ascii",
    )

    snapshot = hardware_memory._linux_ram_snapshot(meminfo)

    assert snapshot == hardware_memory.RamSnapshot(
        total_bytes=1000 * 1024,
        available_bytes=250 * 1024,
    )
    assert snapshot.utilization_fraction == pytest.approx(0.75)


@pytest.mark.parametrize(
    ("available_bytes", "expected"),
    [
        (21, True),
        (20, False),
        (19, False),
    ],
)
def test_additional_workers_stop_at_eighty_percent_ram_use(
    available_bytes, expected
):
    snapshot = hardware_memory.RamSnapshot(
        total_bytes=100,
        available_bytes=available_bytes,
    )

    assert can_start_additional_worker(snapshot) is expected


def test_unknown_ram_availability_keeps_pool_at_one_worker():
    assert can_start_additional_worker(None) is False
