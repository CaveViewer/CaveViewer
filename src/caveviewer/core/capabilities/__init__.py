"""Immutable capability values shared by feature-policy layers.

The package deliberately contains only values and validation.  Platform probes
live at application edges so core code remains independent of GUI toolkits,
operating systems, and process-level integrations.
"""

from .model import (
    CapabilityEvidenceValue,
    CapabilityResult,
    CapabilitySource,
    CapabilityStatus,
)

__all__ = [
    "CapabilityEvidenceValue",
    "CapabilityResult",
    "CapabilitySource",
    "CapabilityStatus",
]
