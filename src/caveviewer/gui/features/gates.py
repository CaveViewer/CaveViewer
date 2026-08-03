"""Immutable registry of feature decisions selected for one app runtime."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .ids import FeatureId
from .model import FeatureDecision


@dataclass(frozen=True, slots=True)
class FeatureGateRegistry:
    """Expose feature decisions without allowing callers to mutate a runtime."""

    decisions: Mapping[FeatureId, FeatureDecision]

    def __post_init__(self) -> None:
        frozen_decisions = dict(self.decisions)
        for feature, decision in frozen_decisions.items():
            if feature is not decision.feature:
                raise ValueError("feature gate key must match its decision feature")
        object.__setattr__(self, "decisions", MappingProxyType(frozen_decisions))

    def decision_for(self, feature: FeatureId) -> FeatureDecision:
        """Return the decision for a registered application feature."""
        return self.decisions[feature]
