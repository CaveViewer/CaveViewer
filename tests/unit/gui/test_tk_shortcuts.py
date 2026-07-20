"""Tests for platform-aware Tk shortcut binding helpers."""

from __future__ import annotations

from caveviewer.gui.tk_shortcuts import bind_primary_shortcut, primary_modifier_event_sequence


class FakeWidget:
    def __init__(self):
        self.bind_calls = []

    def bind(self, sequence, callback, **kwargs):
        self.bind_calls.append((sequence, callback, kwargs))


def test_primary_modifier_event_sequence_uses_command_on_macos():
    assert primary_modifier_event_sequence("w", platform="darwin") == "<Command-w>"


def test_primary_modifier_event_sequence_uses_control_elsewhere():
    assert primary_modifier_event_sequence("w", platform="linux") == "<Control-w>"
    assert primary_modifier_event_sequence("w", platform="win32") == "<Control-w>"


def test_bind_primary_shortcut_returns_sequence_and_preserves_add():
    widget = FakeWidget()
    callback = object()

    sequence = bind_primary_shortcut(widget, "w", callback, add="+")

    assert sequence in {"<Command-w>", "<Control-w>"}
    assert widget.bind_calls == [(sequence, callback, {"add": "+"})]
