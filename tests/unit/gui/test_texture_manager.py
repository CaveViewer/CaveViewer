"""Tests for texture decode/upload budgeting helpers."""

from __future__ import annotations

import logging
import math
import threading
import time

from PIL import Image
import pytest

from caveviewer.core.textures import decoding as texture_decode_module
from caveviewer.core.textures.decoding import (
    DecodedImage,
    TEXTURE_MAX_SIZE_ENV_VAR,
    TextureDecodeCache,
)
from caveviewer.gui.texture_manager import TextureManager


GIB = 1024 ** 3


def _materials(count: int) -> dict[str, str]:
    return {f"mat_{index}": f"texture_{index}.jpg" for index in range(count)}


class FakeTextureContext:
    def __init__(self):
        self.uploads = []
        self.allocations = []
        self.textures = []

    def texture(self, size, components, data=None):
        byte_count = 0 if data is None else len(data)
        if data is None:
            self.allocations.append((size, components))
        else:
            self.uploads.append((size, components, byte_count))
        texture = FakeTexture(size=size, components=components, byte_count=byte_count)
        self.textures.append(texture)
        return texture


class FakeTexture:
    def __init__(self, *, size, components, byte_count):
        self.size = size
        self.components = components
        self.byte_count = byte_count
        self.mipmaps_built = False
        self.released = False
        self.writes = []

    def write(self, data, viewport=None):
        self.writes.append((viewport, len(data)))
        self.byte_count += len(data)

    def build_mipmaps(self):
        self.mipmaps_built = True

    def release(self):
        self.released = True


class _LockProbe:
    def __init__(self):
        self._depth = 0

    @property
    def locked(self) -> bool:
        return self._depth > 0

    def __enter__(self):
        self._depth += 1
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self._depth -= 1


class _LockCheckedFalse:
    def __init__(self, lock: _LockProbe):
        self._lock = lock

    def __bool__(self):
        assert self._lock.locked
        return False


def test_texture_manager_owns_material_mapping_snapshot():
    materials = {"mat": "original.png"}
    manager = TextureManager(object(), "", materials)

    materials["mat"] = "mutated.png"

    assert manager.material_to_file["mat"] == "original.png"


def test_texture_decode_cache_owns_material_mapping_snapshot():
    materials = {"mat": "original.png"}
    cache = TextureDecodeCache("", materials)

    materials["mat"] = "mutated.png"

    assert cache.material_to_file["mat"] == "original.png"


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


def test_recommend_resident_texture_cache_uses_texture_budget_slice():
    assert TextureManager.recommend_resident_texture_cache_bytes(
        gpu_memory_bytes=10 * GIB,
        gpu_target_fraction=0.50,
    ) == 4 * GIB


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


def test_decode_rejects_oversized_texture_before_full_convert(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "huge.png").write_bytes(b"not actually decoded")
    manager = TextureManager(
        object(),
        str(tmp_path),
        {"mat": "huge.png"},
    )

    class FakeLargeImage:
        size = (1_000_000, 1_000_000)

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def convert(self, *_args, **_kwargs):
            raise AssertionError("oversized image must be rejected before convert")

    monkeypatch.setattr(
        texture_decode_module.Image,
        "open",
        lambda _path: FakeLargeImage(),
    )

    assert manager._decode_from_disk("huge.png") is None


def test_downscale_log_flag_is_checked_under_state_lock():
    context = FakeTextureContext()
    manager = TextureManager(
        context,
        "",
        {},
        max_texture_dimension=512,
    )
    lock = _LockProbe()
    manager.decoder._state_lock = lock
    manager.decoder._texture_downscale_logged = _LockCheckedFalse(lock)

    image = Image.new("RGB", (1024, 1024))
    manager._apply_texture_dimension_limit(image, "oversized.png")

    assert manager.decoder._texture_downscale_logged is True


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


def test_incremental_acquire_uploads_texture_in_row_bands(tmp_path):
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(tmp_path / "tile.png")
    context = FakeTextureContext()
    manager = TextureManager(
        context,
        str(tmp_path),
        {"mat": "tile.png"},
        max_decoded_cache_bytes=4096,
    )
    manager.decode_for_material("mat")

    task = manager.begin_acquire_with_timing("mat")

    assert task.complete is False

    texture, first_timing, complete = manager.advance_acquire_with_timing(
        task,
        max_upload_bytes=4 * 3 * 2,
    )

    assert texture is None
    assert complete is False
    assert first_timing["decoded_cache_hit"] is True
    assert first_timing["image_bytes"] == 0
    assert first_timing["texture_alloc_ms"] >= 0.0
    assert first_timing["texture_write_ms"] == 0.0
    assert context.allocations == [((4, 4), 3)]
    assert context.uploads == []
    assert context.textures[0].writes == []

    texture, second_timing, complete = manager.advance_acquire_with_timing(
        task,
        max_upload_bytes=4 * 3 * 2,
    )

    assert texture is None
    assert complete is False
    assert second_timing["image_bytes"] == 4 * 3 * 2
    assert second_timing["texture_alloc_ms"] == 0.0
    assert second_timing["texture_write_ms"] >= 0.0
    assert context.textures[0].writes[-1] == ((0, 0, 4, 2), 4 * 3 * 2)

    texture, third_timing, complete = manager.advance_acquire_with_timing(
        task,
        max_upload_bytes=4 * 3 * 2,
    )

    assert complete is True
    assert texture is context.textures[0]
    assert third_timing["image_bytes"] == 4 * 3 * 2
    assert context.textures[0].writes[-1] == ((0, 2, 4, 2), 4 * 3 * 2)
    assert context.textures[0].mipmaps_built is True
    assert manager.stats()["unique_materials_loaded"] == 1


