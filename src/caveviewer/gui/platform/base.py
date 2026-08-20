"""Compatibility protocol for remaining action-time GUI platform effects.

Static fonts, layouts, shortcuts, input conventions, scaling, and startup
policy belong to :mod:`caveviewer.gui.platform.presentation`.  This protocol
is deliberately restricted to legacy native actions that later refactors will
move into focused adapters.
"""

from __future__ import annotations

from typing import Any, Protocol


class SplashPlatformAdapter(Protocol):
    """Legacy action hooks not yet moved to focused adapter contracts."""

    def reveal_file(self, path: str) -> None:
        ...

    def load_system_certificates(self, context: Any) -> None:
        ...

    def recording_subprocess_startup_kwargs(self) -> dict[str, Any]:
        ...
