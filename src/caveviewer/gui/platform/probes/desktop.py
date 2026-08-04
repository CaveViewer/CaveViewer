"""On-demand capability probes for typed desktop-action routes."""

from __future__ import annotations

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    DesktopNotificationRoute,
    DesktopNotificationTarget,
    DirectorySelectionRoute,
    DirectorySelectionTarget,
    FileSelectionRoute,
    FileSelectionTarget,
    IdleSuspendInhibitionRoute,
    IdleSuspendInhibitionTarget,
)


def probe_desktop_notification(
    desktop_services: object,
) -> CapabilityResult[DesktopNotificationTarget]:
    """Report the safe route available for one optional notification action.

    The probe only inspects the service's side-effect-free declaration. It
    deliberately does not send a notification, contact D-Bus, or construct Tk
    resources. A portable no-op declaration is unavailable rather than an
    executable route; Portal services may still declare a Portal-to-no-op
    composite because their existing action boundary owns that fallback.
    """
    try:
        notifier = getattr(desktop_services, "notify", None)
        target_provider = getattr(
            desktop_services,
            "desktop_notification_target",
            None,
        )
    except Exception:
        return CapabilityResult.unknown(
            reason_code="desktop_notification_capability_probe_failed",
            evidence={"service": "route_declaration_unreadable"},
        )

    if not callable(notifier):
        return CapabilityResult.unavailable(
            reason_code="desktop_notification_service_unavailable",
            source=CapabilitySource.CONSERVATIVE_FALLBACK,
            evidence={"service": "missing_notifier"},
        )

    if not callable(target_provider):
        return CapabilityResult.available(
            DesktopNotificationTarget(DesktopNotificationRoute.INJECTED),
            reason_code="desktop_notification_injected_service_available",
            source=CapabilitySource.CONSERVATIVE_FALLBACK,
            evidence={"primary_route": DesktopNotificationRoute.INJECTED.value},
        )

    try:
        target = target_provider()
    except Exception:
        return CapabilityResult.unknown(
            reason_code="desktop_notification_capability_probe_failed",
            evidence={"service": "route_declaration_failed"},
        )

    if not isinstance(target, DesktopNotificationTarget):
        return CapabilityResult.unknown(
            reason_code="desktop_notification_capability_probe_failed",
            evidence={"service": "invalid_route_declaration"},
        )

    if target.primary_route is DesktopNotificationRoute.NOOP:
        return CapabilityResult.unavailable(
            reason_code="desktop_notification_service_unavailable",
            evidence={
                "primary_route": target.primary_route.value,
                "fallback_route": (
                    target.fallback_route.value if target.fallback_route else None
                ),
            },
        )

    return CapabilityResult.available(
        target,
        reason_code=_desktop_notification_available_reason_code(target),
        evidence={
            "primary_route": target.primary_route.value,
            "fallback_route": (
                target.fallback_route.value if target.fallback_route else None
            ),
        },
    )


def _desktop_notification_available_reason_code(
    target: DesktopNotificationTarget,
) -> str:
    if target.primary_route is DesktopNotificationRoute.PORTAL:
        return "desktop_notification_portal_route_available"
    if target.primary_route is DesktopNotificationRoute.INJECTED:
        return "desktop_notification_injected_service_available"
    return "desktop_notification_service_unavailable"


def probe_idle_suspend_inhibition(
    desktop_services: object,
) -> CapabilityResult[IdleSuspendInhibitionTarget]:
    """Report the safe route available for one scoped idle/suspend inhibitor.

    The probe reads only the service's side-effect-free declaration. It does
    not contact D-Bus, start a Portal inhibitor thread, or acquire an inhibitor
    handle. A portable no-op declaration is unavailable; a Portal-to-no-op
    composite remains executable because its existing action boundary owns the
    fallback.
    """
    try:
        inhibitor = getattr(desktop_services, "inhibit_idle_suspend", None)
        target_provider = getattr(
            desktop_services,
            "idle_suspend_inhibition_target",
            None,
        )
    except Exception:
        return CapabilityResult.unknown(
            reason_code="idle_suspend_inhibition_capability_probe_failed",
            evidence={"service": "route_declaration_unreadable"},
        )

    if not callable(inhibitor):
        return CapabilityResult.unavailable(
            reason_code="idle_suspend_inhibition_service_unavailable",
            source=CapabilitySource.CONSERVATIVE_FALLBACK,
            evidence={"service": "missing_inhibitor"},
        )

    if not callable(target_provider):
        return CapabilityResult.available(
            IdleSuspendInhibitionTarget(IdleSuspendInhibitionRoute.INJECTED),
            reason_code="idle_suspend_inhibition_injected_service_available",
            source=CapabilitySource.CONSERVATIVE_FALLBACK,
            evidence={"primary_route": IdleSuspendInhibitionRoute.INJECTED.value},
        )

    try:
        target = target_provider()
    except Exception:
        return CapabilityResult.unknown(
            reason_code="idle_suspend_inhibition_capability_probe_failed",
            evidence={"service": "route_declaration_failed"},
        )

    if not isinstance(target, IdleSuspendInhibitionTarget):
        return CapabilityResult.unknown(
            reason_code="idle_suspend_inhibition_capability_probe_failed",
            evidence={"service": "invalid_route_declaration"},
        )

    if target.primary_route is IdleSuspendInhibitionRoute.NOOP:
        return CapabilityResult.unavailable(
            reason_code="idle_suspend_inhibition_service_unavailable",
            evidence={
                "primary_route": target.primary_route.value,
                "fallback_route": (
                    target.fallback_route.value if target.fallback_route else None
                ),
            },
        )

    return CapabilityResult.available(
        target,
        reason_code=_idle_suspend_inhibition_available_reason_code(target),
        evidence={
            "primary_route": target.primary_route.value,
            "fallback_route": (
                target.fallback_route.value if target.fallback_route else None
            ),
        },
    )


