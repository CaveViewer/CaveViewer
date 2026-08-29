"""Tests for the user-facing application-log catalog."""

from __future__ import annotations

import os
import time

import pytest

from caveviewer.core.diagnostics import catalog


def _write_log(path, content: str, *, modified_ns: int) -> None:
    path.write_text(content, encoding="utf-8")
    os.utime(path, ns=(modified_ns, modified_ns))


def test_application_logs_are_eligible_and_deterministically_newest_first(tmp_path):
    older = tmp_path / "viewer-session-older.log"
    tied_a = tmp_path / "viewer-session-a.log"
    tied_b = tmp_path / "viewer-session-b.log"
    startup = tmp_path / "startup.log"
    timestamp = time.time_ns()
    _write_log(older, "older", modified_ns=timestamp + 2_000_000_000)
    _write_log(tied_a, "a", modified_ns=timestamp + 3_000_000_000)
    _write_log(tied_b, "b", modified_ns=timestamp + 3_000_000_000)
    _write_log(startup, "startup", modified_ns=timestamp + 1_000_000_000)
    _write_log(
        tmp_path / "benchmark.log",
        "excluded",
        modified_ns=timestamp + 4_000_000_000,
    )
    _write_log(
        tmp_path / "viewer-session-b.jsonl",
        "excluded",
        modified_ns=timestamp + 5_000_000_000,
    )
    (tmp_path / "viewer-session-directory.log").mkdir()

    assert catalog.application_logs(tmp_path) == (
        tied_b,
        tied_a,
        older,
        startup,
    )


def test_catalog_handles_missing_directory_and_deleted_candidate(tmp_path, monkeypatch):
    assert catalog.application_logs(tmp_path / "missing") == ()

    log_path = tmp_path / "viewer-session-current.log"
    log_path.write_text("current", encoding="utf-8")
    original_stat = type(log_path).stat

    def disappearing_stat(path, *args, **kwargs):
        if path == log_path:
            raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(type(log_path), "stat", disappearing_stat)
    assert catalog.application_logs(tmp_path) == ()


def test_catalog_excludes_symlinks_that_could_escape_diagnostics(tmp_path):
    target = tmp_path.parent / "viewer-session-outside.log"
    target.write_text("outside", encoding="utf-8")
    link = tmp_path / "viewer-session-link.log"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable for this test user")

    assert catalog.application_logs(tmp_path) == ()
    removed = catalog.prune_session_logs(
        tmp_path,
        now=time.time() + catalog.DEFAULT_SESSION_LOG_MAX_AGE_SECONDS + 1,
    )
    assert removed == ()
    assert link.is_symlink()


