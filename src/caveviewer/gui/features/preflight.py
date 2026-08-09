"""Shared validation for side-effect-free route-authorizing preflights."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from caveviewer.core.capabilities import CapabilityResult, CapabilityStatus

from .ids import FeatureId
from .model import FeatureDecision


Target = TypeVar("Target")


def validate_route_preflight(
    *,
    capability: CapabilityResult[Target],
    decision: FeatureDecision,
    expected_feature: FeatureId,
    target_type: type[Target],
    route_for_target: Callable[[Target], str],
    feature_label: str,
    target_label: str = "typed target",
) -> None:
    """Validate the invariant shared by executable action-time preflights.

    Policies choose a route from one capability snapshot. Before a caller can
    invoke its native action, the feature identity, typed target, and selected
    route must still agree. Disabled and hidden decisions deliberately need no
    target because they authorize no route.
    """
    if decision.feature is not expected_feature:
        raise ValueError(
            f"{feature_label} preflight must contain a {feature_label} decision"
        )
    if not decision.allows_execution:
        return

    target = capability.value
    if (
        capability.status is not CapabilityStatus.AVAILABLE
        or not isinstance(target, target_type)
    ):
        raise ValueError(
            f"executable {feature_label} preflight requires an available "
            f"{target_label}"
        )
    if decision.route != route_for_target(target):
        raise ValueError(
            f"{feature_label} decision route must match its {target_label}"
        )
