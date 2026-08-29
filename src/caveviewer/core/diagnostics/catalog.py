"""Discover and retain user-facing application diagnostic logs."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from caveviewer.storage_paths import resolve_application_paths


DIAGNOSTICS_DIRECTORY_NAME = "diagnostics"
SESSION_LOG_PREFIX = "viewer-session-"
STARTUP_LOG_FILENAME = "startup.log"
DEFAULT_SESSION_LOG_RETENTION = 10


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


def prune_session_logs(
    directory: str | os.PathLike[str],
    *,
    keep: int = DEFAULT_SESSION_LOG_RETENTION,
    preserve: Iterable[str | os.PathLike[str]] = (),
) -> tuple[Path, ...]:
    """Best-effort remove old session text logs and their JSONL companions."""

    retention = max(1, int(keep))
    preserved = {_normalized_path(path) for path in preserve}
    session_logs = tuple(
        path
        for path in application_logs(directory)
        if path.name.startswith(SESSION_LOG_PREFIX)
    )
    retained = set(session_logs[:retention])
    retained.update(
        path for path in session_logs if _normalized_path(path) in preserved
    )

    removed: list[Path] = []
    for path in session_logs:
        if path in retained or _normalized_path(path) in preserved:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
        companion = path.with_suffix(".jsonl")
        try:
            if not companion.is_symlink():
                companion.unlink(missing_ok=True)
        except OSError:
            pass
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
