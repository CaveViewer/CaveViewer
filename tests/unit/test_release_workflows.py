"""Repository contracts for release workflows and their coverage gates."""

import os
import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPOSITORY_ROOT / ".github" / "workflows"
RELEASE_SCRIPT = REPOSITORY_ROOT / "scripts" / "release.sh"


def test_macos_release_workflows_use_architecture_specific_contracts():
    workflow_contracts = (
        ("macos-arm64-release.yml", "macos-arm64", "macos-15"),
        ("macos-x86_64-release.yml", "macos-x86_64", "macos-15-intel"),
    )

    assert not (WORKFLOWS_DIR / "macos-release.yml").exists()
    for workflow_name, target, runner in workflow_contracts:
        workflow = (WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8")

        assert "workflow_dispatch:" in workflow
        assert "architecture:" not in workflow
        assert f"runs-on: {runner}" in workflow
        assert "uses: ./.github/workflows/tests.yml" in workflow
        assert "needs: essential-tests" in workflow
        assert f"--target={target}" in workflow
        assert "--macos-arch" not in workflow
        assert "--action=package" in workflow
        assert "uses: ./.github/workflows/finalize-release.yml" in workflow
        assert f"platforms: {target}" in workflow
        assert "source_sha: ${{ inputs.source_sha || github.sha }}" in workflow
        assert "CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY" in workflow
        assert (
            f"dist/macos/packages/CaveViewer-${{{{ inputs.version }}}}-{target}.dmg"
        ) in workflow
        assert (
            f"dist/macos/metadata/CaveViewer-${{{{ inputs.version }}}}-{target}.json"
        ) in workflow


def test_platform_release_workflows_package_immutable_source_before_finalizing():
    workflow_contracts = (
        ("linux-arm64-release.yml", "linux-arm64"),
        ("linux-x86_64-release.yml", "linux-x86_64"),
        ("macos-arm64-release.yml", "macos-arm64"),
        ("macos-x86_64-release.yml", "macos-x86_64"),
        ("windows-release.yml", "windows"),
    )

    for workflow_name, target in workflow_contracts:
        workflow = (WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8")
        assert "workflow_call:" in workflow, workflow_name
        assert "publish:" in workflow, workflow_name
        assert "pre_release:" in workflow, workflow_name
        assert "CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY:" in workflow, workflow_name
        assert "uses: ./.github/workflows/tests.yml" in workflow, workflow_name
        assert "needs: essential-tests" in workflow, workflow_name
        assert "skip_essential_tests:" in workflow, workflow_name
        assert "inputs.skip_essential_tests != true" in workflow, workflow_name
        assert "inputs.skip_essential_tests == true" in workflow, workflow_name
        assert "needs.essential-tests.result == 'success'" in workflow, workflow_name
        assert "!cancelled()" in workflow, workflow_name
        dispatch_contract = workflow.split("  workflow_call:", 1)[0]
        assert "skip_essential_tests" not in dispatch_contract, workflow_name
        assert "source_sha" not in dispatch_contract, workflow_name
        assert "ref: ${{ inputs.source_sha || github.sha }}" in workflow, workflow_name
        assert "permissions:\n      contents: read" in workflow, workflow_name
        assert f"group: caveviewer-build-{target}-" in workflow, workflow_name
        assert "--action=package" in workflow, workflow_name
        assert "--action=release" not in workflow, workflow_name
        assert "--skip-tests" in workflow, workflow_name
        assert "Install release test dependencies" not in workflow, workflow_name
        assert "uses: ./.github/workflows/finalize-release.yml" in workflow
        assert f"platforms: {target}" in workflow
        assert "if: ${{ inputs.publish" in workflow


def test_all_platform_release_workflow_builds_platforms_in_parallel_then_finalizes():
    workflow = (WORKFLOWS_DIR / "all-platform-release.yml").read_text(
        encoding="utf-8"
    )
    job_contracts = (
        ("windows", "windows-release.yml"),
        ("linux-arm64", "linux-arm64-release.yml"),
        ("linux-x86_64", "linux-x86_64-release.yml"),
        ("macos-arm64", "macos-arm64-release.yml"),
        ("macos-x86_64", "macos-x86_64-release.yml"),
    )

    assert "name: All Platform Release" in workflow
    assert "workflow_dispatch:" in workflow
    assert "contents: write" in workflow
    assert "group: caveviewer-all-platform-release-${{ github.ref }}" in workflow
    assert workflow.count("uses: ./.github/workflows/tests.yml") == 1
    assert workflow.count("skip_essential_tests: true") == len(job_contracts)
    assert workflow.count("publish: false") == len(job_contracts)
    assert "secrets: inherit" not in workflow

    job_positions = []
    for index, (job_name, called_workflow) in enumerate(job_contracts):
        block_start = workflow.index(f"  {job_name}:\n")
        block_end = (
            workflow.index(f"  {job_contracts[index + 1][0]}:\n")
            if index + 1 < len(job_contracts)
            else workflow.index("  finalize-release:\n")
        )
        job_block = workflow[block_start:block_end]
        job_positions.append(block_start)
        assert f"uses: ./.github/workflows/{called_workflow}" in job_block
        assert "needs: essential-tests" in job_block
        assert "publish: false" in job_block
        assert "source_sha: ${{ github.sha }}" in job_block

    assert job_positions == sorted(job_positions)
    for input_name in ("version", "release_notes", "pre_release"):
        forwarded_input = f"{input_name}: ${{{{ inputs.{input_name} }}}}"
        assert workflow.count(forwarded_input) == len(job_contracts) + 1

    finalizer = workflow[workflow.index("  finalize-release:\n") :]
    assert "uses: ./.github/workflows/finalize-release.yml" in finalizer
    assert "platforms: all" in finalizer
    assert "source_sha: ${{ github.sha }}" in finalizer
    assert "target_branch: ${{ github.ref_name }}" in finalizer
    assert "inputs.publish && !cancelled()" in finalizer
    for job_name, _called_workflow in job_contracts:
        assert f"      - {job_name}\n" in finalizer


def test_release_finalizer_is_the_single_shared_state_writer():
    workflow = (WORKFLOWS_DIR / "finalize-release.yml").read_text(encoding="utf-8")
    finalizer = (
        REPOSITORY_ROOT / "scripts" / "common" / "finalize_release.sh"
    ).read_text(encoding="utf-8")

    assert "contents: write" in workflow
    assert "group: caveviewer-publish-${{ github.ref }}" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "merge-multiple: true" in workflow
    assert "CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY" in workflow
    assert "./scripts/common/finalize_release.sh" in workflow

    assert finalizer.count("gh release create") == 1
    assert finalizer.count('git -C "$repo_root" push') == 1
    assert "origin/$target_branch moved" in finalizer
    assert 'git -C "$repo_root" commit -m "Release $tag $manifest_channel"' in finalizer
    for manifest_path in (
        "updates/windows/$manifest_channel.json",
        "updates/linux/arm64/$manifest_channel.json",
        "updates/linux/x86_64/$manifest_channel.json",
        "updates/macos/arm64/$manifest_channel.json",
        "updates/macos/x86_64/$manifest_channel.json",
    ):
        assert manifest_path in finalizer


def test_release_finalizer_help_and_shell_syntax():
    finalizer = REPOSITORY_ROOT / "scripts" / "common" / "finalize_release.sh"

    syntax = subprocess.run(["bash", "-n", str(finalizer)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    help_result = subprocess.run(
        [str(finalizer), "--help"], capture_output=True, text=True
    )
    assert help_result.returncode == 0
    assert "--expected-source-sha=<sha>" in help_result.stdout
    assert "Single-writer" not in help_result.stdout


def test_release_dispatcher_exposes_architecture_specific_macos_targets():
    completed = subprocess.run(
        [str(RELEASE_SCRIPT), "--help"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "macos-arm64" in completed.stdout
    assert "macos-x86_64" in completed.stdout
    assert "macos-15" not in completed.stdout
    assert "--macos-arch" not in completed.stdout

    for target in ("macos-arm64", "macos-x86_64"):
        target_help = subprocess.run(
            [str(RELEASE_SCRIPT), f"--target={target}", "--help"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
        )
        assert target_help.returncode == 0
        assert f"--target={target}" in target_help.stdout


def test_release_dispatcher_rejects_legacy_macos_arch_options():
    for legacy_option in ("--macos-arch=arm64", "--mac-arch=arm64"):
        completed = subprocess.run(
            [
                str(RELEASE_SCRIPT),
                "--target=macos-arm64",
                legacy_option,
                "--help",
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 1
        assert f"Error: unknown option '{legacy_option}'" in completed.stdout
        assert "Use --target=macos-arm64 or --target=macos-x86_64." in completed.stdout

    legacy_target = subprocess.run(
        [str(RELEASE_SCRIPT), "--target=macos-15", "--help"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
    )
    assert legacy_target.returncode == 1
    assert "Error: unknown target 'macos-15'" in legacy_target.stdout


def test_release_dispatcher_rejects_both_macos_architectures_together():
    completed = subprocess.run(
        [
            str(RELEASE_SCRIPT),
            "--target=macos-arm64,macos-x86_64",
            "--version=1.0.63",
            "--notes=test",
            "--action=build",
            "--skip-tests",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "macos-arm64 and macos-x86_64 cannot be selected together" in completed.stdout


def test_all_target_takes_precedence_over_macos_architecture_conflict():
    completed = subprocess.run(
        [
            str(RELEASE_SCRIPT),
            "--target=macos-arm64,macos-x86_64,all",
            "--version=1.0.63",
            "--notes=test",
            "--action=unsupported",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "Error: unknown action 'unsupported'" in completed.stdout
    assert "cannot be selected together" not in completed.stdout


def test_release_dispatcher_runs_complete_suite_before_version_mutation():
    release_script = (REPOSITORY_ROOT / "scripts" / "release.sh").read_text(
        encoding="utf-8"
    )

    assert "run_all_tests()" in release_script
    assert 'CAVEVIEWER_TEST_PYTHON' in release_script
    assert '"$test_python" -m pytest -p no:cacheprovider -q' in release_script
    assert "--skip-tests)" in release_script
    assert "if $skip_tests; then" in release_script
    assert release_script.index("run_all_tests\n") < release_script.index(
        'current_version="$(cv_read_app_version'
    )


def test_failed_release_test_gate_does_not_change_version(tmp_path):
    fake_python = tmp_path / "failing-python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = \"-c\" ]; then exit 0; fi\n"
        "exit 17\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    version_file = REPOSITORY_ROOT / "src" / "caveviewer" / "version.py"
    version_before = version_file.read_bytes()
    env = os.environ.copy()
    env["CAVEVIEWER_TEST_PYTHON"] = str(fake_python)

    completed = subprocess.run(
        [
            str(REPOSITORY_ROOT / "scripts" / "release.sh"),
            "--target=windows",
            "--version=9.9.9",
            "--notes=test gate failure",
            "--action=build",
        ],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 17
    assert "Running complete release test gate" in completed.stdout
    assert version_file.read_bytes() == version_before


def test_essential_workflow_enforces_module_coverage_floors():
    workflow = (WORKFLOWS_DIR / "tests.yml").read_text(encoding="utf-8")

    assert (
        "--include=src/caveviewer/app.py\n"
        "          --fail-under=90"
    ) in workflow
    assert (
        "--include=src/caveviewer/core/chunker.py\n"
        "          --fail-under=90"
    ) in workflow
