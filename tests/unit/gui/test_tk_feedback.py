"""Tests for inline Tk feedback overlays."""

from __future__ import annotations

import sys
from types import ModuleType

from caveviewer.gui import tk_feedback


class FakeParent:
    def __init__(self, *, width: int = 640):
        self.width = width
        self.update_calls = 0
        self.after_calls = []
        self.after_cancel_calls = []

    def update_idletasks(self):
        self.update_calls += 1

    def winfo_width(self):
        return self.width

    def after(self, duration_ms, callback):
        after_id = f"after-{len(self.after_calls) + 1}"
        self.after_calls.append((after_id, duration_ms, callback))
        return after_id

    def after_cancel(self, after_id):
        self.after_cancel_calls.append(after_id)


class FakeWidget:
    instances = []

    def __init__(self, parent=None, **options):
        self.parent = parent
        self.options = options
        self.destroyed = False
        self.grid_calls = []
        self.place_calls = []
        self.lift_calls = 0
        self.column_config = []
        type(self).instances.append(self)

    def grid_columnconfigure(self, column, **options):
        self.column_config.append((column, options))

    def grid(self, **options):
        self.grid_calls.append(options)

    def place(self, **options):
        self.place_calls.append(options)

    def lift(self):
        self.lift_calls += 1

    def winfo_exists(self):
        return not self.destroyed

    def destroy(self):
        self.destroyed = True


class FakeFrame(FakeWidget):
    instances = []


class FakeLabel(FakeWidget):
    instances = []


def test_feedback_duration_policy_uses_semantic_bounded_values():
    assert tk_feedback.SUCCESS_FEEDBACK_MS == 4_000
    assert tk_feedback.INFO_FEEDBACK_MS == 5_000
    assert tk_feedback.WARNING_FEEDBACK_MS == 7_000
    assert tk_feedback.ERROR_FEEDBACK_MS == 9_000


def test_show_feedback_reuses_parent_overlay_and_dismisses_previous(monkeypatch):
    FakeFrame.instances = []
    FakeLabel.instances = []
    tkinter = ModuleType("tkinter")
    tkinter.Frame = FakeFrame
    tkinter.Label = FakeLabel
    tkinter.TclError = RuntimeError
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)

    parent = FakeParent()

    feedback = tk_feedback.show_feedback(
        parent,
        " First\nmessage ",
        kind="info",
        duration_ms=1000,
        font=("Sans", 10),
    )

    first_frame = feedback._frame
    assert first_frame is FakeFrame.instances[0]
    assert FakeLabel.instances[-1].options["text"] == "First message"
    assert FakeLabel.instances[-1].options["font"] == ("Sans", 10)
    assert first_frame.place_calls == [
        {"relx": 0.5, "rely": 1.0, "y": -18, "anchor": "s"}
    ]
    assert first_frame.lift_calls == 1
    assert parent.after_calls == [("after-1", 1000, feedback.dismiss)]

    same_feedback = tk_feedback.show_feedback(
        parent,
        "Second message",
        kind="error",
        duration_ms=None,
    )

    assert same_feedback is feedback
    assert first_frame.destroyed is True
    assert parent.after_cancel_calls == ["after-1"]
    assert feedback._frame is not first_frame
    assert FakeLabel.instances[-1].options["text"] == "Second message"
    assert parent.after_calls == [("after-1", 1000, feedback.dismiss)]

    active_frame = feedback._frame
    feedback.dismiss()
    assert active_frame.destroyed is True
