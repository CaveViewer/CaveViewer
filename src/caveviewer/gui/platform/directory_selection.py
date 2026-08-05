"""Authorize and execute typed directory-selection routes at the desktop edge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caveviewer.core.capabilities import (
    CapabilityStatus,
    DirectorySelectionTarget,
)
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.features import FeatureDecision, decide_directory_selection

from .desktop_services import (
    DesktopServiceError,
    DesktopServices,
    DirectorySelection,
)
from .probes.desktop import probe_directory_selection
from .runtime import DirectorySelectionPreflight

if TYPE_CHECKING:
    from .runtime import PlatformRuntime


_LOG = get_logger("DirectorySelection")


def directory_selection_preflight(
    desktop_services: DesktopServices,
    *,
    platform_runtime: PlatformRuntime | None = None,
) -> DirectorySelectionPreflight:
    """Return one fresh typed preflight for a directory-picker action.

    The injected runtime owns the shared service and supplies its on-demand
    preflight. Compatibility callers use the same side-effect-free probe and
    pure policy directly. Neither path creates a Tk root or contacts D-Bus.
    """
    if (
        platform_runtime is not None
        and desktop_services is platform_runtime.desktop_services
    ):
        return platform_runtime.directory_selection_preflight()

    capability = probe_directory_selection(desktop_services)
    return DirectorySelectionPreflight(
        capability=capability,
        decision=decide_directory_selection(capability),
    )


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
    return directory_selection_preflight(
        desktop_services,
        platform_runtime=platform_runtime,
    ).decision


def authorized_directory_selection_target(
    preflight: DirectorySelectionPreflight,
    desktop_services: DesktopServices,
) -> DirectorySelectionTarget:
    """Return the target that may execute, after rechecking adapter agreement.

    A route string in a feature decision is presentation and diagnostic data.
    The typed target is the executable contract. Re-probe the adapter's route
    declaration immediately before creating native chooser resources, so a
    changed service cannot execute a stale policy route.
    """
    decision = preflight.decision
    if not decision.allows_execution:
        raise DesktopServiceError(decision.explanation)

    target = preflight.capability.value
    if (
        preflight.capability.status is not CapabilityStatus.AVAILABLE
        or not isinstance(target, DirectorySelectionTarget)
        or decision.route != target.route_key
    ):
        _LOG.warning(
            "Directory-selection preflight has no executable target: "
            "reason=%s route=%s",
            decision.reason_code,
            decision.route,
        )
        raise DesktopServiceError(
            "Directory selection is unavailable in this environment."
        )

    current_capability = probe_directory_selection(desktop_services)
    current_target = current_capability.value
    if (
        current_capability.status is not CapabilityStatus.AVAILABLE
        or not isinstance(current_target, DirectorySelectionTarget)
    ):
        current_decision = decide_directory_selection(current_capability)
        _LOG.warning(
            "Directory-selection adapter is no longer executable: "
            "reason=%s",
            current_decision.reason_code,
        )
        raise DesktopServiceError(current_decision.explanation)
    if current_target != target:
        _LOG.warning(
            "Directory-selection route changed before execution: "
            "selected=%s current=%s",
            target.route_key,
            current_target.route_key,
        )
        raise DesktopServiceError(
            "Directory selection availability changed. Try again."
        )
    return target


def choose_authorized_directory(
    preflight: DirectorySelectionPreflight,
    desktop_services: DesktopServices,
    *,
    title: str,
    initial_dir: str | None = None,
    parent: Any | None = None,
) -> DirectorySelection | None:
    """Execute one directory chooser only through a validated typed target."""
    target = authorized_directory_selection_target(preflight, desktop_services)
    _LOG.debug("Executing directory selection route: %s", target.route_key)
    options: dict[str, Any] = {"title": title, "parent": parent}
    if initial_dir is not None:
        options["initial_dir"] = initial_dir
    return desktop_services.choose_directory(**options)
