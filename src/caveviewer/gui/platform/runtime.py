"""Composition root for immutable platform facts, adapters, and feature gates."""

from __future__ import annotations

import platform
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Mapping

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilityStatus,
    CapabilitySource,
    DesktopNotificationTarget,
    DirectorySelectionTarget,
    FileSelectionTarget,
    IdleSuspendInhibitionTarget,
    UpdatePackageRevealRoute,
    ViewerLaunchTarget,
)
from caveviewer.gui.features import (
    FeatureDecision,
    FeatureGateRegistry,
    FeatureId,
    decide_automatic_update,
    decide_desktop_notification,
    decide_directory_selection,
    decide_file_selection,
    decide_idle_suspend_inhibition,
    decide_update_package_reveal,
    decide_video_recording,
    decide_viewer_launch,
)
from caveviewer.gui.features.preflight import validate_route_preflight

from .base import SplashPlatformAdapter
from .desktop_services import DesktopServices, get_desktop_services
from .factory import get_platform_adapter
from .presentation import PresentationProfile, select_presentation_profile
from .presentation_actions import (
    PresentationActionsAdapter,
    create_presentation_actions_adapter,
)
from .probes.updates import (
    UpdateConfiguration,
    UpdateProfile,
    UpdateTarget,
    build_update_configuration,
    probe_automatic_update,
    select_update_profile,
)
from .probes.recording import VideoRecordingTarget, probe_video_recording
from .probes.desktop import (
    probe_desktop_notification,
    probe_directory_selection,
    probe_file_selection,
    probe_idle_suspend_inhibition,
)
from .probes.update_package_reveal import probe_update_package_reveal
from .probes.windowing import probe_viewer_launch
from .update_package_reveal import (
    UpdatePackageRevealAdapter,
    create_update_package_reveal_adapter,
)
from .update_package_storage import (
    UpdatePackageStorageAdapter,
    create_update_package_storage_adapter,
)
from .saved_artifact_reveal import (
    SavedArtifactRevealAdapter,
    create_saved_artifact_reveal_adapter,
)
from .recording_process import (
    RecordingProcessAdapter,
    create_recording_process_adapter,
)
from .tls_trust import TlsTrustAdapter, create_tls_trust_adapter
from .window_backend import WindowBackendAdapter, create_window_backend_adapter


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
        validate_route_preflight(
            capability=self.capability,
            decision=self.decision,
            expected_feature=FeatureId.VIDEO_RECORDING,
            target_type=VideoRecordingTarget,
            route_for_target=lambda target: target.route_key,
            feature_label="recording",
            target_label="recording target",
            decision_label="video-recording",
        )


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
        validate_route_preflight(
            capability=self.capability,
            decision=self.decision,
            expected_feature=FeatureId.DIRECTORY_SELECTION,
            target_type=DirectorySelectionTarget,
            route_for_target=lambda target: target.route_key,
            feature_label="directory-selection",
        )


@dataclass(frozen=True, slots=True)
class FileSelectionPreflight:
    """One file-opening route fact paired with its current policy decision.

    The route declaration is refreshed immediately before a picker action. It
    does not contact D-Bus or create Tk resources; Linux portal execution keeps
    its existing safe Tk fallback at the action boundary.
    """

    capability: CapabilityResult[FileSelectionTarget]
    decision: FeatureDecision

    def __post_init__(self) -> None:
        validate_route_preflight(
            capability=self.capability,
            decision=self.decision,
            expected_feature=FeatureId.FILE_SELECTION,
            target_type=FileSelectionTarget,
            route_for_target=lambda target: target.route_key,
            feature_label="file-selection",
        )


@dataclass(frozen=True, slots=True)
class DesktopNotificationPreflight:
    """One optional notification route fact paired with its current policy.

    Notification availability may change after startup, so callers request a
    fresh preflight for each best-effort action. A disabled or unknown result
    authorizes no native call, but it never changes the primary workflow's
    state or outcome.
    """

    capability: CapabilityResult[DesktopNotificationTarget]
    decision: FeatureDecision

    def __post_init__(self) -> None:
        validate_route_preflight(
            capability=self.capability,
            decision=self.decision,
            expected_feature=FeatureId.DESKTOP_NOTIFICATION,
            target_type=DesktopNotificationTarget,
            route_for_target=lambda target: target.route_key,
            feature_label="desktop-notification",
        )


