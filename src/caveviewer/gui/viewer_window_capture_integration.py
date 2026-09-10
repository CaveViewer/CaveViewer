"""Render-thread capture and recorded-dive integration for the viewer window."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import math
import os
import queue
import threading
import time

import moderngl
import numpy as np

from caveviewer.core.map import slicing as map_slicing
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui import bitmap_font, manual_dive_trace, recorded_dive, recording
from caveviewer.gui.artifact_capture_controller import ArtifactCaptureStatus
from caveviewer.gui.manual_dive_trace_controller import ManualDiveTraceStateController
from caveviewer.gui.platform.probes.recording import VideoRecordingTarget
from caveviewer.gui.platform.recording_preflight import video_recording_preflight
from caveviewer.gui.recording_capture import RecordingCaptureResources
from caveviewer.gui.recording_controller import RecordingStateController
from caveviewer.gui.slice_export_controller import SliceExportCanceled, SliceExportFailed, SliceExportSucceeded
from caveviewer.gui.slice_selection_controller import SliceAnchors, SliceSelectionController
from caveviewer.gui.viewer_capture_runtime import (
    PendingManualDiveTraceWriter as _PendingManualDiveTraceWriter,
    ViewerCaptureRuntime,
)
from caveviewer.gui.viewer_capture_workflow import CaptureOwner

_LOG = get_logger("CaveViewer")
_RECORDED_DIVE_LOOKAHEAD_SECONDS = 10.0
_RECORDED_DIVE_PREFETCH_RADIUS_CELLS = 1
_RECORDED_DIVE_PREFETCH_CELL_CAP = 256
_RecordingStopResult = recording.RecordingStopResult
_RecordingReadbackSlot = recording.RecordingReadbackSlot

def _capture_runtime_property(attribute_name: str):
    """Bridge transitional capture attributes to the session runtime."""

    def runtime(window) -> ViewerCaptureRuntime:
        value = getattr(window, "_capture_runtime", None)
        if value is None:
            value = ViewerCaptureRuntime()
            window._capture_runtime = value
        return value

    def getter(window):
        return getattr(runtime(window), attribute_name)

    def setter(window, value) -> None:
        setattr(runtime(window), attribute_name, value)

    return property(getter, setter)

def _recording_resource_property(attribute_name: str):
    """Bridge readback fields directly to their authoritative resource owner."""

    def resources(window) -> RecordingCaptureResources:
        return window._ensure_recording_capture()

    def getter(window):
        return getattr(resources(window), attribute_name)

    def setter(window, value) -> None:
        setattr(resources(window), attribute_name, value)

    return property(getter, setter)

def _recording_controller_property(attribute_name: str):
    """Bridge legacy state attributes to the recording controller."""

    def getter(window):
        return getattr(window._ensure_recording_controller(), attribute_name)

    def setter(window, value) -> None:
        setattr(window._ensure_recording_controller(), attribute_name, value)

    return property(getter, setter)


class ViewerWindowCaptureIntegration:
    """Methods executed by ``CaveViewerWindow`` on its owning callback thread."""

    _manual_dive_trace = _capture_runtime_property("manual_dive_trace")
    _manual_dive_trace_writers = _capture_runtime_property(
        "manual_dive_trace_writers"
    )
    _pending_recorded_dive_trace = _capture_runtime_property(
        "pending_recorded_dive_trace"
    )
    _recorded_dive_trace = _capture_runtime_property("recorded_dive_trace")
    _recorded_dive_controller = _capture_runtime_property(
        "recorded_dive_controller"
    )
    _recorded_dive_prefetch_cell_set = _capture_runtime_property(
        "recorded_dive_prefetch_cell_set"
    )
    _recorded_dive_background_paused = _capture_runtime_property(
        "recorded_dive_background_paused"
    )
    _slice_reveal_before_close = _capture_runtime_property(
        "slice_reveal_before_close"
    )
    _slice_reveal_output_path = _capture_runtime_property(
        "slice_reveal_output_path"
    )
    _slice_source_cache_dir = _capture_runtime_property("slice_source_cache_dir")
    _slice_storage_parent = _capture_runtime_property("slice_storage_parent")
    _slice_display_base = _capture_runtime_property("slice_display_base")
    _slice_root_cave_name = _capture_runtime_property("slice_root_cave_name")
    _recording_session = _capture_runtime_property("recording_session")
    _recording_frame_queue = _capture_runtime_property("recording_frame_queue")
    _recording_stop_results = _capture_runtime_property("recording_stop_results")
    _recording_stop_thread = _capture_runtime_property("recording_stop_thread")
    _recording_stop_cancel_event = _capture_runtime_property(
        "recording_stop_cancel_event"
    )
    _recording_viewport = _recording_resource_property("capture_viewport")
    _recording_readback_slots = _recording_resource_property("readback_slots")
    _recording_countdown_until = _recording_controller_property("countdown_until")
    _recording_last_stage_ms = _recording_controller_property("last_stage_ms")
    _recording_last_drain_ms = _recording_controller_property("last_drain_ms")
    _recording_next_frame_time = _recording_controller_property("next_frame_time")
    _recording_dropped_frames = _recording_controller_property("dropped_frames")
    _recording_status_until = _recording_controller_property("status_until")
    _recording_output_path = _capture_runtime_property("recording_output_path")
    _recording_size = _recording_resource_property("output_size")
    _recording_readback_framebuffer = _recording_resource_property(
        "readback_framebuffer"
    )
    _recording_readback_pending = _recording_resource_property("readback_pending")
    _recording_readback_byte_count = _recording_resource_property(
        "readback_byte_count"
    )
    _recording_countdown_started_at = _recording_controller_property(
        "countdown_started_at"
    )
    _recording_frame_interval = _recording_controller_property("frame_interval")
    _recording_status_message = _recording_controller_property("status_message")
    _recording_status_detail = _recording_controller_property("status_detail")
    _recording_status_kind = _recording_controller_property("status_kind")

    def _move_camera(self, forward_amt: float, right_amt: float, up_amt: float,
                     dt: float, speed_multiplier: float) -> None:
        """Move the camera freely without constraining it to map geometry."""
        if self.camera is None:
            return
        self.camera.move(forward_amt, right_amt, up_amt, dt, speed_multiplier)

    def _recording_is_armed(self) -> bool:
        return self._ensure_recording_controller().is_armed(
            process_active=getattr(self, "_recording_session", None) is not None
        )

    def _ensure_recording_stop_state(self) -> None:
        if not hasattr(self, "_recording_stop_results"):
            self._recording_stop_results = queue.Queue()
        if not hasattr(self, "_recording_stop_thread"):
            self._recording_stop_thread = None
        if not hasattr(self, "_recording_stop_cancel_event"):
            self._recording_stop_cancel_event = None

    def _recording_stop_in_progress(self) -> bool:
        self._ensure_recording_stop_state()
        return self._recording_stop_thread is not None

    def _exit_capture_artifacts(self) -> tuple[str, ...]:
        """Return user artifacts that must finish before the viewer may close."""
        artifacts: list[str] = []
        if (
            getattr(self, "_recording_session", None) is not None
            or self._recording_stop_in_progress()
        ):
            artifacts.append("Video")
        if (
            getattr(self, "_manual_dive_trace", None) is not None
            or getattr(self, "_manual_dive_trace_writers", None)
        ):
            artifacts.append("Dive trace")
        if (
            self._ensure_slice_selection_controller().selection_active
            or self._ensure_slice_export_controller().active
        ):
            artifacts.append("Slice")
        return tuple(artifacts)

    def _exit_capture_finalization_active(self) -> bool:
        """Return whether shutdown is waiting for a user artifact writer."""
        return self._ensure_capture_workflow().exit_finalization_active

    def _escape_capture_cancellation_active(self) -> bool:
        """Return whether Escape is canceling a capture before viewer close."""
        return self._ensure_capture_workflow().escape_cancellation_active

    def _capture_close_pending(self) -> bool:
        """Return whether capture cleanup currently owns viewer shutdown."""
        return self._ensure_capture_workflow().close_pending

    def _defer_backend_close_request(self) -> None:
        """Keep the GLFW window alive after its close callback has fired."""
        wnd = getattr(self, "wnd", None)
        if wnd is None:
            return
        try:
            wnd.is_closing = False
        except Exception:
            # A lightweight or future backend may not expose a cancellable
            # native-close flag. Its normal close path remains unchanged.
            pass

    def _begin_exit_capture_finalization(
        self,
        artifact_names: tuple[str, ...],
    ) -> None:
        """Stop active capture cleanly and show progress before shutdown."""
        self._ensure_capture_workflow().begin_exit_finalization()
        self._defer_backend_close_request()
        self._reset_transient_input_state("saving capture before close")

        presentation = self._ensure_artifact_capture_presentation()
        # A user who is closing the viewer did not ask to open a file browser.
        presentation.discard_pending_reveals()

        if getattr(self, "_recording_session", None) is not None:
            self._stop_recording()
        if getattr(self, "_manual_dive_trace", None) is not None:
            self._stop_manual_dive_trace(reason="viewer_closed")
        if self._ensure_slice_selection_controller().selection_active:
            self._finish_active_slice(closing=True)

        remaining_artifact_names = self._exit_capture_artifacts()
        if remaining_artifact_names:
            self._show_artifact_capture_status(
                presentation.exit_saving_status(remaining_artifact_names)
            )
        _LOG.info(
            "Waiting for %s before closing the viewer.",
            " and ".join(name.lower() for name in remaining_artifact_names)
            or "final capture cleanup",
        )

    def _begin_escape_capture_cancellation(self) -> bool:
        """Discard the active capture and close after its result is readable."""
        owner = self._capture_owner()
        if owner is None:
            return False

        workflow = self._ensure_capture_workflow()
        workflow.begin_escape_cancellation()
        self._defer_backend_close_request()
        self._reset_transient_input_state("canceling capture before close")

        # A discarded capture must never reveal a file that was queued by an
        # earlier publication attempt while the cancellation takes ownership.
        self._ensure_artifact_capture_presentation().discard_pending_reveals()
        handled = self._cancel_active_capture()
        owner_after_request = self._capture_owner()
        cancellation_rejected = bool(
            owner_after_request is not None
            and getattr(self, "_recording_status_kind", None) == "error"
        )
        if not handled or cancellation_rejected:
            # If cleanup could not start, keep the viewer open so the owned
            # writer is not silently converted back into save-on-shutdown.
            workflow.complete_escape_cancellation()
            if owner_after_request is None:
                self.on_close()
            return True

        _LOG.info(
            "Waiting for capture cancellation feedback before closing the viewer."
        )
        return True

    def _complete_exit_capture_finalization_if_ready(
        self,
        *,
        allow_unpresented_status: bool = False,
    ) -> bool:
        """Close once all exit-time capture writers have published their files."""
        workflow = self._ensure_capture_workflow()
        if not workflow.can_complete_exit_finalization(
            artifacts_pending=bool(self._exit_capture_artifacts()),
            now=time.perf_counter(),
            allow_unpresented_status=allow_unpresented_status,
        ):
            return False

        workflow.complete_exit_finalization()
        if getattr(self, "_slice_reveal_before_close", False):
            output_path = getattr(self, "_slice_reveal_output_path", None)
            if output_path:
                self._reveal_saved_output(output_path, output_kind="slice")
            self._slice_reveal_before_close = False
            self._slice_reveal_output_path = None
        self._complete_window_close()
        return True

    def _complete_escape_capture_cancellation_if_ready(self) -> bool:
        """Close after cancellation cleanup and its three-second result pause."""
        workflow = self._ensure_capture_workflow()
        if not workflow.can_complete_escape_cancellation(
            artifacts_pending=bool(self._exit_capture_artifacts()),
            confirmation_until=self._recording_status_until,
            now=time.perf_counter(),
        ):
            return False

        workflow.complete_escape_cancellation()
        self._complete_window_close()
        return True

    def _input_is_suppressed(self) -> bool:
        """Return whether viewer controls should ignore late input callbacks."""
        return bool(
            getattr(self, "_closing_requested", False)
            or self._capture_close_pending()
        )

    def _recording_hides_hud(self) -> bool:
        return self._recording_is_armed()

    def _toggle_recording(self) -> None:
        self._drain_recording_stop_results()
        if self._recording_stop_in_progress():
            self._show_artifact_capture_status(
                self._ensure_artifact_capture_presentation().saving_status(
                    "Video",
                    cancelable=True,
                )
            )
            return

        if self._recording_session is not None:
            self._stop_recording(show_message=True, reveal_on_success=True)
            return

        if self._recording_countdown_until is not None:
            now = time.perf_counter()
            self._ensure_recording_controller().clear_countdown()
            self._show_artifact_capture_status(
                self._ensure_artifact_capture_presentation().canceled_status("Video"),
                now=now,
            )
            _LOG.info("Recording countdown canceled.")
            return

        self._start_recording_countdown()

    def _cancel_recording_capture(self) -> bool:
        """Cancel recording countdown, capture, or pending output publication."""
        if self._exit_capture_finalization_active():
            return False
        self._ensure_recording_stop_state()
        self._drain_recording_stop_results()
        controller = self._ensure_recording_controller()
        if controller.countdown_until is not None:
            controller.clear_countdown()
            self._show_artifact_capture_status(
                self._ensure_artifact_capture_presentation().canceled_status(
                    "Video",
                    after_escape=True,
                ),
                now=time.perf_counter(),
            )
            _LOG.info("Recording countdown canceled with Escape.")
            return True
        if getattr(self, "_recording_session", None) is not None:
            self._stop_recording(show_message=True, cancel_output=True)
            _LOG.info("Recording cancellation requested with Escape.")
            return True
        cancel_event = self._recording_stop_cancel_event
        if self._recording_stop_in_progress() and cancel_event is not None:
            cancel_event.set()
            self._show_artifact_capture_status(
                self._ensure_artifact_capture_presentation().canceling_status(
                    "Video"
                )
            )
            _LOG.info("Pending recording publication canceled with Escape.")
            return True
        return False

    def _start_recording_countdown(self) -> None:
        if not self._has_map_loaded:
            return
        if self._capture_start_blocked(CaptureOwner.VIDEO):
            return
        if self._recording_target_if_available() is None:
            return

        self.color_picker.hide()
        if self.controls_overlay.is_manual_mode:
            self.controls_overlay.hide_help()
        now = time.perf_counter()
        self._ensure_recording_controller().start_countdown(
            now=now,
            start_number=self.RECORDING_COUNTDOWN_START_NUMBER,
        )
        _LOG.info(
            "Recording countdown started. Press %s+R to stop or Escape to cancel.",
            self._primary_shortcut_label(),
        )

    def _resolve_ffmpeg_path(self) -> str | None:
        viewer_settings = getattr(self, "_viewer_runtime_settings", None)
        if viewer_settings is None:
            return recording.resolve_ffmpeg_path()
        configured_path = viewer_settings.recording.ffmpeg_path
        if configured_path:
            return configured_path
        return recording.resolve_ffmpeg_path(environ={})

    def _recording_preflight(self) -> VideoRecordingPreflight:
        """Return one fresh recording probe paired with its policy decision."""
        return video_recording_preflight(
            self._recording_output_dir,
            ffmpeg_resolver=self._resolve_ffmpeg_path,
            platform_runtime=getattr(self, "_platform_runtime", None),
        )

    def _recording_target_if_available(self) -> VideoRecordingTarget | None:
        """Return a freshly-probed ffmpeg target or show the policy explanation."""
        preflight = self._recording_preflight()
        capability = preflight.capability
        decision = preflight.decision
        if not decision.allows_execution or capability.value is None:
            self._recording_unavailable(decision.explanation)
            return None
        return capability.value

    def _recording_unavailable(self, reason: str) -> None:
        message = f"Cannot start recording: {reason}"
        _LOG.warning(message)
        self._show_capture_status(
            "Recording unavailable",
            reason,
            kind="error",
            duration=3.4,
        )

    def _recording_capture_viewport(self) -> tuple[int, int, int, int]:
        for viewport in (
            getattr(self.ctx, "viewport", None),
            getattr(self.ctx.screen, "viewport", None),
        ):
            if viewport and len(viewport) >= 4:
                x, y, width, height = (int(v) for v in viewport[:4])
                if width > 0 and height > 0:
                    return x, y, width, height

        screen_size = getattr(self.ctx.screen, "size", None)
        if screen_size:
            width, height = screen_size
            return 0, 0, int(width), int(height)

        width, height = self.wnd.size
        return 0, 0, int(width), int(height)

    def _recording_framebuffer_size(self) -> tuple[int, int]:
        _x, _y, width, height = self._recording_capture_viewport()
        return width, height

    def _recording_output_size(self, width: int, height: int) -> tuple[int, int]:
        return recording.recording_output_size(
            width,
            height,
            self._recording_max_height,
        )

    def _release_recording_readback_framebuffer(self) -> None:
        self._ensure_recording_capture().release_framebuffer()

    def _discard_recording_staged_frames(self) -> int:
        dropped = self._ensure_recording_capture().discard_staged_frames()
        return dropped

    def _release_recording_readback_buffers(self) -> None:
        self._ensure_recording_capture().release_buffers()
        self._ensure_recording_controller().reset_frame_timings()

    def _create_recording_readback_framebuffer(
        self,
        capture_size: tuple[int, int],
        output_size: tuple[int, int],
    ) -> moderngl.Framebuffer | None:
        framebuffer = self._ensure_recording_capture().create_framebuffer(
            capture_size,
            output_size,
        )
        return framebuffer

    def _create_recording_readback_buffers(self, output_size: tuple[int, int]) -> None:
        self._ensure_recording_capture().create_buffers(output_size)

    def _start_recording_encoder(self) -> bool:
        recording_target = self._recording_target_if_available()
        if recording_target is None:
            self._ensure_recording_controller().clear_countdown()
            return False

        viewport = self._recording_capture_viewport()
        width, height = viewport[2], viewport[3]
        if width <= 0 or height <= 0:
            self._ensure_recording_controller().clear_countdown()
            return False

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output_path = os.path.join(
            recording_target.output_directory,
            f"CaveViewerDive-{timestamp}.mp4",
        )
        output_width, output_height = self._recording_output_size(width, height)
        output_size = (output_width, output_height)
        try:
            readback_framebuffer = self._create_recording_readback_framebuffer(
                (width, height),
                output_size,
            )
            self._create_recording_readback_buffers(output_size)
        except Exception as exc:
            self._release_recording_readback_framebuffer()
            self._release_recording_readback_buffers()
            _LOG.warning(f"Cannot start recording: failed to create recording readback resources: {exc}")
            self._ensure_recording_controller().clear_countdown()
            self._show_capture_status(
                "Recording unavailable",
                "Could not prepare the recording framebuffer.",
                kind="error",
                duration=3.4,
            )
            return False

        # Recheck the on-demand gate immediately before launching ffmpeg. The
        # countdown and framebuffer setup can take long enough for a removable
        # drive or folder permission to change underneath the viewer.
        recording_target = self._recording_target_if_available()
        if recording_target is None:
            self._release_recording_readback_framebuffer()
            self._release_recording_readback_buffers()
            self._ensure_recording_controller().clear_countdown()
            return False

        output_path = os.path.join(
            recording_target.output_directory,
            f"CaveViewerDive-{timestamp}.mp4",
        )

        try:
            session = recording.start_encoder_session(
                ffmpeg_path=recording_target.ffmpeg_path,
                output_path=output_path,
                output_size=output_size,
                viewport=viewport,
                fps=self._recording_fps,
                crf=self._recording_crf,
                raw_pix_fmt=self.RECORDING_RAW_PIX_FMT,
                popen_startup_kwargs=(
                    self._active_recording_process_adapter().encoder_popen_kwargs()
                ),
            )
        except (OSError, RuntimeError) as exc:
            _LOG.warning(f"Cannot start recording: {exc}")
            self._release_recording_readback_framebuffer()
            self._release_recording_readback_buffers()
            self._ensure_recording_controller().clear_countdown()
            return False

        self._ensure_capture_runtime().attach_recording(
            session,
            readback_framebuffer=readback_framebuffer,
        )
        now = time.perf_counter()
        self._ensure_recording_controller().mark_encoder_started(now=now)
        _LOG.info(
            f"Recording started: {output_path} "
            f"capture_viewport={viewport} readback_size={output_width}x{output_height} "
            f"output_size={output_width}x{output_height} "
            f"raw_pix_fmt={self.RECORDING_RAW_PIX_FMT} "
            f"readback_buffers={len(self._recording_readback_slots)}"
        )
        return True

    def _recording_signal_writer_stop(self, frame_queue: queue.Queue | None) -> None:
        recording.signal_writer_stop(frame_queue)

    def _recording_drop_frames(self, count: int = 1) -> None:
        if self._ensure_recording_controller().drop_frames(count):
            _LOG.warning("Recording encoder is falling behind; dropping video frames.")

    def _recording_due_frame_slots(self, now: float, next_frame_time: float | None) -> int:
        return self._ensure_recording_controller().due_frame_slots(
            now=now,
            next_frame_time=next_frame_time,
        )

    def _recording_enqueue_frame(self, frame: bytes) -> bool:
        frame_queue = self._recording_frame_queue
        if frame_queue is None:
            self._stop_recording()
            return False

        try:
            frame_queue.put_nowait(frame)
        except queue.Full:
            self._recording_drop_frames()
            return False
        return True

    def _recording_display_path(self, path: str | None) -> str | None:
        return recording.recording_display_path(path)

    def _show_capture_status(
        self,
        message: str,
        detail: str | None = None,
        *,
        kind: str = "info",
        duration: float | None = 2.8,
        now: float | None = None,
    ) -> None:
        self._ensure_recording_controller().show_status(
            message,
            detail=detail,
            kind=kind,
            duration=duration,
            now=time.perf_counter() if now is None else now,
        )

    def _show_artifact_capture_status(
        self,
        status: ArtifactCaptureStatus,
        *,
        now: float | None = None,
    ) -> None:
        """Present one shared artifact-capture status through the HUD."""
        self._show_capture_status(
            status.message,
            status.detail,
            kind=status.kind,
            duration=status.duration,
            now=now,
        )

    def _stop_recording(
        self,
        *,
        show_message: bool = False,
        reveal_on_success: bool = False,
        cancel_output: bool = False,
    ) -> None:
        self._ensure_recording_stop_state()
        self._drain_recording_stop_results()
        if self._recording_stop_in_progress():
            if cancel_output and self._recording_stop_cancel_event is not None:
                self._recording_stop_cancel_event.set()
            return

        self._ensure_recording_controller().clear_countdown()
        session = self._ensure_capture_runtime().detach_recording()
        self._release_recording_readback_buffers()
        self._release_recording_readback_framebuffer()
        self._recording_next_frame_time = None

        if session is None:
            return

        cancel_event = threading.Event()
        if cancel_output:
            cancel_event.set()
        self._recording_stop_cancel_event = cancel_event
        session.signal_writer_stop(discard_pending=cancel_output)
        work = session.stop_work(
            show_message=show_message,
            reveal_on_success=reveal_on_success,
            cancel_event=cancel_event,
        )
        self._recording_stop_thread = recording.start_stop_finalizer(
            work,
            result_queue=self._recording_stop_results,
            stderr_text=session.stderr_text,
            writer_error=lambda: session.writer_error,
            dropped_frames=lambda: self._recording_dropped_frames,
            logger=_LOG,
        )
        if show_message:
            self._show_artifact_capture_status(
                (
                    self._ensure_artifact_capture_presentation().canceling_status(
                        "Video"
                    )
                    if cancel_output
                    else self._ensure_artifact_capture_presentation().saving_status(
                        "Video",
                        cancelable=True,
                    )
                )
            )

    def _drain_recording_stop_results(self) -> None:
        self._ensure_recording_stop_state()
        while True:
            try:
                result = self._recording_stop_results.get_nowait()
            except queue.Empty:
                break
            self._apply_recording_stop_result(result)
            self._recording_stop_thread = None
            self._recording_stop_cancel_event = None

    def _reveal_saved_output(
        self,
        output_path: str | None,
        *,
        output_kind: str,
    ) -> None:
        """Best-effort native reveal after a writer has published final output."""
        if not output_path:
            return
        try:
            self._active_saved_artifact_reveal_adapter().reveal_saved_artifact(
                output_path
            )
        except Exception as exc:
            _LOG.warning(
                "Could not reveal saved %s %s: %s",
                output_kind,
                output_path,
                exc,
            )

    def _drain_due_saved_artifact_reveals(self, *, now: float | None = None) -> None:
        """Reveal artifacts only after their shared success message has been visible."""
        controller = self._ensure_artifact_capture_presentation()
        if not controller.has_pending_reveals:
            return
        current_time = time.perf_counter() if now is None else now
        for request in controller.take_due_reveals(
            now=current_time
        ):
            self._reveal_saved_output(
                request.output_path,
                output_kind=request.artifact_name.lower(),
            )

    def _apply_recording_stop_result(self, result: _RecordingStopResult) -> None:
        self._ensure_recording_controller().reset_after_stop_result()

        if result.canceled:
            if result.cleanup_error:
                _LOG.warning(
                    "Canceled recording cleanup failed: %s",
                    result.cleanup_error,
                )
                if result.show_message and not self._exit_capture_finalization_active():
                    self._show_artifact_capture_status(
                        self._ensure_artifact_capture_presentation().cancellation_failed_status(
                            "Video",
                            "The partial video could not be removed.",
                        )
                    )
            else:
                _LOG.info("Recording canceled and partial output removed.")
                if result.show_message and not self._exit_capture_finalization_active():
                    self._show_artifact_capture_status(
                        self._ensure_artifact_capture_presentation().canceled_status(
                            "Video",
                            after_escape=True,
                        )
                    )
            return

        if result.returncode == 0:
            _LOG.info(f"Recording saved: {result.output_path}")
            if result.dropped_frames:
                _LOG.warning(f"Recording saved after dropping {result.dropped_frames} frame(s).")
            if result.show_message and not self._exit_capture_finalization_active():
                now = time.perf_counter()
                status = self._ensure_artifact_capture_presentation().saved_status(
                    "Video",
                    result.output_path,
                    now=now,
                    reveal=result.reveal_on_success,
                )
                self._show_artifact_capture_status(status, now=now)
        else:
            if result.stderr_text and result.writer_error:
                detail = f": {result.stderr_text}; writer_error={result.writer_error}"
            elif result.stderr_text:
                detail = f": {result.stderr_text}"
            elif result.writer_error:
                detail = f": writer_error={result.writer_error}"
            else:
                detail = ""
            _LOG.warning(f"Recording encoder exited with code {result.returncode}{detail}")
            if result.show_message and not self._exit_capture_finalization_active():
                self._show_artifact_capture_status(
                    self._ensure_artifact_capture_presentation().failed_status(
                        "Video",
                        self._recording_failure_detail(result.stderr_text),
                    )
                )

    def _recording_failure_detail(self, stderr_text: str) -> str:
        return recording.recording_failure_detail(stderr_text)

    def _recording_capture_state(self) -> tuple[tuple[int, int], tuple[int, int, int, int], int]:
        return self._ensure_recording_capture().capture_state()

    def _recording_free_readback_slot(self) -> _RecordingReadbackSlot | None:
        return self._ensure_recording_capture().free_readback_slot()

    def _recording_copy_to_readback_framebuffer(
        self,
        readback_framebuffer: moderngl.Framebuffer,
        output_size: tuple[int, int],
        capture_viewport: tuple[int, int, int, int],
    ) -> None:
        self._ensure_recording_capture().copy_to_readback_framebuffer(
            readback_framebuffer,
            output_size,
            capture_viewport,
        )

    def _recording_stage_frame(
        self,
        render_frame: Callable[[moderngl.Framebuffer, tuple[int, int]], None] | None = None,
    ) -> bool:
        return self._ensure_recording_capture().stage_frame(
            render_frame=render_frame,
        )

    def _recording_drain_staged_frames(self) -> float:
        return self._ensure_recording_capture().drain_staged_frames(
            frame_queue=self._recording_frame_queue,
            enqueue_frame=self._recording_enqueue_frame,
            stop_recording=self._stop_recording,
        )

    def _recording_update_after_scene(
        self,
        now: float,
        *,
        render_frame: Callable[[moderngl.Framebuffer, tuple[int, int]], None] | None = None,
    ) -> float:
        controller = self._ensure_recording_controller()
        controller.reset_frame_timings()

        if controller.countdown_until is not None:
            if not controller.countdown_ready(now=now):
                return 0.0
            if not self._start_recording_encoder():
                return 0.0

        session = self._recording_session
        if session is None:
            return 0.0

        if session.stopped_before_finalization():
            _LOG.warning("Recording encoder stopped before recording was finalized.")
            self._stop_recording(show_message=True)
            return 0.0

        if self._recording_viewport != self._recording_capture_viewport():
            _LOG.warning("Recording stopped because the window size changed.")
            self._stop_recording()
            return 0.0

        read_ms = 0.0
        try:
            drain_ms = self._recording_drain_staged_frames()
            self._recording_last_drain_ms = drain_ms
            read_ms += drain_ms
        except (OSError, moderngl.Error) as exc:
            _LOG.warning(f"Recording stopped because frame capture failed: {exc}")
            self._stop_recording(show_message=True)
            return read_ms
        if self._recording_session is None:
            return read_ms

        next_frame_time = self._recording_next_frame_time
        if next_frame_time is not None and now < next_frame_time:
            return read_ms

        frame_slots = self._recording_due_frame_slots(now, next_frame_time)
        frame_queue = self._recording_frame_queue
        if frame_queue is None:
            self._stop_recording(show_message=True)
            return read_ms

        if frame_queue.full():
            staged_frames = self._discard_recording_staged_frames()
            self._recording_drop_frames(frame_slots + staged_frames)
            controller.advance_next_frame_time(now=now, frame_slots=frame_slots)
            return read_ms

        try:
            self._recording_drop_frames(frame_slots - 1)

            t_stage = time.perf_counter()
            if not self._recording_stage_frame(render_frame=render_frame):
                self._recording_drop_frames()
            stage_ms = (time.perf_counter() - t_stage) * 1000.0
            self._recording_last_stage_ms = stage_ms
            read_ms += stage_ms
            controller.advance_next_frame_time(now=now, frame_slots=frame_slots)
            return read_ms
        except (OSError, moderngl.Error) as exc:
            _LOG.warning(f"Recording stopped because frame capture failed: {exc}")
            self._stop_recording(show_message=True)
            return read_ms

    def _render_countdown_overlay(
        self,
        *,
        now: float,
        controller: (
            RecordingStateController
            | ManualDiveTraceStateController
            | SliceSelectionController
        ),
        start_number: int,
        title: str,
        note: str,
    ) -> None:
        """Render the shared import-style countdown used before capture begins."""
        display = controller.countdown_display(
            now=now,
            start_number=start_number,
        )
        self._render_recording_countdown_scrim(self.wnd.size)
        self.import_progress_panel.draw_countdown_number(
            center_x=self.wnd.size[0] / 2.0,
            center_y=self.wnd.size[1] / 2.0,
            window_size=self.wnd.size,
            number=display.number,
            progress=display.progress,
            fixed_text_scale=self.UI_TEXT_SCALE,
            stage=title,
            note=note,
        )

    def _countdown_cancel_note(self, shortcut_key: str) -> str:
        """Show both the capture toggle and normalized Escape cancellation."""
        return (
            f"Press {self._primary_shortcut_label()}+{shortcut_key} again to stop. "
            "Press Esc to cancel."
        )

    def _print_texture_diagnostics(self, manifest: dict, textures_dir: str) -> None:
        """Print a one-time texture summary to console on map load."""
        from PIL import Image
        import io as _io

        mats = manifest.get("mtl_materials", {})
        _LOG.info(f"Texture diagnostics: {len(mats)} materials, "
              f"{len(manifest.get('chunks', {}))} total chunks")

        # Deduplicate: multiple material names can share one file/bytes blob.
        seen: dict[object, tuple[str, tuple[int, int]]] = {}  # key -> (first_mat, size)
        missing = 0
        embedded = 0

        for mat_name, file_or_bytes in mats.items():
            if file_or_bytes is None:
                missing += 1
                continue
            if file_or_bytes in seen:
                continue
            if isinstance(file_or_bytes, bytes):
                embedded += 1
                try:
                    img = Image.open(_io.BytesIO(file_or_bytes))
                    seen[file_or_bytes] = (mat_name, img.size)
                except Exception:
                    seen[file_or_bytes] = (mat_name, (0, 0))
            else:
                import os as _os
                path = _os.path.join(textures_dir, file_or_bytes)
                try:
                    with Image.open(path) as img:
                        seen[file_or_bytes] = (mat_name, img.size)
                except Exception:
                    seen[file_or_bytes] = (mat_name, (0, 0))

        sizes = [sz for _, sz in seen.values() if sz != (0, 0)]
        unique_files = len(seen)
        total_px = sum(w * h for w, h in sizes)
        total_mb = total_px * 3 / (1024 * 1024)  # RGB uncompressed

        size_counts: dict[tuple, int] = {}
        for sz in sizes:
            size_counts[sz] = size_counts.get(sz, 0) + 1

        _LOG.info(f"  Unique texture files : {unique_files}"
                  + (f" ({embedded} embedded)" if embedded else ""))
        if missing:
            _LOG.info(f"  Materials with no texture: {missing}")
        for sz, count in sorted(size_counts.items(), key=lambda x: -x[1]):
            _LOG.info(f"  {sz[0]}x{sz[1]} : {count} texture(s)")
        _LOG.info(f"  Uncompressed RGB total  : {total_mb:.0f} MB")
        max_dim = max((max(w, h) for w, h in sizes), default=0)
        # Rough atlas fit: next power-of-2 square that holds total_px
        import math as _math
        atlas_side = 2 ** _math.ceil(_math.log2(_math.sqrt(total_px))) if total_px > 0 else 0
        _LOG.info(f"  Estimated atlas needed  : {atlas_side}x{atlas_side} px "
              f"({atlas_side*atlas_side*3/1024/1024:.0f} MB)")

    def _recorded_dive_is_active(self) -> bool:
        controller = getattr(self, "_recorded_dive_controller", None)
        return controller is not None and controller.active

    def _recorded_dive_is_paused(self) -> bool:
        """Report whether an active dive is in orientation-only inspection mode."""
        controller = getattr(self, "_recorded_dive_controller", None)
        return bool(
            controller is not None
            and controller.active
            and getattr(controller, "state", None)
            is recorded_dive.RecordedDivePlaybackState.PAUSED
        )

    def _recorded_dive_prefetch_cells(self) -> frozenset[tuple[int, int, int]]:
        """Build a bounded, chronological chunk tube ahead of trace time."""
        controller = getattr(self, "_recorded_dive_controller", None)
        world = getattr(self, "world", None)
        if controller is None or world is None or not controller.active:
            return frozenset()

        poses = controller.lookahead_poses(_RECORDED_DIVE_LOOKAHEAD_SECONDS)
        if not poses:
            return frozenset()
        chunk_size = max(1e-6, float(world.config.chunk_size))
        sample_step_m = max(0.25, chunk_size * 0.5)
        centers: list[tuple[int, int, int]] = []
        previous_pose = poses[0]
        centers.append(
            world.cell_for_position(
                np.asarray(previous_pose.position, dtype=np.float32)
            )
        )
        for pose in poses[1:]:
            start = np.asarray(previous_pose.position, dtype=np.float64)
            end = np.asarray(pose.position, dtype=np.float64)
            segment = end - start
            distance = float(np.linalg.norm(segment))
            steps = 1
            if pose.record_kind != "discontinuity":
                steps = max(1, int(math.ceil(distance / sample_step_m)))
            for step in range(1, steps + 1):
                position = end if steps == 1 else start + segment * (step / steps)
                center = world.cell_for_position(
                    np.asarray(position, dtype=np.float32)
                )
                if not centers or center != centers[-1]:
                    centers.append(center)
            previous_pose = pose

        wanted: set[tuple[int, int, int]] = set()
        for center in centers:
            nearby = sorted(
                world.available_cells_in_radius(
                    center,
                    _RECORDED_DIVE_PREFETCH_RADIUS_CELLS,
                ),
                key=lambda cell: (
                    (cell[0] - center[0]) ** 2
                    + (cell[1] - center[1]) ** 2
                    + (cell[2] - center[2]) ** 2,
                    cell,
                ),
            )
            for cell in nearby:
                wanted.add(cell)
                if len(wanted) >= _RECORDED_DIVE_PREFETCH_CELL_CAP:
                    return frozenset(wanted)
        return frozenset(wanted)

    def _refresh_recorded_dive_prefetch(self) -> None:
        world = getattr(self, "world", None)
        if world is None:
            return
        cells = self._recorded_dive_prefetch_cells()
        if cells == getattr(self, "_recorded_dive_prefetch_cell_set", frozenset()):
            return
        self._recorded_dive_prefetch_cell_set = cells
        world.set_prefetch_wanted_cells(cells)

    def _recorded_dive_chunks_ready(self, *, now: float) -> bool:
        """Require GPU-resident geometry around the next authoritative pose."""
        controller = getattr(self, "_recorded_dive_controller", None)
        world = getattr(self, "world", None)
        if controller is None or world is None:
            return False
        if (
            not getattr(self, "_initial_chunks_loaded", False)
            or not getattr(self, "_initial_visual_ready", False)
        ):
            return False

        candidate_pose = controller.trace.pose_at(
            controller.candidate_elapsed(now=now)
        )
        center = world.cell_for_position(
            np.asarray(candidate_pose.position, dtype=np.float32)
        )
        required = set(
            world.available_cells_in_radius(
                center,
                _RECORDED_DIVE_PREFETCH_RADIUS_CELLS,
            )
        )
        if not required:
            required = set(
                world.available_cells_in_radius(
                    center,
                    max(1, int(world.config.load_radius_cells)),
                )
            )

        lock = getattr(world, "_lock", None)
        if lock is None:
            loaded = set(getattr(world, "loaded_cells", ()))
            failed = set(getattr(world, "_failed_cells", {}))
        else:
            with lock:
                loaded = set(getattr(world, "loaded_cells", ()))
                failed = set(getattr(world, "_failed_cells", {}))
        failed_required = required & failed
        if failed_required:
            _LOG.error(
                "Recorded Dive stopped because %d required map chunk(s) failed to load.",
                len(failed_required),
            )
            self._stop_recorded_dive(reason="chunk_load_failed")
            return False
        return required.issubset(loaded)

    def _update_recorded_dive(self, *, now: float) -> None:
        controller = getattr(self, "_recorded_dive_controller", None)
        if controller is None or not controller.active:
            return
        self._refresh_recorded_dive_prefetch()
        previous_state = controller.state
        chunks_ready = (
            True
            if previous_state is recorded_dive.RecordedDivePlaybackState.PAUSED
            else self._recorded_dive_chunks_ready(now=now)
        )
        current_state = controller.update(
            self.camera,
            now=now,
            chunks_ready=chunks_ready,
        )
        if current_state is recorded_dive.RecordedDivePlaybackState.BUFFERING:
            if previous_state is not current_state:
                _LOG.info("Recorded Dive paused its clock while map chunks load.")
            return
        if (
            previous_state is recorded_dive.RecordedDivePlaybackState.BUFFERING
            and current_state is recorded_dive.RecordedDivePlaybackState.PLAYING
        ):
            if self.controls_overlay.is_waiting_for_begin:
                self.controls_overlay.dismiss_begin_screen()
            _LOG.info("Recorded Dive playback started/resumed.")
        if current_state is recorded_dive.RecordedDivePlaybackState.FINISHED:
            if self.controls_overlay.is_waiting_for_begin:
                self.controls_overlay.dismiss_begin_screen()
            self._recorded_dive_prefetch_cell_set = frozenset()
            self.world.set_prefetch_wanted_cells(())
            _LOG.info(
                "Recorded Dive completed: %.1f seconds, %d recorded poses.",
                controller.trace.duration_s,
                len(controller.trace.poses),
            )

    def _stop_recorded_dive(self, *, reason: str) -> bool:
        controller = getattr(self, "_recorded_dive_controller", None)
        if controller is None:
            return False
        was_active = controller.active
        controller.stop()
        world = getattr(self, "world", None)
        if world is not None:
            world.set_prefetch_wanted_cells(())
        self._recorded_dive_prefetch_cell_set = frozenset()
        if was_active:
            _LOG.info("Recorded Dive stopped: %s.", reason)
        return was_active

    def _toggle_recorded_dive_pause(self) -> bool:
        controller = getattr(self, "_recorded_dive_controller", None)
        if controller is None or not controller.active:
            return False
        now = time.perf_counter()
        if controller.state is recorded_dive.RecordedDivePlaybackState.PAUSED:
            return controller.resume(self.camera, now=now)
        return controller.pause(now=now)

    def _render_dive_status(self, window_size: tuple[int, int]) -> None:
        """Render status for Recorded Dive playback."""
        self._render_recorded_dive_progress(window_size)

    @staticmethod
    def _recorded_dive_time_label(elapsed_s: float) -> str:
        total_seconds = max(0, int(round(float(elapsed_s))))
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:d}:{seconds:02d}"

    def _render_recorded_dive_progress(
        self,
        window_size: tuple[int, int],
    ) -> bool:
        controller = getattr(self, "_recorded_dive_controller", None)
        if controller is None or not controller.active:
            return False
        elapsed = self._recorded_dive_time_label(controller.elapsed_s)
        duration = self._recorded_dive_time_label(controller.trace.duration_s)
        if controller.state is recorded_dive.RecordedDivePlaybackState.BUFFERING:
            note = f"Loading nearby cave chunks… {elapsed} / {duration}"
        elif controller.state is recorded_dive.RecordedDivePlaybackState.PAUSED:
            note = (
                f"Paused for inspection at {elapsed} / {duration}. "
                "Look around; Space resumes."
            )
        else:
            note = f"{elapsed} / {duration}. Space pauses; movement takes control."
        self._render_dive_status_prompt(
            window_size,
            title="Recorded Dive",
            note=note,
        )
        return True

    def _render_dive_status_prompt(
        self,
        window_size: tuple[int, int],
        *,
        title: str,
        note: str,
    ) -> None:
        """Draw the small top prompt for Recorded Dive playback state."""
        w, h = window_size
        panel_w = min(
            max(
                self.DIVE_STATUS_PANEL_MIN_WIDTH,
                w * self.DIVE_STATUS_PANEL_WIDTH_FRACTION,
            ),
            w - 48.0,
        )
        panel_h = self.DIVE_STATUS_PANEL_HEIGHT
        x0 = (w - panel_w) / 2.0
        y0 = 30.0
        x1 = x0 + panel_w
        y1 = y0 + panel_h
        verts = []

        def px_to_ndc(x: float, y: float) -> tuple[float, float]:
            return (x / w) * 2.0 - 1.0, 1.0 - (y / h) * 2.0

        def add_quad_px(
            qx0: float,
            qy0: float,
            qx1: float,
            qy1: float,
            rgba: tuple[float, float, float, float],
        ) -> None:
            nx0, ny0 = px_to_ndc(qx0, qy0)
            nx1, ny1 = px_to_ndc(qx1, qy1)
            top, bottom = max(ny0, ny1), min(ny0, ny1)
            left, right = min(nx0, nx1), max(nx0, nx1)
            quad = [
                (left, bottom), (right, bottom), (right, top),
                (left, bottom), (right, top), (left, top),
            ]
            for vx, vy in quad:
                verts.append((vx, vy, *rgba))

        def add_centered_text(
            text: str,
            y: float,
            pixel_size: float,
            rgba: tuple[float, float, float, float],
            *,
            max_width: float,
        ) -> float:
            text = " ".join(str(text or "").split())
            if not text:
                return 0.0
            min_pixel_size = bitmap_font.pixel_size_at_text_scale(
                1.20,
                self.UI_TEXT_SCALE,
            )
            pixel_size = bitmap_font.pixel_size_at_text_scale(
                pixel_size,
                self.UI_TEXT_SCALE,
            )
            bounds = bitmap_font.text_bounds_px(text, pixel_size)
            text_w = bounds[2] - bounds[0]
            if text_w > max_width:
                pixel_size = max(min_pixel_size, pixel_size * max_width / text_w)
                bounds = bitmap_font.text_bounds_px(text, pixel_size)
                text_w = bounds[2] - bounds[0]
            text_h = bounds[3] - bounds[1]
            origin_x = (w - text_w) / 2.0 - bounds[0]
            origin_y = y - bounds[1]
            r, g, b, a = rgba
            for glyph in bitmap_font.iter_text_pixels(text, origin_x, origin_y, pixel_size):
                px0, py0, px1, py1 = glyph[0], glyph[1], glyph[2], glyph[3]
                glyph_alpha = glyph[4] if len(glyph) > 4 else 1.0
                add_quad_px(px0, py0, px1, py1, (r, g, b, a * glyph_alpha))
            return text_h

        add_quad_px(x0, y0, x1, y1, (0.025, 0.028, 0.040, 0.86))
        border = 2.0
        border_color = (0.8980, 0.6314, 0.1216, 0.95)
        add_quad_px(x0, y0, x1, y0 + border, border_color)
        add_quad_px(x0, y1 - border, x1, y1, border_color)
        add_quad_px(x0, y0, x0 + border, y1, border_color)
        add_quad_px(x1 - border, y0, x1, y1, border_color)

        title_h = add_centered_text(
            title,
            y0 + 24.0,
            self.DIVE_STATUS_TITLE_PIXEL_SIZE,
            (0.9490, 0.8510, 0.5490, 1.0),
            max_width=panel_w - 48.0,
        )
        add_centered_text(
            note,
            y0 + 24.0 + title_h + 18.0,
            self.DIVE_STATUS_NOTE_PIXEL_SIZE,
            (0.835, 0.855, 0.86, 0.92),
            max_width=panel_w - 48.0,
        )

        data = np.array(verts, dtype=np.float32)
        if len(verts) > self._status_panel_max_verts:
            self._status_panel_vbo.release()
            self._status_panel_max_verts = max(self._status_panel_max_verts * 2, len(verts))
            self._status_panel_vbo = self.ctx.buffer(reserve=self._status_panel_max_verts * 6 * 4)
            self._status_panel_vao = self.ctx.vertex_array(
                self._hud_panel_program,
                [(self._status_panel_vbo, "2f 4f", "in_pos", "in_color")],
            )
        self._status_panel_vbo.write(data.tobytes())
        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        self._status_panel_vao.render(moderngl.TRIANGLES, vertices=len(verts))
        self.ctx.disable(moderngl.BLEND)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.CULL_FACE)

    def _manual_dive_trace_pose(
        self,
    ) -> manual_dive_trace.ManualDivePose | None:
        camera = getattr(self, "camera", None)
        if camera is None:
            return None
        try:
            return manual_dive_trace.ManualDivePose.from_camera(camera)
        except (AttributeError, TypeError, ValueError):
            return None

    def _slice_camera_position(self) -> tuple[float, float, float] | None:
        """Return the current camera position as a finite export anchor."""
        camera = getattr(self, "camera", None)
        position = getattr(camera, "position", None)
        try:
            values = tuple(float(value) for value in position)
        except (TypeError, ValueError):
            return None
        if len(values) != 3 or not all(math.isfinite(value) for value in values):
            return None
        return values  # type: ignore[return-value]

    def _slice_storage_directory(self) -> str:
        """Resolve and preflight the user-configured app-managed map storage."""
        runtime_settings = getattr(self, "_runtime_settings", None)
        if runtime_settings is None:
            from caveviewer.gui.preferences import load_preferences

            configured = str(load_preferences()["map_library_dir"]).strip()
        else:
            configured = runtime_settings.map_library_configuration().directory
        if not configured:
            raise ValueError("The Preferences map-storage folder is not configured.")
        directory = os.path.abspath(os.path.expanduser(configured))
        os.makedirs(directory, exist_ok=True)
        if not os.path.isdir(directory):
            raise ValueError("The Preferences map-storage folder is unavailable.")
        return directory

    def _slice_cave_name(self) -> str:
        """Return the original cave label used as the stable slice-name prefix."""
        manifest = getattr(self, "manifest", None)
        slice_metadata = (
            manifest.get(map_slicing.SLICE_MANIFEST_KEY)
            if isinstance(manifest, Mapping)
            else None
        )
        root_cave_name = (
            slice_metadata.get("root_cave_name")
            if isinstance(slice_metadata, Mapping)
            else None
        )
        if root_cave_name:
            root_label = os.path.basename(str(root_cave_name).strip())
            if root_label:
                # This is already a display label, not a source filename.  In
                # particular, preserve a legitimate cave-name suffix such as
                # ".2" instead of treating it as a file extension.
                return root_label
        map_root = getattr(self, "map_root", None)
        if map_root:
            # The selected folder is the name the Map Library presented to the
            # user. Prefer it over opaque source-model names such as ``D5.obj``
            # so a new slice can inherit the original cave's metadata.
            map_label = os.path.basename(os.path.normpath(str(map_root).strip()))
            if map_label:
                return map_label
        source_obj = manifest.get("source_obj") if isinstance(manifest, Mapping) else None
        raw_name = str(source_obj or "Cave").strip()
        return os.path.splitext(os.path.basename(raw_name))[0] or "Cave"

    def _clear_slice_context(self) -> None:
        self._ensure_capture_runtime().clear_slice_context()

    def _slice_unavailable(self, detail: str) -> None:
        self._show_capture_status(
            "Slice unavailable",
            detail,
            kind="error",
            duration=self._ensure_artifact_capture_presentation().confirmation_seconds,
        )

    def _start_slice_countdown(self) -> bool:
        """Preflight a Ctrl+C slice action and arm the shared capture countdown."""
        if not self._has_map_loaded:
            return False
        selection = self._ensure_slice_selection_controller()
        if (
            selection.countdown_active
            or selection.selection_active
            or selection.saving
            or self._ensure_slice_export_controller().active
        ):
            return False
        if self._capture_start_blocked(CaptureOwner.SLICE):
            return False
        source_cache_dir = getattr(self, "cache_dir", None)
        if not source_cache_dir or not os.path.isdir(source_cache_dir):
            self._slice_unavailable("This map has no readable precompiled cache.")
            return False
        try:
            map_slicing.validate_slice_source(source_cache_dir)
            storage_parent = self._slice_storage_directory()
            root_cave_name = self._slice_cave_name()
            display_name = map_slicing.next_slice_display_name(
                storage_parent,
                root_cave_name,
            )
        except Exception as exc:
            self._slice_unavailable(str(exc))
            return False

        color_picker = getattr(self, "color_picker", None)
        if color_picker is not None:
            color_picker.hide()
        controls_overlay = getattr(self, "controls_overlay", None)
        if controls_overlay is not None and controls_overlay.is_manual_mode:
            controls_overlay.hide_help()
        if not selection.start_countdown(
            now=time.perf_counter(),
            start_number=self.SLICE_COUNTDOWN_START_NUMBER,
        ):
            return False
        self._slice_source_cache_dir = str(source_cache_dir)
        self._slice_storage_parent = storage_parent
        self._slice_display_base = display_name
        self._slice_root_cave_name = root_cave_name
        _LOG.info(
            "Slice countdown started. Press %s+C to stop or Escape to cancel.",
            self._primary_shortcut_label(),
        )
        return True

    def _start_slice_export(
        self,
        anchors: SliceAnchors,
        *,
        closing: bool = False,
    ) -> bool:
        """Build and launch a worker request after active slicing ends."""
        source_cache_dir = getattr(self, "_slice_source_cache_dir", None)
        storage_parent = getattr(self, "_slice_storage_parent", None)
        if not source_cache_dir or not storage_parent:
            self._ensure_slice_selection_controller().complete_export()
            self._clear_slice_context()
            self._slice_unavailable("The active slice no longer has a save location.")
            return False
        root_cave_name = (
            getattr(self, "_slice_root_cave_name", None)
            or self._slice_cave_name()
        )
        display_name = map_slicing.sanitize_slice_name(
            getattr(self, "_slice_display_base", None)
            or map_slicing.next_slice_display_name(storage_parent, root_cave_name)
        )
        try:
            request = map_slicing.SliceExportRequest(
                source_cache_dir=source_cache_dir,
                output_dir=map_slicing.unique_slice_output_dir(
                    storage_parent,
                    display_name,
                ),
                bounds=map_slicing.SliceBounds.from_anchors(
                    anchors.start,
                    anchors.end,
                    padding=self.SLICE_PADDING,
                ),
                entry_position=anchors.start,
                display_name=display_name,
                root_cave_name=root_cave_name,
            )
        except Exception as exc:
            self._ensure_slice_selection_controller().complete_export()
            self._clear_slice_context()
            self._show_artifact_capture_status(
                self._ensure_artifact_capture_presentation().failed_status(
                    "Slice",
                    str(exc),
                )
            )
            return False

        failure = self._ensure_slice_export_controller().start(request)
        if failure is not None:
            self._ensure_slice_selection_controller().complete_export()
            self._clear_slice_context()
            self._show_artifact_capture_status(
                self._ensure_artifact_capture_presentation().failed_status(
                    "Slice",
                    failure.error,
                )
            )
            return False
        if not closing:
            self._show_artifact_capture_status(
                self._ensure_artifact_capture_presentation().saving_status(
                    "Slice",
                    cancelable=True,
                )
            )
        _LOG.info("Started slice export to %s", request.output_dir)
        return True

    def _finish_active_slice(self, *, closing: bool = False) -> bool:
        selection = self._ensure_slice_selection_controller()
        position = self._slice_camera_position()
        if position is None:
            if closing:
                selection.cancel_selection()
                self._clear_slice_context()
                self._show_artifact_capture_status(
                    self._ensure_artifact_capture_presentation().failed_status(
                        "Slice",
                        "Could not read the final camera position.",
                    )
                )
                return False
            self._slice_unavailable("Could not read the current camera position.")
            return False
        anchors = selection.finish_selection(position)
        if anchors is None:
            if closing:
                selection.cancel_selection()
                self._clear_slice_context()
                self._show_artifact_capture_status(
                    self._ensure_artifact_capture_presentation().failed_status(
                        "Slice",
                        "Could not finalize the active slice.",
                    )
                )
            return False
        return self._start_slice_export(anchors, closing=closing)

    def _toggle_slice(self) -> bool:
        """Use Ctrl/Cmd+C to start/cancel a countdown or finish an active slice."""
        selection = self._ensure_slice_selection_controller()
        exporter = self._ensure_slice_export_controller()
        if exporter.active or selection.saving:
            self._show_artifact_capture_status(
                self._ensure_artifact_capture_presentation().saving_status(
                    "Slice",
                    cancelable=True,
                )
            )
            return True
        if selection.countdown_active:
            selection.cancel_countdown()
            self._clear_slice_context()
            self._show_artifact_capture_status(
                self._ensure_artifact_capture_presentation().canceled_status("Slice"),
                now=time.perf_counter(),
            )
            return True
        if selection.selection_active:
            return self._finish_active_slice()
        return self._start_slice_countdown()

    def _cancel_slice_interaction(self) -> bool:
        """Honor Escape for a user-owned slice without canceling close finalization."""
        if self._exit_capture_finalization_active():
            return False
        selection = self._ensure_slice_selection_controller()
        if selection.countdown_active:
            selection.cancel_countdown()
            self._clear_slice_context()
        elif selection.selection_active:
            selection.cancel_selection()
            self._clear_slice_context()
        elif self._ensure_slice_export_controller().active:
            if not self._ensure_slice_export_controller().request_cancel():
                return False
            self._show_artifact_capture_status(
                self._ensure_artifact_capture_presentation().canceling_status(
                    "Slice"
                )
            )
            return True
        else:
            return False
        self._show_artifact_capture_status(
            self._ensure_artifact_capture_presentation().canceled_status(
                "Slice",
                after_escape=True,
            ),
            now=time.perf_counter(),
        )
        return True

    def _cancel_active_capture(self) -> bool:
        """Cancel the one recording, trace, or slice lifecycle owning capture."""
        if self._exit_capture_finalization_active():
            return False
        owner = self._capture_owner()
        if owner is CaptureOwner.VIDEO:
            return self._cancel_recording_capture()
        if owner is CaptureOwner.DIVE_TRACE:
            return self._cancel_manual_dive_trace_capture()
        if owner is CaptureOwner.SLICE:
            return self._cancel_slice_interaction()
        return False

    def _update_slice_export(self, *, now: float | None = None) -> None:
        """Advance countdown anchors and apply child export outcomes on the UI thread."""
        current_time = time.perf_counter() if now is None else now
        selection = self._ensure_slice_selection_controller()
        if selection.countdown_ready(now=current_time):
            position = self._slice_camera_position()
            if position is None or not selection.begin_selection(position):
                selection.complete_export()
                self._clear_slice_context()
                self._slice_unavailable("Could not read the current camera position.")

        exporter = self._ensure_slice_export_controller()
        for update in exporter.poll():
            if isinstance(update, SliceExportSucceeded):
                selection.complete_export()
                self._clear_slice_context()
                try:
                    from caveviewer.gui.map_history import remember_recent_map_path

                    remember_recent_map_path(update.output_dir)
                except Exception:
                    pass
                if self._exit_capture_finalization_active():
                    # Reveal only once exit finalization is ready to hand the
                    # window back to the platform; some file managers would
                    # otherwise focus the viewer again before it closes.
                    self._slice_reveal_before_close = True
                    self._slice_reveal_output_path = update.output_dir
                else:
                    self._show_artifact_capture_status(
                        self._ensure_artifact_capture_presentation().saved_status(
                            "Slice",
                            update.output_dir,
                            now=current_time,
                            reveal=True,
                        ),
                        now=current_time,
                    )
            elif isinstance(update, SliceExportCanceled):
                selection.complete_export()
                self._clear_slice_context()
                if not self._exit_capture_finalization_active():
                    self._show_artifact_capture_status(
                        self._ensure_artifact_capture_presentation().canceled_status(
                            "Slice",
                            after_escape=True,
                        ),
                        now=current_time,
                    )
            elif isinstance(update, SliceExportFailed):
                selection.complete_export()
                self._clear_slice_context()
                self._show_artifact_capture_status(
                    self._ensure_artifact_capture_presentation().failed_status(
                        "Slice",
                        update.error,
                    ),
                    now=current_time,
                )

    def _start_manual_dive_trace_countdown(self) -> bool:
        """Arm a visible countdown before collecting a manual dive trace."""
        if (
            not self._has_map_loaded
            or getattr(self, "_manual_dive_trace", None) is not None
        ):
            return False
        if self._capture_start_blocked(CaptureOwner.DIVE_TRACE):
            return False
        controller = self._ensure_manual_dive_trace_controller()
        if controller.countdown_active:
            return False

        color_picker = getattr(self, "color_picker", None)
        if color_picker is not None:
            color_picker.hide()
        controls_overlay = getattr(self, "controls_overlay", None)
        if controls_overlay is not None and controls_overlay.is_manual_mode:
            controls_overlay.hide_help()

        now = time.perf_counter()
        controller.start_countdown(
            now=now,
            start_number=self.MANUAL_DIVE_TRACE_COUNTDOWN_START_NUMBER,
        )
        _LOG.info(
            "Manual Guided Dive trace countdown started. "
            "Press %s+T to stop or Escape to cancel.",
            self._primary_shortcut_label(),
        )
        return True

    def _start_manual_dive_trace(self) -> bool:
        if (
            not self._has_map_loaded
            or getattr(self, "_manual_dive_trace", None) is not None
        ):
            return False
        if self._capture_start_blocked(CaptureOwner.DIVE_TRACE):
            return False
        pose = self._manual_dive_trace_pose()
        if pose is None:
            _LOG.warning("Manual Guided Dive trace could not read the camera pose.")
            return False
        map_root = getattr(self, "map_root", None)
        if not map_root:
            _LOG.warning(
                "Manual Guided Dive trace could not start because the map root is unknown."
            )
            return False
        recorder = manual_dive_trace.ManualDiveTraceRecorder(
            manual_dive_trace.manual_dive_trace_directory(map_root),
            map_context=manual_dive_trace.manual_dive_trace_map_context(
                self.manifest
            ),
        )
        try:
            output_path = recorder.start(pose)
        except Exception as exc:
            _LOG.warning("Manual Guided Dive trace could not start: %s", exc)
            return False
        self._manual_dive_trace = recorder
        _LOG.info(
            "Manual Guided Dive trace started. Press %s+T to stop and save or "
            "Escape to cancel: %s",
            self._primary_shortcut_label(),
            output_path,
        )
        return True

    def _stop_manual_dive_trace(self, *, reason: str) -> bool:
        self._ensure_manual_dive_trace_controller().clear_countdown()
        recorder = getattr(self, "_manual_dive_trace", None)
        if recorder is None:
            return False
        try:
            output_path = recorder.stop(
                self._manual_dive_trace_pose(),
                reason=reason,
            )
        except Exception as exc:
            _LOG.warning("Manual Guided Dive trace could not stop cleanly: %s", exc)
            output_path = recorder.output_path
        self._manual_dive_trace = None
        writers = getattr(self, "_manual_dive_trace_writers", None)
        if writers is None:
            writers = []
            self._manual_dive_trace_writers = writers
        show_completion = reason in {"user_stopped", "writer_failed"}
        writers.append(
            _PendingManualDiveTraceWriter(
                recorder=recorder,
                show_completion=show_completion,
                reveal_on_success=reason == "user_stopped",
            )
        )
        _LOG.info("Manual Guided Dive trace is saving: %s", output_path)
        if show_completion:
            self._show_artifact_capture_status(
                self._ensure_artifact_capture_presentation().saving_status(
                    "Dive trace",
                    cancelable=True,
                )
            )
        return True

    def _cancel_manual_dive_trace_capture(self) -> bool:
        """Cancel trace countdown or discard the active/pending trace writer."""
        if self._exit_capture_finalization_active():
            return False
        canceled = False
        cleanup_pending = False
        controller = self._ensure_manual_dive_trace_controller()
        if controller.countdown_active:
            controller.clear_countdown()
            canceled = True

        writers = getattr(self, "_manual_dive_trace_writers", None)
        if writers is None:
            writers = []
            self._manual_dive_trace_writers = writers

        recorder = getattr(self, "_manual_dive_trace", None)
        if recorder is not None:
            try:
                requested = bool(recorder.cancel())
            except Exception as exc:
                _LOG.warning("Manual Guided Dive trace could not cancel: %s", exc)
                requested = False
            if requested:
                self._manual_dive_trace = None
                writers.append(
                    _PendingManualDiveTraceWriter(
                        recorder=recorder,
                        show_completion=True,
                        reveal_on_success=False,
                    )
                )
                canceled = True
                cleanup_pending = True
            else:
                self._show_artifact_capture_status(
                    self._ensure_artifact_capture_presentation().cancellation_failed_status(
                        "Dive trace",
                        "The trace writer could not start cleanup.",
                    ),
                    now=time.perf_counter(),
                )
                # Keep the recorder attached so its resources remain owned and
                # a later Escape or normal stop can retry cleanup safely.
                return True

        for index, pending_writer in enumerate(tuple(writers)):
            if pending_writer.recorder is recorder:
                continue
            try:
                requested = bool(pending_writer.recorder.cancel())
            except Exception as exc:
                _LOG.warning(
                    "Pending Manual Guided Dive trace could not cancel: %s",
                    exc,
                )
                requested = False
            if not requested:
                continue
            writers[index] = _PendingManualDiveTraceWriter(
                recorder=pending_writer.recorder,
                show_completion=True,
                reveal_on_success=False,
            )
            canceled = True
            cleanup_pending = True

        if not canceled:
            return False
        presentation = self._ensure_artifact_capture_presentation()
        self._show_artifact_capture_status(
            (
                presentation.canceling_status("Dive trace")
                if cleanup_pending
                else presentation.canceled_status(
                    "Dive trace",
                    after_escape=True,
                )
            ),
            now=time.perf_counter(),
        )
        _LOG.info("Manual Guided Dive trace canceled with Escape.")
        return True

    def _toggle_manual_dive_trace(self) -> bool:
        if getattr(self, "_manual_dive_trace", None) is not None:
            return self._stop_manual_dive_trace(reason="user_stopped")
        controller = self._ensure_manual_dive_trace_controller()
        if controller.countdown_active:
            controller.clear_countdown()
            now = time.perf_counter()
            self._show_artifact_capture_status(
                self._ensure_artifact_capture_presentation().canceled_status(
                    "Dive trace"
                ),
                now=now,
            )
            _LOG.info("Manual Guided Dive trace countdown canceled.")
            return True
        return self._start_manual_dive_trace_countdown()

    def _update_manual_dive_trace(self, *, now: float | None = None) -> None:
        """Advance the trace countdown and apply finished writer outcomes."""
        now = time.perf_counter() if now is None else now
        controller = self._ensure_manual_dive_trace_controller()
        if controller.countdown_ready(now=now):
            controller.clear_countdown()
            if not self._start_manual_dive_trace():
                self._show_capture_status(
                    "Dive trace unavailable",
                    "Could not start the trace for this map.",
                    kind="error",
                    duration=(
                        self._ensure_artifact_capture_presentation().confirmation_seconds
                    ),
                    now=now,
                )

        recorder = getattr(self, "_manual_dive_trace", None)
        if recorder is not None:
            if recorder.writer_failed:
                self._stop_manual_dive_trace(reason="writer_failed")
            else:
                pose = self._manual_dive_trace_pose()
                if pose is not None:
                    recorder.observe(pose)

        pending = getattr(self, "_manual_dive_trace_writers", [])
        for pending_writer in tuple(pending):
            result = pending_writer.recorder.poll_result()
            if result is None:
                continue
            pending.remove(pending_writer)
            show_completion = (
                pending_writer.show_completion
                and not self._exit_capture_finalization_active()
            )
            if result.canceled:
                if show_completion:
                    if result.error:
                        status = self._ensure_artifact_capture_presentation().cancellation_failed_status(
                            "Dive trace",
                            "The partial trace could not be removed.",
                        )
                    else:
                        status = self._ensure_artifact_capture_presentation().canceled_status(
                            "Dive trace",
                            after_escape=True,
                        )
                    self._show_artifact_capture_status(status, now=now)
                if result.error:
                    _LOG.warning(
                        "Manual Guided Dive trace cancellation cleanup failed: %s",
                        result.error,
                    )
                else:
                    _LOG.info(
                        "Manual Guided Dive trace canceled and partial output removed."
                    )
            elif result.completed:
                _LOG.info("Manual Guided Dive trace saved: %s", result.output_path)
                if show_completion:
                    status = self._ensure_artifact_capture_presentation().saved_status(
                        "Dive trace",
                        result.output_path,
                        now=now,
                        reveal=pending_writer.reveal_on_success,
                    )
                    self._show_artifact_capture_status(status, now=now)
            else:
                _LOG.warning(
                    "Manual Guided Dive trace failed to save: %s (%s)",
                    result.partial_path,
                    result.error or "unknown error",
                )
                if show_completion:
                    self._show_artifact_capture_status(
                        self._ensure_artifact_capture_presentation().failed_status(
                            "Dive trace",
                            result.error or "The trace writer did not finish.",
                        ),
                        now=now,
                    )
        self._drain_due_saved_artifact_reveals(now=now)

    def _mark_manual_dive_trace_discontinuity(
        self,
        before: manual_dive_trace.ManualDivePose | None,
        *,
        reason: str,
    ) -> None:
        recorder = getattr(self, "_manual_dive_trace", None)
        if recorder is None or before is None:
            return
        after = self._manual_dive_trace_pose()
        if after is None:
            return
        recorder.mark_discontinuity(before, after, reason=reason)
