"""Tests for shared CaveViewer modal dialog routing contracts."""

from __future__ import annotations

from caveviewer.gui import modal_dialog


def test_confirmation_routes_explicit_actions_through_the_shared_modal(monkeypatch):
    calls = []
    monkeypatch.setattr(
        modal_dialog,
        "_show_modal",
        lambda parent, **options: calls.append((parent, options)) or True,
    )
    parent = object()

    accepted = modal_dialog.ask_confirmation(
        parent,
        title="Restore default preferences?",
        message="Replace the current values?",
        confirm_text="Restore defaults",
        cancel_text="Keep current values",
    )

    assert accepted is True
    assert calls == [
        (
            parent,
            {
                "title": "Restore default preferences?",
                "message": "Replace the current values?",
                "confirm_text": "Restore defaults",
                "cancel_text": "Keep current values",
                "kind": "warning",
            },
        )
    ]


def test_message_dialog_uses_one_close_action(monkeypatch):
    calls = []
    monkeypatch.setattr(
        modal_dialog,
        "_show_modal",
        lambda parent, **options: calls.append((parent, options)) or False,
    )

    modal_dialog.show_message(
        object(),
        title="CaveViewer",
        message="Something happened.",
        kind="info",
    )

    assert calls[0][1]["confirm_text"] == "Close"
    assert calls[0][1]["cancel_text"] is None
