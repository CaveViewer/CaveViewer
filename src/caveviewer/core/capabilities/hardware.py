"""Immutable hardware values returned by RAM and GPU capability probes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GpuMemoryBudget:
    """A positive GPU-memory budget selected for one runtime policy decision."""

    total_bytes: int

    def __post_init__(self) -> None:
        total_bytes = int(self.total_bytes)
        if total_bytes <= 0:
            raise ValueError("GPU memory budget must be positive")
        object.__setattr__(self, "total_bytes", total_bytes)


@dataclass(frozen=True, slots=True)
class RamAvailability:
    """A bounded physical-RAM measurement suitable for admission policy."""

    total_bytes: int
    available_bytes: int

    def __post_init__(self) -> None:
        total_bytes = int(self.total_bytes)
        available_bytes = int(self.available_bytes)
        if total_bytes <= 0:
            raise ValueError("RAM total must be positive")
        if available_bytes < 0 or available_bytes > total_bytes:
            raise ValueError("RAM availability must be between zero and total RAM")
        object.__setattr__(self, "total_bytes", total_bytes)
        object.__setattr__(self, "available_bytes", available_bytes)

    @property
    def utilization_fraction(self) -> float:
        """Return the fraction of physical RAM that is currently unavailable."""
        return 1.0 - (self.available_bytes / self.total_bytes)
