"""Regression coverage for resize-aware rounded Tk section surfaces."""

from types import SimpleNamespace

from caveviewer.gui.rounded_surface import (
    RoundedSectionStyle,
    RoundedSectionSurface,
    RoundedSurfaceRenderer,
    rounded_rectangle_points,
)


class _FakeCanvas:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.polygons: list[tuple[tuple[int, ...], dict]] = []
        self.lowered: list[str] = []

    def delete(self, tag: str) -> None:
        self.deleted.append(tag)

    def create_polygon(self, points, **options) -> None:
        self.polygons.append((tuple(points), options))

    def tag_lower(self, tag: str) -> None:
        self.lowered.append(tag)


class _FakeSectionCanvas:
    def __init__(self) -> None:
        self.requested_height = 1
        self.width = 100
        self.height = 40
        self.configurations: list[dict] = []
        self.itemconfigurations: list[tuple[object, dict]] = []
        self.coords_calls: list[tuple] = []

    def winfo_reqheight(self) -> int:
        return self.requested_height

    def winfo_width(self) -> int:
        return self.width

    def winfo_height(self) -> int:
        return self.height

    def configure(self, **options) -> None:
        self.configurations.append(options)

    def itemconfigure(self, item, **options) -> None:
        self.itemconfigurations.append((item, options))

    def coords(self, *coordinates) -> None:
        self.coords_calls.append(coordinates)


def _style(**overrides) -> RoundedSectionStyle:
    values = {
        "outside_background": "#000000",
        "fill": "#111111",
        "border": "#222222",
        "border_width": 1,
        "radius": 12,
        "padding_x": 22,
        "padding_y": 20,
    }
    values.update(overrides)
    return RoundedSectionStyle(**values)


def test_rounded_rectangle_path_clamps_to_narrow_bounds():
    points = rounded_rectangle_points(8, 6, 12, inset=1)

    assert min(points[0::2]) == 1
    assert max(points[0::2]) == 7
    assert min(points[1::2]) == 1
    assert max(points[1::2]) == 5


def test_renderer_replaces_bordered_geometry_and_stops_after_close():
    canvas = _FakeCanvas()
    renderer = RoundedSurfaceRenderer(canvas)

    for width in (80, 120, 160):
        renderer.redraw(
            width=width,
            height=50,
            radius=12,
            fill="#111111",
            border="#222222",
            border_width=1,
        )

    assert len(canvas.polygons) == 6
    assert canvas.deleted == ["cv-rounded-surface"] * 3
    assert canvas.lowered == ["cv-rounded-surface"] * 3

    renderer.close()
    renderer.redraw(
        width=200,
        height=60,
        radius=12,
        fill="#111111",
        border="#222222",
        border_width=1,
    )

    assert len(canvas.polygons) == 6
    assert canvas.deleted == ["cv-rounded-surface"] * 4


def test_section_surface_updates_width_height_and_redraw_geometry():
    canvas = _FakeSectionCanvas()
    surface = object.__new__(RoundedSectionSurface)
    surface.widget = canvas
    surface.content = SimpleNamespace(winfo_reqheight=lambda: 60)
    surface._content_window = "content"
    surface._style = _style()
    surface._closed = False
    redraws: list[dict] = []
    surface._redraw = lambda **values: redraws.append(values)

    surface._sync_content_width(100)
    surface._sync_height(60)

    assert canvas.itemconfigurations == [("content", {"width": 56})]
    assert canvas.configurations == [{"height": 100}]
    assert redraws == [{"height": 100}]


def test_section_surface_supports_asymmetric_padding_and_a_minimum_height():
    canvas = _FakeSectionCanvas()
    surface = object.__new__(RoundedSectionSurface)
    surface.widget = canvas
    surface._style = _style(padding_bottom_y=25, minimum_height=138)
    surface._closed = False
    redraws: list[dict] = []
    surface._redraw = lambda **values: redraws.append(values)

    surface._sync_height(60)
    surface._sync_height(120)

    assert canvas.configurations == [{"height": 138}, {"height": 165}]
    assert redraws == [{"height": 138}, {"height": 165}]


def test_section_surface_destroy_event_closes_once():
    closed: list[bool] = []
    widget = object()
    surface = object.__new__(RoundedSectionSurface)
    surface.widget = widget
    surface._closed = False
    surface._renderer = SimpleNamespace(close=lambda: closed.append(True))

    surface._on_destroy(SimpleNamespace(widget=object()))
    surface._on_destroy(SimpleNamespace(widget=widget))
    surface._on_destroy(SimpleNamespace(widget=widget))

    assert closed == [True]
    assert surface._closed is True
