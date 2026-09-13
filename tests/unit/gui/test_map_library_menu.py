"""Map popover geometry, rendering, action safety and native keyboard coverage."""

import logging
import tkinter as tk
from types import SimpleNamespace

import pytest
from PIL import Image, ImageTk

from caveviewer.gui.map_library_menu import (
    MapLibraryMenu, MenuEntry, menu_icon, menu_row_positions, menu_surface,
    ordered_menu_entries,
    MenuBackdropCard, menu_backdrop,
)
from caveviewer.gui.map_library_style import create_map_library_panel_style
from caveviewer.gui.map_library_panel import MapLibraryPanel
from caveviewer.gui.tk_typography import create_tk_typography


def style_at(scale=1):
    return create_map_library_panel_style(
        px=lambda value: round(value * scale),
        typography=create_tk_typography("Inter", semibold_family="Inter SemiBold",
                                        semibold_styles=()),
        progress_track_color="#111111", progress_fill_color="#ffffff",
    )


def reference_entries():
    return ordered_menu_entries(tuple(MenuEntry(label, lambda: None) for label in (
        "Remove map files", "Rebuild cache", "Remove cache", "About cave",
    )))


@pytest.mark.parametrize("scale", [1, 1.5, 2])
def test_reference_geometry_and_native_fonts_scale_once(scale):
    style = style_at(scale)
    m = style.menu_metrics
    rows, dividers, height = menu_row_positions(reference_entries(), m)
    assert m.width == round(204 * scale)
    assert m.inset == round(16 * scale)
    assert m.right_inset == round(16 * scale)
    assert rows == (0, m.row_height,
                    2 * m.row_height + m.border, 3 * m.row_height + 2 * m.border)
    assert dividers == (2 * m.row_height, 3 * m.row_height + m.border)
    assert height == 4 * m.row_height + 2 * m.border
    assert style.menu_font == ("Inter", -round(13 * scale))
    assert style.menu_selected_font == ("Inter SemiBold", -round(13 * scale))


def test_conditional_groups_do_not_leave_empty_dividers():
    entries = reference_entries()
    m = style_at().menu_metrics
    assert menu_row_positions(entries[:2], m) == ((0, 40), (), 80)
    assert menu_row_positions(entries[3:], m) == ((0,), (), 40)
    extended = ordered_menu_entries((MenuEntry("Open dive plan…", lambda: None), *entries))
    assert menu_row_positions(extended, m) == ((0, 40, 81, 122, 163), (80, 121, 162), 203)


def test_section_order_keeps_cache_order_and_removal_last():
    assert [entry.label for entry in reference_entries()] == [
        "Rebuild cache", "Remove cache", "About cave", "Remove map files",
    ]
    recent = MenuEntry("Remove from this list", lambda: None)
    disabled = MenuEntry("Resume cache rebuild", None, "Rebuild already running.")
    about = reference_entries()[2]
    entries = ordered_menu_entries((recent, about, disabled))
    assert entries == (disabled, about, recent)


def test_selected_surface_retains_inside_border_dividers_and_rounded_corners():
    style = style_at(2)
    surface = menu_surface(reference_entries(), style, 0)
    assert surface.size == (472, 388)
    assert surface.getpixel((80, 40))[:3] == (27, 30, 37)
    assert surface.getpixel((80, 140))[:3] == (21, 23, 28)
    assert surface.getpixel((32, 70))[:3] != (27, 30, 37)  # inside border
    assert surface.getpixel((32, 16))[:3] != (27, 30, 37)  # clipped corner
    assert surface.getpixel((80, 176))[:3] != (21, 23, 28)  # divider
    assert surface.getpixel((200, 351))[0] < 21  # shadow below the body


