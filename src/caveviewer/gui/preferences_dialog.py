"""Tk presentation adapter for the Preferences form controller."""

from __future__ import annotations

import os
import tkinter as tk
from typing import TYPE_CHECKING, Callable

from caveviewer.gui.preferences import (
    PREFERENCE_FIELDS,
    Preferences,
    PreferenceSpec,
    PreferenceValueType,
    preference_placeholder_text,
    apply_preferences_to_env,
    load_preferences,
    save_preferences,
)
from caveviewer.gui.preferences_workflow import PreferencesDialogWorkflow
from caveviewer.gui.preferences_form import (
    PreferencesFormController,
    PreferencesFormState,
    MessageKind,
)
from caveviewer.gui.features import FeatureState
from caveviewer.gui.dialog_style import (
    DIALOG_BODY_PAD_Y,
    DIALOG_PANEL_BORDER,
    create_dialog_action_button,
    set_dialog_action_button,
)
from caveviewer.gui.platform import (
    DesktopServiceError,
    DesktopServices,
    get_desktop_services,
)
from caveviewer.gui.platform.directory_selection import (
    choose_authorized_directory,
    directory_selection_preflight,
)
from caveviewer.gui.platform.presentation import (
    PresentationProfile,
    get_presentation_profile,
)
from caveviewer.gui.tk_scrolling import vertical_scroll_units
from caveviewer.gui.tk_theme import DARK_THEME
from caveviewer.gui.tk_typography import TkTypography, create_tk_typography

if TYPE_CHECKING:
    from caveviewer.gui.platform.runtime import PlatformRuntime


_BG_COLOR = DARK_THEME.background
_TITLE_COLOR = DARK_THEME.title
_SUBTITLE_COLOR = DARK_THEME.body_text
_INSTRUCTION_COLOR = DARK_THEME.secondary_text
_PANEL_COLOR = DARK_THEME.panel
_BUTTON_BG = DARK_THEME.primary_button
_BUTTON_BORDER_COLOR = DARK_THEME.primary_button_border

_NUMERIC_ENTRY_WIDTH = 8
_PLACEHOLDER_COLOR = DARK_THEME.placeholder_text
_INLINE_FEEDBACK_PAD_X = 10
_CONTROL_GAP_X = 10
_TAB_GAP_X = 10
_SCROLLBAR_WIDTH = 14
_SCROLL_THUMB_WIDTH = 5
_SCROLL_THUMB_MIN_HEIGHT = 36
_SCROLL_THUMB_COLOR = DARK_THEME.secondary_button_border
_SCROLL_THUMB_ACTIVE_COLOR = DARK_THEME.entry_focus_border
_PREFERENCE_PAGES = (
    ("streaming", "Streaming"),
    ("parsing", "Import"),
    ("storage", "Storage"),
)


