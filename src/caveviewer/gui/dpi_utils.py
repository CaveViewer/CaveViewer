"""Tk DPI-scaling setup from typed presentation profile and native action facade.

Small DPI helpers for Tk-based windows/dialogs.

Without process DPI awareness or explicit Tk scaling, high-DPI displays can
make the entire splash screen look blocky or undersized even when the font
itself is fine.
"""

from __future__ import annotations

import os

from caveviewer.gui.platform.factory import get_platform_adapter
from caveviewer.gui.platform.presentation import (
    PresentationProfile,
    get_presentation_profile,
)
from caveviewer.gui.platform.presentation_actions import (
    PresentationActionsAdapter,
    create_presentation_actions_adapter,
)


_DPI_AWARENESS_CONFIGURED = False


def configure_process_dpi_awareness(
    *,
    presentation_actions_adapter: PresentationActionsAdapter | None = None,
) -> None:
    """Best-effort Windows process DPI awareness setup.

    Must run before creating Tk roots/windows. Safe no-op on non-Windows
    platforms or when another subsystem already configured DPI awareness.
    """
    global _DPI_AWARENESS_CONFIGURED
    if _DPI_AWARENESS_CONFIGURED:
        return
    _DPI_AWARENESS_CONFIGURED = True
    adapter = presentation_actions_adapter or create_presentation_actions_adapter(
        get_platform_adapter()
    )
    adapter.configure_process_dpi_awareness()


def apply_tk_scaling(
    root,
    *,
    presentation_profile: PresentationProfile | None = None,
) -> None:
    """Nudge Tk to use the current display DPI for font/layout scaling."""
    profile = presentation_profile or get_presentation_profile()
    if not profile.supports_tk_display_scaling:
        return
    try:
        override = os.getenv("CAVEVIEWER_TK_SCALE", "").strip()
        if override:
            scale = float(override)
        else:
            pixels_per_inch = float(root.winfo_fpixels("1i"))
            scale = pixels_per_inch / 72.0 if pixels_per_inch > 0 else 0.0
        if scale > 0:
            root.tk.call("tk", "scaling", max(0.75, min(4.0, scale)))
    except Exception:
        pass


def tk_display_scale(
    root,
    *,
    presentation_profile: PresentationProfile | None = None,
) -> float:
    """Return display scale relative to 96 DPI for pixel-sized Tk layout values."""
    profile = presentation_profile or get_presentation_profile()
    if not profile.supports_tk_display_scaling:
        return 1.0
    try:
        override = os.getenv("CAVEVIEWER_TK_SCALE", "").strip()
        if override:
            return max(0.75, min(4.0, float(override))) / (96.0 / 72.0)
        pixels_per_inch = float(root.winfo_fpixels("1i"))
        if pixels_per_inch > 0:
            return max(1.0, min(3.0, pixels_per_inch / 96.0))
    except Exception:
        pass
    return 1.0
