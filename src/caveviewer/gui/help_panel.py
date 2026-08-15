"""Tk presentation for the splash-window keyboard-binding reference."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Iterable

from caveviewer.gui.controls_catalog import (
    KeyboardShortcut,
    KeyboardShortcutSection,
    shortcut_keycap_parts,
)
from caveviewer.gui.tk_scrolling import vertical_scroll_units


@dataclass(frozen=True)
class HelpPanelStyle:
    """Theme and typography tokens owned by the splash Keys presentation."""

    background_color: str
    panel_color: str
    border_color: str
    table_border_color: str
    group_background_color: str
    row_background_color: str
    tab_active_color: str
    section_color: str
    keycap_background_color: str
    keycap_border_color: str
    keycap_text_color: str
    action_color: str
    separator_color: str
    scrollbar_thumb_color: str
    scrollbar_active_color: str
    scrollbar_trough_color: str
    tab_font: tuple
    section_font: tuple
    keycap_font: tuple
    action_font: tuple


class HelpPanel:
    """Own the embedded, scrollable Keys table on the Tk main thread."""

    def __init__(
        self,
        parent,
        *,
        px: Callable[[int | float], int],
        style: HelpPanelStyle,
        sections: Iterable[KeyboardShortcutSection],
    ) -> None:
        self.parent = parent
        self._px = px
        self._style = style
        self._sections = tuple(sections)
        self._shell = None
        self._content_canvas = None
        self._content_frame = None
        self._content_window = None
        self._scrollbar = None
        self._table_rows: list[object] = []
        self._action_labels: list[object] = []
        self._next_grid_row = 0
        self._scrollbar_visible = False

    def create(self) -> None:
        """Build the single-tab Keys table inside the splash-owned surface."""
        if self._shell is not None:
            return

        style = self._style
        shell = tk.Frame(
            self.parent,
            bg=style.panel_color,
            highlightthickness=1,
            highlightbackground=style.border_color,
            highlightcolor=style.border_color,
        )
        shell.pack(fill="both", expand=True, pady=self._px(14))
        self._shell = shell

        tab_strip = tk.Frame(shell, bg=style.panel_color)
        tab_strip.pack(fill="x", padx=self._px(14), pady=(self._px(8), 0))
        tab = tk.Frame(tab_strip, bg=style.panel_color)
        tab.pack(anchor="w")
        tk.Label(
            tab,
            text="Keys",
            font=style.tab_font,
            fg=style.tab_active_color,
            bg=style.panel_color,
            anchor="w",
        ).pack(anchor="w", padx=self._px(8))
        tk.Frame(
            tab,
            bg=style.tab_active_color,
            height=max(1, self._px(3)),
        ).pack(fill="x", pady=(self._px(7), 0))
        tk.Frame(
            shell,
            bg=style.separator_color,
            height=max(1, self._px(1)),
        ).pack(fill="x")

        content_shell = tk.Frame(shell, bg=style.background_color)
        content_shell.pack(
            fill="both",
            expand=True,
            padx=self._px(14),
            pady=(self._px(14), self._px(14)),
        )
        content_shell.grid_rowconfigure(0, weight=1)
        content_shell.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(
            content_shell,
            bg=style.background_color,
            borderwidth=0,
            highlightthickness=0,
            takefocus=True,
        )
        canvas.grid(row=0, column=0, sticky="nsew")
        self._content_canvas = canvas

        scrollbar = tk.Scrollbar(
            content_shell,
            orient="vertical",
            command=canvas.yview,
            takefocus=True,
            background=style.scrollbar_thumb_color,
            activebackground=style.scrollbar_active_color,
            troughcolor=style.scrollbar_trough_color,
            borderwidth=0,
            highlightthickness=0,
            width=max(1, self._px(9)),
        )
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(self._px(6), 0))
        scrollbar.grid_remove()
        self._scrollbar = scrollbar
        canvas.configure(yscrollcommand=self._on_canvas_yview)

        content = tk.Frame(
            canvas,
            bg=style.table_border_color,
            highlightthickness=1,
            highlightbackground=style.table_border_color,
            highlightcolor=style.table_border_color,
        )
        content.grid_columnconfigure(0, weight=1)
        self._content_frame = content
        self._content_window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", self._on_content_configure, add="+")
        canvas.bind("<Configure>", self._on_canvas_configure, add="+")

        for section in self._sections:
            self._create_section(content, section)
        self._bind_scroll_events(canvas)
        self._bind_mousewheel(content)

    def focus_content(self) -> None:
        """Give the visible Keys table a stable keyboard-focus target."""
        canvas = self._content_canvas
        if canvas is None:
            return
        try:
            canvas.focus_set()
        except tk.TclError:
            pass

    def _create_section(self, parent, section: KeyboardShortcutSection) -> None:
        style = self._style
        # ``_next_grid_row`` keeps one aligned sequence of dividers and rows
        # instead of independently sized cards.
        next_row = self._next_grid_row
        tk.Label(
            parent,
            text=section.title.upper(),
            font=style.section_font,
            fg=style.section_color,
            bg=style.group_background_color,
            anchor="w",
            padx=self._px(12),
            pady=self._px(7),
        ).grid(row=next_row, column=0, sticky="ew")
        self._next_grid_row = next_row + 1

        for shortcut in section.shortcuts:
            self._create_shortcut_row(parent, shortcut)

    def _create_shortcut_row(self, parent, shortcut: KeyboardShortcut) -> None:
        style = self._style
        row = tk.Frame(
            parent,
            bg=style.row_background_color,
            highlightthickness=1,
            highlightbackground=style.table_border_color,
            highlightcolor=style.table_border_color,
        )
        row.grid(row=self._next_grid_row, column=0, sticky="ew")
        row.grid_columnconfigure(2, weight=1)
        self._next_grid_row += 1
        self._table_rows.append(row)

        key_cell = tk.Frame(row, bg=style.row_background_color)
        key_cell.grid(
            row=0,
            column=0,
            sticky="w",
            padx=(self._px(12), self._px(10)),
            pady=self._px(7),
        )
        self._create_keycap_sequence(key_cell, shortcut.shortcut)
        tk.Frame(row, bg=style.separator_color, width=max(1, self._px(1))).grid(
            row=0,
            column=1,
            sticky="ns",
        )
        action_label = tk.Label(
            row,
            text=shortcut.action,
            font=style.action_font,
            fg=style.action_color,
            bg=style.row_background_color,
            anchor="w",
            justify="left",
        )
        action_label.grid(
            row=0,
            column=2,
            sticky="ew",
            padx=(self._px(14), self._px(12)),
            pady=self._px(7),
        )
        self._action_labels.append(action_label)

    def _create_keycap_sequence(self, parent, shortcut: str) -> None:
        style = self._style
        for part in shortcut_keycap_parts(shortcut):
            if part in {"+", "/"}:
                tk.Label(
                    parent,
                    text=part,
                    font=style.action_font,
                    fg=style.section_color,
                    bg=style.row_background_color,
                ).pack(side="left", padx=self._px(4))
                continue
            tk.Label(
                parent,
                text=part,
                font=style.keycap_font,
                fg=style.keycap_text_color,
                bg=style.keycap_background_color,
                highlightthickness=1,
                highlightbackground=style.keycap_border_color,
                highlightcolor=style.keycap_border_color,
                padx=self._px(6),
                pady=self._px(2),
            ).pack(side="left", padx=(0, self._px(5)))

    def _on_canvas_yview(self, first: str, last: str) -> None:
        scrollbar = self._scrollbar
        if scrollbar is None:
            return
        try:
            scrollbar.set(first, last)
            self._set_scrollbar_visible(float(first) > 0.0 or float(last) < 1.0)
        except (tk.TclError, ValueError):
            return

    def _on_content_configure(self, _event=None) -> None:
        self._sync_scroll_region()

    def _on_canvas_configure(self, event) -> None:
        canvas = self._content_canvas
        if canvas is None or self._content_window is None:
            return
        try:
            canvas.itemconfigure(self._content_window, width=event.width)
        except tk.TclError:
            return
        self._layout_table(event.width)
        self._sync_scroll_region()

    def _layout_table(self, content_width: int | float) -> None:
        try:
            available_width = max(1, int(float(content_width)))
        except (TypeError, ValueError):
            return
        key_width = min(
            self._px(250),
            max(self._px(170), int(available_width * 0.37)),
        )
        action_width = max(
            self._px(140),
            available_width - key_width - self._px(42),
        )
        for row in self._table_rows:
            row.grid_columnconfigure(0, minsize=key_width)
        for label in self._action_labels:
            label.configure(wraplength=action_width)

    def _sync_scroll_region(self) -> None:
        canvas = self._content_canvas
        if canvas is None:
            return
        try:
            bounds = canvas.bbox("all")
            if bounds is None:
                return
            canvas.configure(scrollregion=bounds)
            content_height = max(0, int(bounds[3] - bounds[1]))
            self._set_scrollbar_visible(content_height > canvas.winfo_height())
        except tk.TclError:
            return

    def _set_scrollbar_visible(self, visible: bool) -> None:
        scrollbar = self._scrollbar
        if scrollbar is None or visible == self._scrollbar_visible:
            return
        self._scrollbar_visible = visible
        if visible:
            scrollbar.grid()
        else:
            scrollbar.grid_remove()

    def _scroll_content(self, event):
        canvas = self._content_canvas
        if canvas is None:
            return None
        units = vertical_scroll_units(event)
        if units is None:
            return None
        try:
            canvas.yview_scroll(units, "units")
        except tk.TclError:
            return None
        return "break"

    def _bind_mousewheel(self, widget) -> None:
        self._bind_scroll_events(widget)
        for child in widget.winfo_children():
            self._bind_mousewheel(child)

    def _bind_scroll_events(self, widget) -> None:
        widget.bind("<MouseWheel>", self._scroll_content, add="+")
        widget.bind("<Button-4>", self._scroll_content, add="+")
        widget.bind("<Button-5>", self._scroll_content, add="+")
