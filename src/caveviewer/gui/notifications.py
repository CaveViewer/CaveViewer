"""Application-branded Tk notification dialogs."""

from __future__ import annotations

from typing import Any

from caveviewer.version import APP_NAME


def _show(method_name: str, message: str, *, parent: Any | None = None) -> None:
    """Show one Tk message box with CaveViewer as its application title."""
    from tkinter import messagebox

    options = {"parent": parent} if parent is not None else {}
    getattr(messagebox, method_name)(APP_NAME, message, **options)


def show_info(message: str, *, parent: Any | None = None) -> None:
    """Show a CaveViewer informational notification."""
    _show("showinfo", message, parent=parent)


def show_warning(message: str, *, parent: Any | None = None) -> None:
    """Show a CaveViewer warning notification."""
    _show("showwarning", message, parent=parent)


def show_error(message: str, *, parent: Any | None = None) -> None:
    """Show a CaveViewer error notification."""
    _show("showerror", message, parent=parent)
