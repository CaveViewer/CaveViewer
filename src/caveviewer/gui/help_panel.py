"""Tk presentation for the splash-window keyboard-binding reference."""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from dataclasses import dataclass
from typing import Callable, Iterable

from caveviewer.gui.controls_catalog import (
    KeyboardShortcut,
    KeyboardShortcutSection,
    is_help_shortcut_visible,
    shortcut_keycap_parts,
)
from caveviewer.gui.dialog_style import (
    create_dialog_action_button,
    set_dialog_action_button,
)
from caveviewer.gui.section_spacing import STANDARD_CONTENT_SECTION_SPACING
from caveviewer.gui.scrollable_content import (
    CanvasScrollbarStyle,
    CanvasVerticalScrollbar,
)
from caveviewer.gui.top_tab_strip import (
    TABBED_CONTENT_ALIGNMENT_INSET,
    TopTab,
    TopTabbedContentSurface,
    TopTabbedContentSurfaceStyle,
    TopTabStripStyle,
)
from caveviewer.gui.troubleshooting_logs import (
    TroubleshootingLogController,
    TroubleshootingLogState,
)
from caveviewer.gui.tk_feedback import COPY_FEEDBACK_MS


_CAPTURE_HELP_LAYOUT = (
    (
        "capture-control",
        "Capture Control",
        (
            (
                "capture-cancel",
                "Cancel active capture",
                "Only one capture can run at a time. Other capture shortcuts "
                "are ignored; Escape discards the active capture and removes "
                "partial files, then confirms that nothing was saved before "
                "the viewer closes.",
            ),
        ),
    ),
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
                    if is_help_shortcut_visible(shortcut)
                )
        if shortcuts:
            if section_id == "move":
                speed_ids = {"move-speed-decrease", "move-speed-increase"}
                if speed_ids.issubset(shortcut.id for shortcut in shortcuts):
                    compact_shortcuts: list[KeyboardShortcut] = []
                    for shortcut in shortcuts:
                        if shortcut.id == "move-speed-decrease":
                            compact_shortcuts.append(
                                KeyboardShortcut(
                                    id="move-speed-adjust",
                                    shortcut="- =",
                                    action="Decrease/increase speed",
                                )
                            )
                        elif shortcut.id != "move-speed-increase":
                            compact_shortcuts.append(shortcut)
                    shortcuts = compact_shortcuts
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
    content_pad_x: int
    tab_font: tuple
    section_font: tuple
    keycap_font: tuple
    action_font: tuple
    overview_font: tuple
    detail_font: tuple
    error_font: tuple


def copy_error_excerpt_to_clipboard(clipboard, text: str) -> bool:
    """Replace the Tk clipboard with exactly the displayed error excerpt."""

    try:
        clipboard.clipboard_clear()
        clipboard.clipboard_append(text)
    except Exception:
        return False
    return True


