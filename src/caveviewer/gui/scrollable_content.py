"""Shared Tk canvas scrolling controls with one CaveViewer scrollbar treatment."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Callable

from caveviewer.gui.tk_scrolling import vertical_scroll_units
from caveviewer.gui.tk_theme import DARK_THEME


@dataclass(frozen=True)
class CanvasScrollbarStyle:
    """Scale-independent visual tokens for an app-owned vertical scrollbar."""

    background_color: str
    thumb_color: str = DARK_THEME.secondary_button_border
    active_thumb_color: str = DARK_THEME.entry_focus_border
    rail_width: int = 14
    thumb_width: int = 5
    minimum_thumb_height: int = 36


@dataclass(frozen=True)
class ScrollbarThumbGeometry:
    """Pixel geometry for a visible scrollbar thumb inside its rail."""

    top: int
    bottom: int
    travel: int


def scrollbar_thumb_geometry(
    *,
    rail_height: int | float,
    first: float,
    last: float,
    minimum_thumb_height: int | float,
) -> ScrollbarThumbGeometry | None:
    """Return a clamped thumb geometry, or ``None`` when all content fits."""
    height = max(1, int(round(rail_height)))
    visible_fraction = max(0.0, min(1.0, float(last) - float(first)))
    if visible_fraction >= 1.0:
        return None

    thumb_height = min(
        height,
        max(
            1,
            int(round(minimum_thumb_height)),
            int(round(height * visible_fraction)),
        ),
    )
    travel = max(0, height - thumb_height)
    clamped_first = max(0.0, min(1.0, float(first)))
    top = 0 if travel == 0 else int(round(clamped_first * travel))
    return ScrollbarThumbGeometry(
        top=top,
        bottom=min(height, top + thumb_height),
        travel=travel,
    )


class CanvasVerticalScrollbar:
    """Own a consistent, draggable scrollbar for one Tk Canvas viewport.

    The caller keeps ownership of its content frame and scroll region. This
    component owns the themed rail, thumb geometry, visibility, pointer input,
    and normalized wheel handling so every splash surface behaves alike.
    """

    def __init__(
        self,
        parent,
        *,
        canvas,
        px: Callable[[int | float], int],
        style: CanvasScrollbarStyle,
    ) -> None:
        self._parent = parent
        self._canvas = canvas
        self._px = px
        self._style = style
        self._rail_width = max(1, self._px(style.rail_width))
        self._thumb_width = max(1, self._px(style.thumb_width))
        self._minimum_thumb_height = max(1, self._px(style.minimum_thumb_height))
        self._widget = tk.Canvas(
            parent,
            bg=style.background_color,
            borderwidth=0,
            highlightthickness=0,
            width=self._rail_width,
            cursor="arrow",
        )
        self._thumb = None
        self._fractions = (0.0, 1.0)
        self._visible = False
        self._drag_offset = 0.0
        self._mounted = False

        self._canvas.configure(yscrollcommand=self.set)
        self._widget.bind("<Configure>", self._on_resize, add="+")
        self._widget.bind("<ButtonPress-1>", self._start_drag, add="+")
        self._widget.bind("<B1-Motion>", self._drag, add="+")
        self._widget.bind("<ButtonRelease-1>", self._end_drag, add="+")
        self._bind_scroll_events(self._canvas)
        self._bind_scroll_events(self._widget)

    @property
    def is_visible(self) -> bool:
        """Whether the content currently needs a vertical scrolling affordance."""
        return self._visible

    def mount_grid(self, **grid_options) -> None:
        """Mount the rail in a reserved grid column without layout reflow."""
        column = int(grid_options.get("column", 0))
        self._parent.grid_columnconfigure(column, minsize=self._rail_width)
        self._widget.grid(**grid_options)
        self._widget.grid_remove()
        self._mounted = True

    def sync_overflow(self, content_height: int | float) -> bool:
        """Show or hide the rail after the owner updates its canvas region."""
        overflow = float(content_height) > self._canvas.winfo_height() + 1
        self.set_visible(overflow)
        if overflow:
            try:
                self.set(*self._canvas.yview())
            except (AttributeError, tk.TclError):
                pass
        else:
            self.reset_to_top()
        return overflow

    def set_visible(self, visible: bool) -> None:
        """Display the rail only when it represents a scrollable range."""
        visible = bool(visible)
        if visible == self._visible:
            self._draw_thumb()
            return

        self._visible = visible
        if self._mounted:
            if visible:
                self._widget.grid()
            else:
                self._widget.grid_remove()
        self._widget.configure(cursor="sb_v_double_arrow" if visible else "arrow")
        self._draw_thumb()

    def set(self, first: str | float, last: str | float) -> None:
        """Receive Tk's canvas yview fractions and redraw the thumb."""
        try:
            first_fraction = float(first)
            last_fraction = float(last)
        except (TypeError, ValueError):
            return
        self._fractions = (first_fraction, last_fraction)
        self._draw_thumb()

    def reset_to_top(self) -> None:
        """Restore the viewport to its first scrollable row."""
        try:
            self._canvas.yview_moveto(0)
        except tk.TclError:
            return

    def scroll_from_event(self, event):
        """Scroll the owned canvas for one normalized wheel event."""
        if not self._visible:
            return None
        units = vertical_scroll_units(event)
        if units is None:
            return None
        try:
            self._canvas.yview_scroll(units, "units")
        except tk.TclError:
            return None
        return "break"

    def bind_mousewheel(self, widget) -> None:
        """Bind normalized wheel scrolling to a content subtree."""
        self._bind_scroll_events(widget)
        for child in widget.winfo_children():
            self.bind_mousewheel(child)

    def _geometry(self) -> ScrollbarThumbGeometry | None:
        return scrollbar_thumb_geometry(
            rail_height=self._widget.winfo_height(),
            first=self._fractions[0],
            last=self._fractions[1],
            minimum_thumb_height=self._minimum_thumb_height,
        )

    def _draw_thumb(self) -> None:
        if not self._visible:
            self._delete_thumb()
            return
        geometry = self._geometry()
        if geometry is None:
            self._delete_thumb()
            return

        x = self._rail_width // 2
        if self._thumb is None:
            self._thumb = self._widget.create_line(
                x,
                geometry.top,
                x,
                geometry.bottom,
                fill=self._style.thumb_color,
                width=self._thumb_width,
                capstyle="round",
            )
        else:
            self._widget.coords(self._thumb, x, geometry.top, x, geometry.bottom)

    def _delete_thumb(self) -> None:
        if self._thumb is not None:
            self._widget.delete(self._thumb)
            self._thumb = None

    def _start_drag(self, event):
        geometry = self._geometry()
        if geometry is None:
            return "break"
        if geometry.top <= event.y <= geometry.bottom:
            self._drag_offset = event.y - geometry.top
        else:
            self._drag_offset = (geometry.bottom - geometry.top) / 2
            self._drag(event)
        if self._thumb is not None:
            self._widget.itemconfigure(
                self._thumb,
                fill=self._style.active_thumb_color,
            )
        return "break"

    def _drag(self, event):
        geometry = self._geometry()
        if geometry is None or geometry.travel == 0:
            return "break"
        thumb_top = max(0, min(geometry.travel, event.y - self._drag_offset))
        try:
            self._canvas.yview_moveto(thumb_top / geometry.travel)
        except tk.TclError:
            return "break"
        return "break"

    def _end_drag(self, _event):
        if self._thumb is not None:
            self._widget.itemconfigure(self._thumb, fill=self._style.thumb_color)
        return "break"

    def _on_resize(self, _event) -> None:
        self._draw_thumb()

    def _bind_scroll_events(self, widget) -> None:
        widget.bind("<MouseWheel>", self.scroll_from_event, add="+")
        widget.bind("<Button-4>", self.scroll_from_event, add="+")
        widget.bind("<Button-5>", self.scroll_from_event, add="+")
