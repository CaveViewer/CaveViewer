"""Tests for process lifecycle and exception diagnostics."""

from __future__ import annotations

import json
import sys
import threading
from types import SimpleNamespace

from caveviewer.core.diagnostics.application import ApplicationDiagnostics


def _records(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_application_diagnostics_records_lifecycle(tmp_path):
    path = tmp_path / "application_diagnostics.jsonl"
    diagnostics = ApplicationDiagnostics(
        clock=lambda: "2026-07-27T19:00:00.000+00:00",
        monotonic=lambda: 10.0,
        metadata={"app_name": "CaveViewer", "app_version": "test"},
        session_id="application-1",
    )

    diagnostics.bind_path(path, cache_dir=str(tmp_path), source="test")
    diagnostics.finalize(
        outcome="normal",
        exit_code=0,
        reason="main_returned",
    )

    records = _records(path)
    assert [record["event"] for record in records] == [
        "application_started",
        "application_diagnostics_bound",
        "application_shutdown_started",
        "application_process_exit",
    ]
    assert all(record["scope"] == "application" for record in records)
    assert records[0]["session_id"] == "application-1"
    assert records[-1]["outcome"] == "normal"
    assert records[-1]["explicit"] is True


def test_main_exception_hook_records_and_calls_previous_hook(tmp_path, monkeypatch):
    path = tmp_path / "application.jsonl"
    previous_calls = []
    monkeypatch.setattr(
        sys,
        "excepthook",
        lambda *args: previous_calls.append(args),
    )
    diagnostics = ApplicationDiagnostics(session_id="application-2")
    diagnostics.bind_path(path)
    diagnostics.install_hooks()

    try:
        raise RuntimeError("render thread boundary")
    except RuntimeError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)

    diagnostics.finalize(outcome="fatal_error", exit_code=1, reason="test")

    records = _records(path)
    exception = next(
        record
        for record in records
        if record["event"] == "application_uncaught_exception"
    )
    assert exception["exception_type"] == "RuntimeError"
    assert "render thread boundary" in exception["traceback"]
    assert exception["fatal"] is True
    assert len(previous_calls) == 1


def test_thread_exception_hook_records_worker_failure(tmp_path, monkeypatch):
    path = tmp_path / "application.jsonl"
    previous_calls = []
    monkeypatch.setattr(
        threading,
        "excepthook",
        lambda args: previous_calls.append(args),
    )
    diagnostics = ApplicationDiagnostics(session_id="application-3")
    diagnostics.bind_path(path)
    diagnostics.install_hooks()

    try:
        raise ValueError("worker failed")
    except ValueError as exc:
        args = SimpleNamespace(
            exc_type=type(exc),
            exc_value=exc,
            exc_traceback=exc.__traceback__,
            thread=threading.current_thread(),
        )
        threading.excepthook(args)

    diagnostics.finalize(outcome="normal", exit_code=0, reason="test")

    records = _records(path)
    exception = next(
        record
        for record in records
        if record["event"] == "application_thread_exception"
    )
    assert exception["exception_type"] == "ValueError"
    assert exception["exception_message"] == "worker failed"
    assert exception["fatal"] is False
    assert len(previous_calls) == 1
