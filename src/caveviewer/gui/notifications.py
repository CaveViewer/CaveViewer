"""Application-branded Tk notification dialogs."""

from __future__ import annotations

from typing import Any

from caveviewer.version import APP_NAME
from caveviewer.gui.modal_dialog import show_message


def _show(kind: str, message: str, *, parent: Any | None = None) -> None:
    """Show one CaveViewer-styled message owned by the application window."""
    if parent is None:
        import tkinter as tk

        parent = tk._default_root
    if parent is None:
        raise RuntimeError("an application window is required for notifications")
    show_message(parent, title=APP_NAME, message=message, kind=kind)


def show_info(message: str, *, parent: Any | None = None) -> None:
    """Show a CaveViewer informational notification."""
    _show("info", message, parent=parent)


def show_warning(message: str, *, parent: Any | None = None) -> None:
    """Show a CaveViewer warning notification."""
    _show("warning", message, parent=parent)


def show_error(message: str, *, parent: Any | None = None) -> None:
    """Show a CaveViewer error notification."""
    _show("error", message, parent=parent)
