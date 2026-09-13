"""Protect description wrapping, explicit leading, and resize callback ownership."""

import tkinter as tk
from unittest.mock import Mock

import pytest

from caveviewer.gui.field_description import FieldDescription


def test_description_resize_coalesces_and_uses_native_pixel_height():
    description = object.__new__(FieldDescription)
    description._resize_id = None
    description.after_idle = Mock(return_value="resize-1")
    description._text = Mock(_w=".description")
    description._text.tk.call.return_value = 46
    description.cget = Mock(return_value=23)
    description.configure = Mock()

    description._schedule_resize()
    description._schedule_resize()
    description.after_idle.assert_called_once()
    description._resize_to_text()

    description.configure.assert_called_once_with(height=46)
    assert description._resize_id is None


def test_description_destroy_cancels_pending_resize(monkeypatch):
    description = object.__new__(FieldDescription)
    description._resize_id = "resize-1"
    description.after_cancel = Mock()
    destroy = Mock()
    monkeypatch.setattr(tk.Frame, "destroy", destroy)

    description.destroy()

    description.after_cancel.assert_called_once_with("resize-1")
    assert description._resize_id is None
    destroy.assert_called_once()


@pytest.mark.gui
def test_description_wraps_and_shrinks_without_clipping_or_taking_focus():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    try:
        root.geometry("360x240")
        description = FieldDescription(
            root, text="Optional ceiling. Leave blank for automatic detection. "
            "A smaller detected budget takes precedence.",
            font=("Arial", -12), fg="white", bg="black",
            wraplength=360, line_height=24,
        )
        description.pack(fill="x")
        root.update()
        wide_height = description.winfo_height()
        root.geometry("180x240")
        root.update()
        narrow_height = description.winfo_height()
        assert narrow_height > wide_height
        assert narrow_height % 24 == 0
        assert description._text.dlineinfo("end-1c") is not None
        assert str(description._text.cget("state")) == "disabled"
        assert not root.tk.getboolean(description._text.cget("takefocus"))
        assert "Text" not in description._text.bindtags()
        root.geometry("360x240")
        root.update()
        assert description.winfo_height() == wide_height
    finally:
        root.destroy()
