"""Exercise deterministic behavior of the shared themed canvas scrollbar."""

from __future__ import annotations

from types import SimpleNamespace

from caveviewer.gui.scrollable_content import (
    CanvasScrollbarStyle,
    CanvasVerticalScrollbar,
    scrollbar_thumb_geometry,
)


class _FakeCanvas:
    def __init__(self, *, height: int = 200) -> None:
        self.height = height
        self.scroll_calls = []
        self.moveto_calls = []

    def winfo_height(self) -> int:
        return self.height

    def yview_scroll(self, amount, units) -> None:
        self.scroll_calls.append((amount, units))

    def yview_moveto(self, fraction) -> None:
        self.moveto_calls.append(fraction)


class _FakeRail:
    def __init__(self, *, height: int = 200) -> None:
        self.height = height
        self.grid_calls = []
        self.configurations = []
        self.lines = []
        self.coordinates = []
        self.deleted = []
        self.item_configurations = []

    def winfo_height(self) -> int:
        return self.height

    def grid(self, **options) -> None:
        self.grid_calls.append(options)

    def grid_remove(self) -> None:
        self.grid_calls.append("remove")

    def configure(self, **options) -> None:
        self.configurations.append(options)

    def create_line(self, *coordinates, **options):
        self.lines.append((coordinates, options))
        return len(self.lines)

    def coords(self, item, *coordinates) -> None:
        self.coordinates.append((item, coordinates))

    def delete(self, item) -> None:
        self.deleted.append(item)

    def itemconfigure(self, item, **options) -> None:
        self.item_configurations.append((item, options))


class _FakeParent:
    def __init__(self) -> None:
        self.column_configurations = []

    def grid_columnconfigure(self, column: int, **options) -> None:
        self.column_configurations.append((column, options))


def _scrollbar(*, visible: bool = False) -> tuple[CanvasVerticalScrollbar, _FakeCanvas, _FakeRail]:
    scrollbar = object.__new__(CanvasVerticalScrollbar)
    canvas = _FakeCanvas()
    rail = _FakeRail()
    scrollbar._parent = _FakeParent()
    scrollbar._canvas = canvas
    scrollbar._widget = rail
    scrollbar._style = CanvasScrollbarStyle(background_color="#111111")
    scrollbar._rail_width = 14
    scrollbar._thumb_width = 5
    scrollbar._minimum_thumb_height = 36
    scrollbar._thumb = None
    scrollbar._fractions = (0.0, 0.5)
    scrollbar._visible = visible
    scrollbar._drag_offset = 0.0
    scrollbar._mounted = True
    return scrollbar, canvas, rail


def test_scrollbar_thumb_geometry_clamps_to_the_rail_and_respects_minimum_size():
    assert (
        scrollbar_thumb_geometry(
            rail_height=120,
            first=0.0,
            last=1.0,
            minimum_thumb_height=36,
        )
        is None
    )

    geometry = scrollbar_thumb_geometry(
        rail_height=120,
        first=0.5,
        last=0.6,
        minimum_thumb_height=36,
    )

    assert geometry is not None
    assert geometry.top == 42
    assert geometry.bottom == 78
    assert geometry.travel == 84


def test_canvas_scrollbar_reserves_its_gutter_and_resets_when_content_fits():
    scrollbar, canvas, rail = _scrollbar()

    scrollbar.mount_grid(row=0, column=1, sticky="ns")
    assert scrollbar._parent.column_configurations == [(1, {"minsize": 14})]
    assert rail.grid_calls == [{"row": 0, "column": 1, "sticky": "ns"}, "remove"]

    assert scrollbar.sync_overflow(320) is True
    assert scrollbar.is_visible is True
    assert rail.grid_calls[-1] == {}

    assert scrollbar.sync_overflow(200) is False
    assert scrollbar.is_visible is False
    assert rail.grid_calls[-1] == "remove"
    assert canvas.moveto_calls == [0]


def test_canvas_scrollbar_uses_normalized_wheel_input_and_dragging():
    scrollbar, canvas, rail = _scrollbar(visible=True)
    scrollbar._draw_thumb()

    assert scrollbar.scroll_from_event(SimpleNamespace(delta=-1)) == "break"
    assert canvas.scroll_calls == [(1, "units")]

    assert scrollbar._start_drag(SimpleNamespace(y=20)) == "break"
    assert scrollbar._drag(SimpleNamespace(y=110)) == "break"
    assert canvas.moveto_calls
    assert 0.0 < canvas.moveto_calls[-1] < 1.0
    assert rail.item_configurations[-1][1]["fill"] == scrollbar._style.active_thumb_color

    assert scrollbar._end_drag(SimpleNamespace()) == "break"
    assert rail.item_configurations[-1][1]["fill"] == scrollbar._style.thumb_color
