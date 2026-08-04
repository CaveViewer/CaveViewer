"""Composition root for immutable platform facts, adapters, and feature gates."""

from __future__ import annotations

import platform
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Mapping

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    DirectorySelectionTarget,
    UpdatePackageRevealRoute,
)
from caveviewer.gui.features import (
    FeatureDecision,
    FeatureGateRegistry,
    FeatureId,
    decide_automatic_update,
    decide_directory_selection,
    decide_update_package_reveal,
    decide_video_recording,
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
from .probes.recording import VideoRecordingTarget, probe_video_recording
from .probes.desktop import probe_directory_selection
from .probes.update_package_reveal import probe_update_package_reveal
from .update_package_reveal import (
    UpdatePackageRevealAdapter,
    create_update_package_reveal_adapter,
)


@dataclass(frozen=True, slots=True)
class PlatformProfile:
    """Static process facts retained for diagnostics and policy composition."""

    platform_name: str
    machine: str
    install_channel: str


@dataclass(frozen=True, slots=True)
class VideoRecordingPreflight:
    """One on-demand recording probe paired with its policy decision.

    The capability and decision are derived from the same probe snapshot, so a
    caller can use the confirmed ffmpeg target without probing a mutable output
    directory twice. The caller must request a fresh preflight before starting
    irreversible work.
    """

    capability: CapabilityResult[VideoRecordingTarget]
    decision: FeatureDecision

    def __post_init__(self) -> None:
        if self.decision.feature is not FeatureId.VIDEO_RECORDING:
            raise ValueError("recording preflight must contain a video-recording decision")


@dataclass(frozen=True, slots=True)
class DirectorySelectionPreflight:
    """One directory-picker route fact paired with its current policy decision.

    The route declaration is refreshed immediately before a picker action. It
    does not contact D-Bus or create Tk resources; Linux portal execution keeps
    its existing safe Tk fallback at the action boundary.
    """

    capability: CapabilityResult[DirectorySelectionTarget]
    decision: FeatureDecision

    def __post_init__(self) -> None:
        if self.decision.feature is not FeatureId.DIRECTORY_SELECTION:
            raise ValueError(
                "directory-selection preflight must contain a "
                "directory-selection decision"
            )


@dataclass(frozen=True, slots=True)
class PlatformRuntime:
    """One process-owned set of adapters, static gates, and lazy capability probes.

    The runtime is composed by ``caveviewer.app`` after command-line overrides
    are applied.  It intentionally does not probe D-Bus, the GPU, or network
    state while being created. Action-specific methods run their probes only
    when the corresponding feature requests them.
    """

    profile: PlatformProfile
    platform_adapter: SplashPlatformAdapter
    desktop_services: DesktopServices
    update_configuration: UpdateConfiguration
    automatic_update_capability: CapabilityResult[UpdateTarget]
    update_package_reveal_adapter: UpdatePackageRevealAdapter
    update_package_reveal_capability: CapabilityResult[UpdatePackageRevealRoute]
    feature_gates: FeatureGateRegistry

    def static_feature_decision(self, feature: FeatureId) -> FeatureDecision:
        """Return a process-stable decision composed into ``feature_gates``.

        Mutable action prerequisites deliberately do not appear here. Use the
        feature's on-demand preflight method instead.
        """
        return self.feature_gates.decision_for(feature)

    @property
    def automatic_update_decision(self) -> FeatureDecision:
        """Return the gate used before checking or downloading an update."""
        return self.static_feature_decision(FeatureId.AUTOMATIC_UPDATE)

    @property
    def update_package_reveal_decision(self) -> FeatureDecision:
        """Return the static gate used before revealing a verified package."""
        return self.static_feature_decision(FeatureId.UPDATE_PACKAGE_REVEAL)

    def video_recording_capability(
        self,
        output_directory: str,
        *,
        ffmpeg_resolver: Callable[[], str | None] | None = None,
    ) -> CapabilityResult[VideoRecordingTarget]:
        """Probe video-recording prerequisites only when recording is requested."""
        return probe_video_recording(
            output_directory,
            ffmpeg_resolver=ffmpeg_resolver,
        )

    def video_recording_decision(
        self,
        output_directory: str,
        *,
        ffmpeg_resolver: Callable[[], str | None] | None = None,
    ) -> FeatureDecision:
        """Return the on-demand gate that controls the ffmpeg recording route."""
        return self.video_recording_preflight(
            output_directory,
            ffmpeg_resolver=ffmpeg_resolver,
        ).decision

    def video_recording_preflight(
        self,
        output_directory: str,
        *,
        ffmpeg_resolver: Callable[[], str | None] | None = None,
    ) -> VideoRecordingPreflight:
        """Evaluate recording from one fresh capability probe and pure policy."""
        capability = self.video_recording_capability(
            output_directory,
            ffmpeg_resolver=ffmpeg_resolver,
        )
        return VideoRecordingPreflight(
            capability=capability,
            decision=decide_video_recording(capability),
        )

    def directory_selection_capability(
        self,
    ) -> CapabilityResult[DirectorySelectionTarget]:
        """Probe the current safe map-directory picker route on demand."""
        return probe_directory_selection(self.desktop_services)

    def directory_selection_preflight(self) -> DirectorySelectionPreflight:
        """Pair one fresh directory-selection route fact with pure policy."""
        capability = self.directory_selection_capability()
        return DirectorySelectionPreflight(
            capability=capability,
            decision=decide_directory_selection(capability),
        )


def create_platform_runtime(
    *,
    platform_adapter: SplashPlatformAdapter | None = None,
    desktop_services: DesktopServices | None = None,
    update_package_reveal_adapter: UpdatePackageRevealAdapter | None = None,
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
    resolved_update_package_reveal_adapter = (
        update_package_reveal_adapter
        or create_update_package_reveal_adapter(
            resolved_platform_adapter,
            platform_name=resolved_platform_name,
        )
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
    try:
        update_package_reveal_capability = probe_update_package_reveal(
            resolved_update_package_reveal_adapter
        )
    except Exception:
        update_package_reveal_capability = CapabilityResult.unknown(
            reason_code="update_package_reveal_capability_probe_failed",
            evidence={"probe": "update_package_reveal"},
        )
    update_package_reveal_decision = decide_update_package_reveal(
        update_package_reveal_capability
    )

    return PlatformRuntime(
        profile=profile,
        platform_adapter=resolved_platform_adapter,
        desktop_services=resolved_desktop_services,
        update_configuration=update_configuration,
        automatic_update_capability=automatic_update_capability,
        update_package_reveal_adapter=resolved_update_package_reveal_adapter,
        update_package_reveal_capability=update_package_reveal_capability,
        feature_gates=FeatureGateRegistry(
            {
                FeatureId.AUTOMATIC_UPDATE: automatic_update_decision,
                FeatureId.UPDATE_PACKAGE_REVEAL: update_package_reveal_decision,
            }
        ),
    )
