"""Tests for best-effort per-thread worker priority adjustment."""

from __future__ import annotations

from caveviewer.core.workers import priority


def test_lower_current_thread_priority_targets_native_thread(monkeypatch):
    calls = []

    monkeypatch.setattr(priority.sys, "platform", "linux")
    monkeypatch.setattr(priority.os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(
        priority.os,
        "getpriority",
        lambda _kind, native_id: 2,
        raising=False,
    )
    monkeypatch.setattr(
        priority.os,
        "setpriority",
        lambda kind, native_id, value: calls.append((kind, native_id, value)),
        raising=False,
    )
    monkeypatch.setattr(priority.threading, "get_native_id", lambda: 1234)
    monkeypatch.setenv(priority.STREAMING_WORKER_NICE_ENV_VAR, "4")

    assert priority.lower_current_thread_priority() is True
    assert calls == [(0, 1234, 6)]


def test_lower_current_thread_priority_does_not_exceed_linux_limit(monkeypatch):
    calls = []

    monkeypatch.setattr(priority.sys, "platform", "linux")
    monkeypatch.setattr(priority.os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(
        priority.os,
        "getpriority",
        lambda _kind, _native_id: 19,
        raising=False,
    )
    monkeypatch.setattr(
        priority.os,
        "setpriority",
        lambda *args: calls.append(args),
        raising=False,
    )

    assert priority.lower_current_thread_priority(environ={}) is False
    assert calls == []


def test_lower_current_thread_priority_is_disabled_by_zero(monkeypatch):
    monkeypatch.setattr(priority.sys, "platform", "linux")
    monkeypatch.setenv(priority.STREAMING_WORKER_NICE_ENV_VAR, "0")

    assert priority.lower_current_thread_priority() is False
