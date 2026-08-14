"""Tk presentation surface for the splash Map Library panel."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Iterable

from caveviewer.gui.tk_scrolling import vertical_scroll_units


@dataclass(frozen=True)
class MapLibraryMenuAction:
    """One enabled or explanatory-disabled entry in a row overflow menu."""

    label: str
    action: Callable[[], None] | None = None
    explanation: str | None = None

    @property
    def enabled(self) -> bool:
        return self.action is not None


MenuAction = tuple[str, Callable[[], None]] | MapLibraryMenuAction
MenuActionsFactory = Callable[["MapLibraryRowWidgets"], Iterable[MenuAction]]


@dataclass(frozen=True)
class MapLibraryActionVisual:
    """Presentation-only icon and interaction metadata for one row action."""

    icon: str
    tooltip: str
    row_activates: bool = False


def map_library_action_visual(
    action_text: str,
    *,
    show_stop_progress: bool = False,
    show_pause_progress: bool = False,
) -> MapLibraryActionVisual:
    """Map the workflow's stable action labels to compact row visuals."""
    if show_stop_progress:
        return MapLibraryActionVisual("stop-progress", "Stop download")
    if show_pause_progress:
        return MapLibraryActionVisual("pause-progress", "Pause rebuild")
    if action_text == "Open":
        return MapLibraryActionVisual("chevron-right", "Open map", True)
    if action_text == "Get":
        return MapLibraryActionVisual("download", "Download map")
    if action_text == "Retry":
        return MapLibraryActionVisual("retry", "Retry download")
    return MapLibraryActionVisual("none", "")


@dataclass(frozen=True)
class MapLibraryPanelStyle:
    """Theme and layout tokens used by the splash Map Library panel."""

    panel_color: str
    panel_border_color: str
    title_color: str
    former_map_title_color: str
    instruction_color: str
    title_font: tuple
    body_font: tuple
    supporting_font: tuple
    section_font: tuple
    button_bg: str
    button_fg: str
    button_hover_bg: str
    button_border_color: str
    disabled_button_bg: str
    disabled_button_fg: str
    disabled_button_border: str
    empty_note_color: str
    metadata_color: str
    metadata_error_color: str
    metadata_status_color: str
    metadata_status_duration_ms: int
    metadata_error_duration_ms: int
    progress_track_color: str
    progress_fill_color: str
    action_progress_ring_diameter: int
    action_progress_ring_stroke_width: int
    action_stop_size: int
    action_button_size: int
    action_icon_stroke_width: int
    overflow_button_size: int
    overflow_fg: str
    overflow_hover_fg: str
    overflow_hover_bg: str
    menu_bg: str
    menu_border: str
    menu_hover_bg: str
    menu_text: str


@dataclass(frozen=True)
class MapLibraryRowWidgets:
    """Tk widgets owned by one map-library row on the splash thread."""

    row_shell: object
    overflow_button: object
    action_button: object
    title_label: object
    metadata_label: object | None


@dataclass
class MapLibrarySectionWidgets:
    """Tk widgets and expansion state for one collapsible map group."""

    header: object
    content: object
    title: str
    # Start expanded on every splash so first-time users can discover the
    # CaveViewer Maps catalog without needing to uncover it first.
    expanded: bool = True


