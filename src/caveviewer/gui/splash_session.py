"""Tk-thread splash session state and after-callback ownership."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class SplashSession:
    """Own transient splash selection, closing state, and scheduled callbacks."""

    def __init__(self) -> None:
        self.selected_folder: str | None = None
        self.closing = False
        self._after_ids: set[str] = set()

    def select_folder(self, path: str) -> None:
        """Record the folder selected by the splash workflow."""
        self.selected_folder = path

    def mark_closing(self) -> None:
        """Prevent future scheduled callbacks from mutating Tk widgets."""
        self.closing = True

    def schedule_after(
        self,
        root: Any,
        delay_ms: int,
        callback: Callable[[], None],
    ) -> str:
        """Schedule a Tk callback and keep ownership of its cancellation token."""
        after_id_holder: dict[str, str] = {}

        def wrapped_callback() -> None:
            after_id = after_id_holder.get("after_id")
            if after_id is not None:
                self._after_ids.discard(after_id)
            if self.closing:
                return
            callback()

        after_id = root.after(delay_ms, wrapped_callback)
        after_id_holder["after_id"] = after_id
        self._after_ids.add(after_id)
        return after_id

    def cancel_after_callbacks(self, root: Any) -> None:
        """Cancel outstanding Tk callbacks owned by this splash session."""
        for after_id in tuple(self._after_ids):
            try:
                root.after_cancel(after_id)
            except Exception:
                pass
            finally:
                self._after_ids.discard(after_id)
