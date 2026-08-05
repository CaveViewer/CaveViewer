"""Pure feature policies evaluated from immutable capability snapshots."""

from __future__ import annotations

from typing import TypeVar

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilityStatus,
    DesktopNotificationRoute,
    DesktopNotificationTarget,
    DirectorySelectionRoute,
    DirectorySelectionTarget,
    FileSelectionRoute,
    FileSelectionTarget,
    IdleSuspendInhibitionRoute,
    IdleSuspendInhibitionTarget,
    UpdatePackageRevealRoute,
    ViewerLaunchRoute,
    ViewerLaunchTarget,
)
from caveviewer.core.map.source_model import SourceFormat

from .ids import FeatureId
from .model import FeatureDecision, FeatureState


CapabilityValue = TypeVar("CapabilityValue")

_AUTOMATIC_UPDATE_EXPLANATIONS = {
    "automatic_update_target_unsupported": (
        "Automatic updates are unavailable for this installation."
    ),
    "automatic_update_manifest_unconfigured": (
        "Automatic updates are not configured for this installation."
    ),
    "automatic_update_signature_unconfigured": (
        "Automatic updates require a signed update manifest."
    ),
}

_VIDEO_RECORDING_EXPLANATIONS = {
    "video_recording_encoder_unavailable": (
        "Video recording requires ffmpeg. Install it or set CAVEVIEWER_FFMPEG."
    ),
    "video_recording_output_directory_unavailable": (
        "Video recording cannot save to the selected folder."
    ),
}

_MAP_SOURCE_IMPORT_EXPLANATIONS = {
    "map_source_format_unsupported": (
        "This map source format is not supported by this installation."
    ),
}

_DIRECTORY_SELECTION_EXPLANATIONS = {
    "directory_selection_service_unavailable": (
        "Directory selection is unavailable in this environment."
    ),
}

_FILE_SELECTION_EXPLANATIONS = {
    "file_selection_service_unavailable": (
        "File selection is unavailable in this environment."
    ),
}

_DESKTOP_NOTIFICATION_EXPLANATIONS = {
    "desktop_notification_service_unavailable": (
        "Desktop notifications are unavailable in this environment."
    ),
    "desktop_notification_noop_route": (
        "Desktop notifications are unavailable in this environment."
    ),
}

_IDLE_SUSPEND_INHIBITION_EXPLANATIONS = {
    "idle_suspend_inhibition_service_unavailable": (
        "Desktop idle/suspend inhibition is unavailable in this environment."
    ),
    "idle_suspend_inhibition_noop_route": (
        "Desktop idle/suspend inhibition is unavailable in this environment."
    ),
}

_UPDATE_PACKAGE_REVEAL_EXPLANATIONS = {
    "update_package_reveal_adapter_unavailable": (
        "The verified update package cannot be revealed automatically."
    ),
    "update_package_reveal_route_unsupported": (
        "The verified update package cannot be revealed automatically."
    ),
}

_VIEWER_LAUNCH_EXPLANATIONS = {
    "viewer_launch_backend_request_invalid": (
        "The requested viewer window backend is not valid. "
        "Use CAVEVIEWER_WINDOW_SYSTEM=auto, x11, or wayland."
    ),
    "viewer_launch_x11_display_unavailable": (
        "The viewer cannot start because the requested X11 display is unavailable."
    ),
    "viewer_launch_wayland_display_unavailable": (
        "The viewer cannot start because the requested Wayland display is unavailable."
    ),
    "viewer_launch_display_unavailable": (
        "The viewer cannot start because no supported display is available."
    ),
}

_GUIDED_DIVE_PLAYBACK_EXPLANATIONS = {
    "guided_dive_trace_unavailable": (
        "No completed dive plans are available for this map."
    ),
    "guided_dive_trace_not_map_local": (
        "Choose a dive plan from this map's _guided_dives folder."
    ),
    "guided_dive_trace_missing": "The selected dive plan is no longer available.",
    "guided_dive_trace_invalid": "This dive plan file cannot be opened.",
    "guided_dive_source_unavailable": (
        "The source map for this dive plan is unavailable."
    ),
    "guided_dive_source_not_map_local": (
        "This dive plan does not belong to the selected map."
    ),
    "guided_dive_cache_unavailable": (
        "This map needs a current cache before opening a dive plan."
    ),
    "guided_dive_cache_incompatible": (
        "This dive plan does not match the current map cache."
    ),
}

