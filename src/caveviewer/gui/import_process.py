"""Run first-time map imports in a child process.

The viewer owns OpenGL and desktop events.  Importing owns heavy OBJ/GLB
parsing, cache construction, and texture staging.  Keeping those in a spawned
process prevents long imports from starving the viewer event loop and lets the
viewer recover cleanly when an import fails.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import queue
import shutil
import threading
import time
import traceback
from dataclasses import dataclass
from multiprocessing.context import BaseContext
from typing import Any

from caveviewer.core.chunking.capacity import (
    InsufficientDiskSpaceError,
    InsufficientImportMemoryError,
)
from caveviewer.core.chunking.staging import ImportPaused
from caveviewer.core.diagnostics.logging import configure_logging, get_logger
from caveviewer.gui.platform.process_priority import lower_current_process_priority


_LOG = get_logger("ImportProcess")

ImportEvent = tuple[Any, ...]
IMPORT_HEARTBEAT_INTERVAL_SECONDS = 5.0
IMPORT_CHILD_NICE_INCREMENT = 5
IMPORT_NATIVE_THREAD_LIMIT = "1"
IMPORT_NATIVE_THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "BLIS_NUM_THREADS",
)


@dataclass(frozen=True)
class ImportProcessHandle:
    """Handle for a spawned import process and its event queue."""

    process: Any
    events: Any
    commands: Any
    cache_dir: str


class _ImportEventLogHandler(logging.Handler):
    """Send child-process log records to the parent import event queue."""

    def __init__(self, events: Any):
        super().__init__(logging.DEBUG)
        self._events = events

    def emit(self, record: logging.LogRecord) -> None:
        try:
            component = getattr(record, "component", "CaveViewer")
            _put_event(
                self._events,
                ("log", int(record.levelno), str(component), record.getMessage()),
            )
        except Exception:
            pass


def _configure_import_child_logging(events: Any) -> None:
    configure_logging(force=True, handlers=(_ImportEventLogHandler(events),))


def source_path_from_descriptor(model_descriptor: dict) -> str:
    """Return the source path from a supported model descriptor."""
    source_path = model_descriptor.get("obj_path") or model_descriptor.get("glb_path")
    if not source_path:
        raise ValueError("Import descriptor is missing obj_path or glb_path.")
    return str(source_path)


def cache_dir_for_descriptor(model_descriptor: dict) -> str:
    """Return the managed cache directory that an import will publish into."""
    from caveviewer.core.map.cache_paths import map_cache_build_dir

    return map_cache_build_dir(source_path_from_descriptor(model_descriptor))


def start_import_process(
    model_descriptor: dict,
    textures_dir: str,
    *,
    context: BaseContext | None = None,
) -> ImportProcessHandle:
    """Start a spawned import process and return its event handle."""
    process_context = context or multiprocessing.get_context("spawn")
    cache_dir = cache_dir_for_descriptor(model_descriptor)
    events = process_context.Queue()
    commands = process_context.Queue()
    process = process_context.Process(
        target=_run_import_process,
        args=(dict(model_descriptor), str(textures_dir), events, commands),
        name="CaveViewer-import",
    )
    process.daemon = True
    process.start()
    return ImportProcessHandle(
        process=process,
        events=events,
        commands=commands,
        cache_dir=cache_dir,
    )


def terminate_import_process(
    process: Any,
    *,
    timeout: float = 2.0,
    cache_dir: str | None = None,
) -> None:
    """Best-effort termination for a still-running import child."""
    process_id = getattr(process, "pid", None)
    if process is None:
        if cache_dir:
            cleanup_import_staging_dirs(cache_dir)
        return
    try:
        if not process.is_alive():
            if cache_dir:
                cleanup_import_staging_dirs(cache_dir, process_id=process_id)
            return
    except Exception:
        return

    try:
        process.terminate()
    except Exception as exc:
        _LOG.warning("Could not terminate import process: %s", exc)
        return

    try:
        process.join(timeout=timeout)
    except Exception:
        return

    try:
        if process.is_alive():
            kill = getattr(process, "kill", None)
            if callable(kill):
                kill()
                process.join(timeout=timeout)
    except Exception as exc:
        _LOG.warning("Could not kill import process after terminate: %s", exc)
    finally:
        try:
            alive = process.is_alive()
        except Exception:
            alive = False
        if cache_dir and not alive:
            cleanup_import_staging_dirs(cache_dir, process_id=process_id)


def cleanup_import_staging_dirs(
    cache_dir: str,
    *,
    process_id: int | None = None,
) -> int:
    """Remove abandoned private staging directories for one managed cache."""
    cache_dir = os.path.abspath(cache_dir)
    cache_parent = os.path.dirname(cache_dir)
    cache_name = os.path.basename(cache_dir)
    if not cache_name or not os.path.isdir(cache_parent):
        return 0

    staging_prefix = (
        f".{cache_name}.tmp-{int(process_id)}-"
        if process_id is not None
        else f".{cache_name}.tmp-"
    )
    cleaned = 0
    try:
        names = os.listdir(cache_parent)
    except OSError as exc:
        _LOG.warning("Could not inspect import staging directory %s: %s", cache_parent, exc)
        return 0

    for name in names:
        if not name.startswith(staging_prefix) or name.endswith(".previous"):
            continue
        staging_path = os.path.join(cache_parent, name)
        if os.path.islink(staging_path) or not os.path.isdir(staging_path):
            continue
        try:
            shutil.rmtree(staging_path)
            cleaned += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            _LOG.warning("Could not remove abandoned import staging %s: %s", staging_path, exc)

    if cleaned:
        _LOG.info(
            "Removed %d abandoned import staging director%s for %s.",
            cleaned,
            "y" if cleaned == 1 else "ies",
            cache_dir,
        )
    return cleaned


def configure_import_child_runtime() -> None:
    """Reduce the import child's impact on the interactive desktop."""
    capped = _limit_native_threads()
    if capped:
        _LOG.info(
            "Import child native thread caps applied: %s=%s.",
            ", ".join(capped),
            IMPORT_NATIVE_THREAD_LIMIT,
        )
    lower_current_process_priority(nice_increment=IMPORT_CHILD_NICE_INCREMENT)


