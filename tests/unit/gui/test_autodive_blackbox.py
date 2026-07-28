"""Tests for Guided Dive blackbox JSONL diagnostics."""

from __future__ import annotations

import json
import os

import numpy as np

from caveviewer.core.diagnostics.application import (
    ApplicationDiagnostics,
    set_active_application_diagnostics,
)
from caveviewer.gui.autodive_blackbox import (
    AUTO_DIVE_BLACKBOX_FILENAME,
    AUTO_DIVE_BLACKBOX_SCHEMA_VERSION,
    AutoDiveBlackbox,
    auto_dive_blackbox_path,
)


def test_auto_dive_blackbox_writes_jsonl_events(tmp_path):
    path = tmp_path / AUTO_DIVE_BLACKBOX_FILENAME
    blackbox = AutoDiveBlackbox(
        path,
        session_id="session-1",
        clock=lambda: "2026-07-24T00:00:00.000+00:00",
    )

    blackbox.record(
        "frame",
        position=np.array([1.0, 2.0, 3.0]),
        bad_number=float("inf"),
    )
    blackbox.close()
    blackbox.record("ignored_after_close")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    payload = json.loads(lines[0])
    assert payload["event"] == "frame"
    assert payload["session_id"] == "session-1"
    assert payload["schema_version"] == AUTO_DIVE_BLACKBOX_SCHEMA_VERSION
    assert payload["position"] == [1.0, 2.0, 3.0]
    assert payload["bad_number"] == "inf"


def test_auto_dive_blackbox_path_uses_cache_dir():
    path = auto_dive_blackbox_path("/tmp/cache")

    assert os.path.basename(path) == AUTO_DIVE_BLACKBOX_FILENAME
    assert os.path.basename(os.path.dirname(path)) == "cache"


def test_application_lifecycle_events_continue_after_guided_dive_stops(tmp_path):
    path = tmp_path / AUTO_DIVE_BLACKBOX_FILENAME
    application = ApplicationDiagnostics(session_id="application-1")
    set_active_application_diagnostics(application)
    try:
        blackbox = AutoDiveBlackbox(path, session_id="dive-1")
        blackbox.record("auto_dive_started")
        blackbox.close()

        application.record("viewer_session_returned", outcome="window_closed")
        application.finalize(
            outcome="normal",
            exit_code=0,
            reason="main_returned",
        )
    finally:
        set_active_application_diagnostics(None)

    events = [
        json.loads(line)["event"]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert "auto_dive_started" in events
    assert "viewer_session_returned" in events
    assert "application_process_exit" in events
