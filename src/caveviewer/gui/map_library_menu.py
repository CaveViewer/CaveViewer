"""Map Library popover rendering and Tk interaction, independent of map actions."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from math import cos, pi, sin
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageTk

from caveviewer.gui.map_library_style import MapLibraryMenuMetrics, MapLibraryPanelStyle
from caveviewer.gui.splash_visuals import (
    VectorArc,
    VectorEllipse,
    VectorPath,
    render_vector_icon,
)


@dataclass(frozen=True)
class MenuEntry:
    """Presentation input; callbacks retain ownership of all workflow decisions."""

    label: str
    action: Callable[[], None] | None
    explanation: str | None = None

    @property
    def group(self) -> str:
        if "cache" in self.label.casefold():
            return "cache"
        if self.label == "About cave":
            return "about"
        if self.label.startswith("Remove"):
            return "remove"
        return "map"


def ordered_menu_entries(entries: tuple[MenuEntry, ...]) -> tuple[MenuEntry, ...]:
    """Keep cache actions first and map/list removal last, retaining callbacks."""
    order = {"cache": 0, "about": 1, "map": 2, "remove": 3}
    return tuple(sorted(entries, key=lambda entry: order[entry.group]))


def menu_row_positions(
    entries: tuple[MenuEntry, ...], metrics: MapLibraryMenuMetrics,
) -> tuple[tuple[int, ...], tuple[int, ...], int]:
    """Return row tops and divider tops, without empty or trailing groups."""
    rows, dividers = [], []
    y = 0
    for index, entry in enumerate(entries):
        if index and entry.group != entries[index - 1].group:
            dividers.append(y)
            y += metrics.border
        rows.append(y)
        y += metrics.row_height
    return tuple(rows), tuple(dividers), y


def menu_icon(label: str, size: int, color: str) -> Image.Image:
    """Draw the reference trash, refresh, eraser and information glyphs."""
    scale = size / 16
    paths, arcs, ellipses = [], [], []

    def path(*points, glyph_scale=1.0, stroke_scale=1.0, round_vertices=True):
        paths.append(VectorPath(
            tuple(((8 + (x - 8) * glyph_scale) * scale,
                   (8 + (y - 8) * glyph_scale) * scale) for x, y in points),
            color, 2 * scale * stroke_scale, round_vertices=round_vertices,
        ))

    if label == "About cave":
        # Pillow draws outlines inward. Expand the SVG circle's centerline
        # bounds by half its 2-pixel stroke to preserve the intended diameter.
        ellipses.append(VectorEllipse(
            tuple(value * scale for value in (1 / 3, 1 / 3, 47 / 3, 47 / 3)),
            outline_color=color, outline_width=2 * scale,
        ))
        path((8, 8), (8, 10.667))
        path((8, 5.333), (8.007, 5.333))
    elif label == "Remove cache":
        path((12.5, 12.5), (6, 12.5), (3, 9.5), (9, 3.5),
             (13, 7.5), (8, 12.5), glyph_scale=1.3, stroke_scale=1.1)
        path((4.54, 7.545), (8.955, 11.959), glyph_scale=1.3, stroke_scale=1.1)
    elif "cache" in label.casefold():
        for start in (25, 205):
            arcs.append(VectorArc((8 * scale, 8 * scale), 6 * scale,
                                  start, 155, color, 2 * scale))
        path((10.667, 5.333), (14, 5.333), (14, 2))
        path((2, 14), (2, 10.667), (5.333, 10.667))
    elif label.startswith("Remove"):
        # Follow the SVG's 4/3-pixel corner radius, without vertex dots that
        # can leave bumps where the bottom edge meets the curved corners.
        # Inset the top and bottom by half a pixel to shorten the can without
        # changing its center, width, stroke or corner radius.
        path((10 / 3, 4.5),
             *((14 / 3 + 4 / 3 * cos(pi - i * pi / 16),
                40 / 3 - 0.5 + 4 / 3 * sin(pi - i * pi / 16)) for i in range(9)),
             *((34 / 3 + 4 / 3 * cos(pi / 2 - i * pi / 16),
                40 / 3 - 0.5 + 4 / 3 * sin(pi / 2 - i * pi / 16)) for i in range(9)),
             (38 / 3, 4.5), round_vertices=False)
        path((2, 4.5), (14, 4.5))
        path((5.333, 4.5), (5.333, 1.833), (10.667, 1.833), (10.667, 4.5))
    else:
        # The optional Guided Dive action uses a folded-map glyph.
        path((2, 3), (6, 1.5), (10, 3), (14, 1.5), (14, 13),
             (10, 14.5), (6, 13), (2, 14.5), (2, 3))
        path((6, 1.5), (6, 13))
        path((10, 3), (10, 14.5))
    return render_vector_icon(image_size=(size, size), paths=tuple(paths),
                              arcs=tuple(arcs), ellipses=tuple(ellipses))


def menu_surface(
    entries: tuple[MenuEntry, ...], style: MapLibraryPanelStyle, selected: int | None,
) -> Image.Image:
    """Rasterize the rounded surface, clipped selection and shadow at display size.

    Keep the outer pixels transparent so a separate backdrop can follow the
    actual card edges underneath the popup. Text remains native Tk text.
    """
    m = style.menu_metrics
    rows, dividers, height = menu_row_positions(entries, m)
    width = m.width + 2 * m.shadow_x
    total_height = height + m.shadow_top + m.shadow_bottom
    ss = min(4, max(1, 2048 // max(width, total_height)))
    size = (width * ss, total_height * ss)
    x, y, w, h = (value * ss for value in (m.shadow_x, m.shadow_top, m.width, height))
    bounds = (x, y, x + w - 1, y + h - 1)
    mask = Image.new("L", size)
    ImageDraw.Draw(mask).rounded_rectangle(bounds, m.radius * ss, fill=255)
    shadow = Image.new("L", size)
    spread, offset = m.shadow_spread * ss, m.shadow_offset * ss
    if w > 2 * spread and h > 2 * spread:
        ImageDraw.Draw(shadow).rounded_rectangle(
            (x + spread, y + spread + offset, x + w - spread - 1,
             y + h - spread + offset - 1),
            max(0, (m.radius - m.shadow_spread) * ss), fill=102,
        )
    shadow = shadow.filter(ImageFilter.GaussianBlur(m.shadow_blur * ss))
    image = Image.new("RGBA", size)
    image.putalpha(shadow)
    body = Image.new("RGBA", size, style.menu_bg)
    draw = ImageDraw.Draw(body)
    if selected is not None and entries[selected].action is not None:
        top = y + rows[selected] * ss
        draw.rectangle((x, top, x + w - 1, top + m.row_height * ss - 1),
                       fill=style.menu_hover_bg)
    for top in dividers:
        draw.rectangle((x, y + top * ss, x + w - 1,
                        y + (top + m.border) * ss - 1), fill=style.menu_border)
    draw.rounded_rectangle(bounds, m.radius * ss, outline=style.menu_border,
                           width=max(1, m.border * ss))
    image.paste(body, (0, 0), mask)
    return image.resize((width, total_height), Image.Resampling.LANCZOS)


@dataclass(frozen=True)
class MenuBackdropCard:
    """Visible card geometry in menu-local display pixels."""

    bounds: tuple[int, int, int, int]
    radius: int
    fill: str
    border: str
    border_width: int


def menu_backdrop(
    size: tuple[int, int], *, background: str,
    cards: tuple[MenuBackdropCard, ...], viewport: tuple[int, int, int, int],
) -> Image.Image:
    """Paint only the library surfaces behind one bounded in-window popup.

    Card positions include scrolling; clip them to their actual viewport so
    hidden portions cannot bleed into the application background.
    """
    ss = min(4, max(1, 2048 // max(size)))
    raster_size = tuple(value * ss for value in size)
    image = Image.new("RGBA", raster_size, background)
    card_layer = Image.new("RGBA", raster_size, background)
    draw = ImageDraw.Draw(card_layer)
    for card in cards:
        left, top, right, bottom = (value * ss for value in card.bounds)
        if right <= left or bottom <= top:
            continue
        draw.rounded_rectangle(
            (left, top, right - 1, bottom - 1), radius=card.radius * ss,
            fill=card.fill, outline=card.border, width=max(1, card.border_width * ss),
        )
    clip = (max(0, viewport[0] * ss), max(0, viewport[1] * ss),
            min(raster_size[0], viewport[2] * ss),
            min(raster_size[1], viewport[3] * ss))
    if clip[2] > clip[0] and clip[3] > clip[1]:
        image.paste(card_layer.crop(clip), clip[:2])
    return image.resize(size, Image.Resampling.LANCZOS)


class MapLibraryMenu(tk.Canvas):
    """In-window menu with native labels, full-row hit targets and owned hints."""

    def __init__(self, parent, *, entries: tuple[MenuEntry, ...],
                 style: MapLibraryPanelStyle, invoke: Callable[[Callable], None]):
        self.entries, self.style, self._invoke = entries, style, invoke
        self.rows, _, self.body_height = menu_row_positions(entries, style.menu_metrics)
        self.selected = None
        self._hint = None
        self._labels = []
        self._icons = {}
        self._surfaces = {}
        m = style.menu_metrics
        super().__init__(parent, width=m.width + 2 * m.shadow_x,
                         height=self.body_height + m.shadow_top + m.shadow_bottom,
                         bg=style.panel_color, borderwidth=0, highlightthickness=0)
        self.configure(scrollregion=(0, 0, m.width + 2 * m.shadow_x,
                                     self.body_height + m.shadow_top + m.shadow_bottom),
                       yscrollincrement=m.row_height)
        self._backdrop = self.create_image(0, 0, anchor="nw")
        self._backdrop_photo = None
        self._surface = self.create_image(0, 0, anchor="nw")
        self._icon_items = []
        for index, entry in enumerate(entries):
            top = m.shadow_top + self.rows[index]
            self._icon_items.append(self.create_image(
                m.shadow_x + m.inset + m.icon_size / 2,
                top + m.row_height / 2,
            ))
            label = tk.Label(self, text=entry.label, font=style.menu_font,
                             bg=style.menu_bg, fg=style.menu_text, anchor="w",
                             justify="left", padx=0, pady=0, borderwidth=0,
                             takefocus=True, wraplength=0)
            self.create_window(m.shadow_x + m.inset + m.icon_size + m.icon_gap,
                               top + m.row_height / 2, window=label, anchor="w",
                               width=m.width - m.inset - m.right_inset - m.icon_size - m.icon_gap)
            self._labels.append(label)
            label.bind("<Enter>", lambda _e, i=index: self.select(i))
            label.bind("<FocusIn>", lambda _e, i=index: self._focus_in(i))
            label.bind("<ButtonRelease-1>", lambda _e, i=index: self.activate(i))
            for key in ("<Return>", "<space>"):
                label.bind(key, lambda _e, i=index: self.activate(self._key_index(i)))
            label.bind("<Down>", lambda _e, i=index: self.focus_row(self._key_index(i) + 1))
            label.bind("<Up>", lambda _e, i=index: self.focus_row(self._key_index(i) - 1))
            label.bind("<Home>", lambda _e: self.focus_row(0))
            label.bind("<End>", lambda _e: self.focus_row(len(entries) - 1))
        self.bind("<Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._click)
        self.bind("<Destroy>", self._destroy_hint, add="+")
        self.bind("<Down>", lambda _e: self.focus_row(self._key_index(-1) + 1))
        self.bind("<Up>", lambda _e: self.focus_row(self._key_index(0) - 1))
        self.bind("<Home>", lambda _e: self.focus_row(0))
        self.bind("<End>", lambda _e: self.focus_row(len(entries) - 1))
        for key in ("<Return>", "<space>"):
            self.bind(key, lambda _e: self.activate(self.selected)
                      if self.selected is not None else "break")
        for widget in (self, *self._labels):
            widget.bind("<MouseWheel>", self._wheel)
            widget.bind("<Button-4>", lambda _e: self._scroll(-1))
            widget.bind("<Button-5>", lambda _e: self._scroll(1))
        self.select(None)

    def set_backdrop(self, image: Image.Image) -> None:
        """Set the viewport backing before revealing the menu."""
        self._backdrop_photo = ImageTk.PhotoImage(image, master=self)
        self.itemconfigure(self._backdrop, image=self._backdrop_photo)
        self._sync_backdrop_position()

    def _sync_backdrop_position(self) -> None:
        self.coords(self._backdrop, 0, self.canvasy(0))

    def focus_row(self, index: int) -> str:
        index %= len(self.entries)
        self._labels[index].focus_set()
        self._focus_in(index)
        return "break"

    def _key_index(self, fallback: int) -> int:
        return fallback if self.selected is None else self.selected

    def _focus_in(self, index: int) -> None:
        m = self.style.menu_metrics
        top = m.shadow_top + self.rows[index]
        visible_top = self.canvasy(0)
        if top < visible_top or top + m.row_height > visible_top + self.winfo_height():
            total = self.body_height + m.shadow_top + m.shadow_bottom
            self.yview_moveto(max(0, top - m.shadow_top) / total)
            self._sync_backdrop_position()
        self.select(index)

    def _scroll(self, direction: int) -> str:
        self.select(None)
        if self.winfo_height() < self.body_height + self.style.menu_metrics.shadow_top:
            self.yview_scroll(direction, "units")
            self._sync_backdrop_position()
        return "break"

    def _wheel(self, event) -> str:
        return self._scroll(-1 if event.delta > 0 else 1)

    def activate(self, index: int) -> str:
        action = self.entries[index].action
        if action is not None:
            self._invoke(action)
        return "break"

    def _row_at(self, x: int, y: int) -> int | None:
        m = self.style.menu_metrics
        if m.shadow_x <= x < m.shadow_x + m.width:
            for index, top in enumerate(self.rows):
                if top <= y - m.shadow_top < top + m.row_height:
                    return index
        return None

    def _motion(self, event) -> None:
        self.select(self._row_at(event.x, self.canvasy(event.y)))

    def _click(self, event) -> None:
        index = self._row_at(event.x, self.canvasy(event.y))
        if index is not None:
            self.activate(index)

    def select(self, index: int | None) -> None:
        if index == self.selected and self._surfaces:
            return
        self.selected = index
        style, m = self.style, self.style.menu_metrics
        if index not in self._surfaces:
            self._surfaces[index] = ImageTk.PhotoImage(
                menu_surface(self.entries, style, index), master=self,
            )
        self.itemconfigure(self._surface, image=self._surfaces[index])
        for i, (entry, label) in enumerate(zip(self.entries, self._labels)):
            active = i == index and entry.action is not None
            color = style.menu_selected_text if active else style.menu_text
            if entry.action is None:
                color = style.disabled_button_fg
            label.configure(bg=style.menu_hover_bg if active else style.menu_bg,
                            fg=color,
                            font=style.menu_selected_font if active else style.menu_font)
            key = (entry.label, color)
            if key not in self._icons:
                self._icons[key] = ImageTk.PhotoImage(
                    menu_icon(entry.label, m.icon_size, color), master=self,
                )
            self.itemconfigure(self._icon_items[i], image=self._icons[key])
        self._show_hint(index)

    def _destroy_hint(self, _event=None) -> None:
        if self._hint is not None:
            self._hint.destroy()
            self._hint = None

    def _show_hint(self, index: int | None) -> None:
        self._destroy_hint()
        if index is None or not self.entries[index].explanation:
            return
        root, style, m = self.master, self.style, self.style.menu_metrics
        self._hint = tk.Label(
            root, text=self.entries[index].explanation, font=style.supporting_font,
            bg=style.menu_bg, fg=style.menu_text, padx=m.inset, pady=m.shadow_top,
            wraplength=max(1, min(m.width * 2, root.winfo_width() - 4 * m.inset)),
            justify="left", borderwidth=0, highlightthickness=m.border,
            highlightbackground=style.menu_border,
        )
        self._hint.update_idletasks()
        width, height = self._hint.winfo_reqwidth(), self._hint.winfo_reqheight()
        x = max(0, min(self.winfo_x(), root.winfo_width() - width))
        y = self.winfo_y() + self.winfo_height()
        if y + height > root.winfo_height():
            y = max(0, self.winfo_y() - height)
        self._hint.place(x=x, y=y)
        self._hint.lift()
