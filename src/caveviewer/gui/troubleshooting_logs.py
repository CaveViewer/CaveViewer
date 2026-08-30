"""Help-panel state and actions for discovering application logs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from caveviewer.core.diagnostics.catalog import (
    ErrorLogExcerpt,
    latest_readable_application_log,
    read_last_error_excerpt,
)
from caveviewer.gui.platform.diagnostic_log_reveal import (
    DiagnosticLogRevealAdapter,
)


@dataclass(frozen=True, slots=True)
class TroubleshootingLogState:
    """One refresh of the Help troubleshooting log state."""

    latest_log: Path | None
    status_text: str
    is_error: bool = False
    error_excerpt: ErrorLogExcerpt | None = None
    error_status_text: str = ""

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
                error_status_text="The latest error will appear here when available.",
            )
        try:
            excerpt = read_last_error_excerpt(latest)
        except OSError:
            return TroubleshootingLogState(
                latest_log=latest,
                status_text="",
                is_error=True,
                error_status_text="The latest log is temporarily unavailable.",
            )
        if excerpt is None:
            return TroubleshootingLogState(
                latest_log=latest,
                status_text="",
                error_status_text="No errors were recorded in the latest log.",
            )
        return TroubleshootingLogState(
            latest_log=latest,
            status_text="",
            error_excerpt=excerpt,
        )

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
                error_excerpt=state.error_excerpt,
                error_status_text=state.error_status_text,
            )
        return TroubleshootingLogState(
            latest_log=state.latest_log,
            # The selected file in the opened browser is sufficient success
            # feedback; reserve this line for actionable reveal failures.
            status_text="",
            error_excerpt=state.error_excerpt,
            error_status_text=state.error_status_text,
        )
