"""Tests for application-branded user notifications."""

from __future__ import annotations

from caveviewer.gui import notifications
from caveviewer.version import APP_NAME


def test_all_notification_levels_use_caveviewer_title_and_preserve_parent(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        notifications,
        "show_message",
        lambda parent, **options: calls.append((parent, options)),
    )
    parent = object()

    notifications.show_info("ready", parent=parent)
    notifications.show_warning("careful", parent=parent)
    notifications.show_error("failed", parent=parent)

    assert calls == [
        (parent, {"title": APP_NAME, "message": "ready", "kind": "info"}),
        (parent, {"title": APP_NAME, "message": "careful", "kind": "warning"}),
        (parent, {"title": APP_NAME, "message": "failed", "kind": "error"}),
    ]


def test_notification_does_not_create_a_second_tk_root(monkeypatch):
    import tkinter as tk

    monkeypatch.setattr(tk, "_default_root", None)

    try:
        notifications.show_error("failed")
    except RuntimeError as exc:
        assert "application window" in str(exc)
    else:
        raise AssertionError("notification unexpectedly created a Tk root")
