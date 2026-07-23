"""Render-thread recording framebuffer capture resources.

The viewer window owns recording workflow decisions and the active encoder
session. This module owns the OpenGL resources and readback ring used to stage
raw framebuffer bytes for that encoder without making `viewer_window.py` own
the capture implementation details.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from caveviewer.gui import recording


_RecordingReadbackSlot = recording.RecordingReadbackSlot


class RecordingCaptureResources:
    """Manage recording framebuffer readback resources on the render thread."""

    def __init__(
        self,
        *,
        ctx: Any,
        buffer_count: int,
        readback_components: int,
        logger: Any,
        perf_counter: Callable[[], float],
        output_size: tuple[int, int] | None = None,
        capture_viewport: tuple[int, int, int, int] | None = None,
        readback_framebuffer: Any | None = None,
        readback_slots: list[_RecordingReadbackSlot] | None = None,
        readback_pending: list[_RecordingReadbackSlot] | None = None,
        readback_byte_count: int = 0,
    ) -> None:
        self.ctx = ctx
        self.buffer_count = int(buffer_count)
        self.readback_components = int(readback_components)
        self.logger = logger
        self.perf_counter = perf_counter
        self.output_size = output_size
        self.capture_viewport = capture_viewport
        self.readback_framebuffer = readback_framebuffer
        self.readback_slots = [] if readback_slots is None else readback_slots
        self.readback_pending = [] if readback_pending is None else readback_pending
        self.readback_byte_count = int(readback_byte_count)

    def capture_state(
        self,
    ) -> tuple[tuple[int, int], tuple[int, int, int, int], int]:
        """Return initialized output size, capture viewport, and byte count."""
        if self.output_size is None or self.capture_viewport is None:
            raise OSError("recording capture state is not initialized")
        byte_count = self.readback_byte_count
        if byte_count <= 0:
            raise OSError("recording readback buffers are not initialized")
        return self.output_size, self.capture_viewport, byte_count

    def release_framebuffer(self) -> None:
        """Release the optional output-sized framebuffer used for downscale readback."""
        framebuffer = self.readback_framebuffer
        self.readback_framebuffer = None
        if framebuffer is None:
            return
        try:
            framebuffer.release()
        except Exception:
            pass

    def discard_staged_frames(self) -> int:
        """Clear staged readback slots and return the number of dropped frames."""
        dropped = len(self.readback_pending)
        for slot in self.readback_pending:
            slot.in_flight = False
        self.readback_pending.clear()
        return dropped

    def release_buffers(self) -> None:
        """Release all readback buffers and clear staged-frame state."""
        self.discard_staged_frames()
        slots = self.readback_slots
        self.readback_slots = []
        self.readback_pending = []
        self.readback_byte_count = 0
        for slot in slots:
            try:
                slot.buffer.release()
            except Exception:
                pass

    def create_framebuffer(
        self,
        capture_size: tuple[int, int],
        output_size: tuple[int, int],
    ) -> Any | None:
        """Create an output-sized readback framebuffer when downscaling is needed."""
        self.release_framebuffer()
        if output_size == capture_size:
            return None

        # Downscale on the GPU before readback. Reading the full high-DPI
        # window framebuffer can block the render loop for tens of
        # milliseconds per recorded frame; reading the output-sized buffer
        # keeps the synchronized transfer much smaller.
        framebuffer = self.ctx.simple_framebuffer(output_size, components=4)
        framebuffer.viewport = (0, 0, output_size[0], output_size[1])
        self.readback_framebuffer = framebuffer
        return framebuffer

    def create_buffers(self, output_size: tuple[int, int]) -> None:
        """Allocate the fixed-size ring of pixel buffers used for readback."""
        self.release_buffers()
        width, height = output_size
        byte_count = width * height * self.readback_components
        slots: list[_RecordingReadbackSlot] = []
        try:
            for _ in range(self.buffer_count):
                slots.append(_RecordingReadbackSlot(self.ctx.buffer(reserve=byte_count)))
        except Exception:
            for slot in slots:
                try:
                    slot.buffer.release()
                except Exception:
                    pass
            raise
        self.readback_slots = slots
        self.readback_pending = []
        self.readback_byte_count = byte_count

    def free_readback_slot(self) -> _RecordingReadbackSlot | None:
        """Return the next available readback slot, if the ring has capacity."""
        for slot in self.readback_slots:
            if not slot.in_flight:
                return slot
        return None

    def copy_to_readback_framebuffer(
        self,
        readback_framebuffer: Any,
        output_size: tuple[int, int],
        capture_viewport: tuple[int, int, int, int],
    ) -> None:
        """Copy the current screen framebuffer into the output-sized framebuffer."""
        screen = self.ctx.screen
        previous_screen_viewport = getattr(screen, "viewport", None)
        previous_readback_viewport = getattr(readback_framebuffer, "viewport", None)
        width, height = output_size
        try:
            screen.viewport = capture_viewport
            readback_framebuffer.viewport = (0, 0, width, height)
            self.ctx.copy_framebuffer(readback_framebuffer, screen)
        finally:
            if previous_screen_viewport is not None:
                try:
                    screen.viewport = previous_screen_viewport
                except Exception:
                    pass
            if previous_readback_viewport is not None:
                try:
                    readback_framebuffer.viewport = previous_readback_viewport
                except Exception:
                    pass

    def stage_frame(
        self,
        *,
        render_frame: Callable[[Any, tuple[int, int]], None] | None = None,
    ) -> bool:
        """Stage one framebuffer frame into the next available readback slot."""
        output_size, capture_viewport, _byte_count = self.capture_state()
        slot = self.free_readback_slot()
        if slot is None:
            return False

        width, height = output_size
        readback_framebuffer = self.readback_framebuffer
        if readback_framebuffer is None:
            self.ctx.screen.read_into(
                slot.buffer,
                viewport=capture_viewport,
                components=self.readback_components,
                alignment=1,
            )
        else:
            if render_frame is None:
                self.copy_to_readback_framebuffer(
                    readback_framebuffer,
                    output_size,
                    capture_viewport,
                )
            else:
                render_frame(readback_framebuffer, output_size)
            readback_framebuffer.read_into(
                slot.buffer,
                viewport=(0, 0, width, height),
                components=self.readback_components,
                alignment=1,
            )

        slot.in_flight = True
        self.readback_pending.append(slot)
        return True

    def drain_staged_frames(
        self,
        *,
        frame_queue: Any | None,
        enqueue_frame: Callable[[bytes], bool],
        stop_recording: Callable[[], None],
    ) -> float:
        """Move the oldest staged frame into the encoder queue when the ring is full."""
        pending = self.readback_pending
        slots = self.readback_slots
        if (
            not pending
            or not slots
            or len(pending) < len(slots)
            or frame_queue is None
            or frame_queue.full()
        ):
            return 0.0

        _output_size, _capture_viewport, byte_count = self.capture_state()
        slot = pending.pop(0)
        frame = None
        t_read = self.perf_counter()
        try:
            frame = slot.buffer.read(size=byte_count)
            read_ms = (self.perf_counter() - t_read) * 1000.0
            if len(frame) != byte_count:
                self.logger.warning(
                    "Recording stopped because framebuffer byte size changed: "
                    f"actual={len(frame)} expected={byte_count}."
                )
                stop_recording()
                return read_ms
            enqueue_frame(frame)
            return read_ms
        finally:
            slot.in_flight = False
            frame = None
