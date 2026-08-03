"""Platform-bound capability probes with no product-policy decisions."""

from .updates import (
    UpdateConfiguration,
    UpdateTarget,
    build_update_configuration,
    probe_automatic_update,
)

__all__ = [
    "UpdateConfiguration",
    "UpdateTarget",
    "build_update_configuration",
    "probe_automatic_update",
]
