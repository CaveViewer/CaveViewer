"""Tk presentation adapter for the Preferences form controller."""

from __future__ import annotations

import os
import tkinter as tk
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Mapping

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
from caveviewer.gui.preferences_controls import (
    RoundedActionButton,
    RoundedEntryControl,
    RoundedSectionSurface,
)
from caveviewer.gui.preferences_style import (
    PREFERENCES_VISUAL_METRICS,
    PREFERENCES_VISUAL_PALETTE,
)
from caveviewer.gui.preferences_workflow import PreferencesDialogWorkflow
from caveviewer.gui.preferences_form import (
    PreferencesFormController,
    PreferencesFormState,
    MessageKind,
)
from caveviewer.gui.section_spacing import PRIMARY_SURFACE_VERTICAL_MARGIN
from caveviewer.gui.dialog_style import DIALOG_BODY_PAD_Y
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
from caveviewer.gui.modal_dialog import ask_confirmation
from caveviewer.gui.top_tab_strip import (
    TABBED_CONTENT_ALIGNMENT_INSET,
    TopTab,
    TopTabbedContentSurface,
    TopTabbedContentSurfaceStyle,
    TopTabStripStyle,
)
from caveviewer.gui.tk_theme import DARK_THEME
from caveviewer.gui.tk_feedback import INFO_FEEDBACK_MS
from caveviewer.gui.tk_typography import TkTypography, create_tk_typography

if TYPE_CHECKING:
    from caveviewer.gui.platform.runtime import PlatformRuntime


@dataclass(frozen=True, slots=True)
class PreferencesPanelSnapshot:
    """Unsaved form and navigation state retained across shell recomposition."""

    values: Mapping[str, str]
    active_page_key: str | None
    scroll_fraction: float


_BG_COLOR = DARK_THEME.background

_SCROLLBAR_GUTTER_X = 18
_INLINE_FEEDBACK_PAD_X = 10
_MIN_HINT_WRAP_LENGTH = 200
_HINT_WRAP_INSET = 4
_PREFERENCE_PAGES = (
    ("streaming", "Streaming"),
    ("parsing", "Import"),
    ("storage", "Storage"),
    ("backup", "Backup"),
)
_PREFERENCE_PAGE_KEYS = frozenset(key for key, _label in _PREFERENCE_PAGES)
_PREFERENCE_FIELD_GROUPS = {
    "streaming": (
        (
            "Memory Use",
            (
                "memory_target_percent",
                "gpu_memory_target_percent",
                "gpu_memory_gb",
            ),
        ),
        ("CPU Use", ("io_workers", "io_reserved_cpus")),
        (
            "Frame Loading",
            (
                "upload_chunks_per_frame",
                "upload_groups_per_frame",
                "upload_time_budget_ms",
            ),
        ),
    ),
    "parsing": (
        (
            "Map Processing",
            (
                "chunk_size_meters",
                "max_upload_group_mb",
                "obj_scan_throttle_ms",
                "obj_import_batch_thousands",
            ),
        ),
        (
            "CPU Use",
            ("chunk_build_workers", "chunk_build_reserved_cpus"),
        ),
    ),
    "storage": (
        ("Locations", ("recording_dir", "map_library_dir")),
    ),
}
_PREFERENCE_SECTION_DESCRIPTIONS = {
    "streaming": {
        "Memory Use": "Control system and graphics memory limits.",
        "CPU Use": "Control processor capacity used for loading.",
        "Frame Loading": "Control how much map data is loaded per frame.",
    },
    "parsing": {
        "Map Processing": (
            "Control how map data is divided and processed during import."
        ),
        "CPU Use": (
            "Control processor capacity used to prepare imported map data."
        ),
    },
    "storage": {
        "Locations": "Manage where local files are kept.",
    },
    "backup": {
        "Save & Load": (
            "Keep a copy of your preferences or use one saved earlier."
        ),
        "Reset": (
            "Return import and streaming preferences to their default values."
        ),
    },
}


@dataclass(frozen=True, slots=True)
class PreferenceFieldPresentation:
    """GUI-only display copy derived from one persisted field spec."""

    description: str
    inline_unit: str


