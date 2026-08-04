"""Shared action-time policy boundary for GUI directory chooser actions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from caveviewer.gui.features import FeatureDecision, decide_directory_selection

from .desktop_services import DesktopServices
from .probes.desktop import probe_directory_selection

if TYPE_CHECKING:
    from .runtime import PlatformRuntime


def directory_selection_decision(
    desktop_services: DesktopServices,
    *,
    platform_runtime: PlatformRuntime | None = None,
) -> FeatureDecision:
    """Return a fresh authorization decision for one directory-picker action.

    An injected runtime owns the shared desktop service and supplies its fresh
    on-demand preflight. Compatibility callers that provide another service
    use the same side-effect-free probe and pure policy directly. Neither path
    creates a Tk root or contacts D-Bus; the selected desktop service performs
    its existing Portal-to-Tk fallback only after this decision authorizes the
    chooser action.
    """
    if (
        platform_runtime is not None
        and desktop_services is platform_runtime.desktop_services
    ):
        return platform_runtime.directory_selection_preflight().decision
    return decide_directory_selection(probe_directory_selection(desktop_services))
