"""Tk presentation adapter for the Preferences form controller."""

from __future__ import annotations

import os
import tkinter as tk
from typing import TYPE_CHECKING, Callable

from caveviewer.core.preferences.transfer import PREFERENCES_EXPORT_FILENAME
from caveviewer.storage_paths import default_downloads_dir
from caveviewer.gui.preferences import (
    PREFERENCE_FIELDS,
    Preferences,
    PreferenceSpec,
    PreferenceValueType,
    preference_placeholder_text,
    preference_defaults,
    load_preferences,
    save_preferences,
)
from caveviewer.gui.preferences_workflow import PreferencesDialogWorkflow
from caveviewer.gui.preferences_form import (
    PreferencesFormController,
    PreferencesFormState,
    MessageKind,
)
from caveviewer.gui.section_spacing import STANDARD_CONTENT_SECTION_SPACING
from caveviewer.gui.features import FeatureState
from caveviewer.gui.dialog_style import (
    DIALOG_BODY_PAD_Y,
    create_dialog_action_button,
    set_dialog_action_button,
)
from caveviewer.gui.dpi_utils import tk_display_scale
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
from caveviewer.gui.tk_theme import DARK_THEME
from caveviewer.gui.tk_typography import TkTypography, create_tk_typography

if TYPE_CHECKING:
    from caveviewer.gui.platform.runtime import PlatformRuntime


_BG_COLOR = DARK_THEME.background
_TITLE_COLOR = DARK_THEME.title
_SUBTITLE_COLOR = DARK_THEME.body_text
_INSTRUCTION_COLOR = DARK_THEME.secondary_text
_BUTTON_BG = DARK_THEME.primary_button
_BUTTON_BORDER_COLOR = DARK_THEME.primary_button_border

_NUMERIC_ENTRY_WIDTH = 6
_SCROLLBAR_GUTTER_X = 18
_PLACEHOLDER_COLOR = DARK_THEME.placeholder_text
_INLINE_FEEDBACK_PAD_X = 10
_CONTROL_GAP_X = 10
_MIN_HINT_WRAP_LENGTH = 200
_HINT_WRAP_INSET = 4
_PREFERENCE_PAGES = (
    ("streaming", "Streaming"),
    ("parsing", "Import"),
    ("storage", "Storage"),
    ("backup", "Backup & restore"),
)
_PREFERENCE_PAGE_KEYS = frozenset(key for key, _label in _PREFERENCE_PAGES)
_PREFERENCE_FIELD_GROUPS = {
    "streaming": (
        (
            "Memory",
            (
                "memory_target_percent",
                "gpu_memory_target_percent",
                "gpu_memory_gb",
            ),
        ),
        ("Loading", ("io_workers", "io_reserved_cpus")),
        (
            "Uploads",
            (
                "upload_chunks_per_frame",
                "upload_groups_per_frame",
                "upload_time_budget_ms",
            ),
        ),
    ),
    "parsing": (
        (
            "Import",
            (
                "chunk_size_meters",
                "max_upload_group_mb",
                "obj_scan_throttle_ms",
                "obj_import_batch_thousands",
            ),
        ),
        (
            "Cache building",
            ("chunk_build_workers", "chunk_build_reserved_cpus"),
        ),
    ),
    "storage": (
        ("Locations", ("recording_dir", "map_library_dir")),
    ),
}
def _preference_field_groups(
    section_key: str,
) -> tuple[tuple[str, tuple[PreferenceSpec, ...]], ...]:
    """Return ordered visual groups without making validation depend on them."""
    section_fields = tuple(
        field for field in PREFERENCE_FIELDS if field.section == section_key
    )
    by_key = {field.key: field for field in section_fields}
    groups: list[tuple[str, tuple[PreferenceSpec, ...]]] = []
    for title, keys in _PREFERENCE_FIELD_GROUPS.get(section_key, ()):
        fields = tuple(
            by_key.pop(key)
            for key in keys
            if key in by_key
        )
        if fields:
            groups.append((title, fields))

    remaining = tuple(field for field in section_fields if field.key in by_key)
    if remaining:
        groups.append(("Other", remaining))
    return tuple(groups)


