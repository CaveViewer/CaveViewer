"""Hardware memory detection and environment target parsing."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable

from caveviewer.core.logging_utils import get_logger


AMD_PCI_VENDOR_ID = 0x1002
LINUX_DRM_ROOT = "/sys/class/drm"
UNKNOWN_GPU_MEMORY_FALLBACK_BYTES = 1 * 1024 ** 3
WINDOWS_UNKNOWN_GPU_MEMORY_FALLBACK_BYTES = 8 * 1024 ** 3
AMD_INTEGRATED_VRAM_THRESHOLD_BYTES = 2 * 1024 ** 3
AMD_SHARED_GPU_MEMORY_FRACTION = 0.50
AMD_SHARED_GPU_MEMORY_CAP_BYTES = 2 * 1024 ** 3

_LOG = get_logger("HardwareMemory")
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
        lines = Path(meminfo_path).read_text(encoding="ascii").splitlines()
    except OSError:
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


def parse_target_fraction(
    raw_value: str | None, conservative_default: float
) -> float:
    """Parse either a fraction or percentage and apply safe guardrails."""
    if raw_value is None:
        return conservative_default

    text = raw_value.strip()
    if not text:
        return conservative_default

    try:
        value = float(text)
    except ValueError:
        return conservative_default

    if value > 1.0:
        value = value / 100.0
    return max(0.01, min(0.80, value))


def parse_memory_target_fraction(raw_value: str | None) -> float:
    return parse_target_fraction(raw_value, conservative_default=0.08)


def parse_gpu_target_fraction(raw_value: str | None) -> float:
    return parse_target_fraction(raw_value, conservative_default=0.70)


def read_positive_sysfs_int(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="ascii").strip(), 0)
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def _amd_effective_gpu_memory_budget_bytes(
    vram_bytes: int,
    gtt_bytes: int | None,
) -> int:
    """Return a conservative AMD GPU memory budget.

    AMD APUs report a small ``mem_info_vram_total`` reservation plus a larger
    GTT/shared-memory aperture.  Treating only the VRAM reservation as the whole
    GPU budget over-downscales textures, but treating all GTT as VRAM causes
    stutter because it competes with system RAM and memory bandwidth.  Add only
    a capped fraction of GTT for low-VRAM UMA-style adapters; leave normal
    discrete-card budgets unchanged.
    """
    if vram_bytes <= 0:
        return 0
    if (
        gtt_bytes is None
        or gtt_bytes <= 0
        or vram_bytes > AMD_INTEGRATED_VRAM_THRESHOLD_BYTES
    ):
        return vram_bytes

    shared_allowance = min(
        int(gtt_bytes * AMD_SHARED_GPU_MEMORY_FRACTION),
        AMD_SHARED_GPU_MEMORY_CAP_BYTES,
    )
    return vram_bytes + max(0, shared_allowance)


def detect_linux_amd_gpu_memory_bytes(
    drm_root: str | os.PathLike[str] = LINUX_DRM_ROOT,
) -> int | None:
    """Detect an AMD GPU memory budget through Linux DRM/amdgpu sysfs."""
    root = Path(drm_root)
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return None

    candidates: list[tuple[bool, int]] = []
    for card_path in entries:
        if not card_path.name.startswith("card"):
            continue
        card_index = card_path.name.removeprefix("card")
        if not card_index.isdigit():
            continue

        device_path = card_path / "device"
        vendor_id = read_positive_sysfs_int(device_path / "vendor")
        if vendor_id != AMD_PCI_VENDOR_ID:
            continue

        vram_bytes = read_positive_sysfs_int(
            device_path / "mem_info_vram_total"
        )
        if vram_bytes is None:
            continue

        gtt_bytes = read_positive_sysfs_int(
            device_path / "mem_info_gtt_total"
        )
        budget_bytes = _amd_effective_gpu_memory_budget_bytes(
            vram_bytes, gtt_bytes
        )
        is_boot_gpu = read_positive_sysfs_int(device_path / "boot_vga") == 1
        candidates.append((is_boot_gpu, budget_bytes))

    if not candidates:
        return None
    _is_boot_gpu, vram_bytes = max(
        candidates, key=lambda candidate: (candidate[0], candidate[1])
    )
    return vram_bytes


def detect_nvidia_gpu_memory_bytes() -> int | None:
    """Detect NVIDIA VRAM with nvidia-smi when it is available."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        first_line = result.stdout.strip().splitlines()[0].strip()
        total_mb = int(first_line.split()[0])
        if total_mb > 0:
            return total_mb * 1024 * 1024
    except Exception:
        pass
    return None


