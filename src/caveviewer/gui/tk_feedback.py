"""Inline feedback overlays for Tk windows.

Recoverable UI feedback should not block the current workflow. This helper
shows one transient, in-window message per parent window so callers can replace
modal message boxes with a GNOME-style toast/banner pattern while staying on
the existing Tk shell.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from caveviewer.gui.tk_theme import DARK_THEME


FeedbackKind = Literal["info", "warning", "error"]

# Transient feedback uses semantic durations so equivalent messages behave
# consistently across panels. Progress and actionable form errors remain
# state-driven and intentionally do not use these timeout constants.
SUCCESS_FEEDBACK_MS = 4_000
INFO_FEEDBACK_MS = 5_000
WARNING_FEEDBACK_MS = 7_000
ERROR_FEEDBACK_MS = 9_000

_FEEDBACK_ATTR = "_caveviewer_inline_feedback"


@dataclass(frozen=True)
class _FeedbackColors:
    background: str
    border: str
    foreground: str
    accent: str


_COLORS: dict[str, _FeedbackColors] = {
    "info": _FeedbackColors(
        background=DARK_THEME.panel,
        border=DARK_THEME.entry_focus_border,
        foreground=DARK_THEME.body_text,
        accent=DARK_THEME.primary_button,
    ),
    "warning": _FeedbackColors(
        background="#211b10",
        border=DARK_THEME.primary_button_border,
        foreground=DARK_THEME.body_text,
        accent=DARK_THEME.primary_button,
    ),
    "error": _FeedbackColors(
        background="#261416",
        border=DARK_THEME.invalid_border,
        foreground=DARK_THEME.body_text,
        accent=DARK_THEME.invalid_border,
    ),
}


def show_feedback(
    parent: Any,
    message: str,
    *,
    kind: FeedbackKind = "info",
    duration_ms: int | None = INFO_FEEDBACK_MS,
    font: tuple | None = None,
    max_wraplength: int = 520,
) -> "TkInlineFeedback":
    """Show a transient in-window feedback message on ``parent``.

    A parent owns at most one active feedback overlay. Showing a new message
    dismisses the previous one so repeated recoverable errors do not stack.
    """
    feedback = getattr(parent, _FEEDBACK_ATTR, None)
    if not isinstance(feedback, TkInlineFeedback):
        feedback = TkInlineFeedback(parent)
        try:
            setattr(parent, _FEEDBACK_ATTR, feedback)
        except Exception:
            pass
    feedback.show(
        message,
        kind=kind,
        duration_ms=duration_ms,
        font=font,
        max_wraplength=max_wraplength,
    )
    return feedback


class TkInlineFeedback:
    """Manage one overlay feedback widget for a Tk parent window."""

    def __init__(self, parent: Any):
        self.parent = parent
        self._frame: Any | None = None
        self._after_id: Any | None = None

    def show(
        self,
        message: str,
        *,
        kind: FeedbackKind = "info",
        duration_ms: int | None = INFO_FEEDBACK_MS,
        font: tuple | None = None,
        max_wraplength: int = 520,
    ) -> None:
        """Replace the current overlay with ``message``."""
        import tkinter as tk

        self.dismiss()

        text = " ".join(str(message).split())
        colors = _COLORS.get(kind, _COLORS["info"])
        wraplength = self._wraplength(max_wraplength)

        frame = tk.Frame(
            self.parent,
            bg=colors.background,
            highlightthickness=1,
            highlightbackground=colors.border,
            highlightcolor=colors.border,
            padx=0,
            pady=0,
        )
        frame.grid_columnconfigure(1, weight=1)

        accent = tk.Frame(frame, bg=colors.accent, width=4)
        accent.grid(row=0, column=0, sticky="ns")

        label = tk.Label(
            frame,
            text=text,
            bg=colors.background,
            fg=colors.foreground,
            font=font,
            justify="left",
            wraplength=wraplength,
        )
        label.grid(row=0, column=1, sticky="w", padx=(12, 14), pady=10)

        frame.place(relx=0.5, rely=1.0, y=-18, anchor="s")
        frame.lift()

        self._frame = frame
        if duration_ms is not None and duration_ms > 0:
            self._after_id = self.parent.after(duration_ms, self.dismiss)

    def dismiss(self) -> None:
        """Dismiss the current overlay if it still exists."""
        if self._after_id is not None:
            try:
                self.parent.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

        frame = self._frame
        self._frame = None
        if frame is None:
            return
        try:
            if bool(frame.winfo_exists()):
                frame.destroy()
        except Exception:
            try:
                frame.destroy()
            except Exception:
                pass

    def _wraplength(self, max_wraplength: int) -> int:
        try:
            self.parent.update_idletasks()
        except Exception:
            pass
        try:
            parent_width = int(self.parent.winfo_width())
        except Exception:
            parent_width = max_wraplength + 64
        available = parent_width - 64 if parent_width > 0 else max_wraplength
        return max(220, min(max_wraplength, available))