class PreferenceSectionContainer:
    """Render the standard whitespace grouping around one Preferences group."""

    def __init__(
        self,
        parent,
        *,
        title: str,
        font: tuple,
        px: Callable[[int | float], int],
    ) -> None:
        self._px = px
        self.widget = tk.Frame(parent, bg=_BG_COLOR)
        tk.Label(
            self.widget,
            text=title.upper(),
            font=font,
            fg=_SUBTITLE_COLOR,
            bg=_BG_COLOR,
            anchor="w",
        ).pack(anchor="w")
        self.content = tk.Frame(self.widget, bg=_BG_COLOR)
        self.content.pack(
            fill="x",
            pady=(
                self._px(STANDARD_CONTENT_SECTION_SPACING.heading_to_content_y),
                0,
            ),
        )

    def pack(self, *, first: bool) -> None:
        """Place this group with the standard preceding-section separation."""
        self.widget.pack(
            fill="x",
            pady=(
                0
                if first
                else self._px(STANDARD_CONTENT_SECTION_SPACING.between_sections_y),
                0,
            ),
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
        confirm_restore: Callable[[], bool] | None = None,
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
        self.confirm_restore = confirm_restore
        self.workflow = PreferencesDialogWorkflow(
            load_preferences_fn=load_preferences,
            save_preferences_fn=save_preferences,
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
        self.tab_strip = None
        self.page_scroll_shell = None
        self.page_canvas = None
        self.page_canvas_window = None
        self.page_scrollbar = None
        self.page_stack = None
        self.pages: dict[str, tk.Frame] = {}
        self.page_hint_labels: dict[str, list[tk.Label]] = {}
        self.field_page_keys: dict[str, str] = {
            field.key: field.section for field in PREFERENCE_FIELDS
        }
        self.active_page_key: str | None = None
        self.feedback_frame = None
        self.rendered_state: PreferencesFormState | None = None
        self.button_row = None
        self.error_label = None
        self._feedback_override: tuple[str, str] | None = None
        self._page_layout_after_id: str | None = None
        self._pending_page_canvas_width: int | None = None
        self._page_canvas_window_width: int | None = None
        self._page_scroll_region: tuple[int, int, int, int] | None = None
        self._scrollbar_layout_state: tuple[int, int] | None = None
        self._page_configured_sizes: dict[str, tuple[int, int]] = {}
        self._destroyed = False
        self.container.bind("<Destroy>", self._on_container_destroy, add="+")
        self.container.bind("<Map>", self._on_container_mapped, add="+")

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
        """Render every tab group through the standard section container."""
        section = tk.Frame(parent, bg=_BG_COLOR)
        # The form owns the full content surface. This leaves a stable
        # right-aligned control column and enough width for one-line hints.
        section.pack(fill="x")
        groups = _preference_field_groups(section_key)

        for index, (title, fields) in enumerate(groups):
            group = PreferenceSectionContainer(
                section,
                title=title,
                font=self.section_font,
                px=self._surface_px,
            )
            group.pack(first=index == 0)
            for field_index, field in enumerate(fields):
                self._render_field(
                    group.content,
                    field,
                    bottom_pad_y=(
                        self._form_row_gap()
                        if field_index < len(fields) - 1
                        else 0
                    ),
                )

    def _render_backup_restore(self, parent) -> None:
        """Render whole-snapshot actions separately from preference fields."""

        transfer_group = PreferenceSectionContainer(
            parent,
            title="Transfer",
            font=self.section_font,
            px=self._surface_px,
        )
        transfer_group.pack(first=True)
        self._render_backup_action(
            transfer_group.content,
            title="Export preferences",
            description="Save a preferences.json file that you can share or keep.",
            button_text="Export preferences…",
            command=self.export_preferences,
        )
        self._render_backup_action(
            transfer_group.content,
            title="Import preferences",
            description="Load a preferences.json file, review it, then select Apply.",
            button_text="Import preferences…",
            command=self.import_preferences,
            top_pad=self._form_row_gap(),
        )

        restore_group = PreferenceSectionContainer(
            parent,
            title="Recovery",
            font=self.section_font,
            px=self._surface_px,
        )
        restore_group.pack(first=False)
        self._render_backup_action(
            restore_group.content,
            title="Restore defaults",
            description="Stage the recommended defaults for review before applying.",
            button_text="Restore defaults",
            command=self.restore_defaults,
        )

    def _render_backup_action(
        self,
        parent,
        *,
        title: str,
        description: str,
        button_text: str,
        command: Callable[[], None],
        top_pad: int = 0,
    ) -> None:
        row = tk.Frame(parent, bg=_BG_COLOR)
        row.pack(fill="x", pady=(top_pad, 0))
        row.grid_columnconfigure(0, weight=1)
        text = tk.Frame(row, bg=_BG_COLOR)
        text.grid(row=0, column=0, sticky="ew")
        tk.Label(
            text,
            text=title,
            font=self.body_font,
            fg=_SUBTITLE_COLOR,
            bg=_BG_COLOR,
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            text,
            text=description,
            font=self.small_font,
            fg=_INSTRUCTION_COLOR,
            bg=_BG_COLOR,
            anchor="w",
            justify="left",
            wraplength=self._layout_policy.notice_wrap_length,
        ).pack(anchor="w", pady=(self._surface_px(4), 0))
        button = self._new_dialog_button(row, button_text, command)
        button.grid(
            row=0,
            column=1,
            sticky="e",
            padx=(self._surface_px(_CONTROL_GAP_X), 0),
        )

    def _render_field(
        self,
        section,
        field: PreferenceSpec,
        *,
        bottom_pad_y: int | None = None,
    ) -> None:
        key = field.key
        value_type = field.value_type
        compact_path = value_type in {
            PreferenceValueType.PATH,
            PreferenceValueType.PATH_CREATE,
        }
        row = tk.Frame(
            section,
            bg=_BG_COLOR,
        )
        row.pack(
            fill="x",
            pady=(
                0,
                self._form_row_gap() if bottom_pad_y is None else bottom_pad_y,
            ),
        )
        row.grid_columnconfigure(0, weight=1)
        row.grid_columnconfigure(1, weight=0)

        text_column = tk.Frame(row, bg=_BG_COLOR)
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
            bg=_BG_COLOR,
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

        entry_parent = tk.Frame(row, bg=_BG_COLOR)
        if compact_path:
            entry_parent.grid(
                row=1,
                column=0,
                columnspan=2,
                sticky="ew",
                pady=(self._layout_policy.control_row_top_pad_y, 0),
            )
        else:
            entry_parent.grid(
                row=0,
                column=1,
                sticky="e",
                padx=(self._layout_policy.row_pad_x, 0),
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
            bg=_BG_COLOR,
            justify="left",
            anchor="w",
            wraplength=(
                0 if single_line_hint else self._layout_policy.wrap_length
            ),
        )
        hint_label.pack(anchor="w", fill="x", pady=(3, 0))
        if not single_line_hint:
            self.page_hint_labels.setdefault(field.section, []).append(hint_label)

    @staticmethod
    def _sync_hint_wraplength(label, available_width: int) -> bool:
        """Match one description to its actual rendered text-column width."""
        if int(available_width) <= 1:
            return False
        wraplength = max(
            _MIN_HINT_WRAP_LENGTH,
            int(available_width) - _HINT_WRAP_INSET,
        )
        try:
            if int(label.cget("wraplength")) == wraplength:
                return False
            label.configure(wraplength=wraplength)
        except tk.TclError:
            return False
        return True

    def _sync_active_page_hint_wraplengths(self) -> bool:
        """Resize visible hints once per coalesced viewport layout pass."""
        changed = False
        page_key = self.active_page_key or ""
        uniform_width = self._hint_width_for_page(page_key)
        for label in self.page_hint_labels.get(page_key, ()):
            try:
                available_width = (
                    uniform_width
                    if uniform_width is not None
                    else int(label.master.winfo_width())
                )
                if available_width <= 1:
                    continue
                changed = self._sync_hint_wraplength(label, available_width) or changed
            except tk.TclError:
                continue
        return changed

    def _hint_width_for_page(self, page_key: str) -> int | None:
        """Return one stable description width from the final page width."""
        page_size = getattr(self, "_page_configured_sizes", {}).get(page_key)
        if page_size is None or page_size[0] <= 1:
            return None
        page_width = page_size[0]
        control_widths: list[int] = []
        for key, entry in self.field_entries.items():
            if self.field_page_keys.get(key) != page_key:
                continue
            try:
                control_widths.append(int(entry.winfo_reqwidth()))
            except tk.TclError:
                continue
        if not control_widths:
            return None
        return max(
            _MIN_HINT_WRAP_LENGTH,
            page_width
            - max(control_widths)
            - self._surface_px(self._layout_policy.row_pad_x),
        )

    def _sync_feedback_wraplength(self, available_width: int) -> None:
        """Resize feedback text only when its usable width actually changes."""
        if self.error_label is None:
            return
        wraplength = max(
            120,
            int(available_width) - 2 * _INLINE_FEEDBACK_PAD_X,
        )
        if int(self.error_label.cget("wraplength")) != wraplength:
            self.error_label.configure(wraplength=wraplength)

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

    def _surface_px(self, value: int | float) -> int:
        """Scale shared surface controls to the active splash display."""
        scale = tk_display_scale(
            self.dialog,
            presentation_profile=self.presentation_profile,
        )
        return max(1, int(round(float(value) * scale)))

    def _form_row_gap(self) -> int:
        """Return a display-scaled gap between adjacent preference rows."""
        return self._surface_px(self._layout_policy.row_pad_y + 6)

    def _on_container_destroy(self, event) -> None:
        """Cancel the panel-owned idle layout callback during Tk teardown."""
        if event.widget is not self.container:
            return
        self._destroyed = True
        after_id = self._page_layout_after_id
        self._page_layout_after_id = None
        if after_id is None:
            return
        try:
            self.dialog.after_cancel(after_id)
        except tk.TclError:
            pass

    def _on_container_mapped(self, event) -> None:
        """Refresh wrapping only after Tk maps the embedded panel onscreen."""
        if event.widget is self.container:
            self.on_shown()

    def _schedule_page_layout_sync(
        self,
        *,
        viewport_width: int | None = None,
    ) -> None:
        """Coalesce viewport, wrapping, and scrollbar work into one idle pass."""
        if viewport_width is not None and int(viewport_width) > 0:
            self._pending_page_canvas_width = int(viewport_width)
        if self._destroyed or self._page_layout_after_id is not None:
            return
        try:
            self._page_layout_after_id = self.dialog.after_idle(
                self._run_page_layout_sync
            )
        except tk.TclError:
            return

    def _run_page_layout_sync(self) -> None:
        """Run one scheduled layout pass if the Preferences panel still exists."""
        self._page_layout_after_id = None
        if self._destroyed:
            return
        try:
            if not self.container.winfo_exists():
                return
        except tk.TclError:
            return
        self._sync_page_layout()

    def _sync_page_layout(self) -> None:
        """Apply one stable canvas width, then visible text and overflow geometry."""
        if self.page_canvas is None or self.page_canvas_window is None:
            return
        viewport_width = self._pending_page_canvas_width
        self._pending_page_canvas_width = None
        if viewport_width is None:
            try:
                viewport_width = int(self.page_canvas.winfo_width())
            except tk.TclError:
                return
        if viewport_width <= 1:
            return

        if self._page_canvas_window_width != viewport_width:
            try:
                self.page_canvas.itemconfigure(
                    self.page_canvas_window,
                    width=viewport_width,
                )
            except tk.TclError:
                return
            self._page_canvas_window_width = viewport_width
            # The canvas schedules child geometry after its window width changes.
            # Measure hint columns only in the following idle pass.
            self._schedule_page_layout_sync()
            return

        hints_changed = self._sync_active_page_hint_wraplengths()
        if self.feedback_frame is not None:
            try:
                feedback_width = int(self.feedback_frame.winfo_width())
            except tk.TclError:
                feedback_width = 0
            if feedback_width > 1:
                self._sync_feedback_wraplength(feedback_width)
        self._sync_page_scrollbar()
        if hints_changed:
            # Wrapping can change the requested page height. One final pass
            # updates the scroll region after Tk propagates that new height.
            self._schedule_page_layout_sync()

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
        scroll_region = (0, 0, width, content_height)
        if self._page_scroll_region != scroll_region:
            self.page_canvas.configure(scrollregion=scroll_region)
            self._page_scroll_region = scroll_region
        layout_state = (content_height, self.page_canvas.winfo_height())
        if self._scrollbar_layout_state != layout_state:
            self.page_scrollbar.sync_overflow(content_height)
            self._scrollbar_layout_state = layout_state

    def _resize_page_canvas_window(self, event) -> None:
        self._schedule_page_layout_sync(viewport_width=event.width)

    def _ensure_page(self, page_key: str) -> tk.Frame | None:
        """Build one Preferences tab on first use and retain it thereafter."""
        page = self.pages.get(page_key)
        if page is not None:
            return page
        if self.page_stack is None or page_key not in _PREFERENCE_PAGE_KEYS:
            return None

        page = tk.Frame(self.page_stack, bg=_BG_COLOR)
        page.bind(
            "<Configure>",
            lambda event, key=page_key: self._on_page_configured(
                key,
                event.width,
                event.height,
            ),
            add="+",
        )
        self.pages[page_key] = page
        if page_key == "backup":
            self._render_backup_restore(page)
        else:
            self._render_section(page, page_key)
        if self.page_scrollbar is not None:
            self.page_scrollbar.bind_mousewheel(page)
        return page

    def _on_page_configured(self, page_key: str, width: int, height: int) -> None:
        """Rewrap on width changes and resync overflow on height changes."""
        size = (int(width), int(height))
        previous_size = self._page_configured_sizes.get(page_key)
        if size[0] <= 1 or size[1] <= 1 or previous_size == size:
            return
        self._page_configured_sizes[page_key] = size
        if page_key != self.active_page_key:
            return
        if previous_size is None or previous_size[0] != size[0]:
            self._sync_active_page_hint_wraplengths()
        self._schedule_page_layout_sync()

    def _show_page(self, page_key: str) -> None:
        page = self._ensure_page(page_key)
        if page is None:
            return
        self.active_page_key = page_key
        for key, candidate_page in self.pages.items():
            if key == page_key:
                candidate_page.grid(row=0, column=0, sticky="nsew")
            else:
                candidate_page.grid_remove()
        if self.tab_strip is not None:
            self.tab_strip.select(page_key, notify=False)
        if self.form_ready and self.rendered_state is not None:
            locked_key = (
                self.rendered_state.invalid_key
                if self.rendered_state.form_locked
                else None
            )
            self._set_field_lock(locked_key)
        self._sync_feedback_to_current_state()
        if self.page_canvas is not None:
            self.page_canvas.yview_moveto(0)
        self._page_scroll_region = None
        self._scrollbar_layout_state = None
        self._schedule_page_layout_sync()

    def _build(self) -> None:
        surface = TopTabbedContentSurface(
            self.container,
            tabs=tuple(
                TopTab(page_key, tab_label)
                for page_key, tab_label in _PREFERENCE_PAGES
            ),
            active_key=_PREFERENCE_PAGES[0][0],
            on_selected=self._show_page,
            px=self._surface_px,
            tab_style=TopTabStripStyle(
                background_color=_BG_COLOR,
                active_color=_BUTTON_BG,
                inactive_color=_INSTRUCTION_COLOR,
                focus_color=DARK_THEME.entry_focus_border,
                font=self.action_font,
            ),
            style=TopTabbedContentSurfaceStyle(
                background_color=_BG_COLOR,
                content_pad_x=self._layout_policy.body_pad_x,
                content_bottom_pad_y=DIALOG_BODY_PAD_Y,
            ),
        )
        surface.pack(fill="both", expand=True)
        self.tab_strip = surface.tab_strip
        body = surface.content

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

        self.page_scroll_shell = tk.Frame(body, bg=_BG_COLOR)
        self.page_scroll_shell.pack(side="top", fill="both", expand=True)
        self.page_scroll_shell.grid_rowconfigure(0, weight=1)
        self.page_scroll_shell.grid_columnconfigure(0, weight=1)
        self.page_canvas = tk.Canvas(
            self.page_scroll_shell,
            bg=_BG_COLOR,
            borderwidth=0,
            highlightthickness=0,
        )
        self.page_canvas.grid(row=0, column=0, sticky="nsew")
        self.page_scrollbar = CanvasVerticalScrollbar(
            self.page_scroll_shell,
            canvas=self.page_canvas,
            px=self._surface_px,
            style=CanvasScrollbarStyle(background_color=_BG_COLOR),
        )
        self.page_scrollbar.mount_grid(
            row=0,
            column=1,
            sticky="ns",
            padx=(self._surface_px(_SCROLLBAR_GUTTER_X), 0),
        )

        self.page_stack = tk.Frame(self.page_canvas, bg=_BG_COLOR)
        self.page_canvas_window = self.page_canvas.create_window(
            (0, 0),
            window=self.page_stack,
            anchor="nw",
        )
        self.page_stack.grid_rowconfigure(0, weight=1)
        self.page_stack.grid_columnconfigure(0, weight=1)
        self.page_canvas.bind("<Configure>", self._resize_page_canvas_window, add="+")
        # Only the initial page is constructed. Other tabs are built and
        # mousewheel-bound on first selection by ``_ensure_page``.
        self._show_page(_PREFERENCE_PAGES[0][0])

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
        # A lazily constructed tab reads its value directly from the form
        # snapshot when first shown, so an unbuilt field has no Tk variable to
        # synchronize yet.
        if key not in self.field_vars:
            return
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

    def export_preferences(self) -> None:
        """Choose a visible destination and export the validated form snapshot."""

        state, preferences = self.form.attempt_apply()
        self._render_form_state(state, focus_invalid=True)
        if preferences is None:
            return
        try:
            selection = self.desktop_services.save_file(
                title="Export CaveViewer preferences",
                initial_dir=str(default_downloads_dir()),
                initial_name=PREFERENCES_EXPORT_FILENAME,
                parent=self.dialog,
            )
        except DesktopServiceError as exc:
            self._set_feedback(str(exc), MessageKind.ERROR)
            return
        if selection is None:
            return
        result = self.workflow.export_file(selection.path, preferences)
        if not result.succeeded:
            self._set_feedback(
                result.error or "Could not export preferences.",
                MessageKind.ERROR,
            )
            return
        self._feedback_override = (
            f"Preferences exported to {selection.path}.",
            DARK_THEME.primary_button,
        )
        self._sync_feedback_to_current_state()

    def import_preferences(self) -> None:
        """Choose and stage a portable snapshot without saving it yet."""

        try:
            selection = self.desktop_services.choose_file(
                title="Import CaveViewer preferences",
                initial_dir=str(default_downloads_dir()),
                parent=self.dialog,
            )
        except DesktopServiceError as exc:
            self._set_feedback(str(exc), MessageKind.ERROR)
            return
        if selection is None:
            return
        result = self.workflow.import_file(selection.path)
        if not result.succeeded or result.preferences is None:
            self._set_feedback(
                result.error or "Could not import preferences.",
                MessageKind.ERROR,
            )
            return
        message = "Preferences imported. Review the values, then select Apply."
        if result.defaulted_keys:
            count = len(result.defaulted_keys)
            message = (
                f"Preferences imported; {count} invalid or missing "
                f"{'value was' if count == 1 else 'values were'} replaced "
                f"with {'its' if count == 1 else 'their'} default. "
                "Review the values, then select Apply."
            )
        self._stage_preferences(result.preferences, message)

    def restore_defaults(self) -> None:
        """Confirm and stage defaults while preserving Apply/Cancel semantics."""

        if not self._confirm_restore_defaults():
            return
        self._stage_preferences(
            Preferences(preference_defaults()),
            "Default preferences restored. Review the values, then select Apply.",
        )

    def _confirm_restore_defaults(self) -> bool:
        if self.confirm_restore is not None:
            return bool(self.confirm_restore())
        from tkinter import messagebox

        return bool(
            messagebox.askyesno(
                "Restore default preferences?",
                "Replace the current form values with CaveViewer defaults? "
                "The change is not saved until you select Apply.",
                parent=self.dialog,
            )
        )

    def _stage_preferences(self, preferences: Preferences, message: str) -> None:
        self.form = PreferencesFormController(preferences)
        self.rendering_state = True
        try:
            for key, value in preferences.items():
                self._sync_field_value(key, value)
        finally:
            self.rendering_state = False
        self.rendered_invalid_key = None
        self._feedback_override = (message, DARK_THEME.primary_button)
        self._render_form_state(self.form.state)

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

    def on_shown(self) -> None:
        """Recompute wrapping after the embedded surface receives its final width."""
        after_id = self._page_layout_after_id
        if after_id is not None:
            self._page_layout_after_id = None
            try:
                self.dialog.after_cancel(after_id)
            except tk.TclError:
                pass
        self._pending_page_canvas_width = None
        self._page_canvas_window_width = None
        self._page_scroll_region = None
        self._scrollbar_layout_state = None
        self._page_configured_sizes.clear()
        self._schedule_page_layout_sync()
