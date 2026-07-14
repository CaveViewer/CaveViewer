"""Cover GPU-memory detection, adapter selection, and configuration fallbacks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caveviewer.core import hardware_memory


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

    assert hardware_memory.detect_linux_amd_gpu_memory_bytes(tmp_path) == GIB


def test_linux_amd_detection_uses_largest_adapter_without_primary(tmp_path):
    _make_drm_card(tmp_path, "card0", vram=2 * GIB)
    _make_drm_card(tmp_path, "card1", vram=8 * GIB, boot_vga=0)

    assert hardware_memory.detect_linux_amd_gpu_memory_bytes(tmp_path) == 8 * GIB


def test_linux_amd_detection_adds_capped_shared_budget_for_integrated_gpu(tmp_path):
    _make_drm_card(tmp_path, "card0", vram=GIB, gtt=8 * GIB, boot_vga=1)

    assert hardware_memory.detect_linux_amd_gpu_memory_bytes(tmp_path) == 3 * GIB


def test_linux_amd_detection_uses_fractional_shared_budget_below_cap(tmp_path):
    _make_drm_card(tmp_path, "card0", vram=GIB, gtt=2 * GIB, boot_vga=1)

    assert hardware_memory.detect_linux_amd_gpu_memory_bytes(tmp_path) == 2 * GIB


def test_linux_amd_detection_keeps_discrete_vram_budget(tmp_path):
    _make_drm_card(tmp_path, "card0", vram=8 * GIB, gtt=8 * GIB, boot_vga=1)

    assert hardware_memory.detect_linux_amd_gpu_memory_bytes(tmp_path) == 8 * GIB


def test_linux_amd_detection_ignores_connectors_and_invalid_values(tmp_path):
    _make_drm_card(tmp_path, "card0", vendor="0x8086", vram=8 * GIB)
    _make_drm_card(tmp_path, "card1", vram="not-a-number")
    _make_drm_card(tmp_path, "card2", vram=0)
    _make_drm_card(tmp_path, "card3-eDP-1", vram=4 * GIB, boot_vga=1)

    assert hardware_memory.detect_linux_amd_gpu_memory_bytes(tmp_path) is None


def test_gpu_memory_override_can_lower_detected_budget(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_GPU_MEMORY_GB", "3.5")
    monkeypatch.setattr(
        hardware_memory.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="8192\n"),
    )

    assert hardware_memory.detect_total_gpu_memory_bytes() == int(3.5 * GIB)


def test_gpu_memory_override_cannot_exceed_detected_active_gpu(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_GPU_MEMORY_GB", "16")
    monkeypatch.setattr(
        hardware_memory.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="4096\n"),
    )

    assert hardware_memory.detect_total_gpu_memory_bytes() == 4 * GIB


def test_unverified_gpu_memory_override_is_used_when_detection_fails(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_GPU_MEMORY_GB", "2")
    monkeypatch.setattr(
        hardware_memory.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(hardware_memory.sys, "platform", "linux")
    monkeypatch.setattr(
        hardware_memory,
        "detect_linux_amd_gpu_memory_bytes",
        lambda: None,
    )

    assert hardware_memory.detect_total_gpu_memory_bytes() == 2 * GIB


def test_nvidia_detection_still_precedes_amd(monkeypatch):
    monkeypatch.setattr(
        hardware_memory.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="8192\n"),
    )

    def unexpected_amd_detection(*_args, **_kwargs):
        pytest.fail("AMD fallback should not run after NVIDIA detection succeeds")

    monkeypatch.setattr(
        hardware_memory,
        "detect_linux_amd_gpu_memory_bytes",
        unexpected_amd_detection,
    )

    assert hardware_memory.detect_total_gpu_memory_bytes() == 8192 * 1024 ** 2


def test_active_amd_vendor_skips_secondary_nvidia_gpu(monkeypatch):
    def unexpected_nvidia_detection(*_args, **_kwargs):
        pytest.fail("NVIDIA detection should not run for an active AMD context")

    monkeypatch.setattr(
        hardware_memory, "detect_nvidia_gpu_memory_bytes", unexpected_nvidia_detection
    )
    monkeypatch.setattr(hardware_memory.sys, "platform", "linux")
    monkeypatch.setattr(
        hardware_memory,
        "detect_linux_amd_gpu_memory_bytes",
        lambda: 2 * GIB,
    )

    assert hardware_memory.detect_total_gpu_memory_bytes("AMD") == 2 * GIB


def test_known_non_amd_vendor_does_not_use_amd_adapter(monkeypatch):
    def unexpected_detection(*_args, **_kwargs):
        pytest.fail("a secondary GPU should not supply the active GPU's budget")

    monkeypatch.setattr(
        hardware_memory, "detect_nvidia_gpu_memory_bytes", unexpected_detection
    )
    monkeypatch.setattr(
        hardware_memory, "detect_linux_amd_gpu_memory_bytes", unexpected_detection
    )

    assert (
        hardware_memory.detect_total_gpu_memory_bytes("Intel")
        == hardware_memory.UNKNOWN_GPU_MEMORY_FALLBACK_BYTES
    )


def test_unknown_gpu_uses_conservative_fallback(monkeypatch):
    monkeypatch.setattr(
        hardware_memory.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(hardware_memory.sys, "platform", "linux")
    monkeypatch.setattr(
        hardware_memory,
        "detect_linux_amd_gpu_memory_bytes",
        lambda: None,
    )

    assert (
        hardware_memory.detect_total_gpu_memory_bytes()
        == hardware_memory.UNKNOWN_GPU_MEMORY_FALLBACK_BYTES
    )


def test_unknown_windows_gpu_uses_larger_texture_friendly_fallback(monkeypatch):
    monkeypatch.setattr(hardware_memory.sys, "platform", "win32")

    assert (
        hardware_memory.detect_total_gpu_memory_bytes("Intel")
        == hardware_memory.WINDOWS_UNKNOWN_GPU_MEMORY_FALLBACK_BYTES
    )


def test_linux_amd_detection_is_used_when_nvidia_is_unavailable(monkeypatch):
    def no_nvidia(*_args, **_kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(hardware_memory.subprocess, "run", no_nvidia)
    monkeypatch.setattr(hardware_memory.sys, "platform", "linux")
    monkeypatch.setattr(
        hardware_memory,
        "detect_linux_amd_gpu_memory_bytes",
        lambda: 4 * GIB,
    )

    assert hardware_memory.detect_total_gpu_memory_bytes() == 4 * GIB


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [(None, 0.08), ("", 0.08), ("25", 0.25), ("0.5", 0.5), ("bad", 0.08)],
)
def test_ram_target_fraction_parsing(raw_value, expected):
    assert hardware_memory.parse_memory_target_fraction(raw_value) == expected
