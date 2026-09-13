"""Prepare and own the static mesh behind the Tk startup text and progress."""

from __future__ import annotations

import math
import tkinter as tk

from PIL import Image, ImageOps, ImageTk

from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.tk_theme import DARK_THEME
from caveviewer.resources import image_path


_LOGGER = get_logger(__name__)
_MESH_LINE_COLOR = "#34373E"


def render_launch_background(source: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Cover the viewport with quiet mesh detail and a clear center for live text."""
    mesh = ImageOps.fit(source, size, method=Image.Resampling.LANCZOS)
    background = ImageOps.colorize(mesh, DARK_THEME.panel, _MESH_LINE_COLOR)
    # A small mask keeps the radial fade cheap at high display resolutions.
    mask = Image.new("L", (64, 64))
    mask.putdata([
        round(240 * math.exp(-3 * (((x - 31.5) / 32) ** 2 + ((y - 31.5) / 32) ** 2)))
        for y in range(64) for x in range(64)
    ])
    mask = mask.resize(size, Image.Resampling.BILINEAR)
    background.paste(DARK_THEME.background, (0, 0, *size), mask)
    return background


class LaunchBackground:
    """Keep one source and one sized Tk image, releasing both with the canvas."""

    def __init__(self, canvas) -> None:
        self._canvas = canvas
        self._source = None
        self._photo = None
        self._size = None
        self._item = None
        try:
            with Image.open(image_path("startup-mesh.png")) as source:
                # The supplied capture includes a narrow window-edge border.
                bounds = (5, 7, source.width - 3, source.height - 5)
                self._source = source.crop(bounds).convert("L")
        except (OSError, ValueError):
            _LOGGER.warning(
                "Startup mesh could not be loaded; using the plain background",
                exc_info=True,
            )
        canvas.bind("<Destroy>", self._destroy, add="+")

    def resize(self, width: int, height: int) -> None:
        """Rebuild only for a changed viewport; progress ticks reuse the image."""
        size = (max(1, int(width)), max(1, int(height)))
        if self._source is None or size == self._size:
            return
        try:
            with render_launch_background(self._source, size) as raster:
                photo = ImageTk.PhotoImage(raster, master=self._canvas)
            if self._item is None:
                self._item = self._canvas.create_image(
                    0, 0, anchor="nw", image=photo, tags="launch_background",
                )
            else:
                self._canvas.itemconfigure(self._item, image=photo)
            self._photo = photo
            self._size = size
            self._canvas.tag_lower(self._item)
        except (OSError, ValueError, tk.TclError):
            _LOGGER.warning(
                "Startup mesh could not be drawn; retaining the launch content",
                exc_info=True,
            )

    def _destroy(self, _event=None) -> None:
        if self._source is not None:
            self._source.close()
        self._source = None
        self._photo = None
        self._canvas = None
