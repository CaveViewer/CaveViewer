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
from .desktop import (
    DesktopNotificationRoute,
    DesktopNotificationTarget,
    DirectorySelectionRoute,
    DirectorySelectionTarget,
    FileSelectionRoute,
    FileSelectionTarget,
    IdleSuspendInhibitionRoute,
    IdleSuspendInhibitionTarget,
    UpdatePackageRevealRoute,
)
from .hardware import GpuMemoryBudget, RamAvailability
from .windowing import (
    ViewerLaunchRoute,
    ViewerLaunchTarget,
    WindowBackendPlan,
    WindowSystem,
)

__all__ = [
    "CapabilityEvidenceValue",
    "CapabilityResult",
    "CapabilitySource",
    "CapabilityStatus",
    "DesktopNotificationRoute",
    "DesktopNotificationTarget",
    "DirectorySelectionRoute",
    "DirectorySelectionTarget",
    "FileSelectionRoute",
    "FileSelectionTarget",
    "IdleSuspendInhibitionRoute",
    "IdleSuspendInhibitionTarget",
    "UpdatePackageRevealRoute",
    "ViewerLaunchRoute",
    "ViewerLaunchTarget",
    "WindowBackendPlan",
    "WindowSystem",
    "GpuMemoryBudget",
    "RamAvailability",
]
