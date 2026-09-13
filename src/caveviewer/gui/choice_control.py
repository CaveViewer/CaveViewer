"""Rounded fixed-choice control and its owned, keyboard-accessible Tk popover."""

from __future__ import annotations

import tkinter as tk
from dataclasses import replace
from typing import Callable

from caveviewer.gui.preferences_controls import RoundedActionButton
from caveviewer.gui.rounded_surface import RoundedSurfaceRenderer, rounded_rectangle_points


CHOICE_HIGHLIGHT = "#1F2228"
CHOICE_ACCENT = "#F5C451"


class RoundedChoiceControl:
    """Present choices without changing the saved value until the owner accepts it."""

    def __init__(self, parent, *, choices: tuple[tuple[str, str], ...], value: str,
                 on_selected: Callable[[str], None], font, metrics, palette, width: int,
                 px: Callable[[float], int]):
        self._choices = dict(choices)
        self._keys = tuple(self._choices)
        self._value = value
        self._on_selected = on_selected
        self._px = px
        self._metrics = replace(metrics, control_height=px(44), control_corner_radius=px(4))
        self._palette = palette
        self._root = parent.winfo_toplevel()
        self._menu = None
        self._bindings = []
        self._focus_check = None
        self._closed = False
        self._row_font = (font[0], -px(13), *font[2:])
        choice_palette = replace(
            palette,
            secondary_action_background=palette.section_background,
            secondary_action_hover_background=CHOICE_HIGHLIGHT,
            secondary_action_pressed_background=CHOICE_HIGHLIGHT,
            secondary_action_border=palette.tab_indicator,
            secondary_action_text=palette.heading_text,
        )
        self._action = RoundedActionButton(
            parent, text=self._choices[value], command=self.open_menu,
            font=(font[0], -px(15), *font[2:]),
            metrics=self._metrics, palette=choice_palette, kind="secondary", width=width,
            outside_background=palette.section_background,
        )
        self.widget = self._action.widget
        self.button = self._action.button
        self.button.configure(anchor="w", padx=0)
        self.button._cv_accessible_name = "Log Information"
        self.button.bind("<Down>", self._open_from_key)
        self.button.bind("<Up>", self._open_from_key)
        self.widget.bind("<Configure>", self._layout, add="+")
        self.widget.bind("<Unmap>", lambda _event: self.dismiss(), add="+")
        self.widget.bind("<Destroy>", self._destroy, add="+")
        self._layout()

    @property
    def height(self) -> int:
        return self._metrics.control_height

    def set_value(self, value: str) -> None:
        self.button.configure(text=self._choices[value])
        self._value = value
        if self._menu is not None:
            self._paint_menu()

    def _layout(self, event=None) -> None:
        width = event.width if event is not None else int(self.widget.cget("width"))
        height = self.height
        left, right = self._px(18), self._px(40)
        self.widget.coords(self._action._button_window, (left + width - right) / 2, height / 2)
        self.widget.itemconfigure(
            self._action._button_window, width=max(1, width - left - right),
            height=max(1, height - self._metrics.focus_border_thickness * 2),
        )
        self.widget.delete("choice-arrow")
        x, y = width - self._px(22), height / 2
        direction = -1 if self._menu is not None else 1
        self.widget.create_line(
            x - self._px(5), y - direction * self._px(2),
            x, y + direction * self._px(2),
            x + self._px(5), y - direction * self._px(2),
            fill=CHOICE_ACCENT, width=self._px(2), tags="choice-arrow",
        )

    def open_menu(self) -> None:
        if self._closed:
            return
        if self._menu is not None:
            self.dismiss(restore_focus=True)
            return
        self.button.focus_set()
        # Flush the opener's focus-driven scroll reveal before placing the menu.
        self.widget.update_idletasks()
        if not self.widget.winfo_ismapped():
            return
        width = self.widget.winfo_width()
        self._row_height = self._px(40)
        height = self._row_height * len(self._keys)
        self._active = self._keys.index(self._value)
        menu = self._menu = tk.Canvas(
            self._root, width=width, height=height, borderwidth=0,
            highlightthickness=0, background=self._palette.section_background,
            takefocus=True,
        )
        self._renderer = RoundedSurfaceRenderer(menu)
        self._rows = []
        for index, key in enumerate(self._keys):
            label = tk.Label(
                menu, text=self._choices[key], font=self._row_font, anchor="w",
                padx=0, pady=0, borderwidth=0, foreground=self._palette.heading_text,
            )
            menu.create_window(
                self._px(12), index * self._row_height + self._row_height / 2,
                window=label, anchor="w", width=max(1, width - self._px(48)),
            )
            self._rows.append(label)
            label.bind("<Enter>", lambda _event, i=index: self._highlight(i))
            label.bind("<ButtonRelease-1>", lambda _event, i=index: self._choose(i))
        menu.bind("<Motion>", self._hover)
        menu.bind("<ButtonRelease-1>", self._click)
        menu.bind("<Down>", lambda _event: self._move(1))
        menu.bind("<Up>", lambda _event: self._move(-1))
        menu.bind("<Home>", lambda _event: self._highlight(0))
        menu.bind("<End>", lambda _event: self._highlight(len(self._keys) - 1))
        menu.bind("<Return>", lambda _event: self._choose(self._active))
        menu.bind("<space>", lambda _event: self._choose(self._active))
        menu.bind("<Escape>", lambda _event: self._cancel())
        menu.bind("<Tab>", lambda _event: self._traverse(False))
        menu.bind("<Shift-Tab>", lambda _event: self._traverse(True))
        try:
            menu.bind("<ISO_Left_Tab>", lambda _event: self._traverse(True))
        except tk.TclError:
            pass  # Some Tk builds expose reverse traversal only as Shift-Tab.
        self._paint_menu()
        gap = self._px(4)
        x = self.widget.winfo_rootx() - self._root.winfo_rootx()
        y = self.widget.winfo_rooty() - self._root.winfo_rooty()
        below = y + self.widget.winfo_height() + gap
        if below + height <= self._root.winfo_height():
            y = below
        else:
            y = max(0, y - height - gap)
        x = max(0, min(x, self._root.winfo_width() - width))
        menu.place(x=x, y=y)
        tk.Misc.lift(menu)
        menu.focus_set()
        self._layout()
        self._install_dismissal()

    def _paint_menu(self) -> None:
        menu = self._menu
        width = int(menu.cget("width"))
        height = self._row_height * len(self._keys)
        border, radius = self._metrics.control_border_thickness, self._px(4)
        self._renderer.redraw(
            width=width, height=height, radius=radius,
            fill=self._palette.section_background,
            border=self._palette.tab_indicator, border_width=border,
        )
        menu.delete("choice-state")
        # Clip the highlight to the first/last row's rounded corners, keeping
        # every edge of the outer border visible.
        top = max(border, self._active * self._row_height)
        bottom = min(height - border, (self._active + 1) * self._row_height)
        points = rounded_rectangle_points(width - border * 2, bottom - top, radius - border)
        menu.create_polygon(
            tuple(v + (border if i % 2 == 0 else top) for i, v in enumerate(points)),
            fill=CHOICE_HIGHLIGHT, outline="", smooth=True, tags="choice-state",
        )
        square_top = top if self._active else top + radius
        square_bottom = bottom if self._active < len(self._keys) - 1 else bottom - radius
        menu.create_rectangle(
            border, square_top, width - border, square_bottom,
            fill=CHOICE_HIGHLIGHT, outline="", tags="choice-state",
        )
        for index, label in enumerate(self._rows):
            label.configure(background=(CHOICE_HIGHLIGHT if index == self._active
                                        else self._palette.section_background))
        x = width - self._px(20)
        y = self._keys.index(self._value) * self._row_height + self._row_height / 2
        menu.create_line(
            x - self._px(5), y, x - self._px(2), y + self._px(3),
            x + self._px(5), y - self._px(4), fill=CHOICE_ACCENT,
            width=self._px(2), tags="choice-state",
        )

    def _highlight(self, index: int) -> str:
        self._active = index
        self._paint_menu()
        return "break"

    def _move(self, offset: int) -> str:
        return self._highlight((self._active + offset) % len(self._keys))

    def _hover(self, event) -> None:
        index = event.y // self._row_height
        if 0 <= index < len(self._keys) and index != self._active:
            self._highlight(index)

    def _click(self, event) -> str:
        index = event.y // self._row_height
        if 0 <= index < len(self._keys):
            return self._choose(index)
        return "break"

    def _choose(self, index: int) -> str:
        value = self._keys[index]
        self.dismiss(restore_focus=True)
        self._on_selected(value)
        return "break"

    def _cancel(self) -> str:
        self.dismiss(restore_focus=True)
        return "break"

    def _traverse(self, backwards: bool) -> str:
        command = "tk_focusPrev" if backwards else "tk_focusNext"
        interpreter = self.button.tk
        if not interpreter.call("info", "commands", command):
            if not interpreter.call("auto_load", command):
                # Some Tk installations omit these helpers from the cached
                # autoload index. Use Tk's own traversal implementation.
                script = interpreter.call(
                    "file", "join", interpreter.call("set", "tk_library"), "focus.tcl",
                )
                interpreter.call("source", script)
        self.dismiss()
        target = self.button.tk_focusPrev() if backwards else self.button.tk_focusNext()
        target.focus_set()
        return "break"

    def _open_from_key(self, _event=None) -> str:
        self.open_menu()
        return "break"

    @staticmethod
    def _contains(parent, widget) -> bool:
        return widget is not None and (widget == parent or str(widget).startswith(f"{parent}."))

    def _install_dismissal(self) -> None:
        ancestors = set()
        current = self.widget
        while current is not None:
            ancestors.add(current)
            current = current.master

        def pointer(event):
            if not (self._contains(self._menu, event.widget)
                    or self._contains(self.widget, event.widget)):
                self.dismiss()

        def layout(event):
            if event.widget in ancestors:
                self.dismiss()

        for sequence, callback in (
            ("<ButtonPress-1>", pointer), ("<ButtonPress-2>", pointer),
            ("<ButtonPress-3>", pointer), ("<Configure>", layout),
            ("<MouseWheel>", lambda _event: self.dismiss()),
            ("<Button-4>", lambda _event: self.dismiss()),
            ("<Button-5>", lambda _event: self.dismiss()),
            ("<FocusOut>", self._schedule_focus_check),
        ):
            self._bindings.append((sequence, self._root.bind(sequence, callback, add="+")))

    def _schedule_focus_check(self, _event) -> None:
        if self._focus_check is None:
            self._focus_check = self._root.after_idle(self._dismiss_if_focus_left)

    def _dismiss_if_focus_left(self) -> None:
        self._focus_check = None
        if self._menu is not None and not (
            self._contains(self._menu, self._root.focus_displayof())
            or self._contains(self.widget, self._root.focus_displayof())
        ):
            self.dismiss()

    def dismiss(self, *, restore_focus: bool = False) -> None:
        if self._focus_check is not None:
            self._root.after_cancel(self._focus_check)
            self._focus_check = None
        for sequence, binding in self._bindings:
            self._root.unbind(sequence, binding)
        self._bindings.clear()
        if self._menu is not None:
            menu, self._menu = self._menu, None
            self._renderer.close()
            menu.destroy()
            if not self._closed:
                self._layout()
                if restore_focus:
                    self.button.focus_set()

    def _destroy(self, event) -> None:
        if event.widget == self.widget:
            self._closed = True
            self.dismiss()
