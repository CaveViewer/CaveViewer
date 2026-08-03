"""Test pure feature gates without platform or GUI side effects."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import CapabilityResult, CapabilitySource
from caveviewer.gui.features import (
    FeatureDecision,
    FeatureGateRegistry,
    FeatureId,
    FeatureState,
    decide_automatic_update,
    decide_video_recording,
)


@pytest.mark.parametrize(
    ("state", "route", "allows_execution", "is_visible"),
    [
        (FeatureState.ENABLED, "normal", True, True),
        (FeatureState.DEGRADED, "safe_fallback", True, True),
        (FeatureState.DISABLED, None, False, True),
        (FeatureState.HIDDEN, None, False, False),
    ],
)
def test_feature_decision_state_contract(
    state, route, allows_execution, is_visible
):
    decision = FeatureDecision(
        feature=FeatureId.AUTOMATIC_UPDATE,
        state=state,
        reason_code="test_state",
        explanation="A concise user-facing explanation.",
        route=route,
    )

    assert decision.allows_execution is allows_execution
    assert decision.is_visible is is_visible


@pytest.mark.parametrize(
    ("state", "route"),
    [
        (FeatureState.ENABLED, None),
        (FeatureState.DEGRADED, None),
        (FeatureState.DISABLED, "normal"),
        (FeatureState.HIDDEN, "normal"),
    ],
)
def test_feature_decision_rejects_routes_that_conflict_with_its_state(state, route):
    with pytest.raises(ValueError):
        FeatureDecision(
            feature=FeatureId.AUTOMATIC_UPDATE,
            state=state,
            reason_code="test_state",
            explanation="A concise user-facing explanation.",
            route=route,
        )


@pytest.mark.parametrize(
    ("capability", "state", "reason_code", "route"),
    [
        (
            CapabilityResult.available(
                "target",
                reason_code="automatic_update_target_available",
            ),
            FeatureState.ENABLED,
            "automatic_update_available",
            "signed_manifest",
        ),
        (
            CapabilityResult.unavailable(
                reason_code="automatic_update_target_unsupported",
            ),
            FeatureState.DISABLED,
            "automatic_update_target_unsupported",
            None,
        ),
        (
            CapabilityResult.unknown(
                reason_code="automatic_update_target_probe_failed",
                source=CapabilitySource.CONSERVATIVE_FALLBACK,
            ),
            FeatureState.DISABLED,
            "automatic_update_capability_unknown",
            None,
        ),
    ],
)
def test_automatic_update_policy_is_a_pure_capability_table(
    capability, state, reason_code, route
):
    decision = decide_automatic_update(capability)

    assert decision.feature is FeatureId.AUTOMATIC_UPDATE
    assert decision.state is state
    assert decision.reason_code == reason_code
    assert decision.route == route
    assert decision.allows_execution is (state is FeatureState.ENABLED)


@pytest.mark.parametrize(
    ("capability", "state", "reason_code", "route"),
    [
        (
            CapabilityResult.available(
                "target",
                reason_code="video_recording_target_available",
            ),
            FeatureState.ENABLED,
            "video_recording_available",
            "ffmpeg",
        ),
        (
            CapabilityResult.unavailable(
                reason_code="video_recording_encoder_unavailable",
            ),
            FeatureState.DISABLED,
            "video_recording_encoder_unavailable",
            None,
        ),
        (
            CapabilityResult.unavailable(
                reason_code="video_recording_output_directory_unavailable",
            ),
            FeatureState.DISABLED,
            "video_recording_output_directory_unavailable",
            None,
        ),
        (
            CapabilityResult.unknown(
                reason_code="video_recording_encoder_probe_failed",
                source=CapabilitySource.CONSERVATIVE_FALLBACK,
            ),
            FeatureState.DISABLED,
            "video_recording_capability_unknown",
            None,
        ),
    ],
)
def test_video_recording_policy_is_a_pure_capability_table(
    capability, state, reason_code, route
):
    decision = decide_video_recording(capability)

    assert decision.feature is FeatureId.VIDEO_RECORDING
    assert decision.state is state
    assert decision.reason_code == reason_code
    assert decision.route == route
    assert decision.allows_execution is (state is FeatureState.ENABLED)


def test_feature_gate_registry_copies_and_validates_registered_decisions():
    decision = decide_automatic_update(
        CapabilityResult.available(
            "target",
            reason_code="automatic_update_target_available",
        )
    )
    decisions = {FeatureId.AUTOMATIC_UPDATE: decision}

    registry = FeatureGateRegistry(decisions)
    decisions.clear()

    assert registry.decision_for(FeatureId.AUTOMATIC_UPDATE) is decision
    with pytest.raises(TypeError):
        registry.decisions[FeatureId.AUTOMATIC_UPDATE] = decision  # type: ignore[index]