_MAP_LIBRARY_CACHE_REBUILD_EXPLANATIONS = {
    "map_cache_rebuild_no_generated_cache": (
        "No generated cache is available to rebuild for this map."
    ),
    "map_cache_rebuild_precompiled_map": (
        "This entry is a precompiled cache, not a source map that can be rebuilt."
    ),
    "map_cache_rebuild_source_unavailable": (
        "The source map is unavailable, so its cache cannot be rebuilt."
    ),
    "map_cache_rebuild_source_unreadable": (
        "The source map cannot be read, so its cache cannot be rebuilt."
    ),
    "map_cache_rebuild_destination_unsafe": (
        "The generated cache location is not safe to replace."
    ),
    "map_cache_rebuild_already_in_progress": (
        "This map's cache is already being rebuilt."
    ),
}


def decide_automatic_update(
    capability: CapabilityResult[CapabilityValue],
) -> FeatureDecision:
    """Choose the safe automatic-update route without probing the platform.

    A transient offline result is deliberately not a capability failure; this
    policy runs before network work.  An unknown install target fails closed,
    because offering a package for an incompatible installation is unsafe.
    """
    if capability.status is CapabilityStatus.AVAILABLE:
        return FeatureDecision(
            feature=FeatureId.AUTOMATIC_UPDATE,
            state=FeatureState.ENABLED,
            reason_code="automatic_update_available",
            explanation="Automatic updates are available for this installation.",
            route="signed_manifest",
        )

    if capability.status is CapabilityStatus.UNAVAILABLE:
        return FeatureDecision(
            feature=FeatureId.AUTOMATIC_UPDATE,
            state=FeatureState.DISABLED,
            reason_code=capability.reason_code,
            explanation=_AUTOMATIC_UPDATE_EXPLANATIONS.get(
                capability.reason_code,
                "Automatic updates are unavailable for this installation.",
            ),
        )

    return FeatureDecision(
        feature=FeatureId.AUTOMATIC_UPDATE,
        state=FeatureState.DISABLED,
        reason_code="automatic_update_capability_unknown",
        explanation="Automatic update availability could not be determined.",
    )


def decide_video_recording(
    capability: CapabilityResult[CapabilityValue],
) -> FeatureDecision:
    """Choose whether video recording may start from an injected capability fact.

    Recording is only enabled when the encoder and the configured output folder
    were both confirmed on demand. An uncertain preflight fails closed because
    starting capture without a usable destination would discard the user's
    recording.
    """
    if capability.status is CapabilityStatus.AVAILABLE:
        return FeatureDecision(
            feature=FeatureId.VIDEO_RECORDING,
            state=FeatureState.ENABLED,
            reason_code="video_recording_available",
            explanation="Video recording is available.",
            route="ffmpeg",
        )

    if capability.status is CapabilityStatus.UNAVAILABLE:
        return FeatureDecision(
            feature=FeatureId.VIDEO_RECORDING,
            state=FeatureState.DISABLED,
            reason_code=capability.reason_code,
            explanation=_VIDEO_RECORDING_EXPLANATIONS.get(
                capability.reason_code,
                "Video recording is unavailable in this environment.",
            ),
        )

    return FeatureDecision(
        feature=FeatureId.VIDEO_RECORDING,
        state=FeatureState.DISABLED,
        reason_code="video_recording_capability_unknown",
        explanation="Video recording availability could not be determined.",
    )


def decide_guided_dive_playback(
    capability: CapabilityResult[CapabilityValue],
) -> FeatureDecision:
    """Choose whether one map-local Guided Dive may enter playback.

    Trace presence, bounded trace validation, and cache identity are all facts
    about one selected map and may change while the splash window is open.
    This policy is therefore deliberately action-time rather than a static
    ``PlatformRuntime.feature_gates`` entry. A map with no trace is hidden;
    a selected malformed or incompatible trace becomes a non-executable
    feedback outcome.
    """
    if capability.status is CapabilityStatus.AVAILABLE and capability.value is not None:
        return FeatureDecision(
            feature=FeatureId.GUIDED_DIVE_PLAYBACK,
            state=FeatureState.ENABLED,
            reason_code="guided_dive_playback_available",
            explanation="Dive plan playback is available for this map.",
            route="map_local_trace",
        )

    if capability.status is CapabilityStatus.UNAVAILABLE:
        state = (
            FeatureState.HIDDEN
            if capability.reason_code == "guided_dive_trace_unavailable"
            else FeatureState.DISABLED
        )
        return FeatureDecision(
            feature=FeatureId.GUIDED_DIVE_PLAYBACK,
            state=state,
            reason_code=capability.reason_code,
            explanation=_GUIDED_DIVE_PLAYBACK_EXPLANATIONS.get(
                capability.reason_code,
                "Dive plan playback is unavailable for this map.",
            ),
        )

    return FeatureDecision(
        feature=FeatureId.GUIDED_DIVE_PLAYBACK,
        state=FeatureState.DISABLED,
        reason_code="guided_dive_playback_capability_unknown",
        explanation="Dive plan availability could not be determined.",
    )


