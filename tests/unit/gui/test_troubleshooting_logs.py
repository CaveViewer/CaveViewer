"""Tests for Help troubleshooting log state and reveal actions."""

from __future__ import annotations

import os
import time

from caveviewer.gui.troubleshooting_logs import TroubleshootingLogController


class _RevealAdapter:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.paths = []

    def reveal_diagnostic_log(self, log_path: str) -> None:
        self.paths.append(log_path)
        if self.error is not None:
            raise self.error


def test_refresh_reports_empty_directory(tmp_path):
    state = TroubleshootingLogController(
        tmp_path,
        _RevealAdapter(),
    ).refresh()

    assert state.latest_log is None
    assert state.can_reveal is False
    assert "No logs yet" in state.status_text


def test_reveal_latest_resolves_again_at_action_time(tmp_path):
    older = tmp_path / "viewer-session-older.log"
    older.write_text("older", encoding="utf-8")
    controller = TroubleshootingLogController(tmp_path, _RevealAdapter())
    assert controller.refresh().latest_log == older

    newest = tmp_path / "viewer-session-newest.log"
    newest.write_text("newest", encoding="utf-8")
    future = time.time_ns() + 2_000_000_000
    os.utime(newest, ns=(future, future))

    state = controller.reveal_latest()

    assert state.latest_log == newest
    assert controller.reveal_adapter.paths == [str(newest)]
    assert "selected the latest log" in state.status_text


def test_reveal_latest_handles_log_deleted_after_refresh(tmp_path):
    path = tmp_path / "viewer-session-current.log"
    path.write_text("current", encoding="utf-8")
    controller = TroubleshootingLogController(tmp_path, _RevealAdapter())
    assert controller.refresh().latest_log == path
    path.unlink()
    state = controller.reveal_latest()

    assert state.latest_log is None
    assert controller.reveal_adapter.paths == []


def test_reveal_latest_returns_nonfatal_failure_feedback(tmp_path):
    path = tmp_path / "viewer-session-current.log"
    path.write_text("current", encoding="utf-8")
    adapter = _RevealAdapter(error=OSError("desktop unavailable"))

    state = TroubleshootingLogController(tmp_path, adapter).reveal_latest()

    assert state.latest_log == path
    assert state.is_error is True
    assert "Couldn’t open" in state.status_text
