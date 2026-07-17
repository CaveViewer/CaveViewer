"""Tests for texture decode/upload budgeting helpers."""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

from PIL import Image

from caveviewer.core.texture_manager import (
    DecodedImage,
    TEXTURE_MAX_SIZE_ENV_VAR,
    TextureManager,
)


GIB = 1024 ** 3


def _materials(count: int) -> dict[str, str]:
    return {f"mat_{index}": f"texture_{index}.jpg" for index in range(count)}


class FakeTextureContext:
    def __init__(self):
        self.uploads = []

    def texture(self, size, components, data):
        self.uploads.append((size, components, len(data)))
        return SimpleNamespace(build_mipmaps=lambda: None)


def test_recommend_texture_dimension_caps_large_texture_set_for_low_gpu_budget(
    monkeypatch,
):
    monkeypatch.delenv(TEXTURE_MAX_SIZE_ENV_VAR, raising=False)

    assert TextureManager.recommend_max_texture_dimension(
        _materials(100),
        gpu_memory_bytes=1 * GIB,
        gpu_target_fraction=0.70,
    ) == 1024


def test_recommend_texture_dimension_logs_auto_selection_details(
    monkeypatch, caplog
):
    monkeypatch.delenv(TEXTURE_MAX_SIZE_ENV_VAR, raising=False)
    caplog.set_level(logging.INFO, logger="caveviewer")

    assert TextureManager.recommend_max_texture_dimension(
        _materials(100),
        gpu_memory_bytes=1 * GIB,
        gpu_target_fraction=0.70,
    ) == 1024

    log_text = caplog.text
    assert "Texture max dimension cap auto-selected: 1024 px" in log_text
    assert "GPU budget 1.0 GB" in log_text
    assert "target 70%" in log_text
    assert "texture share 80%" in log_text
    assert "100 unique textures" in log_text
    assert "raw square limit" in log_text
    assert "next larger 2048 px step requires raw limit >= 1792 px" in log_text


def test_recommend_texture_dimension_keeps_4k_for_larger_gpu_budget(monkeypatch):
    monkeypatch.delenv(TEXTURE_MAX_SIZE_ENV_VAR, raising=False)

    assert TextureManager.recommend_max_texture_dimension(
        _materials(100),
        gpu_memory_bytes=16 * GIB,
        gpu_target_fraction=0.70,
    ) == 4096


def test_recommend_texture_dimension_allows_16k_for_large_gpu_budget(monkeypatch):
    monkeypatch.delenv(TEXTURE_MAX_SIZE_ENV_VAR, raising=False)

    assert TextureManager.recommend_max_texture_dimension(
        _materials(1),
        gpu_memory_bytes=24 * GIB,
        gpu_target_fraction=0.70,
    ) == 16384


def test_manager_logs_maximum_configured_limit_without_cap_warning(caplog):
    caplog.set_level(logging.INFO, logger="caveviewer")

    TextureManager(
        object(),
        "",
        {},
        max_texture_dimension=16384,
    )

    assert "maximum configured texture dimension: 16384 px" in caplog.text
    assert "Texture max dimension cap active" not in caplog.text


def test_explicit_texture_dimension_limit_overrides_auto_recommendation(monkeypatch):
    monkeypatch.setenv(TEXTURE_MAX_SIZE_ENV_VAR, "1536")

    assert TextureManager.recommend_max_texture_dimension(
        _materials(100),
        gpu_memory_bytes=16 * GIB,
        gpu_target_fraction=0.70,
    ) == 1536


def test_explicit_texture_dimension_limit_logs_env_selection(
    monkeypatch, caplog
):
    monkeypatch.setenv(TEXTURE_MAX_SIZE_ENV_VAR, "1536")
    caplog.set_level(logging.INFO, logger="caveviewer")

    assert TextureManager.recommend_max_texture_dimension(
        _materials(100),
        gpu_memory_bytes=16 * GIB,
        gpu_target_fraction=0.70,
    ) == 1536

    assert (
        "Texture max dimension cap selected from "
        "CAVEVIEWER_MAX_TEXTURE_SIZE='1536': 1536 px"
    ) in caplog.text


def test_decode_downscales_oversized_texture(tmp_path):
    texture_path = tmp_path / "tile.png"
    Image.new("RGB", (1024, 512), color=(10, 20, 30)).save(texture_path)
    manager = TextureManager(
        object(),
        str(tmp_path),
        {"mat": "tile.png"},
        max_texture_dimension=512,
    )

    decoded = manager._decode_from_disk("tile.png")

    assert decoded is not None
    assert decoded.size == (512, 256)
    assert len(decoded.data) == 512 * 256 * 3


def test_predecode_skips_texture_that_exceeds_decode_cache_cap(tmp_path):
    Image.new("RGB", (64, 64), color=(10, 20, 30)).save(tmp_path / "large.png")
    manager = TextureManager(
        object(),
        str(tmp_path),
        {"mat": "large.png"},
        max_decoded_cache_bytes=1024,
    )

    manager.decode_for_material("mat")

    assert manager.stats()["decoded_waiting_for_upload"] == 0
    assert manager.stats()["decoded_waiting_bytes"] == 0