def _idle_suspend_inhibition_available_reason_code(
    target: IdleSuspendInhibitionTarget,
) -> str:
    if target.primary_route is IdleSuspendInhibitionRoute.PORTAL:
        return "idle_suspend_inhibition_portal_route_available"
    if target.primary_route is IdleSuspendInhibitionRoute.INJECTED:
        return "idle_suspend_inhibition_injected_service_available"
    return "idle_suspend_inhibition_service_unavailable"


def probe_directory_selection(
    desktop_services: object,
) -> CapabilityResult[DirectorySelectionTarget]:
    """Report the safe route available for one directory-picker action.

    The probe deliberately does not contact D-Bus or create a Tk root. Portal
    reachability can change while a GUI is running and is already handled at
    the action boundary by the existing Portal-to-Tk fallback service. This
    fact is therefore refreshed immediately before every picker invocation.
    """
    try:
        chooser = getattr(desktop_services, "choose_directory", None)
        target_provider = getattr(
            desktop_services,
            "directory_selection_target",
            None,
        )
    except Exception:
        return CapabilityResult.unknown(
            reason_code="directory_selection_capability_probe_failed",
            evidence={"service": "route_declaration_unreadable"},
        )

    if not callable(chooser):
        return CapabilityResult.unavailable(
            reason_code="directory_selection_service_unavailable",
            source=CapabilitySource.CONSERVATIVE_FALLBACK,
            evidence={"service": "missing_directory_chooser"},
        )

    if not callable(target_provider):
        return CapabilityResult.available(
            DirectorySelectionTarget(DirectorySelectionRoute.INJECTED),
            reason_code="directory_selection_injected_service_available",
            source=CapabilitySource.CONSERVATIVE_FALLBACK,
            evidence={"primary_route": DirectorySelectionRoute.INJECTED.value},
        )

    try:
        target = target_provider()
    except Exception:
        return CapabilityResult.unknown(
            reason_code="directory_selection_capability_probe_failed",
            evidence={"service": "route_declaration_failed"},
        )

    if not isinstance(target, DirectorySelectionTarget):
        return CapabilityResult.unknown(
            reason_code="directory_selection_capability_probe_failed",
            evidence={"service": "invalid_route_declaration"},
        )

    return CapabilityResult.available(
        target,
        reason_code=_available_reason_code(target),
        evidence={
            "primary_route": target.primary_route.value,
            "fallback_route": (
                target.fallback_route.value if target.fallback_route else None
            ),
        },
    )


def _available_reason_code(target: DirectorySelectionTarget) -> str:
    if target.primary_route is DirectorySelectionRoute.PORTAL:
        return "directory_selection_portal_route_available"
    if target.primary_route is DirectorySelectionRoute.TK:
        return "directory_selection_tk_route_available"
    return "directory_selection_injected_service_available"


def probe_file_selection(
    desktop_services: object,
) -> CapabilityResult[FileSelectionTarget]:
    """Report the safe route available for one file-opening action.

    The probe deliberately does not contact D-Bus or create a Tk root. Portal
    reachability can change while a GUI is running and is already handled at
    the action boundary by the existing Portal-to-Tk fallback service. This
    fact is therefore refreshed immediately before every picker invocation.
    """
    try:
        chooser = getattr(desktop_services, "choose_file", None)
        target_provider = getattr(
            desktop_services,
            "file_selection_target",
            None,
        )
    except Exception:
        return CapabilityResult.unknown(
            reason_code="file_selection_capability_probe_failed",
            evidence={"service": "route_declaration_unreadable"},
        )

    if not callable(chooser):
        return CapabilityResult.unavailable(
            reason_code="file_selection_service_unavailable",
            source=CapabilitySource.CONSERVATIVE_FALLBACK,
            evidence={"service": "missing_file_chooser"},
        )

    if not callable(target_provider):
        return CapabilityResult.available(
            FileSelectionTarget(FileSelectionRoute.INJECTED),
            reason_code="file_selection_injected_service_available",
            source=CapabilitySource.CONSERVATIVE_FALLBACK,
            evidence={"primary_route": FileSelectionRoute.INJECTED.value},
        )

    try:
        target = target_provider()
    except Exception:
        return CapabilityResult.unknown(
            reason_code="file_selection_capability_probe_failed",
            evidence={"service": "route_declaration_failed"},
        )

    if not isinstance(target, FileSelectionTarget):
        return CapabilityResult.unknown(
            reason_code="file_selection_capability_probe_failed",
            evidence={"service": "invalid_route_declaration"},
        )

    return CapabilityResult.available(
        target,
        reason_code=_file_selection_available_reason_code(target),
        evidence={
            "primary_route": target.primary_route.value,
            "fallback_route": (
                target.fallback_route.value if target.fallback_route else None
            ),
        },
    )


def _file_selection_available_reason_code(target: FileSelectionTarget) -> str:
    if target.primary_route is FileSelectionRoute.PORTAL:
        return "file_selection_portal_route_available"
    if target.primary_route is FileSelectionRoute.TK:
        return "file_selection_tk_route_available"
    return "file_selection_injected_service_available"
