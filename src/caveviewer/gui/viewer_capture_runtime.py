"""Session-scoped capture resources owned by the viewer render thread.

The workflow coordinator owns non-OpenGL capture decisions. This runtime owns
the active encoder references, framebuffer readback owner, trace writers, and
slice handoff state that must remain attached to one native viewer session.
It never imports or calls the native window adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import queue
from typing import Any

from caveviewer.gui import manual_dive_trace, recording
from caveviewer.gui.recording_capture import RecordingCaptureResources


@dataclass(frozen=True)
class PendingManualDiveTraceWriter:
    """One trace writer paired with its user-visible completion policy."""

    recorder: manual_dive_trace.ManualDiveTraceRecorder
    show_completion: bool
    reveal_on_success: bool


@dataclass
class ViewerCaptureRuntime:
    """Own mutable capture execution state for one native viewer session."""

    manual_dive_trace: manual_dive_trace.ManualDiveTraceRecorder | None = None
    manual_dive_trace_writers: list[PendingManualDiveTraceWriter] = field(
        default_factory=list
    )
    pending_recorded_dive_trace: Any = None
    recorded_dive_trace: Any = None
    recorded_dive_controller: Any = None
    recorded_dive_prefetch_cell_set: frozenset[tuple[int, int, int]] = frozenset()
    recorded_dive_background_paused: bool = False
    slice_reveal_before_close: bool = False
    slice_reveal_output_path: str | None = None
    slice_source_cache_dir: str | None = None
    slice_storage_parent: str | None = None
    slice_display_base: str | None = None
    slice_root_cave_name: str | None = None
    recording_session: recording.RecordingEncoderSession | None = None
    recording_output_path: str | None = None
    recording_resources: RecordingCaptureResources | None = None
    recording_frame_queue: queue.Queue | None = None
    recording_stop_results: queue.Queue = field(default_factory=queue.Queue)
    recording_stop_thread: Any = None
    recording_stop_cancel_event: Any = None

    def ensure_recording_resources(
        self,
        *,
        ctx: Any,
        buffer_count: int,
        readback_components: int,
        logger: Any,
        perf_counter: Callable[[], float],
    ) -> RecordingCaptureResources:
        """Return the single render-thread readback owner for this session."""
        resources = self.recording_resources
        if resources is None:
            resources = RecordingCaptureResources(
                ctx=ctx,
                buffer_count=buffer_count,
                readback_components=readback_components,
                logger=logger,
                perf_counter=perf_counter,
            )
            self.recording_resources = resources
        else:
            resources.ctx = ctx
            resources.buffer_count = int(buffer_count)
            resources.readback_components = int(readback_components)
            resources.logger = logger
            resources.perf_counter = perf_counter
        return resources

    def attach_recording(
        self,
        session: recording.RecordingEncoderSession,
        *,
        readback_framebuffer: Any,
    ) -> None:
        """Publish an initialized encoder and its matching readback state."""
        resources = self.recording_resources
        if resources is None:
            raise RuntimeError("recording readback resources are not initialized")
        self.recording_session = session
        self.recording_output_path = session.output_path
        self.recording_frame_queue = session.frame_queue
        resources.output_size = session.output_size
        resources.capture_viewport = session.viewport
        resources.readback_framebuffer = readback_framebuffer

    def detach_recording(self) -> recording.RecordingEncoderSession | None:
        """Detach the active encoder exactly once before async finalization."""
        session = self.recording_session
        self.recording_session = None
        self.recording_output_path = None
        self.recording_frame_queue = None
        resources = self.recording_resources
        if resources is not None:
            resources.output_size = None
            resources.capture_viewport = None
        return session

    def release_recording_resources(self) -> None:
        """Release framebuffer and buffer resources idempotently."""
        resources = self.recording_resources
        if resources is None:
            return
        resources.release_buffers()
        resources.release_framebuffer()

    def clear_slice_context(self) -> None:
        """Forget source and destination facts for a completed slice."""
        self.slice_source_cache_dir = None
        self.slice_storage_parent = None
        self.slice_display_base = None
        self.slice_root_cave_name = None