def decide_map_library_cache_rebuild(
    capability: CapabilityResult[CapabilityValue],
) -> FeatureDecision:
    """Choose the Map Library rebuild affordance from one map-local probe.

    This policy is deliberately separate from ``PlatformRuntime`` because a
    source, cache target, or competing builder may change per row while the
    splash remains open.  Rows with no generated cache hide the action; once a
    cache exists, unsafe or incomplete facts remain visible but fail closed.
    """
    if capability.status is CapabilityStatus.AVAILABLE and capability.value is not None:
        return FeatureDecision(
            feature=FeatureId.MAP_LIBRARY_CACHE_REBUILD,
            state=FeatureState.ENABLED,
            reason_code="map_cache_rebuild_available",
            explanation="Rebuild this map's generated cache with current import settings.",
            route="forced_import",
        )

    if capability.status is CapabilityStatus.UNAVAILABLE:
        state = (
            FeatureState.HIDDEN
            if capability.reason_code == "map_cache_rebuild_no_generated_cache"
            else FeatureState.DISABLED
        )
        return FeatureDecision(
            feature=FeatureId.MAP_LIBRARY_CACHE_REBUILD,
            state=state,
            reason_code=capability.reason_code,
            explanation=_MAP_LIBRARY_CACHE_REBUILD_EXPLANATIONS.get(
                capability.reason_code,
                "This map's generated cache cannot be rebuilt.",
            ),
        )

    return FeatureDecision(
        feature=FeatureId.MAP_LIBRARY_CACHE_REBUILD,
        state=FeatureState.DISABLED,
        reason_code="map_cache_rebuild_capability_unknown",
        explanation="Cache rebuild availability could not be determined.",
    )


def decide_map_source_import(
    capability: CapabilityResult[SourceFormat],
) -> FeatureDecision:
    """Choose whether one selected source format may enter import workflow.

    The selected descriptor is action-specific, so this policy is evaluated
    immediately before the GUI accepts a source import rather than stored in
    the process-stable feature-gate registry.
    """
    if capability.status is CapabilityStatus.AVAILABLE and capability.value is not None:
        source_format = capability.value
        return FeatureDecision(
            feature=FeatureId.MAP_SOURCE_IMPORT,
            state=FeatureState.ENABLED,
            reason_code="map_source_import_available",
            explanation=f"{source_format.display_name} map import is available.",
            route=source_format.id.value,
        )

    if capability.status is CapabilityStatus.UNAVAILABLE:
        return FeatureDecision(
            feature=FeatureId.MAP_SOURCE_IMPORT,
            state=FeatureState.DISABLED,
            reason_code=capability.reason_code,
            explanation=_MAP_SOURCE_IMPORT_EXPLANATIONS.get(
                capability.reason_code,
                "This map source format is not supported by this installation.",
            ),
        )

    return FeatureDecision(
        feature=FeatureId.MAP_SOURCE_IMPORT,
        state=FeatureState.DISABLED,
        reason_code="map_source_import_capability_unknown",
        explanation="Map source format availability could not be determined.",
    )


