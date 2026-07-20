"""Memory utilization target parsing for runtime and import policy."""

from __future__ import annotations


def parse_target_fraction(
    raw_value: str | None, conservative_default: float
) -> float:
    """Parse either a fraction or percentage and apply safe guardrails."""
    if raw_value is None:
        return conservative_default

    text = raw_value.strip()
    if not text:
        return conservative_default

    try:
        value = float(text)
    except ValueError:
        return conservative_default

    if value > 1.0:
        value = value / 100.0
    return max(0.01, min(0.80, value))


def parse_memory_target_fraction(raw_value: str | None) -> float:
    return parse_target_fraction(raw_value, conservative_default=0.08)


def parse_gpu_target_fraction(raw_value: str | None) -> float:
    return parse_target_fraction(raw_value, conservative_default=0.70)
