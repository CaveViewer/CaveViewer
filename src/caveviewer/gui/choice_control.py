"""A keyboard-accessible choice menu using the shared rounded action control."""

import tkinter as tk
from dataclasses import replace
from typing import Callable

from caveviewer.gui.preferences_controls import RoundedActionButton


class RoundedChoiceControl:
    """Present a fixed choice list without accepting arbitrary typed values."""

    def __init__(self, parent, *, choices: tuple[tuple[str, str], ...], value: str,
                 on_selected: Callable[[str], None], font, metrics, palette, width: int):
        self._choices = dict(choices)
        self._metrics = metrics
        self._palette = palette
        choice_palette = replace(
            palette,
            secondary_action_background=palette.section_background,
            secondary_action_border=palette.tab_indicator,
            secondary_action_text=palette.heading_text,
        )
        self._action = RoundedActionButton(
            parent, text=self._choices[value], command=self.open_menu, font=font,
            metrics=metrics, palette=choice_palette, kind="secondary", width=width,
            outside_background=palette.section_background,
        )
        self.widget = self._action.widget
        self.button = self._action.button
        self.button.configure(anchor="w", padx=0)
        self.button._cv_accessible_name = "Log Information"
        self._menu = tk.Menu(
            self.widget, tearoff=False, font=font,
            background=palette.section_background, foreground=palette.heading_text,
            activebackground=palette.secondary_action_hover_background,
            activeforeground=palette.primary_action_background,
            relief="solid", borderwidth=metrics.control_border_thickness,
        )
        for key, label in choices:
            self._menu.add_command(label=label, command=lambda key=key: on_selected(key))
        self.button.bind("<Down>", self._open_from_key)
        self.button.bind("<Up>", self._open_from_key)
        self.widget.bind("<Configure>", self._layout, add="+")
        self._layout()

    def set_value(self, value: str) -> None:
        self.button.configure(text=self._choices[value])

    def _layout(self, event=None) -> None:
        width = event.width if event is not None else int(self.widget.cget("width"))
        height = self._metrics.control_height
        pad = self._metrics.control_content_pad_x
        # Reserve a separate amber arrow lane beside the native text button.
        self.widget.coords(self._action._button_window, width // 2 - pad, height // 2)
        self.widget.itemconfigure(
            self._action._button_window,
            width=max(1, width - pad * 4),
            height=max(1, height - self._metrics.focus_border_thickness * 2),
        )
        self.widget.delete("choice-arrow")
        x, y = width - pad * 2, height / 2
        self.widget.create_line(
            x - pad / 3, y - pad / 6, x, y + pad / 6,
            x + pad / 3, y - pad / 6,
            fill=self._palette.primary_action_background,
            width=self._metrics.control_border_thickness * 2, tags="choice-arrow",
        )

    def open_menu(self) -> None:
        self.button.focus_set()
        try:
            self._menu.tk_popup(
                self.widget.winfo_rootx(),
                self.widget.winfo_rooty() + self.widget.winfo_height(),
            )
        finally:
            self._menu.grab_release()

    def _open_from_key(self, _event=None) -> str:
        self.open_menu()
        return "break"

    def dismiss(self) -> None:
        self._menu.unpost()
