"""Tests for worker-safe CPU texture decoding state."""

from __future__ import annotations

import threading
import time

from PIL import Image

import pytest

from caveviewer.core.textures.decoding import (
    DecodedImage,
    TextureDecodeCache,
    resolve_texture_path,
)


def test_resolve_texture_path_rejects_paths_outside_texture_root(tmp_path):
    assert resolve_texture_path(str(tmp_path), "tiles/rock.png") == str(
        tmp_path / "tiles" / "rock.png"
    )

    for unsafe_path in ("../rock.png", "/tmp/rock.png"):
        with pytest.raises(ValueError, match="Unsafe texture path"):
            resolve_texture_path(str(tmp_path), unsafe_path)


def test_texture_decode_cache_drops_waiting_payload_when_marked_resident(
    tmp_path, monkeypatch
):
    Image.new("RGB", (8, 8), color=(10, 20, 30)).save(tmp_path / "tile.png")
    cache = TextureDecodeCache(
        str(tmp_path),
        {"mat": "tile.png"},
        max_decoded_cache_bytes=4096,
    )

    cache.decode_for_material("mat")
    assert cache.stats()["decoded_waiting_for_upload"] == 1

    cache.mark_texture_resident("tile.png")
    assert cache.stats()["decoded_waiting_for_upload"] == 0

    decode_calls = []

    def decode_source(_source):
        decode_calls.append(_source)
        return DecodedImage(size=(8, 8), components=3, data=bytes(8 * 8 * 3))

    monkeypatch.setattr(cache, "decode_source", decode_source)
    cache.decode_for_material("mat")

    assert decode_calls == []
    assert cache.stats()["decoded_waiting_for_upload"] == 0


def test_texture_decode_cache_shutdown_unblocks_inflight_waiters(tmp_path):
    cache = TextureDecodeCache(
        str(tmp_path),
        {"mat": "shared.png"},
        max_decoded_cache_bytes=4096,
    )

    with cache._decode_cache_lock:
        cache._decode_inflight.add("shared.png")

    waiter = threading.Thread(
        target=cache.decode_for_material,
        args=("mat",),
        daemon=True,
    )
    waiter.start()
    time.sleep(0.05)

    assert waiter.is_alive()

    cache.shutdown()
    waiter.join(timeout=1.0)

    assert not waiter.is_alive()


def test_decode_cache_limit_log_runs_outside_condition_lock(tmp_path, monkeypatch):
    cache = TextureDecodeCache(
        str(tmp_path),
        {"mat": "tile.png"},
        max_decoded_cache_bytes=1,
    )
    monkeypatch.setattr(cache, "_estimate_decoded_image_bytes", lambda _source: 2)

    lock_owned_states = []

    def log_decode_cache_skip(_source, _estimated_bytes):
        lock_owned_states.append(cache._decode_cache_lock._is_owned())

    monkeypatch.setattr(cache, "_log_decode_cache_skip", log_decode_cache_skip)

    cache.decode_for_material("mat")

    assert lock_owned_states == [False]
