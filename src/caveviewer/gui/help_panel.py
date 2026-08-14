"""Tk presentation for the splash-window keyboard shortcut reference."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Iterable

from caveviewer.gui.controls_catalog import KeyboardShortcutSection
from caveviewer.gui.tk_scrolling import vertical_scroll_units


@dataclass(frozen=True)
class HelpPanelStyle:
    """Theme and typography tokens owned by the splash Help presentation."""

    background_color: str
    panel_color: str
    border_color: str
    title_color: str
    section_color: str
    shortcut_color: str
    action_color: str
    note_color: str
    heading_font: tuple
    section_font: tuple
    shortcut_font: tuple
    action_font: tuple
    note_font: tuple


def help_section_column_count(
    content_width: int | float,
    *,
    two_column_min_width: int | float,
) -> int:
    """Choose the compact two-section layout only when it has room to read."""
    try:
        width = float(content_width)
        threshold = float(two_column_min_width)
    except (TypeError, ValueError):
        return 1
    return 2 if width >= max(1.0, threshold) else 1


class HelpPanel:
    """Own the embedded, scrollable keyboard-reference widgets on the Tk thread."""

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
        self._section_frames: list[object] = []
        self._shortcut_labels: list[object] = []
        self._action_labels: list[object] = []
        self._note_labels: list[object] = []
        self._scrollbar_visible = False

    def create(self) -> None:
        """Build the Help page once inside its splash-owned surface."""
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

        heading = tk.Frame(shell, bg=style.panel_color)
        heading.pack(fill="x", padx=self._px(20), pady=(self._px(14), self._px(8)))
        tk.Label(
            heading,
            text="Keyboard shortcuts",
            font=style.heading_font,
            fg=style.title_color,
            bg=style.panel_color,
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            heading,
            text="Controls are shown for this platform. Notes explain when a shortcut applies.",
            font=style.note_font,
            fg=style.note_color,
            bg=style.panel_color,
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(self._px(4), 0))

        content_shell = tk.Frame(shell, bg=style.panel_color)
        content_shell.pack(fill="both", expand=True, padx=self._px(14), pady=(0, self._px(14)))
        content_shell.grid_rowconfigure(0, weight=1)
        content_shell.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(
            content_shell,
            bg=style.panel_color,
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
        )
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(self._px(6), 0))
        scrollbar.grid_remove()
        self._scrollbar = scrollbar
        canvas.configure(yscrollcommand=self._on_canvas_yview)

        content = tk.Frame(canvas, bg=style.panel_color)
        self._content_frame = content
        self._content_window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", self._on_content_configure, add="+")
        canvas.bind("<Configure>", self._on_canvas_configure, add="+")

        for section in self._sections:
            self._create_section(content, section)
        self._bind_scroll_events(canvas)
        self._bind_mousewheel(content)

    def focus_content(self) -> None:
        """Give the visible Help surface a stable keyboard-focus target."""
        canvas = self._content_canvas
        if canvas is None:
            return
        try:
            canvas.focus_set()
        except tk.TclError:
            pass

    def _create_section(self, parent, section: KeyboardShortcutSection) -> None:
        style = self._style
        frame = tk.Frame(parent, bg=style.background_color)
        self._section_frames.append(frame)
        tk.Label(
            frame,
            text=section.title.upper(),
            font=style.section_font,
            fg=style.section_color,
            bg=style.background_color,
            anchor="w",
        ).grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=self._px(12),
            pady=(self._px(10), self._px(6)),
        )
        frame.grid_columnconfigure(1, weight=1)

        for row_index, shortcut in enumerate(section.shortcuts, start=1):
            row = tk.Frame(frame, bg=style.background_color)
            row.grid(
                row=row_index,
                column=0,
                columnspan=2,
                sticky="ew",
                padx=self._px(12),
                pady=(0, self._px(8)),
            )
            row.grid_columnconfigure(1, weight=1)
            shortcut_label = tk.Label(
                row,
                text=shortcut.shortcut,
                font=style.shortcut_font,
                fg=style.shortcut_color,
                bg=style.background_color,
                anchor="nw",
                justify="left",
            )
            shortcut_label.grid(
                row=0,
                column=0,
                rowspan=2,
                sticky="nw",
                padx=(0, self._px(12)),
            )
            self._shortcut_labels.append(shortcut_label)

            action_label = tk.Label(
                row,
                text=shortcut.action,
                font=style.action_font,
                fg=style.action_color,
                bg=style.background_color,
                anchor="w",
                justify="left",
            )
            action_label.grid(row=0, column=1, sticky="ew")
            self._action_labels.append(action_label)
            if shortcut.context_note:
                note_label = tk.Label(
                    row,
                    text=shortcut.context_note,
                    font=style.note_font,
                    fg=style.note_color,
                    bg=style.background_color,
                    anchor="w",
                    justify="left",
                )
                note_label.grid(row=1, column=1, sticky="ew", pady=(self._px(2), 0))
                self._note_labels.append(note_label)

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
        self._arrange_sections(event.width)
        self._sync_scroll_region()

    def _arrange_sections(self, content_width: int | float) -> None:
        content = self._content_frame
        if content is None:
            return
        columns = help_section_column_count(
            content_width,
            two_column_min_width=self._px(640),
        )
        for column in range(2):
            content.grid_columnconfigure(column, weight=1 if column < columns else 0)
        for index, frame in enumerate(self._section_frames):
            frame.grid(
                row=index // columns,
                column=index % columns,
                sticky="new",
                padx=self._px(6),
                pady=self._px(6),
            )
        available = max(1, int(float(content_width)))
        section_width = max(1, (available - self._px(12) * columns) // columns)
        shortcut_width = max(1, int(section_width * 0.34))
        action_width = max(1, section_width - shortcut_width - self._px(42))
        for label in self._shortcut_labels:
            label.configure(wraplength=shortcut_width)
        for label in (*self._action_labels, *self._note_labels):
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
