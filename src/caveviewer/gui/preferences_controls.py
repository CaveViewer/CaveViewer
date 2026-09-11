"""Rounded Tk primitives owned by the CaveViewer Preferences surface."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, replace
from typing import Callable, Literal

from caveviewer.gui.preferences_style import (
    PREFERENCES_VISUAL_PALETTE,
    PreferencesVisualPalette,
    ScaledPreferencesVisualMetrics,
)


ActionKind = Literal["primary", "secondary"]
EntryState = Literal["normal", "readonly", "disabled"]

_ROUNDED_SURFACE_TAG = "cv-rounded-surface"
_COMPOUND_ACTION_BACKDROP_TAG = "cv-compound-action-backdrop"
_CURVE_STEPS = 24


@dataclass(frozen=True, slots=True)
class ControlInteractionState:
    """Display-independent interaction flags for one rounded control."""

    enabled: bool = True
    hovered: bool = False
    pressed: bool = False
    focused: bool = False
    invalid: bool = False
    readonly: bool = False
    placeholder: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedControlVisual:
    """Colors and border geometry selected for a control state."""

    background: str
    border: str
    foreground: str
    border_width: int


def rounded_rectangle_points(
    width: int,
    height: int,
    radius: int,
    *,
    inset: int = 0,
) -> tuple[int, ...]:
    """Return a clamped clockwise path for one smoothed rounded rectangle."""
    x0 = max(0, int(inset))
    y0 = x0
    x1 = max(x0, int(width) - x0)
    y1 = max(y0, int(height) - y0)
    resolved_radius = max(
        0,
        min(int(radius), (x1 - x0) // 2, (y1 - y0) // 2),
    )
    return (
        x0 + resolved_radius,
        y0,
        x0 + resolved_radius,
        y0,
        x1 - resolved_radius,
        y0,
        x1 - resolved_radius,
        y0,
        x1,
        y0,
        x1,
        y0 + resolved_radius,
        x1,
        y0 + resolved_radius,
        x1,
        y1 - resolved_radius,
        x1,
        y1 - resolved_radius,
        x1,
        y1,
        x1 - resolved_radius,
        y1,
        x1 - resolved_radius,
        y1,
        x0 + resolved_radius,
        y1,
        x0 + resolved_radius,
        y1,
        x0,
        y1,
        x0,
        y1 - resolved_radius,
        x0,
        y1 - resolved_radius,
        x0,
        y0 + resolved_radius,
        x0,
        y0 + resolved_radius,
        x0,
        y0,
    )


def right_rounded_segment_points(
    width: int,
    height: int,
    radius: int,
    start_x: int,
    *,
    inset: int = 0,
) -> tuple[int, ...]:
    """Return the right-hand portion of a rounded rectangle with a square seam."""
    points = rounded_rectangle_points(width, height, radius, inset=inset)
    left = max(int(inset), min(int(start_x), max(points[0::2], default=0)))
    return tuple(
        max(left, coordinate) if index % 2 == 0 else coordinate
        for index, coordinate in enumerate(points)
    )


def resolve_entry_visual(
    state: ControlInteractionState,
    *,
    palette: PreferencesVisualPalette,
    metrics: ScaledPreferencesVisualMetrics,
) -> ResolvedControlVisual:
    """Resolve entry colors with disabled, invalid, and focus precedence."""
    foreground = (
        palette.control_placeholder_text
        if state.placeholder
        else palette.control_text
    )
    background = palette.control_background
    border = palette.control_border
    border_width = metrics.control_border_thickness
    if not state.enabled:
        foreground = palette.control_placeholder_text
        background = palette.disabled_action_background
        border = palette.disabled_action_border
    elif state.invalid:
        border = palette.control_invalid_border
        border_width = metrics.focus_border_thickness
    elif state.focused:
        border = palette.control_focus_border
        border_width = metrics.focus_border_thickness
    return ResolvedControlVisual(
        background=background,
        border=border,
        foreground=foreground,
        border_width=border_width,
    )


def resolve_action_visual(
    state: ControlInteractionState,
    *,
    kind: ActionKind,
    palette: PreferencesVisualPalette,
    metrics: ScaledPreferencesVisualMetrics,
) -> ResolvedControlVisual:
    """Resolve action colors while keeping focus visible in every active state."""
    if not state.enabled:
        return ResolvedControlVisual(
            background=palette.disabled_action_background,
            border=palette.disabled_action_border,
            foreground=palette.disabled_action_text,
            border_width=metrics.control_border_thickness,
        )

    if kind == "primary":
        background = palette.primary_action_background
        if state.pressed:
            background = palette.primary_action_pressed_background
        elif state.hovered:
            background = palette.primary_action_hover_background
        border = palette.primary_action_border
        foreground = palette.primary_action_text
    else:
        background = palette.secondary_action_background
        if state.pressed:
            background = palette.secondary_action_pressed_background
        elif state.hovered:
            background = palette.secondary_action_hover_background
        border = palette.secondary_action_border
        foreground = palette.secondary_action_text

    border_width = metrics.control_border_thickness
    if state.focused:
        border = palette.control_focus_border
        border_width = metrics.focus_border_thickness
    return ResolvedControlVisual(
        background=background,
        border=border,
        foreground=foreground,
        border_width=border_width,
    )


class RoundedSurfaceRenderer:
    """Redraw one rounded Canvas surface without retaining scheduled work."""

    def __init__(self, canvas) -> None:
        self._canvas = canvas
        self._closed = False

    def redraw(
        self,
        *,
        width: int,
        height: int,
        radius: int,
        fill: str,
        border: str,
        border_width: int,
    ) -> None:
        """Replace the prior vector surface with the current bounded geometry."""
        if self._closed:
            return
        width = max(0, int(width))
        height = max(0, int(height))
        self._canvas.delete(_ROUNDED_SURFACE_TAG)
        if width <= 0 or height <= 0:
            return

        border_width = max(0, int(border_width))
        outer_fill = border if border_width else fill
        self._canvas.create_polygon(
            rounded_rectangle_points(width, height, radius),
            fill=outer_fill,
            outline="",
            smooth=True,
            splinesteps=_CURVE_STEPS,
            tags=(_ROUNDED_SURFACE_TAG,),
        )
        if border_width:
            self._canvas.create_polygon(
                rounded_rectangle_points(
                    width,
                    height,
                    max(0, radius - border_width),
                    inset=border_width,
                ),
                fill=fill,
                outline="",
                smooth=True,
                splinesteps=_CURVE_STEPS,
                tags=(_ROUNDED_SURFACE_TAG,),
            )
        self._canvas.tag_lower(_ROUNDED_SURFACE_TAG)

    def close(self) -> None:
        """Prevent later event delivery from drawing into a destroyed surface."""
        if self._closed:
            return
        self._closed = True
        try:
            self._canvas.delete(_ROUNDED_SURFACE_TAG)
        except tk.TclError:
            pass


class RoundedSectionSurface:
    """A resize-aware rounded card that hosts ordinary Tk content widgets."""

    def __init__(
        self,
        parent,
        *,
        metrics: ScaledPreferencesVisualMetrics,
        palette: PreferencesVisualPalette = PREFERENCES_VISUAL_PALETTE,
    ) -> None:
        self._metrics = metrics
        self._palette = palette
        self._closed = False
        self.widget = tk.Canvas(
            parent,
            bg=palette.surface_background,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        self._renderer = RoundedSurfaceRenderer(self.widget)
        self.content = tk.Frame(self.widget, bg=palette.section_background)
        self._content_window = self.widget.create_window(
            (metrics.section_padding_x, metrics.section_padding_y),
            window=self.content,
            anchor="nw",
        )
        self.widget.bind("<Configure>", self._on_widget_configure, add="+")
        self.widget.bind("<Destroy>", self._on_destroy, add="+")
        self.content.bind("<Configure>", self._on_content_configure, add="+")

    def pack(self, **options) -> None:
        self.widget.pack(**options)

    def grid(self, **options) -> None:
        self.widget.grid(**options)

    def set_palette(self, palette: PreferencesVisualPalette) -> None:
        """Apply a recomposed semantic palette and repaint the card."""
        self._palette = palette
        self.widget.configure(bg=palette.surface_background)
        self.content.configure(bg=palette.section_background)
        self._redraw()

    def set_metrics(self, metrics: ScaledPreferencesVisualMetrics) -> None:
        """Apply a recomposed display-scale snapshot to the existing card."""
        self._metrics = metrics
        self.widget.coords(
            self._content_window,
            metrics.section_padding_x,
            metrics.section_padding_y,
        )
        self._sync_content_width(self.widget.winfo_width())
        self._sync_height(self.content.winfo_reqheight())
        self._redraw()

    def destroy(self) -> None:
        self.widget.destroy()

    def _on_widget_configure(self, event) -> None:
        if self._closed:
            return
        self._sync_content_width(event.width)
        self._redraw(width=event.width, height=event.height)

    def _on_content_configure(self, event) -> None:
        if self._closed:
            return
        self._sync_height(event.height)

    def _sync_content_width(self, width: int) -> None:
        content_width = max(1, width - (self._metrics.section_padding_x * 2))
        self.widget.itemconfigure(self._content_window, width=content_width)

    def _sync_height(self, content_height: int) -> None:
        target = max(
            1,
            content_height + (self._metrics.section_padding_y * 2),
        )
        # Tk may retain the default Canvas option as a unit string (for example,
        # ``7c`` on X11); requested geometry is always resolved to pixels.
        if self.widget.winfo_reqheight() != target:
            self.widget.configure(height=target)
        self._redraw(height=target)

    def _redraw(
        self,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        if self._closed:
            return
        self._renderer.redraw(
            width=self.widget.winfo_width() if width is None else width,
            height=self.widget.winfo_height() if height is None else height,
            radius=self._metrics.section_corner_radius,
            fill=self._palette.section_background,
            border=self._palette.section_background,
            border_width=0,
        )

    def _on_destroy(self, event) -> None:
        if event.widget is not self.widget or self._closed:
            return
        self._closed = True
        self._renderer.close()

class RoundedEntryControl:
    """A real Tk entry inside a rounded, state-aware vector shell."""

    def __init__(
        self,
        parent,
        *,
        textvariable,
        font,
        metrics: ScaledPreferencesVisualMetrics,
        palette: PreferencesVisualPalette = PREFERENCES_VISUAL_PALETTE,
        state: EntryState = "normal",
        width: int | None = None,
        outside_background: str | None = None,
        validate: str = "none",
        validatecommand=None,
        action_text: str | None = None,
        action_command: Callable[[], None] | None = None,
        action_font=None,
        action_width: int | None = None,
    ) -> None:
        if (action_text is None) != (action_command is None):
            raise ValueError("action_text and action_command must be supplied together")
        self._metrics = metrics
        self._palette = palette
        self._uses_default_width = width is None
        self._action_uses_default_width = action_text is not None and action_width is None
        self._outside_uses_palette = outside_background is None
        self._outside_background = (
            palette.surface_background
            if outside_background is None
            else outside_background
        )
        self._entry_state: EntryState = state
        self._interaction = ControlInteractionState(
            enabled=state != "disabled",
            readonly=state == "readonly",
        )
        self._closed = False
        self._focus_after_id = None
        self._action_width = (
            action_width or metrics.action_min_width
            if action_text is not None
            else 0
        )
        self._action_interaction = ControlInteractionState(
            enabled=state != "disabled"
        )
        self.widget = tk.Canvas(
            parent,
            width=width or metrics.numeric_control_width,
            height=metrics.control_height,
            bg=self._outside_background,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        self._renderer = RoundedSurfaceRenderer(self.widget)
        self._action_backdrop = None
        if action_text is not None:
            self._action_backdrop = self.widget.create_polygon(
                (0, 0, 0, 0, 0, 0),
                fill=palette.secondary_action_background,
                outline="",
                smooth=True,
                splinesteps=_CURVE_STEPS,
                tags=(_COMPOUND_ACTION_BACKDROP_TAG,),
            )
        self.content = tk.Frame(self.widget, bg=palette.control_background)
        self._content_window = self.widget.create_window(
            (0, 0),
            window=self.content,
            anchor="nw",
        )
        entry_options = {
            "textvariable": textvariable,
            "font": font,
            "relief": "flat",
            "borderwidth": 0,
            "highlightthickness": 0,
            "validate": validate,
        }
        if validatecommand is not None:
            entry_options["validatecommand"] = validatecommand
        self.entry = tk.Entry(self.content, **entry_options)
        self.action_button: tk.Button | None = None
        self.action_seam: tk.Frame | None = None
        if action_text is None:
            self.entry.pack(fill="both", expand=True)
        else:
            self.content.grid_rowconfigure(0, weight=1)
            self.content.grid_columnconfigure(0, weight=1)
            self.entry.grid(row=0, column=0, sticky="nsew")
            self.action_seam = tk.Frame(
                self.content,
                width=metrics.control_seam_thickness,
            )
            self.action_seam.grid(row=0, column=1, sticky="ns")
            self.content.grid_columnconfigure(2, minsize=self._action_width)
            self.action_button = tk.Button(
                self.content,
                text=action_text,
                command=action_command,
                font=action_font or font,
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
            )
            self.action_button.grid(row=0, column=2, sticky="nsew")
            self.action_button.bind("<Return>", self._invoke_action, add="+")
            self.action_button.bind("<space>", self._invoke_action, add="+")
        self._focus_members: list[tk.Widget] = []
        self.register_focus_member(self.entry)
        if self.action_button is not None:
            self.register_focus_member(self.action_button)
            self.action_button.bind(
                "<Enter>",
                self._on_action_enter,
                add="+",
            )
            self.action_button.bind(
                "<Leave>",
                self._on_action_leave,
                add="+",
            )
            self.action_button.bind(
                "<ButtonPress-1>",
                self._on_action_press,
                add="+",
            )
            self.action_button.bind(
                "<ButtonRelease-1>",
                self._on_action_release,
                add="+",
            )
        self.widget.bind("<Button-1>", self._focus_entry, add="+")
        self.widget.bind("<Enter>", self._on_enter, add="+")
        self.widget.bind("<Leave>", self._on_leave, add="+")
        self.widget.bind("<Configure>", self._on_configure, add="+")
        self.widget.bind("<Destroy>", self._on_destroy, add="+")
        self._sync_geometry()
        self._apply_visual()

    def pack(self, **options) -> None:
        self.widget.pack(**options)

    def grid(self, **options) -> None:
        self.widget.grid(**options)

    def register_focus_member(self, widget) -> None:
        """Extend the shared focus outline to a compound-control descendant."""
        if widget in self._focus_members:
            return
        self._focus_members.append(widget)
        widget.bind("<FocusIn>", self._on_focus_in, add="+")
        widget.bind("<FocusOut>", self._on_focus_out, add="+")
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")

    def set_invalid(self, invalid: bool) -> None:
        self._interaction = replace(self._interaction, invalid=bool(invalid))
        self._apply_visual()

    def set_placeholder(self, placeholder: bool) -> None:
        """Keep placeholder text muted through focus and lock state redraws."""
        self._interaction = replace(
            self._interaction,
            placeholder=bool(placeholder),
        )
        self._apply_visual()

    def set_state(self, state: EntryState) -> None:
        self._entry_state = state
        disabled = state == "disabled"
        self._interaction = replace(
            self._interaction,
            enabled=not disabled,
            hovered=False if disabled else self._interaction.hovered,
            pressed=False if disabled else self._interaction.pressed,
            focused=False if disabled else self._interaction.focused,
            readonly=state == "readonly",
        )
        self._action_interaction = replace(
            self._action_interaction,
            enabled=not disabled,
            hovered=False if disabled else self._action_interaction.hovered,
            pressed=False if disabled else self._action_interaction.pressed,
        )
        self._apply_visual()

    def set_palette(self, palette: PreferencesVisualPalette) -> None:
        self._palette = palette
        if self._outside_uses_palette:
            self._outside_background = palette.surface_background
            self.widget.configure(bg=self._outside_background)
        self._apply_visual()

    def set_metrics(self, metrics: ScaledPreferencesVisualMetrics) -> None:
        self._metrics = metrics
        options = {"height": metrics.control_height}
        if self._uses_default_width:
            options["width"] = metrics.numeric_control_width
        self.widget.configure(**options)
        if self.action_button is not None and self._action_uses_default_width:
            self._action_width = metrics.action_min_width
            self.content.grid_columnconfigure(
                2,
                minsize=metrics.action_min_width,
            )
        self._sync_geometry()
        self._apply_visual()

    def destroy(self) -> None:
        self.widget.destroy()

    def focus_set(self) -> bool:
        """Focus the real entry when the compound control is available."""
        if self._closed or not self._interaction.enabled:
            return False
        try:
            self.entry.focus_set()
        except tk.TclError:
            return False
        return True

    def _sync_geometry(self, width: int | None = None) -> None:
        resolved_width = self.widget.winfo_width() if width is None else width
        resolved_width = max(1, resolved_width)
        height = self._metrics.control_height
        inset = max(
            self._metrics.control_corner_radius,
            self._metrics.control_border_thickness,
            self._metrics.control_content_pad_x,
        )
        self.widget.coords(self._content_window, inset, 0)
        self.widget.itemconfigure(
            self._content_window,
            width=max(1, resolved_width - (inset * 2)),
            height=height,
        )

    def _on_configure(self, event) -> None:
        if self._closed:
            return
        self._sync_geometry(event.width)
        self._redraw(width=event.width, height=event.height)

    def _focus_entry(self, _event=None) -> str:
        if self._interaction.enabled:
            self.entry.focus_set()
        return "break"

    def _invoke_action(self, _event=None) -> str:
        if self._interaction.enabled and self.action_button is not None:
            self.action_button.invoke()
        return "break"

    def _on_action_enter(self, _event=None) -> None:
        if self._action_interaction.enabled:
            self._action_interaction = replace(
                self._action_interaction,
                hovered=True,
            )
            self._apply_visual()

    def _on_action_leave(self, _event=None) -> None:
        if self._action_interaction.enabled:
            self._action_interaction = replace(
                self._action_interaction,
                hovered=False,
                pressed=False,
            )
            self._apply_visual()

    def _on_action_press(self, _event=None) -> None:
        if self._action_interaction.enabled:
            self._action_interaction = replace(
                self._action_interaction,
                pressed=True,
            )
            self._apply_visual()

    def _on_action_release(self, _event=None) -> None:
        if self._action_interaction.enabled:
            self._action_interaction = replace(
                self._action_interaction,
                pressed=False,
            )
            self._apply_visual()

    def _on_enter(self, _event=None) -> None:
        self._interaction = replace(self._interaction, hovered=True)

    def _on_leave(self, _event=None) -> None:
        self._interaction = replace(self._interaction, hovered=False)

    def _on_focus_in(self, _event=None) -> None:
        if self._focus_after_id is not None:
            try:
                self.widget.after_cancel(self._focus_after_id)
            except tk.TclError:
                pass
            self._focus_after_id = None
        self._interaction = replace(self._interaction, focused=True)
        self._apply_visual()

    def _on_focus_out(self, _event=None) -> None:
        if self._closed or self._focus_after_id is not None:
            return
        self._focus_after_id = self.widget.after_idle(self._sync_compound_focus)

    def _sync_compound_focus(self) -> None:
        self._focus_after_id = None
        if self._closed:
            return
        try:
            focused = self.widget.focus_get()
        except tk.TclError:
            focused = None
        self._interaction = replace(
            self._interaction,
            focused=focused in self._focus_members,
        )
        self._apply_visual()

    def _apply_visual(self) -> None:
        if self._closed:
            return
        visual = resolve_entry_visual(
            self._interaction,
            palette=self._palette,
            metrics=self._metrics,
        )
        self.content.configure(bg=visual.background)
        self.entry.configure(
            bg=visual.background,
            fg=visual.foreground,
            insertbackground=visual.foreground,
            readonlybackground=visual.background,
            disabledbackground=visual.background,
            disabledforeground=visual.foreground,
            state=self._entry_state,
        )
        if self.action_button is not None:
            action_visual = resolve_action_visual(
                self._action_interaction,
                kind="secondary",
                palette=self._palette,
                metrics=self._metrics,
            )
            self.action_button.configure(
                bg=action_visual.background,
                fg=action_visual.foreground,
                activebackground=self._palette.secondary_action_hover_background,
                activeforeground=self._palette.secondary_action_text,
                disabledforeground=self._palette.disabled_action_text,
                state="normal" if self._interaction.enabled else "disabled",
                takefocus=self._interaction.enabled,
            )
        if self.action_seam is not None:
            self.action_seam.configure(bg=self._palette.control_border)
        self._redraw(visual=visual)

    def _redraw(
        self,
        *,
        visual: ResolvedControlVisual | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        if self._closed:
            return
        resolved = visual or resolve_entry_visual(
            self._interaction,
            palette=self._palette,
            metrics=self._metrics,
        )
        self._renderer.redraw(
            width=self.widget.winfo_width() if width is None else width,
            height=self.widget.winfo_height() if height is None else height,
            radius=self._metrics.control_corner_radius,
            fill=resolved.background,
            border=resolved.border,
            border_width=resolved.border_width,
        )
        if self._action_backdrop is not None:
            action_visual = resolve_action_visual(
                self._action_interaction,
                kind="secondary",
                palette=self._palette,
                metrics=self._metrics,
            )
            resolved_width = self.widget.winfo_width() if width is None else width
            resolved_height = self.widget.winfo_height() if height is None else height
            inset = resolved.border_width
            start_x = (
                resolved_width
                - self._metrics.control_content_pad_x
                - self._action_width
            )
            self.widget.coords(
                self._action_backdrop,
                *right_rounded_segment_points(
                    resolved_width,
                    resolved_height,
                    max(0, self._metrics.control_corner_radius - inset),
                    start_x,
                    inset=inset,
                ),
            )
            self.widget.itemconfigure(
                self._action_backdrop,
                fill=action_visual.background,
            )

    def _on_destroy(self, event) -> None:
        if event.widget is not self.widget or self._closed:
            return
        self._closed = True
        if self._focus_after_id is not None:
            try:
                self.widget.after_cancel(self._focus_after_id)
            except tk.TclError:
                pass
            self._focus_after_id = None
        self._renderer.close()


class RoundedActionButton:
    """A real Tk button presented inside a rounded vector action surface."""

    def __init__(
        self,
        parent,
        *,
        text: str,
        command: Callable[[], None],
        font,
        metrics: ScaledPreferencesVisualMetrics,
        palette: PreferencesVisualPalette = PREFERENCES_VISUAL_PALETTE,
        kind: ActionKind = "primary",
        enabled: bool = True,
        width: int | None = None,
        outside_background: str | None = None,
    ) -> None:
        self._command = command
        self._metrics = metrics
        self._palette = palette
        self._kind: ActionKind = kind
        self._interaction = ControlInteractionState(enabled=bool(enabled))
        self._closed = False
        self._uses_default_width = width is None
        self._outside_uses_palette = outside_background is None
        self._outside_background = (
            palette.surface_background
            if outside_background is None
            else outside_background
        )
        resolved_width = width or metrics.action_min_width
        self.widget = tk.Canvas(
            parent,
            width=resolved_width,
            height=metrics.control_height,
            bg=self._outside_background,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        self._renderer = RoundedSurfaceRenderer(self.widget)
        self.button = tk.Button(
            self.widget,
            text=text,
            command=self._invoke,
            font=font,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            takefocus=enabled,
        )
        self._button_window = self.widget.create_window(
            (resolved_width // 2, metrics.control_height // 2),
            window=self.button,
            anchor="center",
        )
        for target in (self.widget, self.button):
            target.bind("<Enter>", self._on_enter, add="+")
            target.bind("<Leave>", self._on_leave, add="+")
            target.bind("<ButtonPress-1>", self._on_press, add="+")
            target.bind("<ButtonRelease-1>", self._on_release, add="+")
        self.widget.bind("<Button-1>", self._focus_button, add="+")
        self.button.bind("<Return>", self._invoke_from_key, add="+")
        self.button.bind("<space>", self._invoke_from_key, add="+")
        self.button.bind("<FocusIn>", self._on_focus_in, add="+")
        self.button.bind("<FocusOut>", self._on_focus_out, add="+")
        self.widget.bind("<Configure>", self._on_configure, add="+")
        self.widget.bind("<Destroy>", self._on_destroy, add="+")
        self._sync_geometry(resolved_width, metrics.control_height)
        self._apply_visual()

    def pack(self, **options) -> None:
        self.widget.pack(**options)

    def grid(self, **options) -> None:
        self.widget.grid(**options)

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self._interaction = replace(
            self._interaction,
            enabled=enabled,
            hovered=self._interaction.hovered if enabled else False,
            pressed=self._interaction.pressed if enabled else False,
            focused=self._interaction.focused if enabled else False,
        )
        self._apply_visual()

    def set_kind(self, kind: ActionKind) -> None:
        self._kind = kind
        self._apply_visual()

    def set_palette(self, palette: PreferencesVisualPalette) -> None:
        self._palette = palette
        if self._outside_uses_palette:
            self._outside_background = palette.surface_background
            self.widget.configure(bg=self._outside_background)
        self._apply_visual()

    def set_metrics(self, metrics: ScaledPreferencesVisualMetrics) -> None:
        self._metrics = metrics
        options = {"height": metrics.control_height}
        if self._uses_default_width:
            options["width"] = metrics.action_min_width
        self.widget.configure(**options)
        self._sync_geometry(self.widget.winfo_width(), metrics.control_height)
        self._apply_visual()

    def configure_text(self, text: str) -> None:
        self.button.configure(text=text)

    def focus_set(self) -> bool:
        """Focus the real button when the rounded action is available."""
        if self._closed or not self._interaction.enabled:
            return False
        try:
            self.button.focus_set()
        except tk.TclError:
            return False
        return True

    def destroy(self) -> None:
        self.widget.destroy()

    def _invoke(self) -> None:
        if self._interaction.enabled:
            self._command()

    def _invoke_from_key(self, _event=None) -> str:
        self._invoke()
        return "break"

    def _focus_button(self, _event=None) -> None:
        self.focus_set()

    def _on_enter(self, _event=None) -> None:
        if not self._interaction.enabled:
            return
        self._interaction = replace(self._interaction, hovered=True)
        self._apply_visual()

    def _on_leave(self, _event=None) -> None:
        if not self._interaction.enabled:
            return
        self._interaction = replace(
            self._interaction,
            hovered=False,
            pressed=False,
        )
        self._apply_visual()

    def _on_press(self, _event=None) -> None:
        if not self._interaction.enabled:
            return
        self._interaction = replace(self._interaction, pressed=True)
        self._apply_visual()

    def _on_release(self, _event=None) -> None:
        if not self._interaction.enabled:
            return
        was_pressed = self._interaction.pressed
        self._interaction = replace(self._interaction, pressed=False)
        self._apply_visual()
        if (
            was_pressed
            and _event is not None
            and _event.widget is self.widget
        ):
            self._invoke()

    def _on_focus_in(self, _event=None) -> None:
        self._interaction = replace(self._interaction, focused=True)
        self._apply_visual()

    def _on_focus_out(self, _event=None) -> None:
        self._interaction = replace(
            self._interaction,
            focused=False,
            pressed=False,
        )
        self._apply_visual()

    def _on_configure(self, event) -> None:
        if self._closed:
            return
        self._sync_geometry(event.width, event.height)
        self._redraw(width=event.width, height=event.height)

    def _sync_geometry(self, width: int, height: int) -> None:
        inset = max(
            self._metrics.control_corner_radius,
            self._metrics.control_content_pad_x,
        )
        self.widget.coords(self._button_window, width // 2, height // 2)
        self.widget.itemconfigure(
            self._button_window,
            width=max(1, width - (inset * 2)),
            height=max(1, height - (self._metrics.control_border_thickness * 2)),
        )

    def _apply_visual(self) -> None:
        if self._closed:
            return
        visual = resolve_action_visual(
            self._interaction,
            kind=self._kind,
            palette=self._palette,
            metrics=self._metrics,
        )
        state = "normal" if self._interaction.enabled else "disabled"
        self.button.configure(
            bg=visual.background,
            fg=visual.foreground,
            activebackground=visual.background,
            activeforeground=visual.foreground,
            disabledforeground=visual.foreground,
            state=state,
            takefocus=self._interaction.enabled,
        )
        self._redraw(visual=visual)

    def _redraw(
        self,
        *,
        visual: ResolvedControlVisual | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        if self._closed:
            return
        resolved = visual or resolve_action_visual(
            self._interaction,
            kind=self._kind,
            palette=self._palette,
            metrics=self._metrics,
        )
        self._renderer.redraw(
            width=self.widget.winfo_width() if width is None else width,
            height=self.widget.winfo_height() if height is None else height,
            radius=self._metrics.control_corner_radius,
            fill=resolved.background,
            border=resolved.border,
            border_width=resolved.border_width,
        )

    def _on_destroy(self, event) -> None:
        if event.widget is not self.widget or self._closed:
            return
        self._closed = True
        self._renderer.close()
