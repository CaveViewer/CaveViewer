"""Map-scoped viewer runtime ownership and cleanup tests."""

from __future__ import annotations

import logging

import pytest

from caveviewer.gui import viewer_window
from caveviewer.gui.viewer_map_runtime import ViewerMapRuntime


class _ResourceProbe:
    def __init__(self, calls, name):
        self.calls = calls
        self.name = name

    def release(self):
        self.calls.append(self.name)


def test_map_runtime_releases_owned_resources_in_lifetime_order():
    calls = []

    class FutureProbe:
        def cancel(self):
            calls.append("cancel_validation")

    class ExecutorProbe:
        def shutdown(self, *, wait, cancel_futures):
            calls.append(("shutdown_validation", wait, cancel_futures))

    class WorldProbe:
        def shutdown(self, *, timeout):
            calls.append(("shutdown_world", timeout))

    class UploadProbe:
        def unload_all(self):
            calls.append("unload_gpu_chunks")

    class TextureProbe:
        def shutdown(self):
            calls.append("shutdown_textures")

    runtime = ViewerMapRuntime(
        cache_dir="/cache/cave",
        textures_dir="/maps/cave",
        manifest={"chunks": {}},
        world=WorldProbe(),
        minimap=_ResourceProbe(calls, "release_minimap"),
        texture_manager=TextureProbe(),
        chunk_upload_manager=UploadProbe(),
        texture_validation_future=FutureProbe(),
        texture_validation_executor=ExecutorProbe(),
        loaded=True,
    )

    runtime.release(streaming_shutdown_timeout=2.0, logger=logging.getLogger())

    assert calls == [
        "cancel_validation",
        ("shutdown_validation", False, True),
        ("shutdown_world", 2.0),
        "unload_gpu_chunks",
        "shutdown_textures",
        "release_minimap",
    ]
    assert runtime.loaded is False
    assert runtime.world is None
    assert runtime.texture_manager is None
    assert runtime.chunk_upload_manager is None
    assert runtime.minimap is None


def test_map_runtime_finishes_cleanup_after_streaming_shutdown_failure():
    calls = []

    class WorldProbe:
        def shutdown(self, *, timeout):
            calls.append(("shutdown_world", timeout))
            raise OSError("streaming worker failed")

    class UploadProbe:
        def unload_all(self):
            calls.append("unload_gpu_chunks")

    class TextureProbe:
        def shutdown(self):
            calls.append("shutdown_textures")

    runtime = ViewerMapRuntime(
        world=WorldProbe(),
        minimap=_ResourceProbe(calls, "release_minimap"),
        texture_manager=TextureProbe(),
        chunk_upload_manager=UploadProbe(),
        loaded=True,
    )

    with pytest.raises(OSError, match="streaming worker failed"):
        runtime.release(
            streaming_shutdown_timeout=1.5,
            logger=logging.getLogger(),
        )

    assert calls == [
        ("shutdown_world", 1.5),
        "unload_gpu_chunks",
        "shutdown_textures",
        "release_minimap",
    ]
    assert runtime.loaded is False
    assert runtime.world is None


def test_failed_map_load_releases_partial_runtime_and_restores_pending_trace(
    monkeypatch,
):
    calls = []
    pending_trace = object()
    window = object.__new__(viewer_window.CaveViewerWindow)
    window._pending_recorded_dive_trace = pending_trace
    window._recorded_dive_trace = None
    window._recorded_dive_controller = None

    class WorldProbe:
        def shutdown(self, *, timeout):
            calls.append(("shutdown_world", timeout))

    class TextureProbe:
        def shutdown(self):
            calls.append("shutdown_textures")

    def fail_initialization(active_window):
        active_window.world = WorldProbe()
        active_window.texture_manager = TextureProbe()
        raise OSError("minimap construction failed")

    monkeypatch.setattr(
        viewer_window.CaveViewerWindow,
        "_initialize_map_runtime",
        fail_initialization,
    )

    with pytest.raises(OSError, match="minimap construction failed"):
        window._load_map(
            "/cache/cave",
            "/maps/cave",
            {"chunks": {}, "mtl_materials": {}},
            map_root="/maps/cave",
        )

    assert calls == [
        (
            "shutdown_world",
            viewer_window._VIEWER_STREAMING_SHUTDOWN_TIMEOUT_SECONDS,
        ),
        "shutdown_textures",
    ]
    assert window._map_runtime.loaded is False
    assert window._map_runtime.cache_dir is None
    assert window._pending_recorded_dive_trace is pending_trace