def _limit_native_threads(environ: dict[str, str] | None = None) -> list[str]:
    """Set conservative native-library thread caps before NumPy is imported."""
    env = os.environ if environ is None else environ
    capped: list[str] = []
    for name in IMPORT_NATIVE_THREAD_ENV_VARS:
        if str(env.get(name, "")).strip():
            continue
        env[name] = IMPORT_NATIVE_THREAD_LIMIT
        capped.append(name)
    return capped


def _put_event(events: Any, event: ImportEvent) -> None:
    try:
        events.put(event)
    except Exception:
        # If the parent disappears, let the import continue to either publish
        # atomically or fail through the normal cleanup path.
        pass


def _start_heartbeat_thread(
    events: Any,
    state: dict[str, float | str],
    state_lock: threading.Lock,
    stop_event: threading.Event,
    *,
    interval_seconds: float = IMPORT_HEARTBEAT_INTERVAL_SECONDS,
) -> threading.Thread:
    """Emit periodic liveness/RAM snapshots while the import child is running."""

    def loop() -> None:
        from caveviewer.core.hardware import system_memory

        while not stop_event.wait(interval_seconds):
            with state_lock:
                stage = str(state["stage"])
                fraction = float(state["fraction"])
                elapsed = time.monotonic() - float(state["started_at"])
            snapshot = system_memory.detect_ram_snapshot()
            available_bytes = snapshot.available_bytes if snapshot else None
            total_bytes = snapshot.total_bytes if snapshot else None
            _put_event(
                events,
                (
                    "heartbeat",
                    stage,
                    fraction,
                    elapsed,
                    available_bytes,
                    total_bytes,
                ),
            )

    thread = threading.Thread(target=loop, name="CaveViewer-import-heartbeat", daemon=True)
    thread.start()
    return thread


