"""Validate the one-click Preview release promotion contract."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMMON_SCRIPTS = REPOSITORY_ROOT / "scripts" / "common"
PROMOTION_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "preview-release-promotion.yml"
)
requires_executable_shell_scripts = pytest.mark.skipif(
    os.name == "nt",
    reason="Preview release shell helpers are executed by Unix CI jobs",
)


def _load_version_module():
    path = COMMON_SCRIPTS / "next_release_version.py"
    spec = importlib.util.spec_from_file_location("next_release_version", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_launcher_module():
    path = COMMON_SCRIPTS / "launch_preview_release.py"
    spec = importlib.util.spec_from_file_location("launch_preview_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_generic_launcher_module():
    path = COMMON_SCRIPTS / "launch_github_workflow.py"
    spec = importlib.util.spec_from_file_location("launch_github_workflow", path)
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


@pytest.mark.parametrize("branch", ("", "HEAD", "main", "release/next"))
def test_shared_preview_launcher_rejects_unsafe_source_branches(branch):
    launcher = _load_launcher_module()

    with pytest.raises(ValueError):
        launcher.validate_source_branch_name(branch)


def test_shared_preview_launcher_resolves_only_one_new_workflow_run():
    launcher = _load_launcher_module()
    existing = {10, 11}

    assert launcher.select_new_workflow_run(existing, [{"databaseId": 11}]) is None
    assert launcher.select_new_workflow_run(
        existing, [{"databaseId": 12, "url": "https://example.test/run/12"}]
    )["databaseId"] == 12
    with pytest.raises(RuntimeError, match="Multiple new Preview workflow runs"):
        launcher.select_new_workflow_run(
            existing, [{"databaseId": 12}, {"databaseId": 13}]
        )


def test_shared_generic_launcher_accepts_only_manual_repository_workflows():
    launcher = _load_generic_launcher_module()

    assert launcher.validate_workflow_name("tests.yml").name == "tests.yml"
    with pytest.raises(ValueError, match="Invalid workflow filename"):
        launcher.validate_workflow_name("../tests.yml")
    with pytest.raises(ValueError, match="not manually dispatchable"):
        launcher.validate_workflow_name("finalize-release.yml")


def test_shared_generic_launcher_resolves_release_version_automatically():
    launcher = _load_generic_launcher_module()
    workflow = launcher.validate_workflow_name("linux-x86_64-release.yml")

    fields = launcher.resolve_dispatch_fields(
        workflow,
        [],
        input_fn=lambda _prompt: pytest.fail("version must not prompt"),
        automatic_values={"version": lambda: "1.0.90"},
    )

    assert launcher.required_dispatch_inputs(workflow) == ("version",)
    assert fields == ("version=1.0.90",)


@pytest.mark.parametrize(
    ("published_tags", "expected"),
    [
        ("1.0.88\n1.0.90", "1.0.91"),
        ("1.0.92\n1.0.91", "1.0.93"),
    ],
)
def test_shared_generic_launcher_increments_highest_published_release(
    monkeypatch, published_tags, expected
):
    launcher = _load_generic_launcher_module()
    commands = []

    def fake_output(command):
        commands.append(command)
        if command[:3] == ["gh", "repo", "view"]:
            return "CaveViewer/CaveViewer"
        return published_tags

    monkeypatch.setattr(launcher, "_output", fake_output)

    assert launcher.next_published_release_version() == expected
    assert commands[1] == [
        "gh",
        "api",
        "--paginate",
        "repos/CaveViewer/CaveViewer/releases?per_page=100",
        "--jq",
        ".[] | select(.draft == false) | .tag_name",
    ]


def test_shared_generic_launcher_preserves_explicit_workflow_fields():
    launcher = _load_generic_launcher_module()
    workflow = launcher.validate_workflow_name("linux-x86_64-release.yml")

    fields = launcher.resolve_dispatch_fields(
        workflow,
        ["version=1.0.91", "preview=true", "publish=false"],
        input_fn=lambda _prompt: pytest.fail("explicit version must not prompt"),
    )

    assert fields == (
        "version=1.0.91",
        "preview=true",
        "publish=false",
    )


def test_shared_generic_launcher_passes_resolved_fields_to_gh(monkeypatch):
    launcher = _load_generic_launcher_module()
    commands = []
    monkeypatch.setattr(launcher, "_preflight", lambda _workflow: "release/next")
    monkeypatch.setattr(launcher, "_list_runs", lambda _workflow, _branch: [])
    monkeypatch.setattr(
        launcher,
        "_wait_for_new_run",
        lambda *_args: {"databaseId": 42, "url": "https://example.test/run/42"},
    )
    monkeypatch.setattr(
        launcher,
        "_run",
        lambda command, **_kwargs: commands.append(list(command)),
    )

    result = launcher.main(
        [
            "--workflow",
            "linux-x86_64-release.yml",
            "--field",
            "version=1.0.92",
        ]
    )

    assert result == 0
    assert [
        "gh",
        "workflow",
        "run",
        "linux-x86_64-release.yml",
        "--ref",
        "release/next",
        "--field",
        "version=1.0.92",
    ] in commands


def test_shared_generic_launcher_passes_automatic_version_to_gh(monkeypatch):
    launcher = _load_generic_launcher_module()
    commands = []
    monkeypatch.setattr(launcher, "_preflight", lambda _workflow: "release/next")
    monkeypatch.setattr(launcher, "_list_runs", lambda _workflow, _branch: [])
    monkeypatch.setattr(
        launcher, "next_published_release_version", lambda: "1.0.93"
    )
    monkeypatch.setattr(
        launcher,
        "_wait_for_new_run",
        lambda *_args: {"databaseId": 43, "url": "https://example.test/run/43"},
    )
    monkeypatch.setattr(
        launcher,
        "_run",
        lambda command, **_kwargs: commands.append(list(command)),
    )

    assert launcher.main(["--workflow", "linux-x86_64-release.yml"]) == 0
    assert [
        "gh",
        "workflow",
        "run",
        "linux-x86_64-release.yml",
        "--ref",
        "release/next",
        "--field",
        "version=1.0.93",
    ] in commands


@pytest.mark.parametrize("field", ("version", "=1.0.90", "version="))
def test_shared_generic_launcher_rejects_malformed_workflow_fields(field):
    launcher = _load_generic_launcher_module()
    workflow = launcher.validate_workflow_name("linux-x86_64-release.yml")

    with pytest.raises(ValueError, match="name=value"):
        launcher.resolve_dispatch_fields(workflow, [field])


def test_shared_pycharm_preview_configuration_contains_no_credentials():
    configuration = (
        REPOSITORY_ROOT / ".run" / "GitHub - Preview Release.run.xml"
    ).read_text(encoding="utf-8")

    assert "launch_preview_release.py" in configuration
    assert "$PROJECT_DIR$" in configuration
    assert "TOKEN" not in configuration.upper()
    assert str(Path.home()) not in configuration


def test_release_workflows_have_focused_shared_pycharm_actions():
    workflow_names = {
        "all-platform-release.yml",
        "linux-x86_64-release.yml",
        "macos-arm64-release.yml",
        "macos-x86_64-release.yml",
        "preview-release-promotion.yml",
        "tests.yml",
        "windows-release.yml",
    }
    configurations = sorted((REPOSITORY_ROOT / ".run").glob("GitHub - *.run.xml"))

    assert len(configurations) == len(workflow_names)
    generic_workflows = set()
    configuration_names = set()
    for path in configurations:
        root = ET.parse(path).getroot()
        configuration = root.find("configuration")
        assert configuration is not None
        name = configuration.attrib["name"]
        configuration_names.add(name)
        assert "Current Branch" not in name
        assert configuration.attrib.get("folderName") == "Release Actions"
        text = path.read_text(encoding="utf-8")
        assert "TOKEN" not in text.upper()
        assert str(Path.home()) not in text
        for option in configuration.findall("option"):
            parameters = option.attrib.get("value", "")
            if parameters.startswith("--workflow "):
                generic_workflows.add(parameters.removeprefix("--workflow "))

    assert "Preview Release" in configuration_names
    assert generic_workflows == workflow_names - {"preview-release-promotion.yml"}
    assert not any("Smoke" in name for name in configuration_names)
    assert configuration_names.isdisjoint({"Pages", "Viewer Benchmark"})


def test_release_documentation_preserves_branch_and_pycharm_policy():
    releases = (REPOSITORY_ROOT / "docs" / "development" / "releases.md").read_text(
        encoding="utf-8"
    )
    source_setup = (
        REPOSITORY_ROOT / "docs" / "development" / "source-setup.md"
    ).read_text(encoding="utf-8")

    assert "All published releases use pull-request and branch gates" in releases
    assert "Every `publish: true` release" in releases
    assert "must run from `release/next`" in releases
    assert "merge `release/next` back into protected `main`" in releases
    assert "No third-party GitHub Actions PyCharm plug-in" in source_setup
    assert "Preview Release" in source_setup
    assert "never add `GH_TOKEN`" in source_setup


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
    assert "runs-on: ubuntu-latest" in workflow
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
@requires_executable_shell_scripts
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
