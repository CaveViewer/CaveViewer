"""Reusable resize-aware rounded surfaces for Tk compositions."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, replace


_ROUNDED_SURFACE_TAG = "cv-rounded-surface"
_CURVE_STEPS = 24


def rounded_rectangle_points(
    width: int,
    height: int,
    radius: int,
    *,
    inset: int = 0,
) -> tuple[int, ...]:
    """Return a clamped clockwise path for one smoothed rounded rectangle."""
    x0 = max(0, int(inset))
    y0 = x0
    x1 = max(x0, int(width) - x0)
    y1 = max(y0, int(height) - y0)
    resolved_radius = max(
        0,
        min(int(radius), (x1 - x0) // 2, (y1 - y0) // 2),
    )
    return (
        x0 + resolved_radius,
        y0,
        x0 + resolved_radius,
        y0,
        x1 - resolved_radius,
        y0,
        x1 - resolved_radius,
        y0,
        x1,
        y0,
        x1,
        y0 + resolved_radius,
        x1,
        y0 + resolved_radius,
        x1,
        y1 - resolved_radius,
        x1,
        y1 - resolved_radius,
        x1,
        y1,
        x1 - resolved_radius,
        y1,
        x1 - resolved_radius,
        y1,
        x0 + resolved_radius,
        y1,
        x0 + resolved_radius,
        y1,
        x0,
        y1,
        x0,
        y1 - resolved_radius,
        x0,
        y1 - resolved_radius,
        x0,
        y0 + resolved_radius,
        x0,
        y0 + resolved_radius,
        x0,
        y0,
    )


@dataclass(frozen=True, slots=True)
class RoundedSectionStyle:
    """Resolved geometry and colors for one rounded content surface."""

    outside_background: str
    fill: str
    border: str
    border_width: int
    radius: int
    padding_x: int
    padding_y: int
    padding_bottom_y: int | None = None
    minimum_height: int = 0


class RoundedSurfaceRenderer:
    """Redraw one rounded Canvas surface without retaining scheduled work."""

    def __init__(self, canvas) -> None:
        self._canvas = canvas
        self._closed = False

    def redraw(
        self,
        *,
        width: int,
        height: int,
        radius: int,
        fill: str,
        border: str,
        border_width: int,
    ) -> None:
        """Replace the prior vector surface with the current bounded geometry."""
        if self._closed:
            return
        width = max(0, int(width))
        height = max(0, int(height))
        self._canvas.delete(_ROUNDED_SURFACE_TAG)
        if width <= 0 or height <= 0:
            return

        border_width = max(0, int(border_width))
        outer_fill = border if border_width else fill
        self._canvas.create_polygon(
            rounded_rectangle_points(width, height, radius),
            fill=outer_fill,
            outline="",
            smooth=True,
            splinesteps=_CURVE_STEPS,
            tags=(_ROUNDED_SURFACE_TAG,),
        )
        if border_width:
            self._canvas.create_polygon(
                rounded_rectangle_points(
                    width,
                    height,
                    max(0, radius - border_width),
                    inset=border_width,
                ),
                fill=fill,
                outline="",
                smooth=True,
                splinesteps=_CURVE_STEPS,
                tags=(_ROUNDED_SURFACE_TAG,),
            )
        self._canvas.tag_lower(_ROUNDED_SURFACE_TAG)

    def close(self) -> None:
        """Prevent later event delivery from drawing into a destroyed surface."""
        if self._closed:
            return
        self._closed = True
        try:
            self._canvas.delete(_ROUNDED_SURFACE_TAG)
        except tk.TclError:
            pass


class RoundedSectionSurface:
    """A resize-aware rounded card that hosts ordinary Tk content widgets."""

    def __init__(self, parent, *, style: RoundedSectionStyle) -> None:
        self._style = style
        self._closed = False
        self.widget = tk.Canvas(
            parent,
            bg=style.outside_background,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        self._renderer = RoundedSurfaceRenderer(self.widget)
        self.content = tk.Frame(self.widget, bg=style.fill)
        self._content_window = self.widget.create_window(
            (style.padding_x, style.padding_y),
            window=self.content,
            anchor="nw",
        )
        self.widget.bind("<Configure>", self._on_widget_configure, add="+")
        self.widget.bind("<Destroy>", self._on_destroy, add="+")
        self.content.bind("<Configure>", self._on_content_configure, add="+")

    def pack(self, **options) -> None:
        self.widget.pack(**options)

    def pack_forget(self) -> None:
        self.widget.pack_forget()

    def grid(self, **options) -> None:
        self.widget.grid(**options)

    def set_style(self, style: RoundedSectionStyle) -> None:
        """Apply recomposed geometry and colors to the existing surface."""
        self._style = style
        self.widget.configure(bg=style.outside_background)
        self.content.configure(bg=style.fill)
        self.widget.coords(
            self._content_window,
            style.padding_x,
            style.padding_y,
        )
        self._sync_content_width(self.widget.winfo_width())
        self._sync_height(self.content.winfo_reqheight())
        self._redraw()

    def set_content_padding(
        self,
        *,
        x: int | None = None,
        y: int | None = None,
    ) -> None:
        """Update padding without rebuilding the hosted Tk content."""
        self.set_style(
            replace(
                self._style,
                padding_x=self._style.padding_x if x is None else max(0, int(x)),
                padding_y=self._style.padding_y if y is None else max(0, int(y)),
            )
        )

    def sync_geometry(self) -> None:
        """Synchronize requested height and surface bounds after content changes."""
        if self._closed:
            return
        self.widget.update_idletasks()
        self._sync_content_width(self.widget.winfo_width())
        self._sync_height(self.content.winfo_reqheight())

    def destroy(self) -> None:
        self.widget.destroy()

    def _on_widget_configure(self, event) -> None:
        if self._closed:
            return
        self._sync_content_width(event.width)
        self._redraw(width=event.width, height=event.height)

    def _on_content_configure(self, event) -> None:
        if self._closed:
            return
        self._sync_height(event.height)

    def _sync_content_width(self, width: int) -> None:
        content_width = max(1, int(width) - (self._style.padding_x * 2))
        self.widget.itemconfigure(self._content_window, width=content_width)

    def _sync_height(self, content_height: int) -> None:
        padding_bottom_y = self._style.padding_bottom_y
        if padding_bottom_y is None:
            padding_bottom_y = self._style.padding_y
        self._sync_height_for_padding(
            content_height,
            self._style.padding_y,
            padding_bottom_y,
        )

    def _sync_height_for_padding(
        self,
        content_height: int,
        padding_top_y: int,
        padding_bottom_y: int | None = None,
    ) -> None:
        if padding_bottom_y is None:
            padding_bottom_y = padding_top_y
        minimum_height = max(
            0,
            int(getattr(getattr(self, "_style", None), "minimum_height", 0)),
        )
        target = max(
            1,
            minimum_height,
            int(content_height)
            + max(0, int(padding_top_y))
            + max(0, int(padding_bottom_y)),
        )
        # Tk may retain the default Canvas option as a unit string (for example,
        # ``7c`` on X11); requested geometry is always resolved to pixels.
        if self.widget.winfo_reqheight() != target:
            self.widget.configure(height=target)
        self._redraw(height=target)

    def set_minimum_height(self, height: int) -> None:
        """Update the expanded-state floor without rebuilding the surface."""
        self.set_style(
            replace(
                self._style,
                minimum_height=max(0, int(height)),
            )
        )

    def _redraw(
        self,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        if self._closed:
            return
        self._renderer.redraw(
            width=self.widget.winfo_width() if width is None else width,
            height=self.widget.winfo_height() if height is None else height,
            radius=self._style.radius,
            fill=self._style.fill,
            border=self._style.border,
            border_width=self._style.border_width,
        )

    def _on_destroy(self, event) -> None:
        if event.widget is not self.widget or self._closed:
            return
        self._closed = True
        self._renderer.close()