def _start_command_thread(
    commands: Any,
    pause_event: threading.Event,
    stop_event: threading.Event,
) -> threading.Thread | None:
    """Listen for cooperative commands from the viewer process."""
    if commands is None:
        return None

    def loop() -> None:
        while not stop_event.is_set():
            try:
                command = commands.get(timeout=0.25)
            except queue.Empty:
                continue
            except Exception:
                return
            kind = command[0] if isinstance(command, tuple) and command else command
            if kind == "pause":
                pause_event.set()

    thread = threading.Thread(target=loop, name="CaveViewer-import-command", daemon=True)
    thread.start()
    return thread


def _actionable_import_failure(exc: BaseException) -> tuple[str, str] | None:
    """Return a user-actionable message/suggestion for expected import failures."""
    if isinstance(exc, InsufficientImportMemoryError):
        return (
            str(exc),
            "Close memory-heavy applications and retry, or import this map on a "
            "machine with more RAM. Reducing source-model detail before import "
            "can also lower the peak memory requirement.",
        )

    if isinstance(exc, InsufficientDiskSpaceError):
        return (
            str(exc),
            "Free space on the cache drive and retry, or move CaveViewer's map "
            "cache to a larger drive with CAVEVIEWER_MAP_CACHE_DIR.",
        )

    return None


def _run_import_process(
    model_descriptor: dict,
    textures_dir: str,
    events: Any,
    commands: Any = None,
) -> None:
    """Child-process entry point. Sends progress, done, or error events."""
    _configure_import_child_logging(events)
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    command_stop: threading.Event | None = None
    command_thread: threading.Thread | None = None
    pause_event = threading.Event()
    try:
        configure_import_child_runtime()
        heartbeat_stop = threading.Event()
        command_stop = threading.Event()
        heartbeat_state: dict[str, float | str] = {
            "stage": "starting import",
            "fraction": 0.0,
            "started_at": time.monotonic(),
        }
        heartbeat_lock = threading.Lock()
        heartbeat_thread = _start_heartbeat_thread(
            events,
            heartbeat_state,
            heartbeat_lock,
            heartbeat_stop,
        )
        command_thread = _start_command_thread(
            commands,
            pause_event,
            command_stop,
        )

        from caveviewer.app import import_and_cache_any
        source_path = source_path_from_descriptor(model_descriptor)
        _LOG.info("Import process started for %s.", source_path)

        def on_progress(stage: str, fraction: float) -> None:
            stage = str(stage)
            fraction = float(fraction)
            with heartbeat_lock:
                heartbeat_state["stage"] = stage
                heartbeat_state["fraction"] = fraction
            _put_event(events, ("progress", stage, fraction))

        on_progress("starting import", 0.0)
        cache_dir = import_and_cache_any(
            model_descriptor,
            textures_dir,
            force_rebuild=False,
            extra_progress_cb=on_progress,
            console_progress=False,
            pause_requested=pause_event.is_set,
        )
        _put_event(events, ("done", cache_dir, cache_dir))
    except KeyboardInterrupt:
        _LOG.info("Import process interrupted by user.")
        _put_event(events, ("cancelled",))
    except BaseException as exc:
        if isinstance(exc, ImportPaused):
            resume_dir = getattr(exc, "resume_dir", None)
            _LOG.info("Import paused; resume checkpoint saved in %s.", resume_dir)
            _put_event(events, ("paused", resume_dir or ""))
            return

        actionable_failure = _actionable_import_failure(exc)
        if actionable_failure is not None:
            message, suggestion = actionable_failure
            _LOG.error("Import failed: %s", message)
            _LOG.error("Suggestion: %s", suggestion)
            _put_event(events, ("error", message, "", suggestion))
            return

        trace = traceback.format_exc()
        _LOG.error("Import process failed: %s\n%s", exc, trace)
        _put_event(events, ("error", str(exc), trace))
    finally:
        if command_stop is not None:
            command_stop.set()
        if command_thread is not None:
            command_thread.join(timeout=1.0)
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