def decide_directory_selection(
    capability: CapabilityResult[DirectorySelectionTarget],
) -> FeatureDecision:
    """Choose a safe directory-picker route without invoking desktop APIs.

    Portal-backed Linux services own their existing Tk fallback internally, so
    the Portal/Tk composite is the normal executable route. A Tk-only or
    legacy injected service is still safe, but presented as degraded so callers
    can preserve a concise compatibility explanation if needed.
    """
    if capability.status is CapabilityStatus.AVAILABLE and capability.value is not None:
        target = capability.value
        if target.primary_route is DirectorySelectionRoute.PORTAL:
            return FeatureDecision(
                feature=FeatureId.DIRECTORY_SELECTION,
                state=FeatureState.ENABLED,
                reason_code="directory_selection_available",
                explanation="Directory selection is available.",
                route=target.route_key,
            )
        if target.primary_route is DirectorySelectionRoute.TK:
            return FeatureDecision(
                feature=FeatureId.DIRECTORY_SELECTION,
                state=FeatureState.DEGRADED,
                reason_code="directory_selection_tk_fallback",
                explanation="Directory selection will use the compatible desktop picker.",
                route=target.route_key,
            )
        if target.primary_route is DirectorySelectionRoute.INJECTED:
            return FeatureDecision(
                feature=FeatureId.DIRECTORY_SELECTION,
                state=FeatureState.DEGRADED,
                reason_code="directory_selection_injected_service",
                explanation=(
                    "Directory selection is available through this desktop service."
                ),
                route=target.route_key,
            )
        return FeatureDecision(
            feature=FeatureId.DIRECTORY_SELECTION,
            state=FeatureState.DISABLED,
            reason_code="directory_selection_route_unsupported",
            explanation="Directory selection has no supported execution route.",
        )

    if capability.status is CapabilityStatus.UNAVAILABLE:
        return FeatureDecision(
            feature=FeatureId.DIRECTORY_SELECTION,
            state=FeatureState.DISABLED,
            reason_code=capability.reason_code,
            explanation=_DIRECTORY_SELECTION_EXPLANATIONS.get(
                capability.reason_code,
                "Directory selection is unavailable in this environment.",
            ),
        )

    return FeatureDecision(
        feature=FeatureId.DIRECTORY_SELECTION,
        state=FeatureState.DISABLED,
        reason_code="directory_selection_capability_unknown",
        explanation="Directory selection availability could not be determined.",
    )


def decide_file_selection(
    capability: CapabilityResult[FileSelectionTarget],
) -> FeatureDecision:
    """Choose a safe file-opening route without invoking desktop APIs.

    Portal-backed Linux services own their existing Tk fallback internally, so
    the Portal/Tk composite is the normal executable route. A Tk-only or
    legacy injected service is still safe, but presented as degraded so callers
    can preserve a concise compatibility explanation if needed.
    """
    if capability.status is CapabilityStatus.AVAILABLE and capability.value is not None:
        target = capability.value
        if target.primary_route is FileSelectionRoute.PORTAL:
            return FeatureDecision(
                feature=FeatureId.FILE_SELECTION,
                state=FeatureState.ENABLED,
                reason_code="file_selection_available",
                explanation="File selection is available.",
                route=target.route_key,
            )
        if target.primary_route is FileSelectionRoute.TK:
            return FeatureDecision(
                feature=FeatureId.FILE_SELECTION,
                state=FeatureState.DEGRADED,
                reason_code="file_selection_tk_fallback",
                explanation="File selection will use the compatible desktop picker.",
                route=target.route_key,
            )
        if target.primary_route is FileSelectionRoute.INJECTED:
            return FeatureDecision(
                feature=FeatureId.FILE_SELECTION,
                state=FeatureState.DEGRADED,
                reason_code="file_selection_injected_service",
                explanation="File selection is available through this desktop service.",
                route=target.route_key,
            )
        return FeatureDecision(
            feature=FeatureId.FILE_SELECTION,
            state=FeatureState.DISABLED,
            reason_code="file_selection_route_unsupported",
            explanation="File selection has no supported execution route.",
        )

    if capability.status is CapabilityStatus.UNAVAILABLE:
        return FeatureDecision(
            feature=FeatureId.FILE_SELECTION,
            state=FeatureState.DISABLED,
            reason_code=capability.reason_code,
            explanation=_FILE_SELECTION_EXPLANATIONS.get(
                capability.reason_code,
                "File selection is unavailable in this environment.",
            ),
        )

    return FeatureDecision(
        feature=FeatureId.FILE_SELECTION,
        state=FeatureState.DISABLED,
        reason_code="file_selection_capability_unknown",
        explanation="File selection availability could not be determined.",
    )


