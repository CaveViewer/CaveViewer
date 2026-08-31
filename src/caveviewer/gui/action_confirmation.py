"""Shared transient confirmation presentation for completed GUI actions."""

from __future__ import annotations

import tkinter as tk
from typing import Callable

from caveviewer.gui.tk_theme import DARK_THEME


ACTION_CONFIRMATION_GAP = 12
ACTION_CONFIRMATION_MS = 3_000
ACTION_CONFIRMATION_SIZE = 18


def create_confirmation_mark(
    parent,
    *,
    px,
    background: str | None = None,
    accessible_name: str = "Action completed",
):
    """Draw a compact, font-independent checkmark for transient feedback."""

    size = px(ACTION_CONFIRMATION_SIZE)
    resolved_background = background or DARK_THEME.background
    mark = tk.Canvas(
        parent,
        width=size,
        height=size,
        bg=resolved_background,
        highlightthickness=0,
        takefocus=False,
    )
    mark.create_line(
        [(px(3), px(9)), (px(7), px(13)), (px(15), px(4))],
        fill=DARK_THEME.title,
        width=px(2),
        capstyle="round",
        joinstyle="round",
    )
    setattr(mark, "_cv_accessible_name", accessible_name)
    return mark


class TransientActionConfirmation:
    """Own one restartable Tk timer and its confirmation visibility state."""

    def __init__(
        self,
        scheduler,
        *,
        on_visibility_changed: Callable[[bool], None],
        duration_ms: int = ACTION_CONFIRMATION_MS,
    ) -> None:
        self._scheduler = scheduler
        self._on_visibility_changed = on_visibility_changed
        self._duration_ms = duration_ms
        self._after_id: str | None = None
        self._visible = False

    @property
    def visible(self) -> bool:
        """Return whether the confirmation should currently be presented."""

        return self._visible

    def show(self) -> None:
        """Show the confirmation and restart its full visibility interval."""

        self._cancel_timer()
        self._set_visible(True)
        try:
            self._after_id = self._scheduler.after(
                self._duration_ms,
                self._expire,
            )
        except (AttributeError, tk.TclError):
            self._after_id = None
            self._set_visible(False)

    def clear(self) -> None:
        """Cancel pending work and hide the confirmation immediately."""

        self._cancel_timer()
        self._set_visible(False)

    def _expire(self) -> None:
        self._after_id = None
        self._set_visible(False)

    def _cancel_timer(self) -> None:
        after_id = self._after_id
        self._after_id = None
        if after_id is None:
            return
        try:
            self._scheduler.after_cancel(after_id)
        except (AttributeError, tk.TclError):
            pass

    def _set_visible(self, visible: bool) -> None:
        if self._visible == visible:
            return
        self._visible = visible
        try:
            self._on_visibility_changed(visible)
        except (AttributeError, tk.TclError):
            pass