def test_cancel_incremental_acquire_releases_partial_texture(tmp_path):
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(tmp_path / "tile.png")
    context = FakeTextureContext()
    manager = TextureManager(
        context,
        str(tmp_path),
        {"mat": "tile.png"},
        max_decoded_cache_bytes=4096,
    )
    manager.decode_for_material("mat")
    task = manager.begin_acquire_with_timing("mat")

    _texture, _timing, complete = manager.advance_acquire_with_timing(
        task,
        max_upload_bytes=4 * 3,
    )

    assert complete is False

    manager.cancel_acquire_task(task)

    assert context.textures[0].released is True
    assert manager.stats()["unique_materials_loaded"] == 0


def test_release_keeps_texture_idle_for_lru_reuse(tmp_path):
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(tmp_path / "tile.png")
    context = FakeTextureContext()
    manager = TextureManager(
        context,
        str(tmp_path),
        {"mat": "tile.png"},
        max_resident_texture_bytes=4096,
    )

    first_texture = manager.acquire("mat")
    manager.release("mat")

    assert manager.stats()["unique_materials_loaded"] == 0
    assert manager.stats()["unique_files_resident"] == 1
    assert manager.stats()["idle_files_resident"] == 1
    assert context.textures[0].released is False

    second_texture, timing = manager.acquire_with_timing("mat")

    assert second_texture is first_texture
    assert timing["file_cache_hit"] is True
    assert timing["texture_ms"] == 0.0
    assert len(context.uploads) == 1
    assert manager.stats()["unique_materials_loaded"] == 1
    assert manager.stats()["idle_files_resident"] == 0


def test_shared_texture_file_counts_once_in_resident_budget(tmp_path):
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(tmp_path / "shared.png")
    context = FakeTextureContext()
    manager = TextureManager(
        context,
        str(tmp_path),
        {"a": "shared.png", "b": "shared.png"},
        max_resident_texture_bytes=4096,
    )

    manager.acquire("a")
    _texture, timing = manager.acquire_with_timing("b")

    stats = manager.stats()
    assert len(context.uploads) == 1
    assert timing["file_cache_hit"] is True
    assert stats["unique_materials_loaded"] == 2
    assert stats["unique_files_resident"] == 1
    assert stats["resident_texture_bytes"] == math.ceil(4 * 4 * 4 * (4.0 / 3.0))


def test_idle_texture_lru_evicts_oldest_when_budget_needs_room(tmp_path):
    for name, color in (
        ("a.png", (10, 20, 30)),
        ("b.png", (40, 50, 60)),
        ("c.png", (70, 80, 90)),
    ):
        Image.new("RGB", (4, 4), color=color).save(tmp_path / name)
    context = FakeTextureContext()
    manager = TextureManager(
        context,
        str(tmp_path),
        {"a": "a.png", "b": "b.png", "c": "c.png"},
        max_resident_texture_bytes=200,
    )

    manager.acquire("a")
    manager.release("a")
    manager.acquire("b")
    manager.release("b")

    assert manager.stats()["unique_files_resident"] == 2
    assert manager.stats()["idle_files_resident"] == 2
    assert context.textures[0].released is False
    assert context.textures[1].released is False

    _third_texture, timing = manager.acquire_with_timing("c")

    assert len(context.uploads) == 3
    assert context.textures[0].released is True
    assert context.textures[1].released is False
    assert context.textures[2].released is False
    assert timing["texture_evictions"] == 1
    assert timing["texture_evicted_bytes"] == math.ceil(4 * 4 * 4 * (4.0 / 3.0))
    assert manager.stats()["unique_files_resident"] == 2
    assert manager.stats()["idle_files_resident"] == 1


def test_cached_texture_release_happens_outside_state_lock(tmp_path):
    context = FakeTextureContext()
    manager = TextureManager(
        context,
        str(tmp_path),
        {"mat": "tile.png"},
    )
    texture = FakeTexture(size=(1, 1), components=4, byte_count=4)
    manager._file_cache["tile.png"] = texture
    manager._file_cache_bytes["tile.png"] = 4
    manager._file_cache_total_bytes = 4
    manager._idle_file_lru["tile.png"] = None
    lock = _LockProbe()
    release_lock_states = []

    def release():
        release_lock_states.append(lock.locked)
        texture.released = True

    texture.release = release
    manager._state_lock = lock

    manager._release_cached_texture("tile.png")

    assert release_lock_states == [False]
    assert texture.released is True


