"""Tk presentation adapter for the Advanced Settings form controller."""

from __future__ import annotations

import os
import sys
import tkinter as tk
from typing import Callable

from caveviewer.gui.advanced_settings import (
    ADVANCED_SETTING_COLUMNS,
    ADVANCED_SETTING_FIELDS,
    AdvancedSettingsSaveError,
    SettingSpec,
    ValueType,
    advanced_setting_placeholder_text,
    apply_advanced_settings_to_env,
    load_advanced_settings,
    save_advanced_settings,
)
from caveviewer.gui.advanced_settings_form import (
    AdvancedSettingsFormController,
    AdvancedSettingsFormState,
    MessageKind,
)
from caveviewer.gui.platform import DesktopServices, get_desktop_services
from caveviewer.gui.tk_theme import DARK_THEME


_BG_COLOR = DARK_THEME.background
_TITLE_COLOR = DARK_THEME.title
_SUBTITLE_COLOR = DARK_THEME.body_text
_INSTRUCTION_COLOR = DARK_THEME.secondary_text
_BUTTON_BG = DARK_THEME.primary_button
_BUTTON_HOVER_BG = DARK_THEME.primary_button_hover
_BUTTON_BORDER_COLOR = DARK_THEME.primary_button_border
_BUTTON_FG = DARK_THEME.primary_button_text
_BORDER_COLOR = DARK_THEME.border

_LINUX_LAYOUT = sys.platform.startswith("linux")
_TWO_COLUMN_LAYOUT = True
_WRAP_LENGTH = 620 if sys.platform == "win32" else 340
_TEXT_ENTRY_WIDTH = 42 if sys.platform == "win32" else 22
_NUMERIC_ENTRY_WIDTH = 12
_PLACEHOLDER_COLOR = DARK_THEME.placeholder_text
_ERROR_COLOR = DARK_THEME.error_text
_WARNING_COLOR = _TITLE_COLOR
_BODY_PAD_X = 18 if sys.platform == "darwin" else (32 if sys.platform == "win32" else 24)
_SECTION_GAP = 44 if sys.platform == "win32" else 18
_MIN_WIDTH = (
    1320
    if sys.platform == "win32"
    else 1040
    if sys.platform.startswith("linux")
    else 0
)


class _LabelButton(tk.Label):
    """Keyboard-accessible label button with an explicit enabled state."""

    def __init__(
        self,
        parent,
        *,
        text: str,
        command: Callable[[], None],
        font,
        bg: str,
        fg: str,
        hover_bg: str,
        padx: int,
        pady: int,
        border_color: str,
    ) -> None:
        super().__init__(
            parent,
            text=text,
            font=font,
            bg=bg,
            fg=fg,
            padx=padx,
            pady=pady,
            cursor="hand2",
            takefocus=True,
            highlightthickness=1,
            highlightbackground=border_color,
            highlightcolor=border_color,
        )
        self._command = command
        self._normal_bg = bg
        self._normal_fg = fg
        self._hover_bg = hover_bg
        self._enabled = True
        self.bind("<Button-1>", self._invoke)
        self.bind("<Return>", self._invoke)
        self.bind("<space>", self._invoke)
        self.bind("<Enter>", self._show_hover)
        self.bind("<Leave>", self._clear_hover)

    def _invoke(self, _event=None):
        if self._enabled:
            self._command()
        return "break"

    def _show_hover(self, _event=None) -> None:
        if self._enabled:
            self.config(bg=self._hover_bg)

    def _clear_hover(self, _event=None) -> None:
        self.config(bg=self._normal_bg)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.config(
            bg=self._normal_bg,
            fg=self._normal_fg if self._enabled else _PLACEHOLDER_COLOR,
            cursor="hand2" if self._enabled else "",
            takefocus=self._enabled,
        )


