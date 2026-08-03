"""Pure feature policies evaluated from immutable capability snapshots."""

from __future__ import annotations

from typing import TypeVar

from caveviewer.core.capabilities import CapabilityResult, CapabilityStatus

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
