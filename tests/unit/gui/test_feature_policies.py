"""Test pure feature gates without platform or GUI side effects."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
    DirectorySelectionRoute,
    DirectorySelectionTarget,
    UpdatePackageRevealRoute,
)
from caveviewer.core.map import source_model
from caveviewer.gui.features import (
    FeatureDecision,
    FeatureGateRegistry,
    FeatureId,
    FeatureState,
    decide_automatic_update,
    decide_directory_selection,
    decide_map_source_import,
    decide_update_package_reveal,
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


@pytest.mark.parametrize(
    ("capability", "state", "reason_code", "route"),
    [
        (
            CapabilityResult.available(
                source_model.OBJ_SOURCE_FORMAT,
                reason_code="map_source_format_available",
            ),
            FeatureState.ENABLED,
            "map_source_import_available",
            "obj",
        ),
        (
            CapabilityResult.available(
                source_model.GLB_SOURCE_FORMAT,
                reason_code="map_source_format_available",
            ),
            FeatureState.ENABLED,
            "map_source_import_available",
            "glb",
        ),
        (
            CapabilityResult.unavailable(
                reason_code="map_source_format_unsupported",
            ),
            FeatureState.DISABLED,
            "map_source_format_unsupported",
            None,
        ),
        (
            CapabilityResult.unknown(
                reason_code="map_source_format_probe_failed",
                source=CapabilitySource.CONSERVATIVE_FALLBACK,
            ),
            FeatureState.DISABLED,
            "map_source_import_capability_unknown",
            None,
        ),
    ],
)
def test_map_source_import_policy_is_a_pure_capability_table(
    capability, state, reason_code, route
):
    decision = decide_map_source_import(capability)

    assert decision.feature is FeatureId.MAP_SOURCE_IMPORT
    assert decision.state is state
    assert decision.reason_code == reason_code
    assert decision.route == route
    assert decision.allows_execution is (state is FeatureState.ENABLED)


@pytest.mark.parametrize(
    ("capability", "state", "reason_code", "route"),
    [
        (
            CapabilityResult.available(
                DirectorySelectionTarget(
                    primary_route=DirectorySelectionRoute.PORTAL,
                    fallback_route=DirectorySelectionRoute.TK,
                ),
                reason_code="directory_selection_portal_route_available",
            ),
            FeatureState.ENABLED,
            "directory_selection_available",
            "portal_then_tk",
        ),
        (
            CapabilityResult.available(
                DirectorySelectionTarget(DirectorySelectionRoute.TK),
                reason_code="directory_selection_tk_route_available",
            ),
            FeatureState.DEGRADED,
            "directory_selection_tk_fallback",
            "tk",
        ),
        (
            CapabilityResult.available(
                DirectorySelectionTarget(DirectorySelectionRoute.INJECTED),
                reason_code="directory_selection_injected_service_available",
            ),
            FeatureState.DEGRADED,
            "directory_selection_injected_service",
            "injected",
        ),
        (
            CapabilityResult.unavailable(
                reason_code="directory_selection_service_unavailable",
            ),
            FeatureState.DISABLED,
            "directory_selection_service_unavailable",
            None,
        ),
        (
            CapabilityResult.unknown(
                reason_code="directory_selection_capability_probe_failed",
                source=CapabilitySource.CONSERVATIVE_FALLBACK,
            ),
            FeatureState.DISABLED,
            "directory_selection_capability_unknown",
            None,
        ),
    ],
)
def test_directory_selection_policy_is_a_pure_capability_table(
    capability,
    state,
    reason_code,
    route,
):
    decision = decide_directory_selection(capability)

    assert decision.feature is FeatureId.DIRECTORY_SELECTION
    assert decision.state is state
    assert decision.reason_code == reason_code
    assert decision.route == route
    assert decision.allows_execution is (
        state in {FeatureState.ENABLED, FeatureState.DEGRADED}
    )


@pytest.mark.parametrize(
    ("capability", "state", "reason_code", "route"),
    [
        (
            CapabilityResult.available(
                UpdatePackageRevealRoute.FINDER,
                reason_code="update_package_reveal_route_available",
            ),
            FeatureState.ENABLED,
            "update_package_reveal_available",
            "finder",
        ),
        (
            CapabilityResult.available(
                UpdatePackageRevealRoute.LEGACY_ADAPTER,
                reason_code="update_package_reveal_route_available",
            ),
            FeatureState.DEGRADED,
            "update_package_reveal_legacy_adapter",
            "legacy_adapter",
        ),
        (
            CapabilityResult.unavailable(
                reason_code="update_package_reveal_route_unsupported",
            ),
            FeatureState.DISABLED,
            "update_package_reveal_route_unsupported",
            None,
        ),
        (
            CapabilityResult.unknown(
                reason_code="update_package_reveal_capability_probe_failed",
                source=CapabilitySource.CONSERVATIVE_FALLBACK,
            ),
            FeatureState.DISABLED,
            "update_package_reveal_capability_unknown",
            None,
        ),
    ],
)
def test_update_package_reveal_policy_is_a_pure_capability_table(
    capability,
    state,
    reason_code,
    route,
):
    decision = decide_update_package_reveal(capability)

    assert decision.feature is FeatureId.UPDATE_PACKAGE_REVEAL
    assert decision.state is state
    assert decision.reason_code == reason_code
    assert decision.route == route
    assert decision.allows_execution is (
        state in {FeatureState.ENABLED, FeatureState.DEGRADED}
    )


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
