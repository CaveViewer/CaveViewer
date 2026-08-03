"""Immutable results of pure feature-availability policy evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .ids import FeatureId


class FeatureState(str, Enum):
    """Presentation and execution state selected by a feature policy.

    ``ENABLED`` selects a normal executable route. ``DEGRADED`` selects a
    documented safe fallback route and remains executable. ``DISABLED`` may be
    presented with an explanation but never authorizes the feature service.
    ``HIDDEN`` is neither presented nor executable.
    """

    ENABLED = "enabled"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    HIDDEN = "hidden"


@dataclass(frozen=True, slots=True)
class FeatureDecision:
    """A stable feature decision suitable for UI and enforcement boundaries.

    ``reason_code`` is a machine-stable diagnostic identifier; ``explanation``
    is concise, user-safe presentation text. A route is present exactly when
    the policy authorizes execution. UI state remains advisory: action-time
    policies must be re-evaluated immediately before irreversible work.
    """

    feature: FeatureId
    state: FeatureState
    reason_code: str
    explanation: str
    route: str | None = None

    def __post_init__(self) -> None:
        reason_code = self.reason_code.strip()
        explanation = self.explanation.strip()
        if not reason_code:
            raise ValueError("feature decision reason_code must be non-empty")
        if not explanation:
            raise ValueError("feature decision explanation must be non-empty")
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "explanation", explanation)
        if self.route is not None:
            route = self.route.strip()
            if not route:
                raise ValueError("feature decision route must be non-empty when set")
            object.__setattr__(self, "route", route)
        if self.allows_execution and self.route is None:
            raise ValueError("executable feature decisions must select a route")
        if not self.allows_execution and self.route is not None:
            raise ValueError("non-executable feature decisions must not select a route")

    @property
    def allows_execution(self) -> bool:
        """Return whether the feature service may invoke its selected route."""
        return self.state in {FeatureState.ENABLED, FeatureState.DEGRADED}

    @property
    def is_visible(self) -> bool:
        """Return whether UI may present the feature or its explanation."""
        return self.state is not FeatureState.HIDDEN