_STREAMING_DESCRIPTIONS = {
    "memory_target_percent": (
        "Percentage of available RAM used for loaded chunks."
    ),
    "gpu_memory_target_percent": (
        "Percentage of GPU memory used for textures and geometry."
    ),
    "gpu_memory_gb": (
        "Optional ceiling. Leave blank for automatic detection. "
        "A smaller detected budget takes precedence."
    ),
    "io_workers": "Maximum chunk-loading worker threads.",
    "io_reserved_cpus": "Logical CPUs reserved from loading.",
    "upload_chunks_per_frame": (
        "Maximum ready chunks uploaded during each frame."
    ),
    "upload_groups_per_frame": (
        "Maximum upload slices from one ready chunk in each frame."
    ),
    "upload_time_budget_ms": (
        "Target time spent uploading chunks during each frame."
    ),
}
_STREAMING_INLINE_UNITS = {
    "memory_target_percent": "%",
    "gpu_memory_target_percent": "%",
    "gpu_memory_gb": "GB",
    "upload_time_budget_ms": "ms",
}
_PREFERENCE_INLINE_UNITS = {
    **_STREAMING_INLINE_UNITS,
    "max_upload_group_mb": "MB",
    "obj_scan_throttle_ms": "ms",
    "obj_import_batch_thousands": "thousand faces",
}


