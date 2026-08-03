"""System RAM detection for import and streaming admission policy."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from dataclasses import dataclass
import re
import subprocess
import sys

from caveviewer.core.capabilities import CapabilityResult, RamAvailability

LINUX_MEMINFO_MAX_BYTES = 1024 * 1024
_DARWIN_VM_STAT_PAGE_SIZE_RE = re.compile(r"page size of\s+(\d+)\s+bytes")
_DARWIN_VM_STAT_TOTAL_KEYS = (
    "Pages free",
    "Pages active",
    "Pages inactive",
    "Pages speculative",
    "Pages throttled",
    "Pages wired down",
    "Pages occupied by compressor",
)


@dataclass(frozen=True)
class RamSnapshot:
    """One current system-RAM measurement used for worker admission."""

    total_bytes: int
    available_bytes: int

    @property
    def utilization_fraction(self) -> float:
        """Return the fraction of physical RAM currently unavailable."""
        if self.total_bytes <= 0:
            return 1.0
        available = max(0, min(self.available_bytes, self.total_bytes))
        return 1.0 - (available / self.total_bytes)


def _windows_ram_snapshot() -> RamSnapshot | None:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            total_bytes = int(stat.ullTotalPhys)
            available_bytes = int(stat.ullAvailPhys)
            if total_bytes > 0 and available_bytes >= 0:
                return RamSnapshot(total_bytes, available_bytes)
    except Exception:
        pass
    return None


def _linux_ram_snapshot(
    meminfo_path: str | os.PathLike[str] = "/proc/meminfo",
) -> RamSnapshot | None:
    """Read Linux's reclaim-aware MemAvailable measurement."""
    try:
        with open(meminfo_path, "rb") as file_obj:
            payload = file_obj.read(LINUX_MEMINFO_MAX_BYTES + 1)
        if len(payload) > LINUX_MEMINFO_MAX_BYTES:
            return None
        lines = payload.decode("ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None

    values: dict[str, int] = {}
    for line in lines:
        key, separator, raw_value = line.partition(":")
        if not separator:
            continue
        fields = raw_value.strip().split()
        if not fields:
            continue
        try:
            value = int(fields[0])
        except ValueError:
            continue
        multiplier = 1024 if len(fields) > 1 and fields[1].lower() == "kb" else 1
        values[key] = value * multiplier

    total_bytes = values.get("MemTotal", 0)
    available_bytes = values.get("MemAvailable", values.get("MemFree", -1))
    if total_bytes <= 0 or available_bytes < 0:
        return None
    return RamSnapshot(total_bytes, available_bytes)


def _darwin_total_ram_bytes() -> int | None:
    """Read macOS physical RAM size from sysctl."""
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        total_bytes = int(result.stdout.strip())
    except Exception:
        return None
    return total_bytes if total_bytes > 0 else None


def _parse_darwin_vm_stat_pages(output: str) -> tuple[int, dict[str, int]] | None:
    """Parse macOS ``vm_stat`` page size and named page counters."""
    lines = output.splitlines()
    if not lines:
        return None

    page_size_match = _DARWIN_VM_STAT_PAGE_SIZE_RE.search(lines[0])
    if page_size_match is None:
        return None

    try:
        page_size = int(page_size_match.group(1))
    except ValueError:
        return None
    if page_size <= 0:
        return None

    pages: dict[str, int] = {}
    for line in lines[1:]:
        key, separator, raw_value = line.partition(":")
        if not separator:
            continue
        try:
            token = raw_value.strip().split()[0].rstrip(".").replace(",", "")
            pages[key.strip()] = int(token)
        except (IndexError, ValueError):
            continue
    return page_size, pages


def _parse_darwin_vm_stat_available_bytes(output: str) -> int | None:
    """Estimate reclaim-aware available RAM from macOS ``vm_stat`` output."""
    parsed = _parse_darwin_vm_stat_pages(output)
    if parsed is None:
        return None

    page_size, pages = parsed
    # macOS lacks a stable sysconf available-page counter. Free, inactive, and
    # speculative pages are either immediately free or reclaimable under memory
    # pressure, which matches the admission policy's conservative needs.
    available_pages = sum(
        pages.get(key, 0)
        for key in ("Pages free", "Pages inactive", "Pages speculative")
    )
    if available_pages < 0:
        return None
    return available_pages * page_size


def _parse_darwin_vm_stat_total_bytes(output: str) -> int | None:
    """Estimate physical RAM from disjoint macOS ``vm_stat`` page categories."""
    parsed = _parse_darwin_vm_stat_pages(output)
    if parsed is None:
        return None

    page_size, pages = parsed
    total_pages = sum(pages.get(key, 0) for key in _DARWIN_VM_STAT_TOTAL_KEYS)
    if total_pages <= 0:
        return None
    return total_pages * page_size


def _darwin_ram_snapshot() -> RamSnapshot | None:
    """Read macOS total and currently reclaimable physical RAM."""
    total_bytes = _darwin_total_ram_bytes()

    try:
        result = subprocess.run(
            ["vm_stat"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception:
        return None

    available_bytes = _parse_darwin_vm_stat_available_bytes(result.stdout)
    if available_bytes is None:
        return None
    if total_bytes is None:
        total_bytes = _parse_darwin_vm_stat_total_bytes(result.stdout)
        if total_bytes is None:
            return None
    return RamSnapshot(
        total_bytes=total_bytes,
        available_bytes=min(available_bytes, total_bytes),
    )


def _sysconf_ram_snapshot() -> RamSnapshot | None:
    """Use POSIX page counters when the platform exposes available pages."""
    if not hasattr(os, "sysconf"):
        return None
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total_pages = int(os.sysconf("SC_PHYS_PAGES"))
        available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
    except (OSError, TypeError, ValueError):
        return None
    if page_size <= 0 or total_pages <= 0 or available_pages < 0:
        return None
    return RamSnapshot(
        total_bytes=page_size * total_pages,
        available_bytes=page_size * available_pages,
    )


def detect_ram_snapshot() -> RamSnapshot | None:
    """Best-effort current physical-RAM availability across desktop platforms."""
    if os.name == "nt":
        return _windows_ram_snapshot()
    if sys.platform == "darwin":
        snapshot = _darwin_ram_snapshot()
        if snapshot is not None:
            return snapshot
    if sys.platform.startswith("linux"):
        snapshot = _linux_ram_snapshot()
        if snapshot is not None:
            return snapshot
    return _sysconf_ram_snapshot()


def probe_ram_availability(
    *,
    snapshot_detector: Callable[[], RamSnapshot | None] | None = None,
) -> CapabilityResult[RamAvailability]:
    """Return a typed current-RAM fact without changing numeric fallback APIs.

    This represents current availability, which must stay unknown when no
    platform probe can measure it. Callers that only need a total-RAM fallback
    continue to use ``detect_total_ram_bytes()`` unchanged.
    """
    detect_snapshot = snapshot_detector or detect_ram_snapshot
    try:
        snapshot = detect_snapshot()
    except Exception:
        return CapabilityResult.unknown(
            reason_code="ram_availability_probe_failed",
            evidence={"probe": "ram_snapshot"},
        )

    if snapshot is None:
        return CapabilityResult.unknown(
            reason_code="ram_availability_unknown",
            evidence={"probe": "ram_snapshot"},
        )

    try:
        total_bytes = int(snapshot.total_bytes)
        available_bytes = int(snapshot.available_bytes)
    except (TypeError, ValueError):
        return CapabilityResult.unknown(
            reason_code="ram_availability_invalid",
            evidence={"probe": "ram_snapshot"},
        )
    if total_bytes <= 0 or available_bytes < 0:
        return CapabilityResult.unknown(
            reason_code="ram_availability_invalid",
            evidence={"probe": "ram_snapshot"},
        )

    return CapabilityResult.available(
        RamAvailability(
            total_bytes=total_bytes,
            available_bytes=min(available_bytes, total_bytes),
        ),
        reason_code="ram_availability_detected",
        evidence={"probe": "ram_snapshot"},
    )


def detect_total_ram_bytes() -> int:
    """Best-effort total physical RAM detection without extra dependencies."""
    snapshot = detect_ram_snapshot()
    if snapshot is not None:
        return snapshot.total_bytes
    if sys.platform == "darwin":
        total_bytes = _darwin_total_ram_bytes()
        if total_bytes is not None:
            return total_bytes
    try:
        if hasattr(os, "sysconf"):
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            total_pages = int(os.sysconf("SC_PHYS_PAGES"))
            if page_size > 0 and total_pages > 0:
                return page_size * total_pages
    except (OSError, TypeError, ValueError):
        pass
    return 8 * 1024 * 1024 * 1024
