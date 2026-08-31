"""Application-styled synchronous confirmation and message modals."""

from __future__ import annotations

import tkinter as tk
from typing import Literal

from caveviewer.gui.dialog_style import (
    create_dialog_action_button,
    set_dialog_action_button,
)
from caveviewer.gui.dpi_utils import tk_display_scale
from caveviewer.gui.platform.presentation import get_presentation_profile
from caveviewer.gui.tk_theme import DARK_THEME
from caveviewer.gui.tk_typography import create_tk_typography


MessageKind = Literal["info", "warning", "error"]
MODAL_MIN_WIDTH = 430
MODAL_MIN_HEIGHT = 220
MODAL_CONTENT_PAD_X = 28
MODAL_CONTENT_PAD_Y = 24


def _center_dialog(dialog, parent, *, width: int, height: int) -> None:
    """Center one bounded modal over its owning application window."""
    try:
        screen_width = dialog.winfo_screenwidth()
        screen_height = dialog.winfo_screenheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        x = max(0, min(x, screen_width - width))
        y = max(0, min(y, screen_height - height))
        dialog.geometry(f"{width}x{height}+{x}+{y}")
    except tk.TclError:
        dialog.geometry(f"{width}x{height}")


def _inherit_window_icon(dialog, parent) -> None:
    """Reuse the parent's retained Tk icon without resolving branding again."""
    icon = getattr(parent, "_cv_app_icon_photo", None)
    if icon is None:
        return
    try:
        dialog.iconphoto(True, icon)
        dialog._cv_app_icon_photo = icon
    except tk.TclError:
        pass


def _replace_clipboard(clipboard, text: str) -> bool:
    """Replace the Tk clipboard without allowing platform errors to close a modal."""
    try:
        clipboard.clipboard_clear()
        clipboard.clipboard_append(text)
    except Exception:
        return False
    return True


def _show_modal(
    parent,
    *,
    title: str,
    message: str,
    confirm_text: str,
    cancel_text: str | None,
    kind: MessageKind,
    copy_details: str | None = None,
) -> bool:
    """Show one branded modal and return whether its primary action was used."""
    profile = get_presentation_profile()
    typography = create_tk_typography(profile.ui_font_family)
    display_scale = tk_display_scale(parent, presentation_profile=profile)

    def px(value: int | float) -> int:
        return max(1, int(round(float(value) * display_scale)))

    dialog = tk.Toplevel(parent)
    dialog.withdraw()
    dialog.title(title)
    dialog.configure(bg=DARK_THEME.background)
    dialog.resizable(False, False)
    dialog.transient(parent)
    _inherit_window_icon(dialog, parent)

    result = False
    content = tk.Frame(dialog, bg=DARK_THEME.background)
    content.pack(
        fill="both",
        expand=True,
        padx=px(MODAL_CONTENT_PAD_X),
        pady=px(MODAL_CONTENT_PAD_Y),
    )
    title_color = (
        DARK_THEME.error_text if kind == "error" else DARK_THEME.title
    )
    tk.Label(
        content,
        text=title,
        font=typography.body_strong,
        fg=title_color,
        bg=DARK_THEME.background,
        anchor="w",
    ).pack(fill="x")
    tk.Label(
        content,
        text=message,
        font=typography.body,
        fg=DARK_THEME.body_text,
        bg=DARK_THEME.background,
        justify="left",
        anchor="w",
        wraplength=px(380),
    ).pack(fill="x", pady=(px(8), px(20)))
    button_row = tk.Frame(content, bg=DARK_THEME.background)
    button_row.pack(side="bottom", fill="x")

    def close(*, accepted: bool = False) -> None:
        nonlocal result
        result = accepted
        try:
            dialog.grab_release()
        except tk.TclError:
            pass
        dialog.destroy()

    confirm_button = create_dialog_action_button(
        button_row,
        confirm_text,
        lambda: close(accepted=True),
        font=typography.body_strong,
        kind="primary",
        padx=px(14),
        pady=px(7),
        dialog_layout=profile.dialog_layout,
    )
    confirm_button.pack(side="right")
    if cancel_text is not None:
        def run_secondary_action() -> None:
            if copy_details is None:
                close()
                return
            copied = _replace_clipboard(dialog, copy_details)
            set_dialog_action_button(
                cancel_button,
                text="Copied" if copied else "Copy failed",
            )
            if not copied:
                try:
                    dialog.bell()
                except tk.TclError:
                    pass

        cancel_button = create_dialog_action_button(
            button_row,
            cancel_text,
            run_secondary_action,
            font=typography.body_strong,
            kind="secondary",
            padx=px(14),
            pady=px(7),
            dialog_layout=profile.dialog_layout,
        )
        cancel_button.pack(side="right", padx=(0, px(8)))

    dialog.bind("<Escape>", lambda _event: close())
    dialog.protocol("WM_DELETE_WINDOW", close)
    dialog.update_idletasks()
    width = max(px(MODAL_MIN_WIDTH), dialog.winfo_reqwidth())
    height = max(px(MODAL_MIN_HEIGHT), dialog.winfo_reqheight())
    _center_dialog(dialog, parent, width=width, height=height)
    dialog.deiconify()
    dialog.lift(parent)
    try:
        dialog.grab_set()
        confirm_button.focus_set()
    except tk.TclError:
        pass
    dialog.wait_window()
    return result


def ask_confirmation(
    parent,
    *,
    title: str,
    message: str,
    confirm_text: str,
    cancel_text: str = "Cancel",
) -> bool:
    """Ask one app-styled yes/no question."""
    return _show_modal(
        parent,
        title=title,
        message=message,
        confirm_text=confirm_text,
        cancel_text=cancel_text,
        kind="warning",
    )


def show_message(
    parent,
    *,
    title: str,
    message: str,
    kind: MessageKind,
) -> None:
    """Show one app-styled informational, warning, or error message."""
    _show_modal(
        parent,
        title=title,
        message=message,
        confirm_text="Close",
        cancel_text=None,
        kind=kind,
    )


def show_copyable_error(
    parent,
    *,
    title: str,
    message: str,
    details: str,
) -> None:
    """Show a recoverable error with non-dismissing diagnostic copy support."""
    _show_modal(
        parent,
        title=title,
        message=message,
        confirm_text="Dismiss",
        cancel_text="Copy details",
        kind="error",
        copy_details=details,
    )