def _preference_field_presentation(
    field: PreferenceSpec,
) -> PreferenceFieldPresentation:
    """Return card-field copy without changing the schema or persisted value."""
    return PreferenceFieldPresentation(
        description=_STREAMING_DESCRIPTIONS.get(field.key, field.hint),
        inline_unit=_PREFERENCE_INLINE_UNITS.get(field.key, ""),
    )


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
        px: Callable[[int | float], int] | None = None,
        on_applied: Callable[[Preferences], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        confirm_restore: Callable[[], bool] | None = None,
        initial_snapshot: PreferencesPanelSnapshot | None = None,
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
        self._layout_px = px
        self.container = tk.Frame(parent, bg=_BG_COLOR)
        self.container.pack(fill="both", expand=True)

        self.heading_font = self.typography.heading
        self.action_font = self.typography.body_strong
        self.body_font = self.typography.body
        self.body_strong_font = self.typography.body_strong
        self.small_font = self.typography.supporting
        self.preferences_palette = PREFERENCES_VISUAL_PALETTE
        self.preferences_metrics = PREFERENCES_VISUAL_METRICS.scaled(
            self._surface_px
        )

        self.field_vars: dict[str, tk.StringVar] = {}
        self.field_entries: dict[str, tk.Entry] = {}
        self.field_title_labels: dict[str, tk.Label] = {}
        self.field_display_vars: dict[str, tk.StringVar] = {}
        self.field_entry_states: dict[str, str] = {}
        self.rounded_field_controls: dict[str, RoundedEntryControl] = {}
        self.preference_cards: dict[str, list[RoundedSectionSurface]] = {}
        self.preference_action_buttons: list[RoundedActionButton] = []
        self.page_focus_targets: dict[
            str,
            list[RoundedEntryControl | RoundedActionButton],
        ] = {}
        self.numeric_entry_states: dict[str, tuple] = {}
        self.numeric_placeholder_keys: set[str] = set()
        self.form_ready = False
        self.rendering_state = False
        self.rendered_invalid_key: str | None = None
        self.apply_button = None
        self.discard_button = None
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
        self._feedback_override_is_transient = False
        self._feedback_after_id: str | None = None
        self._page_layout_after_id: str | None = None
        self._invalid_focus_after_id: str | None = None
        self._scroll_restore_after_id: str | None = None
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
        if initial_snapshot is not None:
            self._restore_snapshot(initial_snapshot)

    def snapshot(self) -> PreferencesPanelSnapshot:
        """Capture staged values without validating or saving them."""
        scroll_fraction = 0.0
        if self.page_canvas is not None:
            try:
                scroll_fraction = float(self.page_canvas.yview()[0])
            except (IndexError, TypeError, ValueError, tk.TclError):
                pass
        return PreferencesPanelSnapshot(
            values=dict(self.form.state.values),
            active_page_key=self.active_page_key,
            scroll_fraction=scroll_fraction,
        )

    def _restore_snapshot(self, snapshot: PreferencesPanelSnapshot) -> None:
        state = self.form.stage(snapshot.values)
        target_page = snapshot.active_page_key
        if target_page in _PREFERENCE_PAGE_KEYS:
            self._show_page(target_page)
        self.rendering_state = True
        try:
            for key, value in state.values.items():
                self._sync_field_value(key, value)
        finally:
            self.rendering_state = False
        self._render_form_state(state)
        if self.page_canvas is not None and snapshot.scroll_fraction > 0.0:
            fraction = max(0.0, min(1.0, float(snapshot.scroll_fraction)))
            self._schedule_scroll_restore(fraction)

    def _schedule_scroll_restore(self, fraction: float) -> None:
        """Restore a recomposed page position while owning the idle callback."""
        self._cancel_after_callback("_scroll_restore_after_id")

        def restore() -> None:
            self._scroll_restore_after_id = None
            if getattr(self, "_destroyed", False) or self.page_canvas is None:
                return
            try:
                self.page_canvas.yview_moveto(fraction)
            except tk.TclError:
                return

        try:
            self._scroll_restore_after_id = self.dialog.after_idle(restore)
        except tk.TclError:
            self._scroll_restore_after_id = None

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
        entry.configure(validate=previous_validation)
        rounded_control = getattr(self, "rounded_field_controls", {}).get(key)
        if rounded_control is not None:
            rounded_control.set_placeholder(True)

    def _clear_numeric_placeholder(self, key: str) -> None:
        if key not in self.numeric_placeholder_keys:
            return
        entry, display_var, _placeholder_text = self.numeric_entry_states[key]
        previous_validation = entry.cget("validate")
        entry.configure(validate="none")
        self.numeric_placeholder_keys.discard(key)
        display_var.set("")
        entry.configure(validate=previous_validation)
        rounded_control = getattr(self, "rounded_field_controls", {}).get(key)
        if rounded_control is not None:
            rounded_control.set_placeholder(False)
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

    def _new_preferences_action(
        self,
        parent,
        text: str,
        command: Callable[[], None],
        *,
        kind: str,
        outside_background: str = _BG_COLOR,
        width: int | None = None,
    ) -> RoundedActionButton:
        """Create one Preferences action from the rounded control contract."""
        button = RoundedActionButton(
            parent,
            text=text,
            command=command,
            font=self.action_font,
            metrics=self.preferences_metrics,
            palette=self.preferences_palette,
            kind=kind,
            outside_background=outside_background,
            width=width,
        )
        self.preference_action_buttons.append(button)
        return button

    def _render_section(self, parent, section_key: str) -> None:
        """Render every preference group through the shared card stack."""
        section = tk.Frame(parent, bg=_BG_COLOR)
        section.pack(fill="x")
        self.preference_cards[section_key] = []
        for index, (title, fields) in enumerate(
            _preference_field_groups(section_key)
        ):
            description = _PREFERENCE_SECTION_DESCRIPTIONS.get(
                section_key, {}
            ).get(title)
            card, fields_host = self._new_preference_card(
                section,
                title=title,
                first=index == 0,
                description=description,
                page_key=section_key,
            )
            self.preference_cards[section_key].append(card)
            for field_index, field in enumerate(fields):
                self._render_field(fields_host, field)
                if field_index < len(fields) - 1:
                    self._render_card_item_gap(fields_host)

    def _render_card_item_gap(self, parent) -> None:
        """Separate adjacent fields or actions without drawing a line."""
        gap = tk.Frame(
            parent,
            bg=self.preferences_palette.section_background,
            height=self.preferences_metrics.card_item_gap_y,
        )
        gap.pack(fill="x")

    def _new_preference_card(
        self,
        parent,
        *,
        title: str,
        first: bool,
        description: str | None = None,
        page_key: str | None = None,
    ) -> tuple[RoundedSectionSurface, tk.Frame]:
        """Create one aligned rounded card and its field/action content host."""
        card = RoundedSectionSurface(
            parent,
            metrics=self.preferences_metrics,
            palette=self.preferences_palette,
        )
        card.pack(
            fill="x",
            padx=(self._surface_px(TABBED_CONTENT_ALIGNMENT_INSET), 0),
            pady=(
                0 if first else self.preferences_metrics.section_gap_y,
                0,
            ),
        )
        tk.Label(
            card.content,
            text=title,
            font=self.heading_font,
            fg=self.preferences_palette.heading_text,
            bg=self.preferences_palette.section_background,
            anchor="w",
        ).pack(anchor="w")
        content_gap = self.preferences_metrics.section_heading_to_fields_y
        if description:
            description_label = tk.Label(
                card.content,
                text=description,
                font=self.small_font,
                fg=self.preferences_palette.supporting_text,
                bg=self.preferences_palette.section_background,
                anchor="w",
                justify="left",
                wraplength=self._layout_policy.wrap_length,
            )
            description_label.pack(
                anchor="w",
                fill="x",
                pady=(
                    self.preferences_metrics.section_heading_to_description_y,
                    0,
                ),
            )
            if page_key is not None:
                self.page_hint_labels.setdefault(page_key, []).append(
                    description_label
                )
            content_gap = self.preferences_metrics.section_description_to_fields_y
        content = tk.Frame(
            card.content,
            bg=self.preferences_palette.section_background,
        )
        content.pack(
            fill="x",
            pady=(content_gap, 0),
        )
        return card, content

    def _render_backup_restore(self, parent) -> None:
        """Render whole-snapshot actions with the shared card/action system."""
        section = tk.Frame(parent, bg=_BG_COLOR)
        section.pack(fill="x")
        self.preference_cards["backup"] = []
        groups = (
            (
                "Save & Load",
                (
                    (
                        "Save preferences",
                        "Save preferences to a file.",
                        "Save",
                        self.export_preferences,
                    ),
                    (
                        "Load preferences",
                        "Load preferences from a file.",
                        "Load",
                        self.import_preferences,
                    ),
                ),
            ),
            (
                "Reset",
                (
                    (
                        "Restore defaults",
                        "Restore default import and streaming settings.",
                        "Restore",
                        self.restore_defaults,
                    ),
                ),
            ),
        )
        for index, (title, actions) in enumerate(groups):
            card, actions_host = self._new_preference_card(
                section,
                title=title,
                first=index == 0,
                description=_PREFERENCE_SECTION_DESCRIPTIONS["backup"][title],
                page_key="backup",
            )
            self.preference_cards["backup"].append(card)
            for action_index, (
                action_title,
                description,
                button_text,
                command,
            ) in enumerate(actions):
                self._render_backup_action(
                    actions_host,
                    title=action_title,
                    description=description,
                    button_text=button_text,
                    command=command,
                )
                if action_index < len(actions) - 1:
                    self._render_card_item_gap(actions_host)

    def _render_backup_action(
        self,
        parent,
        *,
        title: str,
        description: str,
        button_text: str,
        command: Callable[[], None],
    ) -> None:
        """Render one stacked backup action inside a Preferences card."""
        row = tk.Frame(parent, bg=self.preferences_palette.section_background)
        row.pack(fill="x")
        tk.Label(
            row,
            text=title,
            font=self.body_strong_font,
            fg=self.preferences_palette.field_label_text,
            bg=self.preferences_palette.section_background,
            anchor="w",
        ).pack(anchor="w")
        description_label = tk.Label(
            row,
            text=description,
            font=self.small_font,
            fg=self.preferences_palette.supporting_text,
            bg=self.preferences_palette.section_background,
            anchor="w",
            justify="left",
            wraplength=self._layout_policy.wrap_length,
        )
        description_label.pack(
            anchor="w",
            fill="x",
            pady=(self.preferences_metrics.field_label_to_description_y, 0),
        )
        self.page_hint_labels.setdefault("backup", []).append(description_label)
        button = self._new_preferences_action(
            row,
            button_text,
            command,
            kind="secondary",
            outside_background=self.preferences_palette.section_background,
        )
        self.page_focus_targets.setdefault("backup", []).append(button)
        button.pack(
            anchor="w",
            pady=(self.preferences_metrics.field_description_to_control_y, 0),
        )

    def _render_field(
        self,
        parent,
        field: PreferenceSpec,
    ) -> None:
        """Render one field with the shared label, description, and control."""
        key = field.key
        value_type = field.value_type
        compact_path = value_type in {
            PreferenceValueType.PATH,
            PreferenceValueType.PATH_CREATE,
        }
        presentation = _preference_field_presentation(field)
        row = tk.Frame(parent, bg=self.preferences_palette.section_background)
        row.pack(fill="x")

        title_label = tk.Label(
            row,
            text=field.label,
            font=self.body_strong_font,
            fg=self.preferences_palette.field_label_text,
            bg=self.preferences_palette.section_background,
            anchor="w",
        )
        title_label.pack(anchor="w")
        self.field_title_labels[key] = title_label

        description_label = tk.Label(
            row,
            text=presentation.description,
            font=self.small_font,
            fg=self.preferences_palette.supporting_text,
            bg=self.preferences_palette.section_background,
            anchor="w",
            justify="left",
            wraplength=self._layout_policy.wrap_length,
        )
        description_label.pack(
            anchor="w",
            fill="x",
            pady=(self.preferences_metrics.field_label_to_description_y, 0),
        )
        self.page_hint_labels.setdefault(field.section, []).append(
            description_label
        )

        var = tk.StringVar(master=self.dialog, value=self.form.state.values[key])
        self.field_vars[key] = var
        entry_var = var
        placeholder_text = preference_placeholder_text(field)
        if placeholder_text:
            entry_var = tk.StringVar(master=self.dialog, value=var.get())
            if not var.get():
                entry_var.set(placeholder_text)
                self.numeric_placeholder_keys.add(key)
        elif compact_path:
            entry_var = tk.StringVar(
                master=self.dialog,
                value=self._compact_directory_path(var.get()),
            )
            var.trace_add(
                "write",
                lambda *_args, source=var, display=entry_var: display.set(
                    self._compact_directory_path(source.get())
                ),
            )
        self.field_display_vars[key] = entry_var

        control_row = tk.Frame(
            row,
            bg=self.preferences_palette.section_background,
        )
        control_row.pack(
            fill="x",
            pady=(self.preferences_metrics.field_description_to_control_y, 0),
        )
        rounded_entry = RoundedEntryControl(
            control_row,
            textvariable=entry_var,
            font=self.body_font,
            metrics=self.preferences_metrics,
            palette=self.preferences_palette,
            state="readonly" if compact_path else "normal",
            width=1 if compact_path else None,
            outside_background=self.preferences_palette.section_background,
            validate="none" if compact_path else "key",
            validatecommand=(
                None
                if compact_path
                else (
                    self.numeric_entry_validator,
                    value_type.value,
                    "%P",
                )
            ),
            action_text="Browse" if compact_path else None,
            action_command=(
                (
                    lambda field_key=key, title=field.label: self._choose_directory(
                        field_key,
                        title,
                    )
                )
                if compact_path
                else None
            ),
            action_font=self.action_font if compact_path else None,
        )
        if compact_path:
            rounded_entry.pack(fill="x")
        else:
            rounded_entry.pack(side="left")
        entry = rounded_entry.entry
        self.rounded_field_controls[key] = rounded_entry
        self.page_focus_targets.setdefault(field.section, []).append(
            rounded_entry
        )
        self.field_entries[key] = entry
        self.field_entry_states[key] = "readonly" if compact_path else "normal"
        if key in self.numeric_placeholder_keys:
            rounded_entry.set_placeholder(True)

        if presentation.inline_unit:
            tk.Label(
                control_row,
                text=presentation.inline_unit,
                font=self.body_font,
                fg=self.preferences_palette.supporting_text,
                bg=self.preferences_palette.section_background,
                anchor="w",
            ).pack(
                side="left",
                padx=(self.preferences_metrics.unit_gap_x, 0),
            )

        if placeholder_text:
            self.numeric_entry_states[key] = (entry, entry_var, placeholder_text)
            entry_var.trace_add(
                "write",
                lambda *_args, field_key=key, source=entry_var: self._sync_numeric_value(
                    field_key,
                    source,
                ),
            )
            entry.bind(
                "<KeyPress>",
                lambda event, field_key=key: self._begin_numeric_edit_from_key(
                    event,
                    field_key,
                ),
                add="+",
            )
            entry.bind(
                "<Button-1>",
                lambda event, field_key=key: self._begin_numeric_edit_from_click(
                    event,
                    field_key,
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
        return max(
            _MIN_HINT_WRAP_LENGTH,
            page_width
            - self._surface_px(TABBED_CONTENT_ALIGNMENT_INSET)
            - (self.preferences_metrics.section_padding_x * 2),
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
        if self._layout_px is not None:
            return int(self._layout_px(value))
        scale = tk_display_scale(
            self.dialog,
            presentation_profile=self.presentation_profile,
        )
        return int(round(float(value) * scale))

    def _form_row_gap(self) -> int:
        """Return a display-scaled gap between adjacent preference rows."""
        return self._surface_px(self._layout_policy.row_pad_y + 6)

    def _on_container_destroy(self, event) -> None:
        """Cancel panel-owned callbacks during Tk teardown."""
        if event.widget is not self.container:
            return
        self._destroyed = True
        self._cancel_transient_feedback(clear=False)
        self._cancel_after_callback("_page_layout_after_id")
        self._cancel_after_callback("_invalid_focus_after_id")
        self._cancel_after_callback("_scroll_restore_after_id")

    def _cancel_after_callback(self, attribute: str) -> None:
        """Cancel one named panel callback and clear its ownership token."""
        after_id = getattr(self, attribute, None)
        setattr(self, attribute, None)
        if after_id is None:
            return
        try:
            self.dialog.after_cancel(after_id)
        except (AttributeError, tk.TclError):
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

    def measure_preferred_page_height(self) -> int:
        """Return the tallest Preferences page height before the shell is shown."""
        original_page_key = self.active_page_key
        original_scroll_fraction = 0.0
        if self.page_canvas is not None:
            try:
                original_scroll_fraction = float(self.page_canvas.yview()[0])
            except (IndexError, TypeError, ValueError, tk.TclError):
                pass

        page_heights: list[int] = []
        for page_key, _label in _PREFERENCE_PAGES:
            self._show_page(page_key)
            self.dialog.update_idletasks()
            self._sync_page_layout()
            self.dialog.update_idletasks()
            page = self.pages.get(page_key)
            if page is not None:
                page_heights.append(max(0, int(page.winfo_reqheight())))

        if original_page_key in _PREFERENCE_PAGE_KEYS:
            self._show_page(original_page_key)
        if self.page_canvas is not None:
            try:
                self.page_canvas.yview_moveto(original_scroll_fraction)
            except tk.TclError:
                pass
        return max(page_heights, default=0)

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
                active_color=self.preferences_palette.tab_active_text,
                inactive_color=self.preferences_palette.tab_inactive_text,
                focus_color=DARK_THEME.entry_focus_border,
                font=self.action_font,
                active_font=self.action_font,
                inactive_font=self.body_font,
            ),
            style=TopTabbedContentSurfaceStyle(
                background_color=_BG_COLOR,
                content_pad_left_x=0,
                content_pad_right_x=self._layout_policy.body_pad_x,
                content_bottom_pad_y=DIALOG_BODY_PAD_Y,
            ),
        )
        surface.pack(
            fill="both",
            expand=True,
            pady=self._surface_px(PRIMARY_SURFACE_VERTICAL_MARGIN),
        )
        self.tab_strip = surface.tab_strip
        body = surface.content

        # Create the page host before the footer so Tk traverses page controls
        # before footer actions, while packing the footer first still protects
        # it from clipping in height-limited windows.
        self.page_scroll_shell = tk.Frame(body, bg=_BG_COLOR)
        self.button_row = tk.Frame(body, bg=_BG_COLOR)
        # Pack the action row before the page stack so a height-limited
        # Windows dialog shrinks form content first instead of clipping
        # Apply/Cancel off the bottom edge.
        self.button_row.pack(
            side="bottom",
            fill="x",
            pady=(self.preferences_metrics.footer_top_gap_y, 0),
        )

        self.discard_button = self._new_preferences_action(
            self.button_row,
            "Discard changes",
            self.discard_changes,
            kind="secondary",
        )
        self.apply_button = self._new_preferences_action(
            self.button_row,
            "Save changes",
            self.apply,
            kind="primary",
        )

        self.apply_button.pack(side="right")
        self.discard_button.pack(
            side="right",
            padx=(0, self.preferences_metrics.footer_action_gap_x),
        )

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
        self._set_action_enabled(self.apply_button, enabled=enabled)

    @staticmethod
    def _set_action_enabled(
        button: RoundedActionButton,
        *,
        enabled: bool,
    ) -> None:
        button.set_enabled(enabled)

    def _render_dirty_state(self, state: PreferencesFormState) -> None:
        """Keep pending changes visible across tab navigation."""
        has_changes = state.has_unsaved_changes
        self._set_action_enabled(
            self.apply_button,
            enabled=has_changes and state.apply_enabled,
        )
        self._set_action_enabled(self.discard_button, enabled=has_changes)
        if self.tab_strip is not None:
            self.tab_strip.set_indicated(state.dirty_sections)
        fields_by_key = {field.key: field for field in PREFERENCE_FIELDS}
        for key, label in self.field_title_labels.items():
            suffix = " •" if key in state.dirty_keys else ""
            label.configure(text=f"{fields_by_key[key].label}{suffix}")

    def _set_field_lock(self, invalid_key: str | None) -> None:
        for key in self.field_entries:
            enabled = invalid_key is None or key == invalid_key
            rounded_control = self.rounded_field_controls[key]
            rounded_control.set_state(
                self.field_entry_states[key] if enabled else "disabled"
            )
            rounded_control.set_invalid(key == invalid_key)

    def _focus_invalid_field(
        self, key: str, *, select_value: bool = False
    ) -> None:
        page_key = self.field_page_keys.get(key)
        if page_key is not None:
            self._show_page(page_key)

        self._cancel_after_callback("_invalid_focus_after_id")

        def focus() -> None:
            self._invalid_focus_after_id = None
            if getattr(self, "_destroyed", False):
                return
            entry = self.field_entries.get(key)
            if entry is None:
                return
            try:
                if not entry.winfo_exists():
                    return
                entry.focus_set()
            except tk.TclError:
                return
            if (
                select_value
                and self.field_entry_states.get(key) == "normal"
                and key not in self.numeric_placeholder_keys
            ):
                entry.selection_range(0, "end")

        try:
            self._invalid_focus_after_id = self.dialog.after_idle(focus)
        except tk.TclError:
            self._invalid_focus_after_id = None

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
        self._render_dirty_state(state)
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
        self._cancel_transient_feedback(clear=False)
        self._feedback_override = None
        self._feedback_override_is_transient = False
        state = self.form.change(key, self.field_vars[key].get())
        self._render_form_state(state, preferred_key=key)

    def _cancel_transient_feedback(self, *, clear: bool) -> None:
        """Cancel an event confirmation and optionally remove its message."""
        after_id = getattr(self, "_feedback_after_id", None)
        self._feedback_after_id = None
        if after_id is not None:
            try:
                self.dialog.after_cancel(after_id)
            except (AttributeError, tk.TclError):
                pass
        if clear and getattr(self, "_feedback_override_is_transient", False):
            self._feedback_override = None
            self._feedback_override_is_transient = False
            if not getattr(self, "_destroyed", False):
                self._sync_feedback_to_current_state()

    def _clear_transient_feedback(self) -> None:
        """Remove the current event confirmation after its bounded lifetime."""
        self._feedback_after_id = None
        if not getattr(self, "_feedback_override_is_transient", False):
            return
        self._feedback_override = None
        self._feedback_override_is_transient = False
        if not getattr(self, "_destroyed", False):
            self._sync_feedback_to_current_state()

    def _show_transient_feedback(
        self,
        message: str,
        color: str,
        *,
        duration_ms: int,
    ) -> None:
        """Show one bounded event confirmation owned by this panel."""
        self._cancel_transient_feedback(clear=False)
        self._feedback_override = (message, color)
        self._feedback_override_is_transient = True
        self._sync_feedback_to_current_state()
        try:
            self._feedback_after_id = self.dialog.after(
                duration_ms,
                self._clear_transient_feedback,
            )
        except (AttributeError, tk.TclError):
            self._feedback_after_id = None

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
                    rounded_control = getattr(
                        self, "rounded_field_controls", {}
                    ).get(key)
                    if rounded_control is not None:
                        rounded_control.set_placeholder(False)
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
        self._cancel_transient_feedback(clear=False)
        self._feedback_override = None
        self._feedback_override_is_transient = False
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
        if result.preferences is not None:
            clean_state = self.form.mark_saved(result.preferences)
            self._render_form_state(clean_state)
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
                title="Save CaveViewer preferences",
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
        self._show_transient_feedback(
            f"Preferences saved to {selection.path}.",
            DARK_THEME.primary_button,
            duration_ms=INFO_FEEDBACK_MS,
        )

    def import_preferences(self) -> None:
        """Choose and stage a portable snapshot without saving it yet."""

        state, current_preferences = self.form.attempt_apply()
        self._render_form_state(state, focus_invalid=True)
        if current_preferences is None:
            return
        try:
            selection = self.desktop_services.choose_file(
                title="Load CaveViewer preferences",
                initial_dir=str(default_downloads_dir()),
                parent=self.dialog,
            )
        except DesktopServiceError as exc:
            self._set_feedback(str(exc), MessageKind.ERROR)
            return
        if selection is None:
            return
        result = self.workflow.import_file(
            selection.path,
            current_preferences,
        )
        if not result.succeeded or result.preferences is None:
            self._set_feedback(
                result.error or "Could not import preferences.",
                MessageKind.ERROR,
            )
            return
        message = "Preferences loaded. Review the values, then save changes."
        if result.defaulted_keys:
            count = len(result.defaulted_keys)
            message = (
                f"Preferences loaded; {count} invalid or missing "
                f"{'value was' if count == 1 else 'values were'} replaced "
                f"with {'its' if count == 1 else 'their'} default. "
                "Review the values, then save changes."
            )
        self._stage_preferences(result.preferences, message)

    def restore_defaults(self) -> None:
        """Confirm and stage defaults for review before they are saved."""

        if not self._confirm_restore_defaults():
            return
        self._stage_preferences(
            Preferences(preference_defaults()),
            "Default preferences restored. Review the values, then save changes.",
        )

    def _confirm_restore_defaults(self) -> bool:
        if self.confirm_restore is not None:
            return bool(self.confirm_restore())
        return ask_confirmation(
            self.dialog,
            title="Restore default preferences?",
            message=(
                "Replace the current form values with CaveViewer defaults? "
                "The change is not saved until you select Save changes."
            ),
            confirm_text="Restore",
            cancel_text="Cancel",
        )

    def _stage_preferences(self, preferences: Preferences, message: str) -> None:
        self._cancel_transient_feedback(clear=False)
        state = self.form.stage(preferences.as_dict())
        self.rendering_state = True
        try:
            for key, value in preferences.items():
                self._sync_field_value(key, value)
        finally:
            self.rendering_state = False
        self.rendered_invalid_key = None
        self._feedback_override = (message, DARK_THEME.primary_button)
        self._feedback_override_is_transient = False
        self._render_form_state(state)

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
        return form.state.has_unsaved_changes

    def discard_changes(self) -> None:
        """Restore the last saved values without destroying this panel."""
        self._cancel_transient_feedback(clear=False)
        preferences = getattr(self, "preferences", None)
        if preferences is None:
            return
        state = self.form.discard()
        self.rendering_state = True
        try:
            for key, value in preferences.items():
                self._sync_field_value(key, value)
        finally:
            self.rendering_state = False
        self.rendered_invalid_key = None
        self._feedback_override = None
        self._feedback_override_is_transient = False
        self._render_form_state(state)

    def on_hidden(self) -> None:
        """Clear obsolete event confirmations when Preferences is left."""
        self._cancel_transient_feedback(clear=True)
        self._cancel_after_callback("_invalid_focus_after_id")
        self._cancel_after_callback("_scroll_restore_after_id")

    def focus_content(self) -> None:
        """Move keyboard focus into the active embedded Preferences view."""
        if self.form.state.invalid_key is not None:
            self._focus_invalid_field(self.form.state.invalid_key, select_value=True)
            return
        targets = self.page_focus_targets.get(self.active_page_key or "", ())
        for target in targets:
            if target.focus_set():
                return

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
