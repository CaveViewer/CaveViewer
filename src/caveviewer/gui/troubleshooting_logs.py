"""Help-panel state and actions for discovering application logs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from caveviewer.core.diagnostics.catalog import latest_readable_application_log
from caveviewer.gui.platform.diagnostic_log_reveal import (
    DiagnosticLogRevealAdapter,
)


@dataclass(frozen=True, slots=True)
class TroubleshootingLogState:
    """One refresh of the Help troubleshooting log state."""

    latest_log: Path | None
    status_text: str
    is_error: bool = False

    @property
    def can_reveal(self) -> bool:
        return self.latest_log is not None


@dataclass(frozen=True, slots=True)
class TroubleshootingLogController:
    """Resolve and reveal the latest log without importing Tk."""

    directory: Path
    reveal_adapter: DiagnosticLogRevealAdapter

    def refresh(self) -> TroubleshootingLogState:
        """Return current log availability for presentation."""

        latest = latest_readable_application_log(self.directory)
        if latest is None:
            return TroubleshootingLogState(
                latest_log=None,
                status_text=(
                    "No logs yet. A log will appear after CaveViewer records "
                    "an application session."
                ),
            )
        return TroubleshootingLogState(latest_log=latest, status_text="")

    def reveal_latest(self) -> TroubleshootingLogState:
        """Resolve again at action time and reveal the newest readable log."""

        state = self.refresh()
        if state.latest_log is None:
            return state
        try:
            self.reveal_adapter.reveal_diagnostic_log(
                os.fspath(state.latest_log)
            )
        except Exception:
            return TroubleshootingLogState(
                latest_log=state.latest_log,
                status_text=(
                    "Couldn’t open the log folder. The latest log remains "
                    f"at {state.latest_log}."
                ),
                is_error=True,
            )
        return TroubleshootingLogState(
            latest_log=state.latest_log,
            status_text="Opened the log folder and selected the latest log.",
        )