def decide_desktop_notification(
    capability: CapabilityResult[DesktopNotificationTarget],
) -> FeatureDecision:
    """Choose whether an optional desktop notification route may run.

    Notifications never control the underlying workflow. This policy only
    decides whether the best-effort desktop action may be attempted: an
    indeterminate or unavailable route becomes a logged no-op at the action
    boundary, while the download, import, or other primary work continues.
    """
    if capability.status is CapabilityStatus.AVAILABLE and capability.value is not None:
        target = capability.value
        if target.primary_route is DesktopNotificationRoute.PORTAL:
            return FeatureDecision(
                feature=FeatureId.DESKTOP_NOTIFICATION,
                state=FeatureState.ENABLED,
                reason_code="desktop_notification_available",
                explanation="Desktop notifications are available.",
                route=target.route_key,
            )
        if target.primary_route is DesktopNotificationRoute.INJECTED:
            return FeatureDecision(
                feature=FeatureId.DESKTOP_NOTIFICATION,
                state=FeatureState.DEGRADED,
                reason_code="desktop_notification_injected_service",
                explanation=(
                    "Desktop notifications are available through this desktop service."
                ),
                route=target.route_key,
            )
        if target.primary_route is DesktopNotificationRoute.NOOP:
            return FeatureDecision(
                feature=FeatureId.DESKTOP_NOTIFICATION,
                state=FeatureState.DISABLED,
                reason_code="desktop_notification_noop_route",
                explanation=_DESKTOP_NOTIFICATION_EXPLANATIONS[
                    "desktop_notification_noop_route"
                ],
            )
        return FeatureDecision(
            feature=FeatureId.DESKTOP_NOTIFICATION,
            state=FeatureState.DISABLED,
            reason_code="desktop_notification_route_unsupported",
            explanation="Desktop notifications have no supported execution route.",
        )

    if capability.status is CapabilityStatus.UNAVAILABLE:
        return FeatureDecision(
            feature=FeatureId.DESKTOP_NOTIFICATION,
            state=FeatureState.DISABLED,
            reason_code=capability.reason_code,
            explanation=_DESKTOP_NOTIFICATION_EXPLANATIONS.get(
                capability.reason_code,
                "Desktop notifications are unavailable in this environment.",
            ),
        )

    return FeatureDecision(
        feature=FeatureId.DESKTOP_NOTIFICATION,
        state=FeatureState.DISABLED,
        reason_code="desktop_notification_capability_unknown",
        explanation="Desktop notification availability could not be determined.",
    )


def decide_idle_suspend_inhibition(
    capability: CapabilityResult[IdleSuspendInhibitionTarget],
) -> FeatureDecision:
    """Choose whether one optional scoped inhibitor may be acquired.

    Inhibition only augments a long-running operation. An unavailable or
    uncertain route becomes a diagnostic no-op at the action boundary; it never
    blocks the map import, map-library download, or update download that asked
    for it.
    """
    if capability.status is CapabilityStatus.AVAILABLE and capability.value is not None:
        target = capability.value
        if target.primary_route is IdleSuspendInhibitionRoute.PORTAL:
            return FeatureDecision(
                feature=FeatureId.IDLE_SUSPEND_INHIBITION,
                state=FeatureState.ENABLED,
                reason_code="idle_suspend_inhibition_available",
                explanation="Desktop idle/suspend inhibition is available.",
                route=target.route_key,
            )
        if target.primary_route is IdleSuspendInhibitionRoute.INJECTED:
            return FeatureDecision(
                feature=FeatureId.IDLE_SUSPEND_INHIBITION,
                state=FeatureState.DEGRADED,
                reason_code="idle_suspend_inhibition_injected_service",
                explanation=(
                    "Desktop idle/suspend inhibition is available through this "
                    "desktop service."
                ),
                route=target.route_key,
            )
        if target.primary_route is IdleSuspendInhibitionRoute.NOOP:
            return FeatureDecision(
                feature=FeatureId.IDLE_SUSPEND_INHIBITION,
                state=FeatureState.DISABLED,
                reason_code="idle_suspend_inhibition_noop_route",
                explanation=_IDLE_SUSPEND_INHIBITION_EXPLANATIONS[
                    "idle_suspend_inhibition_noop_route"
                ],
            )
        return FeatureDecision(
            feature=FeatureId.IDLE_SUSPEND_INHIBITION,
            state=FeatureState.DISABLED,
            reason_code="idle_suspend_inhibition_route_unsupported",
            explanation=(
                "Desktop idle/suspend inhibition has no supported execution route."
            ),
        )

    if capability.status is CapabilityStatus.UNAVAILABLE:
        return FeatureDecision(
            feature=FeatureId.IDLE_SUSPEND_INHIBITION,
            state=FeatureState.DISABLED,
            reason_code=capability.reason_code,
            explanation=_IDLE_SUSPEND_INHIBITION_EXPLANATIONS.get(
                capability.reason_code,
                "Desktop idle/suspend inhibition is unavailable in this environment.",
            ),
        )

    return FeatureDecision(
        feature=FeatureId.IDLE_SUSPEND_INHIBITION,
        state=FeatureState.DISABLED,
        reason_code="idle_suspend_inhibition_capability_unknown",
        explanation=(
            "Desktop idle/suspend inhibition availability could not be determined."
        ),
    )


