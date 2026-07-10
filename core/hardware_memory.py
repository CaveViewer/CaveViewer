"""Hardware memory detection and environment target parsing."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable

from core.logging_utils import get_logger


AMD_PCI_VENDOR_ID = 0x1002
LINUX_DRM_ROOT = "/sys/class/drm"

_LOG = get_logger("HardwareMemory")


def detect_total_ram_bytes() -> int:
    """Best-effort total physical RAM detection without extra dependencies."""
    try:
        if os.name == "nt":
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

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys)
            return 8 * 1024 * 1024 * 1024

        if hasattr(os, "sysconf"):
            page_size = os.sysconf("SC_PAGE_SIZE")
            pages = os.sysconf("SC_PHYS_PAGES")
            if (
                isinstance(page_size, int)
                and isinstance(pages, int)
                and page_size > 0
                and pages > 0
            ):
                return int(page_size * pages)
    except Exception:
        return 8 * 1024 * 1024 * 1024

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


def detect_linux_amd_gpu_memory_bytes(
    drm_root: str | os.PathLike[str] = LINUX_DRM_ROOT,
) -> int | None:
    """Detect AMD VRAM through the Linux DRM/amdgpu sysfs interface."""
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

        is_boot_gpu = read_positive_sysfs_int(device_path / "boot_vga") == 1
        candidates.append((is_boot_gpu, vram_bytes))

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


def detect_total_gpu_memory_bytes(
    gpu_vendor: str | None = None,
    *,
    nvidia_detector: Callable[[], int | None] | None = None,
    amd_detector: Callable[[], int | None] | None = None,
    logger=None,
) -> int | None:
    """Best-effort dedicated GPU memory detection with an env override."""
    log = logger or _LOG
    nvidia_detector = nvidia_detector or detect_nvidia_gpu_memory_bytes
    amd_detector = amd_detector or detect_linux_amd_gpu_memory_bytes

    override_gb = os.environ.get("CAVEVIEWER_GPU_MEMORY_GB", "").strip()
    if override_gb:
        try:
            value = float(override_gb)
            if value > 0.0:
                memory_bytes = int(value * (1024 ** 3))
                log.info(
                    "Using configured GPU memory override: %.1f GB.",
                    memory_bytes / (1024 ** 3),
                )
                return memory_bytes
        except ValueError:
            pass

    normalized_vendor = (gpu_vendor or "").strip().casefold()
    vendor_is_nvidia = "nvidia" in normalized_vendor
    vendor_is_amd = (
        "amd" in normalized_vendor
        or "advanced micro devices" in normalized_vendor
        or normalized_vendor.startswith("ati ")
    )

    if vendor_is_nvidia or not normalized_vendor:
        memory_bytes = nvidia_detector()
        if memory_bytes is not None:
            log.info(
                "Detected NVIDIA GPU memory via nvidia-smi: %.1f GB.",
                memory_bytes / (1024 ** 3),
            )
            return memory_bytes

    if sys.platform.startswith("linux") and (
        vendor_is_amd or not normalized_vendor
    ):
        memory_bytes = amd_detector()
        if memory_bytes is not None:
            log.info(
                "Detected AMD GPU memory via Linux DRM sysfs: %.1f GB.",
                memory_bytes / (1024 ** 3),
            )
            return memory_bytes

    return None
