"""Shared underline-tab navigation for splash-window content surfaces."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class TopTab:
    """One stable tab identifier and its user-facing label."""

    key: str
    label: str


@dataclass(frozen=True)
class TopTabStripStyle:
    """Scale-independent visual tokens for one underline-tab strip."""

    background_color: str
    baseline_color: str
    active_color: str
    inactive_color: str
    focus_color: str
    font: tuple
    horizontal_inset: int = 14
    top_inset: int = 8
    tab_pad_x: int = 8
    tab_pad_y: int = 7
    tab_gap: int = 10
    underline_height: int = 3
    focus_highlight_thickness: int = 1


def next_tab_key(tabs: tuple[TopTab, ...], current_key: str, offset: int) -> str:
    """Return the adjacent tab key, wrapping at either end of a tab strip."""
    if not tabs:
        raise ValueError("tabs must not be empty")
    keys = tuple(tab.key for tab in tabs)
    try:
        index = keys.index(current_key)
    except ValueError:
        index = 0
    return keys[(index + offset) % len(keys)]


class TopTabStrip:
    """Own a compact, keyboard-accessible underline-tab strip on Tk's UI thread."""

    def __init__(
        self,
        parent,
        *,
        tabs: Iterable[TopTab],
        active_key: str,
        on_selected: Callable[[str], None] | None,
        px: Callable[[int | float], int],
        style: TopTabStripStyle,
    ) -> None:
        self._tabs = tuple(tabs)
        self._validate_tabs(active_key)
        self._active_key = active_key
        self._on_selected = on_selected
        self._px = px
        self._style = style
        self._tab_labels: dict[str, tk.Label] = {}
        self._indicators: dict[str, tk.Frame] = {}

        self.widget = tk.Frame(parent, bg=style.background_color)
        tab_row = tk.Frame(self.widget, bg=style.background_color)
        tab_row.pack(
            fill="x",
            padx=self._px(style.horizontal_inset),
            pady=(self._px(style.top_inset), 0),
        )
        for index, tab in enumerate(self._tabs):
            tab_shell = tk.Frame(tab_row, bg=style.background_color)
            right_gap = (
                self._px(style.tab_gap) if index < len(self._tabs) - 1 else 0
            )
            tab_shell.pack(
                side="left",
                padx=(0, right_gap),
            )
            label = tk.Label(
                tab_shell,
                text=tab.label,
                font=style.font,
                fg=style.inactive_color,
                bg=style.background_color,
                padx=self._px(style.tab_pad_x),
                pady=self._px(style.tab_pad_y),
                cursor="hand2",
                takefocus=True,
                highlightthickness=self._px(style.focus_highlight_thickness),
                highlightbackground=style.background_color,
                highlightcolor=style.focus_color,
            )
            label.pack(anchor="w")
            indicator = tk.Frame(
                tab_shell,
                bg=style.background_color,
                height=max(1, self._px(style.underline_height)),
            )
            indicator.pack(fill="x")
            self._tab_labels[tab.key] = label
            self._indicators[tab.key] = indicator
            self._bind_tab_events(label, tab.key)

        tk.Frame(
            self.widget,
            bg=style.baseline_color,
            height=max(1, self._px(1)),
        ).pack(fill="x")
        self.select(active_key, notify=False)

    @property
    def active_key(self) -> str:
        """Return the currently selected tab key."""
        return self._active_key

    def pack(self, **pack_options) -> None:
        """Pack the strip's outer widget into its owner surface."""
        self.widget.pack(**pack_options)

    def select(self, key: str, *, notify: bool = True) -> None:
        """Select a tab and optionally notify the owner to change its content."""
        if key not in self._tab_labels:
            return
        self._active_key = key
        for tab in self._tabs:
            active = tab.key == key
            self._tab_labels[tab.key].configure(
                fg=(
                    self._style.active_color
                    if active
                    else self._style.inactive_color
                ),
            )
            self._indicators[tab.key].configure(
                bg=(
                    self._style.active_color
                    if active
                    else self._style.background_color
                ),
            )
        if notify and self._on_selected is not None:
            self._on_selected(key)

    def _validate_tabs(self, active_key: str) -> None:
        if not self._tabs:
            raise ValueError("tabs must not be empty")
        keys = tuple(tab.key for tab in self._tabs)
        if any(not key for key in keys):
            raise ValueError("tab keys must not be empty")
        if len(set(keys)) != len(keys):
            raise ValueError("tab keys must be unique")
        if active_key not in keys:
            raise ValueError("active_key must identify one supplied tab")

    def _bind_tab_events(self, label, key: str) -> None:
        def activate(_event=None) -> str:
            self.select(key)
            self._focus_tab(key)
            return "break"

        label.bind("<Button-1>", activate)
        label.bind("<Return>", activate)
        label.bind("<space>", activate)
        label.bind(
            "<Left>",
            lambda _event, current=key: self._select_adjacent(current, -1),
        )
        label.bind(
            "<Right>",
            lambda _event, current=key: self._select_adjacent(current, 1),
        )

    def _select_adjacent(self, current_key: str, offset: int) -> str:
        target_key = next_tab_key(self._tabs, current_key, offset)
        self.select(target_key)
        self._focus_tab(target_key)
        return "break"

    def _focus_tab(self, key: str) -> None:
        try:
            self._tab_labels[key].focus_set()
        except tk.TclError:
            return
