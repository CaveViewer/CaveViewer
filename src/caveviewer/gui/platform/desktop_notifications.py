"""Authorize and execute optional desktop notifications at the platform edge."""

from __future__ import annotations

from typing import TYPE_CHECKING

from caveviewer.core.capabilities import (
    CapabilityStatus,
    DesktopNotificationTarget,
)
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.features import decide_desktop_notification

from .desktop_services import DesktopServiceError, DesktopServices
from .probes.desktop import probe_desktop_notification
from .runtime import DesktopNotificationPreflight

if TYPE_CHECKING:
    from .runtime import PlatformRuntime


_LOG = get_logger("DesktopNotification")


def desktop_notification_preflight(
    desktop_services: DesktopServices,
    *,
    platform_runtime: PlatformRuntime | None = None,
) -> DesktopNotificationPreflight:
    """Return one fresh typed preflight for an optional notification action.

    The injected runtime supplies its on-demand preflight when it owns the
    same desktop service. Compatibility callers use the same side-effect-free
    probe and pure policy directly. Neither path contacts the Portal or sends
    a notification; the desktop service keeps ownership of its Portal/fallback
    behavior once the action is authorized.
    """
    if (
        platform_runtime is not None
        and desktop_services is platform_runtime.desktop_services
    ):
        return platform_runtime.desktop_notification_preflight()

    capability = probe_desktop_notification(desktop_services)
    return DesktopNotificationPreflight(
        capability=capability,
        decision=decide_desktop_notification(capability),
    )


def authorized_desktop_notification_target(
    preflight: DesktopNotificationPreflight,
    desktop_services: DesktopServices,
) -> DesktopNotificationTarget:
    """Return the typed notification route after a last adapter recheck.

    The generic decision route is only diagnostic data. Re-probing the typed
    declaration immediately before native work prevents a stale preflight from
    sending through a changed desktop service. Callers of this low-level helper
    receive a normal desktop-service error; the public notification functions
    below convert it to their required best-effort no-op outcome.
    """
    decision = preflight.decision
    if not decision.allows_execution:
        raise DesktopServiceError(decision.explanation)

    target = preflight.capability.value
    if (
        preflight.capability.status is not CapabilityStatus.AVAILABLE
        or not isinstance(target, DesktopNotificationTarget)
        or decision.route != target.route_key
    ):
        _LOG.warning(
            "Desktop-notification preflight has no executable target: "
            "reason=%s route=%s",
            decision.reason_code,
            decision.route,
        )
        raise DesktopServiceError(
            "Desktop notifications are unavailable in this environment."
        )

    current_capability = probe_desktop_notification(desktop_services)
    current_target = current_capability.value
    if (
        current_capability.status is not CapabilityStatus.AVAILABLE
        or not isinstance(current_target, DesktopNotificationTarget)
    ):
        current_decision = decide_desktop_notification(current_capability)
        _LOG.debug(
            "Desktop notification is no longer executable: reason=%s",
            current_decision.reason_code,
        )
        raise DesktopServiceError(current_decision.explanation)
    if current_target != target:
        _LOG.debug(
            "Desktop-notification route changed before execution: "
            "selected=%s current=%s",
            target.route_key,
            current_target.route_key,
        )
        raise DesktopServiceError(
            "Desktop notification availability changed. Continuing without it."
        )
    return target


def send_authorized_desktop_notification(
    preflight: DesktopNotificationPreflight,
    desktop_services: DesktopServices,
    notification_id: str,
    title: str,
    body: str = "",
    *,
    priority: str = "normal",
) -> bool:
    """Send one notification or safely report that no desktop action ran."""
    try:
        target = authorized_desktop_notification_target(preflight, desktop_services)
        desktop_services.notify(
            notification_id,
            title,
            body,
            priority=priority,
        )
    except Exception as exc:
        _LOG.debug(
            "Desktop notification skipped: reason=%s route=%s error_type=%s",
            preflight.decision.reason_code,
            preflight.decision.route,
            type(exc).__name__,
        )
        return False

    _LOG.debug("Desktop notification sent: route=%s", target.route_key)
    return True


def withdraw_authorized_desktop_notification(
    preflight: DesktopNotificationPreflight,
    desktop_services: DesktopServices,
    notification_id: str,
) -> bool:
    """Withdraw one notification or safely report that no desktop action ran."""
    try:
        target = authorized_desktop_notification_target(preflight, desktop_services)
        withdraw = getattr(desktop_services, "withdraw_notification")
        if not callable(withdraw):
            raise DesktopServiceError(
                "Desktop notification withdrawal is unavailable in this environment."
            )
        withdraw(notification_id)
    except Exception as exc:
        _LOG.debug(
            "Desktop notification withdrawal skipped: reason=%s route=%s "
            "error_type=%s",
            preflight.decision.reason_code,
            preflight.decision.route,
            type(exc).__name__,
        )
        return False

    _LOG.debug("Desktop notification withdrawn: route=%s", target.route_key)
    return True


def send_desktop_notification(
    desktop_services: DesktopServices,
    notification_id: str,
    title: str,
    body: str = "",
    *,
    priority: str = "normal",
    platform_runtime: PlatformRuntime | None = None,
) -> bool:
    """Preflight and send one optional desktop notification best-effort."""
    return send_authorized_desktop_notification(
        desktop_notification_preflight(
            desktop_services,
            platform_runtime=platform_runtime,
        ),
        desktop_services,
        notification_id,
        title,
        body,
        priority=priority,
    )


def withdraw_desktop_notification(
    desktop_services: DesktopServices,
    notification_id: str,
    *,
    platform_runtime: PlatformRuntime | None = None,
) -> bool:
    """Preflight and withdraw one optional desktop notification best-effort."""
    return withdraw_authorized_desktop_notification(
        desktop_notification_preflight(
            desktop_services,
            platform_runtime=platform_runtime,
        ),
        desktop_services,
        notification_id,
    )
