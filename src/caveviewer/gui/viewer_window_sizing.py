"""Pure desktop and framebuffer sizing policy for the native viewer window."""

from __future__ import annotations

from collections.abc import Mapping
import os

from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.platform import tk_root_options
from caveviewer.gui.platform.presentation import PresentationProfile


_LOG = get_logger("CaveViewer")

DEFAULT_WINDOW_SIZE = (1600, 1000)
DESKTOP_WINDOW_SCALE = 0.80
VIEWER_UI_BASE_WINDOW_SIZE = (1536, 864)
UI_TEXT_SCALE_ENV = "CAVEVIEWER_UI_TEXT_SCALE"
VIEWER_UI_SCALE_ENV = "CAVEVIEWER_VIEWER_UI_SCALE"
VIEWER_UI_SCALE_MAX = 1.45


def tk_root_exists(root) -> bool:
    """Return whether a Tk root-like object is still usable."""
    if root is None:
        return False
    try:
        return bool(root.winfo_exists())
    except Exception:
        return False


def screen_size_from_tk_root(root) -> tuple[int, int] | None:
    """Read a positive desktop size from a Tk root-like object."""
    try:
        desktop_width = int(root.winfo_screenwidth())
        desktop_height = int(root.winfo_screenheight())
    except Exception:
        return None
    if desktop_width <= 0 or desktop_height <= 0:
        return None
    return desktop_width, desktop_height


def window_size_from_desktop_size(
    desktop_size: tuple[int, int],
) -> tuple[int, int]:
    """Return CaveViewer's default viewer size for a detected desktop."""
    desktop_width, desktop_height = desktop_size
    if desktop_width <= 0 or desktop_height <= 0:
        return DEFAULT_WINDOW_SIZE

    window_size = (
        max(1, int(round(desktop_width * DESKTOP_WINDOW_SCALE))),
        max(1, int(round(desktop_height * DESKTOP_WINDOW_SCALE))),
    )
    _LOG.info(
        "Desktop size %dx%d; opening viewer at %dx%d.",
        desktop_width,
        desktop_height,
        *window_size,
    )
    return window_size


def desktop_relative_window_size(screen_source=None) -> tuple[int, int]:
    """Return an 80%-of-screen fallback for non-GLFW desktop backends."""
    if screen_source is not None:
        screen_size = screen_size_from_tk_root(screen_source)
        if screen_size is None:
            _LOG.warning(
                "Could not detect desktop size from existing Tk root; using %dx%d.",
                *DEFAULT_WINDOW_SIZE,
            )
            return DEFAULT_WINDOW_SIZE
        return window_size_from_desktop_size(screen_size)

    root = None
    owns_root = False
    try:
        import tkinter as tk

        default_root = getattr(tk, "_default_root", None)
        if tk_root_exists(default_root):
            screen_size = screen_size_from_tk_root(default_root)
            if screen_size is None:
                _LOG.warning(
                    "Could not detect desktop size from existing Tk root; using %dx%d.",
                    *DEFAULT_WINDOW_SIZE,
                )
                return DEFAULT_WINDOW_SIZE
            return window_size_from_desktop_size(screen_size)

        root = tk.Tk(**tk_root_options())
        owns_root = True
        root.withdraw()
        screen_size = screen_size_from_tk_root(root)
        if screen_size is None:
            return DEFAULT_WINDOW_SIZE
        return window_size_from_desktop_size(screen_size)
    except Exception as error:
        _LOG.warning(
            "Could not detect desktop size (%s); using %dx%d.",
            error,
            *DEFAULT_WINDOW_SIZE,
        )
        return DEFAULT_WINDOW_SIZE
    finally:
        if owns_root and root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def window_pixel_ratio(window) -> float:
    """Return framebuffer pixels per logical window pixel for crisp UI text."""
    try:
        width, height = window.size
        buffer_width, buffer_height = window.buffer_size
        width = max(1, int(width))
        height = max(1, int(height))
        return max(1.0, min(4.0, max(buffer_width / width, buffer_height / height)))
    except Exception:
        return 1.0


def viewer_overlay_text_scale(
    presentation_profile: PresentationProfile,
    base_scale: float,
    environ: Mapping[str, str] | None = None,
    *,
    configured_scale: float | None = None,
) -> float:
    """Return the startup text scale for FreeType-rendered viewer overlays."""
    if configured_scale is not None:
        return float(configured_scale)
    environment = os.environ if environ is None else environ
    raw_override = str(environment.get(UI_TEXT_SCALE_ENV, "")).strip()
    if raw_override:
        try:
            return float(raw_override)
        except ValueError:
            pass
    return presentation_profile.viewer_overlay_text_scale(float(base_scale))


def viewer_ui_surface_size(
    window,
    fallback_size: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Return the framebuffer-aware surface size used for HUD auto-scaling."""
    fallback = fallback_size or DEFAULT_WINDOW_SIZE
    try:
        buffer_width, buffer_height = window.buffer_size
        buffer_width = int(buffer_width)
        buffer_height = int(buffer_height)
        if buffer_width > 0 and buffer_height > 0:
            return buffer_width, buffer_height
    except Exception:
        pass
    try:
        width, height = window.size
        width = int(width)
        height = int(height)
        if width > 0 and height > 0:
            return width, height
    except Exception:
        pass
    return fallback


def viewer_ui_scale_for_window_size(
    window_size: tuple[int, int] | None,
    environ: Mapping[str, str] | None = None,
    *,
    configured_scale: float | None = None,
) -> float:
    """Return the responsive HUD scale for the current viewer surface."""
    if configured_scale is not None:
        try:
            return max(0.75, min(2.0, float(configured_scale)))
        except (TypeError, ValueError):
            pass
    else:
        environment = os.environ if environ is None else environ
        raw_override = str(environment.get(VIEWER_UI_SCALE_ENV, "")).strip()
        if raw_override:
            try:
                return max(0.75, min(2.0, float(raw_override)))
            except ValueError:
                pass

    try:
        width, height = window_size or DEFAULT_WINDOW_SIZE
        width = max(1, int(width))
        height = max(1, int(height))
    except Exception:
        width, height = DEFAULT_WINDOW_SIZE

    base_width, base_height = VIEWER_UI_BASE_WINDOW_SIZE
    size_scale = min(width / base_width, height / base_height)
    return max(1.0, min(VIEWER_UI_SCALE_MAX, size_scale))
