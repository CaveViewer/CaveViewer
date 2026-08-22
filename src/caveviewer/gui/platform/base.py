"""Compatibility protocol for remaining action-time GUI platform effects.

Static fonts, layouts, shortcuts, input conventions, scaling, and startup
policy belong to :mod:`caveviewer.gui.platform.presentation`.  This protocol
is deliberately restricted to legacy native actions that later refactors will
move into focused adapters.
"""

from __future__ import annotations

from typing import Protocol


class SplashPlatformAdapter(Protocol):
    """Frozen compatibility marker pending deletion of the broad factory."""
