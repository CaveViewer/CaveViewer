"""Exercise pure state and lifecycle contracts for rounded Preferences controls."""

from types import SimpleNamespace

from caveviewer.gui.preferences_controls import (
    ControlInteractionState,
    RoundedActionButton,
    RoundedEntryControl,
    RoundedSectionSurface,
    RoundedSurfaceRenderer,
    resolve_action_visual,
    resolve_entry_visual,
    right_rounded_segment_points,
    rounded_rectangle_points,
)
from caveviewer.gui.preferences_style import (
    PREFERENCES_VISUAL_METRICS,
    PREFERENCES_VISUAL_PALETTE,
)


_METRICS = PREFERENCES_VISUAL_METRICS.scaled(round)
_PALETTE = PREFERENCES_VISUAL_PALETTE


class _FakeCanvas:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.polygons: list[tuple[tuple[int, ...], dict]] = []
        self.lowered: list[str] = []

    def delete(self, tag: str) -> None:
        self.deleted.append(tag)

    def create_polygon(self, points, **options) -> int:
        self.polygons.append((tuple(points), options))
        return len(self.polygons)

    def tag_lower(self, tag: str) -> None:
        self.lowered.append(tag)


class _FakeSectionCanvas:
    def __init__(self, *, requested_height: int, option_height: str) -> None:
        self.requested_height = requested_height
        self.option_height = option_height
        self.configure_calls: list[dict[str, int]] = []

    def cget(self, option: str) -> str:
        assert option == "height"
        return self.option_height

    def winfo_reqheight(self) -> int:
        return self.requested_height

    def configure(self, **options: int) -> None:
        self.configure_calls.append(options)
        self.requested_height = options["height"]


class _FakeFocusOwner:
    def __init__(self) -> None:
        self.after_id = "after-1"
        self.callback = None
        self.cancelled: list[str] = []
        self.focused_widget = None

    def after_idle(self, callback):
        self.callback = callback
        return self.after_id

    def after_cancel(self, after_id: str) -> None:
        self.cancelled.append(after_id)

    def focus_get(self):
        return self.focused_widget


def test_rounded_rectangle_path_clamps_radius_to_available_geometry():
    points = rounded_rectangle_points(12, 8, 20, inset=1)

    assert points[0:2] == (4, 1)
    assert points[4:6] == (8, 1)
    assert min(points[0::2]) == 1
    assert max(points[0::2]) == 11
    assert min(points[1::2]) == 1
    assert max(points[1::2]) == 7


def test_compound_action_backdrop_keeps_a_square_seam_and_rounded_right_edge():
    points = right_rounded_segment_points(100, 40, 4, 68, inset=1)

    assert min(points[0::2]) == 68
    assert max(points[0::2]) == 99
    assert min(points[1::2]) == 1
    assert max(points[1::2]) == 39


def test_entry_visual_prioritizes_disabled_then_invalid_then_focus():
    focused = resolve_entry_visual(
        ControlInteractionState(focused=True),
        palette=_PALETTE,
        metrics=_METRICS,
    )
    invalid = resolve_entry_visual(
        ControlInteractionState(focused=True, invalid=True),
        palette=_PALETTE,
        metrics=_METRICS,
    )
    disabled = resolve_entry_visual(
        ControlInteractionState(enabled=False, focused=True, invalid=True),
        palette=_PALETTE,
        metrics=_METRICS,
    )

    assert focused.border == _PALETTE.control_focus_border
    assert focused.border_width == _METRICS.focus_border_thickness
    assert invalid.border == _PALETTE.control_invalid_border
    assert disabled.border == _PALETTE.disabled_action_border
    assert disabled.foreground == _PALETTE.control_placeholder_text


def test_entry_visual_keeps_placeholder_text_muted_during_active_redraws():
    placeholder = resolve_entry_visual(
        ControlInteractionState(placeholder=True, focused=True),
        palette=_PALETTE,
        metrics=_METRICS,
    )

    assert placeholder.foreground == _PALETTE.control_placeholder_text
    assert placeholder.border == _PALETTE.control_focus_border


def test_action_visual_covers_hover_press_focus_and_disabled_states():
    hovered = resolve_action_visual(
        ControlInteractionState(hovered=True),
        kind="primary",
        palette=_PALETTE,
        metrics=_METRICS,
    )
    pressed = resolve_action_visual(
        ControlInteractionState(hovered=True, pressed=True),
        kind="primary",
        palette=_PALETTE,
        metrics=_METRICS,
    )
    focused = resolve_action_visual(
        ControlInteractionState(focused=True),
        kind="secondary",
        palette=_PALETTE,
        metrics=_METRICS,
    )
    disabled = resolve_action_visual(
        ControlInteractionState(enabled=False, hovered=True, focused=True),
        kind="secondary",
        palette=_PALETTE,
        metrics=_METRICS,
    )

    assert hovered.background == _PALETTE.primary_action_hover_background
    assert pressed.background == _PALETTE.primary_action_pressed_background
    assert focused.border == _PALETTE.control_focus_border
    assert focused.border_width == _METRICS.focus_border_thickness
    assert disabled.background == _PALETTE.disabled_action_background
    assert disabled.foreground == _PALETTE.disabled_action_text


