"""Read-only field descriptions with native wrapping and explicit line spacing."""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont


class FieldDescription(tk.Frame):
    """Size a non-focusable description to its wrapped text in display pixels."""

    def __init__(
        self, parent, *, text: str, font: tuple, fg: str, bg: str,
        wraplength: int, line_height: int,
    ) -> None:
        super().__init__(parent, bg=bg, width=wraplength, height=line_height)
        self.pack_propagate(False)
        self._wraplength = wraplength
        self._resize_id: str | None = None
        self._font = tkfont.Font(root=self, font=font)
        # Do not clip glyphs if an unavailable face requires a taller fallback.
        extra = max(0, line_height - self._font.metrics("linespace"))
        self._text = tk.Text(
            self, font=self._font, fg=fg, bg=bg, wrap="word",
            width=1, height=1, borderwidth=0, highlightthickness=0,
            padx=0, pady=0, takefocus=False, cursor="",
            spacing1=extra // 2, spacing2=extra, spacing3=extra - extra // 2,
        )
        self._text.insert("1.0", text)
        self._text.configure(state="disabled")
        # Match static labels: no editing, focus, or private wheel scrolling.
        self._text.bindtags((str(self._text), str(self.winfo_toplevel()), "all"))
        self._text.pack(fill="both", expand=True)
        self._text.bind("<Configure>", self._schedule_resize)

    def cget(self, key: str):
        if key == "wraplength":
            return self._wraplength
        return super().cget(key)

    def configure(self, cnf=None, **kwargs):
        if "wraplength" in kwargs:
            self._wraplength = int(kwargs.pop("wraplength"))
            kwargs["width"] = self._wraplength
        return super().configure(cnf, **kwargs)

    def _schedule_resize(self, _event=None) -> None:
        if self._resize_id is None:
            self._resize_id = self.after_idle(self._resize_to_text)

    def _resize_to_text(self) -> None:
        self._resize_id = None
        pixels = self._text.tk.call(
            self._text._w, "count", "-update", "-ypixels", "1.0", "end",
        )
        height = max(1, int(pixels))
        if int(self.cget("height")) != height:
            self.configure(height=height)

    def destroy(self) -> None:
        if self._resize_id is not None:
            self.after_cancel(self._resize_id)
            self._resize_id = None
        super().destroy()
