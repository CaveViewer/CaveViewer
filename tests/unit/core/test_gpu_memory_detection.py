from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import streaming_world


GIB = 1024 ** 3


def _make_drm_card(
    drm_root,
    name: str,
    *,
    vendor: str = "0x1002",
    vram: str | int = GIB,
    boot_vga: str | int | None = None,
):
    device = drm_root / name / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text(str(vendor), encoding="ascii")
    (device / "mem_info_vram_total").write_text(str(vram), encoding="ascii")
    if boot_vga is not None:
        (device / "boot_vga").write_text(str(boot_vga), encoding="ascii")


def test_linux_amd_detection_prefers_primary_adapter(tmp_path):
    _make_drm_card(
        tmp_path, "card0", vendor="0x8086", vram=16 * GIB, boot_vga=1
    )
    _make_drm_card(tmp_path, "card1", vram=GIB, boot_vga=1)
    _make_drm_card(tmp_path, "card2", vram=8 * GIB, boot_vga=0)

    assert streaming_world._detect_linux_amd_gpu_memory_bytes(tmp_path) == GIB


def test_linux_amd_detection_uses_largest_adapter_without_primary(tmp_path):
    _make_drm_card(tmp_path, "card0", vram=2 * GIB)
    _make_drm_card(tmp_path, "card1", vram=8 * GIB, boot_vga=0)

    assert streaming_world._detect_linux_amd_gpu_memory_bytes(tmp_path) == 8 * GIB


def test_linux_amd_detection_ignores_connectors_and_invalid_values(tmp_path):
    _make_drm_card(tmp_path, "card0", vendor="0x8086", vram=8 * GIB)
    _make_drm_card(tmp_path, "card1", vram="not-a-number")
    _make_drm_card(tmp_path, "card2", vram=0)
    _make_drm_card(tmp_path, "card3-eDP-1", vram=4 * GIB, boot_vga=1)

    assert streaming_world._detect_linux_amd_gpu_memory_bytes(tmp_path) is None


def test_gpu_memory_override_takes_precedence(monkeypatch):
    monkeypatch.setenv("CAVEVIEWER_GPU_MEMORY_GB", "3.5")

    def unexpected_detection(*_args, **_kwargs):
        pytest.fail("automatic GPU detection should not run with an override")

    monkeypatch.setattr(streaming_world.subprocess, "run", unexpected_detection)
    monkeypatch.setattr(
        streaming_world, "_detect_linux_amd_gpu_memory_bytes", unexpected_detection
    )

    assert streaming_world._detect_total_gpu_memory_bytes() == int(3.5 * GIB)


def test_nvidia_detection_still_precedes_amd(monkeypatch):
    monkeypatch.setattr(
        streaming_world.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="8192\n"),
    )

    def unexpected_amd_detection(*_args, **_kwargs):
        pytest.fail("AMD fallback should not run after NVIDIA detection succeeds")

    monkeypatch.setattr(
        streaming_world,
        "_detect_linux_amd_gpu_memory_bytes",
        unexpected_amd_detection,
    )

    assert streaming_world._detect_total_gpu_memory_bytes() == 8192 * 1024 ** 2


def test_active_amd_vendor_skips_secondary_nvidia_gpu(monkeypatch):
    def unexpected_nvidia_detection(*_args, **_kwargs):
        pytest.fail("NVIDIA detection should not run for an active AMD context")

    monkeypatch.setattr(
        streaming_world, "_detect_nvidia_gpu_memory_bytes", unexpected_nvidia_detection
    )
    monkeypatch.setattr(streaming_world.sys, "platform", "linux")
    monkeypatch.setattr(
        streaming_world,
        "_detect_linux_amd_gpu_memory_bytes",
        lambda: 2 * GIB,
    )

    assert streaming_world._detect_total_gpu_memory_bytes("AMD") == 2 * GIB


def test_known_non_amd_vendor_does_not_use_amd_adapter(monkeypatch):
    def unexpected_detection(*_args, **_kwargs):
        pytest.fail("a secondary GPU should not supply the active GPU's budget")

    monkeypatch.setattr(
        streaming_world, "_detect_nvidia_gpu_memory_bytes", unexpected_detection
    )
    monkeypatch.setattr(
        streaming_world, "_detect_linux_amd_gpu_memory_bytes", unexpected_detection
    )

    assert streaming_world._detect_total_gpu_memory_bytes("Intel") is None


def test_linux_amd_detection_is_used_when_nvidia_is_unavailable(monkeypatch):
    def no_nvidia(*_args, **_kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(streaming_world.subprocess, "run", no_nvidia)
    monkeypatch.setattr(streaming_world.sys, "platform", "linux")
    monkeypatch.setattr(
        streaming_world,
        "_detect_linux_amd_gpu_memory_bytes",
        lambda: 4 * GIB,
    )

    assert streaming_world._detect_total_gpu_memory_bytes() == 4 * GIB
