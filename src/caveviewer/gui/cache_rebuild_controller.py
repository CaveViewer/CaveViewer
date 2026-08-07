"""Splash-owned lifecycle controller for one Map Library cache rebuild.

This deliberately does not reuse the viewer's ``MapImportController``.  A
Map Library rebuild must never create a viewer, load a map, or auto-open after
publication; it only owns a forced child import and reports its state to the
splash workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import queue
from typing import Any, Callable

from caveviewer.gui.import_process import start_import_process
from caveviewer.gui.map_cache_rebuild import CacheRebuildTarget


class CacheRebuildJobState(str, Enum):
    """Lifecycle state for the one splash-owned rebuild job."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CacheRebuildStarted:
    """The child process was started for one forced cache replacement."""

    target: CacheRebuildTarget


@dataclass(frozen=True, slots=True)
class CacheRebuildProgress:
    """Latest progress reported by the import child."""

    target: CacheRebuildTarget
    stage: str
    fraction: float
    pausing: bool = False


@dataclass(frozen=True, slots=True)
class CacheRebuildSucceeded:
    """The staged replacement was published successfully."""

    target: CacheRebuildTarget
    cache_dir: str


@dataclass(frozen=True, slots=True)
class CacheRebuildPaused:
    """The OBJ importer saved a resumable staging checkpoint."""

    target: CacheRebuildTarget
    resume_dir: str


@dataclass(frozen=True, slots=True)
class CacheRebuildFailed:
    """A rebuild could not complete; the prior published cache remains intact."""

    target: CacheRebuildTarget
    error: str
    suggestion: str = ""


CacheRebuildUpdate = (
    CacheRebuildProgress
    | CacheRebuildSucceeded
    | CacheRebuildPaused
    | CacheRebuildFailed
)


