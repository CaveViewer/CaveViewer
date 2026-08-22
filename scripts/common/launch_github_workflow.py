#!/usr/bin/env python3
"""Dispatch a GitHub workflow for the checked-out branch and watch its run."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_ROOT = REPOSITORY_ROOT / ".github" / "workflows"


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


def validate_workflow_name(workflow: str) -> Path:
    """Resolve a manually dispatchable workflow without allowing path escape."""
    if Path(workflow).name != workflow or not workflow.endswith((".yml", ".yaml")):
        raise ValueError(f"Invalid workflow filename: {workflow!r}")
    path = WORKFLOWS_ROOT / workflow
    if not path.is_file():
        raise ValueError(f"Workflow does not exist: {workflow}")
    if "\n  workflow_dispatch:" not in path.read_text(encoding="utf-8"):
        raise ValueError(f"Workflow is not manually dispatchable: {workflow}")
    return path


def select_new_workflow_run(
    previous_ids: set[int], runs: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the single new run, failing rather than watching the wrong run."""
    new_runs = [run for run in runs if int(run["databaseId"]) not in previous_ids]
    if not new_runs:
        return None
    if len(new_runs) > 1:
        identifiers = ", ".join(str(run["databaseId"]) for run in new_runs)
        raise RuntimeError(f"Multiple new workflow runs appeared: {identifiers}")
    return new_runs[0]


def _list_runs(workflow: str, branch: str) -> list[dict[str, Any]]:
    payload = _output(
        [
            "gh",
            "run",
            "list",
            "--workflow",
            workflow,
            "--event",
            "workflow_dispatch",
            "--branch",
            branch,
            "--limit",
            "100",
            "--json",
            "databaseId,url",
        ]
    )
    return json.loads(payload)


def _preflight(workflow: str) -> str:
    validate_workflow_name(workflow)
    for executable in ("git", "gh"):
        if shutil.which(executable) is None:
            raise RuntimeError(f"Required executable is not available: {executable}")
    _run(["gh", "auth", "status"])

    branch = _output(["git", "branch", "--show-current"])
    if not branch:
        raise RuntimeError("Cannot dispatch a workflow from a detached HEAD.")
    if _output(["git", "status", "--porcelain"]):
        raise RuntimeError("Commit or stash local changes before dispatching.")

    local_sha = _output(["git", "rev-parse", "HEAD"])
    remote = _run(
        ["git", "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
        check=False,
    )
    if remote.returncode != 0 or not remote.stdout.strip():
        raise RuntimeError(f"Push branch {branch!r} to origin before dispatching.")
    remote_sha = remote.stdout.split()[0]
    if local_sha != remote_sha:
        raise RuntimeError(
            f"Local branch {branch!r} is not fully pushed to origin "
            f"({local_sha[:8]} != {remote_sha[:8]})."
        )
    return branch


def _wait_for_new_run(
    workflow: str,
    branch: str,
    previous_ids: set[int],
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run = select_new_workflow_run(previous_ids, _list_runs(workflow, branch))
        if run is not None:
            return run
        time.sleep(2)
    raise RuntimeError("Timed out while locating the newly dispatched workflow run.")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dispatch and watch a GitHub workflow for the current branch."
    )
    parser.add_argument("--workflow", required=True, help="Workflow YAML filename")
    parser.add_argument(
        "--lookup-timeout", type=int, default=60, help=argparse.SUPPRESS
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        branch = _preflight(args.workflow)
        previous_ids = {
            int(run["databaseId"]) for run in _list_runs(args.workflow, branch)
        }
        print(f"Dispatching {args.workflow} on {branch}.")
        print("GitHub CLI will prompt for any workflow inputs.")
        _run(
            ["gh", "workflow", "run", args.workflow, "--ref", branch],
            capture_output=False,
        )
        run = _wait_for_new_run(
            args.workflow, branch, previous_ids, args.lookup_timeout
        )
        run_id = str(run["databaseId"])
        print(f"Workflow run: {run['url']}")
        _run(["gh", "run", "watch", run_id, "--exit-status"], capture_output=False)
        return 0
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
