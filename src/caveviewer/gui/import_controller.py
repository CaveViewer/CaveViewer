"""Background map-import lifecycle for the viewer window.

The controller owns import worker state, progress messages, pause/resume
requests, and render-thread queue draining.  CaveViewerWindow still owns
OpenGL rendering and map loading; this module only decides when those actions
should happen.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import os
import queue as queue_module
import threading
from typing import Any

from caveviewer.core.diagnostics.logging import (
    finish_console_progress_line,
    set_console_progress,
)
from caveviewer.core.map import cache_paths


_IMPORT_EVENT_POLL_SECONDS = 0.25
_IMPORT_HEARTBEAT_LOG_SECONDS = 30.0
_IMPORT_STALE_LOG_SECONDS = 30.0


class MapImportController:
    """Manage asynchronous map imports for a viewer-window owner."""

    def __init__(
        self,
        owner: Any,
        *,
        logger: Callable[[], Any],
        chunker: Callable[[], Any],
        start_import_process: Callable[[], Callable[[dict, str], Any]],
        terminate_import_process: Callable[[], Callable[..., None]],
        acquire_inhibitor: Callable[[], Callable[[str], Any]],
        release_inhibitor: Callable[[], Callable[[Any], None]],
        perf_counter: Callable[[], float],
        monotonic: Callable[[], float],
    ) -> None:
        self._owner = owner
        self._logger = logger
        self._chunker = chunker
        self._start_import_process = start_import_process
        self._terminate_import_process = terminate_import_process
        self._acquire_inhibitor = acquire_inhibitor
        self._release_inhibitor = release_inhibitor
        self._perf_counter = perf_counter
        self._monotonic = monotonic

        self.active: bool = False
        self.is_startup: bool = False
        self.thread: threading.Thread | None = None
        self.process = None
        self.command_queue = None
        self.cache_dir: str | None = None
        self.stop_event: threading.Event | None = None
        self.event_queue: queue_module.Queue | None = None
        self.pause_requested: bool = False
        self.model_format: str | None = None
        self.map_name: str = ""
        self.progress_stage: str = ""
        self.progress_fraction: float = 0.0
        self.progress_title: str = ""
        self.progress_note: str = ""
        self.resuming_from_checkpoint: bool = False
        self.pause_notice_until: float | None = None
        self.pause_notice_close_after: bool = False
        self.pause_notice_map_name: str = ""
        self.pause_notice_title: str = "Import paused"
        self.pause_notice_stage: str = "resume point saved"
        self.pause_notice_note: str = ""

    @property
    def log(self):
        return self._logger()

    @staticmethod
    def import_model_format_from_descriptor(model_descriptor: dict) -> str | None:
        return (
            model_descriptor.get("format")
            or ("obj" if model_descriptor.get("obj_path") else None)
            or ("glb" if model_descriptor.get("glb_path") else None)
        )

    @staticmethod
    def default_progress_note() -> str:
        return "First-time setup in progress. Next time, this map will open faster."

    def set_progress_message(self, title: str, note: str) -> None:
        self.progress_title = title
        self.progress_note = note

    def update_progress_message_for_stage(self, stage: str) -> None:
        normalized = " ".join(str(stage or "").strip().lower().split())
        if normalized == "resuming import":
            self.resuming_from_checkpoint = True

        if self.pause_requested or normalized == "pausing import":
            self.set_progress_message(
                "Pausing import",
                "Saving a resume point.",
            )
        elif self.resuming_from_checkpoint:
            self.set_progress_message(
                "Resuming import",
                "Using saved work from the previous session.",
            )
        else:
            self.set_progress_message(
                "",
                self.default_progress_note(),
            )

    def show_pause_notice(
        self,
        map_name: str,
        *,
        close_after: bool = False,
        duration: float = 6.0,
    ) -> None:
        self.pause_notice_until = self._perf_counter() + duration
        self.pause_notice_close_after = close_after
        self.pause_notice_map_name = map_name
        self.pause_notice_title = "Import paused"
        self.pause_notice_stage = "resume point saved"
        if close_after:
            self.pause_notice_note = (
                "This window will close shortly; open this map again to continue."
            )
        else:
            self.pause_notice_note = "Open this map again to continue."

    def clear_pause_notice(self) -> bool:
        close_after = self.pause_notice_close_after
        self.pause_notice_until = None
        self.pause_notice_close_after = False
        self.pause_notice_map_name = ""
        self.pause_notice_title = "Import paused"
        self.pause_notice_stage = "resume point saved"
        self.pause_notice_note = ""
        return close_after

    def render_pause_notice_if_active(self, panel, window) -> bool:
        until = self.pause_notice_until
        if until is None:
            return False
        if self._perf_counter() >= until:
            close_after = self.clear_pause_notice()
            if close_after and hasattr(window, "close"):
                window.close()
            return close_after

        panel.render(
            window.size,
            self.pause_notice_map_name or self.map_name or "map",
            self.pause_notice_stage,
            1.0,
            title=self.pause_notice_title,
            note=self.pause_notice_note,
        )
        return True

    def render_pending_import_splash(
        self,
        pending_import: dict | None,
        panel,
        window_size: tuple[int, int],
    ) -> None:
        pending = pending_import or {}
        model_descriptor = pending.get("model_descriptor") or {}
        source_path = (
            model_descriptor.get("obj_path")
            or model_descriptor.get("glb_path")
            or ""
        )
        map_name = os.path.basename(source_path) if source_path else "map"
        panel.render(
            window_size,
            map_name,
            "starting import",
            0.0,
            title="",
            note=self.default_progress_note(),
        )

    def start_async(
        self,
        model_descriptor: dict,
        textures_dir: str,
        map_name: str,
        is_startup: bool = False,
    ) -> None:
        """Start an OBJ/GLB import without blocking the viewer event loop."""
        if self.active:
            self.log.warning("Import already in progress; ignoring duplicate start request.")
            return
        self.active = True
        self.is_startup = is_startup
        self.map_name = map_name
        self.progress_stage = "starting import"
        self.progress_fraction = 0.0
        self.event_queue = queue_module.Queue()
        self.process = None
        self.command_queue = None
        self.cache_dir = None
        self.stop_event = threading.Event()
        self.pause_requested = False
        self.model_format = self.import_model_format_from_descriptor(model_descriptor)
        self.resuming_from_checkpoint = False
        self.set_progress_message("", self.default_progress_note())
        self.clear_pause_notice()

        event_queue = self.event_queue
        source_path = model_descriptor.get("obj_path") or model_descriptor.get("glb_path")
        stop_event = self.stop_event

        def worker() -> None:
            def on_progress(stage: str, fraction: float) -> None:
                set_console_progress(stage, fraction)
                event_queue.put(("progress", stage, fraction))

            try:
                chunker = self._chunker()
                if chunker.cache_is_valid(source_path):
                    on_progress("loading cached map", 1.0)
                    cache_dir = chunker.get_cache_dir(source_path)
                    resolved_textures_dir = cache_paths.map_texture_dir(
                        source_path, cache_dir, textures_dir
                    )
                else:
                    on_progress("starting import", 0.0)
                    inhibitor = self._acquire_inhibitor()(map_name)
                    try:
                        if stop_event.is_set():
                            event_queue.put(("cancelled",))
                            return
                        handle = self._start_import_process()(
                            model_descriptor, textures_dir
                        )
                        self.process = handle.process
                        self.command_queue = getattr(handle, "commands", None)
                        self.cache_dir = getattr(handle, "cache_dir", None)
                        if self.pause_requested and self.command_queue is not None:
                            self.command_queue.put(("pause",))
                        self._relay_child_import_events(handle, stop_event, event_queue)
                    finally:
                        self.process = None
                        self.command_queue = None
                        self.cache_dir = None
                        self._release_inhibitor()(inhibitor)
                    return
                finish_console_progress_line()
                event_queue.put(("done", cache_dir, resolved_textures_dir))
            except Exception as exc:
                finish_console_progress_line()
                event_queue.put(("error", str(exc), ""))

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def _relay_child_import_events(
        self,
        handle,
        stop_event: threading.Event,
        event_queue: queue_module.Queue,
    ) -> None:
        last_event_at = self._monotonic()
        last_stale_log_at = 0.0
        last_heartbeat_log_at = 0.0
        last_stage = "starting import"
        last_fraction = 0.0
        while not stop_event.is_set():
            try:
                event = handle.events.get(timeout=_IMPORT_EVENT_POLL_SECONDS)
            except queue_module.Empty:
                now = self._monotonic()
                if handle.process.exitcode is None:
                    if (
                        now - last_event_at >= _IMPORT_STALE_LOG_SECONDS
                        and now - last_stale_log_at >= _IMPORT_STALE_LOG_SECONDS
                    ):
                        self.log.info(
                            "Import process is still running but has not reported "
                            "progress or a heartbeat for %.0fs; last stage %r "
                            "at %.0f%%.",
                            now - last_event_at,
                            last_stage,
                            last_fraction * 100.0,
                        )
                        last_stale_log_at = now
                    continue
                exitcode = handle.process.exitcode
                self._terminate_import_process()(
                    handle.process,
                    cache_dir=getattr(handle, "cache_dir", None),
                )
                event_queue.put(
                    (
                        "error",
                        "Import process exited without reporting "
                        f"a result (exit code {exitcode}).",
                        "",
                    )
                )
                break

            last_event_at = self._monotonic()
            kind = event[0]
            if kind == "progress":
                last_stage = str(event[1])
                last_fraction = float(event[2])
                set_console_progress(last_stage, last_fraction)
                event_queue.put(event)
            elif kind == "heartbeat":
                if len(event) >= 3:
                    last_stage = str(event[1])
                    last_fraction = float(event[2])
                    set_console_progress(last_stage, last_fraction)
                if last_event_at - last_heartbeat_log_at >= _IMPORT_HEARTBEAT_LOG_SECONDS:
                    self._log_heartbeat(event, last_stage, last_fraction)
                    last_heartbeat_log_at = last_event_at
                event_queue.put(event)
            elif kind == "log":
                level = int(event[1])
                component = str(event[2])
                message = str(event[3])
                logging.getLogger("caveviewer").log(
                    level,
                    message,
                    extra={"component": component},
                )
            elif kind == "done":
                finish_console_progress_line()
                event_queue.put(event)
                break
            elif kind == "error":
                finish_console_progress_line()
                event_queue.put(event)
                break
            elif kind == "cancelled":
                finish_console_progress_line()
                event_queue.put(("cancelled",))
                break
            elif kind == "paused":
                finish_console_progress_line()
                event_queue.put(event)
                break

        if stop_event.is_set():
            self._terminate_import_process()(
                handle.process,
                cache_dir=getattr(handle, "cache_dir", None),
            )
            event_queue.put(("cancelled",))
        else:
            handle.process.join(timeout=1.0)

    def _log_heartbeat(
        self,
        event: tuple,
        last_stage: str,
        last_fraction: float,
    ) -> None:
        elapsed = float(event[3]) if len(event) > 3 else 0.0
        available_bytes = event[4] if len(event) > 4 else None
        total_bytes = event[5] if len(event) > 5 else None
        if available_bytes is not None and total_bytes:
            self.log.info(
                "Import heartbeat: %.0fs elapsed; stage %r at %.0f%%; "
                "system RAM %.1f GB available of %.1f GB.",
                elapsed,
                last_stage,
                last_fraction * 100.0,
                float(available_bytes) / (1024 ** 3),
                float(total_bytes) / (1024 ** 3),
            )
        else:
            self.log.info(
                "Import heartbeat: %.0fs elapsed; stage %r at %.0f%%.",
                elapsed,
                last_stage,
                last_fraction * 100.0,
            )

    def drain_queue(self) -> None:
        """Drain import worker messages on the render thread."""
        if self.event_queue is None:
            return
        while True:
            try:
                msg = self.event_queue.get_nowait()
            except queue_module.Empty:
                break
            kind = msg[0]
            if kind == "progress":
                self.progress_stage = msg[1]
                self.progress_fraction = msg[2]
                self.update_progress_message_for_stage(msg[1])
            elif kind == "heartbeat":
                self.progress_stage = msg[1]
                self.progress_fraction = msg[2]
                self.update_progress_message_for_stage(msg[1])
            elif kind == "done":
                self._handle_done_message(msg)
                break
            elif kind == "error":
                self._handle_error_message(msg)
                break
            elif kind == "cancelled":
                finish_console_progress_line()
                self._clear_active_references()
                break
            elif kind == "paused":
                self._handle_paused_message(msg)
                break

    def _handle_done_message(self, msg: tuple) -> None:
        finish_console_progress_line()
        _, cache_dir, textures_dir = msg
        self._clear_active_references()
        try:
            manifest = self._chunker().load_manifest(cache_dir)
            if manifest is None:
                raise ValueError(
                    f"Map cache manifest could not be loaded from {cache_dir}."
                )
        except Exception as exc:
            self.log.error("Failed to load imported map manifest: %s", exc)
            if self.is_startup:
                self.log.error("Closing -- no map to show without a valid cache manifest.")
                self._close_window_if_possible()
            return
        self._owner.load_new_map(cache_dir, textures_dir, manifest)

    def _handle_error_message(self, msg: tuple) -> None:
        finish_console_progress_line()
        error_msg = msg[1]
        error_trace = msg[2] if len(msg) > 2 else ""
        error_suggestion = msg[3] if len(msg) > 3 else ""
        self._clear_active_references()
        self.log.error(f"Import failed: {error_msg}")
        if error_suggestion:
            self.log.error("Suggestion: %s", error_suggestion)
        if error_trace:
            self.log.error("Import process traceback:\n%s", error_trace)
        if self.is_startup:
            self.log.error("Closing -- no map to show without a successful import.")
            self._close_window_if_possible()

    def _handle_paused_message(self, msg: tuple) -> None:
        finish_console_progress_line()
        resume_dir = msg[1] if len(msg) > 1 else ""
        was_startup_import = self.is_startup
        map_name = self.map_name
        self._clear_active_references()
        if resume_dir:
            self.log.info("Import paused. Resume checkpoint: %s", resume_dir)
        self.log.info("Open this map again to resume the import.")
        if self._owner._has_map_loaded:
            self._owner._show_recording_status(
                "Import paused",
                "Resume point saved. Open this map again to continue.",
                kind="success",
                duration=5.0,
            )
        else:
            self.show_pause_notice(map_name, close_after=was_startup_import)
            if was_startup_import:
                self.log.info(
                    "Viewer will close after showing the paused import message."
                )

    def _clear_active_references(self) -> None:
        self.active = False
        self.event_queue = None
        self.thread = None
        self.process = None
        self.command_queue = None
        self.cache_dir = None
        self.stop_event = None
        self.pause_requested = False
        self.model_format = None
        self.resuming_from_checkpoint = False

    def _close_window_if_possible(self) -> None:
        window = getattr(self._owner, "wnd", None)
        if hasattr(window, "close"):
            window.close()

    def request_pause(self) -> None:
        """Ask the import child to checkpoint at the next safe pause point."""
        if not self.active:
            return
        if self.model_format != "obj":
            self.log.warning(
                "Import pause/resume is currently supported only for .obj maps."
            )
            return
        if self.pause_requested:
            return

        self.pause_requested = True
        self.progress_stage = "pausing import"
        self.set_progress_message(
            "Pausing import",
            "Saving a resume point.",
        )
        if self.command_queue is not None:
            self.command_queue.put(("pause",))
        self.log.info(
            "Import pause requested; waiting for the current safe checkpoint."
        )

    def cancel_active_import(self) -> None:
        """Signal a running import to stop without blocking the GUI thread."""
        if self.stop_event is not None:
            self.stop_event.set()

        thread_alive = self.thread is not None and self.thread.is_alive()
        if self.process is not None and not thread_alive:
            self._terminate_import_process()(
                self.process,
                timeout=0.0,
                cache_dir=self.cache_dir,
            )
        elif thread_alive:
            self.log.info(
                "Import cancellation requested; relay worker will terminate "
                "the child process."
            )
