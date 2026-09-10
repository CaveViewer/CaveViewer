"""Tests for the session-scoped render-thread capture runtime."""

from __future__ import annotations

import queue
from types import SimpleNamespace

from caveviewer.gui.viewer_capture_runtime import ViewerCaptureRuntime


class Releasable:
    def __init__(self) -> None:
        self.release_calls = 0

    def release(self) -> None:
        self.release_calls += 1


def test_recording_resources_are_single_owned_and_refresh_dependencies():
    runtime = ViewerCaptureRuntime()
    first_context = SimpleNamespace()
    second_context = SimpleNamespace()

    resources = runtime.ensure_recording_resources(
        ctx=first_context,
        buffer_count=2,
        readback_components=3,
        logger=SimpleNamespace(),
        perf_counter=lambda: 1.0,
    )
    same_resources = runtime.ensure_recording_resources(
        ctx=second_context,
        buffer_count=4,
        readback_components=4,
        logger=SimpleNamespace(),
        perf_counter=lambda: 2.0,
    )

    assert same_resources is resources
    assert resources.ctx is second_context
    assert resources.buffer_count == 4
    assert resources.readback_components == 4


def test_attach_and_detach_recording_publish_one_coherent_capture_state():
    runtime = ViewerCaptureRuntime()
    resources = runtime.ensure_recording_resources(
        ctx=SimpleNamespace(),
        buffer_count=3,
        readback_components=3,
        logger=SimpleNamespace(),
        perf_counter=lambda: 1.0,
    )
    frame_queue = queue.Queue()
    session = SimpleNamespace(
        output_path="dive.mp4",
        frame_queue=frame_queue,
        output_size=(1280, 720),
        viewport=(0, 0, 1920, 1080),
    )
    framebuffer = SimpleNamespace()

    runtime.attach_recording(session, readback_framebuffer=framebuffer)

    assert runtime.recording_session is session
    assert runtime.recording_output_path == "dive.mp4"
    assert runtime.recording_frame_queue is frame_queue
    assert resources.output_size == (1280, 720)
    assert resources.capture_viewport == (0, 0, 1920, 1080)
    assert resources.readback_framebuffer is framebuffer

    assert runtime.detach_recording() is session
    assert runtime.detach_recording() is None
    assert runtime.recording_output_path is None
    assert runtime.recording_frame_queue is None
    assert resources.output_size is None
    assert resources.capture_viewport is None


def test_release_recording_resources_is_idempotent():
    runtime = ViewerCaptureRuntime()
    resources = runtime.ensure_recording_resources(
        ctx=SimpleNamespace(),
        buffer_count=1,
        readback_components=3,
        logger=SimpleNamespace(),
        perf_counter=lambda: 1.0,
    )
    buffer = Releasable()
    framebuffer = Releasable()
    resources.readback_slots = [SimpleNamespace(buffer=buffer, in_flight=True)]
    resources.readback_pending = list(resources.readback_slots)
    resources.readback_byte_count = 12
    resources.readback_framebuffer = framebuffer

    runtime.release_recording_resources()
    runtime.release_recording_resources()

    assert buffer.release_calls == 1
    assert framebuffer.release_calls == 1
    assert resources.readback_slots == []
    assert resources.readback_pending == []


def test_clear_slice_context_preserves_completed_reveal_handoff():
    runtime = ViewerCaptureRuntime(
        slice_source_cache_dir="cache",
        slice_storage_parent="maps",
        slice_display_base="Slice 1",
        slice_root_cave_name="Cave",
        slice_reveal_before_close=True,
        slice_reveal_output_path="maps/Slice 1",
    )

    runtime.clear_slice_context()

    assert runtime.slice_source_cache_dir is None
    assert runtime.slice_storage_parent is None
    assert runtime.slice_display_base is None
    assert runtime.slice_root_cave_name is None
    assert runtime.slice_reveal_before_close is True
    assert runtime.slice_reveal_output_path == "maps/Slice 1"
