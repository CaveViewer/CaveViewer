"""Discover and retain user-facing application diagnostic logs."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from caveviewer.storage_paths import resolve_application_paths


DIAGNOSTICS_DIRECTORY_NAME = "diagnostics"
SESSION_LOG_PREFIX = "viewer-session-"
STARTUP_LOG_FILENAME = "startup.log"
DEFAULT_SESSION_LOG_RETENTION = 10
DEFAULT_SESSION_LOG_MAX_AGE_SECONDS = 24 * 60 * 60
DEFAULT_ERROR_SEARCH_BYTES = 256 * 1024
DEFAULT_ERROR_DISPLAY_CHARACTERS = 32 * 1024
_LOG_RECORD_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)? "
    r"\[[^\]]+\] (?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL):(?: |$)"
)


@dataclass(frozen=True, slots=True)
class ErrorLogExcerpt:
    """A complete error record and its preceding physical context lines."""

    text: str
    context_line_count: int
    error_line_count: int
    truncated: bool = False


def application_log_directory(*, platform_name: str | None = None) -> Path:
    """Return the platform-specific directory containing application logs."""

    return (
        resolve_application_paths(platform_name=platform_name).state_dir
        / DIAGNOSTICS_DIRECTORY_NAME
    )


def application_logs(directory: str | os.PathLike[str]) -> tuple[Path, ...]:
    """Return eligible direct-child logs in deterministic newest-first order."""

    root = Path(directory)
    try:
        candidates = tuple(root.iterdir())
    except OSError:
        return ()

    logs: list[tuple[int, str, Path]] = []
    for candidate in candidates:
        if not _is_eligible_log(candidate):
            continue
        try:
            modified_ns = candidate.stat().st_mtime_ns
        except OSError:
            continue
        logs.append((modified_ns, candidate.name, candidate))

    logs.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return tuple(item[2] for item in logs)


def latest_readable_application_log(
    directory: str | os.PathLike[str],
) -> Path | None:
    """Return the newest eligible log that can currently be opened for reading."""

    for candidate in application_logs(directory):
        try:
            with candidate.open("rb") as stream:
                stream.read(1)
        except OSError:
            continue
        return candidate
    return None


def read_last_error_excerpt(
    path: str | os.PathLike[str],
    *,
    context_lines: int = 3,
    max_search_bytes: int = DEFAULT_ERROR_SEARCH_BYTES,
    max_display_characters: int = DEFAULT_ERROR_DISPLAY_CHARACTERS,
) -> ErrorLogExcerpt | None:
    """Read the newest complete ERROR record from a bounded file tail.

    The file identity is checked after reading. If rotation replaced the path,
    the operation retries once against the new file. A concurrently growing
    file is treated as the stable snapshot size observed when its handle was
    opened.
    """

    log_path = Path(path)
    search_limit = max(1, int(max_search_bytes))
    display_limit = max(1, int(max_display_characters))
    for attempt in range(2):
        with log_path.open("rb") as stream:
            opened_stat = os.fstat(stream.fileno())
            size = max(0, int(opened_stat.st_size))
            start = max(0, size - search_limit)
            stream.seek(start)
            payload = stream.read(size - start)
        try:
            current_stat = log_path.stat()
        except OSError:
            if attempt == 0:
                continue
            raise
        if _same_file_identity(opened_stat, current_stat):
            return _error_excerpt_from_tail(
                payload,
                starts_at_file_beginning=start == 0,
                context_lines=max(0, int(context_lines)),
                max_display_characters=display_limit,
            )
    return None


def prune_session_logs(
    directory: str | os.PathLike[str],
    *,
    keep: int = DEFAULT_SESSION_LOG_RETENTION,
    preserve: Iterable[str | os.PathLike[str]] = (),
    max_age_seconds: float = DEFAULT_SESSION_LOG_MAX_AGE_SECONDS,
    now: float | None = None,
) -> tuple[Path, ...]:
    """Best-effort expire old session artifacts, then apply the count cap."""

    retention = max(1, int(keep))
    preserved_paths = tuple(preserve)
    preserved = {_normalized_path(path) for path in preserved_paths}
    preserved.update(
        _normalized_path(Path(path).with_suffix(".jsonl"))
        for path in preserved_paths
    )
    cutoff = (time.time() if now is None else float(now)) - max(
        0.0,
        float(max_age_seconds),
    )
    removed: list[Path] = []

    # Age cleanup includes orphan JSONL files left after an interrupted session.
    for artifact in _session_artifacts(directory):
        if _normalized_path(artifact) in preserved:
            continue
        try:
            modified_at = artifact.stat().st_mtime
        except OSError:
            continue
        if modified_at >= cutoff:
            continue
        if _unlink_session_artifact(artifact):
            removed.append(artifact)

    session_logs = tuple(
        path
        for path in application_logs(directory)
        if path.name.startswith(SESSION_LOG_PREFIX)
    )
    retained = set(session_logs[:retention])
    retained.update(
        path for path in session_logs if _normalized_path(path) in preserved
    )

    for path in session_logs:
        if path in retained or _normalized_path(path) in preserved:
            continue
        if _unlink_session_artifact(path):
            removed.append(path)
        companion = path.with_suffix(".jsonl")
        if (
            _normalized_path(companion) not in preserved
            and _unlink_session_artifact(companion)
        ):
            removed.append(companion)
    return tuple(removed)


def _is_eligible_log(path: Path) -> bool:
    try:
        if path.is_symlink():
            return False
    except OSError:
        return False
    name = path.name
    if name != STARTUP_LOG_FILENAME and not (
        name.startswith(SESSION_LOG_PREFIX) and name.endswith(".log")
    ):
        return False
    try:
        return path.is_file()
    except OSError:
        return False


def _normalized_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _session_artifacts(directory: str | os.PathLike[str]) -> tuple[Path, ...]:
    root = Path(directory)
    try:
        candidates = tuple(root.iterdir())
    except OSError:
        return ()
    artifacts = []
    for path in candidates:
        if not path.name.startswith(SESSION_LOG_PREFIX):
            continue
        if path.suffix not in {".log", ".jsonl"}:
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        artifacts.append(path)
    return tuple(artifacts)


def _unlink_session_artifact(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        path.unlink()
    except OSError:
        return False
    return True


def _same_file_identity(opened_stat, current_stat) -> bool:
    opened_identity = (
        getattr(opened_stat, "st_dev", None),
        getattr(opened_stat, "st_ino", None),
    )
    current_identity = (
        getattr(current_stat, "st_dev", None),
        getattr(current_stat, "st_ino", None),
    )
    if None not in opened_identity and None not in current_identity:
        return opened_identity == current_identity
    return True


def _error_excerpt_from_tail(
    payload: bytes,
    *,
    starts_at_file_beginning: bool,
    context_lines: int,
    max_display_characters: int,
) -> ErrorLogExcerpt | None:
    if not payload:
        return None

    ended_with_newline = payload.endswith((b"\n", b"\r"))
    text = payload.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not starts_at_file_beginning and lines:
        # A bounded tail normally starts in the middle of a physical line.
        lines = lines[1:]
    if not ended_with_newline and lines:
        # Ignore the final physical line while another thread may be writing it.
        lines = lines[:-1]
    if not lines:
        return None

    record_starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _LOG_RECORD_PATTERN.match(line)
        if match is not None:
            record_starts.append((index, match.group("level")))

    for record_index in range(len(record_starts) - 1, -1, -1):
        start, level = record_starts[record_index]
        if level != "ERROR":
            continue
        end = (
            record_starts[record_index + 1][0]
            if record_index + 1 < len(record_starts)
            else len(lines)
        )
        excerpt_lines = lines[max(0, start - context_lines) : end]
        error_line_count = end - start
        excerpt = "\n".join(excerpt_lines)
        truncated = len(excerpt) > max_display_characters
        if truncated:
            excerpt = excerpt[:max_display_characters].rstrip() + "\n…[truncated]"
        return ErrorLogExcerpt(
            text=excerpt,
            context_line_count=min(context_lines, start),
            error_line_count=error_line_count,
            truncated=truncated,
        )
    return None