@pytest.mark.parametrize("scale", [1, 2])
def test_shadow_has_no_opaque_outer_fill_across_a_card_edge(scale):
    style = style_at(scale)
    surface = menu_surface(reference_entries(), style, None)
    assert surface.getpixel((0, 0))[3] == 0
    assert 0 < surface.getpixel((100 * scale, 175 * scale))[3] < 255
    backing = menu_backdrop(
        surface.size, background=style.panel_color,
        cards=(MenuBackdropCard(
            (-100 * scale, -100 * scale, 400 * scale, 100 * scale),
            10 * scale, style.card_color, style.panel_border_color, scale,
        ),),
        viewport=(0, 0, *surface.size),
    )
    assert backing.getpixel((5 * scale, 30 * scale))[:3] == (21, 23, 28)
    assert backing.getpixel((5 * scale, 150 * scale))[:3] == (13, 15, 19)
    # A shadow may darken either surface, but cannot paint card fill onto the
    # darker shell below it, including after hover rerenders the menu.
    for selected in (None, 0, 3):
        composite = Image.alpha_composite(backing, menu_surface(reference_entries(), style, selected))
        assert composite.getpixel((5 * scale, 150 * scale))[0] <= 13
        assert composite.getpixel((100 * scale, 185 * scale))[0] <= 13


def test_backdrop_clips_scrolled_cards_to_the_library_viewport():
    style = style_at()
    backing = menu_backdrop(
        (200, 194), background=style.panel_color,
        cards=(MenuBackdropCard((-100, -100, 400, 400), 10,
                                style.card_color, style.panel_border_color, 1),),
        viewport=(0, 20, 200, 100),
    )
    assert backing.getpixel((100, 5))[:3] == (13, 15, 19)
    assert backing.getpixel((100, 50))[:3] == (21, 23, 28)
    assert backing.getpixel((100, 150))[:3] == (13, 15, 19)


def test_disabled_entry_cannot_invoke_or_receive_the_amber_background():
    invoked = []
    entries = (MenuEntry("Rebuild cache", None, "Another rebuild is running."),)
    menu = object.__new__(MapLibraryMenu)
    menu.entries = entries
    menu._invoke = invoked.append
    assert menu.activate(0) == "break"
    assert not invoked
    assert menu_surface(entries, style_at(), 0).getpixel((40, 25))[:3] == (21, 23, 28)


def test_row_hit_targets_exclude_shadow_and_dividers():
    menu = object.__new__(MapLibraryMenu)
    menu.style = style_at()
    menu.rows, _, _ = menu_row_positions(reference_entries(), menu.style.menu_metrics)
    assert menu._row_at(17, 28) == 0
    assert menu._row_at(182, 69) == 1
    assert menu._row_at(16, 88) is None
    assert menu._row_at(15, 28) is None
    assert menu._row_at(220, 28) is None
    assert menu._row_at(40, 171) is None


@pytest.mark.parametrize("label", [entry.label for entry in reference_entries()])
def test_icons_stay_inside_their_centered_slot(label):
    icon = menu_icon(label, 32, "#F5C451")
    assert icon.size == (32, 32)
    assert icon.getbbox() is not None
    assert icon.getpixel((0, 0))[3] == 0


@pytest.mark.parametrize("size", [16, 24, 32])
def test_about_circle_matches_the_svg_centered_stroke_diameter(size):
    about = menu_icon("About cave", size, "#A9AFBC")
    rebuild = menu_icon("Rebuild cache", size, "#A9AFBC")

    def visible_diameter(icon):
        alpha = icon.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
        left, top, right, bottom = alpha.getbbox()
        return right - left, bottom - top

    # The reference path radius is 6 2/3, with a centered 2-pixel stroke:
    # outer diameter 15 1/3 logical pixels, slightly larger than Rebuild's 14.
    expected = (46 / 3) * size / 16
    diameter = visible_diameter(about)
    assert all(abs(value - expected) <= 1 for value in diameter)
    assert diameter[0] >= visible_diameter(rebuild)[0]


@pytest.fixture(scope="module")
def menu_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    root.attributes("-alpha", 0)
    yield root
    root.destroy()


