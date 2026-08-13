"""Run bounded cave-slice export work in a spawned child process."""

from __future__ import annotations

from dataclasses import dataclass
import multiprocessing
import os
import queue
import shutil
import traceback
from multiprocessing.context import BaseContext
from typing import Any

from caveviewer.core.diagnostics.logging import configure_logging, get_logger
from caveviewer.core.map.slicing import (
    SliceExportCancelled,
    SliceExportRequest,
    export_slice,
)
from caveviewer.gui.platform.process_priority import lower_current_process_priority


_LOG = get_logger("SliceProcess")
SLICE_CHILD_NICE_INCREMENT = 5


@dataclass(frozen=True, slots=True)
class SliceProcessHandle:
    """Parent-owned child process, event queue, and cancellation primitive."""

    process: Any
    events: Any
    cancel_event: Any
    output_dir: str


def start_slice_process(
    request: SliceExportRequest,
    *,
    daemon: bool = False,
    context: BaseContext | None = None,
) -> SliceProcessHandle:
    """Spawn an export process without blocking the OpenGL render thread."""
    process_context = context or multiprocessing.get_context("spawn")
    events = process_context.Queue()
    cancel_event = process_context.Event()
    process = process_context.Process(
        target=_run_slice_process,
        args=(request, events, cancel_event),
        name="CaveViewer-slice",
    )
    process.daemon = bool(daemon)
    process.start()
    return SliceProcessHandle(
        process=process,
        events=events,
        cancel_event=cancel_event,
        output_dir=request.output_dir,
    )


def request_slice_process_cancel(handle: SliceProcessHandle | Any) -> bool:
    """Cooperatively request cancellation without terminating staging cleanup."""
    cancel_event = getattr(handle, "cancel_event", None)
    if cancel_event is None:
        return False
    try:
        cancel_event.set()
    except Exception:
        return False
    return True


def terminate_slice_process(
    process: Any,
    *,
    timeout: float = 2.0,
    output_dir: str | None = None,
) -> None:
    """Best-effort emergency termination and private-staging cleanup."""
    process_id = getattr(process, "pid", None)
    if process is None:
        if output_dir:
            cleanup_slice_staging_dirs(output_dir)
        return
    try:
        active = bool(process.is_alive())
    except Exception:
        active = False
    if active:
        try:
            process.terminate()
            process.join(timeout=timeout)
        except Exception as exc:
            _LOG.warning("Could not terminate slice process: %s", exc)
    try:
        active = bool(process.is_alive())
    except Exception:
        active = False
    if active:
        try:
            kill = getattr(process, "kill", None)
            if callable(kill):
                kill()
                process.join(timeout=timeout)
        except Exception as exc:
            _LOG.warning("Could not kill slice process: %s", exc)
    if output_dir:
        cleanup_slice_staging_dirs(output_dir, process_id=process_id)


def cleanup_slice_staging_dirs(
    output_dir: str,
    *,
    process_id: int | None = None,
) -> int:
    """Remove only matching private staging directories after a hard stop."""
    target = os.path.abspath(os.fspath(output_dir))
    parent = os.path.dirname(target)
    name = os.path.basename(target)
    if not name or not os.path.isdir(parent):
        return 0
    prefix = (
        f".{name}.tmp-{int(process_id)}-"
        if process_id is not None
        else f".{name}.tmp-"
    )
    removed = 0
    try:
        names = os.listdir(parent)
    except OSError:
        return 0
    for child_name in names:
        if not child_name.startswith(prefix) or child_name.endswith(".previous"):
            continue
        child_path = os.path.join(parent, child_name)
        if os.path.islink(child_path) or not os.path.isdir(child_path):
            continue
        try:
            shutil.rmtree(child_path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            _LOG.warning("Could not remove slice staging %s: %s", child_path, exc)
    return removed


def _run_slice_process(request: SliceExportRequest, events: Any, cancel_event: Any) -> None:
    """Child entry point.  Events are deliberately compact and picklable."""
    configure_logging(force=True)
    try:
        lower_current_process_priority(nice_increment=SLICE_CHILD_NICE_INCREMENT)
    except Exception:
        pass

    def progress(stage: str, fraction: float) -> None:
        _put_event(events, ("progress", str(stage), float(fraction)))

    try:
        result = export_slice(
            request,
            progress_cb=progress,
            cancel_requested=cancel_event.is_set,
        )
    except SliceExportCancelled:
        _put_event(events, ("cancelled",))
    except BaseException as exc:
        _put_event(events, ("error", str(exc), traceback.format_exc(limit=8)))
    else:
        _put_event(
            events,
            (
                "done",
                result.output_dir,
                result.triangle_count,
                result.chunk_count,
                result.texture_count,
            ),
        )


def _put_event(events: Any, event: tuple[Any, ...]) -> None:
    try:
        events.put(event)
    except (BrokenPipeError, EOFError, OSError, queue.Full):
        pass
    except Exception:
        pass
