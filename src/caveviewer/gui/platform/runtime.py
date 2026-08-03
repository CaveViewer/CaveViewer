"""Composition root for immutable platform facts, adapters, and feature gates."""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from typing import Mapping

from caveviewer.core.capabilities import CapabilityResult, CapabilitySource
from caveviewer.gui.features import (
    FeatureDecision,
    FeatureGateRegistry,
    FeatureId,
    decide_automatic_update,
)

from .base import SplashPlatformAdapter
from .desktop_services import DesktopServices, get_desktop_services
from .factory import get_platform_adapter
from .probes.updates import (
    UpdateConfiguration,
    UpdateTarget,
    build_update_configuration,
    probe_automatic_update,
)


@dataclass(frozen=True, slots=True)
class PlatformProfile:
    """Static process facts retained for diagnostics and policy composition."""

    platform_name: str
    machine: str
    install_channel: str


@dataclass(frozen=True, slots=True)
class PlatformRuntime:
    """One process-owned set of platform adapters and static feature decisions.

    The runtime is composed by ``caveviewer.app`` after command-line overrides
    are applied.  It intentionally does not probe D-Bus, the GPU, or network
    state while being created.
    """

    profile: PlatformProfile
    platform_adapter: SplashPlatformAdapter
    desktop_services: DesktopServices
    update_configuration: UpdateConfiguration
    automatic_update_capability: CapabilityResult[UpdateTarget]
    feature_gates: FeatureGateRegistry

    @property
    def automatic_update_decision(self) -> FeatureDecision:
        """Return the gate used before checking or downloading an update."""
        return self.feature_gates.decision_for(FeatureId.AUTOMATIC_UPDATE)


def create_platform_runtime(
    *,
    platform_adapter: SplashPlatformAdapter | None = None,
    desktop_services: DesktopServices | None = None,
    environment: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    machine: str | None = None,
) -> PlatformRuntime:
    """Compose one runtime with shared desktop actions and static update facts."""
    resolved_platform_name = platform_name or sys.platform
    resolved_desktop_services = desktop_services or get_desktop_services(
        platform_name=resolved_platform_name
    )
    resolved_platform_adapter = platform_adapter or get_platform_adapter(
        desktop_services=resolved_desktop_services,
        platform_name=resolved_platform_name,
    )
    try:
        install_channel = resolved_platform_adapter.install_channel().strip().lower()
    except Exception:
        install_channel = "unknown"
    profile = PlatformProfile(
        platform_name=resolved_platform_name,
        machine=(machine or platform.machine()).strip() or "unknown",
        install_channel=install_channel or "unknown",
    )
    try:
        update_configuration = build_update_configuration(
            resolved_platform_adapter,
            environment=environment,
        )
        automatic_update_capability = probe_automatic_update(
            resolved_platform_adapter,
            update_configuration,
        )
    except Exception:
        # A platform-default bug must not prevent the offline viewer from
        # starting. The update policy receives an explicit fail-closed fact.
        update_configuration = UpdateConfiguration(
            repository="",
            branch="main",
            manifest_channel="stable",
            manifest_url="",
            manifest_signature_url="",
            source=CapabilitySource.CONSERVATIVE_FALLBACK,
        )
        automatic_update_capability = CapabilityResult.unknown(
            reason_code="automatic_update_configuration_probe_failed",
            evidence={"probe": "update_configuration"},
        )
    automatic_update_decision = decide_automatic_update(
        automatic_update_capability
    )

    return PlatformRuntime(
        profile=profile,
        platform_adapter=resolved_platform_adapter,
        desktop_services=resolved_desktop_services,
        update_configuration=update_configuration,
        automatic_update_capability=automatic_update_capability,
        feature_gates=FeatureGateRegistry(
            {FeatureId.AUTOMATIC_UPDATE: automatic_update_decision}
        ),
    )
