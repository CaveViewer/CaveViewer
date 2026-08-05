"""Authorize and execute typed file-selection routes at the desktop edge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caveviewer.core.capabilities import (
    CapabilityStatus,
    FileSelectionTarget,
)
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.features import decide_file_selection

from .desktop_services import DesktopServiceError, DesktopServices, FileSelection
from .probes.desktop import probe_file_selection
from .runtime import FileSelectionPreflight

if TYPE_CHECKING:
    from .runtime import PlatformRuntime


_LOG = get_logger("FileSelection")


def file_selection_preflight(
    desktop_services: DesktopServices,
    *,
    platform_runtime: PlatformRuntime | None = None,
) -> FileSelectionPreflight:
    """Return one fresh typed preflight for a file-opening action.

    The injected runtime owns the shared service and supplies its on-demand
    preflight. Compatibility callers use the same side-effect-free probe and
    pure policy directly. Neither path creates a Tk root or contacts D-Bus.
    """
    if (
        platform_runtime is not None
        and desktop_services is platform_runtime.desktop_services
    ):
        return platform_runtime.file_selection_preflight()

    capability = probe_file_selection(desktop_services)
    return FileSelectionPreflight(
        capability=capability,
        decision=decide_file_selection(capability),
    )


def authorized_file_selection_target(
    preflight: FileSelectionPreflight,
    desktop_services: DesktopServices,
) -> FileSelectionTarget:
    """Return the target that may execute, after rechecking adapter agreement.

    A route string in a feature decision is presentation and diagnostic data.
    The typed target is the executable contract. Re-probe the adapter's route
    declaration immediately before the chooser runs, so a changed service
    cannot execute a stale policy route.
    """
    decision = preflight.decision
    if not decision.allows_execution:
        raise DesktopServiceError(decision.explanation)

    target = preflight.capability.value
    if (
        preflight.capability.status is not CapabilityStatus.AVAILABLE
        or not isinstance(target, FileSelectionTarget)
        or decision.route != target.route_key
    ):
        _LOG.warning(
            "File-selection preflight has no executable target: reason=%s route=%s",
            decision.reason_code,
            decision.route,
        )
        raise DesktopServiceError("File selection is unavailable in this environment.")

    current_capability = probe_file_selection(desktop_services)
    current_target = current_capability.value
    if (
        current_capability.status is not CapabilityStatus.AVAILABLE
        or not isinstance(current_target, FileSelectionTarget)
    ):
        current_decision = decide_file_selection(current_capability)
        _LOG.warning(
            "File-selection adapter is no longer executable: reason=%s",
            current_decision.reason_code,
        )
        raise DesktopServiceError(current_decision.explanation)
    if current_target != target:
        _LOG.warning(
            "File-selection route changed before execution: selected=%s current=%s",
            target.route_key,
            current_target.route_key,
        )
        raise DesktopServiceError("File selection availability changed. Try again.")
    return target


def choose_authorized_file(
    preflight: FileSelectionPreflight,
    desktop_services: DesktopServices,
    *,
    title: str,
    initial_dir: str | None = None,
    parent: Any | None = None,
) -> FileSelection | None:
    """Execute one file-opening chooser only through a validated typed target."""
    target = authorized_file_selection_target(preflight, desktop_services)
    _LOG.debug("Executing file selection route: %s", target.route_key)
    options: dict[str, Any] = {"title": title, "parent": parent}
    if initial_dir is not None:
        options["initial_dir"] = initial_dir
    return desktop_services.choose_file(**options)
