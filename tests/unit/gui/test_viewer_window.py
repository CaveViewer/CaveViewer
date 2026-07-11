"""Tests for viewer-window startup sizing."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from caveviewer.gui import viewer_window


def test_desktop_relative_window_size_uses_eighty_percent_per_axis(monkeypatch):
    class FakeRoot:
        def __init__(self):
            self.withdrawn = False
            self.destroyed = False

        def withdraw(self):
            self.withdrawn = True

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

        def destroy(self):
            self.destroyed = True

    root = FakeRoot()
    root_options = []
    monkeypatch.setitem(
        sys.modules,
        "tkinter",
        SimpleNamespace(Tk=lambda **options: root_options.append(options) or root),
    )

    assert viewer_window._desktop_relative_window_size() == (1536, 864)
    assert root_options == [
        {"baseName": viewer_window.APP_NAME, "className": viewer_window.APP_NAME}
    ]
    assert root.withdrawn is True
    assert root.destroyed is True


def test_window_pixel_ratio_uses_framebuffer_size():
    window = SimpleNamespace(size=(1000, 700), buffer_size=(2000, 1400))

    assert viewer_window._window_pixel_ratio(window) == 2.0


def test_window_pixel_ratio_falls_back_for_missing_backend_data():
    assert viewer_window._window_pixel_ratio(SimpleNamespace(size=(1000, 700))) == 1.0


def test_linux_launch_defers_sizing_to_glfw_workarea(monkeypatch):
    calls = []
    monkeypatch.setattr(viewer_window.sys, "platform", "linux")
    monkeypatch.setattr(
        viewer_window,
        "_desktop_relative_window_size",
        lambda: (_ for _ in ()).throw(
            AssertionError("Linux sizing must not mix Tk and GLFW coordinates")
        ),
    )
    monkeypatch.setattr(
        viewer_window,
        "run_window_config",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    viewer_window._launch_viewer_window()

    assert viewer_window.CaveViewerWindow.window_size == (1600, 1000)
    assert calls[0][0] == (viewer_window.CaveViewerWindow,)
    assert calls[0][1]["window_size_fraction"] == 0.8
    assert calls[0][1]["fallback_window_size"] == (1600, 1000)
