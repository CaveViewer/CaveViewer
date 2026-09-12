"""Shared text-tab navigation for splash-window content surfaces."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, replace
from typing import Callable, Iterable


TABBED_CONTENT_TOP_GAP = 26
TABBED_CONTENT_ALIGNMENT_INSET = 12


@dataclass(frozen=True)
class TopTab:
    """One stable tab identifier and its user-facing label."""

    key: str
    label: str


@dataclass(frozen=True)
class TopTabStripStyle:
    """Scale-independent visual tokens for one text-tab strip."""

    background_color: str
    active_color: str
    inactive_color: str
    focus_color: str
    font: tuple
    active_font: tuple | None = None
    inactive_font: tuple | None = None
    horizontal_inset: int = 14
    top_inset: int = 8
    tab_pad_x: int = TABBED_CONTENT_ALIGNMENT_INSET
    tab_pad_y: int = 7
    tab_gap: int = 10
    focus_highlight_thickness: int = 1
    active_indicator_color: str | None = None
    active_indicator_thickness: int = 2
    row_height: int | None = None


@dataclass(frozen=True)
class TopTabbedContentSurfaceStyle:
    """Shared layout tokens for content shown beneath a text tab strip."""

    background_color: str
    content_pad_left_x: int
    content_pad_right_x: int
    content_bottom_pad_y: int = 0


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
    """Own a compact, keyboard-accessible text-tab strip on Tk's UI thread."""

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
        self._tab_indicators: dict[str, tk.Frame] = {}
        self._tab_text = {tab.key: tab.label for tab in self._tabs}

        self.widget = tk.Frame(parent, bg=style.background_color)
        tab_row = tk.Frame(self.widget, bg=style.background_color)
        if style.row_height is not None:
            tab_row.configure(height=self._px(style.row_height))
        tab_row.pack(
            fill="x",
            padx=self._px(style.horizontal_inset),
            pady=(self._px(style.top_inset), 0),
        )
        if style.row_height is not None:
            tab_row.pack_propagate(False)
        for index, tab in enumerate(self._tabs):
            tab_shell = tk.Frame(tab_row, bg=style.background_color)
            right_gap = (
                self._px(style.tab_gap) if index < len(self._tabs) - 1 else 0
            )
            if style.row_height is not None:
                tab_shell.pack(
                    side="left",
                    fill="y",
                    padx=(0, right_gap),
                )
            else:
                tab_shell.pack(
                    side="left",
                    padx=(0, right_gap),
                )
            label = tk.Label(
                tab_shell,
                text=tab.label,
                font=style.inactive_font or style.font,
                fg=style.inactive_color,
                bg=style.background_color,
                anchor="center",
                padx=self._px(style.tab_pad_x),
                pady=self._px(style.tab_pad_y),
                takefocus=True,
                highlightthickness=self._px(style.focus_highlight_thickness),
                highlightbackground=style.background_color,
                highlightcolor=style.focus_color,
            )
            if style.row_height is None:
                label.pack(anchor="w")
            else:
                label.pack(fill="both", expand=True)
            self._tab_labels[tab.key] = label
            self._bind_tab_events(label, tab.key)
            if (
                style.active_indicator_color is not None
                and style.active_indicator_thickness > 0
            ):
                indicator = tk.Frame(
                    tab_shell,
                    bg=style.background_color,
                    height=self._px(style.active_indicator_thickness),
                )
                if style.row_height is None:
                    indicator.pack(
                        fill="x",
                        padx=self._px(style.tab_pad_x),
                    )
                else:
                    indicator_thickness = max(
                        1,
                        self._px(style.active_indicator_thickness),
                    )
                    indicator_pad_x = self._px(style.tab_pad_x)
                    indicator.place(
                        x=indicator_pad_x,
                        rely=1.0,
                        y=-indicator_thickness,
                        relwidth=1.0,
                        width=-(indicator_pad_x * 2),
                        height=indicator_thickness,
                    )
                self._tab_indicators[tab.key] = indicator

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
            label_options = {
                "fg": (
                    self._style.active_color
                    if active
                    else self._style.inactive_color
                )
            }
            selected_font = (
                self._style.active_font
                if active
                else self._style.inactive_font
            )
            if selected_font is not None:
                label_options["font"] = selected_font
            self._tab_labels[tab.key].configure(**label_options)
            indicator = self._tab_indicators.get(tab.key)
            if indicator is not None:
                indicator.configure(
                    bg=(
                        self._style.active_indicator_color
                        if active
                        else self._style.background_color
                    )
                )
        if notify and self._on_selected is not None:
            self._on_selected(key)

    def set_indicated(self, keys: Iterable[str]) -> None:
        """Mark tabs with pending content using a visible text indicator."""
        indicated = frozenset(keys)
        for key, label in self._tab_labels.items():
            suffix = " •" if key in indicated else ""
            label.configure(text=f"{self._tab_text[key]}{suffix}")

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


class TopTabbedContentSurface:
    """Provide standard text tabs and a content gap for splash panels.

    Every consumer gets the same inset, zero extra tab-top space, and the
    standard gap before the first content group.  Callers own only the
    content placed inside :attr:`content`.
    """

    def __init__(
        self,
        parent,
        *,
        tabs: Iterable[TopTab],
        active_key: str,
        on_selected: Callable[[str], None] | None,
        px: Callable[[int | float], int],
        tab_style: TopTabStripStyle,
        style: TopTabbedContentSurfaceStyle,
    ) -> None:
        self._px = px
        self._style = style
        self.widget = tk.Frame(parent, bg=style.background_color)
        horizontal_padding = (
            self._px(style.content_pad_left_x),
            self._px(style.content_pad_right_x),
        )

        tab_host = tk.Frame(self.widget, bg=style.background_color)
        tab_host.pack(fill="x", padx=horizontal_padding)
        self.tab_strip = TopTabStrip(
            tab_host,
            tabs=tabs,
            active_key=active_key,
            on_selected=on_selected,
            px=px,
            style=replace(
                tab_style,
                horizontal_inset=0,
                top_inset=0,
            ),
        )
        self.tab_strip.pack(fill="x")

        self.content = tk.Frame(self.widget, bg=style.background_color)
        self.content.pack(
            fill="both",
            expand=True,
            padx=horizontal_padding,
            pady=(
                self._px(TABBED_CONTENT_TOP_GAP),
                self._px(style.content_bottom_pad_y),
            ),
        )

    def pack(self, **pack_options) -> None:
        """Pack the full tabbed surface into its owner."""
        self.widget.pack(**pack_options)