@dataclass(frozen=True, slots=True)
class IdleSuspendInhibitionPreflight:
    """One optional inhibitor route fact paired with its current policy.

    Inhibition is scoped to a long-running action and desktop availability can
    change during the process lifetime. Callers request this fresh preflight
    immediately before acquiring a handle. Releasing a successfully acquired
    handle remains ordinary cleanup and is not re-gated.
    """

    capability: CapabilityResult[IdleSuspendInhibitionTarget]
    decision: FeatureDecision

    def __post_init__(self) -> None:
        validate_route_preflight(
            capability=self.capability,
            decision=self.decision,
            expected_feature=FeatureId.IDLE_SUSPEND_INHIBITION,
            target_type=IdleSuspendInhibitionTarget,
            route_for_target=lambda target: target.route_key,
            feature_label="idle-suspend-inhibition",
        )


@dataclass(frozen=True, slots=True)
class ViewerLaunchPreflight:
    """One fresh viewer-window route fact paired with its policy decision.

    The fact is deliberately gathered only when a viewer session is about to
    start: display endpoints and requested backend overrides can change while
    the splash UI is open. It does not initialize GLFW or create a rendering
    context; the platform launch boundary rechecks the declaration immediately
    before native execution.
    """

    capability: CapabilityResult[ViewerLaunchTarget]
    decision: FeatureDecision

    def __post_init__(self) -> None:
        validate_route_preflight(
            capability=self.capability,
            decision=self.decision,
            expected_feature=FeatureId.VIEWER_LAUNCH,
            target_type=ViewerLaunchTarget,
            route_for_target=lambda target: target.route_key,
            feature_label="viewer-launch",
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
    presentation_profile: PresentationProfile
    presentation_actions_adapter: PresentationActionsAdapter
    platform_adapter: SplashPlatformAdapter
    desktop_services: DesktopServices
    update_profile: UpdateProfile
    update_configuration: UpdateConfiguration
    automatic_update_capability: CapabilityResult[UpdateTarget]
    update_package_reveal_adapter: UpdatePackageRevealAdapter
    update_package_storage_adapter: UpdatePackageStorageAdapter
    saved_artifact_reveal_adapter: SavedArtifactRevealAdapter
    recording_process_adapter: RecordingProcessAdapter
    tls_trust_adapter: TlsTrustAdapter
    window_backend_adapter: WindowBackendAdapter
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
    def automatic_update_target(self) -> UpdateTarget | None:
        """Return the configured network target when its static gate is usable."""
        if self.automatic_update_capability.status is not CapabilityStatus.AVAILABLE:
            return None
        return self.automatic_update_capability.value

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
        """Probe the current safe directory-picker route on demand."""
        return probe_directory_selection(self.desktop_services)

    def directory_selection_preflight(self) -> DirectorySelectionPreflight:
        """Pair one fresh directory-selection route fact with pure policy."""
        capability = self.directory_selection_capability()
        return DirectorySelectionPreflight(
            capability=capability,
            decision=decide_directory_selection(capability),
        )

    def file_selection_capability(
        self,
    ) -> CapabilityResult[FileSelectionTarget]:
        """Probe the current safe file-opening route on demand."""
        return probe_file_selection(self.desktop_services)

    def file_selection_preflight(self) -> FileSelectionPreflight:
        """Pair one fresh file-opening route fact with pure policy."""
        capability = self.file_selection_capability()
        return FileSelectionPreflight(
            capability=capability,
            decision=decide_file_selection(capability),
        )

    def desktop_notification_capability(
        self,
    ) -> CapabilityResult[DesktopNotificationTarget]:
        """Probe the current optional desktop-notification route on demand."""
        return probe_desktop_notification(self.desktop_services)

    def desktop_notification_preflight(self) -> DesktopNotificationPreflight:
        """Pair one fresh notification route fact with its pure policy."""
        capability = self.desktop_notification_capability()
        return DesktopNotificationPreflight(
            capability=capability,
            decision=decide_desktop_notification(capability),
        )

    def idle_suspend_inhibition_capability(
        self,
    ) -> CapabilityResult[IdleSuspendInhibitionTarget]:
        """Probe the current scoped desktop-inhibition route on demand."""
        return probe_idle_suspend_inhibition(self.desktop_services)

    def idle_suspend_inhibition_preflight(
        self,
    ) -> IdleSuspendInhibitionPreflight:
        """Pair one fresh inhibitor route fact with its pure policy."""
        capability = self.idle_suspend_inhibition_capability()
        return IdleSuspendInhibitionPreflight(
            capability=capability,
            decision=decide_idle_suspend_inhibition(capability),
        )

    def viewer_launch_capability(self) -> CapabilityResult[ViewerLaunchTarget]:
        """Probe the current display/backend route only when opening a viewer."""
        return probe_viewer_launch(platform_name=self.profile.platform_name)

    def viewer_launch_preflight(self) -> ViewerLaunchPreflight:
        """Pair one fresh viewer-launch route fact with its pure policy."""
        capability = self.viewer_launch_capability()
        return ViewerLaunchPreflight(
            capability=capability,
            decision=decide_viewer_launch(capability),
        )


def create_platform_runtime(
    *,
    platform_adapter: SplashPlatformAdapter | None = None,
    desktop_services: DesktopServices | None = None,
    update_profile: UpdateProfile | None = None,
    presentation_profile: PresentationProfile | None = None,
    presentation_actions_adapter: PresentationActionsAdapter | None = None,
    update_package_reveal_adapter: UpdatePackageRevealAdapter | None = None,
    update_package_storage_adapter: UpdatePackageStorageAdapter | None = None,
    saved_artifact_reveal_adapter: SavedArtifactRevealAdapter | None = None,
    recording_process_adapter: RecordingProcessAdapter | None = None,
    tls_trust_adapter: TlsTrustAdapter | None = None,
    window_backend_adapter: WindowBackendAdapter | None = None,
    environment: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    machine: str | None = None,
) -> PlatformRuntime:
    """Compose one runtime with shared platform actions and static update facts."""
    resolved_platform_name = platform_name or sys.platform
    resolved_machine = (machine or platform.machine()).strip() or "unknown"
    resolved_update_profile = update_profile or select_update_profile(
        platform_name=resolved_platform_name,
        machine=resolved_machine,
    )
    resolved_presentation_profile = (
        presentation_profile
        or select_presentation_profile(platform_name=resolved_platform_name)
    )
    resolved_desktop_services = desktop_services or get_desktop_services(
        platform_name=resolved_platform_name
    )
    resolved_platform_adapter = platform_adapter or get_platform_adapter(
        desktop_services=resolved_desktop_services,
        platform_name=resolved_platform_name,
    )
    resolved_presentation_actions_adapter = (
        presentation_actions_adapter
        or create_presentation_actions_adapter(resolved_platform_adapter)
    )
    profile = PlatformProfile(
        platform_name=resolved_platform_name,
        machine=resolved_machine,
        install_channel=resolved_update_profile.install_channel,
    )
    resolved_update_package_reveal_adapter = (
        update_package_reveal_adapter
        or create_update_package_reveal_adapter(
            resolved_platform_adapter,
            platform_name=resolved_platform_name,
        )
    )
    resolved_update_package_storage_adapter = (
        update_package_storage_adapter
        or create_update_package_storage_adapter(platform_name=resolved_platform_name)
    )
    resolved_saved_artifact_reveal_adapter = (
        saved_artifact_reveal_adapter
        or create_saved_artifact_reveal_adapter(resolved_platform_adapter)
    )
    resolved_recording_process_adapter = (
        recording_process_adapter
        or create_recording_process_adapter(resolved_platform_adapter)
    )
    resolved_tls_trust_adapter = (
        tls_trust_adapter or create_tls_trust_adapter(resolved_platform_adapter)
    )
    resolved_window_backend_adapter = (
        window_backend_adapter or create_window_backend_adapter()
    )
    try:
        update_configuration = build_update_configuration(
            resolved_update_profile,
            environment=environment,
        )
        automatic_update_capability = probe_automatic_update(
            resolved_update_profile,
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
        presentation_profile=resolved_presentation_profile,
        presentation_actions_adapter=resolved_presentation_actions_adapter,
        platform_adapter=resolved_platform_adapter,
        desktop_services=resolved_desktop_services,
        update_profile=resolved_update_profile,
        update_configuration=update_configuration,
        automatic_update_capability=automatic_update_capability,
        update_package_reveal_adapter=resolved_update_package_reveal_adapter,
        update_package_storage_adapter=resolved_update_package_storage_adapter,
        saved_artifact_reveal_adapter=resolved_saved_artifact_reveal_adapter,
        recording_process_adapter=resolved_recording_process_adapter,
        tls_trust_adapter=resolved_tls_trust_adapter,
        window_backend_adapter=resolved_window_backend_adapter,
        update_package_reveal_capability=update_package_reveal_capability,
        feature_gates=FeatureGateRegistry(
            {
                FeatureId.AUTOMATIC_UPDATE: automatic_update_decision,
                FeatureId.UPDATE_PACKAGE_REVEAL: update_package_reveal_decision,
            }
        ),
    )