@pytest.mark.gui
@pytest.mark.parametrize("scale", [1, 1.5, 2])
def test_native_menu_navigation_hints_and_destroy_cleanup(scale, menu_root):
    root = menu_root
    try:
        root.geometry("600x500")
        invoked = []
        entries = (
            MenuEntry("Remove from this list", lambda: invoked.append("remove")),
            MenuEntry("Resume cache rebuild", None, "Another rebuild is running."),
            MenuEntry("About cave", lambda: invoked.append("about")),
        )
        menu = MapLibraryMenu(root, entries=entries, style=style_at(scale),
                              invoke=lambda action: action())
        menu.place(x=30, y=30)
        root.deiconify()
        root.update()
        # Long contextual actions must fit without wrapping or clipping in
        # either font state, including at fractional display scaling.
        for selected in (None, 0):
            menu.select(selected)
            root.update_idletasks()
            for label in menu._labels:
                assert int(label.cget("wraplength")) == 0
                assert label.winfo_reqwidth() <= label.winfo_width()
        root.focus_force()
        menu.focus_row(0)
        root.update()
        # Exercise the actual Tk bindings without forcing a foreground window.
        menu._labels[1].event_generate("<Enter>")
        root.update()
        assert menu.selected == 1
        hint = menu._hint
        assert hint is not None and hint.cget("text") == entries[1].explanation
        assert menu.activate(1) == "break"
        assert not invoked
        assert all(label.winfo_reqheight() <= menu.style.menu_metrics.row_height
                   for label in menu._labels)
        menu._labels[2].event_generate("<Enter>")
        root.update()
        assert not hint.winfo_exists()
        assert menu.selected == 2
        # Pointer selection must also be the keyboard action, even while the
        # first label retains native focus (it can be a destructive action).
        menu._labels[0].event_generate("<Return>")
        assert invoked == ["about"]
        menu._labels[2].event_generate("<ButtonRelease-1>")
        assert invoked == ["about", "about"]
        menu.place_configure(height=70 * scale)
        root.update_idletasks()
        backing = Image.new("RGBA", (menu.winfo_width(), menu.winfo_height()), "#0D0F13")
        menu.set_backdrop(backing)
        menu._focus_in(2)
        top = menu.rows[2] + menu.style.menu_metrics.shadow_top
        assert menu.canvasy(0) <= top
        assert top + menu.style.menu_metrics.row_height <= menu.canvasy(menu.winfo_height())
        assert menu.coords(menu._backdrop) == [0, menu.canvasy(0)]
        assert ImageTk.getimage(menu._backdrop_photo).getpixel((0, 0))[:3] == (13, 15, 19)
        focused = []
        menu._labels = [SimpleNamespace(focus_set=lambda i=i: focused.append(i))
                        for i in range(3)]
        menu._focus_in = lambda _i: None
        assert menu.focus_row(3) == menu.focus_row(-1) == "break"
        assert focused == [0, 2]
        menu._show_hint(1)
        hint = menu._hint
        menu.destroy()
        assert not hint.winfo_exists()
    finally:
        for widget in root.winfo_children():
            widget.destroy()
        root.withdraw()


