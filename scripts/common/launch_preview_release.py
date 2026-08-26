#!/usr/bin/env python3
"""Safely dispatch and watch a Preview release from protected main."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence


WORKFLOW = "preview-release-promotion.yml"
WORKFLOW_REF = "main"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _run(
    command: Sequence[str],
    *,
    capture_output: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=REPOSITORY_ROOT,
        check=check,
        capture_output=capture_output,
        text=True,
    )


def _output(command: Sequence[str]) -> str:
    return _run(command).stdout.strip()


def validate_checked_out_branch(branch: str) -> None:
    """Require the protected branch used as the Preview source."""
    if not branch or branch == "HEAD":
        raise ValueError("Cannot create a Preview release from a detached HEAD.")
    if branch != WORKFLOW_REF:
        raise ValueError(
            f"Check out {WORKFLOW_REF!r} before creating a Preview release."
        )


def select_new_workflow_run(
    previous_ids: set[int], runs: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the single newly observed workflow run, if it is unambiguous."""
    new_runs = [run for run in runs if int(run["databaseId"]) not in previous_ids]
    if not new_runs:
        return None
    if len(new_runs) > 1:
        ids = ", ".join(str(run["databaseId"]) for run in new_runs)
        raise RuntimeError(f"Multiple new Preview workflow runs appeared: {ids}")
    return new_runs[0]


def _list_runs() -> list[dict[str, Any]]:
    payload = _output(
        [
            "gh",
            "run",
            "list",
            "--workflow",
            WORKFLOW,
            "--event",
            "workflow_dispatch",
            "--limit",
            "100",
            "--json",
            "databaseId,url",
        ]
    )
    return json.loads(payload)


def _preflight() -> str:
    for executable in ("git", "gh"):
        if shutil.which(executable) is None:
            raise RuntimeError(f"Required executable is not available: {executable}")

    _run(["gh", "auth", "status"])
    branch = _output(["git", "branch", "--show-current"])
    validate_checked_out_branch(branch)

    if _output(["git", "status", "--porcelain"]):
        raise RuntimeError("Commit or stash local changes before creating a release.")

    local_sha = _output(["git", "rev-parse", "HEAD"])
    remote = _run(
        ["git", "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
        check=False,
    )
    if remote.returncode != 0 or not remote.stdout.strip():
        raise RuntimeError(
            f"Push branch {branch!r} to origin before creating a release."
        )
    remote_sha = remote.stdout.split()[0]
    if remote_sha != local_sha:
        raise RuntimeError(
            f"Local branch {branch!r} is not fully pushed to origin "
            f"({local_sha[:8]} != "
            f"{remote_sha[:8]})."
        )
    return local_sha


def _confirm(main_sha: str, notes: str, assume_yes: bool) -> None:
    print(f"Source revision: origin/{WORKFLOW_REF} at {main_sha}")
    print(f"Release notes: {notes or '(none)'}")
    print("The workflow may merge pull requests and publish a Preview release.")
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Interactive confirmation is unavailable; pass --yes explicitly."
        )
    answer = input("Type 'preview' to continue: ").strip().lower()
    if answer != "preview":
        raise RuntimeError("Preview release cancelled.")


def _wait_for_new_run(previous_ids: set[int], timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run = select_new_workflow_run(previous_ids, _list_runs())
        if run is not None:
            return run
        time.sleep(2)
    raise RuntimeError("Timed out while locating the newly dispatched workflow run.")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create and watch a Preview release from protected main."
    )
    parser.add_argument("--notes", default="", help="Optional Preview release notes")
    parser.add_argument(
        "--yes", action="store_true", help="Skip the interactive confirmation"
    )
    parser.add_argument(
        "--lookup-timeout", type=int, default=60, help=argparse.SUPPRESS
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        main_sha = _preflight()
        notes = args.notes
        if not notes and sys.stdin.isatty():
            notes = input("Preview release notes (optional): ").strip()
        _confirm(main_sha, notes, args.yes)

        previous_ids = {int(run["databaseId"]) for run in _list_runs()}
        command = [
            "gh",
            "workflow",
            "run",
            WORKFLOW,
            "--ref",
            WORKFLOW_REF,
            "-f",
            f"main_sha={main_sha}",
        ]
        if notes:
            command.extend(("-f", f"release_notes={notes}"))
        _run(command)

        run = _wait_for_new_run(previous_ids, args.lookup_timeout)
        run_id = str(run["databaseId"])
        print(f"Preview workflow: {run['url']}")
        _run(["gh", "run", "watch", run_id, "--exit-status"], capture_output=False)
        return 0
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
