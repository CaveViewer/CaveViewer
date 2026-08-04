"""Static capability probe for non-executing update-package reveal routes."""

from __future__ import annotations

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    UpdatePackageRevealRoute,
)


def probe_update_package_reveal(
    reveal_adapter: object,
) -> CapabilityResult[UpdatePackageRevealRoute]:
    """Report the route declared by one focused update-package reveal adapter.

    The declaration is intentionally side-effect free: it does not mount a
    package, launch a file manager, or contact a desktop service. Those actions
    remain behind the feature service after its policy decision is checked.
    """
    try:
        route_provider = getattr(reveal_adapter, "reveal_route", None)
    except Exception:
        return CapabilityResult.unknown(
            reason_code="update_package_reveal_capability_probe_failed",
            evidence={"adapter": "route_declaration_unreadable"},
        )

    if not callable(route_provider):
        return CapabilityResult.unavailable(
            reason_code="update_package_reveal_adapter_unavailable",
            source=CapabilitySource.CONSERVATIVE_FALLBACK,
            evidence={"adapter": "missing_route_declaration"},
        )

    try:
        route = route_provider()
    except Exception:
        return CapabilityResult.unknown(
            reason_code="update_package_reveal_capability_probe_failed",
            evidence={"adapter": "route_declaration_failed"},
        )

    if route is None:
        return CapabilityResult.unavailable(
            reason_code="update_package_reveal_route_unsupported",
            evidence={"route": "unsupported"},
        )
    if not isinstance(route, UpdatePackageRevealRoute):
        return CapabilityResult.unknown(
            reason_code="update_package_reveal_capability_probe_failed",
            evidence={"adapter": "invalid_route_declaration"},
        )

    return CapabilityResult.available(
        route,
        reason_code="update_package_reveal_route_available",
        evidence={"route": route.value},
    )