def decide_viewer_launch(
    capability: CapabilityResult[ViewerLaunchTarget],
) -> FeatureDecision:
    """Select a viewer-window route from a fresh, typed platform fact.

    A viewer cannot safely continue without a window, so known unavailable or
    indeterminate launch facts fail closed before map import or render setup.
    The adapter still owns native GLFW/ModernGL creation and may report a
    bounded initialization failure after this policy authorizes the target.
    """
    if capability.status is CapabilityStatus.AVAILABLE and isinstance(
        capability.value,
        ViewerLaunchTarget,
    ):
        target = capability.value
        if target.route is ViewerLaunchRoute.NATIVE_MODERNGL:
            return FeatureDecision(
                feature=FeatureId.VIEWER_LAUNCH,
                state=FeatureState.ENABLED,
                reason_code="viewer_launch_native_route_available",
                explanation="The viewer window is available.",
                route=target.route_key,
            )
        if target.route is ViewerLaunchRoute.GLFW_MODERNGL:
            return FeatureDecision(
                feature=FeatureId.VIEWER_LAUNCH,
                state=FeatureState.ENABLED,
                reason_code="viewer_launch_glfw_route_available",
                explanation="The viewer window is available.",
                route=target.route_key,
            )
        return FeatureDecision(
            feature=FeatureId.VIEWER_LAUNCH,
            state=FeatureState.DISABLED,
            reason_code="viewer_launch_route_unsupported",
            explanation="The viewer window has no supported execution route.",
        )

    if capability.status is CapabilityStatus.UNAVAILABLE:
        return FeatureDecision(
            feature=FeatureId.VIEWER_LAUNCH,
            state=FeatureState.DISABLED,
            reason_code=capability.reason_code,
            explanation=_VIEWER_LAUNCH_EXPLANATIONS.get(
                capability.reason_code,
                "The viewer window is unavailable in this environment.",
            ),
        )

    return FeatureDecision(
        feature=FeatureId.VIEWER_LAUNCH,
        state=FeatureState.DISABLED,
        reason_code="viewer_launch_capability_unknown",
        explanation="Viewer-window availability could not be determined.",
    )


def decide_update_package_reveal(
    capability: CapabilityResult[UpdatePackageRevealRoute],
) -> FeatureDecision:
    """Choose whether a verified update package may be exposed to the user.

    Reveal is intentionally non-executing. Its route is process-stable, so the
    runtime composes this decision once; the action still checks the decision
    before it invokes a file-manager or package-specific adapter.
    """
    if capability.status is CapabilityStatus.AVAILABLE and capability.value is not None:
        route = capability.value
        if route is UpdatePackageRevealRoute.LEGACY_ADAPTER:
            return FeatureDecision(
                feature=FeatureId.UPDATE_PACKAGE_REVEAL,
                state=FeatureState.DEGRADED,
                reason_code="update_package_reveal_legacy_adapter",
                explanation=(
                    "Verified update package reveal is available through the "
                    "compatibility adapter."
                ),
                route=route.value,
            )
        return FeatureDecision(
            feature=FeatureId.UPDATE_PACKAGE_REVEAL,
            state=FeatureState.ENABLED,
            reason_code="update_package_reveal_available",
            explanation="Verified update package reveal is available.",
            route=route.value,
        )

    if capability.status is CapabilityStatus.UNAVAILABLE:
        return FeatureDecision(
            feature=FeatureId.UPDATE_PACKAGE_REVEAL,
            state=FeatureState.DISABLED,
            reason_code=capability.reason_code,
            explanation=_UPDATE_PACKAGE_REVEAL_EXPLANATIONS.get(
                capability.reason_code,
                "The verified update package cannot be revealed automatically.",
            ),
        )

    return FeatureDecision(
        feature=FeatureId.UPDATE_PACKAGE_REVEAL,
        state=FeatureState.DISABLED,
        reason_code="update_package_reveal_capability_unknown",
        explanation="Verified update package reveal availability could not be determined.",
    )
