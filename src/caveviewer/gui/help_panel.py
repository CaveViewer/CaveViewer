"""Tk presentation for the splash-window keyboard-binding reference."""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from dataclasses import dataclass
from typing import Callable, Iterable

from caveviewer.gui.controls_catalog import (
    KeyboardShortcut,
    KeyboardShortcutSection,
    shortcut_keycap_parts,
)
from caveviewer.gui.scrollable_content import (
    CanvasScrollbarStyle,
    CanvasVerticalScrollbar,
)
from caveviewer.gui.top_tab_strip import (
    TopTab,
    TopTabbedContentSurface,
    TopTabbedContentSurfaceStyle,
    TopTabStripStyle,
)


_CAPTURE_HELP_LAYOUT = (
    (
        "video",
        "Video",
        (
            (
                "recording-toggle",
                "Start/stop video recording",
                "Saves what you see while diving as an MP4 video. It is not a "
                "replay route or map.",
            ),
        ),
    ),
    (
        "dive-trace",
        "Dive Trace",
        (
            (
                "manual-trace-toggle",
                "Start/stop manual trace",
                "Saves your camera path and timing for replay or analysis. It "
                "does not capture video or map geometry.",
            ),
        ),
    ),
    (
        "cave-slice",
        "Cave Slice",
        (
            (
                "slice-toggle",
                "Start/stop cave slice",
                "Saves the selected cave section as an independent CaveViewer "
                "map. It is precompiled and cannot be rebuilt because the source "
                "model is not included.",
            ),
        ),
    ),
)
_KEY_HELP_LAYOUT = (
    ("move", "Move", ("movement",)),
    ("look", "Look", ("view",)),
    ("navigate", "Navigate", ("bookmarks", "map", "recorded-dive")),
)
# Keep this supported chord out of the compact Help reference until its
# long-term bookmark-delete interaction is decided.
_KEY_HELP_EXCLUDED_SHORTCUT_IDS = frozenset({"bookmark-delete-control-shift"})


def key_help_sections(
    sections: Iterable[KeyboardShortcutSection],
) -> tuple[KeyboardShortcutSection, ...]:
    """Return Keys-tab shortcuts grouped like the in-view controls overlay.

    Capture has its own artifact-focused tab below. Map-import controls remain
    out of this compact navigation reference for the same reason.
    """
    sections_by_id = {section.id: section for section in sections}
    grouped_sections: list[KeyboardShortcutSection] = []
    for section_id, title, source_section_ids in _KEY_HELP_LAYOUT:
        shortcuts: list[KeyboardShortcut] = []
        for source_section_id in source_section_ids:
            source_section = sections_by_id.get(source_section_id)
            if source_section is not None:
                shortcuts.extend(
                    shortcut
                    for shortcut in source_section.shortcuts
                    if shortcut.id not in _KEY_HELP_EXCLUDED_SHORTCUT_IDS
                )
        if shortcuts:
            grouped_sections.append(
                KeyboardShortcutSection(
                    id=section_id,
                    title=title,
                    shortcuts=tuple(shortcuts),
                )
            )
    return tuple(grouped_sections)


def capture_help_sections(
    capture_section: KeyboardShortcutSection,
) -> tuple[KeyboardShortcutSection, ...]:
    """Return Capture-tab rows with artifact-specific user guidance.

    Shortcuts come from the shared keyboard catalog so the displayed primary
    modifier remains correct for the current platform.  The Help tab owns the
    extra description of each saved artifact.
    """
    shortcuts_by_id = {
        shortcut.id: shortcut for shortcut in capture_section.shortcuts
    }
    sections: list[KeyboardShortcutSection] = []
    for section_id, title, rows in _CAPTURE_HELP_LAYOUT:
        shortcuts = tuple(
            KeyboardShortcut(
                id=shortcut_id,
                shortcut=shortcuts_by_id[shortcut_id].shortcut,
                action=action,
                context_note=context_note,
            )
            for shortcut_id, action, context_note in rows
            if shortcut_id in shortcuts_by_id
        )
        if shortcuts:
            sections.append(
                KeyboardShortcutSection(
                    id=section_id,
                    title=title,
                    shortcuts=shortcuts,
                )
            )
    return tuple(sections)


@dataclass(frozen=True)
class HelpPanelStyle:
    """Theme and typography tokens owned by the splash Help presentation."""

    background_color: str
    tab_active_color: str
    tab_focus_color: str
    section_color: str
    keycap_background_color: str
    keycap_border_color: str
    keycap_text_color: str
    action_color: str
    detail_color: str
    row_divider_color: str
    content_pad_x: int
    tab_font: tuple
    section_font: tuple
    keycap_font: tuple
    action_font: tuple
    overview_font: tuple
    detail_font: tuple


