"""Present matched cave information and source links inside the library shell."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from typing import Callable

from caveviewer.gui.cave_metadata import CaveMetadata
from caveviewer.gui.text_link import configure_text_link
from caveviewer.gui.scrollable_content import CanvasScrollbarStyle, CanvasVerticalScrollbar
from caveviewer.gui.section_spacing import (
    PRIMARY_LABEL_ROW_GAP,
    PRIMARY_LABEL_ROW_HEIGHT,
    PRIMARY_LABEL_ROW_TOP_INSET,
    PRIMARY_SURFACE_VERTICAL_MARGIN,
)


_BODY_INSET = 24
_PARAGRAPH_GAP = 20
_SECTION_GAP = 32
_STATISTIC_LABEL_WIDTH = 260
_STATISTIC_COLUMN_GAP = 16


@dataclass(frozen=True)
class CaveMetadataPanelStyle:
    """Theme tokens for the Map Library cave-information surface."""

    background_color: str
    title_color: str
    subtitle_color: str
    section_color: str
    body_color: str
    divider_color: str
    link_color: str
    link_hover_color: str
    title_font: tuple
    subtitle_font: tuple
    section_font: tuple
    body_strong_font: tuple
    body_font: tuple
    small_font: tuple


class CaveMetadataPanel:
    """Render descriptive metadata without owning map or source-opening policy."""

    def __init__(
        self,
        parent,
        *,
        cave: CaveMetadata,
        px: Callable[[int | float], int],
        bind_activation: Callable[[object, Callable[[], None]], None],
        style: CaveMetadataPanelStyle,
        on_back: Callable[[], None],
        on_open_source: Callable[[str], None],
    ) -> None:
        self.parent = parent
        self.cave = cave
        self._px = px
        self._bind_activation = bind_activation
        self._style = style
        self._on_back = on_back
        self._on_open_source = on_open_source
        self._content = None
        self._back_control = None
        self._wrapping_labels: list[tk.Label] = []
        self._statistic_labels: list[tk.Label] = []
        self._statistic_values: list[tk.Label] = []
        self._statistics = None

    def create(self) -> None:
        """Compose the breadcrumb and one scrollable, flat details column."""
        style = self._style
        content = tk.Frame(self.parent, bg=style.background_color)
        content.pack(
            fill="both", expand=True,
            pady=(self._px(PRIMARY_LABEL_ROW_TOP_INSET), self._px(PRIMARY_SURFACE_VERTICAL_MARGIN)),
        )
        self._content = content
        breadcrumb = tk.Frame(
            content, bg=style.background_color,
            height=self._px(PRIMARY_LABEL_ROW_HEIGHT),
        )
        breadcrumb.pack(fill="x")
        breadcrumb.pack_propagate(False)
        arrow_slot = tk.Frame(
            breadcrumb, bg=style.background_color, width=self._px(_BODY_INSET),
        )
        arrow_slot.pack(side="left", fill="y")
        arrow_slot.pack_propagate(False)
        arrow = self._label(arrow_slot, "←", color=style.subtitle_color)
        self._bind_link(arrow, self._on_back, color=style.subtitle_color)
        arrow.configure(takefocus=False)
        arrow.pack(fill="both", expand=True)
        back = self._label(breadcrumb, "Map Library", color=style.subtitle_color)
        self._bind_link(back, self._on_back, color=style.subtitle_color)
        back.pack(side="left", fill="y")
        self._back_control = back
        self._label(breadcrumb, "/", color=style.subtitle_color).pack(
            side="left", padx=self._px(10), fill="y",
        )
        name = self._label(breadcrumb, self.cave.name, color=style.subtitle_color)
        name.pack(side="left", fill="both", expand=True)
        name.bind("<Configure>", lambda event: name.configure(wraplength=max(1, event.width)))

        viewport = tk.Frame(content, bg=style.background_color)
        viewport.pack(
            fill="both", expand=True, padx=(self._px(_BODY_INSET), 0),
            pady=(self._px(PRIMARY_LABEL_ROW_GAP), 0),
        )
        viewport.grid_columnconfigure(0, weight=1)
        viewport.grid_rowconfigure(0, weight=1)
        self._canvas = tk.Canvas(
            viewport, bg=style.background_color, borderwidth=0, highlightthickness=0,
        )
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._scrollbar = CanvasVerticalScrollbar(
            viewport, canvas=self._canvas, px=self._px,
            style=CanvasScrollbarStyle(background_color=style.background_color),
        )
        self._scrollbar.mount_grid(row=0, column=1, sticky="ns")
        body = tk.Frame(self._canvas, bg=style.background_color)
        self._body = body
        self._body_window = self._canvas.create_window(0, 0, window=body, anchor="nw")

        # Center the first line on the second navigation row, even when the
        # paragraph wraps or the operating system scales the font.
        line_height = tkfont.Font(root=content, font=style.body_font).metrics("linespace")
        first_line_inset = max(0, (self._px(PRIMARY_LABEL_ROW_HEIGHT) - line_height) // 2)
        tk.Frame(body, bg=style.background_color, height=first_line_inset).pack(fill="x")
        for index, fact in enumerate(self.cave.facts):
            self._label(body, fact, wrap=True).pack(
                fill="x", pady=(0 if index == 0 else self._px(_PARAGRAPH_GAP), 0),
            )

        if self.cave.statistics:
            self._section_label(body, "Key facts", first=not self.cave.facts)
            statistics = tk.Frame(body, bg=style.background_color)
            statistics.pack(fill="x")
            self._statistics = statistics
            for row, statistic in enumerate(self.cave.statistics):
                label = self._label(statistics, statistic.label)
                label.grid(row=row, column=0, sticky="nw", pady=(0, self._px(8)))
                value = self._label(
                    statistics, statistic.display_value,
                    color=style.link_color, font=style.body_strong_font,
                )
                value.grid(
                    row=row, column=1, sticky="nw",
                    padx=(self._px(_STATISTIC_COLUMN_GAP), 0), pady=(0, self._px(8)),
                )
                self._statistic_labels.append(label)
                self._statistic_values.append(value)

        if self.cave.sources:
            self._section_label(body, "Source", first=not (self.cave.facts or self.cave.statistics))
            for source in self.cave.sources:
                link = self._label(body, f"{source.title}  ↗", color=style.link_color, wrap=True)
                self._bind_link(link, lambda url=source.url: self._on_open_source(url), color=style.link_color)
                link.pack(fill="x", pady=(0, self._px(6)))

        self._label(
            body, "This describes the cave system, not necessarily this 3D map.",
            color=style.subtitle_color, font=style.small_font, wrap=True,
        ).pack(fill="x", pady=(self._px(28), 0))

        self._canvas.bind("<Configure>", self._resize_content, add="+")
        body.bind("<Configure>", self._sync_scroll_region, add="+")
        self._scrollbar.bind_mousewheel(body)
        self._bind_focus_scrolling(body)

    def _label(self, parent, text: str, *, color=None, font=None, wrap=False):
        label = tk.Label(
            parent, text=text, bg=self._style.background_color,
            fg=color or self._style.body_color, font=font or self._style.body_font,
            anchor="w", justify="left", padx=0, pady=0, borderwidth=0,
        )
        if wrap:
            self._wrapping_labels.append(label)
        return label

    def _bind_link(self, link, command, *, color: str) -> None:
        state = {"hovered": False, "focused": False}
        configure_text_link(link)
        self._bind_activation(link, command)

        def update(key, value):
            state[key] = value
            link.configure(fg=self._style.link_hover_color if any(state.values()) else color)

        for event, key, value in (
            ("<Enter>", "hovered", True), ("<Leave>", "hovered", False),
            ("<FocusIn>", "focused", True), ("<FocusOut>", "focused", False),
        ):
            link.bind(event, lambda _event, key=key, value=value: update(key, value), add="+")

    def _section_label(self, parent, text: str, *, first=False) -> None:
        self._label(parent, text.upper(), color=self._style.section_color, font=self._style.section_font).pack(
            fill="x", pady=(0 if first else self._px(_SECTION_GAP), self._px(14)),
        )

    def _resize_content(self, event) -> None:
        width = max(1, event.width)
        self._canvas.itemconfigure(self._body_window, width=width)
        for label in self._wrapping_labels:
            label.configure(wraplength=width)
        if self._statistics is not None:
            label_width = min(self._px(_STATISTIC_LABEL_WIDTH), width // 2)
            self._statistics.grid_columnconfigure(0, minsize=label_width)
            for label in self._statistic_labels:
                label.configure(wraplength=label_width)
            for value in self._statistic_values:
                value.configure(wraplength=max(1, width - label_width - self._px(_STATISTIC_COLUMN_GAP)))
        self._sync_scroll_region()

    def _sync_scroll_region(self, _event=None) -> None:
        height = self._body.winfo_reqheight()
        self._canvas.configure(scrollregion=(0, 0, self._canvas.winfo_width(), height))
        self._scrollbar.sync_overflow(height)

    def _bind_focus_scrolling(self, widget) -> None:
        widget.bind("<FocusIn>", self._reveal_focus, add="+")
        for child in widget.winfo_children():
            self._bind_focus_scrolling(child)

    def _reveal_focus(self, event) -> None:
        widget = event.widget
        top = widget.winfo_rooty() - self._body.winfo_rooty()
        bottom = top + widget.winfo_height()
        visible_top = self._canvas.canvasy(0)
        visible_height = self._canvas.winfo_height()
        height = max(1, self._body.winfo_height())
        if top < visible_top:
            self._canvas.yview_moveto(top / height)
        elif bottom > visible_top + visible_height:
            self._canvas.yview_moveto((bottom - visible_height) / height)

    def focus_content(self) -> None:
        """Focus the neutral detail surface without selecting an action."""
        if self._content is not None:
            try:
                self._content.focus_set()
            except tk.TclError:
                pass