class HelpPanel:
    """Own the embedded, scrollable Help tables on the Tk main thread."""

    def __init__(
        self,
        parent,
        *,
        px: Callable[[int | float], int],
        style: HelpPanelStyle,
        sections: Iterable[KeyboardShortcutSection],
        troubleshooting_controller: TroubleshootingLogController | None = None,
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
        self._tabs.append(TopTab("troubleshooting", "Troubleshooting"))
        self._troubleshooting_controller = troubleshooting_controller
        self._troubleshooting_state = TroubleshootingLogState(
            latest_log=None,
            status_text=(
                "No logs yet. A log will appear after CaveViewer records "
                "an application session."
            ),
            error_status_text="The latest error will appear here when available.",
        )
        self._troubleshooting_button = None
        self._copy_error_button = None
        self._copy_feedback = ""
        self._copy_feedback_is_error = False
        self._copy_feedback_after_id: str | None = None
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
        self._troubleshooting_button = create_dialog_action_button(
            canvas,
            "Show latest log",
            self._show_latest_log,
            font=style.overview_font,
            enabled=False,
            padx=self._px(12),
            pady=self._px(7),
        )
        self._copy_error_button = create_dialog_action_button(
            canvas,
            "⧉  Copy",
            self._copy_last_error,
            font=style.detail_font,
            kind="secondary",
            enabled=False,
            padx=self._px(10),
            pady=self._px(5),
        )
        self._copy_error_button._cv_accessible_name = "Copy last error"

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
        if key not in {*self._tab_sections, "troubleshooting"}:
            return
        if key == self._active_tab_key:
            return

        self._active_tab_key = key
        if key == "troubleshooting":
            self._refresh_troubleshooting()
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
        if active_tab_key == "troubleshooting":
            self._render_troubleshooting(canvas, width)
            return
        y = 0
        for section_index, section in enumerate(self._tab_sections[active_tab_key]):
            if section_index:
                y += self._px(STANDARD_CONTENT_SECTION_SPACING.between_sections_y)
            y = self._draw_section_heading(canvas, section, y)
            for shortcut_index, shortcut in enumerate(section.shortcuts):
                y = self._draw_shortcut_row(
                    canvas,
                    shortcut,
                    y,
                    width,
                    top_pad_y=0 if shortcut_index == 0 else None,
                )

        self._content_height = max(0, y)
        try:
            canvas.configure(scrollregion=(0, 0, width, self._content_height))
        except tk.TclError:
            return
        scrollbar = self._scrollbar
        if scrollbar is not None:
            scrollbar.sync_overflow(self._content_height)

    def _refresh_troubleshooting(self) -> None:
        self._cancel_copy_feedback_timer()
        controller = self._troubleshooting_controller
        if controller is not None:
            self._troubleshooting_state = controller.refresh()
        self._copy_feedback = ""
        self._copy_feedback_is_error = False

    def _show_latest_log(self) -> None:
        controller = self._troubleshooting_controller
        if controller is None:
            return
        self._troubleshooting_state = controller.reveal_latest()
        canvas = self._content_canvas
        if canvas is not None:
            self._render_table(canvas.winfo_width())

    def _copy_last_error(self) -> None:
        excerpt = self._troubleshooting_state.error_excerpt
        canvas = self._content_canvas
        if excerpt is None or canvas is None:
            return
        copied = copy_error_excerpt_to_clipboard(canvas, excerpt.text)
        self._cancel_copy_feedback_timer()
        self._copy_feedback = (
            "Copied" if copied else "Couldn’t copy. Select the text manually."
        )
        self._copy_feedback_is_error = not copied
        button = self._copy_error_button
        if button is not None:
            set_dialog_action_button(
                button,
                text="Copied" if copied else "⧉  Copy",
            )
            if copied:
                try:
                    self._copy_feedback_after_id = canvas.after(
                        COPY_FEEDBACK_MS,
                        self._reset_copy_button_label,
                    )
                except tk.TclError:
                    pass
        self._render_table(canvas.winfo_width())

    def _reset_copy_button_label(self) -> None:
        self._copy_feedback_after_id = None
        self._copy_feedback = ""
        self._copy_feedback_is_error = False
        button = self._copy_error_button
        if button is not None:
            try:
                set_dialog_action_button(button, text="⧉  Copy")
            except tk.TclError:
                return
        canvas = self._content_canvas
        if canvas is not None:
            try:
                self._render_table(canvas.winfo_width())
            except tk.TclError:
                pass

    def _cancel_copy_feedback_timer(self) -> None:
        """Cancel the copy confirmation timer before replacing its state."""
        after_id = self._copy_feedback_after_id
        self._copy_feedback_after_id = None
        canvas = self._content_canvas
        if after_id is None or canvas is None:
            return
        try:
            canvas.after_cancel(after_id)
        except tk.TclError:
            pass

    def on_hidden(self) -> None:
        """Clear copy feedback when the user leaves Help."""
        self._cancel_copy_feedback_timer()
        self._copy_feedback = ""
        self._copy_feedback_is_error = False
        button = self._copy_error_button
        if button is not None:
            try:
                set_dialog_action_button(button, text="⧉  Copy")
            except tk.TclError:
                pass

    def _render_troubleshooting(self, canvas, width: int) -> None:
        """Render the log-reveal action in the shared Help scroll surface."""

        style = self._style
        state = self._troubleshooting_state
        content_x = self._px(TABBED_CONTENT_ALIGNMENT_INSET)
        content_right = width - self._px(12)
        content_width = max(self._px(260), content_right - content_x)
        y = 0
        canvas.create_text(
            content_x,
            y,
            text="APPLICATION LOGS",
            font=self._canvas_font("section"),
            fill=style.section_color,
            anchor="nw",
            tags="help-content",
        )
        y += self._font_line_height("section") + self._px(
            STANDARD_CONTENT_SECTION_SPACING.heading_to_content_y
        )
        description = canvas.create_text(
            content_x,
            y,
            text="Share the latest log with support to help diagnose a problem.",
            font=self._canvas_font("action"),
            fill=style.action_color,
            anchor="nw",
            justify="left",
            width=content_width,
            tags="help-content",
        )
        bounds = canvas.bbox(description)
        y += (
            self._font_line_height("action")
            if bounds is None
            else max(1, bounds[3] - bounds[1])
        ) + self._px(18)

        button = self._troubleshooting_button
        if button is not None:
            # Native Tk buttons created disabled intentionally receive no
            # command. Restore it when refreshed log state enables the action.
            set_dialog_action_button(
                button,
                command=self._show_latest_log,
                enabled=state.can_reveal,
            )
            canvas.create_window(
                content_x,
                y,
                window=button,
                anchor="nw",
                tags="help-content",
            )
            try:
                button.update_idletasks()
                button_height = max(self._px(36), button.winfo_reqheight())
            except tk.TclError:
                button_height = self._px(36)
            y += button_height + self._px(12)

        if state.status_text:
            status_item = canvas.create_text(
                content_x,
                y,
                text=state.status_text,
                font=self._canvas_font("detail"),
                fill=("#ff9b90" if state.is_error else style.detail_color),
                anchor="nw",
                justify="left",
                width=content_width,
                tags="help-content",
            )
            status_bounds = canvas.bbox(status_item)
            y += (
                self._font_line_height("detail")
                if status_bounds is None
                else max(1, status_bounds[3] - status_bounds[1])
            )

        y += self._px(STANDARD_CONTENT_SECTION_SPACING.between_sections_y)
        canvas.create_text(
            content_x,
            y,
            text="LAST ERROR",
            font=self._canvas_font("section"),
            fill=style.section_color,
            anchor="nw",
            tags="help-content",
        )
        heading_height = self._font_line_height("section")
        copy_button = self._copy_error_button
        if copy_button is not None and state.error_excerpt is not None:
            set_dialog_action_button(
                copy_button,
                command=self._copy_last_error,
                enabled=True,
            )
            try:
                copy_button.update_idletasks()
                copy_width = copy_button.winfo_reqwidth()
            except tk.TclError:
                copy_width = self._px(104)
            canvas.create_window(
                max(content_x, content_right - copy_width),
                y,
                window=copy_button,
                anchor="nw",
                tags="help-content",
            )
        y += heading_height + self._px(
            STANDARD_CONTENT_SECTION_SPACING.heading_to_content_y
        )

        excerpt = state.error_excerpt
        if excerpt is not None:
            text_pad = self._px(12)
            excerpt_item = canvas.create_text(
                content_x + text_pad,
                y + text_pad,
                text=excerpt.text,
                font=style.error_font,
                fill=style.action_color,
                anchor="nw",
                justify="left",
                width=max(self._px(240), content_width - (text_pad * 2)),
                tags="help-content",
            )
            excerpt_bounds = canvas.bbox(excerpt_item)
            excerpt_height = (
                self._font_line_height("detail")
                if excerpt_bounds is None
                else max(1, excerpt_bounds[3] - excerpt_bounds[1])
            )
            canvas.create_rectangle(
                content_x,
                y,
                content_right,
                y + excerpt_height + (text_pad * 2),
                fill=style.keycap_background_color,
                outline=style.keycap_border_color,
                width=1,
                tags="help-content",
            )
            canvas.tag_raise(excerpt_item)
            y += excerpt_height + (text_pad * 2) + self._px(10)
        elif state.error_status_text:
            empty_item = canvas.create_text(
                content_x,
                y,
                text=state.error_status_text,
                font=self._canvas_font("detail"),
                fill=("#ff9b90" if state.is_error else style.detail_color),
                anchor="nw",
                justify="left",
                width=content_width,
                tags="help-content",
            )
            empty_bounds = canvas.bbox(empty_item)
            y += (
                self._font_line_height("detail")
                if empty_bounds is None
                else max(1, empty_bounds[3] - empty_bounds[1])
            ) + self._px(10)

        if self._copy_feedback:
            feedback_item = canvas.create_text(
                content_x,
                y,
                text=self._copy_feedback,
                font=self._canvas_font("detail"),
                fill=(
                    "#ff9b90"
                    if self._copy_feedback_is_error
                    else style.detail_color
                ),
                anchor="nw",
                tags="help-content",
            )
            feedback_bounds = canvas.bbox(feedback_item)
            y += (
                self._font_line_height("detail")
                if feedback_bounds is None
                else max(1, feedback_bounds[3] - feedback_bounds[1])
            )

        self._content_height = max(0, y + self._px(16))
        try:
            canvas.configure(scrollregion=(0, 0, width, self._content_height))
        except tk.TclError:
            return
        if self._scrollbar is not None:
            self._scrollbar.sync_overflow(self._content_height)

    def _draw_section_heading(
        self,
        canvas,
        section: KeyboardShortcutSection,
        y: int,
    ) -> int:
        style = self._style
        canvas.create_text(
            self._px(TABBED_CONTENT_ALIGNMENT_INSET),
            y,
            text=section.title.upper(),
            font=self._canvas_font("section"),
            fill=style.section_color,
            anchor="nw",
            tags="help-content",
        )
        return (
            y
            + self._font_line_height("section")
            + self._px(STANDARD_CONTENT_SECTION_SPACING.heading_to_content_y)
        )

    def _draw_shortcut_row(
        self,
        canvas,
        shortcut: KeyboardShortcut,
        y: int,
        content_width: int,
        *,
        top_pad_y: int | None = None,
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
        resolved_top_pad_y = row_pad_y if top_pad_y is None else top_pad_y
        detail = shortcut.context_note
        primary_font_role = "overview" if detail else "action"
        action_item = canvas.create_text(
            action_x,
            y + resolved_top_pad_y,
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
                y + resolved_top_pad_y + action_height + detail_gap,
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
            x=self._px(TABBED_CONTENT_ALIGNMENT_INSET),
            y=(
                y
                + resolved_top_pad_y
                + max(0, (content_height - keycap_height) // 2)
            ),
            shortcut=shortcut.shortcut,
        )
        row_bottom = (
            y
            + resolved_top_pad_y
            + row_pad_y
            + max(content_height, keycap_height)
        )
        return row_bottom

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
