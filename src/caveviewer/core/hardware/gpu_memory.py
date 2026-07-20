"""GPU memory budget detection for runtime streaming policy."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Callable

from caveviewer.core.diagnostics.logging import get_logger


AMD_PCI_VENDOR_ID = 0x1002
LINUX_DRM_ROOT = "/sys/class/drm"
UNKNOWN_GPU_MEMORY_FALLBACK_BYTES = 1 * 1024 ** 3
WINDOWS_UNKNOWN_GPU_MEMORY_FALLBACK_BYTES = UNKNOWN_GPU_MEMORY_FALLBACK_BYTES
AMD_INTEGRATED_VRAM_THRESHOLD_BYTES = 2 * 1024 ** 3
AMD_SHARED_GPU_MEMORY_FRACTION = 0.50
AMD_SHARED_GPU_MEMORY_CAP_BYTES = 2 * 1024 ** 3
SYSFS_INT_MAX_BYTES = 4096

_LOG = get_logger("GpuMemory")


def read_positive_sysfs_int(path: Path) -> int | None:
    try:
        with open(path, "rb") as file_obj:
            payload = file_obj.read(SYSFS_INT_MAX_BYTES + 1)
        if len(payload) > SYSFS_INT_MAX_BYTES:
            return None
        value = int(payload.decode("ascii").strip(), 0)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return value if value > 0 else None


def _amd_effective_gpu_memory_budget_bytes(
    vram_bytes: int,
    gtt_bytes: int | None,
) -> int:
    """Return a conservative AMD GPU memory budget.

    AMD APUs report a small ``mem_info_vram_total`` reservation plus a larger
    GTT/shared-memory aperture. Treating only the VRAM reservation as the whole
    GPU budget over-downscales textures, but treating all GTT as VRAM causes
    stutter because it competes with system RAM and memory bandwidth. Add only
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
    value that blindly overrides hardware discovery. If platform detection can
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
