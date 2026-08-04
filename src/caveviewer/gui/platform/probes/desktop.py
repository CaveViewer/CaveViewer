"""On-demand capability probes for desktop directory-selection routes."""

from __future__ import annotations

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    DirectorySelectionRoute,
    DirectorySelectionTarget,
)


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