class HelpPanel:
    """Own the embedded, scrollable Help tables on the Tk main thread."""

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
        source_sections = tuple(sections)
        capture_section = next(
            (section for section in source_sections if section.id == "capture"),
            None,
        )
        self._tab_sections: dict[str, tuple[KeyboardShortcutSection, ...]] = {
            "keys": key_help_sections(source_sections),
        }
        self._tabs = [TopTab("keys", "Keys")]
        if capture_section is not None:
            self._tab_sections["capture"] = capture_help_sections(capture_section)
            self._tabs.append(TopTab("capture", "Capture"))
        self._shell = None
        self._content_canvas = None
        self._scrollbar = None
        self._tab_strip = None
        self._active_tab_key = None
        self._section_font = None
        self._keycap_font = None
        self._action_font = None
        self._overview_font = None
        self._detail_font = None
        self._content_height = 0

    def create(self) -> None:
        """Build the tabbed Help reference inside the splash-owned surface."""
        if self._shell is not None:
            return

        style = self._style
        surface = TopTabbedContentSurface(
            self.parent,
            tabs=tuple(self._tabs),
            active_key="keys",
            on_selected=self._show_tab,
            px=self._px,
            tab_style=TopTabStripStyle(
                background_color=style.background_color,
                baseline_color=style.row_divider_color,
                active_color=style.tab_active_color,
                inactive_color=style.section_color,
                focus_color=style.tab_focus_color,
                font=style.tab_font,
            ),
            style=TopTabbedContentSurfaceStyle(
                background_color=style.background_color,
                content_pad_x=style.content_pad_x,
                content_bottom_pad_y=14,
            ),
        )
        surface.pack(fill="both", expand=True)
        self._shell = surface.widget
        self._tab_strip = surface.tab_strip

        content_shell = surface.content
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
        self._section_font = self._create_canvas_font(canvas, style.section_font)
        self._keycap_font = self._create_canvas_font(canvas, style.keycap_font)
        self._action_font = self._create_canvas_font(canvas, style.action_font)
        self._overview_font = self._create_canvas_font(canvas, style.overview_font)
        self._detail_font = self._create_canvas_font(canvas, style.detail_font)

        self._scrollbar = CanvasVerticalScrollbar(
            content_shell,
            canvas=canvas,
            px=self._px,
            style=CanvasScrollbarStyle(background_color=style.background_color),
        )
        self._scrollbar.mount_grid(row=0, column=1, sticky="ns")
        canvas.bind("<Configure>", self._on_canvas_configure, add="+")
        self._show_tab("keys")

    def focus_content(self) -> None:
        """Give the visible Help table a stable keyboard-focus target."""
        canvas = self._content_canvas
        if canvas is None:
            return
        try:
            canvas.focus_set()
        except tk.TclError:
            pass

    def _show_tab(self, key: str) -> None:
        """Draw the selected Help table without rebuilding Tk widget trees."""
        if key not in self._tab_sections:
            return
        if key == self._active_tab_key:
            return

        self._active_tab_key = key
        canvas = self._content_canvas
        if canvas is None:
            return
        self._render_table(canvas.winfo_width())
        try:
            canvas.yview_moveto(0)
        except tk.TclError:
            return

    def _create_canvas_font(self, canvas, font_spec: tuple):
        """Create one reusable measurement font for the static canvas table."""
        try:
            return tkfont.Font(root=canvas, font=font_spec)
        except tk.TclError:
            return None

    def _canvas_font(self, font_role: str):
        fallback = getattr(self._style, f"{font_role}_font")
        return getattr(self, f"_{font_role}_font") or fallback

    def _font_line_height(self, font_role: str) -> int:
        font = self._canvas_font(font_role)
        try:
            return max(1, int(font.metrics("linespace")))
        except (AttributeError, tk.TclError):
            return max(1, self._px(14))

    def _font_width(self, font_role: str, text: str) -> int:
        font = self._canvas_font(font_role)
        try:
            return max(0, int(font.measure(text)))
        except (AttributeError, tk.TclError):
            return max(0, self._px(8) * len(text))

    def _render_table(self, content_width: int | float) -> None:
        """Render a compact Help tab as canvas items instead of Tk widgets."""
        canvas = self._content_canvas
        active_tab_key = self._active_tab_key
        if canvas is None or active_tab_key is None:
            return
        try:
            width = max(self._px(320), int(float(content_width)))
        except (TypeError, ValueError):
            width = self._px(320)

        canvas.delete("help-content")
        y = 0
        for section_index, section in enumerate(self._tab_sections[active_tab_key]):
            if section_index:
                y += self._px(7)
            y = self._draw_section_heading(canvas, section, y)
            for shortcut in section.shortcuts:
                y = self._draw_shortcut_row(canvas, shortcut, y, width)

        self._content_height = max(0, y)
        try:
            canvas.configure(scrollregion=(0, 0, width, self._content_height))
        except tk.TclError:
            return
        scrollbar = self._scrollbar
        if scrollbar is not None:
            scrollbar.sync_overflow(self._content_height)

    def _draw_section_heading(
        self,
        canvas,
        section: KeyboardShortcutSection,
        y: int,
    ) -> int:
        style = self._style
        canvas.create_text(
            0,
            y,
            text=section.title.upper(),
            font=self._canvas_font("section"),
            fill=style.section_color,
            anchor="nw",
            tags="help-content",
        )
        return y + self._font_line_height("section") + self._px(7)

    def _draw_shortcut_row(
        self,
        canvas,
        shortcut: KeyboardShortcut,
        y: int,
        content_width: int,
    ) -> int:
        style = self._style
        key_width = min(
            self._px(250),
            max(self._px(170), int(content_width * 0.37)),
        )
        action_x = key_width + self._px(32)
        action_width = max(
            self._px(140),
            content_width - action_x - self._px(12),
        )
        row_pad_y = self._px(7)
        detail = shortcut.context_note
        primary_font_role = "overview" if detail else "action"
        action_item = canvas.create_text(
            action_x,
            y + row_pad_y,
            text=shortcut.action,
            font=self._canvas_font(primary_font_role),
            fill=style.action_color,
            anchor="nw",
            justify="left",
            width=action_width,
            tags="help-content",
        )
        action_bounds = canvas.bbox(action_item)
        action_height = (
            self._font_line_height(primary_font_role)
            if action_bounds is None
            else max(1, action_bounds[3] - action_bounds[1])
        )
        content_height = action_height
        if detail:
            detail_gap = self._px(3)
            detail_item = canvas.create_text(
                action_x,
                y + row_pad_y + action_height + detail_gap,
                text=detail,
                font=self._canvas_font("detail"),
                fill=style.detail_color,
                anchor="nw",
                justify="left",
                width=action_width,
                tags="help-content",
            )
            detail_bounds = canvas.bbox(detail_item)
            detail_height = (
                self._font_line_height("detail")
                if detail_bounds is None
                else max(1, detail_bounds[3] - detail_bounds[1])
            )
            content_height += detail_gap + detail_height
        keycap_height = self._keycap_height(shortcut.shortcut)
        self._draw_keycap_sequence(
            canvas,
            x=self._px(12),
            y=y + row_pad_y + max(0, (content_height - keycap_height) // 2),
            shortcut=shortcut.shortcut,
        )
        row_bottom = y + row_pad_y * 2 + max(content_height, keycap_height)
        canvas.create_line(
            0,
            row_bottom,
            content_width,
            row_bottom,
            fill=style.row_divider_color,
            width=max(1, self._px(1)),
            tags="help-content",
        )
        return row_bottom + max(1, self._px(1))

    def _keycap_height(self, shortcut: str) -> int:
        if not shortcut_keycap_parts(shortcut):
            return self._font_line_height("action")
        return self._font_line_height("keycap") + self._px(4) + 2

    def _draw_keycap_sequence(self, canvas, *, x: int, y: int, shortcut: str) -> None:
        style = self._style
        keycap_height = self._keycap_height(shortcut)
        cursor = x
        for part in shortcut_keycap_parts(shortcut):
            if part in {"+", "/"}:
                canvas.create_text(
                    cursor,
                    y + keycap_height / 2,
                    text=part,
                    font=self._canvas_font("action"),
                    fill=style.section_color,
                    anchor="w",
                    tags="help-content",
                )
                cursor += self._font_width("action", part) + self._px(8)
                continue
            keycap_width = self._font_width("keycap", part) + self._px(12) + 2
            canvas.create_rectangle(
                cursor,
                y,
                cursor + keycap_width,
                y + keycap_height,
                fill=style.keycap_background_color,
                outline=style.keycap_border_color,
                width=1,
                tags="help-content",
            )
            canvas.create_text(
                cursor + self._px(6) + 1,
                y + keycap_height / 2,
                text=part,
                font=self._canvas_font("keycap"),
                fill=style.keycap_text_color,
                anchor="w",
                tags="help-content",
            )
            cursor += keycap_width + self._px(5)

    def _on_canvas_configure(self, event) -> None:
        self._render_table(event.width)
