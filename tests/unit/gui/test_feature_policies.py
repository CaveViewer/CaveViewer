"""Test pure feature gates without platform or GUI side effects."""

from __future__ import annotations

import pytest

from caveviewer.core.capabilities import CapabilityResult, CapabilitySource
from caveviewer.gui.features import (
    FeatureGateRegistry,
    FeatureId,
    FeatureState,
    decide_automatic_update,
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
