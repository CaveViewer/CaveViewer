"""Tests for shared action-time route-preflight validation."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from caveviewer.core.capabilities import CapabilityResult
from caveviewer.gui.features import FeatureDecision, FeatureId, FeatureState
from caveviewer.gui.features.preflight import validate_route_preflight


@dataclass(frozen=True)
class _Target:
    route_key: str


def _decision(
    *,
    feature: FeatureId = FeatureId.DIRECTORY_SELECTION,
    state: FeatureState = FeatureState.ENABLED,
    route: str | None = "demo-route",
) -> FeatureDecision:
    return FeatureDecision(
        feature=feature,
        state=state,
        reason_code="demo",
        explanation="Demo preflight.",
        route=route,
    )


def _validate(
    capability: CapabilityResult[_Target],
    decision: FeatureDecision,
) -> None:
    validate_route_preflight(
        capability=capability,
        decision=decision,
        expected_feature=FeatureId.DIRECTORY_SELECTION,
        target_type=_Target,
        route_for_target=lambda target: target.route_key,
        feature_label="demo",
    )


def test_route_preflight_accepts_an_available_matching_target():
    _validate(
        CapabilityResult.available(_Target("demo-route"), reason_code="available"),
        _decision(),
    )


def test_route_preflight_allows_a_non_executable_decision_without_a_target():
    _validate(
        CapabilityResult.unavailable(reason_code="unavailable"),
        _decision(state=FeatureState.DISABLED, route=None),
    )


def test_route_preflight_rejects_a_different_feature():
    with pytest.raises(ValueError, match="demo preflight must contain a demo decision"):
        _validate(
            CapabilityResult.available(
                _Target("demo-route"),
                reason_code="available",
            ),
            _decision(feature=FeatureId.FILE_SELECTION),
        )


def test_route_preflight_rejects_an_executable_decision_without_a_target():
    with pytest.raises(ValueError, match="requires an available typed target"):
        _validate(
            CapabilityResult.unavailable(reason_code="unavailable"),
            _decision(),
        )


def test_route_preflight_rejects_a_route_that_does_not_match_its_target():
    with pytest.raises(ValueError, match="must match its typed target"):
        _validate(
            CapabilityResult.available(
                _Target("other-route"),
                reason_code="available",
            ),
            _decision(),
        )
