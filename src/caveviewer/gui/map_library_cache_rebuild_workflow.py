"""Tk-thread orchestration around the cache-rebuild process controller."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from caveviewer.gui.cache_rebuild_controller import CacheRebuildJobController


class MapLibraryCacheRebuildWorkflow:
    """Own rebuild polling tokens, pause requests, and update delivery."""

    def __init__(
        self,
        *,
        controller: CacheRebuildJobController,
        scheduler: Any,
        splash_exists: Callable[[], bool],
        apply_updates: Callable[[tuple[Any, ...]], None],
    ) -> None:
        self.controller = controller
        self._scheduler = scheduler
        self._splash_exists = splash_exists
        self._apply_updates = apply_updates
        self._after_id: Any | None = None

    @property
    def poll_scheduled(self) -> bool:
        return self._after_id is not None

    def request_pause(self, *, for_close: bool = False) -> bool:
        request = (
            self.controller.request_pause_for_close
            if for_close
            else self.controller.request_pause
        )
        return request()

    def schedule_poll(self) -> None:
        if self._after_id is not None:
            return
        if not self.controller.active or not self._splash_exists():
            return
        self._after_id = self._scheduler.after(100, self.poll)

    def poll(self) -> None:
        self._after_id = None
        if not self._splash_exists():
            self.request_pause(for_close=True)
            return
        updates = self.controller.poll()
        self._apply_updates(updates)
        if self.controller.active:
            self.schedule_poll()

    def cancel_poll(self) -> None:
        if self._after_id is None:
            return
        try:
            self._scheduler.after_cancel(self._after_id)
        except Exception:
            pass
        self._after_id = None