def test_predecoded_texture_bytes_are_released_on_upload(tmp_path):
    Image.new("RGB", (16, 16), color=(10, 20, 30)).save(tmp_path / "tile.png")
    context = FakeTextureContext()
    manager = TextureManager(
        context,
        str(tmp_path),
        {"mat": "tile.png"},
        max_decoded_cache_bytes=4096,
    )

    manager.decode_for_material("mat")

    assert manager.stats()["decoded_waiting_for_upload"] == 1
    assert manager.stats()["decoded_waiting_bytes"] == 16 * 16 * 3

    manager.acquire("mat")

    assert manager.stats()["decoded_waiting_for_upload"] == 0
    assert manager.stats()["decoded_waiting_bytes"] == 0
    assert context.uploads == [((16, 16), 3, 16 * 16 * 3)]


def test_acquire_with_timing_reports_predecoded_upload(tmp_path):
    Image.new("RGB", (16, 8), color=(10, 20, 30)).save(tmp_path / "tile.png")
    context = FakeTextureContext()
    manager = TextureManager(
        context,
        str(tmp_path),
        {"mat": "tile.png"},
        max_decoded_cache_bytes=4096,
    )

    manager.decode_for_material("mat")
    _texture, timing = manager.acquire_with_timing("mat")

    assert timing["material"] == "mat"
    assert timing["decoded_cache_hit"] is True
    assert timing["sync_decode"] is False
    assert timing["image_size"] == (16, 8)
    assert timing["image_bytes"] == 16 * 8 * 3
    assert timing["texture_ms"] >= 0.0
    assert timing["mipmap_ms"] >= 0.0
    assert timing["total_ms"] >= 0.0
    assert context.uploads == [((16, 8), 3, 16 * 8 * 3)]


def test_predecode_waits_for_shared_texture_already_inflight(tmp_path, monkeypatch):
    context = FakeTextureContext()
    manager = TextureManager(
        context,
        str(tmp_path),
        {"mat": "shared.png"},
        max_decoded_cache_bytes=4096,
    )
    file_key = "shared.png"

    def unexpected_decode(_file_or_bytes):
        raise AssertionError("waiting worker should not start a duplicate decode")

    monkeypatch.setattr(manager, "_decode_image", unexpected_decode)

    with manager._decode_cache_lock:
        manager._decode_inflight.add(file_key)

    waiter = threading.Thread(
        target=manager.decode_for_material,
        args=("mat",),
        daemon=True,
    )
    waiter.start()
    time.sleep(0.05)

    assert waiter.is_alive()

    decoded = DecodedImage(
        size=(4, 4),
        components=3,
        data=bytes(4 * 4 * 3),
    )
    with manager._decode_cache_lock:
        manager._decode_inflight.discard(file_key)
        manager._decode_cache[file_key] = decoded
        manager._decode_cache_bytes += len(decoded.data)
        manager._decode_cache_lock.notify_all()

    waiter.join(timeout=1.0)

    assert not waiter.is_alive()

    _texture, timing = manager.acquire_with_timing("mat")

    assert timing["decoded_cache_hit"] is True
    assert timing["sync_decode"] is False
    assert context.uploads == [((4, 4), 3, 4 * 4 * 3)]


def test_validate_textures_logs_when_no_downscale_will_be_applied(
    tmp_path, caplog
):
    Image.new("RGB", (512, 256), color=(10, 20, 30)).save(tmp_path / "a.png")
    Image.new("RGB", (256, 256), color=(40, 50, 60)).save(tmp_path / "b.png")
    caplog.set_level(logging.INFO, logger="caveviewer")
    manager = TextureManager(
        object(),
        str(tmp_path),
        {"a": "a.png", "b": "b.png"},
        max_texture_dimension=1024,
    )

    manager.validate_textures()

    log_text = caplog.text
    assert "No texture downscaling will be applied for this map" in log_text
    assert "GPU resources allow all 2 inspected texture(s)" in log_text
    assert "largest source texture 'a.png' is 512x256" in log_text
    assert "selected limit 1024 px" in log_text


def test_validate_textures_logs_when_downscale_is_expected(tmp_path, caplog):
    Image.new("RGB", (1024, 512), color=(10, 20, 30)).save(tmp_path / "large.png")
    Image.new("RGB", (256, 256), color=(40, 50, 60)).save(tmp_path / "small.png")
    caplog.set_level(logging.INFO, logger="caveviewer")
    manager = TextureManager(
        object(),
        str(tmp_path),
        {"large": "large.png", "small": "small.png"},
        max_texture_dimension=512,
    )

    manager.validate_textures()

    log_text = caplog.text
    assert "No texture downscaling will be applied for this map" not in log_text
    assert "Texture downscaling will be applied to 1 of 2 inspected texture(s)" in log_text
    assert "selected 512 px limit" in log_text
