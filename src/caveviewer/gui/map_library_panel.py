"""Tk presentation surface for the splash Map Library panel."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from typing import Callable, Iterable


MenuAction = tuple[str, Callable[[], None]]
MenuActionsFactory = Callable[["MapLibraryRowWidgets"], Iterable[MenuAction]]


@dataclass(frozen=True)
class MapLibraryPanelStyle:
    """Theme and layout tokens used by the splash Map Library panel."""

    panel_color: str
    panel_border_color: str
    title_color: str
    instruction_color: str
    small_font: tuple
    metadata_font: tuple
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
    action_button_width: int
    action_button_pad_x: int
    action_button_pad_y: int
    overflow_text: str
    overflow_font: tuple
    overflow_fg: str
    overflow_hover_fg: str
    overflow_hover_bg: str
    menu_bg: str
    menu_border: str
    menu_hover_bg: str
    menu_text: str
    scrollbar_width: int
    scroll_thumb_min_height: int
    scroll_thumb_width: int
    scroll_thumb_color: str
    scroll_thumb_active_color: str


@dataclass(frozen=True)
class MapLibraryRowWidgets:
    """Tk widgets owned by one map-library row on the splash thread."""

    row_shell: object
    leading_widget: object
    action_button: object
    metadata_label: object | None


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
    ) -> None:
        self.root = root
        self._px = px
        self._bind_activation = bind_activation
        self._widget_exists = widget_exists
        self._log = logger
        self._style = style
        self.standard_rows: dict[str, MapLibraryRowWidgets] = {}
        self.recent_rows: dict[str, MapLibraryRowWidgets] = {}
        self._recent_container = None
        self._recent_empty_note = None
        self._rows_frame = None
        self._content_canvas = None
        self._content_scrollbar = None
        self._rows_window = None
        self._scrollbar_fraction = (0.0, 1.0)
        self._scrollbar_thumb = None
        self._scrollbar_drag_offset = 0.0
        self._active_menu = None

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
        panel.pack(fill="both", expand=True, pady=self._px(26))

        scroll_shell = tk.Frame(panel, bg=style.panel_color)
        scroll_shell.pack(
            fill="both",
            expand=True,
            padx=self._px(12),
            pady=(0, self._px(12)),
        )

        self._content_canvas = tk.Canvas(
            scroll_shell,
            bg=style.panel_color,
            borderwidth=0,
            highlightthickness=0,
            yscrollcommand=lambda *_args: None,
        )
        self._content_scrollbar = tk.Canvas(
            scroll_shell,
            bg=style.panel_color,
            borderwidth=0,
            highlightthickness=0,
            width=style.scrollbar_width,
            cursor="sb_v_double_arrow",
        )
        self._content_canvas.pack(side="left", fill="both", expand=True)

        self._rows_frame = tk.Frame(self._content_canvas, bg=style.panel_color)
        self._rows_window = self._content_canvas.create_window(
            (0, 0),
            window=self._rows_frame,
            anchor="nw",
        )

        self._content_canvas.configure(
            yscrollcommand=self._set_scrollbar_fraction
        )
        self._content_canvas.bind(
            "<Configure>",
            self._resize_canvas_window,
            add="+",
        )
        self._content_canvas.bind("<MouseWheel>", self._scroll_content, add="+")
        self._content_canvas.bind("<Button-4>", self._scroll_content, add="+")
        self._content_canvas.bind("<Button-5>", self._scroll_content, add="+")
        self._content_scrollbar.bind(
            "<Configure>",
            lambda _event: self._draw_scrollbar_thumb(),
            add="+",
        )
        self._content_scrollbar.bind(
            "<ButtonPress-1>",
            self._start_scrollbar_drag,
            add="+",
        )
        self._content_scrollbar.bind(
            "<B1-Motion>",
            self._drag_scrollbar,
            add="+",
        )
        self._content_scrollbar.bind(
            "<ButtonRelease-1>",
            self._end_scrollbar_drag,
            add="+",
        )

        self._create_section(self._rows_frame, "Your Recent Maps", top_pad=16)
        self._recent_container = tk.Frame(
            self._rows_frame, bg=style.panel_color
        )
        self._recent_container.pack(fill="x")
        self._create_section(self._rows_frame, "CaveViewer Maps")

    def finish_population(self) -> None:
        """Bind mousewheel events after rows exist and schedule scroll sync."""
        if self._rows_frame is None:
            return
        self.bind_mousewheel_if_ready(self._rows_frame)
        self.root.after_idle(self.sync_scrollbar)

    def close_active_menu(self) -> None:
        """Close the transient overflow menu if it is currently open."""
        menu = self._active_menu
        self._active_menu = None
        if self._widget_exists(menu):
            try:
                menu.destroy()
            except tk.TclError:
                pass

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
            menu_actions_factory=menu_actions_factory,
        )
        self.recent_rows[entry.key] = widgets
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
            row_widgets.row_shell.destroy()
        self.ensure_recent_empty_note()
        self.sync_after_row_change()

    def add_standard_row(
        self,
        row,
        *,
        action: Callable[[], None],
        menu_actions_factory: MenuActionsFactory | None = None,
    ) -> MapLibraryRowWidgets:
        """Append one standard-library row to the CaveViewer Maps section."""
        if self._rows_frame is None:
            raise RuntimeError("MapLibraryPanel.create() must run first")
        widgets = self._create_row(
            self._rows_frame,
            title=row.title,
            detail=row.detail,
            size_text="",
            action_text=row.action_text,
            action=action,
            reserve_metadata=True,
            menu_actions_factory=menu_actions_factory,
        )
        self.standard_rows[row.key] = widgets
        return widgets

    def has_standard_row(self, key: str) -> bool:
        """Return whether a standard-library row currently exists."""
        return key in self.standard_rows

    def remove_standard_row(self, key: str) -> None:
        """Remove a standard-library row when refreshed catalog metadata drops it."""
        row_widgets = self.standard_rows.pop(key, None)
        if row_widgets is not None and self._widget_exists(row_widgets.row_shell):
            row_widgets.row_shell.destroy()
        self.sync_after_row_change()

    def set_standard_row_metadata(
        self,
        key: str,
        text: str,
        *,
        error: bool = False,
    ) -> None:
        """Set the stable metadata text for one standard-library row."""
        widgets = self.standard_rows.get(key)
        if widgets is None or not self._widget_exists(widgets.metadata_label):
            return
        self._set_metadata_label_base(widgets.metadata_label, text, error=error)

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

    def set_standard_row_action(
        self,
        key: str,
        text: str,
        command: Callable[[], None],
        *,
        enabled: bool = True,
        show_stop_progress: bool = False,
    ) -> bool:
        """Update the primary action button for one standard-library row."""
        widgets = self.standard_rows.get(key)
        if widgets is None or not self._widget_exists(widgets.action_button):
            return False
        self._set_action_button(
            widgets.action_button,
            text,
            command,
            enabled=enabled,
            show_stop_progress=show_stop_progress,
        )
        return True

    def refresh_standard_row_overflow(self, key: str) -> None:
        """Refresh a standard-library row overflow button after state changes."""
        widgets = self.standard_rows.get(key)
        if widgets is not None:
            self.refresh_row_overflow(widgets)

    def refresh_row_overflow(self, row_widgets: MapLibraryRowWidgets | None) -> None:
        """Refresh one overflow button after its action availability changes."""
        if row_widgets is None:
            return
        if self._widget_exists(row_widgets.leading_widget):
            self._refresh_overflow_button(row_widgets.leading_widget)

    def reset_standard_progress(self, key: str) -> None:
        """Return a standard-library row action button to text mode."""
        widgets = self.standard_rows.get(key)
        if widgets is None or not self._widget_exists(widgets.action_button):
            return
        widgets.action_button._cv_show_stop_progress = False
        widgets.action_button._cv_progress_fraction = 0.0
        self._draw_action_button(widgets.action_button)

    def show_standard_progress(self, key: str) -> None:
        """Show the stop/progress affordance for an active download row."""
        widgets = self.standard_rows.get(key)
        if widgets is None or not self._widget_exists(widgets.action_button):
            return
        widgets.action_button._cv_show_stop_progress = True
        widgets.action_button._cv_progress_fraction = 0.0
        self._draw_action_button(widgets.action_button)
        self.root.update_idletasks()

    def apply_standard_progress(
        self,
        key: str,
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

    def sync_after_row_change(self) -> None:
        """Schedule a scroll-region refresh after row insertion/removal."""
        if self._widget_exists(self.root):
            self.root.after_idle(self.sync_scrollbar)

    def bind_mousewheel_if_ready(self, widget) -> None:
        """Bind recursive mousewheel handlers when panel scrolling is ready."""
        if self._widget_exists(widget):
            self._bind_mousewheel(widget)

    def sync_scrollbar(self) -> None:
        """Synchronize the scroll region and scrollbar visibility."""
        if self._content_canvas is None or self._rows_frame is None:
            return
        if self._content_scrollbar is None:
            return
        width = max(1, self._content_canvas.winfo_width())
        content_height = self._rows_frame.winfo_reqheight()
        self._content_canvas.configure(scrollregion=(0, 0, width, content_height))
        visible_height = self._content_canvas.winfo_height()
        if content_height > visible_height + 1:
            if not self._content_scrollbar.winfo_manager():
                self._content_scrollbar.pack(side="right", fill="y")
        else:
            if self._content_scrollbar.winfo_manager():
                self._content_scrollbar.pack_forget()
            self._content_canvas.yview_moveto(0)

    def _create_section(self, parent, text: str, *, top_pad: int = 10) -> None:
        label = tk.Label(
            parent,
            text=text,
            font=self._style.small_font,
            fg=self._style.instruction_color,
            bg=self._style.panel_color,
            anchor="w",
        )
        label.pack(anchor="w", fill="x", pady=(self._px(top_pad), self._px(6)))

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
            font=self._style.small_font,
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

        leading_widget = self._create_overflow_button(
            row_content,
            button_factory,
        )

        text_column = tk.Frame(row_content, bg=style.panel_color)
        text_column.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(self._px(12), self._px(8)),
            pady=self._px(5),
        )

        name_label = tk.Label(
            text_column,
            text=title,
            font=style.small_font,
            fg=style.title_color,
            bg=style.panel_color,
            anchor="w",
            justify="left",
            wraplength=self._px(250),
        )
        name_label.pack(anchor="w", fill="x")

        metadata_text = detail or size_text
        metadata_label = None
        if metadata_text or reserve_metadata:
            metadata_label = tk.Label(
                text_column,
                text=metadata_text,
                font=style.metadata_font,
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
        action_button.pack(side="right", padx=(0, self._px(12)), pady=self._px(5))

        row_widgets = MapLibraryRowWidgets(
            row_shell=row_shell,
            leading_widget=leading_widget,
            action_button=action_button,
            metadata_label=metadata_label,
        )
        row_holder[0] = row_widgets
        self.refresh_row_overflow(row_widgets)
        return row_widgets

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
        """Create the fixed-size canvas button used by map-library rows."""
        style = self._style
        button_width, button_height = self._action_button_pixel_size()
        button = tk.Canvas(
            parent,
            width=button_width,
            height=button_height,
            bg=style.button_bg,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=style.button_border_color,
            highlightcolor=style.button_border_color,
            cursor="hand2",
            takefocus=True,
        )
        button._cv_enabled = True
        button._cv_action_text = text
        button._cv_show_stop_progress = False
        button._cv_progress_fraction = 0.0
        self._bind_activation(button, action)
        button.bind(
            "<Configure>",
            lambda _event, target=button: self._draw_action_button(target),
            add="+",
        )
        self._draw_action_button(button)
        return button

    def _action_button_pixel_size(self) -> tuple[int, int]:
        """Return a canvas size matching the old fixed-width label button."""
        style = self._style
        pad_x = self._px(style.action_button_pad_x)
        pad_y = self._px(style.action_button_pad_y)
        try:
            font = tkfont.Font(font=style.small_font)
            text_width = font.measure("0" * style.action_button_width)
            text_height = font.metrics("linespace")
        except tk.TclError:
            text_width = self._px(style.action_button_width * 8)
            text_height = self._px(16)
        return max(1, text_width + pad_x * 2), max(1, text_height + pad_y * 2)

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
            highlightbackground=border,
            highlightcolor=border,
        )

    def _draw_action_button(self, button) -> None:
        """Redraw the button text or in-button stop/progress affordance."""
        if not self._widget_exists(button):
            return

        button.delete("cv_action_content")
        width = self._canvas_dimension(button, "width")
        height = self._canvas_dimension(button, "height")
        if getattr(button, "_cv_show_stop_progress", False):
            self._draw_action_stop_progress(button, width, height)
            return

        enabled = getattr(button, "_cv_enabled", True)
        fill = self._style.button_fg if enabled else self._style.disabled_button_fg
        button.create_text(
            width / 2,
            height / 2,
            text=getattr(button, "_cv_action_text", ""),
            font=self._style.small_font,
            fill=fill,
            tags="cv_action_content",
        )

    def _draw_action_stop_progress(
        self,
        button,
        width: int,
        height: int,
    ) -> None:
        """Draw the centered circular progress ring with a stop square."""
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
    ) -> None:
        button._cv_enabled = bool(enabled)
        button._cv_action_text = text
        button._cv_show_stop_progress = bool(show_stop_progress)
        if not show_stop_progress:
            button._cv_progress_fraction = 0.0

        def invoke_if_enabled() -> None:
            if getattr(button, "_cv_enabled", True):
                command()

        self._bind_activation(button, invoke_if_enabled)
        self._set_action_button_style(button)
        self._draw_action_button(button)

    def _configure_action_button_hover(self, button) -> None:
        def show_hover(_event) -> None:
            if getattr(button, "_cv_enabled", True):
                self._set_action_button_style(button, hovered=True)

        def clear_hover(_event) -> None:
            self._set_action_button_style(button)

        button.bind("<Enter>", show_hover)
        button.bind("<Leave>", clear_hover)

    def _menu_actions(self, button) -> tuple[MenuAction, ...]:
        factory = getattr(button, "_cv_menu_actions_factory", None)
        if factory is None:
            return ()
        try:
            return tuple(factory() or ())
        except Exception as exc:
            self._log.warning("could not build map-library row menu: %s", exc)
            return ()

    def _refresh_overflow_button(self, button) -> None:
        has_actions = bool(self._menu_actions(button))
        button._cv_has_menu_actions = has_actions
        style = self._style
        button.config(
            text=style.overflow_text if has_actions else "",
            fg=style.overflow_fg if has_actions else style.panel_color,
            cursor="hand2" if has_actions else "arrow",
            takefocus=has_actions,
            highlightbackground=style.panel_color,
            highlightcolor=(
                style.button_border_color if has_actions else style.panel_color
            ),
        )

    def _show_row_menu(self, button) -> None:
        self.close_active_menu()
        if not self._widget_exists(button):
            return

        actions = self._menu_actions(button)
        if not actions:
            self._refresh_overflow_button(button)
            return

        style = self._style
        menu = tk.Toplevel(self.root)
        self._active_menu = menu
        menu.withdraw()
        menu.overrideredirect(True)
        menu.transient(self.root)
        menu.configure(bg=style.menu_border)

        frame = tk.Frame(
            menu,
            bg=style.menu_bg,
            highlightthickness=1,
            highlightbackground=style.menu_border,
            highlightcolor=style.menu_border,
        )
        frame.pack()

        first_item = [None]
        for item_text, item_action in actions:

            def invoke_and_close(action=item_action) -> None:
                self.close_active_menu()
                action()

            item = tk.Label(
                frame,
                text=item_text,
                font=style.small_font,
                bg=style.menu_bg,
                fg=style.menu_text,
                padx=self._px(12),
                pady=self._px(7),
                cursor="hand2",
                takefocus=True,
                anchor="w",
            )
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
            if first_item[0] is None:
                first_item[0] = item

        try:
            x = button.winfo_rootx()
            y = button.winfo_rooty() + button.winfo_height() + self._px(4)
            menu.geometry(f"+{x}+{y}")
            menu.deiconify()
            menu.lift()
            if first_item[0] is not None:
                first_item[0].focus_set()
        except tk.TclError:
            self.close_active_menu()
            return

        menu.bind("<Escape>", lambda _event: self.close_active_menu())
        menu.bind(
            "<FocusOut>",
            lambda _event: self.root.after(80, self.close_active_menu),
        )

    def _create_overflow_button(self, parent, menu_actions_factory=None):
        style = self._style
        button = tk.Label(
            parent,
            text="",
            font=style.overflow_font,
            bg=style.panel_color,
            fg=style.panel_color,
            width=2,
            padx=0,
            pady=0,
            cursor="arrow",
            takefocus=False,
            highlightthickness=1,
            highlightbackground=style.panel_color,
            highlightcolor=style.panel_color,
        )
        button._cv_menu_actions_factory = menu_actions_factory
        button._cv_has_menu_actions = False

        def show_hover(_event=None) -> None:
            if not getattr(button, "_cv_has_menu_actions", False):
                return
            button.config(
                bg=style.overflow_hover_bg,
                fg=style.overflow_hover_fg,
                highlightbackground=style.menu_border,
            )

        def clear_hover(_event=None) -> None:
            if not getattr(button, "_cv_has_menu_actions", False):
                button.config(
                    bg=style.panel_color,
                    fg=style.panel_color,
                    highlightbackground=style.panel_color,
                )
                return
            button.config(
                bg=style.panel_color,
                fg=style.overflow_fg,
                highlightbackground=style.panel_color,
            )

        self._bind_activation(button, lambda: self._show_row_menu(button))
        button.bind("<Enter>", show_hover)
        button.bind("<Leave>", clear_hover)
        button.pack(side="left", padx=(self._px(6), 0), pady=self._px(5))
        self._refresh_overflow_button(button)
        return button

    def _draw_scrollbar_thumb(self) -> None:
        if self._content_scrollbar is None:
            return
        height = max(1, self._content_scrollbar.winfo_height())
        first, last = self._scrollbar_fraction
        visible_fraction = max(0.0, min(1.0, last - first))
        if visible_fraction >= 1.0:
            if self._scrollbar_thumb is not None:
                self._content_scrollbar.delete(self._scrollbar_thumb)
                self._scrollbar_thumb = None
            return

        style = self._style
        thumb_height = max(
            style.scroll_thumb_min_height,
            int(round(height * visible_fraction)),
        )
        travel = max(1, height - thumb_height)
        y0 = int(round(max(0.0, min(1.0, first)) * travel))
        y1 = min(height, y0 + thumb_height)
        x = style.scrollbar_width // 2
        if self._scrollbar_thumb is None:
            self._scrollbar_thumb = self._content_scrollbar.create_line(
                x,
                y0,
                x,
                y1,
                fill=style.scroll_thumb_color,
                width=style.scroll_thumb_width,
                capstyle="round",
            )
        else:
            self._content_scrollbar.coords(self._scrollbar_thumb, x, y0, x, y1)

    def _set_scrollbar_fraction(self, first: str, last: str) -> None:
        self._scrollbar_fraction = (float(first), float(last))
        self._draw_scrollbar_thumb()

    def _resize_canvas_window(self, event) -> None:
        if self._content_canvas is None or self._rows_window is None:
            return
        self._content_canvas.itemconfigure(self._rows_window, width=event.width)
        self.sync_scrollbar()

    def _scroll_content(self, event):
        if self._content_canvas is None or self._content_scrollbar is None:
            return None
        if not self._content_scrollbar.winfo_manager():
            return None
        delta = getattr(event, "delta", 0)
        if delta:
            self._content_canvas.yview_scroll(int(-1 * (delta / 120)), "units")
        elif getattr(event, "num", None) == 4:
            self._content_canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            self._content_canvas.yview_scroll(1, "units")
        return "break"

    def _start_scrollbar_drag(self, event):
        if self._content_scrollbar is None:
            return "break"
        first, last = self._scrollbar_fraction
        height = max(1, self._content_scrollbar.winfo_height())
        visible_fraction = max(0.0, min(1.0, last - first))
        thumb_height = max(
            self._style.scroll_thumb_min_height,
            int(round(height * visible_fraction)),
        )
        travel = max(1, height - thumb_height)
        thumb_top = int(round(first * travel))
        thumb_bottom = thumb_top + thumb_height
        if thumb_top <= event.y <= thumb_bottom:
            self._scrollbar_drag_offset = event.y - thumb_top
        else:
            self._scrollbar_drag_offset = thumb_height / 2
            self._drag_scrollbar(event)
        if self._scrollbar_thumb is not None:
            self._content_scrollbar.itemconfigure(
                self._scrollbar_thumb,
                fill=self._style.scroll_thumb_active_color,
            )
        return "break"

    def _drag_scrollbar(self, event):
        if self._content_canvas is None or self._content_scrollbar is None:
            return "break"
        first, last = self._scrollbar_fraction
        height = max(1, self._content_scrollbar.winfo_height())
        visible_fraction = max(0.0, min(1.0, last - first))
        thumb_height = max(
            self._style.scroll_thumb_min_height,
            int(round(height * visible_fraction)),
        )
        travel = max(1, height - thumb_height)
        thumb_top = max(0, min(travel, event.y - self._scrollbar_drag_offset))
        self._content_canvas.yview_moveto(thumb_top / travel)
        return "break"

    def _end_scrollbar_drag(self, _event):
        if self._content_scrollbar is None:
            return "break"
        if self._scrollbar_thumb is not None:
            self._content_scrollbar.itemconfigure(
                self._scrollbar_thumb,
                fill=self._style.scroll_thumb_color,
            )
        return "break"

    def _bind_mousewheel(self, widget) -> None:
        widget.bind("<MouseWheel>", self._scroll_content, add="+")
        widget.bind("<Button-4>", self._scroll_content, add="+")
        widget.bind("<Button-5>", self._scroll_content, add="+")
        for child in widget.winfo_children():
            self._bind_mousewheel(child)
