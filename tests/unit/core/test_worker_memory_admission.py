"""Validate current-RAM probing and additional-worker admission policy."""

from __future__ import annotations

from types import SimpleNamespace

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


def test_windows_ram_snapshot_uses_global_memory_status(monkeypatch):
    class Kernel32:
        @staticmethod
        def GlobalMemoryStatusEx(stat_pointer):
            stat = stat_pointer._obj
            stat.ullTotalPhys = 32 * 1024**3
            stat.ullAvailPhys = 12 * 1024**3
            return 1

    monkeypatch.setattr(
        hardware_memory.ctypes,
        "windll",
        SimpleNamespace(kernel32=Kernel32()),
        raising=False,
    )

    snapshot = hardware_memory._windows_ram_snapshot()

    assert snapshot == hardware_memory.RamSnapshot(
        total_bytes=32 * 1024**3,
        available_bytes=12 * 1024**3,
    )
    assert snapshot.utilization_fraction == pytest.approx(0.625)


def test_windows_ram_snapshot_returns_none_when_probe_fails(monkeypatch):
    class Kernel32:
        @staticmethod
        def GlobalMemoryStatusEx(_stat_pointer):
            return 0

    monkeypatch.setattr(
        hardware_memory.ctypes,
        "windll",
        SimpleNamespace(kernel32=Kernel32()),
        raising=False,
    )

    assert hardware_memory._windows_ram_snapshot() is None


def test_detect_ram_snapshot_uses_windows_probe(monkeypatch):
    expected = hardware_memory.RamSnapshot(total_bytes=100, available_bytes=40)

    monkeypatch.setattr(hardware_memory.os, "name", "nt")
    monkeypatch.setattr(hardware_memory, "_windows_ram_snapshot", lambda: expected)

    assert hardware_memory.detect_ram_snapshot() == expected


def test_darwin_vm_stat_parser_uses_reclaimable_pages():
    output = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               1,000.\n"
        "Pages active:                             9,000.\n"
        "Pages inactive:                           2,000.\n"
        "Pages speculative:                          500.\n"
        "Pages wired down:                          3,000.\n"
        "Pages occupied by compressor:              1,000.\n"
    )

    available_bytes = hardware_memory._parse_darwin_vm_stat_available_bytes(output)

    assert available_bytes == 3_500 * 16_384


def test_darwin_ram_snapshot_reads_total_and_available_memory(monkeypatch):
    def fake_run(args, **_kwargs):
        if args == ["sysctl", "-n", "hw.memsize"]:
            return SimpleNamespace(stdout=str(16 * 1024**3))
        if args == ["vm_stat"]:
            return SimpleNamespace(
                stdout=(
                    "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                    "Pages free:        1,000,000.\n"
                    "Pages inactive:      500,000.\n"
                    "Pages speculative:   250,000.\n"
                )
            )
        raise AssertionError(f"unexpected command: {args!r}")

    monkeypatch.setattr(hardware_memory.subprocess, "run", fake_run)

    snapshot = hardware_memory._darwin_ram_snapshot()

    assert snapshot == hardware_memory.RamSnapshot(
        total_bytes=16 * 1024**3,
        available_bytes=1_750_000 * 4096,
    )
    assert can_start_additional_worker(snapshot) is True


def test_darwin_ram_snapshot_estimates_total_when_sysctl_is_unavailable(monkeypatch):
    def fake_run(args, **_kwargs):
        if args == ["sysctl", "-n", "hw.memsize"]:
            raise PermissionError("sysctl denied")
        if args == ["vm_stat"]:
            return SimpleNamespace(
                stdout=(
                    "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                    "Pages free:                         100.\n"
                    "Pages active:                       400.\n"
                    "Pages inactive:                     200.\n"
                    "Pages speculative:                   50.\n"
                    "Pages throttled:                     10.\n"
                    "Pages wired down:                   150.\n"
                    "Pages occupied by compressor:        90.\n"
                    "Pages purgeable:                    999.\n"
                    "File-backed pages:                  999.\n"
                )
            )
        raise AssertionError(f"unexpected command: {args!r}")

    monkeypatch.setattr(hardware_memory.subprocess, "run", fake_run)

    snapshot = hardware_memory._darwin_ram_snapshot()

    assert snapshot == hardware_memory.RamSnapshot(
        total_bytes=1_000 * 4096,
        available_bytes=350 * 4096,
    )


def test_detect_ram_snapshot_uses_darwin_probe(monkeypatch):
    expected = hardware_memory.RamSnapshot(total_bytes=100, available_bytes=50)

    monkeypatch.setattr(hardware_memory.os, "name", "posix")
    monkeypatch.setattr(hardware_memory.sys, "platform", "darwin")
    monkeypatch.setattr(hardware_memory, "_darwin_ram_snapshot", lambda: expected)

    assert hardware_memory.detect_ram_snapshot() == expected


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