def test_latest_readable_application_log_skips_open_failure(tmp_path, monkeypatch):
    newest = tmp_path / "viewer-session-newest.log"
    older = tmp_path / "viewer-session-older.log"
    timestamp = time.time_ns()
    _write_log(older, "older", modified_ns=timestamp + 1_000_000_000)
    _write_log(newest, "newest", modified_ns=timestamp + 2_000_000_000)
    original_open = type(newest).open

    def selective_open(path, *args, **kwargs):
        if path == newest:
            raise PermissionError(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(type(newest), "open", selective_open)
    assert catalog.latest_readable_application_log(tmp_path) == older


def test_prune_session_logs_preserves_startup_and_requested_log(tmp_path):
    timestamp = time.time_ns()
    logs = []
    for index in range(4):
        path = tmp_path / f"viewer-session-{index}.log"
        _write_log(
            path,
            str(index),
            modified_ns=timestamp + ((index + 1) * 1_000_000_000),
        )
        path.with_suffix(".jsonl").write_text(str(index), encoding="utf-8")
        logs.append(path)
    startup = tmp_path / "startup.log"
    _write_log(startup, "startup", modified_ns=timestamp + 10_000_000_000)

    removed = catalog.prune_session_logs(tmp_path, keep=2, preserve=(logs[0],))

    assert removed == (logs[1], logs[1].with_suffix(".jsonl"))
    assert logs[0].exists()
    assert logs[2].exists()
    assert logs[3].exists()
    assert startup.exists()
    assert not logs[1].with_suffix(".jsonl").exists()


def test_prune_session_logs_expires_artifacts_older_than_one_day(tmp_path):
    now = 1_000_000.0
    old_log = tmp_path / "viewer-session-old.log"
    old_jsonl = old_log.with_suffix(".jsonl")
    orphan_jsonl = tmp_path / "viewer-session-orphan.jsonl"
    boundary_log = tmp_path / "viewer-session-boundary.log"
    future_log = tmp_path / "viewer-session-future.log"
    startup_log = tmp_path / "startup.log"
    unrelated_log = tmp_path / "benchmark.log"
    for path in (
        old_log,
        old_jsonl,
        orphan_jsonl,
        boundary_log,
        future_log,
        startup_log,
        unrelated_log,
    ):
        path.write_text(path.name, encoding="utf-8")
    old_timestamp = now - catalog.DEFAULT_SESSION_LOG_MAX_AGE_SECONDS - 1
    boundary_timestamp = now - catalog.DEFAULT_SESSION_LOG_MAX_AGE_SECONDS
    for path in (old_log, old_jsonl, orphan_jsonl):
        os.utime(path, (old_timestamp, old_timestamp))
    os.utime(boundary_log, (boundary_timestamp, boundary_timestamp))
    os.utime(future_log, (now + 60, now + 60))
    os.utime(startup_log, (old_timestamp, old_timestamp))
    os.utime(unrelated_log, (old_timestamp, old_timestamp))

    removed = catalog.prune_session_logs(tmp_path, keep=10, now=now)

    assert set(removed) == {old_log, old_jsonl, orphan_jsonl}
    assert boundary_log.exists()
    assert future_log.exists()
    assert startup_log.exists()
    assert unrelated_log.exists()


def test_prune_session_logs_preserves_active_session_even_when_timestamp_is_old(
    tmp_path,
):
    now = 1_000_000.0
    active_log = tmp_path / "viewer-session-active.log"
    active_jsonl = active_log.with_suffix(".jsonl")
    active_log.write_text("active", encoding="utf-8")
    active_jsonl.write_text("active", encoding="utf-8")
    old_timestamp = now - catalog.DEFAULT_SESSION_LOG_MAX_AGE_SECONDS - 1
    os.utime(active_log, (old_timestamp, old_timestamp))
    os.utime(active_jsonl, (old_timestamp, old_timestamp))

    removed = catalog.prune_session_logs(
        tmp_path,
        keep=10,
        preserve=(active_log,),
        now=now,
    )

    assert removed == ()
    assert active_log.exists()
    assert active_jsonl.exists()


def test_prune_session_logs_ignores_deletion_failure(
    tmp_path,
    monkeypatch,
):
    now = 1_000_000.0
    blocked_log = tmp_path / "viewer-session-blocked.log"
    blocked_log.write_text("blocked", encoding="utf-8")
    old_timestamp = now - catalog.DEFAULT_SESSION_LOG_MAX_AGE_SECONDS - 1
    os.utime(blocked_log, (old_timestamp, old_timestamp))
    original_unlink = type(blocked_log).unlink

    def fail_blocked(path, *args, **kwargs):
        if path == blocked_log:
            raise PermissionError(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(blocked_log), "unlink", fail_blocked)
    removed = catalog.prune_session_logs(tmp_path, keep=10, now=now)

    assert removed == ()
    assert blocked_log.exists()


def test_last_error_excerpt_includes_three_physical_context_lines_and_traceback(
    tmp_path,
):
    path = tmp_path / "viewer-session-current.log"
    path.write_text(
        "2026-08-29T10:00:00 [caveviewer] INFO: first\n"
        "2026-08-29T10:00:01 [caveviewer] INFO: second\n"
        "2026-08-29T10:00:02 [caveviewer] WARNING: third café\n"
        "2026-08-29T10:00:03 [caveviewer] ERROR: context failed\n"
        "Traceback (most recent call last):\n"
        "  RuntimeError: boom\n"
        "2026-08-29T10:00:04 [caveviewer] INFO: recovered\n",
        encoding="utf-8",
    )

    excerpt = catalog.read_last_error_excerpt(path)

    assert excerpt is not None
    assert excerpt.context_line_count == 3
    assert excerpt.error_line_count == 3
    assert excerpt.text == (
        "2026-08-29T10:00:00 [caveviewer] INFO: first\n"
        "2026-08-29T10:00:01 [caveviewer] INFO: second\n"
        "2026-08-29T10:00:02 [caveviewer] WARNING: third café\n"
        "2026-08-29T10:00:03 [caveviewer] ERROR: context failed\n"
        "Traceback (most recent call last):\n"
        "  RuntimeError: boom"
    )


def test_last_error_excerpt_supports_crlf_and_fewer_context_lines(tmp_path):
    path = tmp_path / "viewer-session-current.log"
    path.write_bytes(
        b"2026-08-29T10:00:00 [caveviewer] INFO: ready\r\n"
        b"2026-08-29T10:00:01 [caveviewer] ERROR: failed\r\n"
    )

    excerpt = catalog.read_last_error_excerpt(path)

    assert excerpt is not None
    assert excerpt.context_line_count == 1
    assert excerpt.text.endswith("[caveviewer] ERROR: failed")
    assert "\r" not in excerpt.text


def test_last_error_excerpt_ignores_incomplete_final_record(tmp_path):
    path = tmp_path / "viewer-session-current.log"
    path.write_text(
        "2026-08-29T10:00:00 [caveviewer] ERROR: complete\n"
        "2026-08-29T10:00:01 [caveviewer] ERROR: partial",
        encoding="utf-8",
    )

    excerpt = catalog.read_last_error_excerpt(path)

    assert excerpt is not None
    assert excerpt.text.endswith("ERROR: complete")
    assert "partial" not in excerpt.text


def test_last_error_excerpt_returns_none_for_malformed_or_error_free_log(tmp_path):
    path = tmp_path / "viewer-session-current.log"
    path.write_text(
        "malformed ERROR text\n"
        "2026-08-29T10:00:00 [caveviewer] INFO: ready\n",
        encoding="utf-8",
    )

    assert catalog.read_last_error_excerpt(path) is None


def test_last_error_excerpt_bounds_large_file_search_and_display(tmp_path):
    path = tmp_path / "viewer-session-current.log"
    path.write_text(
        ("old noise\n" * 10_000)
        + "2026-08-29T10:00:00 [caveviewer] ERROR: "
        + ("x" * 2_000)
        + "\n",
        encoding="utf-8",
    )

    excerpt = catalog.read_last_error_excerpt(
        path,
        max_search_bytes=4_096,
        max_display_characters=200,
    )

    assert excerpt is not None
    assert excerpt.truncated is True
    assert len(excerpt.text) < 230
    assert excerpt.text.endswith("…[truncated]")


def test_last_error_excerpt_retries_when_log_rotates(tmp_path, monkeypatch):
    path = tmp_path / "viewer-session-current.log"
    path.write_text(
        "2026-08-29T10:00:00 [caveviewer] ERROR: old\n",
        encoding="utf-8",
    )
    original_identity = catalog._same_file_identity
    calls = []

    def rotate_once(opened_stat, current_stat):
        calls.append((opened_stat, current_stat))
        if len(calls) == 1:
            replacement = tmp_path / "replacement.log"
            replacement.write_text(
                "2026-08-29T10:00:01 [caveviewer] ERROR: new\n",
                encoding="utf-8",
            )
            os.replace(replacement, path)
            return False
        return original_identity(opened_stat, current_stat)

    monkeypatch.setattr(catalog, "_same_file_identity", rotate_once)

    excerpt = catalog.read_last_error_excerpt(path)

    assert excerpt is not None
    assert excerpt.text.endswith("ERROR: new")
    assert len(calls) == 2
