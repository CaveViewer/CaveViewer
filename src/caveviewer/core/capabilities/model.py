"""Typed, immutable facts returned by runtime capability probes.

Capability probes report what is known about the environment.  They do not
make product decisions: feature policies consume these values separately and
choose whether a feature is enabled, degraded, or unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Generic, Mapping, TypeVar


class CapabilityStatus(str, Enum):
    """How confidently a probe can describe a capability."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class CapabilitySource(str, Enum):
    """Where a capability fact or conservative fallback came from."""

    DETECTED = "detected"
    USER_OVERRIDE = "user_override"
    CONSERVATIVE_FALLBACK = "conservative_fallback"


CapabilityEvidenceValue = str | int | float | bool | None
CapabilityValue = TypeVar("CapabilityValue")


@dataclass(frozen=True, slots=True)
class CapabilityResult(Generic[CapabilityValue]):
    """One immutable capability fact with diagnostics-safe evidence.

    ``reason_code`` is machine-stable and deliberately separate from UI text.
    Evidence is copied into a read-only mapping and permits only scalar values
    so probes cannot leave mutable environment state inside a snapshot.
    """

    status: CapabilityStatus
    value: CapabilityValue | None
    source: CapabilitySource
    reason_code: str
    evidence: Mapping[str, CapabilityEvidenceValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        reason_code = self.reason_code.strip()
        if not reason_code:
            raise ValueError("capability reason_code must be non-empty")
        object.__setattr__(self, "reason_code", reason_code)

        frozen_evidence: dict[str, CapabilityEvidenceValue] = {}
        for key, value in self.evidence.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                raise ValueError("capability evidence keys must be non-empty")
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise TypeError(
                    "capability evidence values must be scalar diagnostics values"
                )
            frozen_evidence[normalized_key] = value
        object.__setattr__(self, "evidence", MappingProxyType(frozen_evidence))

    @property
    def is_available(self) -> bool:
        """Return whether the probe positively established availability."""
        return self.status is CapabilityStatus.AVAILABLE

    @classmethod
    def available(
        cls,
        value: CapabilityValue,
        *,
        reason_code: str,
        source: CapabilitySource = CapabilitySource.DETECTED,
        evidence: Mapping[str, CapabilityEvidenceValue] | None = None,
    ) -> "CapabilityResult[CapabilityValue]":
        """Build an available result without repeating the status value."""
        return cls(
            status=CapabilityStatus.AVAILABLE,
            value=value,
            source=source,
            reason_code=reason_code,
            evidence=evidence or {},
        )

    @classmethod
    def unavailable(
        cls,
        *,
        reason_code: str,
        source: CapabilitySource = CapabilitySource.DETECTED,
        evidence: Mapping[str, CapabilityEvidenceValue] | None = None,
    ) -> "CapabilityResult[CapabilityValue]":
        """Build an unavailable result without an executable value."""
        return cls(
            status=CapabilityStatus.UNAVAILABLE,
            value=None,
            source=source,
            reason_code=reason_code,
            evidence=evidence or {},
        )

    @classmethod
    def unknown(
        cls,
        *,
        reason_code: str,
        source: CapabilitySource = CapabilitySource.CONSERVATIVE_FALLBACK,
        evidence: Mapping[str, CapabilityEvidenceValue] | None = None,
    ) -> "CapabilityResult[CapabilityValue]":
        """Build an indeterminate result without pretending it is false."""
        return cls(
            status=CapabilityStatus.UNKNOWN,
            value=None,
            source=source,
            reason_code=reason_code,
            evidence=evidence or {},
        )
