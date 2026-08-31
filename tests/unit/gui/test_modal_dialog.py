"""Tests for shared CaveViewer modal dialog routing contracts."""

from __future__ import annotations

import inspect

from caveviewer.gui import modal_dialog


def test_standard_modal_geometry_anchors_actions_to_the_bottom():
    source = inspect.getsource(modal_dialog._show_modal)

    assert modal_dialog.MODAL_MIN_WIDTH == 430
    assert modal_dialog.MODAL_MIN_HEIGHT == 220
    assert modal_dialog.MODAL_CONTENT_PAD_X == 28
    assert modal_dialog.MODAL_CONTENT_PAD_Y == 24
    assert 'button_row.pack(side="bottom", fill="x")' in source
    assert "px(MODAL_MIN_WIDTH)" in source
    assert "px(MODAL_MIN_HEIGHT)" in source


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


def test_copyable_error_uses_copy_details_and_explicit_dismiss(monkeypatch):
    calls = []
    monkeypatch.setattr(
        modal_dialog,
        "_show_modal",
        lambda parent, **options: calls.append((parent, options)) or False,
    )
    parent = object()

    modal_dialog.show_copyable_error(
        parent,
        title="Couldn’t open map",
        message="CaveViewer could not open this map due to an error.",
        details="Error: cache busy",
    )

    assert calls == [
        (
            parent,
            {
                "title": "Couldn’t open map",
                "message": "CaveViewer could not open this map due to an error.",
                "confirm_text": "Dismiss",
                "cancel_text": "Copy details",
                "kind": "error",
                "copy_details": "Error: cache busy",
            },
        )
    ]


def test_replace_clipboard_reports_success_and_failure():
    class Clipboard:
        def __init__(self, *, fail=False):
            self.fail = fail
            self.value = None

        def clipboard_clear(self):
            if self.fail:
                raise RuntimeError("clipboard unavailable")
            self.value = ""

        def clipboard_append(self, text):
            self.value = text

    working = Clipboard()
    assert modal_dialog._replace_clipboard(working, "details") is True
    assert working.value == "details"
    assert modal_dialog._replace_clipboard(Clipboard(fail=True), "details") is False


def test_copy_feedback_keeps_action_label_stable_and_uses_separate_status():
    source = inspect.getsource(modal_dialog._show_modal)

    assert 'cancel_text="Copy details"' not in source
    assert '"Details copied."' in source
    assert '"Couldn’t copy details."' in source
    assert "set_dialog_action_button" not in source
    assert "copy_status.config(" in source


def test_error_icon_is_geometric_and_has_an_accessible_name():
    source = inspect.getsource(modal_dialog._create_error_icon)

    assert "create_oval(" in source
    assert source.count("create_line(") == 2
    assert 'icon._cv_accessible_name = "Error"' in source
    assert "text=" not in source
