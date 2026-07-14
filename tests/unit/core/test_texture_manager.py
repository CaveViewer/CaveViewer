"""Tests for texture decode/upload budgeting helpers."""

from __future__ import annotations

import logging

from PIL import Image

from caveviewer.core.texture_manager import (
    TEXTURE_MAX_SIZE_ENV_VAR,
    TextureManager,
)


GIB = 1024 ** 3


def _materials(count: int) -> dict[str, str]:
    return {f"mat_{index}": f"texture_{index}.jpg" for index in range(count)}


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
