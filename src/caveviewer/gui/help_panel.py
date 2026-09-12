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
    shortcut_keycap_unit_count,
)
from caveviewer.gui.help_canvas import (
    CanvasBounds,
    draw_rounded_canvas_box,
    measure_help_card,
    measure_shortcut_row,
)
from caveviewer.gui.help_style import (
    HELP_VISUAL_PALETTE,
    HELP_VISUAL_METRICS,
    help_section_content,
)
from caveviewer.gui.preferences_controls import RoundedActionButton
from caveviewer.gui.action_confirmation import (
    ACTION_CONFIRMATION_GAP,
    TransientActionConfirmation,
    create_confirmation_mark,
)
from caveviewer.gui.section_spacing import (
    PRIMARY_LABEL_ROW_HEIGHT,
    PRIMARY_LABEL_ROW_TOP_INSET,
    PRIMARY_SURFACE_VERTICAL_MARGIN,
)
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

_CAPTURE_HELP_LAYOUT = (
    (
        "capture-control",
        "Capture Control",
        (
            (
                "capture-cancel",
                "Cancel active capture",
                "Discards the active capture and removes partial files.",
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
                "Creates an MP4 file. It is not a replay route or map.",
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
                "Keeps path and timing data. It does not capture video or map "
                "geometry.",
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
                "Creates an independent pre-compiled map without the original "
                "source model.",
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
    section_background_color: str
    tab_active_color: str
    tab_indicator_color: str
    tab_focus_color: str
    section_color: str
    keycap_background_color: str
    keycap_border_color: str
    keycap_text_color: str
    action_color: str
    detail_color: str
    error_color: str
    content_pad_x: int
    tab_font: tuple
    tab_inactive_font: tuple
    section_font: tuple
    section_summary_font: tuple
    keycap_font: tuple
    action_font: tuple
    overview_font: tuple
    detail_font: tuple
    error_font: tuple


@dataclass(frozen=True, slots=True)
class _HelpCardIntro:
    """Measured card origin and first content position."""

    heading_item: int
    card_left: int
    content_x: int
    content_width: int
    content_y: int


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
        self._metrics = HELP_VISUAL_METRICS.scaled(px)
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
        self._copy_confirmation_mark = None
        self._copy_confirmation = None
        self._copy_confirmation_visible = False
        self._copy_feedback = ""
        self._copy_feedback_is_error = False
        self._shell = None
        self._content_canvas = None
        self._scrollbar = None
        self._tab_strip = None
        self._active_tab_key = None
        self._section_font = None
        self._section_summary_font = None
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
                inactive_color=style.detail_color,
                focus_color=style.tab_focus_color,
                font=style.tab_font,
                active_font=style.tab_font,
                inactive_font=style.tab_inactive_font,
                active_indicator_color=style.tab_indicator_color,
                row_height=PRIMARY_LABEL_ROW_HEIGHT,
            ),
            style=TopTabbedContentSurfaceStyle(
                background_color=style.background_color,
                content_pad_left_x=0,
                content_pad_right_x=style.content_pad_x,
                content_bottom_pad_y=14,
            ),
        )
        surface.pack(
            fill="both",
            expand=True,
            pady=(
                self._px(PRIMARY_LABEL_ROW_TOP_INSET),
                self._px(PRIMARY_SURFACE_VERTICAL_MARGIN),
            ),
        )
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
        self._section_summary_font = self._create_canvas_font(
            canvas,
            style.section_summary_font,
        )
        self._keycap_font = self._create_canvas_font(canvas, style.keycap_font)
        self._action_font = self._create_canvas_font(canvas, style.action_font)
        self._overview_font = self._create_canvas_font(canvas, style.overview_font)
        self._detail_font = self._create_canvas_font(canvas, style.detail_font)
        self._troubleshooting_button = RoundedActionButton(
            canvas,
            text="Show latest log",
            command=self._show_latest_log,
            font=style.overview_font,
            metrics=self._metrics,
            palette=HELP_VISUAL_PALETTE,
            enabled=False,
            outside_background=style.section_background_color,
        )
        self._copy_error_button = RoundedActionButton(
            canvas,
            text="⧉  Copy",
            command=self._copy_last_error,
            font=style.action_font,
            metrics=self._metrics,
            palette=HELP_VISUAL_PALETTE,
            kind="secondary",
            enabled=False,
            outside_background=style.section_background_color,
        )
        self._copy_error_button.button._cv_accessible_name = "Copy last error"
        self._copy_confirmation_mark = create_confirmation_mark(
            canvas,
            px=self._px,
            background=style.section_background_color,
            accessible_name="Error details copied",
        )
        self._copy_confirmation = TransientActionConfirmation(
            canvas,
            on_visibility_changed=self._set_copy_confirmation_visible,
        )

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
            width = max(1, int(float(content_width)))
        except (TypeError, ValueError):
            width = self._metrics.content_min_width

        canvas.delete("help-content")
        if active_tab_key == "troubleshooting":
            self._render_troubleshooting(canvas, width)
            return
        y = 0
        for section in self._tab_sections[active_tab_key]:
            y = self._draw_shortcut_card(
                canvas,
                tab_key=active_tab_key,
                section=section,
                top=y,
                width=width,
            )
            y += self._metrics.section_gap_y

        self._content_height = max(0, y - self._metrics.section_gap_y)
        try:
            canvas.configure(scrollregion=(0, 0, width, self._content_height))
        except tk.TclError:
            return
        scrollbar = self._scrollbar
        if scrollbar is not None:
            scrollbar.sync_overflow(self._content_height)

    def _refresh_troubleshooting(self) -> None:
        self._clear_copy_confirmation()
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
        self._copy_feedback = "" if copied else "Couldn’t copy error details."
        self._copy_feedback_is_error = not copied
        confirmation = self._copy_confirmation
        if confirmation is not None:
            was_visible = confirmation.visible
            if copied:
                confirmation.show()
            else:
                confirmation.clear()
                if not was_visible:
                    self._render_table(canvas.winfo_width())
        else:
            self._copy_confirmation_visible = copied
            self._render_table(canvas.winfo_width())

    def _set_copy_confirmation_visible(self, visible: bool) -> None:
        """Render a visibility change requested by the shared confirmation."""

        self._copy_confirmation_visible = visible
        canvas = self._content_canvas
        if canvas is not None:
            try:
                self._render_table(canvas.winfo_width())
            except tk.TclError:
                pass

    def _clear_copy_confirmation(self) -> None:
        """Hide the shared copy confirmation and cancel its pending timer."""

        confirmation = self._copy_confirmation
        if confirmation is not None:
            confirmation.clear()
        else:
            self._copy_confirmation_visible = False

    def on_hidden(self) -> None:
        """Clear copy feedback when the user leaves Help."""
        self._clear_copy_confirmation()
        self._copy_feedback = ""
        self._copy_feedback_is_error = False

    def _render_troubleshooting(self, canvas, width: int) -> None:
        """Render troubleshooting states inside the shared rounded cards."""

        metrics = self._metrics
        state = self._troubleshooting_state
        logs_intro = self._draw_card_intro(
            canvas,
            tab_key="troubleshooting",
            section_id="application-logs",
            top=0,
            width=width,
        )
        y = logs_intro.content_y
        button = self._troubleshooting_button
        if button is not None:
            button.set_enabled(state.can_reveal)
            canvas.create_window(
                logs_intro.content_x,
                y,
                window=button.widget,
                anchor="nw",
                tags="help-content",
            )
            y += metrics.control_height

        if state.status_text:
            y += metrics.card_item_gap_y
            status_item = canvas.create_text(
                logs_intro.content_x,
                y,
                text=state.status_text,
                font=self._canvas_font("detail"),
                fill=(self._style.error_color if state.is_error else self._style.detail_color),
                anchor="nw",
                justify="left",
                width=logs_intro.content_width,
                tags="help-content",
            )
            y += self._canvas_item_height(canvas, status_item, "detail")

        logs_bottom = self._finish_card(
            canvas,
            width=width,
            top=0,
            content_bottom=y,
            intro=logs_intro,
        )
        error_top = logs_bottom + metrics.section_gap_y
        error_intro = self._draw_card_intro(
            canvas,
            tab_key="troubleshooting",
            section_id="last-error",
            top=error_top,
            width=width,
        )
        y = error_intro.content_y
        excerpt = state.error_excerpt
        copy_button = self._copy_error_button
        if copy_button is not None:
            copy_button.set_enabled(excerpt is not None)
        if excerpt is not None:
            text_pad = metrics.error_excerpt_padding
            excerpt_item = canvas.create_text(
                error_intro.content_x + text_pad,
                y + text_pad,
                text=excerpt.text,
                font=self._style.error_font,
                fill=self._style.action_color,
                anchor="nw",
                justify="left",
                width=max(1, error_intro.content_width - (text_pad * 2)),
                tags="help-content",
            )
            excerpt_height = self._canvas_item_height(canvas, excerpt_item, "detail")
            excerpt_bounds = CanvasBounds(
                error_intro.content_x,
                y,
                error_intro.content_x + error_intro.content_width,
                y + excerpt_height + (text_pad * 2),
            )
            draw_rounded_canvas_box(
                canvas,
                excerpt_bounds,
                radius=metrics.control_corner_radius,
                fill=self._style.keycap_background_color,
                outline=self._style.keycap_border_color,
                border_width=metrics.control_border_thickness,
                tags=("help-content", "help-error-excerpt"),
                below=excerpt_item,
            )
            y = excerpt_bounds.bottom + metrics.card_item_gap_y

            if copy_button is not None:
                copy_y = y
                canvas.create_window(
                    error_intro.content_x,
                    copy_y,
                    window=copy_button.widget,
                    anchor="nw",
                    tags="help-content",
                )
                copy_width = metrics.action_min_width
                copy_height = metrics.control_height
                y += copy_height
                confirmation_mark = self._copy_confirmation_mark
                if confirmation_mark is not None and self._copy_confirmation_visible:
                    canvas.create_window(
                        error_intro.content_x
                        + copy_width
                        + self._px(ACTION_CONFIRMATION_GAP),
                        copy_y + (copy_height // 2),
                        window=confirmation_mark,
                        anchor="w",
                        tags="help-content",
                    )
        elif state.error_status_text:
            empty_item = canvas.create_text(
                error_intro.content_x,
                y,
                text=state.error_status_text,
                font=self._canvas_font("detail"),
                fill=(self._style.error_color if state.is_error else self._style.detail_color),
                anchor="nw",
                justify="left",
                width=error_intro.content_width,
                tags="help-content",
            )
            y += self._canvas_item_height(canvas, empty_item, "detail")

        if self._copy_feedback:
            y += metrics.card_item_gap_y
            feedback_item = canvas.create_text(
                error_intro.content_x,
                y,
                text=self._copy_feedback,
                font=self._canvas_font("detail"),
                fill=(
                    self._style.error_color
                    if self._copy_feedback_is_error
                    else self._style.detail_color
                ),
                anchor="nw",
                justify="left",
                width=error_intro.content_width,
                tags="help-content",
            )
            y += self._canvas_item_height(canvas, feedback_item, "detail")

        error_bottom = self._finish_card(
            canvas,
            width=width,
            top=error_top,
            content_bottom=y,
            intro=error_intro,
        )
        self._content_height = max(0, error_bottom + metrics.content_bottom_pad_y)
        try:
            canvas.configure(scrollregion=(0, 0, width, self._content_height))
        except tk.TclError:
            return
        if self._scrollbar is not None:
            self._scrollbar.sync_overflow(self._content_height)

    def _canvas_item_height(self, canvas, item: int, font_role: str) -> int:
        """Return one rendered text height with a semantic-font fallback."""

        bounds = canvas.bbox(item)
        if bounds is None:
            return self._font_line_height(font_role)
        return max(1, bounds[3] - bounds[1])

    def _draw_shortcut_card(
            self,
            canvas,
            *,
            tab_key: str,
            section: KeyboardShortcutSection,
            top: int,
            width: int,
    ) -> int:
        """Draw one measured shortcut card and return its lower edge."""

        metrics = self._metrics
        intro = self._draw_card_intro(
            canvas,
            tab_key=tab_key,
            section_id=section.id,
            top=top,
            width=width,
        )
        y = intro.content_y
        for shortcut_index, shortcut in enumerate(section.shortcuts):
            if shortcut_index:
                y += metrics.card_item_gap_y
            y = self._draw_shortcut_row(
                canvas,
                shortcut,
                y,
                intro.content_x,
                intro.content_width,
            )

        return self._finish_card(
            canvas,
            width=width,
            top=top,
            content_bottom=y,
            intro=intro,
        )

    def _draw_card_intro(
            self,
            canvas,
            *,
            tab_key: str,
            section_id: str,
            top: int,
            width: int,
    ) -> _HelpCardIntro:
        """Draw a card title and purpose line using the shared vertical rhythm."""

        style = self._style
        metrics = self._metrics
        content = help_section_content(tab_key, section_id)
        card_left = self._px(TABBED_CONTENT_ALIGNMENT_INSET)
        initial_geometry = measure_help_card(
            width=width,
            top=top,
            content_bottom=top + metrics.section_padding_y,
            left=card_left,
            metrics=metrics,
        )
        content_x = initial_geometry.content.left
        content_width = initial_geometry.content.width
        y = initial_geometry.content.top
        heading_item = canvas.create_text(
            content_x,
            y,
            text=content.title,
            font=self._canvas_font("section"),
            fill=style.section_color,
            anchor="nw",
            tags="help-content",
        )
        heading_bounds = canvas.bbox(heading_item)
        heading_height = (
            self._font_line_height("section")
            if heading_bounds is None
            else max(1, heading_bounds[3] - heading_bounds[1])
        )
        purpose_y = y + heading_height + metrics.section_heading_to_description_y
        purpose_item = canvas.create_text(
            content_x,
            purpose_y,
            text=content.purpose,
            font=self._canvas_font("section_summary"),
            fill=style.detail_color,
            anchor="nw",
            justify="left",
            width=content_width,
            tags="help-content",
        )
        purpose_bounds = canvas.bbox(purpose_item)
        purpose_height = (
            self._font_line_height("section_summary")
            if purpose_bounds is None
            else max(1, purpose_bounds[3] - purpose_bounds[1])
        )
        return _HelpCardIntro(
            heading_item=heading_item,
            card_left=card_left,
            content_x=content_x,
            content_width=content_width,
            content_y=(
                    purpose_y
                    + purpose_height
                    + metrics.section_description_to_content_y
            ),
        )

    def _finish_card(
            self,
            canvas,
            *,
            width: int,
            top: int,
            content_bottom: int,
            intro: _HelpCardIntro,
    ) -> int:
        """Draw a card behind its measured foreground and return its lower edge."""

        metrics = self._metrics
        geometry = measure_help_card(
            width=width,
            top=top,
            content_bottom=content_bottom,
            left=intro.card_left,
            metrics=metrics,
        )
        draw_rounded_canvas_box(
            canvas,
            geometry.outer,
            radius=metrics.section_corner_radius,
            fill=self._style.section_background_color,
            tags=("help-content", "help-card"),
            below=intro.heading_item,
        )
        return geometry.outer.bottom

    def _draw_shortcut_row(
            self,
            canvas,
            shortcut: KeyboardShortcut,
            y: int,
            content_x: int,
            content_width: int,
    ) -> int:
        style = self._style
        metrics = self._metrics
        row = measure_shortcut_row(
            content_x=content_x,
            content_width=content_width,
            metrics=metrics,
        )
        detail = shortcut.context_note
        primary_font_role = "overview" if detail else "action"
        keycap_height = self._keycap_height(shortcut.shortcut)
        action_y = (
            y + keycap_height + metrics.shortcut_row_pad_y
            if row.stacked
            else y
        )
        action_item = canvas.create_text(
            row.action_x,
            action_y,
            text=shortcut.action,
            font=self._canvas_font(primary_font_role),
            fill=style.action_color,
            anchor="nw",
            justify="left",
            width=row.action_width,
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
            detail_gap = metrics.detail_gap_y
            detail_item = canvas.create_text(
                row.action_x,
                action_y + action_height + detail_gap,
                text=detail,
                font=self._canvas_font("detail"),
                fill=style.detail_color,
                anchor="nw",
                justify="left",
                width=row.action_width,
                tags="help-content",
            )
            detail_bounds = canvas.bbox(detail_item)
            detail_height = (
                self._font_line_height("detail")
                if detail_bounds is None
                else max(1, detail_bounds[3] - detail_bounds[1])
            )
            content_height += detail_gap + detail_height
        self._draw_keycap_sequence(
            canvas,
            x=row.keycap_x,
            y=(
                y
                if row.stacked
                else y + max(0, (content_height - keycap_height) // 2)
            ),
            shortcut=shortcut.shortcut,
        )
        return (
            action_y + content_height
            if row.stacked
            else y + max(content_height, keycap_height)
        )

    def _keycap_height(self, shortcut: str) -> int:
        if not shortcut_keycap_parts(shortcut):
            return self._font_line_height("action")
        return (
                self._font_line_height("keycap")
                + (self._metrics.keycap_content_pad_y * 2)
                + (self._metrics.control_border_thickness * 2)
        )

    def _keycap_width(self, part: str) -> int:
        """Return a fixed unit span or a natural descriptive-key width."""
        unit_count = shortcut_keycap_unit_count(part)
        if unit_count is not None:
            return self._keycap_span_width(unit_count)
        return (
                self._font_width("keycap", part)
                + self._metrics.control_content_pad_x
                + (self._metrics.control_border_thickness * 2)
        )

    def _keycap_span_width(self, unit_count: int) -> int:
        """Return ``unit_count`` 1u caps plus their intervening gaps."""
        unit_width = (
                self._font_width("keycap", "W")
                + self._metrics.control_content_pad_x
                + (self._metrics.control_border_thickness * 2)
        )
        return (
                unit_width * unit_count
                + self._metrics.keycap_sequence_gap_x * (unit_count - 1)
        )

    def _keycap_separator_width(self, part: str = "+") -> int:
        """Return the width of one borderless 1u separator cell."""
        del part
        return self._keycap_span_width(1)

    def _draw_keycap_sequence(self, canvas, *, x: int, y: int, shortcut: str) -> None:
        style = self._style
        keycap_height = self._keycap_height(shortcut)
        sequence_gap = self._metrics.keycap_sequence_gap_x
        cursor = x
        parts = shortcut_keycap_parts(shortcut)
        for index, part in enumerate(parts):
            if part in {"+", "/"}:
                separator_width = self._keycap_separator_width(part)
                canvas.create_text(
                    cursor + (separator_width / 2),
                    y + keycap_height / 2,
                    text=part,
                    font=self._canvas_font("action"),
                    fill=style.section_color,
                    anchor="center",
                    tags="help-content",
                )
                cursor += separator_width
                if index + 1 < len(parts):
                    cursor += sequence_gap
                continue
            keycap_width = self._keycap_width(part)
            draw_rounded_canvas_box(
                canvas,
                CanvasBounds(
                    cursor,
                    y,
                    cursor + keycap_width,
                    y + keycap_height,
                ),
                radius=self._metrics.control_corner_radius,
                fill=style.keycap_background_color,
                outline=style.keycap_border_color,
                border_width=self._metrics.control_border_thickness,
                tags=("help-content", "help-keycap"),
            )
            canvas.create_text(
                cursor + (keycap_width / 2),
                y + keycap_height / 2,
                text=part,
                font=self._canvas_font("keycap"),
                fill=style.keycap_text_color,
                anchor="center",
                tags="help-content",
            )
            cursor += keycap_width
            if index + 1 < len(parts):
                cursor += sequence_gap

    def _on_canvas_configure(self, event) -> None:
        self._render_table(event.width)
