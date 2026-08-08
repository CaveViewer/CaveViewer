"""Shared Tk dialog presentation helpers.

The app still uses hand-built Tk dialogs, so keeping button and notice styling
in one place prevents Preferences, Map Library, and future dialogs from
quietly drifting apart.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Literal

from caveviewer.gui.platform.base import DialogLayoutPolicy
from caveviewer.gui.platform.presentation import get_presentation_profile
from caveviewer.gui.tk_theme import DARK_THEME


DialogButtonKind = Literal["primary", "secondary"]
DialogNoticeKind = Literal["info", "warning", "error"]

_DIALOG_LAYOUT = get_presentation_profile().dialog_layout
DIALOG_BODY_PAD_X = _DIALOG_LAYOUT.body_pad_x
DIALOG_BODY_PAD_Y = 18
DIALOG_PANEL_BORDER = DARK_THEME.entry_border

_UNSET = object()


@dataclass(frozen=True)
class _ButtonColors:
    background: str
    foreground: str
    active_background: str
    active_foreground: str
    border: str


@dataclass(frozen=True)
class _NoticeColors:
    background: str
    border: str
    foreground: str
    accent: str


_BUTTON_COLORS: dict[str, _ButtonColors] = {
    "primary": _ButtonColors(
        background=DARK_THEME.primary_button,
        foreground=DARK_THEME.primary_button_text,
        active_background=DARK_THEME.primary_button_hover,
        active_foreground=DARK_THEME.primary_button_text,
        border=DARK_THEME.primary_button_border,
    ),
    "secondary": _ButtonColors(
        background=DARK_THEME.secondary_button,
        foreground=DARK_THEME.body_text,
        active_background=DARK_THEME.secondary_button_hover,
        active_foreground=DARK_THEME.body_text,
        border=DARK_THEME.secondary_button_border,
    ),
}

_DISABLED_BUTTON_COLORS = _ButtonColors(
    background=DARK_THEME.secondary_button,
    foreground=DARK_THEME.placeholder_text,
    active_background=DARK_THEME.secondary_button,
    active_foreground=DARK_THEME.placeholder_text,
    border=DARK_THEME.entry_border,
)

_NOTICE_COLORS: dict[str, _NoticeColors] = {
    "info": _NoticeColors(
        background=DARK_THEME.panel,
        border=DARK_THEME.entry_focus_border,
        foreground=DARK_THEME.body_text,
        accent=DARK_THEME.primary_button,
    ),
    "warning": _NoticeColors(
        background="#211b10",
        border=DARK_THEME.primary_button_border,
        foreground=DARK_THEME.title,
        accent=DARK_THEME.primary_button,
    ),
    "error": _NoticeColors(
        background="#261416",
        border=DARK_THEME.invalid_border,
        foreground=DARK_THEME.error_text,
        accent=DARK_THEME.invalid_border,
    ),
}


def _button_colors(kind: DialogButtonKind, *, enabled: bool) -> _ButtonColors:
    if not enabled:
        return _DISABLED_BUTTON_COLORS
    return _BUTTON_COLORS.get(kind, _BUTTON_COLORS["secondary"])


class DialogActionLabel(tk.Label):
    """Label-backed button used where native Tk buttons cannot be styled."""

    def __init__(
        self,
        parent,
        *,
        text: str,
        command: Callable[[], None],
        font,
        kind: DialogButtonKind = "primary",
        enabled: bool = True,
        width: int | None = None,
        padx: int = 12,
        pady: int = 6,
    ) -> None:
        self._cv_kind: DialogButtonKind = kind
        self._cv_command = command
        self._cv_enabled = bool(enabled)
        colors = _button_colors(kind, enabled=enabled)
        options = {
            "text": text,
            "font": font,
            "bg": colors.background,
            "fg": colors.foreground,
            "padx": padx,
            "pady": pady,
            "cursor": "hand2" if enabled else "arrow",
            "takefocus": enabled,
            "highlightthickness": 1,
            "highlightbackground": colors.border,
            "highlightcolor": colors.border,
        }
        if width is not None:
            options["width"] = width
            options["anchor"] = "center"
        super().__init__(parent, **options)
        self.bind("<Button-1>", self._invoke)
        self.bind("<Return>", self._invoke)
        self.bind("<space>", self._invoke)
        self.bind("<Enter>", self._show_hover)
        self.bind("<Leave>", self._clear_hover)

    def _invoke(self, _event=None):
        if self._cv_enabled:
            self._cv_command()
        return "break"

    def _show_hover(self, _event=None) -> None:
        if self._cv_enabled:
            self.config(bg=_button_colors(self._cv_kind, enabled=True).active_background)

    def _clear_hover(self, _event=None) -> None:
        self.config(
            bg=_button_colors(
                self._cv_kind, enabled=self._cv_enabled
            ).background
        )

    def configure_action(
        self,
        *,
        text=_UNSET,
        command=_UNSET,
        enabled: bool | None = None,
        kind: DialogButtonKind | None = None,
    ) -> None:
        if kind is not None:
            self._cv_kind = kind
        if command is not _UNSET:
            self._cv_command = command
        if enabled is not None:
            self._cv_enabled = bool(enabled)
        colors = _button_colors(self._cv_kind, enabled=self._cv_enabled)
        options = {
            "bg": colors.background,
            "fg": colors.foreground,
            "cursor": "hand2" if self._cv_enabled else "arrow",
            "takefocus": self._cv_enabled,
            "highlightbackground": colors.border,
            "highlightcolor": colors.border,
        }
        if text is not _UNSET:
            options["text"] = text
        self.config(**options)


def create_dialog_action_button(
    parent,
    text: str,
    command: Callable[[], None],
    *,
    font,
    kind: DialogButtonKind = "primary",
    enabled: bool = True,
    width: int | None = None,
    padx: int = 12,
    pady: int = 6,
    default: str | None = None,
    dialog_layout: DialogLayoutPolicy | None = None,
):
    """Create a consistently styled action button for CaveViewer dialogs.

    ``dialog_layout`` lets a runtime-owned presentation profile control a
    dialog instance without changing the pure module default used by direct
    callers.
    """
    layout = dialog_layout or _DIALOG_LAYOUT
    if layout.use_label_action_buttons:
        return DialogActionLabel(
            parent,
            text=text,
            command=command,
            font=font,
            kind=kind,
            enabled=enabled,
            width=width,
            padx=padx,
            pady=pady,
        )

    colors = _button_colors(kind, enabled=enabled)
    options = {
        "text": text,
        "command": command if enabled else None,
        "font": font,
        "bg": colors.background,
        "fg": colors.foreground,
        "activebackground": colors.active_background,
        "activeforeground": colors.active_foreground,
        "disabledforeground": _DISABLED_BUTTON_COLORS.foreground,
        "relief": "flat",
        "borderwidth": 1,
        "highlightthickness": 1,
        "highlightbackground": colors.border,
        "highlightcolor": colors.border,
        "padx": padx,
        "pady": pady,
        "state": "normal" if enabled else "disabled",
        "cursor": "hand2" if enabled else "arrow",
        "takefocus": enabled,
    }
    if width is not None:
        options["width"] = width
    if default is not None:
        options["default"] = default
    button = tk.Button(parent, **options)
    button._cv_kind = kind
    return button


def set_dialog_action_button(
    button,
    *,
    text=_UNSET,
    command=_UNSET,
    enabled: bool | None = None,
    kind: DialogButtonKind | None = None,
) -> None:
    """Update a button created by :func:`create_dialog_action_button`."""
    if isinstance(button, DialogActionLabel):
        button.configure_action(
            text=text,
            command=command,
            enabled=enabled,
            kind=kind,
        )
        return

    current_kind = kind or getattr(button, "_cv_kind", "primary")
    if kind is not None:
        button._cv_kind = kind
    is_enabled = (
        bool(enabled)
        if enabled is not None
        else str(button.cget("state")) != "disabled"
    )
    colors = _button_colors(current_kind, enabled=is_enabled)
    options = {
        "bg": colors.background,
        "fg": colors.foreground,
        "activebackground": colors.active_background,
        "activeforeground": colors.active_foreground,
        "disabledforeground": _DISABLED_BUTTON_COLORS.foreground,
        "highlightbackground": colors.border,
        "highlightcolor": colors.border,
        "state": "normal" if is_enabled else "disabled",
        "cursor": "hand2" if is_enabled else "arrow",
        "takefocus": is_enabled,
    }
    if text is not _UNSET:
        options["text"] = text
    if command is not _UNSET:
        options["command"] = command if is_enabled else None
    button.config(**options)


def create_dialog_notice(
    parent,
    *,
    font,
    wraplength: int,
    kind: DialogNoticeKind = "info",
):
    """Create an initially empty accent notice/feedback panel."""
    colors = _NOTICE_COLORS.get(kind, _NOTICE_COLORS["info"])
    frame = tk.Frame(
        parent,
        bg=colors.background,
        highlightthickness=1,
        highlightbackground=colors.border,
        highlightcolor=colors.border,
    )
    frame.grid_columnconfigure(1, weight=1)
    accent = tk.Frame(frame, bg=colors.accent, width=4)
    accent.grid(row=0, column=0, sticky="ns")
    label = tk.Label(
        frame,
        text="",
        font=font,
        fg=colors.foreground,
        bg=colors.background,
        anchor="w",
        justify="left",
        wraplength=wraplength,
    )
    label.grid(row=0, column=1, sticky="ew", padx=(10, 12), pady=6)
    frame._cv_accent = accent
    return frame, label


def set_dialog_notice(
    frame,
    label,
    message: str,
    *,
    kind: DialogNoticeKind = "info",
) -> None:
    """Set the visual state and text for a dialog notice/feedback panel."""
    colors = _NOTICE_COLORS.get(kind, _NOTICE_COLORS["info"])
    frame.config(
        bg=colors.background,
        highlightbackground=colors.border,
        highlightcolor=colors.border,
    )
    accent = getattr(frame, "_cv_accent", None)
    if accent is not None:
        accent.config(bg=colors.accent)
    label.config(
        text=message,
        bg=colors.background,
        fg=colors.foreground,
    )
