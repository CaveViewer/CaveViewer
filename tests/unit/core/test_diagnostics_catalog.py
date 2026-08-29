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

    assert removed == (logs[1],)
    assert logs[0].exists()
    assert logs[2].exists()
    assert logs[3].exists()
    assert startup.exists()
    assert not logs[1].with_suffix(".jsonl").exists()


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
