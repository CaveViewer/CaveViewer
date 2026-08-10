"""Tk presentation for the Map Library's in-panel cave information view.

The panel receives already matched, offline catalog data and only renders it.
It deliberately owns no matching, storage, or desktop-opening policy.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Callable

from caveviewer.gui.cave_metadata import CaveMetadata


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
    """Render one cave's descriptive metadata inside the splash content area."""

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

    def create(self) -> None:
        """Build the compact, non-modal cave details presentation."""
        style = self._style
        content = tk.Frame(self.parent, bg=style.background_color)
        content.pack(fill="both", expand=True, padx=self._px(18), pady=self._px(16))
        self._content = content

        back = tk.Label(
            content,
            text="‹  Map Library",
            font=style.body_font,
            fg=style.subtitle_color,
            bg=style.background_color,
            anchor="w",
            cursor="hand2",
            takefocus=True,
            highlightthickness=0,
        )

        back_state = {"hovered": False, "focused": False}

        def refresh_back_link() -> None:
            back.config(
                fg=(
                    style.link_hover_color
                    if back_state["hovered"] or back_state["focused"]
                    else style.subtitle_color
                )
            )

        def set_back_hovered(hovered: bool) -> None:
            back_state["hovered"] = hovered
            refresh_back_link()

        def set_back_focused(focused: bool) -> None:
            back_state["focused"] = focused
            refresh_back_link()

        self._bind_activation(back, self._on_back)
        back.bind("<Enter>", lambda _event: set_back_hovered(True))
        back.bind("<Leave>", lambda _event: set_back_hovered(False))
        back.bind("<FocusIn>", lambda _event: set_back_focused(True))
        back.bind("<FocusOut>", lambda _event: set_back_focused(False))
        back.pack(anchor="w", pady=(0, self._px(20)))
        self._back_control = back

        tk.Label(
            content,
            text=self.cave.name,
            font=style.title_font,
            fg=style.title_color,
            bg=style.background_color,
            anchor="w",
            justify="left",
            wraplength=self._px(500),
        ).pack(fill="x")
        tk.Label(
            content,
            text=self.cave.library_detail,
            font=style.subtitle_font,
            fg=style.subtitle_color,
            bg=style.background_color,
            anchor="w",
            justify="left",
            wraplength=self._px(500),
        ).pack(fill="x", pady=(self._px(4), self._px(18)))

        if self.cave.facts:
            self._divider(content)
            self._section_label(content, "About this cave")
            for fact in self.cave.facts:
                tk.Label(
                    content,
                    text=fact,
                    font=style.body_font,
                    fg=style.body_color,
                    bg=style.background_color,
                    anchor="w",
                    justify="left",
                    wraplength=self._px(500),
                ).pack(fill="x", pady=(0, self._px(8)))

        if self.cave.statistics:
            self._divider(content)
            self._section_label(content, "Key facts")
            for statistic in self.cave.statistics:
                statistic_row = tk.Frame(content, bg=style.background_color)
                statistic_row.pack(fill="x", pady=(0, self._px(6)))
                tk.Label(
                    statistic_row,
                    text=statistic.display_value,
                    font=style.body_strong_font,
                    fg=style.title_color,
                    bg=style.background_color,
                    anchor="e",
                ).pack(side="right", padx=(self._px(14), 0))
                tk.Label(
                    statistic_row,
                    text=statistic.label,
                    font=style.body_font,
                    fg=style.body_color,
                    bg=style.background_color,
                    anchor="w",
                ).pack(side="left", fill="x", expand=True)

        if self.cave.sources:
            self._divider(content)
            self._section_label(content, "Source")
            for source in self.cave.sources:
                link = tk.Label(
                    content,
                    text=f"{source.title}  ↗",
                    font=style.body_font,
                    fg=style.link_color,
                    bg=style.background_color,
                    anchor="w",
                    cursor="hand2",
                    takefocus=True,
                    highlightthickness=1,
                    highlightbackground=style.background_color,
                    highlightcolor=style.link_color,
                    justify="left",
                    wraplength=self._px(500),
                )
                self._bind_activation(
                    link,
                    lambda url=source.url: self._on_open_source(url),
                )
                link.bind(
                    "<Enter>",
                    lambda _event, target=link: target.config(fg=style.link_hover_color),
                )
                link.bind(
                    "<Leave>",
                    lambda _event, target=link: target.config(fg=style.link_color),
                )
                link.pack(fill="x", pady=(0, self._px(6)))

        self._divider(content)
        tk.Label(
            content,
            text="This describes the cave system, not necessarily this 3D map.",
            font=style.small_font,
            fg=style.subtitle_color,
            bg=style.background_color,
            anchor="w",
            justify="left",
            wraplength=self._px(500),
        ).pack(fill="x")

    def focus_content(self) -> None:
        """Focus the neutral detail surface without outlining the back link."""
        target = self._content or self._back_control
        if target is None:
            return
        try:
            target.focus_set()
        except tk.TclError:
            pass

    def _divider(self, parent) -> None:
        tk.Frame(
            parent,
            bg=self._style.divider_color,
            height=max(1, self._px(1)),
        ).pack(fill="x", pady=(self._px(14), self._px(13)))

    def _section_label(self, parent, text: str) -> None:
        tk.Label(
            parent,
            text=text.upper(),
            font=self._style.section_font,
            fg=self._style.section_color,
            bg=self._style.background_color,
            anchor="w",
        ).pack(fill="x", pady=(0, self._px(9)))