class CacheRebuildJobController:
    """Own one forced import process and turn child events into typed updates."""

    def __init__(
        self,
        *,
        start_process: Callable[..., Any] = start_import_process,
    ) -> None:
        self._start_process = start_process
        self._handle: Any | None = None
        self.target: CacheRebuildTarget | None = None
        self.state = CacheRebuildJobState.IDLE
        self.stage = ""
        self.fraction = 0.0
        self.pause_requested = False
        self._exit_without_event_polls = 0

    @property
    def active(self) -> bool:
        """Return whether the controller still owns a live rebuild process."""
        return self.state in {
            CacheRebuildJobState.RUNNING,
            CacheRebuildJobState.PAUSING,
        }

    @property
    def pause_supported(self) -> bool:
        """Return whether the active importer can preserve a resume checkpoint."""
        target = self.target
        if target is None:
            return False
        descriptor = target.model_descriptor
        return descriptor.get("format") == "obj" or bool(descriptor.get("obj_path"))

    def start(
        self,
        target: CacheRebuildTarget,
        *,
        resume_required: bool = False,
    ) -> CacheRebuildStarted | CacheRebuildFailed:
        """Start a forced rebuild or require reuse of a validated checkpoint."""
        if self.active:
            return CacheRebuildFailed(
                target=target,
                error="Another cache rebuild is already running.",
            )

        self.target = target
        self.state = CacheRebuildJobState.RUNNING
        self.stage = "starting import"
        self.fraction = 0.0
        self.pause_requested = False
        self._exit_without_event_polls = 0
        try:
            start_options = {
                "force_rebuild": True,
                "daemon": False,
            }
            if resume_required:
                start_options["resume_required"] = True
            self._handle = self._start_process(
                dict(target.model_descriptor),
                str(target.textures_dir),
                **start_options,
            )
        except Exception as exc:
            self._handle = None
            self.state = CacheRebuildJobState.FAILED
            return CacheRebuildFailed(target=target, error=str(exc))
        return CacheRebuildStarted(target=target)

    def request_pause(self) -> bool:
        """Ask an active OBJ import to checkpoint without terminating it."""
        if not self.active or self.pause_requested or not self.pause_supported:
            return False
        commands = getattr(self._handle, "commands", None)
        if commands is None:
            return False
        try:
            commands.put(("pause",))
        except Exception:
            return False
        self.pause_requested = True
        self.state = CacheRebuildJobState.PAUSING
        self.stage = "pausing import"
        return True

    def request_pause_for_close(self) -> bool:
        """Request a resumable checkpoint before the splash is allowed to close."""
        return self.request_pause()

    def poll(self) -> tuple[CacheRebuildUpdate, ...]:
        """Drain child messages without blocking the Tk thread."""
        handle = self._handle
        target = self.target
        if handle is None or target is None or not self.active:
            return ()

        updates: list[CacheRebuildUpdate] = []
        while True:
            try:
                event = handle.events.get_nowait()
            except queue.Empty:
                break
            except Exception as exc:
                updates.append(
                    self._fail(target, f"Couldn't read rebuild progress: {exc}")
                )
                return tuple(updates)

            update = self._handle_event(target, event)
            if update is not None:
                updates.append(update)
            if not self.active:
                return tuple(updates)

        process = getattr(handle, "process", None)
        exitcode = getattr(process, "exitcode", None)
        if exitcode is not None and self.active:
            # A multiprocessing queue can lag a child process exit by one or
            # two Tk polls. Give its feeder a brief non-blocking grace period
            # before treating an absent terminal event as an abnormal exit.
            self._exit_without_event_polls += 1
            if self._exit_without_event_polls >= 3:
                updates.append(
                    self._fail(
                        target,
                        "Cache rebuild process exited without reporting a result "
                        f"(exit code {exitcode}).",
                    )
                )
        else:
            self._exit_without_event_polls = 0
        return tuple(updates)

    def _handle_event(
        self,
        target: CacheRebuildTarget,
        event: Any,
    ) -> CacheRebuildUpdate | None:
        if not isinstance(event, tuple) or not event:
            return self._fail(
                target,
                "Cache rebuild process sent an invalid status update.",
            )
        kind = event[0]
        if kind in {"progress", "heartbeat"}:
            stage = str(event[1]) if len(event) > 1 else "rebuilding cache"
            try:
                fraction = float(event[2]) if len(event) > 2 else 0.0
            except (TypeError, ValueError):
                fraction = 0.0
            self.stage = stage
            self.fraction = min(1.0, max(0.0, fraction))
            return CacheRebuildProgress(
                target=target,
                stage=self.stage,
                fraction=self.fraction,
                pausing=self.state is CacheRebuildJobState.PAUSING,
            )
        if kind == "done":
            cache_dir = str(event[1]) if len(event) > 1 else str(target.cache_dir)
            self._finish(CacheRebuildJobState.SUCCEEDED)
            return CacheRebuildSucceeded(target=target, cache_dir=cache_dir)
        if kind == "paused":
            resume_dir = str(event[1]) if len(event) > 1 else ""
            self._finish(CacheRebuildJobState.PAUSED)
            return CacheRebuildPaused(target=target, resume_dir=resume_dir)
        if kind == "error":
            error = str(event[1]) if len(event) > 1 else "Cache rebuild failed."
            suggestion = str(event[3]) if len(event) > 3 else ""
            return self._fail(target, error, suggestion)
        if kind == "cancelled":
            return self._fail(target, "Cache rebuild was interrupted.")
        if kind == "log":
            return None
        return self._fail(target, f"Cache rebuild process sent unknown event {kind!r}.")

    def _fail(
        self,
        target: CacheRebuildTarget,
        error: str,
        suggestion: str = "",
    ) -> CacheRebuildFailed:
        self._finish(CacheRebuildJobState.FAILED)
        return CacheRebuildFailed(target=target, error=error, suggestion=suggestion)

    def _finish(self, state: CacheRebuildJobState) -> None:
        handle = self._handle
        self._handle = None
        self.state = state
        self.pause_requested = False
        process = getattr(handle, "process", None)
        join = getattr(process, "join", None)
        if callable(join):
            try:
                join(timeout=0.0)
            except Exception:
                pass