class AdvancedSettingsDialog:
    """Render Advanced Settings state and forward Tk events to the controller."""

    def __init__(
        self,
        parent,
        *,
        ui_font_family: str,
        desktop_services: DesktopServices | None = None,
    ) -> None:
        self.parent = parent
        self.ui_font_family = ui_font_family
        self.desktop_services = desktop_services or get_desktop_services()
        self.settings = load_advanced_settings()
        apply_advanced_settings_to_env(self.settings)
        self.form = AdvancedSettingsFormController(self.settings)

        self.dialog = tk.Toplevel(parent)
        self.dialog.withdraw()
        self.dialog.title("Advanced Settings")
        self.dialog.configure(bg=_BG_COLOR)
        self.dialog.resizable(False, False)
        self.dialog.transient(parent)

        if _LINUX_LAYOUT:
            self.section_font = (ui_font_family, 10)
            self.body_font = (ui_font_family, 10)
            self.small_font = (ui_font_family, 9)
            self.field_gap = 14
            self.entry_pad_y = 6
            self.section_pad_y = 15
        else:
            self.section_font = (ui_font_family, 12)
            self.body_font = (ui_font_family, 12)
            self.small_font = (ui_font_family, 10)
            self.field_gap = 9
            self.entry_pad_y = 4
            self.section_pad_y = 12

        self.field_vars: dict[str, tk.StringVar] = {}
        self.field_entries: dict[str, tk.Entry] = {}
        self.field_display_vars: dict[str, tk.StringVar] = {}
        self.field_entry_states: dict[str, str] = {}
        self.field_browse_buttons: dict[str, _LabelButton] = {}
        self.numeric_entry_states: dict[str, tuple] = {}
        self.numeric_placeholder_keys: set[str] = set()
        self.form_ready = False
        self.rendering_state = False
        self.rendered_invalid_key: str | None = None
        self.apply_button = None
        self.section_row = None
        self.button_row = None
        self.error_label = None

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
    def _compact_directory_path(path: str, max_chars: int = 42) -> str:
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

    def _new_label_button(
        self,
        parent,
        text: str,
        command: Callable[[], None],
        *,
        bg: str = DARK_THEME.secondary_button,
        fg: str = _SUBTITLE_COLOR,
        hover_bg: str = DARK_THEME.secondary_button_hover,
        padx: int = 10,
        pady: int = 5,
        border_color: str = DARK_THEME.secondary_button_border,
    ) -> _LabelButton:
        return _LabelButton(
            parent,
            text=text,
            command=command,
            font=self.small_font,
            bg=bg,
            fg=fg,
            hover_bg=hover_bg,
            padx=padx,
            pady=pady,
            border_color=border_color,
        )

    def _render_section(self, parent, title: str, section_key: str) -> None:
        section = tk.Frame(
            parent,
            bg=_BG_COLOR,
            padx=14,
            pady=self.section_pad_y,
            highlightthickness=1,
            highlightbackground=_BORDER_COLOR,
            highlightcolor=_BORDER_COLOR,
        )
        section.pack(
            fill="both", expand=(section_key == "streaming"), pady=(0, 12)
        )
        tk.Label(
            section,
            text=title,
            font=self.section_font,
            fg=_TITLE_COLOR,
            bg=_BG_COLOR,
        ).pack(anchor="w", pady=(0, 10))

        fields = [
            field
            for field in ADVANCED_SETTING_FIELDS
            if field.section == section_key
        ]
        for field in fields:
            self._render_field(section, field)

    def _render_field(self, section, field: SettingSpec) -> None:
        key = field.key
        row = tk.Frame(section, bg=_BG_COLOR)
        row.pack(fill="x", pady=(0, self.field_gap))
        tk.Label(
            row,
            text=field.label,
            font=self.body_font,
            fg=_SUBTITLE_COLOR,
            bg=_BG_COLOR,
            anchor="w",
        ).pack(anchor="w")

        var = tk.StringVar(master=self.dialog, value=self.form.state.values[key])
        self.field_vars[key] = var
        value_type = field.value_type
        entry_width = (
            _NUMERIC_ENTRY_WIDTH
            if value_type in {ValueType.INT, ValueType.FLOAT}
            else _TEXT_ENTRY_WIDTH
        )
        compact_path = key == "recording_dir"
        entry_var = var
        placeholder_text = advanced_setting_placeholder_text(field)
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

        entry_parent = row
        entry_pack_options = {
            "anchor": "w",
            "pady": (self.entry_pad_y, self.entry_pad_y),
        }
        if value_type in {ValueType.PATH, ValueType.PATH_CREATE}:
            entry_parent = tk.Frame(row, bg=_BG_COLOR)
            entry_parent.pack(
                fill="x", pady=(self.entry_pad_y, self.entry_pad_y)
            )
            entry_pack_options = {"side": "left", "fill": "x", "expand": True}

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
        entry.pack(**entry_pack_options)

        if value_type in {ValueType.PATH, ValueType.PATH_CREATE}:
            browse_button = self._new_label_button(
                entry_parent,
                "Browse",
                lambda field_key=key, title=field.label: self._choose_directory(
                    field_key, title
                ),
                padx=8,
            )
            browse_button.pack(side="left", padx=(8, 0))
            self.field_browse_buttons[key] = browse_button

        single_line_hint = key == "recording_dir"
        hint_label = tk.Label(
            row,
            text=field.hint,
            font=self.small_font,
            fg=_INSTRUCTION_COLOR,
            bg=_BG_COLOR,
            justify="left",
            anchor="w",
            wraplength=0 if single_line_hint else _WRAP_LENGTH,
        )
        hint_label.pack(anchor="w", fill="x")
        if _LINUX_LAYOUT and not single_line_hint:
            row.bind(
                "<Configure>",
                lambda event, label=hint_label: self._resize_hint(event, label),
                add="+",
            )

    @staticmethod
    def _resize_hint(event, label) -> None:
        wraplength = max(200, event.width - 4)
        if int(label.cget("wraplength")) != wraplength:
            label.configure(wraplength=wraplength)

    def _choose_directory(self, key: str, title: str) -> None:
        var = self.field_vars[key]
        initial_dir = os.path.expanduser(var.get().strip() or "~")
        if not os.path.isdir(initial_dir):
            initial_dir = os.path.dirname(initial_dir)
        if not os.path.isdir(initial_dir):
            initial_dir = os.path.expanduser("~")
        selection = self.desktop_services.choose_directory(
            title=title, initial_dir=initial_dir, parent=self.dialog
        )
        if selection:
            var.set(selection.path)

    def _build(self) -> None:
        body = tk.Frame(
            self.dialog, bg=_BG_COLOR, padx=_BODY_PAD_X, pady=18
        )
        body.pack(fill="both", expand=True)
        self.section_row = tk.Frame(body, bg=_BG_COLOR)
        self.section_row.pack(fill="both", expand=True, pady=(0, 10))

        for column_index, sections in enumerate(ADVANCED_SETTING_COLUMNS):
            column = tk.Frame(self.section_row, bg=_BG_COLOR)
            if _TWO_COLUMN_LAYOUT:
                half_gap = _SECTION_GAP // 2
                pad_left = half_gap if column_index > 0 else 0
                pad_right = (
                    half_gap
                    if column_index < len(ADVANCED_SETTING_COLUMNS) - 1
                    else 0
                )
                self.section_row.grid_columnconfigure(
                    column_index,
                    weight=1,
                    uniform="advanced_settings_column",
                )
                column.grid(
                    row=0,
                    column=column_index,
                    sticky="nsew",
                    padx=(pad_left, pad_right),
                )
            else:
                column.pack(fill="x")
            for section_key, section_title in sections:
                self._render_section(column, section_title, section_key)

        self.button_row = tk.Frame(body, bg=_BG_COLOR)
        error_parent = self.button_row if _LINUX_LAYOUT else body
        self.error_label = tk.Label(
            error_parent,
            text="",
            font=self.small_font,
            fg=_ERROR_COLOR,
            bg=_BG_COLOR,
            justify="left",
            anchor="w",
            wraplength=620 if _LINUX_LAYOUT else _WRAP_LENGTH,
        )
        if _LINUX_LAYOUT:
            self.button_row.pack(fill="x")
            self.error_label.pack(
                side="left", fill="x", expand=True, padx=(0, 12)
            )
        else:
            self.error_label.pack(anchor="w", pady=(4, 10))
            self.button_row.pack(fill="x")

        if sys.platform == "darwin":
            cancel_button = self._new_label_button(
                self.button_row,
                "Cancel",
                self.cancel,
                padx=12,
                pady=6,
            )
            self.apply_button = self._new_label_button(
                self.button_row,
                "Apply",
                self.apply,
                bg=_BUTTON_BG,
                fg=_BUTTON_FG,
                hover_bg=_BUTTON_HOVER_BG,
                padx=16,
                pady=6,
                border_color=_BUTTON_BORDER_COLOR,
            )
        else:
            cancel_button = tk.Button(
                self.button_row,
                text="Cancel",
                command=self.cancel,
                font=self.small_font,
                bg=DARK_THEME.secondary_button,
                fg=_SUBTITLE_COLOR,
                activebackground=DARK_THEME.secondary_button_hover,
                activeforeground=_SUBTITLE_COLOR,
                relief="flat",
                borderwidth=1,
                highlightthickness=1,
                highlightbackground=DARK_THEME.secondary_button_border,
                highlightcolor=DARK_THEME.secondary_button_border,
                padx=12,
                pady=6,
                cursor="hand2",
            )
            self.apply_button = tk.Button(
                self.button_row,
                text="Apply",
                command=self.apply,
                font=self.small_font,
                bg=_BUTTON_BG,
                fg=_BUTTON_FG,
                activebackground=_BUTTON_HOVER_BG,
                activeforeground=_BUTTON_FG,
                relief="flat",
                borderwidth=1,
                highlightthickness=1,
                highlightbackground=_BUTTON_BORDER_COLOR,
                highlightcolor=_BUTTON_BORDER_COLOR,
                padx=16,
                pady=6,
                cursor="hand2",
                default="active",
            )

        self.apply_button.pack(side="right")
        cancel_button.pack(side="right", padx=(0, 8))
        self.form_ready = True
        self._render_form_state(self.form.state, focus_invalid=True)

        self.dialog.protocol("WM_DELETE_WINDOW", self.cancel)
        self.dialog.bind("<Escape>", lambda _event: self.cancel())
        self.dialog.bind("<Return>", lambda _event: self.apply())

    def _set_feedback(self, message: str, message_kind: MessageKind) -> None:
        self.error_label.config(
            text=message,
            fg=(
                _WARNING_COLOR
                if message_kind is MessageKind.WARNING
                else _ERROR_COLOR
            ),
        )

    def _set_apply_enabled(self, enabled: bool) -> None:
        if isinstance(self.apply_button, _LabelButton):
            self.apply_button.set_enabled(enabled)
        else:
            self.apply_button.config(state="normal" if enabled else "disabled")

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
            browse_button.set_enabled(invalid_key is None or key == invalid_key)

    def _focus_invalid_field(
        self, key: str, *, select_value: bool = False
    ) -> None:
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
        state: AdvancedSettingsFormState,
        *,
        preferred_key: str | None = None,
        focus_invalid: bool = False,
    ) -> None:
        previous_invalid_key = self.rendered_invalid_key
        self.rendered_invalid_key = state.invalid_key
        locked_key = state.invalid_key if state.form_locked else None
        self._set_field_lock(locked_key)
        self._set_apply_enabled(state.apply_enabled)
        self._set_feedback(state.message, state.message_kind)

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
        state = self.form.change(key, self.field_vars[key].get())
        self._render_form_state(state, preferred_key=key)

    def _sync_field_value(self, key: str, value: str) -> None:
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
            self.rendering_state = False

    def _on_field_blurred(self, key: str) -> None:
        if not self.form_ready:
            return
        state = self.form.blur(key)
        self._sync_field_value(key, state.values[key])
        self._render_form_state(state, preferred_key=key)

    def apply(self) -> None:
        state, settings = self.form.attempt_apply()
        self._render_form_state(state, focus_invalid=True)
        if settings is None:
            return

        for key in self.numeric_entry_states:
            self._show_numeric_placeholder(key)
        try:
            save_advanced_settings(settings)
        except AdvancedSettingsSaveError as exc:
            self._set_feedback(str(exc), MessageKind.ERROR)
            return
        self.settings = settings
        apply_advanced_settings_to_env(settings)
        self.dialog.destroy()

    def cancel(self) -> None:
        self.dialog.destroy()

    def _natural_height(self) -> int:
        height = 36
        height += self.section_row.winfo_reqheight() + 10
        height += self.button_row.winfo_reqheight()
        return height

    def _apply_geometry(self) -> None:
        geometry_applied = False
        try:
            self.parent.update_idletasks()
            dialog_w = max(self.dialog.winfo_reqwidth(), _MIN_WIDTH)
            dialog_h = self.dialog.winfo_reqheight()
            screen_w = self.dialog.winfo_screenwidth()
            screen_h = self.dialog.winfo_screenheight()
            dialog_w = min(dialog_w, max(320, screen_w - 16))
            parent_x = self.parent.winfo_rootx()
            parent_y = self.parent.winfo_rooty()
            parent_w = self.parent.winfo_width()
            desired_x = parent_x + parent_w - dialog_w + 72
            desired_y = parent_y + 8
            clamped_x = max(8, min(desired_x, screen_w - dialog_w - 8))
            clamped_y = max(8, min(desired_y, screen_h - 328))
            dialog_h = min(dialog_h, max(320, screen_h - clamped_y - 8))
            self.dialog.geometry(
                f"{dialog_w}x{dialog_h}+{clamped_x}+{clamped_y}"
            )
            if _LINUX_LAYOUT:
                for _ in range(2):
                    self.dialog.update_idletasks()
                fitted_height = min(
                    self._natural_height(), max(320, screen_h - clamped_y - 8)
                )
                self.dialog.geometry(
                    f"{dialog_w}x{fitted_height}+{clamped_x}+{clamped_y}"
                )
            geometry_applied = True
        except Exception:
            pass
        if not geometry_applied:
            self.dialog.geometry(
                "+%d+%d"
                % (self.parent.winfo_rootx() + 24, self.parent.winfo_rooty() + 24)
            )

    def show(self) -> None:
        self.dialog.update_idletasks()
        self._apply_geometry()
        self.dialog.deiconify()
        self.dialog.lift(self.parent)
        self.dialog.wait_visibility()
        self.dialog.grab_set()
        self.dialog.focus_force()
        if self.form.state.invalid_key is not None:
            self._focus_invalid_field(
                self.form.state.invalid_key, select_value=True
            )
        else:
            self.apply_button.focus_set()


def show_advanced_settings_dialog(
    parent,
    *,
    ui_font_family: str,
    desktop_services: DesktopServices | None = None,
) -> None:
    """Create and display a non-blocking modal Advanced Settings dialog."""
    AdvancedSettingsDialog(
        parent,
        ui_font_family=ui_font_family,
        desktop_services=desktop_services,
    ).show()