def test_placeholder_release_happens_outside_state_lock(tmp_path):
    context = FakeTextureContext()
    manager = TextureManager(
        context,
        str(tmp_path),
        {"mat": None},
    )
    texture = manager.acquire("mat")
    lock = _LockProbe()
    release_lock_states = []

    def release():
        release_lock_states.append(lock.locked)
        texture.released = True

    texture.release = release
    manager._state_lock = lock

    manager.release("mat")

    assert release_lock_states == [False]
    assert texture.released is True


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

    monkeypatch.setattr(manager.decoder, "decode_source", unexpected_decode)

    with manager.decoder._decode_cache_lock:
        manager.decoder._decode_inflight.add(file_key)

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
    with manager.decoder._decode_cache_lock:
        manager.decoder._decode_inflight.discard(file_key)
        manager.decoder._decode_cache[file_key] = decoded
        manager.decoder._decode_cache_bytes += len(decoded.data)
        manager.decoder._decode_cache_lock.notify_all()

    waiter.join(timeout=1.0)

    assert not waiter.is_alive()

    _texture, timing = manager.acquire_with_timing("mat")

    assert timing["decoded_cache_hit"] is True
    assert timing["sync_decode"] is False
    assert context.uploads == [((4, 4), 3, 4 * 4 * 3)]


def test_predecode_exception_clears_inflight_and_notifies_waiters(
    tmp_path,
    monkeypatch,
):
    manager = TextureManager(
        object(),
        str(tmp_path),
        {"mat": "bad.png"},
        max_decoded_cache_bytes=4096,
    )
    monkeypatch.setattr(
        manager.decoder, "_estimate_decoded_image_bytes", lambda _file: 128
    )

    def fail_decode(_file_or_bytes):
        raise RuntimeError("decoder bug")

    monkeypatch.setattr(manager.decoder, "decode_source", fail_decode)

    manager.decode_for_material("mat")

    with manager.decoder._decode_cache_lock:
        assert "bad.png" not in manager.decoder._decode_inflight
        assert manager.decoder._decode_inflight_reserved_bytes == 0


def test_shutdown_unblocks_predecode_waiting_on_inflight_texture(tmp_path):
    context = FakeTextureContext()
    manager = TextureManager(
        context,
        str(tmp_path),
        {"mat": "shared.png"},
        max_decoded_cache_bytes=4096,
    )
    file_key = "shared.png"

    with manager.decoder._decode_cache_lock:
        manager.decoder._decode_inflight.add(file_key)

    waiter = threading.Thread(
        target=manager.decode_for_material,
        args=("mat",),
        daemon=True,
    )
    waiter.start()
    time.sleep(0.05)

    assert waiter.is_alive()

    manager.shutdown()
    waiter.join(timeout=1.0)

    assert not waiter.is_alive()
    assert manager.stats()["decoded_waiting_for_upload"] == 0


def test_decode_inflight_reservation_is_not_reported_as_waiting_bytes(tmp_path):
    manager = TextureManager(
        object(),
        str(tmp_path),
        {"mat": "shared.png"},
        max_decoded_cache_bytes=4096,
    )
    file_key = "shared.png"

    with manager.decoder._decode_cache_lock:
        manager.decoder._decode_inflight.add(file_key)
        manager.decoder._decode_inflight_reserved_bytes = 512

    stats = manager.stats()

    assert stats["decoded_waiting_for_upload"] == 0
    assert stats["decoded_waiting_bytes"] == 0
    assert stats["decode_inflight_reserved_bytes"] == 512


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


def test_validate_textures_populates_size_cache_for_later_estimates(
    tmp_path, monkeypatch
):
    Image.new("RGB", (512, 256), color=(10, 20, 30)).save(tmp_path / "tile.png")
    manager = TextureManager(
        object(),
        str(tmp_path),
        {"mat": "tile.png"},
        max_texture_dimension=512,
    )
    original_open = texture_decode_module.Image.open
    open_count = 0

    def counting_open(*args, **kwargs):
        nonlocal open_count
        open_count += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr(texture_decode_module.Image, "open", counting_open)

    manager.validate_textures()
    assert open_count == 1

    assert manager._estimate_resident_bytes_for_file("tile.png") == math.ceil(
        512 * 256 * 4 * (4.0 / 3.0)
    )
    assert open_count == 1


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


def test_texture_manager_rejects_gpu_work_from_non_owner_thread(tmp_path):
    manager = TextureManager(
        FakeTextureContext(),
        str(tmp_path),
        {"mat": None},
    )
    errors = []

    def acquire_from_worker():
        with pytest.raises(RuntimeError) as exc_info:
            manager.acquire("mat")
        errors.append(str(exc_info.value))

    worker = threading.Thread(target=acquire_from_worker)
    worker.start()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert "must run on the render thread" in errors[0]
