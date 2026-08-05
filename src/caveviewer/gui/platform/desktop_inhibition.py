"""Authorize and acquire optional desktop idle/suspend inhibition at the edge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caveviewer.core.capabilities import (
    CapabilityStatus,
    IdleSuspendInhibitionTarget,
)
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.features import decide_idle_suspend_inhibition

from .desktop_services import DesktopInhibitor, DesktopServiceError, DesktopServices
from .probes.desktop import probe_idle_suspend_inhibition
from .runtime import IdleSuspendInhibitionPreflight

if TYPE_CHECKING:
    from .runtime import PlatformRuntime


_LOG = get_logger("DesktopInhibition")


def idle_suspend_inhibition_preflight(
    desktop_services: DesktopServices,
    *,
    platform_runtime: PlatformRuntime | None = None,
) -> IdleSuspendInhibitionPreflight:
    """Return one fresh typed preflight for an optional inhibitor acquisition.

    The injected runtime supplies its on-demand preflight when it owns the
    same desktop service. Compatibility callers use the same side-effect-free
    probe and pure policy directly. Neither path opens D-Bus or starts an
    inhibitor worker; the desktop service owns its Portal/fallback behavior
    only after acquisition has been authorized.
    """
    if (
        platform_runtime is not None
        and desktop_services is platform_runtime.desktop_services
    ):
        runtime_preflight = getattr(
            platform_runtime,
            "idle_suspend_inhibition_preflight",
            None,
        )
        if callable(runtime_preflight):
            return runtime_preflight()

    capability = probe_idle_suspend_inhibition(desktop_services)
    return IdleSuspendInhibitionPreflight(
        capability=capability,
        decision=decide_idle_suspend_inhibition(capability),
    )


def authorized_idle_suspend_inhibition_target(
    preflight: IdleSuspendInhibitionPreflight,
    desktop_services: DesktopServices,
) -> IdleSuspendInhibitionTarget:
    """Return the typed route after a final declaration recheck.

    A generic decision route is presentation and diagnostic data, not an
    executable authority. Re-probing immediately before handle acquisition
    prevents a stale preflight from starting a Portal request through a changed
    service. The public acquire helper converts failures to its required
    best-effort no-op; callers of this lower-level helper receive a normal
    desktop-service error for direct contract tests.
    """
    decision = preflight.decision
    if not decision.allows_execution:
        raise DesktopServiceError(decision.explanation)

    target = preflight.capability.value
    if (
        preflight.capability.status is not CapabilityStatus.AVAILABLE
        or not isinstance(target, IdleSuspendInhibitionTarget)
        or decision.route != target.route_key
    ):
        _LOG.warning(
            "Idle-suspend-inhibition preflight has no executable target: "
            "reason=%s route=%s",
            decision.reason_code,
            decision.route,
        )
        raise DesktopServiceError(
            "Desktop idle/suspend inhibition is unavailable in this environment."
        )

    current_capability = probe_idle_suspend_inhibition(desktop_services)
    current_target = current_capability.value
    if (
        current_capability.status is not CapabilityStatus.AVAILABLE
        or not isinstance(current_target, IdleSuspendInhibitionTarget)
    ):
        current_decision = decide_idle_suspend_inhibition(current_capability)
        _LOG.debug(
            "Desktop idle/suspend inhibition is no longer executable: reason=%s",
            current_decision.reason_code,
        )
        raise DesktopServiceError(current_decision.explanation)
    if current_target != target:
        _LOG.debug(
            "Idle-suspend-inhibition route changed before acquisition: "
            "selected=%s current=%s",
            target.route_key,
            current_target.route_key,
        )
        raise DesktopServiceError(
            "Desktop idle/suspend inhibition availability changed. Continuing without it."
        )
    return target


def acquire_authorized_idle_suspend_inhibitor(
    preflight: IdleSuspendInhibitionPreflight,
    desktop_services: DesktopServices,
    reason: str,
    *,
    parent: Any | None = None,
) -> DesktopInhibitor | None:
    """Acquire one scoped inhibitor or safely report that no action ran."""
    try:
        target = authorized_idle_suspend_inhibition_target(
            preflight,
            desktop_services,
        )
        if parent is None:
            # Older injected services accepted only the required reason. Keep
            # that compatibility route viable without weakening the typed
            # target/recheck that authorizes it.
            inhibitor = desktop_services.inhibit_idle_suspend(reason)
        else:
            inhibitor = desktop_services.inhibit_idle_suspend(reason, parent=parent)
    except Exception as exc:
        _LOG.debug(
            "Desktop idle/suspend inhibition skipped: reason=%s route=%s "
            "error_type=%s",
            preflight.decision.reason_code,
            preflight.decision.route,
            type(exc).__name__,
        )
        return None

    if inhibitor is None:
        _LOG.debug(
            "Desktop idle/suspend inhibition acquired no handle: route=%s",
            target.route_key,
        )
        return None
    _LOG.debug("Desktop idle/suspend inhibitor acquired: route=%s", target.route_key)
    return inhibitor


def acquire_idle_suspend_inhibitor(
    desktop_services: DesktopServices,
    reason: str,
    *,
    parent: Any | None = None,
    platform_runtime: PlatformRuntime | None = None,
) -> DesktopInhibitor | None:
    """Preflight and acquire one optional desktop inhibitor best-effort."""
    return acquire_authorized_idle_suspend_inhibitor(
        idle_suspend_inhibition_preflight(
            desktop_services,
            platform_runtime=platform_runtime,
        ),
        desktop_services,
        reason,
        parent=parent,
    )


def release_desktop_inhibitor(inhibitor: DesktopInhibitor | None) -> None:
    """Release a previously acquired inhibitor without another capability check."""
    if inhibitor is None:
        return
    try:
        inhibitor.close()
    except Exception as exc:
        _LOG.debug(
            "Desktop idle/suspend inhibitor release failed: error_type=%s",
            type(exc).__name__,
        )