def _unknown_gpu_memory_fallback_bytes() -> tuple[int, str]:
    if os.name == "nt" or sys.platform.startswith("win"):
        return WINDOWS_UNKNOWN_GPU_MEMORY_FALLBACK_BYTES, "Windows fallback"
    return UNKNOWN_GPU_MEMORY_FALLBACK_BYTES, "conservative fallback"


def detect_total_gpu_memory_bytes(
    gpu_vendor: str | None = None,
    *,
    nvidia_detector: Callable[[], int | None] | None = None,
    amd_detector: Callable[[], int | None] | None = None,
    logger=None,
) -> int | None:
    """Best-effort active-GPU memory budget with a conservative fallback.

    ``CAVEVIEWER_GPU_MEMORY_GB`` is treated as a user-requested ceiling, not a
    value that blindly overrides hardware discovery.  If platform detection can
    identify a smaller active adapter, the detected value wins so an optimistic
    manual setting cannot push the streamer into predictable driver OOMs.
    """
    log = logger or _LOG
    nvidia_detector = nvidia_detector or detect_nvidia_gpu_memory_bytes
    amd_detector = amd_detector or detect_linux_amd_gpu_memory_bytes

    override_bytes = None
    override_gb = os.environ.get("CAVEVIEWER_GPU_MEMORY_GB", "").strip()
    if override_gb:
        try:
            value = float(override_gb)
            if value > 0.0:
                override_bytes = int(value * (1024 ** 3))
        except ValueError:
            pass

    normalized_vendor = (gpu_vendor or "").strip().casefold()
    vendor_is_nvidia = "nvidia" in normalized_vendor
    vendor_is_amd = (
        "amd" in normalized_vendor
        or "advanced micro devices" in normalized_vendor
        or normalized_vendor.startswith("ati ")
    )

    detected_bytes = None
    detected_source = None
    if vendor_is_nvidia or not normalized_vendor:
        memory_bytes = nvidia_detector()
        if memory_bytes is not None:
            detected_bytes = memory_bytes
            detected_source = "NVIDIA GPU memory via nvidia-smi"

    if detected_bytes is None and sys.platform.startswith("linux") and (
        vendor_is_amd or not normalized_vendor
    ):
        memory_bytes = amd_detector()
        if memory_bytes is not None:
            detected_bytes = memory_bytes
            detected_source = "AMD GPU memory budget via Linux DRM sysfs"

    if detected_bytes is not None:
        if override_bytes is None:
            log.info(
                "Detected %s: %.1f GB.",
                detected_source,
                detected_bytes / (1024 ** 3),
            )
            return detected_bytes

        effective_bytes = min(override_bytes, detected_bytes)
        if override_bytes > detected_bytes:
            log.warning(
                "Configured GPU memory override %.1f GB exceeds detected active GPU memory %.1f GB; using detected value.",
                override_bytes / (1024 ** 3),
                detected_bytes / (1024 ** 3),
            )
        else:
            log.info(
                "Using configured GPU memory override %.1f GB below detected %s %.1f GB.",
                override_bytes / (1024 ** 3),
                detected_source,
                detected_bytes / (1024 ** 3),
            )
        return effective_bytes

    if override_bytes is not None:
        log.warning(
            "Using unverified configured GPU memory override: %.1f GB. "
            "Automatic active-GPU memory detection was unavailable.",
            override_bytes / (1024 ** 3),
        )
        return override_bytes

    fallback_bytes, fallback_label = _unknown_gpu_memory_fallback_bytes()
    log.warning(
        "GPU memory detection unavailable; using %s: %.1f GB.",
        fallback_label,
        fallback_bytes / (1024 ** 3),
    )
    return fallback_bytes