class PreferencesPanel:
    """Reusable Preferences form displayed in the splash right-hand panel."""

    def __init__(
        self,
        parent,
        *,
        ui_font_family: str,
        desktop_services: DesktopServices | None = None,
        platform_runtime: PlatformRuntime | None = None,
        presentation_profile: PresentationProfile | None = None,
        typography: TkTypography | None = None,
        on_applied: Callable[[Preferences], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        if (
            platform_runtime is not None
            and desktop_services is not None
            and desktop_services is not platform_runtime.desktop_services
        ):
            raise ValueError(
                "desktop_services must match the injected platform_runtime"
            )
        runtime_presentation_profile = (
            getattr(platform_runtime, "presentation_profile", None)
            if platform_runtime is not None
            else None
        )
        if (
            runtime_presentation_profile is not None
            and presentation_profile is not None
            and presentation_profile != runtime_presentation_profile
        ):
            raise ValueError(
                "presentation_profile must match the injected platform_runtime"
            )
        self.parent = parent
        self.ui_font_family = ui_font_family
        self.platform_runtime = platform_runtime
        self.presentation_profile = (
            runtime_presentation_profile
            or presentation_profile
            or get_presentation_profile()
        )
        self._layout_policy = self.presentation_profile.preferences_dialog_layout
        self._dialog_layout = self.presentation_profile.dialog_layout
        self.typography = typography or create_tk_typography(
            ui_font_family,
            text_scale=self.presentation_profile.minimum_tk_text_scale,
        )
        self.desktop_services = (
            platform_runtime.desktop_services
            if platform_runtime is not None
            else desktop_services or get_desktop_services()
        )
        self.on_applied = on_applied
        self.on_cancel = on_cancel
        self.workflow = PreferencesDialogWorkflow(
            load_preferences_fn=load_preferences,
            save_preferences_fn=save_preferences,
            apply_preferences_to_env_fn=apply_preferences_to_env,
        )
        self.preferences = self.workflow.load_initial()
        self.form = PreferencesFormController(self.preferences)
        # The form needs a toplevel for Tk variables, scheduling, and native
        # directory pickers, while its widgets belong to the supplied panel.
        # The splash supplies the right-hand content frame, so this is its root.
        self.dialog = parent.winfo_toplevel()
        self.container = tk.Frame(parent, bg=_BG_COLOR)
        self.container.pack(fill="both", expand=True)

        self.section_font = self.typography.section
        self.action_font = self.typography.body_strong
        self.body_font = self.typography.body
        self.small_font = self.typography.supporting

        self.field_vars: dict[str, tk.StringVar] = {}
        self.field_entries: dict[str, tk.Entry] = {}
        self.field_display_vars: dict[str, tk.StringVar] = {}
        self.field_entry_states: dict[str, str] = {}
        self.field_browse_buttons: dict[str, tk.Widget] = {}
        self.numeric_entry_states: dict[str, tuple] = {}
        self.numeric_placeholder_keys: set[str] = set()
        self.form_ready = False
        self.rendering_state = False
        self.rendered_invalid_key: str | None = None
        self.apply_button = None
        self.tab_bar = None
        self.page_scroll_shell = None
        self.page_canvas = None
        self.page_canvas_window = None
        self.page_scrollbar = None
        self.page_scrollbar_thumb = None
        self.page_scrollbar_fraction = (0.0, 1.0)
        self.page_scroll_drag_offset = 0.0
        self.page_stack = None
        self.pages: dict[str, tk.Frame] = {}
        self.page_buttons: dict[str, tk.Label] = {}
        self.field_page_keys: dict[str, str] = {}
        self.active_page_key: str | None = None
        self.feedback_frame = None
        self.rendered_state: PreferencesFormState | None = None
        self.button_row = None
        self.error_label = None
        self._feedback_override: tuple[str, str] | None = None

        self.numeric_entry_validator = self.dialog.register(
            self._is_numeric_entry_candidate
        )
        self._build()

    @staticmethod
    def _is_numeric_entry_candidate(value_type: str, candidate: str) -> bool:
        if candidate == "":
            return True
        if value_type == "int":
            return candidate.isdigit()
        if value_type == "float":
            if candidate == ".":
                return True
            if candidate.count(".") > 1:
                return False
            return all(ch.isdigit() or ch == "." for ch in candidate)
        return True

    @staticmethod
    def _compact_directory_path(path: str, max_chars: int = 80) -> str:
        expanded = os.path.abspath(os.path.expanduser(path.strip() or "~"))
        home = os.path.abspath(os.path.expanduser("~"))
        if expanded == home:
            display = "~"
        elif expanded.startswith(home + os.sep):
            display = "~" + expanded[len(home):]
        else:
            display = expanded
        if len(display) <= max_chars:
            return display

        drive, tail = os.path.splitdrive(display)
        parts = [part for part in tail.split(os.sep) if part]
        if len(parts) >= 2:
            suffix = os.sep.join(parts[-2:])
            prefix = (
                "~"
                if display.startswith("~" + os.sep)
                else drive + os.sep
                if drive
                else os.sep
            )
            compact = prefix + "…" + os.sep + suffix
            if len(compact) <= max_chars:
                return compact
        return "…" + display[-(max_chars - 1):]

    def _show_numeric_placeholder(self, key: str) -> None:
        state = self.numeric_entry_states.get(key)
        if state is None:
            return
        entry, display_var, placeholder_text = state
        if display_var.get():
            return
        self.numeric_placeholder_keys.add(key)
        previous_validation = entry.cget("validate")
        entry.configure(validate="none")
        display_var.set(placeholder_text)
        entry.configure(fg=_PLACEHOLDER_COLOR, validate=previous_validation)

    def _clear_numeric_placeholder(self, key: str) -> None:
        if key not in self.numeric_placeholder_keys:
            return
        entry, display_var, _placeholder_text = self.numeric_entry_states[key]
        previous_validation = entry.cget("validate")
        entry.configure(validate="none")
        self.numeric_placeholder_keys.discard(key)
        display_var.set("")
        entry.configure(fg=_SUBTITLE_COLOR, validate=previous_validation)
        entry.icursor(0)

    def _begin_numeric_edit_from_key(self, event, key: str) -> None:
        entry = self.field_entries.get(key)
        if entry is not None and entry.cget("state") != "normal":
            return
        if event.char or event.keysym in {"BackSpace", "Delete"}:
            self._clear_numeric_placeholder(key)

    def _begin_numeric_edit_from_click(self, _event, key: str) -> None:
        entry = self.field_entries.get(key)
        if entry is not None and entry.cget("state") != "normal":
            return
        self._clear_numeric_placeholder(key)

    def _sync_numeric_value(self, key: str, display_var: tk.StringVar) -> None:
        if key in self.numeric_placeholder_keys:
            return
        value = display_var.get()
        self.field_vars[key].set(value)
        if not value:
            self.dialog.after_idle(
                lambda field_key=key: self._show_numeric_placeholder(field_key)
            )

    def _new_dialog_button(
        self,
        parent,
        text: str,
        command: Callable[[], None],
        *,
        kind: str = "secondary",
        padx: int = 10,
        pady: int = 5,
        width: int | None = None,
        default: str | None = None,
    ):
        return create_dialog_action_button(
            parent,
            text,
            command,
            font=self.action_font,
            kind=kind,
            padx=padx,
            pady=pady,
            width=width,
            default=default,
            dialog_layout=self._dialog_layout,
        )

    def _render_section(self, parent, section_key: str) -> None:
        section = tk.Frame(
            parent,
            bg=_BG_COLOR,
        )
        section.pack(fill="x")

        group = tk.Frame(
            section,
            bg=_PANEL_COLOR,
            padx=0,
            pady=0,
            highlightthickness=1,
            highlightbackground=DIALOG_PANEL_BORDER,
            highlightcolor=DIALOG_PANEL_BORDER,
        )
        group.pack(fill="x")

        fields = [
            field
            for field in PREFERENCE_FIELDS
            if field.section == section_key
        ]
        for index, field in enumerate(fields):
            self._render_field(group, field, last=index == len(fields) - 1)

    def _render_field(self, section, field: PreferenceSpec, *, last: bool) -> None:
        key = field.key
        value_type = field.value_type
        self.field_page_keys[key] = field.section
        compact_path = value_type in {
            PreferenceValueType.PATH,
            PreferenceValueType.PATH_CREATE,
        }
        row = tk.Frame(
            section,
            bg=_PANEL_COLOR,
            padx=self._layout_policy.row_pad_x,
            pady=self._layout_policy.row_pad_y,
        )
        row.pack(fill="x")
        row.grid_columnconfigure(0, weight=1)

        text_column = tk.Frame(row, bg=_PANEL_COLOR)
        text_column.grid(
            row=0,
            column=0,
            sticky="ew",
        )
        tk.Label(
            text_column,
            text=field.label,
            font=self.body_font,
            fg=_SUBTITLE_COLOR,
            bg=_PANEL_COLOR,
            anchor="w",
        ).pack(anchor="w")

        var = tk.StringVar(master=self.dialog, value=self.form.state.values[key])
        self.field_vars[key] = var
        entry_width = (
            _NUMERIC_ENTRY_WIDTH
            if value_type in {PreferenceValueType.INT, PreferenceValueType.FLOAT}
            else self._layout_policy.text_entry_width
        )
        entry_var = var
        placeholder_text = preference_placeholder_text(field)
        if placeholder_text:
            entry_var = tk.StringVar(master=self.dialog, value=var.get())
            if not var.get():
                entry_var.set(placeholder_text)
                self.numeric_placeholder_keys.add(key)
        elif compact_path:
            entry_var = tk.StringVar(
                master=self.dialog, value=self._compact_directory_path(var.get())
            )
            var.trace_add(
                "write",
                lambda *_args, source=var, display=entry_var: display.set(
                    self._compact_directory_path(source.get())
                ),
            )
        self.field_display_vars[key] = entry_var

        entry_parent = tk.Frame(row, bg=_PANEL_COLOR)
        entry_parent.grid(
            row=1,
            column=0,
            sticky="ew" if compact_path else "w",
            pady=(self._layout_policy.control_row_top_pad_y, 0),
        )
        entry_parent.grid_columnconfigure(0, weight=1)
        if compact_path:
            entry_parent.grid_columnconfigure(1, weight=0)

        entry = tk.Entry(
            entry_parent,
            textvariable=entry_var,
            font=self.body_font,
            bg=DARK_THEME.entry_background,
            fg=(
                _PLACEHOLDER_COLOR
                if key in self.numeric_placeholder_keys
                else _SUBTITLE_COLOR
            ),
            insertbackground=_SUBTITLE_COLOR,
            relief="flat",
            highlightthickness=1,
            highlightbackground=DARK_THEME.entry_border,
            highlightcolor=DARK_THEME.entry_focus_border,
            width=entry_width,
            state="readonly" if compact_path else "normal",
            readonlybackground=DARK_THEME.entry_background,
            disabledbackground=DARK_THEME.entry_background,
            disabledforeground=_SUBTITLE_COLOR,
            validate="none" if compact_path else "key",
            validatecommand=(self.numeric_entry_validator, value_type.value, "%P"),
        )
        self.field_entries[key] = entry
        self.field_entry_states[key] = "readonly" if compact_path else "normal"

        if placeholder_text:
            self.numeric_entry_states[key] = (entry, entry_var, placeholder_text)
            entry_var.trace_add(
                "write",
                lambda *_args, field_key=key, source=entry_var: self._sync_numeric_value(
                    field_key, source
                ),
            )
            entry.bind(
                "<KeyPress>",
                lambda event, field_key=key: self._begin_numeric_edit_from_key(
                    event, field_key
                ),
                add="+",
            )
            entry.bind(
                "<Button-1>",
                lambda event, field_key=key: self._begin_numeric_edit_from_click(
                    event, field_key
                ),
                add="+",
            )
            entry.bind(
                "<FocusOut>",
                lambda _event, field_key=key: self._show_numeric_placeholder(
                    field_key
                ),
                add="+",
            )

        var.trace_add(
            "write",
            lambda *_args, field_key=key: self._on_field_changed(field_key),
        )
        entry.bind(
            "<FocusIn>",
            lambda _event, field_key=key: self._on_field_focused(field_key),
            add="+",
        )
        entry.bind(
            "<FocusOut>",
            lambda _event, field_key=key: self._on_field_blurred(field_key),
            add="+",
        )
        if compact_path:
            entry.grid(row=0, column=0, sticky="ew")
        else:
            entry.pack(side="left")

        if value_type in {PreferenceValueType.PATH, PreferenceValueType.PATH_CREATE}:
            browse_button = self._new_dialog_button(
                entry_parent,
                "Browse",
                lambda field_key=key, title=field.label: self._choose_directory(
                    field_key, title
                ),
                padx=10,
            )
            if compact_path:
                browse_button.grid(
                    row=0,
                    column=1,
                    sticky="e",
                    padx=(_CONTROL_GAP_X, 0),
                )
            else:
                browse_button.pack(side="left", padx=(_CONTROL_GAP_X, 0))
            self.field_browse_buttons[key] = browse_button

        single_line_hint = compact_path
        hint_label = tk.Label(
            text_column,
            text=field.hint,
            font=self.small_font,
            fg=_INSTRUCTION_COLOR,
            bg=_PANEL_COLOR,
            justify="left",
            anchor="w",
            wraplength=(
                0 if single_line_hint else self._layout_policy.wrap_length
            ),
        )
        hint_label.pack(anchor="w", fill="x", pady=(3, 0))
        if not single_line_hint:
            text_column.bind(
                "<Configure>",
                lambda event, label=hint_label: self._resize_hint(event, label),
                add="+",
            )

        if not last:
            separator = tk.Frame(section, bg=DARK_THEME.entry_border, height=1)
            separator.pack(fill="x")

    @staticmethod
    def _resize_hint(event, label) -> None:
        wraplength = max(200, event.width - 4)
        if int(label.cget("wraplength")) != wraplength:
            label.configure(wraplength=wraplength)

    def _choose_directory(self, key: str, title: str) -> None:
        preflight = directory_selection_preflight(
            self.desktop_services,
            platform_runtime=self.platform_runtime,
        )
        decision = preflight.decision
        if not decision.allows_execution:
            self._set_feedback(decision.explanation, MessageKind.WARNING)
            return
        if decision.state is FeatureState.DEGRADED:
            self._set_feedback(decision.explanation, MessageKind.WARNING)

        var = self.field_vars[key]
        initial_dir = os.path.expanduser(var.get().strip() or "~")
        if not os.path.isdir(initial_dir):
            initial_dir = os.path.dirname(initial_dir)
        if not os.path.isdir(initial_dir):
            initial_dir = os.path.expanduser("~")
        try:
            selection = choose_authorized_directory(
                preflight,
                self.desktop_services,
                title=title,
                initial_dir=initial_dir,
                parent=self.dialog,
            )
        except DesktopServiceError as exc:
            self._set_feedback(str(exc), MessageKind.ERROR)
            return
        if selection:
            var.set(selection.path)

    def _new_page_tab(self, parent, page_key: str, label: str) -> tk.Label:
        tab = tk.Label(
            parent,
            text=label,
            font=self.body_font,
            bg=_BG_COLOR,
            fg=_INSTRUCTION_COLOR,
            padx=self._layout_policy.tab_pad_x,
            pady=self._layout_policy.tab_pad_y,
            cursor="hand2",
            takefocus=True,
            highlightthickness=self._layout_policy.tab_highlight_thickness,
            highlightbackground=_BG_COLOR,
            highlightcolor=DARK_THEME.entry_focus_border,
        )

        def _invoke(_event=None, key=page_key):
            self._show_page(key)
            return "break"

        tab.bind("<Button-1>", _invoke)
        tab.bind("<Return>", _invoke)
        tab.bind("<space>", _invoke)
        return tab

    def _sync_feedback_to_current_state(self) -> None:
        if (
            getattr(self, "error_label", None) is None
            or getattr(self, "rendered_state", None) is None
        ):
            return
        if not self.rendered_state.message and self._feedback_override is not None:
            message, color = self._feedback_override
            self.error_label.config(text=message, fg=color)
            return
        self._set_feedback(
            self.rendered_state.message,
            self.rendered_state.message_kind,
        )

    def _bind_page_mousewheel(self, widget) -> None:
        widget.bind("<MouseWheel>", self._scroll_page_content, add="+")
        widget.bind("<Button-4>", self._scroll_page_content, add="+")
        widget.bind("<Button-5>", self._scroll_page_content, add="+")
        for child in widget.winfo_children():
            self._bind_page_mousewheel(child)

    def _sync_page_scrollbar(self) -> None:
        if (
            self.page_canvas is None
            or self.page_stack is None
            or self.page_scrollbar is None
        ):
            return
        width = max(1, self.page_canvas.winfo_width())
        active_page = self.pages.get(self.active_page_key or "")
        content_height = (
            active_page.winfo_reqheight()
            if active_page is not None
            else self.page_stack.winfo_reqheight()
        )
        self.page_canvas.configure(scrollregion=(0, 0, width, content_height))
        visible_height = self.page_canvas.winfo_height()
        if content_height > visible_height + 1:
            if not self.page_scrollbar.winfo_manager():
                self.page_scrollbar.pack(side="right", fill="y")
        else:
            if self.page_scrollbar.winfo_manager():
                self.page_scrollbar.pack_forget()
            self.page_canvas.yview_moveto(0)

    def _resize_page_canvas_window(self, event) -> None:
        if self.page_canvas is None or self.page_canvas_window is None:
            return
        self.page_canvas.itemconfigure(self.page_canvas_window, width=event.width)
        self._sync_page_scrollbar()

    def _draw_page_scrollbar_thumb(self) -> None:
        if self.page_scrollbar is None:
            return
        height = max(1, self.page_scrollbar.winfo_height())
        first, last = self.page_scrollbar_fraction
        visible_fraction = max(0.0, min(1.0, last - first))
        if visible_fraction >= 1.0:
            if self.page_scrollbar_thumb is not None:
                self.page_scrollbar.delete(self.page_scrollbar_thumb)
                self.page_scrollbar_thumb = None
            return

        thumb_height = max(
            _SCROLL_THUMB_MIN_HEIGHT,
            int(round(height * visible_fraction)),
        )
        travel = max(1, height - thumb_height)
        y0 = int(round(max(0.0, min(1.0, first)) * travel))
        y1 = min(height, y0 + thumb_height)
        x = _SCROLLBAR_WIDTH // 2
        if self.page_scrollbar_thumb is None:
            self.page_scrollbar_thumb = self.page_scrollbar.create_line(
                x,
                y0,
                x,
                y1,
                fill=_SCROLL_THUMB_COLOR,
                width=_SCROLL_THUMB_WIDTH,
                capstyle="round",
            )
        else:
            self.page_scrollbar.coords(self.page_scrollbar_thumb, x, y0, x, y1)

    def _set_page_scrollbar(self, first: str, last: str) -> None:
        self.page_scrollbar_fraction = (float(first), float(last))
        self._draw_page_scrollbar_thumb()

    def _scroll_page_content(self, event):
        if (
            self.page_canvas is None
            or self.page_scrollbar is None
            or not self.page_scrollbar.winfo_manager()
        ):
            return None
        units = vertical_scroll_units(event)
        if units is not None:
            self.page_canvas.yview_scroll(units, "units")
        return "break"

    def _start_page_scrollbar_drag(self, event):
        if self.page_canvas is None or self.page_scrollbar is None:
            return "break"
        first, last = self.page_scrollbar_fraction
        height = max(1, self.page_scrollbar.winfo_height())
        visible_fraction = max(0.0, min(1.0, last - first))
        thumb_height = max(
            _SCROLL_THUMB_MIN_HEIGHT,
            int(round(height * visible_fraction)),
        )
        travel = max(1, height - thumb_height)
        thumb_top = int(round(first * travel))
        thumb_bottom = thumb_top + thumb_height
        if thumb_top <= event.y <= thumb_bottom:
            self.page_scroll_drag_offset = event.y - thumb_top
        else:
            self.page_scroll_drag_offset = thumb_height / 2
            self._drag_page_scrollbar(event)
        if self.page_scrollbar_thumb is not None:
            self.page_scrollbar.itemconfigure(
                self.page_scrollbar_thumb,
                fill=_SCROLL_THUMB_ACTIVE_COLOR,
            )
        return "break"

    def _drag_page_scrollbar(self, event):
        if self.page_canvas is None or self.page_scrollbar is None:
            return "break"
        first, last = self.page_scrollbar_fraction
        height = max(1, self.page_scrollbar.winfo_height())
        visible_fraction = max(0.0, min(1.0, last - first))
        thumb_height = max(
            _SCROLL_THUMB_MIN_HEIGHT,
            int(round(height * visible_fraction)),
        )
        travel = max(1, height - thumb_height)
        thumb_top = max(0, min(travel, event.y - self.page_scroll_drag_offset))
        self.page_canvas.yview_moveto(thumb_top / travel)
        return "break"

    def _end_page_scrollbar_drag(self, _event):
        if self.page_scrollbar is not None and self.page_scrollbar_thumb is not None:
            self.page_scrollbar.itemconfigure(
                self.page_scrollbar_thumb,
                fill=_SCROLL_THUMB_COLOR,
            )
        return "break"

    def _resize_page_scrollbar(self, _event) -> None:
        self._draw_page_scrollbar_thumb()


    def _show_page(self, page_key: str) -> None:
        page = self.pages.get(page_key)
        if page is None:
            return
        self.active_page_key = page_key
        for key, candidate_page in self.pages.items():
            if key == page_key:
                candidate_page.tkraise()
        for key, tab in self.page_buttons.items():
            active = key == page_key
            tab.config(
                bg=_PANEL_COLOR if active else _BG_COLOR,
                fg=_TITLE_COLOR if active else _INSTRUCTION_COLOR,
                highlightbackground=(
                    DARK_THEME.entry_border if active else _BG_COLOR
                ),
                highlightcolor=(
                    DARK_THEME.entry_focus_border
                    if active
                    else _BG_COLOR
                ),
            )
        self._sync_feedback_to_current_state()
        if self.page_canvas is not None:
            self.page_canvas.yview_moveto(0)
        self.dialog.after_idle(self._sync_page_scrollbar)

    def _build(self) -> None:
        body = tk.Frame(
            self.container,
            bg=_BG_COLOR,
            padx=self._layout_policy.body_pad_x,
            pady=DIALOG_BODY_PAD_Y,
        )
        body.pack(fill="both", expand=True)

        self.tab_bar = tk.Frame(body, bg=_BG_COLOR)
        self.tab_bar.pack(
            fill="x",
            pady=(0, self._layout_policy.tab_bottom_pad_y),
        )
        for page_key, tab_label in _PREFERENCE_PAGES:
            tab = self._new_page_tab(self.tab_bar, page_key, tab_label)
            tab.pack(side="left", padx=(0, _TAB_GAP_X))
            self.page_buttons[page_key] = tab

        self.button_row = tk.Frame(body, bg=_BG_COLOR)
        # Pack the action row before the page stack so a height-limited
        # Windows dialog shrinks form content first instead of clipping
        # Apply/Cancel off the bottom edge.
        self.button_row.pack(
            side="bottom",
            fill="x",
            pady=(self._layout_policy.button_row_top_pad_y, 0),
        )

        cancel_button = self._new_dialog_button(
            self.button_row,
            "Cancel",
            self.cancel,
            padx=12,
            pady=6,
        )
        self.apply_button = self._new_dialog_button(
            self.button_row,
            "Apply",
            self.apply,
            kind="primary",
            padx=16,
            pady=6,
            default="active",
        )

        self.apply_button.pack(side="right")
        cancel_button.pack(side="right", padx=(0, 8))

        self.feedback_frame = tk.Frame(self.button_row, bg=_BG_COLOR)
        self.feedback_frame.pack(side="left", fill="x", expand=True)
        self.error_label = tk.Label(
            self.feedback_frame,
            text="",
            font=self.small_font,
            fg=DARK_THEME.error_text,
            bg=_BG_COLOR,
            anchor="w",
            justify="left",
            wraplength=self._layout_policy.notice_wrap_length,
        )
        self.error_label.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(_INLINE_FEEDBACK_PAD_X, _INLINE_FEEDBACK_PAD_X),
        )
        self.feedback_frame.bind(
            "<Configure>",
            lambda event: self.error_label.config(
                wraplength=max(120, event.width - 2 * _INLINE_FEEDBACK_PAD_X)
            ),
            add="+",
        )

        self.page_scroll_shell = tk.Frame(body, bg=_BG_COLOR)
        self.page_scroll_shell.pack(side="top", fill="both", expand=True)
        self.page_canvas = tk.Canvas(
            self.page_scroll_shell,
            bg=_BG_COLOR,
            borderwidth=0,
            highlightthickness=0,
            yscrollcommand=lambda *_args: None,
        )
        self.page_scrollbar = tk.Canvas(
            self.page_scroll_shell,
            bg=_BG_COLOR,
            borderwidth=0,
            highlightthickness=0,
            width=_SCROLLBAR_WIDTH,
            cursor="sb_v_double_arrow",
        )
        self.page_canvas.configure(yscrollcommand=self._set_page_scrollbar)
        self.page_canvas.pack(side="left", fill="both", expand=True)

        self.page_stack = tk.Frame(self.page_canvas, bg=_BG_COLOR)
        self.page_canvas_window = self.page_canvas.create_window(
            (0, 0),
            window=self.page_stack,
            anchor="nw",
        )
        self.page_stack.grid_rowconfigure(0, weight=1)
        self.page_stack.grid_columnconfigure(0, weight=1)
        for page_key, _tab_label in _PREFERENCE_PAGES:
            page = tk.Frame(self.page_stack, bg=_BG_COLOR)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[page_key] = page
            self._render_section(page, page_key)
        self.dialog.update_idletasks()
        max_page_width = max(
            (page.winfo_reqwidth() for page in self.pages.values()),
            default=1,
        )
        max_page_height = max(
            (page.winfo_reqheight() for page in self.pages.values()),
            default=1,
        )
        self.page_stack.configure(width=max_page_width, height=max_page_height)
        self.page_canvas.configure(width=max_page_width, height=max_page_height)
        self.page_stack.grid_propagate(False)
        self.page_stack.bind(
            "<Configure>",
            lambda _event: self._sync_page_scrollbar(),
            add="+",
        )
        self.page_canvas.bind("<Configure>", self._resize_page_canvas_window, add="+")
        self.page_canvas.bind("<MouseWheel>", self._scroll_page_content, add="+")
        self.page_canvas.bind("<Button-4>", self._scroll_page_content, add="+")
        self.page_canvas.bind("<Button-5>", self._scroll_page_content, add="+")
        self.page_scrollbar.bind("<Configure>", self._resize_page_scrollbar, add="+")
        self.page_scrollbar.bind(
            "<ButtonPress-1>",
            self._start_page_scrollbar_drag,
            add="+",
        )
        self.page_scrollbar.bind("<B1-Motion>", self._drag_page_scrollbar, add="+")
        self.page_scrollbar.bind(
            "<ButtonRelease-1>",
            self._end_page_scrollbar_drag,
            add="+",
        )
        self._bind_page_mousewheel(self.page_stack)
        self._show_page(_PREFERENCE_PAGES[0][0])
        self._sync_page_scrollbar()

        self.form_ready = True
        self._render_form_state(self.form.state, focus_invalid=True)

    def _set_feedback(self, message: str, message_kind: MessageKind) -> None:
        if not message:
            self.error_label.config(text="")
            return

        color = (
            DARK_THEME.title
            if message_kind is MessageKind.WARNING
            else DARK_THEME.error_text
        )
        self.error_label.config(text=message, fg=color)

    def _set_apply_enabled(self, enabled: bool) -> None:
        set_dialog_action_button(self.apply_button, enabled=enabled)

    def _set_field_lock(self, invalid_key: str | None) -> None:
        for key, entry in self.field_entries.items():
            enabled = invalid_key is None or key == invalid_key
            entry.config(
                state=self.field_entry_states[key] if enabled else "readonly",
                highlightbackground=(
                    DARK_THEME.invalid_border
                    if key == invalid_key
                    else DARK_THEME.entry_border
                ),
                highlightcolor=(
                    DARK_THEME.invalid_border
                    if key == invalid_key
                    else DARK_THEME.entry_focus_border
                ),
            )
        for key, browse_button in self.field_browse_buttons.items():
            set_dialog_action_button(
                browse_button,
                enabled=invalid_key is None or key == invalid_key,
            )

    def _focus_invalid_field(
        self, key: str, *, select_value: bool = False
    ) -> None:
        page_key = self.field_page_keys.get(key)
        if page_key is not None:
            self._show_page(page_key)

        def focus() -> None:
            entry = self.field_entries.get(key)
            if entry is None or not entry.winfo_exists():
                return
            entry.focus_set()
            if (
                select_value
                and self.field_entry_states.get(key) == "normal"
                and key not in self.numeric_placeholder_keys
            ):
                entry.selection_range(0, "end")

        self.dialog.after_idle(focus)

    def _render_form_state(
        self,
        state: PreferencesFormState,
        *,
        preferred_key: str | None = None,
        focus_invalid: bool = False,
    ) -> None:
        self.rendered_state = state
        previous_invalid_key = self.rendered_invalid_key
        self.rendered_invalid_key = state.invalid_key
        locked_key = state.invalid_key if state.form_locked else None
        self._set_field_lock(locked_key)
        self._set_apply_enabled(state.apply_enabled)
        self._sync_feedback_to_current_state()

        if state.invalid_key is not None and (
            focus_invalid or state.invalid_key != previous_invalid_key
        ):
            self._focus_invalid_field(
                state.invalid_key,
                select_value=(
                    focus_invalid or state.invalid_key != preferred_key
                ),
            )

    def _on_field_focused(self, key: str) -> None:
        if self.form_ready:
            self.form.focus(key)

    def _on_field_changed(self, key: str) -> None:
        if not self.form_ready or self.rendering_state:
            return
        self._feedback_override = None
        state = self.form.change(key, self.field_vars[key].get())
        self._render_form_state(state, preferred_key=key)

    def _sync_field_value(self, key: str, value: str) -> None:
        was_rendering_state = self.rendering_state
        self.rendering_state = True
        try:
            if key in self.numeric_entry_states:
                entry, display_var, _placeholder = self.numeric_entry_states[key]
                if value:
                    self.numeric_placeholder_keys.discard(key)
                    if display_var.get() != value:
                        display_var.set(value)
                    entry.configure(fg=_SUBTITLE_COLOR)
                elif key not in self.numeric_placeholder_keys:
                    if display_var.get():
                        display_var.set("")
                    self._show_numeric_placeholder(key)
            elif self.field_vars[key].get() != value:
                self.field_vars[key].set(value)
        finally:
            self.rendering_state = was_rendering_state

    def _on_field_blurred(self, key: str) -> None:
        if not self.form_ready:
            return
        state = self.form.blur(key)
        self._sync_field_value(key, state.values[key])
        self._render_form_state(state, preferred_key=key)

    def apply(self) -> bool:
        self._feedback_override = None
        state, preferences = self.form.attempt_apply()
        self._render_form_state(state, focus_invalid=True)
        if preferences is None:
            return False

        for key in self.numeric_entry_states:
            self._show_numeric_placeholder(key)
        workflow = getattr(self, "workflow", None)
        if workflow is None:
            workflow = PreferencesDialogWorkflow(
                save_preferences_fn=save_preferences,
                apply_preferences_to_env_fn=apply_preferences_to_env,
            )
        result = workflow.apply(preferences)
        if not result.succeeded:
            self._set_feedback(result.error or "", MessageKind.ERROR)
            return False
        self.preferences = result.preferences
        self._feedback_override = (
            "Preferences saved.",
            DARK_THEME.primary_button,
        )
        self._sync_feedback_to_current_state()
        on_applied = getattr(self, "on_applied", None)
        if on_applied is not None and result.preferences is not None:
            on_applied(result.preferences)
        return True

    def cancel(self) -> None:
        self.discard_changes()
        on_cancel = getattr(self, "on_cancel", None)
        if on_cancel is not None:
            on_cancel()

    @property
    def has_unsaved_changes(self) -> bool:
        """Return whether the visible form differs from the last saved state."""
        preferences = getattr(self, "preferences", None)
        form = getattr(self, "form", None)
        if preferences is None or form is None:
            return False
        return dict(form.state.values) != preferences.as_dict()

    def discard_changes(self) -> None:
        """Restore the last saved values without destroying this panel."""
        preferences = getattr(self, "preferences", None)
        if preferences is None:
            return
        self.form = PreferencesFormController(preferences)
        self.rendering_state = True
        try:
            for key, value in preferences.items():
                self._sync_field_value(key, value)
        finally:
            self.rendering_state = False
        self.rendered_invalid_key = None
        self._feedback_override = None
        self._render_form_state(self.form.state)

    def focus_content(self) -> None:
        """Move keyboard focus into the active embedded Preferences view."""
        if self.form.state.invalid_key is not None:
            self._focus_invalid_field(self.form.state.invalid_key, select_value=True)
            return
        if self.apply_button is not None:
            self.apply_button.focus_set()
