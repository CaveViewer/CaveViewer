"""Cover GPU-memory detection, adapter selection, and configuration fallbacks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caveviewer.core.hardware import gpu_memory


GIB = 1024 ** 3


def _make_drm_card(
    drm_root,
    name: str,
    *,
    vendor: str = "0x1002",
    vram: str | int = GIB,
    gtt: str | int | None = None,
    boot_vga: str | int | None = None,
):
    device = drm_root / name / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text(str(vendor), encoding="ascii")
    (device / "mem_info_vram_total").write_text(str(vram), encoding="ascii")
    if gtt is not None:
        (device / "mem_info_gtt_total").write_text(str(gtt), encoding="ascii")
    if boot_vga is not None:
        (device / "boot_vga").write_text(str(boot_vga), encoding="ascii")


def test_linux_amd_detection_prefers_primary_adapter(tmp_path):
    _make_drm_card(
        tmp_path, "card0", vendor="0x8086", vram=16 * GIB, boot_vga=1
    )
    _make_drm_card(tmp_path, "card1", vram=GIB, boot_vga=1)
    _make_drm_card(tmp_path, "card2", vram=8 * GIB, boot_vga=0)

    assert gpu_memory.detect_linux_amd_gpu_memory_bytes(tmp_path) == GIB


def test_linux_amd_detection_uses_largest_adapter_without_primary(tmp_path):
    _make_drm_card(tmp_path, "card0", vram=2 * GIB)
    _make_drm_card(tmp_path, "card1", vram=8 * GIB, boot_vga=0)

    assert gpu_memory.detect_linux_amd_gpu_memory_bytes(tmp_path) == 8 * GIB


def test_linux_amd_detection_adds_capped_shared_budget_for_integrated_gpu(tmp_path):
    _make_drm_card(tmp_path, "card0", vram=GIB, gtt=8 * GIB, boot_vga=1)

    assert gpu_memory.detect_linux_amd_gpu_memory_bytes(tmp_path) == 3 * GIB


def test_linux_amd_detection_uses_fractional_shared_budget_below_cap(tmp_path):
    _make_drm_card(tmp_path, "card0", vram=GIB, gtt=2 * GIB, boot_vga=1)

    assert gpu_memory.detect_linux_amd_gpu_memory_bytes(tmp_path) == 2 * GIB


def test_linux_amd_detection_keeps_discrete_vram_budget(tmp_path):
    _make_drm_card(tmp_path, "card0", vram=8 * GIB, gtt=8 * GIB, boot_vga=1)

    assert gpu_memory.detect_linux_amd_gpu_memory_bytes(tmp_path) == 8 * GIB


def test_linux_amd_detection_ignores_connectors_and_invalid_values(tmp_path):
    _make_drm_card(tmp_path, "card0", vendor="0x8086", vram=8 * GIB)
    _make_drm_card(tmp_path, "card1", vram="not-a-number")
    _make_drm_card(tmp_path, "card2", vram=0)
    _make_drm_card(tmp_path, "card3-eDP-1", vram=4 * GIB, boot_vga=1)

    assert gpu_memory.detect_linux_amd_gpu_memory_bytes(tmp_path) is None


def test_sysfs_int_reader_rejects_oversized_values(tmp_path, monkeypatch):
    path = tmp_path / "value"
    path.write_text("12345", encoding="ascii")
    monkeypatch.setattr(gpu_memory, "SYSFS_INT_MAX_BYTES", 4)

    assert gpu_memory.read_positive_sysfs_int(path) is None


def test_gpu_memory_override_can_lower_detected_budget(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_GPU_MEMORY_GB", "3.5")
    monkeypatch.setattr(
        gpu_memory.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="8192\n"),
    )

    assert gpu_memory.detect_total_gpu_memory_bytes() == int(3.5 * GIB)


def test_gpu_memory_override_cannot_exceed_detected_active_gpu(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_GPU_MEMORY_GB", "16")
    monkeypatch.setattr(
        gpu_memory.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="4096\n"),
    )

    assert gpu_memory.detect_total_gpu_memory_bytes() == 4 * GIB


def test_unverified_gpu_memory_override_is_used_when_detection_fails(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_GPU_MEMORY_GB", "2")
    monkeypatch.setattr(
        gpu_memory.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(gpu_memory.sys, "platform", "linux")
    monkeypatch.setattr(
        gpu_memory,
        "detect_linux_amd_gpu_memory_bytes",
        lambda: None,
    )

    assert gpu_memory.detect_total_gpu_memory_bytes() == 2 * GIB


def test_nvidia_detection_still_precedes_amd(monkeypatch):
    monkeypatch.setattr(
        gpu_memory.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="8192\n"),
    )

    def unexpected_amd_detection(*_args, **_kwargs):
        pytest.fail("AMD fallback should not run after NVIDIA detection succeeds")

    monkeypatch.setattr(
        gpu_memory,
        "detect_linux_amd_gpu_memory_bytes",
        unexpected_amd_detection,
    )

    assert gpu_memory.detect_total_gpu_memory_bytes() == 8192 * 1024 ** 2


def test_active_amd_vendor_skips_secondary_nvidia_gpu(monkeypatch):
    def unexpected_nvidia_detection(*_args, **_kwargs):
        pytest.fail("NVIDIA detection should not run for an active AMD context")

    monkeypatch.setattr(
        gpu_memory, "detect_nvidia_gpu_memory_bytes", unexpected_nvidia_detection
    )
    monkeypatch.setattr(gpu_memory.sys, "platform", "linux")
    monkeypatch.setattr(
        gpu_memory,
        "detect_linux_amd_gpu_memory_bytes",
        lambda: 2 * GIB,
    )

    assert gpu_memory.detect_total_gpu_memory_bytes("AMD") == 2 * GIB


def test_known_non_amd_vendor_does_not_use_amd_adapter(monkeypatch):
    def unexpected_detection(*_args, **_kwargs):
        pytest.fail("a secondary GPU should not supply the active GPU's budget")

    monkeypatch.setattr(
        gpu_memory, "detect_nvidia_gpu_memory_bytes", unexpected_detection
    )
    monkeypatch.setattr(
        gpu_memory, "detect_linux_amd_gpu_memory_bytes", unexpected_detection
    )

    assert (
        gpu_memory.detect_total_gpu_memory_bytes("Intel")
        == gpu_memory.UNKNOWN_GPU_MEMORY_FALLBACK_BYTES
    )


def test_unknown_gpu_uses_conservative_fallback(monkeypatch):
    monkeypatch.setattr(
        gpu_memory.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(gpu_memory.sys, "platform", "linux")
    monkeypatch.setattr(
        gpu_memory,
        "detect_linux_amd_gpu_memory_bytes",
        lambda: None,
    )

    assert (
        gpu_memory.detect_total_gpu_memory_bytes()
        == gpu_memory.UNKNOWN_GPU_MEMORY_FALLBACK_BYTES
    )


def test_unknown_windows_gpu_uses_conservative_fallback(monkeypatch):
    monkeypatch.setattr(gpu_memory.sys, "platform", "win32")

    assert (
        gpu_memory.detect_total_gpu_memory_bytes("Intel")
        == gpu_memory.UNKNOWN_GPU_MEMORY_FALLBACK_BYTES
    )


def test_linux_amd_detection_is_used_when_nvidia_is_unavailable(monkeypatch):
    def no_nvidia(*_args, **_kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(gpu_memory.subprocess, "run", no_nvidia)
    monkeypatch.setattr(gpu_memory.sys, "platform", "linux")
    monkeypatch.setattr(
        gpu_memory,
        "detect_linux_amd_gpu_memory_bytes",
        lambda: 4 * GIB,
    )

    assert gpu_memory.detect_total_gpu_memory_bytes() == 4 * GIB
