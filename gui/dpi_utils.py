"""
gui/dpi_utils.py

Small Windows DPI helpers for Tk-based windows/dialogs.

Without process DPI awareness, Windows can bitmap-scale Tk windows on
high-DPI displays, making the entire splash screen look blocky even when
the font itself is fine.
"""

from __future__ import annotations

import ctypes
import os


_DPI_AWARENESS_CONFIGURED = False


def configure_process_dpi_awareness() -> None:
    """Best-effort Windows process DPI awareness setup.

    Must run before creating Tk roots/windows. Safe no-op on non-Windows
    platforms or when another subsystem already configured DPI awareness.
    """
    global _DPI_AWARENESS_CONFIGURED
    if _DPI_AWARENESS_CONFIGURED or os.name != "nt":
        return
    _DPI_AWARENESS_CONFIGURED = True

    try:
        # Windows 10+: per-monitor DPI awareness v2.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass

    try:
        # Windows 8.1+: per-monitor DPI awareness.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass

    try:
        # Vista fallback: system DPI awareness.
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def apply_tk_scaling(root) -> None:
    """Nudge Tk to use the current display DPI for font/layout scaling."""
    if os.name != "nt":
        return
    try:
        pixels_per_inch = float(root.winfo_fpixels("1i"))
        if pixels_per_inch > 0:
            root.tk.call("tk", "scaling", pixels_per_inch / 72.0)
    except Exception:
        pass