def test_surface_renderer_replaces_geometry_on_resize_and_stops_after_close():
    canvas = _FakeCanvas()
    renderer = RoundedSurfaceRenderer(canvas)

    renderer.redraw(
        width=100,
        height=40,
        radius=4,
        fill="#111111",
        border="#222222",
        border_width=1,
    )
    renderer.redraw(
        width=160,
        height=48,
        radius=6,
        fill="#333333",
        border="#444444",
        border_width=2,
    )

    assert len(canvas.polygons) == 4
    assert canvas.deleted == ["cv-rounded-surface", "cv-rounded-surface"]
    assert canvas.polygons[-2][0][4] == 154
    assert canvas.lowered == ["cv-rounded-surface", "cv-rounded-surface"]

    renderer.close()
    renderer.redraw(
        width=200,
        height=60,
        radius=8,
        fill="#555555",
        border="#666666",
        border_width=1,
    )

    assert len(canvas.polygons) == 4
    assert canvas.deleted[-1] == "cv-rounded-surface"


def test_section_surface_resolves_unit_height_options_through_requested_pixels():
    canvas = _FakeSectionCanvas(requested_height=120, option_height="7c")
    surface = object.__new__(RoundedSectionSurface)
    surface.widget = canvas
    surface._metrics = SimpleNamespace(section_padding_y=10)
    redraw_heights: list[int] = []
    surface._redraw = lambda *, height: redraw_heights.append(height)

    surface._sync_height(100)
    canvas.requested_height = 80
    surface._sync_height(100)

    assert canvas.configure_calls == [{"height": 120}]
    assert redraw_heights == [120, 120]


def test_entry_compound_focus_waits_for_the_destination_widget():
    owner = _FakeFocusOwner()
    entry = object.__new__(RoundedEntryControl)
    member = object()
    entry.widget = owner
    entry._focus_members = [member]
    entry._focus_after_id = None
    entry._closed = False
    entry._interaction = ControlInteractionState(focused=True)
    applied: list[bool] = []
    entry._apply_visual = lambda: applied.append(entry._interaction.focused)

    entry._on_focus_out()
    owner.focused_widget = member
    owner.callback()

    assert entry._interaction.focused
    assert applied == [True]


def test_compound_entry_action_invokes_only_while_the_outer_control_is_enabled():
    invoked = []
    action = SimpleNamespace(invoke=lambda: invoked.append("browse"))
    entry = object.__new__(RoundedEntryControl)
    entry.action_button = action
    entry._interaction = ControlInteractionState(enabled=True)

    assert entry._invoke_action() == "break"
    entry._interaction = ControlInteractionState(enabled=False)
    assert entry._invoke_action() == "break"

    assert invoked == ["browse"]


def test_compound_entry_owns_one_internal_seam_and_shared_focus_members():
    import inspect

    source = inspect.getsource(RoundedEntryControl)

    assert "self.action_seam = tk.Frame(" in source
    assert "width=metrics.control_seam_thickness" in source
    assert "self.register_focus_member(self.action_button)" in source
    assert 'self.action_button.bind("<Return>", self._invoke_action' in source
    assert 'self.action_button.bind("<space>", self._invoke_action' in source
    assert 'state="normal" if self._interaction.enabled else "disabled"' in source


def test_entry_destroy_cancels_owned_focus_callback_and_closes_renderer():
    owner = _FakeFocusOwner()
    renderer = SimpleNamespace(closed=False, close=lambda: None)
    entry = object.__new__(RoundedEntryControl)
    entry.widget = owner
    entry._focus_after_id = owner.after_id
    entry._closed = False
    entry._renderer = renderer
    renderer.close = lambda: setattr(renderer, "closed", True)

    entry._on_destroy(SimpleNamespace(widget=owner))

    assert owner.cancelled == [owner.after_id]
    assert entry._focus_after_id is None
    assert entry._closed
    assert renderer.closed


def test_action_invocation_and_disabled_transition_share_one_enabled_state():
    calls: list[str] = []
    button = object.__new__(RoundedActionButton)
    button._command = lambda: calls.append("invoked")
    button._interaction = ControlInteractionState(
        enabled=True,
        hovered=True,
        pressed=True,
        focused=True,
    )
    button._apply_visual = lambda: None

    button._invoke()
    assert button._invoke_from_key() == "break"
    button.set_enabled(False)
    button._invoke()
    assert button._invoke_from_key() == "break"

    assert calls == ["invoked", "invoked"]
    assert button._interaction == ControlInteractionState(enabled=False)


def test_rounded_action_explicitly_supports_return_and_space_activation():
    import inspect

    source = inspect.getsource(RoundedActionButton)

    assert 'self.button.bind("<Return>", self._invoke_from_key' in source
    assert 'self.button.bind("<space>", self._invoke_from_key' in source