@pytest.mark.gui
def test_actual_map_row_click_opens_its_menu_and_preserves_dismissal(menu_root):
    root = menu_root
    invoked = []

    def bind_activation(widget, callback):
        def invoke(_event):
            callback()
            return "break"
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            widget.bind(sequence, invoke)

    panel = MapLibraryPanel(
        root, px=round, bind_activation=bind_activation,
        widget_exists=lambda widget: widget is not None and bool(widget.winfo_exists()),
        logger=logging.getLogger(__name__), style=style_at(),
    )
    try:
        root.geometry("1000x900")
        panel.create(root)
        rows = []
        for key, add_row in (("recent-one", panel.add_recent_row),
                             ("recent-two", panel.add_recent_row),
                             ("catalog", panel.add_standard_row)):
            entry = SimpleNamespace(key=key, title=key, detail="", size_text="",
                                    action_text="Open")
            rows.append(add_row(
                entry, action=lambda: None,
                menu_actions_factory=lambda _row, key=key: (
                    ("Remove map files", lambda: invoked.append((key, "remove"))),
                    ("Rebuild cache", lambda: invoked.append((key, "rebuild"))),
                    ("Remove cache", lambda: invoked.append((key, "cache-remove"))),
                    ("About cave", lambda: invoked.append((key, "about"))),
                ),
            ))
        panel.finish_population()
        root.deiconify()
        root.update()
        root.focus_force()

        for row, key in zip(rows, ("recent-one", "recent-two", "catalog")):
            opener = row.overflow_button
            assert opener.winfo_ismapped()
            opener.event_generate("<ButtonPress-1>", x=10, y=10)
            opener.event_generate("<ButtonRelease-1>", x=10, y=10)
            root.update()
            menu = panel._active_menu
            assert menu is not None, "Clicking the row opener must retain its menu"
            assert menu.winfo_ismapped()
            assert menu._backdrop_photo is not None
            assert panel._menu_owns_keyboard_focus(menu)
            assert root.focus_get() == menu
            assert menu.selected is None
            assert [entry.label for entry in menu.entries] == [
                "Rebuild cache", "Remove cache", "About cave", "Remove map files",
            ]
            assert all(label.cget("bg") == menu.style.menu_bg for label in menu._labels)
            before = list(invoked)
            menu.event_generate("<Return>")
            menu.event_generate("<space>")
            root.update()
            assert invoked == before
            menu.event_generate("<Down>")
            root.update()
            assert menu.selected == 0
            assert root.focus_get() == menu._labels[0]
            menu._labels[2].event_generate("<ButtonRelease-1>")
            root.update()
            assert invoked[-1] == (key, "about")
            assert panel._active_menu is None
            assert not menu.winfo_exists()
            assert not panel._active_menu_root_bindings
            opener.event_generate("<ButtonPress-1>", x=10, y=10)
            root.update()
            panel._active_menu.event_generate("<Escape>")
            root.update()
            assert panel._active_menu is None
            assert root.focus_get() == opener

            opener.event_generate("<Return>")
            root.update()
            assert panel._active_menu is not None
            menu = panel._active_menu
            assert menu.selected is None
            menu.event_generate("<Up>")
            root.update()
            assert menu.selected == len(menu.entries) - 1
            root.event_generate("<ButtonPress-1>", x=1, y=1)
            root.update()
            assert panel._active_menu is None
            assert not panel._active_menu_root_bindings
            opener.event_generate("<ButtonPress-1>", x=10, y=10)
            root.update()
            assert panel._active_menu is not None
            root.geometry(f"{root.winfo_width() + 1}x900")
            root.update()
            assert panel._active_menu is None
            assert not panel._active_menu_root_bindings
        # Native window managers may clamp the requested height. Exercise a
        # compact viewport and scroll the sampled card edge into view first.
        root.geometry("900x500")
        root.update()
        panel._content_canvas.yview_moveto(1.0)
        root.update()
        assert panel._content_canvas.yview()[0] > 0
        card = panel._standard_section.surface.widget
        bottom = card.winfo_rooty() + card.winfo_height() - root.winfo_rooty()
        center_x = card.winfo_rootx() + card.winfo_width() // 2 - root.winfo_rootx()
        backing = panel._menu_backdrop(center_x - 100, bottom - 80, 200, 194)
        assert backing.getpixel((100, 20))[:3] == (21, 23, 28)
        assert backing.getpixel((100, 160))[:3] == (13, 15, 19)
    finally:
        panel.close_active_menu()
        root.update_idletasks()
        for widget in root.winfo_children():
            widget.destroy()
        root.withdraw()
