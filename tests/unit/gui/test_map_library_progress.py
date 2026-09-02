"""Verify Map Library flat-progress state and callback ownership."""

from __future__ import annotations

from types import SimpleNamespace

from caveviewer.gui.map_library_panel import MapLibraryPanel


class _Root:
    def __init__(self) -> None:
        self.cancelled = []

    def after(self, _delay, _callback):
        return "progress-after"

    def after_cancel(self, after_id) -> None:
        self.cancelled.append(after_id)


class _Canvas:
    def __init__(self) -> None:
        self._cv_progress_fraction = 0.0
        self._cv_progress_phase = 0.0
        self._cv_progress_after_id = None
        self._cv_progress_visible = False
        self.pack_calls = []
        self.pack_forget_calls = 0
        self.rectangles = []

    def pack(self, **options) -> None:
        self.pack_calls.append(options)

    def pack_forget(self) -> None:
        self.pack_forget_calls += 1

    def winfo_width(self) -> int:
        return 100

    def cget(self, option: str):
        assert option == "height"
        return 3

    def delete(self, _tag) -> None:
        self.rectangles = []

    def create_rectangle(self, *coordinates, **options) -> None:
        self.rectangles.append((coordinates, options))


def _panel() -> MapLibraryPanel:
    panel = object.__new__(MapLibraryPanel)
    panel.root = _Root()
    panel._px = round
    panel._widget_exists = lambda _widget: True
    panel._style = SimpleNamespace(
        progress_track_color="#3B3428",
        progress_fill_color="#FFB000",
    )
    return panel


def test_determinate_row_progress_is_monotonic_within_one_operation():
    panel = _panel()
    canvas = _Canvas()
    row = SimpleNamespace(progress_bar_canvas=canvas)

    panel._set_row_progress_bar(row, 0.7)
    panel._set_row_progress_bar(row, 0.2)

    assert canvas._cv_progress_fraction == 0.7
    assert canvas.rectangles[-1][0] == (0.0, 0, 70.0, 3)
    assert canvas.pack_calls == []


def test_indeterminate_row_progress_cancels_owned_callback_when_hidden():
    panel = _panel()
    canvas = _Canvas()
    row = SimpleNamespace(progress_bar_canvas=canvas)

    panel._set_row_progress_bar(row, None)
    assert canvas._cv_progress_after_id == "progress-after"

    panel._hide_row_progress(row)

    assert panel.root.cancelled == ["progress-after"]
    assert canvas._cv_progress_after_id is None
    assert canvas.pack_forget_calls == 0
    assert canvas.rectangles == []