class MapLibraryPanel:
    """
    Own Tk widgets and widget mutations for the splash Map Library panel.

    The splash screen supplies row models and callbacks. This class keeps Tk
    layout, overflow menu behavior, scroll behavior, metadata/status mutation,
    and progress rendering in one presentation-only component.
    """

    def __init__(
        self,
        root,
        *,
        px: Callable[[int | float], int],
        bind_activation: Callable[[object, Callable[[], None]], None],
        widget_exists: Callable[[object | None], bool],
        logger,
        style: MapLibraryPanelStyle,
        open_map_folder: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
        self._px = px
        self._bind_activation = bind_activation
        self._widget_exists = widget_exists
        self._log = logger
        self._style = style
        self._open_map_folder = open_map_folder
        self.standard_rows: dict[object, MapLibraryRowWidgets] = {}
        self._standard_row_former: dict[object, bool] = {}
        self.recent_rows: dict[str, MapLibraryRowWidgets] = {}
        self._recent_section: MapLibrarySectionWidgets | None = None
        self._recent_container = None
        self._recent_empty_note = None
        self._standard_section: MapLibrarySectionWidgets | None = None
        self._standard_container = None
        self._rows_frame = None
        self._content_canvas = None
        self._rows_window = None
        self._scroll_hint = None
        self._content_overflows = False
        self._active_menu = None
        self._active_menu_root_bindings: list[tuple[str, str]] = []

    def create(self, parent) -> None:
        """Create the scrollable panel shell and section containers."""
        style = self._style
        panel = tk.Frame(
            parent,
            bg=style.panel_color,
            highlightthickness=1,
            highlightbackground=style.panel_border_color,
            highlightcolor=style.panel_border_color,
        )
        # Keep a modest outer margin so the library can use more of the
        # available splash height without losing its visual separation.
        panel.pack(fill="both", expand=True, pady=self._px(14))
        panel.grid_columnconfigure(0, weight=1)

        scroll_row = 0
        if self._open_map_folder is not None:
            open_map_shell = tk.Frame(panel, bg=style.panel_color)
            open_map_shell.grid(
                row=0,
                column=0,
                sticky="ew",
                padx=self._px(12),
                pady=(self._px(12), self._px(2)),
            )
            self._create_open_map_action(open_map_shell)
            scroll_row = 1
        panel.grid_rowconfigure(scroll_row, weight=1)

        scroll_shell = tk.Frame(panel, bg=style.panel_color)
        scroll_shell.grid(
            row=scroll_row,
            column=0,
            sticky="nsew",
            padx=self._px(12),
            pady=(0, self._px(4)),
        )
        self._scroll_hint = tk.Label(
            panel,
            text="Scroll to browse more maps ↓",
            font=style.supporting_font,
            fg=style.metadata_color,
            bg=style.panel_color,
            anchor="center",
        )
        self._scroll_hint.grid(
            row=scroll_row + 1,
            column=0,
            sticky="ew",
            pady=(0, self._px(10)),
        )
        self._scroll_hint.grid_remove()

        self._content_canvas = tk.Canvas(
            scroll_shell,
            bg=style.panel_color,
            borderwidth=0,
            highlightthickness=0,
        )
        self._content_canvas.pack(side="left", fill="both", expand=True)

        self._rows_frame = tk.Frame(self._content_canvas, bg=style.panel_color)
        self._rows_window = self._content_canvas.create_window(
            (0, 0),
            window=self._rows_frame,
            anchor="nw",
        )

        self._content_canvas.bind(
            "<Configure>",
            self._resize_canvas_window,
            add="+",
        )
        self._content_canvas.bind("<MouseWheel>", self._scroll_content, add="+")
        self._content_canvas.bind("<Button-4>", self._scroll_content, add="+")
        self._content_canvas.bind("<Button-5>", self._scroll_content, add="+")

        self._recent_section = self._create_section(
            self._rows_frame,
            "Your Recent Maps",
            top_pad=16,
        )
        self._recent_container = self._recent_section.content
        self._standard_section = self._create_section(
            self._rows_frame,
            "CaveViewer Maps",
        )
        self._standard_container = self._standard_section.content

    def finish_population(self) -> None:
        """Bind mousewheel events after rows exist and schedule scroll sync."""
        if self._rows_frame is None:
            return
        self.bind_mousewheel_if_ready(self._rows_frame)
        self.root.after_idle(self.sync_scroll_region)

    def close_active_menu(self) -> None:
        """Close the transient overflow menu if it is currently open."""
        menu = self._active_menu
        self._active_menu = None
        bindings = getattr(self, "_active_menu_root_bindings", ())
        self._active_menu_root_bindings = []
        for sequence, callback_id in bindings:
            try:
                self.root.unbind(sequence, callback_id)
            except (tk.TclError, AttributeError):
                pass
        if self._widget_exists(menu):
            try:
                menu.destroy()
            except tk.TclError:
                pass
        for widgets in (*self.recent_rows.values(), *self.standard_rows.values()):
            self._hide_row_action_tooltips(widgets)

    def add_recent_row(
        self,
        entry,
        *,
        action: Callable[[], None],
        menu_actions_factory: MenuActionsFactory | None = None,
    ) -> MapLibraryRowWidgets:
        """Append one recent-map row to the Your Recent Maps section."""
        if self._recent_container is None:
            raise RuntimeError("MapLibraryPanel.create() must run first")
        if self._widget_exists(self._recent_empty_note):
            self._recent_empty_note.destroy()
        self._recent_empty_note = None
        widgets = self._create_row(
            self._recent_container,
            title=entry.title,
            detail=entry.detail,
            size_text="",
            action_text="Open",
            action=action,
            reserve_metadata=True,
            menu_actions_factory=menu_actions_factory,
        )
        self.recent_rows[entry.key] = widgets
        self.bind_mousewheel_if_ready(widgets.row_shell)
        self.sync_after_row_change()
        return widgets

    def ensure_recent_empty_note(self) -> None:
        """Show the empty Your Recent Maps note when no recent rows remain."""
        if self._recent_container is None:
            return
        if self.recent_rows:
            return
        if self._widget_exists(self._recent_empty_note):
            return
        self._recent_empty_note = self._create_empty_note(
            self._recent_container,
            "No maps added yet.",
            bottom_pad=18,
        )
        self.bind_mousewheel_if_ready(self._recent_empty_note)

    def remove_recent_row(self, key: str) -> None:
        """Remove a recent row and restore the empty note if needed."""
        row_widgets = self.recent_rows.pop(key, None)
        if row_widgets is not None and self._widget_exists(row_widgets.row_shell):
            self._hide_row_action_tooltips(row_widgets)
            row_widgets.row_shell.destroy()
        self.ensure_recent_empty_note()
        self.sync_after_row_change()

    def add_standard_row(
        self,
        row,
        *,
        action: Callable[[], None],
        former: bool = False,
        menu_actions_factory: MenuActionsFactory | None = None,
    ) -> MapLibraryRowWidgets:
        """Append one current or former map to the CaveViewer Maps section."""
        if self._standard_container is None:
            raise RuntimeError("MapLibraryPanel.create() must run first")
        widgets = self._create_row(
            self._standard_container,
            title=row.title,
            detail=row.detail,
            size_text="",
            action_text=row.action_text,
            action=action,
            title_color=(
                self._style.former_map_title_color if former else self._style.title_color
            ),
            reserve_metadata=True,
            menu_actions_factory=menu_actions_factory,
        )
        self.standard_rows[row.key] = widgets
        self._standard_row_former[row.key] = former
        self.bind_mousewheel_if_ready(widgets.row_shell)
        self.sync_after_row_change()
        return widgets

    def has_standard_row(self, key: object) -> bool:
        """Return whether a standard-library row currently exists."""
        return key in self.standard_rows

    def is_standard_row_former(self, key: object) -> bool:
        """Return whether the row is styled as a former standard-library map."""
        return self._standard_row_former.get(key, False)

    def set_standard_row_former(self, key: object, former: bool) -> None:
        """Update one standard row's muted former-map title treatment."""
        widgets = self.standard_rows.get(key)
        if widgets is None:
            return
        self._standard_row_former[key] = bool(former)
        if self._widget_exists(widgets.title_label):
            widgets.title_label.config(
                fg=(
                    self._style.former_map_title_color
                    if former
                    else self._style.title_color
                )
            )

    def remove_standard_row(self, key: object) -> None:
        """Remove a standard-library row when refreshed catalog metadata drops it."""
        row_widgets = self.standard_rows.pop(key, None)
        self._standard_row_former.pop(key, None)
        if row_widgets is not None and self._widget_exists(row_widgets.row_shell):
            self._hide_row_action_tooltips(row_widgets)
            row_widgets.row_shell.destroy()
        self.sync_after_row_change()

    def set_standard_row_metadata(
        self,
        key: object,
        text: str,
        *,
        error: bool = False,
    ) -> None:
        """Set the stable metadata text for one standard-library row."""
        widgets = self.standard_rows.get(key)
        self.set_row_metadata(widgets, text, error=error)

    def set_row_metadata(
        self,
        row_widgets: MapLibraryRowWidgets | None,
        text: str,
        *,
        error: bool = False,
    ) -> bool:
        """Set stable metadata for either a recent or standard-library row."""
        widgets = row_widgets
        if widgets is None or not self._widget_exists(widgets.metadata_label):
            return False
        self._set_metadata_label_base(widgets.metadata_label, text, error=error)
        return True

    def show_row_status(
        self,
        row_widgets: MapLibraryRowWidgets | None,
        text: str,
        *,
        error: bool = False,
    ) -> bool:
        """Temporarily replace a row metadata label with status text."""
        if row_widgets is None or not self._widget_exists(row_widgets.metadata_label):
            return False

        label = row_widgets.metadata_label
        self._cancel_row_status(label)
        label.config(
            text=text,
            fg=(
                self._style.metadata_error_color
                if error
                else self._style.metadata_status_color
            ),
        )

        def restore_metadata() -> None:
            label._cv_status_after_id = None
            if not self._widget_exists(label):
                return
            label.config(
                text=getattr(label, "_cv_base_text", ""),
                fg=getattr(label, "_cv_base_fg", self._style.metadata_color),
            )

        label._cv_status_after_id = self.root.after(
            (
                self._style.metadata_error_duration_ms
                if error
                else self._style.metadata_status_duration_ms
            ),
            restore_metadata,
        )
        return True

    def show_standard_row_status(
        self,
        key: object,
        text: str,
        *,
        error: bool = False,
    ) -> bool:
        """Temporarily replace one standard row's stable metadata text."""
        return self.show_row_status(
            self.standard_rows.get(key),
            text,
            error=error,
        )

    def set_standard_row_action(
        self,
        key: object,
        text: str,
        command: Callable[[], None],
        *,
        enabled: bool = True,
        show_stop_progress: bool = False,
        show_pause_progress: bool = False,
    ) -> bool:
        """Update the primary action button for one standard-library row."""
        widgets = self.standard_rows.get(key)
        return self.set_row_action(
            widgets,
            text,
            command,
            enabled=enabled,
            show_stop_progress=show_stop_progress,
            show_pause_progress=show_pause_progress,
        )

    def set_row_action(
        self,
        row_widgets: MapLibraryRowWidgets | None,
        text: str,
        command: Callable[[], None],
        *,
        enabled: bool = True,
        show_stop_progress: bool = False,
        show_pause_progress: bool = False,
    ) -> bool:
        """Update the primary action for either a recent or standard row."""
        widgets = row_widgets
        if widgets is None or not self._widget_exists(widgets.action_button):
            return False
        self._set_action_button(
            widgets.action_button,
            text,
            command,
            enabled=enabled,
            show_stop_progress=show_stop_progress,
            show_pause_progress=show_pause_progress,
        )
        return True

    def refresh_standard_row_overflow(self, key: object) -> None:
        """Refresh a standard-library row overflow button after state changes."""
        widgets = self.standard_rows.get(key)
        if widgets is not None:
            self.refresh_row_overflow(widgets)

    def refresh_row_overflow(self, row_widgets: MapLibraryRowWidgets | None) -> None:
        """Refresh one overflow button after its action availability changes."""
        if row_widgets is None:
            return
        if self._widget_exists(row_widgets.overflow_button):
            self._refresh_overflow_button(row_widgets.overflow_button)

    def reset_standard_progress(self, key: object) -> None:
        """Return a standard-library row action button to text mode."""
        widgets = self.standard_rows.get(key)
        if widgets is None or not self._widget_exists(widgets.action_button):
            return
        widgets.action_button._cv_show_stop_progress = False
        widgets.action_button._cv_show_pause_progress = False
        widgets.action_button._cv_progress_fraction = 0.0
        self._draw_action_button(widgets.action_button)

    def show_standard_progress(self, key: object) -> None:
        """Show the stop/progress affordance for an active download row."""
        widgets = self.standard_rows.get(key)
        if widgets is None or not self._widget_exists(widgets.action_button):
            return
        widgets.action_button._cv_show_stop_progress = True
        widgets.action_button._cv_show_pause_progress = False
        widgets.action_button._cv_progress_fraction = 0.0
        self._draw_action_button(widgets.action_button)
        self.root.update_idletasks()

    def apply_standard_progress(
        self,
        key: object,
        downloaded_bytes: int,
        total_bytes: int | None,
    ) -> None:
        """Apply download progress to one standard-library row."""
        widgets = self.standard_rows.get(key)
        if widgets is None or not self._widget_exists(widgets.action_button):
            return
        if total_bytes is None or total_bytes <= 0:
            self.set_standard_row_metadata(key, "Downloading…")
            widgets.action_button._cv_progress_fraction = None
            self._draw_action_button(widgets.action_button)
            return
        fraction = min(1.0, downloaded_bytes / total_bytes)
        self.set_standard_row_metadata(key, "Downloading…")
        widgets.action_button._cv_progress_fraction = fraction
        self._draw_action_button(widgets.action_button)

    def set_row_progress(
        self,
        row_widgets: MapLibraryRowWidgets | None,
        fraction: float,
    ) -> None:
        """Update an inline action-progress ring for either row kind."""
        if row_widgets is None or not self._widget_exists(row_widgets.action_button):
            return
        row_widgets.action_button._cv_progress_fraction = min(
            1.0,
            max(0.0, float(fraction)),
        )
        self._draw_action_button(row_widgets.action_button)

    def sync_after_row_change(self) -> None:
        """Schedule a scroll-region refresh after row insertion/removal."""
        if self._widget_exists(self.root):
            self.root.after_idle(self.sync_scroll_region)

    def bind_mousewheel_if_ready(self, widget) -> None:
        """Bind recursive mousewheel handlers when panel scrolling is ready."""
        if self._widget_exists(widget):
            self._bind_mousewheel(widget)

    def sync_scroll_region(self) -> None:
        """Synchronize the scroll region and its overflow guidance."""
        if self._content_canvas is None or self._rows_frame is None:
            return
        width = max(1, self._content_canvas.winfo_width())
        content_height = self._rows_frame.winfo_reqheight()
        self._content_canvas.configure(scrollregion=(0, 0, width, content_height))
        visible_height = self._content_canvas.winfo_height()
        content_overflows = content_height > visible_height + 1
        self._set_scroll_hint_visible(content_overflows)
        if not content_overflows:
            self._content_canvas.yview_moveto(0)

    def _set_scroll_hint_visible(self, visible: bool) -> None:
        """Show a quiet scroll cue only while rows extend below the viewport."""
        self._content_overflows = bool(visible)
        hint = self._scroll_hint
        if not self._widget_exists(hint):
            return
        try:
            mapped = bool(hint.winfo_manager())
        except tk.TclError:
            return
        if visible and not mapped:
            hint.grid()
        elif not visible and mapped:
            hint.grid_remove()

    def focus_content(self) -> None:
        """Return the library to its beginning and give it keyboard focus."""
        self.close_active_menu()
        canvas = self._content_canvas
        if not self._widget_exists(canvas):
            return
        try:
            canvas.yview_moveto(0)
            canvas.focus_set()
        except tk.TclError:
            return

    def _create_open_map_action(self, parent) -> None:
        """Create the featured entry point for opening a local map folder."""
        callback = self._open_map_folder
        if callback is None:
            return

        style = self._style
        action = tk.Canvas(
            parent,
            height=self._px(58),
            bg=style.button_hover_bg,
            borderwidth=0,
            cursor="hand2",
            takefocus=True,
            highlightthickness=1,
            highlightbackground=style.panel_border_color,
            highlightcolor=style.button_border_color,
        )
        action._cv_open_map_hovered = False

        def activate() -> None:
            self.close_active_menu()
            callback()

        def set_hovered(hovered: bool) -> None:
            action._cv_open_map_hovered = hovered
            action.config(
                bg=(style.menu_hover_bg if hovered else style.button_hover_bg)
            )
            self._draw_open_map_action(action)

        self._bind_activation(action, activate)
        action.bind("<Enter>", lambda _event: set_hovered(True))
        action.bind("<Leave>", lambda _event: set_hovered(False))
        action.bind(
            "<Configure>",
            lambda _event, target=action: self._draw_open_map_action(target),
            add="+",
        )
        action.pack(anchor="w", fill="x")
        self.bind_mousewheel_if_ready(action)

    def _draw_open_map_action(self, action) -> None:
        """Draw the folder command card without relying on font glyphs."""
        if not self._widget_exists(action):
            return
        action.delete("cv_open_map_action")
        width = max(1, action.winfo_width())
        height = max(1, action.winfo_height())
        style = self._style
        accent_width = max(2, self._px(3))
        stroke_width = max(1, self._px(2))
        icon_left = self._px(22)
        icon_top = max(self._px(8), height / 2 - self._px(11))
        icon_right = icon_left + self._px(28)
        icon_bottom = icon_top + self._px(22)
        icon_tab_right = icon_left + self._px(15)
        text_left = icon_right + self._px(16)
        title_y = height / 2 - self._px(8)
        subtitle_y = height / 2 + self._px(11)
        chevron_x = width - self._px(22)
        chevron_size = max(3, self._px(5))

        action.create_rectangle(
            0,
            0,
            accent_width,
            height,
            fill=style.progress_fill_color,
            outline="",
            tags="cv_open_map_action",
        )
        action.create_line(
            icon_left,
            icon_top + self._px(5),
            icon_left + self._px(8),
            icon_top + self._px(5),
            icon_left + self._px(11),
            icon_top,
            icon_tab_right,
            icon_top,
            icon_right,
            icon_top + self._px(5),
            icon_right,
            icon_bottom,
            icon_left,
            icon_bottom,
            icon_left,
            icon_top + self._px(5),
            fill=style.title_color,
            width=stroke_width,
            capstyle="round",
            joinstyle="round",
            tags="cv_open_map_action",
        )
        action.create_text(
            text_left,
            title_y,
            text="Open a local map",
            font=style.title_font,
            fill=style.title_color,
            anchor="w",
            tags="cv_open_map_action",
        )
        action.create_text(
            text_left,
            subtitle_y,
            text="Browse a cave map folder",
            font=style.supporting_font,
            fill=style.metadata_color,
            anchor="w",
            tags="cv_open_map_action",
        )
        action.create_line(
            chevron_x - chevron_size,
            height / 2 - chevron_size,
            chevron_x,
            height / 2,
            chevron_x - chevron_size,
            height / 2 + chevron_size,
            fill=style.progress_fill_color,
            width=stroke_width,
            capstyle="round",
            joinstyle="round",
            tags="cv_open_map_action",
        )

    def _create_section(
        self,
        parent,
        text: str,
        *,
        top_pad: int = 10,
    ) -> MapLibrarySectionWidgets:
        """Create an expanded, keyboard-accessible disclosure header and body."""
        header = tk.Canvas(
            parent,
            bg=self._style.panel_color,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=self._style.panel_color,
            highlightcolor=self._style.button_border_color,
            height=max(1, self._px(24)),
            cursor="hand2",
            takefocus=True,
        )
        header.pack(fill="x", pady=(self._px(top_pad), self._px(6)))
        content = tk.Frame(parent, bg=self._style.panel_color)
        content.pack(fill="x")
        section = MapLibrarySectionWidgets(
            header=header,
            content=content,
            title=text,
        )
        self._bind_activation(
            header,
            lambda target=section: self._toggle_section(target),
        )
        header.bind(
            "<Configure>",
            lambda _event, target=section: self._draw_section_header(target),
            add="+",
        )
        self._draw_section_header(section)
        return section

    def _toggle_section(self, section: MapLibrarySectionWidgets) -> None:
        """Show or hide a section while preserving its place in the row order."""
        if not (
            self._widget_exists(section.header)
            and self._widget_exists(section.content)
        ):
            return
        self.close_active_menu()
        section.expanded = not section.expanded
        if section.expanded:
            section.content.pack(fill="x", after=section.header)
        else:
            section.content.pack_forget()
        self._draw_section_header(section)
        self.sync_after_row_change()

    def _draw_section_header(self, section: MapLibrarySectionWidgets) -> None:
        """Draw a section title with an adjacent disclosure triangle."""
        header = section.header
        if not self._widget_exists(header):
            return
        header.delete("cv_section_header")
        height = max(1, header.winfo_height())
        title_item = header.create_text(
            self._px(2),
            height / 2,
            text=section.title,
            font=self._style.section_font,
            fill=self._style.instruction_color,
            anchor="w",
            tags="cv_section_header",
        )
        try:
            title_bounds = header.bbox(title_item)
        except (AttributeError, tk.TclError):
            title_bounds = None
        title_right = (
            title_bounds[2]
            if title_bounds is not None
            else self._px(2) + self._px(120)
        )
        triangle_center_x = title_right + self._px(10)
        triangle_center_y = height / 2
        triangle_size = max(2, self._px(3))
        if section.expanded:
            points = (
                triangle_center_x - triangle_size,
                triangle_center_y - triangle_size / 2,
                triangle_center_x + triangle_size,
                triangle_center_y - triangle_size / 2,
                triangle_center_x,
                triangle_center_y + triangle_size / 2,
            )
        else:
            points = (
                triangle_center_x - triangle_size / 2,
                triangle_center_y - triangle_size,
                triangle_center_x - triangle_size / 2,
                triangle_center_y + triangle_size,
                triangle_center_x + triangle_size / 2,
                triangle_center_y,
            )
        header.create_polygon(
            *points,
            fill=self._style.instruction_color,
            outline="",
            tags="cv_section_header",
        )

    def _create_empty_note(
        self,
        parent,
        text: str,
        *,
        bottom_pad: int = 8,
    ) -> tk.Label:
        label = tk.Label(
            parent,
            text=text,
            font=self._style.supporting_font,
            fg=self._style.empty_note_color,
            bg=self._style.panel_color,
            anchor="w",
            justify="left",
        )
        label.pack(anchor="w", fill="x", pady=(0, self._px(bottom_pad)))
        return label

    def _create_row(
        self,
        parent,
        *,
        title: str,
        detail: str,
        size_text: str,
        action_text: str,
        action: Callable[[], None],
        title_color: str | None = None,
        reserve_metadata: bool = False,
        menu_actions_factory: MenuActionsFactory | None = None,
    ) -> MapLibraryRowWidgets:
        style = self._style
        row_shell = tk.Frame(
            parent,
            bg=style.panel_color,
            highlightthickness=0,
        )
        row_shell.pack(fill="x", pady=(0, self._px(12)))

        row_content = tk.Frame(row_shell, bg=style.panel_color)
        row_content.pack(fill="x")

        row_holder: list[MapLibraryRowWidgets | None] = [None]
        button_factory = None
        if menu_actions_factory is not None:

            def button_factory() -> Iterable[MenuAction]:
                row_widgets = row_holder[0]
                if row_widgets is None:
                    return ()
                return menu_actions_factory(row_widgets)

        overflow_button = self._create_overflow_button(
            row_content,
            button_factory,
        )

        text_column = tk.Frame(row_content, bg=style.panel_color)
        text_column.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(0, self._px(8)),
            pady=self._px(5),
        )

        name_label = tk.Label(
            text_column,
            text=title,
            font=style.title_font,
            fg=title_color or style.title_color,
            bg=style.panel_color,
            anchor="w",
            justify="left",
        )
        name_label.pack(anchor="w", fill="x")
        text_column.bind(
            "<Configure>",
            lambda event, target=name_label: self._sync_row_title_wraplength(
                target,
                event.width,
            ),
            add="+",
        )

        metadata_text = detail or size_text
        metadata_label = None
        if metadata_text or reserve_metadata:
            metadata_label = tk.Label(
                text_column,
                text=metadata_text,
                font=style.supporting_font,
                fg=style.metadata_color,
                bg=style.panel_color,
                anchor="w",
                justify="left",
            )
            metadata_label.pack(anchor="w", fill="x", pady=(self._px(2), 0))
            metadata_label._cv_base_text = metadata_text
            metadata_label._cv_base_fg = style.metadata_color
            metadata_label._cv_status_after_id = None

        action_button = self._create_action_button(
            row_content,
            text=action_text,
            action=action,
        )
        self._configure_action_button_hover(action_button)
        action_button.pack(side="right", padx=(0, self._px(4)), pady=self._px(5))

        row_action_widgets = [row_content, text_column, name_label]
        if metadata_label is not None:
            row_action_widgets.append(metadata_label)
        action_button._cv_row_action_widgets = tuple(row_action_widgets)
        self._set_row_open_activation(action_button)

        row_widgets = MapLibraryRowWidgets(
            row_shell=row_shell,
            overflow_button=overflow_button,
            action_button=action_button,
            title_label=name_label,
            metadata_label=metadata_label,
        )
        row_holder[0] = row_widgets
        self.refresh_row_overflow(row_widgets)
        return row_widgets

    def _sync_row_title_wraplength(self, title_label, available_width: int) -> None:
        """Wrap a map title only at the width its row can really provide."""
        if not self._widget_exists(title_label):
            return
        try:
            wraplength = max(1, int(available_width))
        except (TypeError, ValueError):
            return
        if getattr(title_label, "_cv_title_wraplength", None) == wraplength:
            return
        title_label._cv_title_wraplength = wraplength
        title_label.configure(wraplength=wraplength)
        # A title may gain or lose a line after a window resize. Refresh the
        # canvas region after Tk has recalculated the row's natural height.
        self.sync_after_row_change()

    def _cancel_row_status(self, metadata_label) -> None:
        after_id = getattr(metadata_label, "_cv_status_after_id", None)
        if after_id is None:
            return
        metadata_label._cv_status_after_id = None
        try:
            self.root.after_cancel(after_id)
        except tk.TclError:
            pass

    def _set_metadata_label_base(
        self,
        metadata_label,
        text: str,
        *,
        error: bool = False,
    ) -> None:
        self._cancel_row_status(metadata_label)
        fg = (
            self._style.metadata_error_color
            if error
            else self._style.metadata_color
        )
        metadata_label._cv_base_text = text
        metadata_label._cv_base_fg = fg
        metadata_label.config(text=text, fg=fg)

    def _create_action_button(
        self,
        parent,
        *,
        text: str,
        action: Callable[[], None],
    ) -> tk.Canvas:
        """Create the compact icon control used by one map-library row."""
        style = self._style
        button_width, button_height = self._action_button_pixel_size()
        button = tk.Canvas(
            parent,
            width=button_width,
            height=button_height,
            bg=style.button_bg,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=style.panel_color,
            highlightcolor=style.button_border_color,
            cursor="hand2",
            takefocus=True,
        )
        button._cv_enabled = True
        button._cv_action_text = text
        button._cv_show_stop_progress = False
        button._cv_show_pause_progress = False
        button._cv_progress_fraction = 0.0
        button._cv_action_visual = map_library_action_visual(text)
        button._cv_row_action_widgets = ()
        button._cv_tooltip_after_id = None
        button._cv_tooltip = None
        self._set_action_button(button, text, action)
        button.bind(
            "<Configure>",
            lambda _event, target=button: self._draw_action_button(target),
            add="+",
        )
        return button

    def _action_button_pixel_size(self) -> tuple[int, int]:
        """Return the fixed square hit target shared by all row actions."""
        size = max(1, self._px(self._style.action_button_size))
        return size, size

    def _set_action_button_style(self, button, *, hovered: bool = False) -> None:
        enabled = getattr(button, "_cv_enabled", True)
        style = self._style
        bg = (
            style.button_hover_bg
            if enabled and hovered
            else style.button_bg
            if enabled
            else style.disabled_button_bg
        )
        border = style.button_border_color if enabled else style.disabled_button_border
        button.config(
            bg=bg,
            cursor="hand2" if enabled else "arrow",
            takefocus=enabled,
            highlightbackground=style.panel_color,
            highlightcolor=border,
        )

    def _draw_action_button(self, button) -> None:
        """Redraw the state-specific icon inside one compact row control."""
        if not self._widget_exists(button):
            return

        button.delete("cv_action_content")
        width = self._canvas_dimension(button, "width")
        height = self._canvas_dimension(button, "height")
        visual = getattr(button, "_cv_action_visual", None)
        icon = getattr(visual, "icon", "none")
        if icon == "stop-progress":
            self._draw_action_stop_progress(button, width, height)
            return
        if icon == "pause-progress":
            self._draw_action_pause_progress(button, width, height)
            return
        color = (
            self._style.button_fg
            if getattr(button, "_cv_enabled", True)
            else self._style.disabled_button_fg
        )
        if icon == "chevron-right":
            self._draw_chevron_right(button, width, height, color)
        elif icon == "download":
            self._draw_download(button, width, height, color)
        elif icon == "retry":
            self._draw_retry(button, width, height, color)

    def _draw_chevron_right(self, button, width: int, height: int, color: str) -> None:
        inset = max(2, self._px(7))
        button.create_line(
            width / 2 - inset / 3,
            height / 2 - inset / 2,
            width / 2 + inset / 3,
            height / 2,
            width / 2 - inset / 3,
            height / 2 + inset / 2,
            fill=color,
            width=max(1, self._px(self._style.action_icon_stroke_width)),
            capstyle="round",
            joinstyle="round",
            tags="cv_action_content",
        )

    def _draw_download(self, button, width: int, height: int, color: str) -> None:
        """Draw a quiet download arrow with an open tray, not a boxed button."""
        stroke_width = max(1, self._px(self._style.action_icon_stroke_width))
        center_x = width / 2
        center_y = height / 2
        button.create_line(
            center_x,
            center_y - self._px(8),
            center_x,
            center_y + self._px(2),
            fill=color,
            width=stroke_width,
            capstyle="round",
            tags="cv_action_content",
        )
        button.create_line(
            center_x - self._px(4),
            center_y - self._px(2),
            center_x,
            center_y + self._px(2),
            center_x + self._px(4),
            center_y - self._px(2),
            fill=color,
            width=stroke_width,
            capstyle="round",
            joinstyle="round",
            tags="cv_action_content",
        )
        button.create_line(
            center_x - self._px(7),
            center_y + self._px(6),
            center_x - self._px(7),
            center_y + self._px(9),
            center_x + self._px(7),
            center_y + self._px(9),
            center_x + self._px(7),
            center_y + self._px(6),
            fill=color,
            width=stroke_width,
            capstyle="round",
            tags="cv_action_content",
        )

    def _draw_retry(self, button, width: int, height: int, color: str) -> None:
        stroke_width = max(1, self._px(self._style.action_icon_stroke_width))
        radius = max(2, self._px(7))
        center_x = width / 2
        center_y = height / 2
        button.create_arc(
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
            start=38,
            extent=282,
            style="arc",
            outline=color,
            width=stroke_width,
            tags="cv_action_content",
        )
        button.create_line(
            center_x + self._px(4),
            center_y - self._px(7),
            center_x + self._px(8),
            center_y - self._px(7),
            center_x + self._px(8),
            center_y - self._px(3),
            fill=color,
            width=stroke_width,
            capstyle="round",
            joinstyle="round",
            tags="cv_action_content",
        )

    def _draw_action_stop_progress(
        self,
        button,
        width: int,
        height: int,
    ) -> None:
        """Draw the centered circular progress ring with a stop square."""
        self._draw_action_progress(button, width, height, pause=False)

    def _draw_action_pause_progress(
        self,
        button,
        width: int,
        height: int,
    ) -> None:
        """Draw the centered circular progress ring with a pause glyph."""
        self._draw_action_progress(button, width, height, pause=True)

    def _draw_action_progress(
        self,
        button,
        width: int,
        height: int,
        *,
        pause: bool,
    ) -> None:
        """Draw a shared progress ring with either stop or pause affordance."""
        style = self._style
        enabled = getattr(button, "_cv_enabled", True)
        diameter = self._px(style.action_progress_ring_diameter)
        stroke_width = max(1, self._px(style.action_progress_ring_stroke_width))
        stop_size = self._px(style.action_stop_size)
        center_x = width / 2
        center_y = height / 2
        radius = diameter / 2
        inset = stroke_width / 2
        x0 = center_x - radius + inset
        y0 = center_y - radius + inset
        x1 = center_x + radius - inset
        y1 = center_y + radius - inset

        track_color = style.progress_track_color
        progress_fill_color = (
            style.progress_fill_color if enabled else style.disabled_button_fg
        )
        stop_fill_color = style.button_fg if enabled else style.disabled_button_fg
        button.create_oval(
            x0,
            y0,
            x1,
            y1,
            outline=track_color,
            width=stroke_width,
            tags="cv_action_content",
        )

        fraction = getattr(button, "_cv_progress_fraction", 0.0)
        if fraction is None:
            extent = -100
        else:
            extent = -max(2, int(round(359 * max(0.0, min(1.0, fraction)))))
        button.create_arc(
            x0,
            y0,
            x1,
            y1,
            start=90,
            extent=extent,
            style="arc",
            outline=progress_fill_color,
            width=stroke_width,
            tags="cv_action_content",
        )

        if pause:
            pause_width = max(1, stop_size / 3)
            pause_gap = max(1, stop_size / 5)
            half_height = stop_size / 2
            button.create_rectangle(
                center_x - pause_gap / 2 - pause_width,
                center_y - half_height,
                center_x - pause_gap / 2,
                center_y + half_height,
                fill=stop_fill_color,
                outline="",
                tags="cv_action_content",
            )
            button.create_rectangle(
                center_x + pause_gap / 2,
                center_y - half_height,
                center_x + pause_gap / 2 + pause_width,
                center_y + half_height,
                fill=stop_fill_color,
                outline="",
                tags="cv_action_content",
            )
            return

        half_stop = stop_size / 2
        button.create_rectangle(
            center_x - half_stop,
            center_y - half_stop,
            center_x + half_stop,
            center_y + half_stop,
            fill=stop_fill_color,
            outline="",
            tags="cv_action_content",
        )

    def _canvas_dimension(self, canvas, option: str) -> int:
        try:
            return max(1, int(float(canvas.cget(option))))
        except (tk.TclError, TypeError, ValueError):
            return 1

    def _set_action_button(
        self,
        button,
        text: str,
        command: Callable[[], None],
        *,
        enabled: bool = True,
        show_stop_progress: bool = False,
        show_pause_progress: bool = False,
    ) -> None:
        self._hide_action_tooltip(button)
        button._cv_enabled = bool(enabled)
        button._cv_action_text = text
        button._cv_show_stop_progress = bool(show_stop_progress)
        button._cv_show_pause_progress = bool(show_pause_progress)
        button._cv_action_visual = map_library_action_visual(
            text,
            show_stop_progress=show_stop_progress,
            show_pause_progress=show_pause_progress,
        )
        if not show_stop_progress and not show_pause_progress:
            button._cv_progress_fraction = 0.0

        def invoke_if_enabled() -> None:
            if getattr(button, "_cv_enabled", True):
                command()

        button._cv_invoke = invoke_if_enabled
        self._bind_activation(button, invoke_if_enabled)
        self._set_action_button_style(button)
        self._draw_action_button(button)
        self._set_row_open_activation(button)

    def _set_row_open_activation(self, button) -> None:
        """Make only ready map rows mouse-openable; downloads stay explicit."""
        visual = getattr(button, "_cv_action_visual", None)
        row_activates = bool(
            getattr(visual, "row_activates", False)
            and getattr(button, "_cv_enabled", True)
        )

        def invoke_row(_event=None):
            callback = getattr(button, "_cv_invoke", None)
            if callback is not None:
                callback()
            return "break"

        for widget in getattr(button, "_cv_row_action_widgets", ()):
            if not self._widget_exists(widget):
                continue
            try:
                widget.unbind("<Button-1>")
                widget.config(cursor="hand2" if row_activates else "arrow")
                if row_activates:
                    widget.bind("<Button-1>", invoke_row)
            except tk.TclError:
                continue

    def _configure_action_button_hover(self, button) -> None:
        def show_hover(_event) -> None:
            if getattr(button, "_cv_enabled", True):
                self._set_action_button_style(button, hovered=True)
                self._schedule_action_tooltip(button)

        def clear_hover(_event) -> None:
            self._hide_action_tooltip(button)
            self._set_action_button_style(button)

        button.bind("<Enter>", show_hover)
        button.bind("<Leave>", clear_hover)
        button.bind("<FocusIn>", lambda _event: self._schedule_action_tooltip(button))
        button.bind("<FocusOut>", lambda _event: self._hide_action_tooltip(button))

    def _schedule_action_tooltip(self, button) -> None:
        """Show the text action name after a short hover or focus delay."""
        if not getattr(button, "_cv_enabled", True):
            return
        visual = getattr(button, "_cv_action_visual", None)
        text = getattr(visual, "tooltip", "")
        if not text:
            return
        self._hide_action_tooltip(button)
        try:
            button._cv_tooltip_after_id = self.root.after(
                500,
                lambda target=button, tooltip_text=text: self._show_action_tooltip(
                    target,
                    tooltip_text,
                ),
            )
        except tk.TclError:
            pass

    def _hide_action_tooltip(self, button) -> None:
        after_id = getattr(button, "_cv_tooltip_after_id", None)
        button._cv_tooltip_after_id = None
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        tooltip = getattr(button, "_cv_tooltip", None)
        button._cv_tooltip = None
        if self._widget_exists(tooltip):
            try:
                tooltip.destroy()
            except tk.TclError:
                pass

    def _hide_row_action_tooltips(self, row_widgets: MapLibraryRowWidgets) -> None:
        """Dispose of any tooltip windows associated with a row's controls."""
        self._hide_action_tooltip(row_widgets.action_button)
        self._hide_action_tooltip(row_widgets.overflow_button)

    def _show_action_tooltip(self, button, text: str) -> None:
        button._cv_tooltip_after_id = None
        if not self._widget_exists(button) or not getattr(button, "_cv_enabled", True):
            return
        self._hide_action_tooltip(button)
        try:
            tooltip = tk.Toplevel(self.root)
            tooltip.withdraw()
            tooltip.overrideredirect(True)
            tooltip.transient(self.root)
            tooltip.configure(bg=self._style.menu_border)
            label = tk.Label(
                tooltip,
                text=text,
                font=self._style.supporting_font,
                bg=self._style.menu_bg,
                fg=self._style.menu_text,
                padx=self._px(8),
                pady=self._px(4),
            )
            label.pack()
            x = button.winfo_rootx() + button.winfo_width() + self._px(4)
            y = button.winfo_rooty()
            tooltip.geometry(f"+{x}+{y}")
            button._cv_tooltip = tooltip
            tooltip.deiconify()
            tooltip.lift()
        except tk.TclError:
            self._hide_action_tooltip(button)

    def _menu_actions(self, button) -> tuple[MenuAction, ...]:
        factory = getattr(button, "_cv_menu_actions_factory", None)
        if factory is None:
            return ()
        try:
            return tuple(factory() or ())
        except Exception as exc:
            self._log.warning("could not build map-library row menu: %s", exc)
            return ()

    @staticmethod
    def _menu_action_parts(
        menu_action: MenuAction,
    ) -> tuple[str, Callable[[], None] | None, str | None]:
        """Normalize legacy action tuples and explanatory disabled entries."""
        if isinstance(menu_action, MapLibraryMenuAction):
            return (
                menu_action.label,
                menu_action.action,
                menu_action.explanation,
            )
        label, action = menu_action
        return label, action, None

    def _refresh_overflow_button(self, button) -> None:
        has_actions = bool(self._menu_actions(button))
        button._cv_has_menu_actions = has_actions
        button._cv_enabled = has_actions
        style = self._style
        if not has_actions:
            self._hide_action_tooltip(button)
        button.config(
            bg=style.panel_color,
            cursor="hand2" if has_actions else "arrow",
            takefocus=has_actions,
            highlightbackground=style.panel_color,
            highlightcolor=(
                style.button_border_color if has_actions else style.panel_color
            ),
        )
        self._draw_overflow_button(button)

    def _draw_overflow_button(self, button) -> None:
        """Draw the trailing map-actions icon without relying on a font glyph."""
        if not self._widget_exists(button):
            return
        button.delete("cv_overflow_content")
        if not getattr(button, "_cv_has_menu_actions", False):
            return
        color = (
            self._style.overflow_hover_fg
            if getattr(button, "_cv_overflow_hover", False)
            else self._style.overflow_fg
        )
        width = self._canvas_dimension(button, "width")
        height = self._canvas_dimension(button, "height")
        radius = max(1, self._px(1.3))
        center_x = width / 2
        center_y = height / 2
        spacing = max(3, self._px(5))
        for y in (center_y - spacing, center_y, center_y + spacing):
            button.create_oval(
                center_x - radius,
                y - radius,
                center_x + radius,
                y + radius,
                fill=color,
                outline="",
                tags="cv_overflow_content",
            )

    @staticmethod
    def _menu_popover_position(
        *,
        button_x: int,
        button_y: int,
        button_width: int,
        button_height: int,
        root_width: int,
        root_height: int,
        menu_width: int,
        menu_height: int,
        margin: int,
        gap: int,
    ) -> tuple[int, int]:
        """Return a root-local popover origin that remains inside the splash."""
        margin = max(0, margin)
        gap = max(0, gap)
        root_width = max(1, root_width)
        root_height = max(1, root_height)
        menu_width = max(1, menu_width)
        menu_height = max(1, menu_height)

        max_x = max(margin, root_width - menu_width - margin)
        x = min(
            max(margin, button_x + button_width - menu_width),
            max_x,
        )
        max_y = max(margin, root_height - menu_height - margin)
        below_y = button_y + button_height + gap
        if below_y <= max_y:
            return x, below_y
        return x, min(max(margin, button_y - menu_height - gap), max_y)

    @staticmethod
    def _menu_contains_widget(menu, widget) -> bool:
        """Return whether a widget belongs to the current in-window popover."""
        if widget is None:
            return False
        try:
            menu_name = str(menu)
            widget_name = str(widget)
        except tk.TclError:
            return False
        return widget_name == menu_name or widget_name.startswith(f"{menu_name}.")

    def _menu_owns_keyboard_focus(self, menu) -> bool:
        """Return whether focus moved into the open menu rather than away."""
        try:
            focused = self.root.focus_displayof()
        except (tk.TclError, AttributeError):
            return False
        if focused is None:
            return False
        return self._menu_contains_widget(menu, focused)

    def _install_menu_dismissal_bindings(self, menu, opener) -> None:
        """Attach temporary splash callbacks for one in-window popover.

        The callbacks remain scoped to this menu instance, so an old popover
        cannot close a newer replacement menu.  They use the splash root's
        bind tag rather than a global ``bind_all`` handler.
        """

        def dismiss_for_pointer(event, expected_menu=menu, expected_opener=opener):
            if self._active_menu is not expected_menu:
                return None
            # The click that created this menu can still reach the splash
            # toplevel bind tag.  Let that click finish instead of closing the
            # menu immediately; a click on a different overflow button first
            # closes this old menu and then opens its replacement normally.
            clicked_widget = getattr(event, "widget", None)
            if (
                clicked_widget is expected_opener
                or self._menu_contains_widget(expected_menu, clicked_widget)
            ):
                return None
            self.close_active_menu()
            return None

        def dismiss_for_focus(_event, expected_menu=menu):
            def close_if_focus_left() -> None:
                if (
                    self._active_menu is expected_menu
                    and not self._menu_owns_keyboard_focus(expected_menu)
                ):
                    self.close_active_menu()

            try:
                self.root.after_idle(close_if_focus_left)
            except (tk.TclError, AttributeError):
                close_if_focus_left()
            return None

        def dismiss_for_escape(_event, expected_menu=menu):
            if self._active_menu is not expected_menu:
                return None
            self.close_active_menu()
            return "break"

        bindings: list[tuple[str, str]] = []
        try:
            pointer_id = self.root.bind(
                "<ButtonPress-1>",
                dismiss_for_pointer,
                add="+",
            )
            focus_id = self.root.bind("<FocusOut>", dismiss_for_focus, add="+")
            escape_id = self.root.bind("<Escape>", dismiss_for_escape, add="+")
            if pointer_id:
                bindings.append(("<ButtonPress-1>", pointer_id))
            if focus_id:
                bindings.append(("<FocusOut>", focus_id))
            if escape_id:
                bindings.append(("<Escape>", escape_id))
        except (tk.TclError, AttributeError):
            for sequence, callback_id in bindings:
                try:
                    self.root.unbind(sequence, callback_id)
                except (tk.TclError, AttributeError):
                    pass
            self.close_active_menu()
            return
        self._active_menu_root_bindings = bindings

    def _show_row_menu(self, button) -> None:
        self.close_active_menu()
        if not self._widget_exists(button):
            return

        actions = self._menu_actions(button)
        if not actions:
            self._refresh_overflow_button(button)
            return

        style = self._style
        try:
            root_width = max(1, self.root.winfo_width())
            root_height = max(1, self.root.winfo_height())
        except (tk.TclError, AttributeError):
            return
        menu_margin = max(1, self._px(8))
        menu_gap = max(1, self._px(4))
        max_menu_width = max(1, root_width - (menu_margin * 2))
        menu_text_wraplength = max(1, max_menu_width - self._px(24))
        menu = tk.Frame(
            self.root,
            bg=style.menu_bg,
            highlightthickness=1,
            highlightbackground=style.menu_border,
            highlightcolor=style.menu_border,
        )
        self._active_menu = menu

        first_item = [None]
        for menu_action in actions:
            item_text, item_action, explanation = self._menu_action_parts(menu_action)
            enabled = item_action is not None
            display_text = item_text
            if explanation:
                display_text = f"{item_text}\n{explanation}"

            item = tk.Label(
                menu,
                text=display_text,
                font=style.body_font,
                bg=style.menu_bg,
                fg=style.menu_text if enabled else style.disabled_button_fg,
                padx=self._px(12),
                pady=self._px(7),
                cursor="hand2" if enabled else "arrow",
                takefocus=enabled,
                anchor="w",
                justify="left",
                wraplength=menu_text_wraplength,
            )
            if enabled:

                def invoke_and_close(action=item_action) -> None:
                    self.close_active_menu()
                    action()

                self._bind_activation(item, invoke_and_close)
                item.bind(
                    "<Enter>",
                    lambda _event, target=item: target.config(bg=style.menu_hover_bg),
                )
                item.bind(
                    "<Leave>",
                    lambda _event, target=item: target.config(bg=style.menu_bg),
                )
            item.pack(fill="x")
            if enabled and first_item[0] is None:
                first_item[0] = item

        try:
            menu.update_idletasks()
            menu_width = min(menu.winfo_reqwidth(), max_menu_width)
            max_menu_height = max(1, root_height - (menu_margin * 2))
            menu_height = min(menu.winfo_reqheight(), max_menu_height)
            x, y = self._menu_popover_position(
                button_x=button.winfo_rootx() - self.root.winfo_rootx(),
                button_y=button.winfo_rooty() - self.root.winfo_rooty(),
                button_width=button.winfo_width(),
                button_height=button.winfo_height(),
                root_width=root_width,
                root_height=root_height,
                menu_width=menu_width,
                menu_height=menu_height,
                margin=menu_margin,
                gap=menu_gap,
            )
            menu.place(x=x, y=y, width=menu_width, height=menu_height)
            menu.lift()
            if first_item[0] is not None:
                first_item[0].focus_set()
        except tk.TclError:
            self.close_active_menu()
            return

        self._install_menu_dismissal_bindings(menu, button)

    def _create_overflow_button(self, parent, menu_actions_factory=None):
        style = self._style
        size = max(1, self._px(style.overflow_button_size))
        button = tk.Canvas(
            parent,
            width=size,
            height=size,
            bg=style.panel_color,
            borderwidth=0,
            cursor="arrow",
            takefocus=False,
            highlightthickness=1,
            highlightbackground=style.panel_color,
            highlightcolor=style.panel_color,
        )
        button._cv_menu_actions_factory = menu_actions_factory
        button._cv_has_menu_actions = False
        button._cv_overflow_hover = False
        button._cv_enabled = False
        button._cv_action_visual = MapLibraryActionVisual(
            "more-vertical",
            "Map actions",
        )
        button._cv_tooltip_after_id = None
        button._cv_tooltip = None

        def show_hover(_event=None) -> None:
            if not getattr(button, "_cv_has_menu_actions", False):
                return
            button._cv_overflow_hover = True
            button.config(bg=style.overflow_hover_bg, highlightbackground=style.menu_border)
            self._draw_overflow_button(button)
            self._schedule_action_tooltip(button)

        def clear_hover(_event=None) -> None:
            self._hide_action_tooltip(button)
            button._cv_overflow_hover = False
            if not getattr(button, "_cv_has_menu_actions", False):
                button.config(bg=style.panel_color, highlightbackground=style.panel_color)
                return
            button.config(bg=style.panel_color, highlightbackground=style.panel_color)
            self._draw_overflow_button(button)

        self._bind_activation(button, lambda: self._show_row_menu(button))
        button.bind("<Enter>", show_hover)
        button.bind("<Leave>", clear_hover)
        button.bind("<FocusIn>", lambda _event: self._schedule_action_tooltip(button))
        button.bind("<FocusOut>", lambda _event: self._hide_action_tooltip(button))
        button.bind(
            "<Configure>",
            lambda _event, target=button: self._draw_overflow_button(target),
            add="+",
        )
        button.pack(side="right", padx=(0, self._px(12)), pady=self._px(5))
        self._refresh_overflow_button(button)
        return button

    def _resize_canvas_window(self, event) -> None:
        if self._content_canvas is None or self._rows_window is None:
            return
        self._content_canvas.itemconfigure(self._rows_window, width=event.width)
        self.sync_scroll_region()

    def _scroll_content(self, event):
        if self._content_canvas is None or not self._content_overflows:
            return None
        units = vertical_scroll_units(event)
        if units is not None:
            self._content_canvas.yview_scroll(units, "units")
        return "break"

    def _bind_mousewheel(self, widget) -> None:
        widget.bind("<MouseWheel>", self._scroll_content, add="+")
        widget.bind("<Button-4>", self._scroll_content, add="+")
        widget.bind("<Button-5>", self._scroll_content, add="+")
        for child in widget.winfo_children():
            self._bind_mousewheel(child)
