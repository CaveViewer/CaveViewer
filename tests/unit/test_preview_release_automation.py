"""Validate the one-click Preview release promotion contract."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMMON_SCRIPTS = REPOSITORY_ROOT / "scripts" / "common"
PROMOTION_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "preview-release-promotion.yml"
)


def _load_version_module():
    path = COMMON_SCRIPTS / "next_release_version.py"
    spec = importlib.util.spec_from_file_location("next_release_version", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        (["1.0.89", "v1.0.91", "not-a-release"], "1.0.92"),
        (["2.4", "2.3.99"], "2.5"),
        (["1.0.009"], "1.0.10"),
    ],
)
def test_next_release_version_increments_the_greatest_numeric_candidate(
    candidates,
    expected,
):
    module = _load_version_module()

    assert module.next_release_version(candidates) == expected


def test_next_release_version_rejects_an_empty_valid_candidate_set():
    module = _load_version_module()

    with pytest.raises(ValueError, match="no valid dotted release versions"):
        module.next_release_version(["preview", "v1.0.0-rc1"])


def test_preview_promotion_workflow_is_manual_serial_and_write_scoped():
    workflow = PROMOTION_WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "source_branch:" in workflow
    assert "release_notes:" in workflow
    assert "actions: write" in workflow
    assert "contents: write" in workflow
    assert "pull-requests: write" in workflow
    assert "group: caveviewer-preview-release-promotion" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "timeout-minutes: 360" in workflow
    assert "./scripts/common/preview_release_automation.sh" in workflow


def test_preview_automation_has_one_fixed_gated_promotion_sequence():
    source = (COMMON_SCRIPTS / "preview_release_automation.sh").read_text(
        encoding="utf-8"
    )

    assert 'release_branch="release/next"' in source
    assert 'main_branch="main"' in source
    assert "contains changes not reconciled" in source
    assert '--workflow=all-platform-release.yml' in source
    assert '--field="preview=true"' in source
    assert '--field="publish=true"' in source
    assert '--field="reuse_pr_validation=true"' in source
    assert 'repos/$repo/releases?per_page=100' in source
    assert 'repos/$repo/tags?per_page=100' in source
    assert source.count("validate_pr \"") == 2
    assert source.count("merge_pr \"") == 2

    source_sync = source.index(
        'git -C "$repo_root" switch -C "$source_branch" "origin/$source_branch"'
    )
    source_merge = source.index('merge_pr "$source_pr"')
    release_sync = source.index(
        'git -C "$repo_root" merge --no-edit "origin/$main_branch"',
        source_merge,
    )
    release_dispatch = source.index("--workflow=all-platform-release.yml")
    metadata_pr = source.index('metadata_pr="$(')
    metadata_merge = source.index('merge_pr "$metadata_pr"')
    assert (
        source_sync
        < source_merge
        < release_sync
        < release_dispatch
        < metadata_pr
        < metadata_merge
    )


def test_explicit_pr_validation_preserves_required_checks_and_legacy_aliases():
    workflow = (
        REPOSITORY_ROOT / ".github" / "workflows" / "tests.yml"
    ).read_text(encoding="utf-8")

    assert workflow.count("pr_base_sha:") == 2
    assert workflow.count("pr_head_sha:") == 2
    assert "inputs.pr_head_sha != ''" in workflow
    assert '"preview" if path_channel == "prerelease" else path_channel' in workflow


@pytest.mark.parametrize(
    "script_name",
    (
        "dispatch_workflow_and_wait.sh",
        "preview_release_automation.sh",
    ),
)
def test_preview_automation_shell_helpers_have_valid_syntax_and_help(script_name):
    script = COMMON_SCRIPTS / script_name

    syntax_command = ["bash", "-n", str(script)]
    help_command = ["bash", str(script), "--help"]

    # Always select the shell explicitly. Executing a .sh path directly works
    # on Unix but raises WinError 193 on Windows before its shebang is read.
    assert syntax_command[0] == "bash"
    assert help_command[0] == "bash"

    syntax = subprocess.run(syntax_command, capture_output=True, text=True)
    help_result = subprocess.run(help_command, capture_output=True, text=True)

    assert syntax.returncode == 0, syntax.stderr
    assert help_result.returncode == 0, help_result.stderr
    assert "Usage:" in help_result.stdout
