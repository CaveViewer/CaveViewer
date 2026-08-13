"""Viewer-owned lifecycle controller for one background cave-slice export."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import queue
from typing import Any, Callable

from caveviewer.core.map.slicing import SliceExportRequest
from caveviewer.gui.slice_process import (
    cleanup_slice_staging_dirs,
    request_slice_process_cancel,
    start_slice_process,
)


class SliceExportState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SliceExportProgress:
    request: SliceExportRequest
    stage: str
    fraction: float


@dataclass(frozen=True, slots=True)
class SliceExportSucceeded:
    request: SliceExportRequest
    output_dir: str
    triangle_count: int
    chunk_count: int
    texture_count: int


@dataclass(frozen=True, slots=True)
class SliceExportCanceled:
    request: SliceExportRequest


@dataclass(frozen=True, slots=True)
class SliceExportFailed:
    request: SliceExportRequest
    error: str


SliceExportUpdate = (
    SliceExportProgress | SliceExportSucceeded | SliceExportCanceled | SliceExportFailed
)


class SliceExportController:
    """Turn spawned-process events into a small, render-thread-safe API."""

    def __init__(
        self,
        *,
        start_process: Callable[..., Any] = start_slice_process,
        request_cancel: Callable[[Any], bool] = request_slice_process_cancel,
    ) -> None:
        self._start_process = start_process
        self._request_cancel = request_cancel
        self._handle: Any | None = None
        self.request: SliceExportRequest | None = None
        self.state = SliceExportState.IDLE
        self.stage = ""
        self.fraction = 0.0
        self._exit_without_event_polls = 0

    @property
    def active(self) -> bool:
        return self.state is SliceExportState.RUNNING

    def start(
        self,
        request: SliceExportRequest,
    ) -> SliceExportFailed | None:
        if self.active:
            return SliceExportFailed(request, "Another slice export is already running.")
        self.request = request
        self.state = SliceExportState.RUNNING
        self.stage = "starting slice export"
        self.fraction = 0.0
        self._exit_without_event_polls = 0
        try:
            self._handle = self._start_process(request, daemon=False)
        except Exception as exc:
            self._handle = None
            self.state = SliceExportState.FAILED
            return SliceExportFailed(request, str(exc))
        return None

    def request_cancel(self) -> bool:
        if not self.active or self._handle is None:
            return False
        return bool(self._request_cancel(self._handle))

    def poll(self) -> tuple[SliceExportUpdate, ...]:
        request = self.request
        handle = self._handle
        if request is None or handle is None or not self.active:
            return ()

        updates: list[SliceExportUpdate] = []
        while True:
            try:
                event = handle.events.get_nowait()
            except queue.Empty:
                break
            except Exception as exc:
                updates.append(self._fail(request, f"Couldn't read slice progress: {exc}"))
                return tuple(updates)
            update = self._handle_event(request, event)
            if update is not None:
                updates.append(update)
            if not self.active:
                return tuple(updates)

        process = getattr(handle, "process", None)
        exitcode = getattr(process, "exitcode", None)
        if exitcode is not None and self.active:
            self._exit_without_event_polls += 1
            if self._exit_without_event_polls >= 3:
                process_id = getattr(process, "pid", None)
                if isinstance(process_id, int) and process_id > 0:
                    cleanup_slice_staging_dirs(
                        request.output_dir,
                        process_id=process_id,
                    )
                updates.append(
                    self._fail(
                        request,
                        "Slice export process exited without reporting a result "
                        f"(exit code {exitcode}).",
                    )
                )
        else:
            self._exit_without_event_polls = 0
        return tuple(updates)

    def _handle_event(
        self,
        request: SliceExportRequest,
        event: Any,
    ) -> SliceExportUpdate | None:
        if not isinstance(event, tuple) or not event:
            return self._fail(request, "Slice export process sent an invalid update.")
        kind = event[0]
        if kind == "progress":
            stage = str(event[1]) if len(event) > 1 else "exporting slice"
            try:
                fraction = float(event[2]) if len(event) > 2 else 0.0
            except (TypeError, ValueError):
                fraction = 0.0
            self.stage = stage
            self.fraction = min(1.0, max(0.0, fraction))
            return SliceExportProgress(request, self.stage, self.fraction)
        if kind == "done":
            output_dir = str(event[1]) if len(event) > 1 else request.output_dir
            triangle_count = _event_int(event, 2)
            chunk_count = _event_int(event, 3)
            texture_count = _event_int(event, 4)
            self._finish(SliceExportState.SUCCEEDED)
            return SliceExportSucceeded(
                request,
                output_dir,
                triangle_count,
                chunk_count,
                texture_count,
            )
        if kind == "cancelled":
            self._finish(SliceExportState.CANCELED)
            return SliceExportCanceled(request)
        if kind == "error":
            return self._fail(
                request,
                str(event[1]) if len(event) > 1 else "Slice export failed.",
            )
        return self._fail(request, f"Slice export process sent unknown event {kind!r}.")

    def _fail(self, request: SliceExportRequest, error: str) -> SliceExportFailed:
        self._finish(SliceExportState.FAILED)
        return SliceExportFailed(request, error)

    def _finish(self, state: SliceExportState) -> None:
        handle = self._handle
        self._handle = None
        self.state = state
        process = getattr(handle, "process", None)
        join = getattr(process, "join", None)
        if callable(join):
            try:
                join(timeout=0.0)
            except Exception:
                pass


def _event_int(event: tuple[Any, ...], index: int) -> int:
    try:
        return max(0, int(event[index])) if len(event) > index else 0
    except (TypeError, ValueError):
        return 0
