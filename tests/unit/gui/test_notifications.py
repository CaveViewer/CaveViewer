"""Tests for application-branded user notifications."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from caveviewer.gui import notifications
from caveviewer.version import APP_NAME


def test_all_notification_levels_use_caveviewer_title_and_preserve_parent(
    monkeypatch,
):
    calls = []
    tkinter = ModuleType("tkinter")
    tkinter.messagebox = SimpleNamespace(
        showinfo=lambda *args, **kwargs: calls.append(("info", args, kwargs)),
        showwarning=lambda *args, **kwargs: calls.append(("warning", args, kwargs)),
        showerror=lambda *args, **kwargs: calls.append(("error", args, kwargs)),
    )
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    parent = object()

    notifications.show_info("ready")
    notifications.show_warning("careful", parent=parent)
    notifications.show_error("failed", parent=parent)

    assert calls == [
        ("info", (APP_NAME, "ready"), {}),
        ("warning", (APP_NAME, "careful"), {"parent": parent}),
        ("error", (APP_NAME, "failed"), {"parent": parent}),
    ]
