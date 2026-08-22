#!/usr/bin/env python3
"""Dispatch a GitHub workflow for the checked-out branch and watch its run."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_ROOT = REPOSITORY_ROOT / ".github" / "workflows"
COMMON_SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(COMMON_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_SCRIPTS_ROOT))

from next_release_version import next_release_version  # noqa: E402


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


def required_dispatch_inputs(workflow_path: Path) -> tuple[str, ...]:
    """Return required manual-dispatch inputs from a repository workflow."""
    lines = workflow_path.read_text(encoding="utf-8").splitlines()
    in_dispatch = False
    in_inputs = False
    dispatch_indent = -1
    inputs_indent = -1
    current_input: str | None = None
    required: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if stripped == "workflow_dispatch:":
            in_dispatch = True
            dispatch_indent = indent
            continue
        if in_dispatch and indent <= dispatch_indent:
            break
        if not in_dispatch:
            continue
        if stripped == "inputs:":
            in_inputs = True
            inputs_indent = indent
            continue
        if not in_inputs:
            continue
        if indent == inputs_indent + 2 and stripped.endswith(":"):
            current_input = stripped[:-1]
            continue
        if (
            current_input is not None
            and indent > inputs_indent + 2
            and stripped == "required: true"
        ):
            required.append(current_input)
    return tuple(required)


def resolve_dispatch_fields(
    workflow_path: Path,
    supplied_fields: Sequence[str],
    *,
    input_fn: Callable[[str], str] = input,
    automatic_values: Mapping[str, Callable[[], str]] | None = None,
) -> tuple[str, ...]:
    """Validate fields and resolve required values not explicitly supplied."""
    values: dict[str, str] = {}
    for field in supplied_fields:
        name, separator, value = field.partition("=")
        name = name.strip()
        if not separator or not name or not value.strip():
            raise ValueError("--field must use a non-empty name=value")
        if name in values:
            raise ValueError(f"Workflow input {name!r} was provided more than once")
        values[name] = value.strip()
    for name in required_dispatch_inputs(workflow_path):
        if name in values:
            continue
        resolver = (automatic_values or {}).get(name)
        value = (resolver() if resolver is not None else input_fn(f"{name}: ")).strip()
        if not value:
            raise ValueError(f"Required workflow input {name!r} was not provided")
        values[name] = value
    return tuple(f"{name}={value}" for name, value in values.items())


def next_published_release_version() -> str:
    """Increment the highest stable or preview GitHub release version."""
    repository = _output(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    if not repository:
        raise RuntimeError("Could not determine the GitHub repository name.")
    tags = _output(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repository}/releases?per_page=100",
            "--jq",
            ".[] | select(.draft == false) | .tag_name",
        ]
    ).splitlines()
    try:
        return next_release_version(tags)
    except ValueError as error:
        raise RuntimeError(
            "No dotted numeric stable or preview GitHub release was found."
        ) from error


def release_dispatch_fields(
    supplied_fields: Sequence[str],
    channel: str | None,
    *,
    input_fn: Callable[[str], str] = input,
) -> tuple[str, ...]:
    """Force publication/reconciliation and select the release channel."""
    selected_channel = channel
    if selected_channel is None:
        selected_channel = input_fn(
            "Release channel [preview/stable] (preview): "
        ).strip().lower()
        if not selected_channel:
            selected_channel = "preview"
    if selected_channel not in {"stable", "preview"}:
        raise ValueError("Release channel must be 'stable' or 'preview'.")

    reserved = {"publish", "preview", "reconcile_metadata"}
    explicit_names = {field.partition("=")[0].strip() for field in supplied_fields}
    conflicts = reserved & explicit_names
    if conflicts:
        names = ", ".join(sorted(conflicts))
        raise ValueError(f"Release mode controls workflow input(s): {names}")
    return (
        *supplied_fields,
        "publish=true",
        f"preview={'true' if selected_channel == 'preview' else 'false'}",
        "reconcile_metadata=true",
    )


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
        "--release",
        action="store_true",
        help="Publish from release/next and reconcile metadata into main.",
    )
    parser.add_argument(
        "--channel",
        choices=("stable", "preview"),
        help="Release channel; release mode prompts when omitted.",
    )
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "Workflow input; repeat as needed. The release version is selected "
            "automatically unless explicitly supplied."
        ),
    )
    parser.add_argument(
        "--lookup-timeout", type=int, default=60, help=argparse.SUPPRESS
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        branch = _preflight(args.workflow)
        if args.release and branch != "release/next":
            raise RuntimeError(
                "Release actions must run from the 'release/next' branch."
            )
        workflow_path = validate_workflow_name(args.workflow)
        supplied_fields = args.field
        if args.release:
            supplied_fields = list(
                release_dispatch_fields(supplied_fields, args.channel)
            )
        elif args.channel is not None:
            raise ValueError("--channel requires --release")
        fields = resolve_dispatch_fields(
            workflow_path,
            supplied_fields,
            automatic_values={"version": next_published_release_version},
        )
        previous_ids = {
            int(run["databaseId"]) for run in _list_runs(args.workflow, branch)
        }
        print(f"Dispatching {args.workflow} on {branch}.")
        for field in fields:
            if field.startswith("version="):
                print(f"Selected release {field}.")
        command = ["gh", "workflow", "run", args.workflow, "--ref", branch]
        for field in fields:
            command.extend(("--field", field))
        _run(
            command,
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
