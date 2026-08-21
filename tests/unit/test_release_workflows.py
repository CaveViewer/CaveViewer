"""Repository contracts for release workflows and their coverage gates."""

import os
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPOSITORY_ROOT / ".github" / "workflows"
RELEASE_SCRIPT = REPOSITORY_ROOT / "scripts" / "release.sh"
MACOS_DMG_SMOKE_SCRIPT = REPOSITORY_ROOT / "scripts" / "macos" / "smoke_dmg.sh"
requires_executable_shell_scripts = pytest.mark.skipif(
    os.name == "nt",
    reason="release shell scripts are executed by Unix CI jobs",
)


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

    intel_workflow = (WORKFLOWS_DIR / "macos-x86_64-release.yml").read_text(
        encoding="utf-8"
    )
    assert "cache: pip" in intel_workflow
    assert "requirements-dev.txt" in intel_workflow
    assert "Verify Intel runtime architecture" in intel_workflow
    assert 'test "$(uname -m)" = "x86_64"' in intel_workflow
    assert "Run complete Intel test suite" in intel_workflow
    assert "python -m pytest -p no:cacheprovider -q" in intel_workflow
    assert "Run Intel CLI smoke checks" in intel_workflow
    assert intel_workflow.count("if: ${{ inputs.reuse_pr_validation != true }}") == 3
    assert (
        "inputs.skip_essential_tests != true && inputs.reuse_pr_validation != true"
        in intel_workflow
    )
    assert "./scripts/macos/smoke_dmg.sh" in intel_workflow
    assert intel_workflow.index("./scripts/macos/smoke_dmg.sh") < intel_workflow.index(
        "Upload macOS x86_64 DMG for testing"
    )


def test_platform_release_workflows_package_immutable_source_before_finalizing():
    assert not (WORKFLOWS_DIR / "linux-arm64-release.yml").exists()

    workflow_contracts = (
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
        assert "reuse_pr_validation:" in workflow, workflow_name
        assert "inputs.skip_essential_tests != true" in workflow, workflow_name
        assert "inputs.reuse_pr_validation != true" in workflow, workflow_name
        assert "inputs.skip_essential_tests == true" in workflow, workflow_name
        assert "inputs.reuse_pr_validation == true" in workflow, workflow_name
        assert "needs.essential-tests.result == 'success'" in workflow, workflow_name
        assert "!cancelled()" in workflow, workflow_name
        dispatch_contract = workflow.split("  workflow_call:", 1)[0]
        assert "skip_essential_tests" not in dispatch_contract, workflow_name
        assert "source_sha" not in dispatch_contract, workflow_name
        assert "reuse_pr_validation" in dispatch_contract, workflow_name
        assert "ref: ${{ inputs.source_sha || github.sha }}" in workflow, workflow_name
        workflow_header = workflow.split("\njobs:\n", 1)[0]
        assert "permissions:\n  contents: write" in workflow_header, workflow_name
        assert workflow.count("permissions:\n      contents: read") == 2, workflow_name
        assert "permissions:\n      contents: write" in workflow, workflow_name
        assert f"group: caveviewer-build-{target}-" in workflow, workflow_name
        assert "--action=package" in workflow, workflow_name
        assert "--action=release" not in workflow, workflow_name
        assert "--skip-tests" in workflow, workflow_name
        assert "Install release test dependencies" not in workflow, workflow_name
        assert "uses: ./.github/workflows/finalize-release.yml" in workflow
        assert f"platforms: {target}" in workflow
        assert "if: ${{ inputs.publish" in workflow


def test_linux_release_workflows_build_before_packaging_on_fresh_runners():
    for workflow_name in (
        "linux-x86_64-release.yml",
    ):
        workflow = (WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8")

        assert workflow.count("--action=build") == 1, workflow_name
        assert workflow.count("--action=package") == 1, workflow_name
        assert workflow.count("--skip-tests") == 2, workflow_name
        assert workflow.index("--action=build") < workflow.index(
            "--action=package"
        ), workflow_name


def test_windows_release_workflow_builds_the_exe_on_a_signing_capable_runner():
    workflow = (WORKFLOWS_DIR / "windows-release.yml").read_text(encoding="utf-8")
    signed_artifact_condition = (
        "inputs.require_signed_installer || "
        "(inputs.publish && !inputs.allow_unsigned_windows_community)"
    )

    assert "Install Inno Setup" in workflow
    assert "choco install innosetup" in workflow
    assert "Build Windows installer" in workflow
    assert "CaveViewer-${{ inputs.version }}-windows.exe" in workflow
    assert "CaveViewer-${{ inputs.version }}-windows.zip" not in workflow
    assert "CAVEVIEWER_ALLOW_UNSIGNED_WINDOWS_PACKAGE" in workflow
    assert "CAVEVIEWER_WINDOWS_UNSIGNED_RELEASE" in workflow
    assert "CAVEVIEWER_WINDOWS_SIGNING_RUNNER" in workflow
    assert "CAVEVIEWER_WINDOWS_SIGNING_CERTIFICATE_SUBJECT" in workflow
    assert "CAVEVIEWER_WINDOWS_TIMESTAMP_URL" in workflow
    assert "require_signed_installer:" in workflow
    assert "allow_unsigned_windows_community:" in workflow
    assert signed_artifact_condition in workflow
    assert "SMOKE_UNSIGNED_COMMUNITY" in workflow
    assert "AllowUnsignedCommunity" in workflow
    assert (
        "if: ${{ inputs.publish && needs.build-windows.result == 'success' }}"
        in workflow
    )
    assert "Smoke-test Windows installer and update handoff" in workflow
    assert "smoke_installer.ps1" in workflow
    assert workflow.index("Smoke-test Windows installer and update handoff") < workflow.index(
        "Upload Windows installer for testing"
    )


def test_linux_release_workflow_smoke_tests_appimage_desktop_integration():
    workflow = (WORKFLOWS_DIR / "linux-x86_64-release.yml").read_text(
        encoding="utf-8"
    )

    assert "Install Linux desktop metadata validators" in workflow
    assert "appstream desktop-file-utils" in workflow
    assert "Smoke-test AppImage desktop integration" in workflow
    assert "APPIMAGE_EXTRACT_AND_RUN: \"1\"" in workflow
    assert "CAVEVIEWER_APPRUN_INSTALL_ONLY=1 \"$appimage\"" in workflow
    assert "CAVEVIEWER_APPRUN_UNINSTALL=1 \"$appimage\"" in workflow
    assert "desktop-file-validate \"$installed_desktop\"" in workflow
    assert "appstreamcli validate --no-net --pedantic \"$installed_metainfo\"" in workflow
    assert 'grep -F "Exec=\\"$appimage\\" %f" "$installed_desktop"' in workflow
    assert "AppRun must not assign CaveViewer as a MIME default." in workflow
    assert "AppRun uninstall left CaveViewer hicolor icons behind." in workflow
    assert workflow.index("Smoke-test AppImage desktop integration") < workflow.index(
        "Upload Linux x86_64 AppImage for testing"
    )


def test_package_smoke_workflows_are_read_only_and_non_publishing():
    workflow_contracts = (
        (
            "linux-package-smoke.yml",
            "Linux Package Smoke",
            "ubuntu-latest",
            "x86_64.AppImage",
            "--target=linux-x86_64",
            "CAVEVIEWER_APPRUN_INSTALL_ONLY=1",
        ),
        (
            "macos-arm64-package-smoke.yml",
            "macOS ARM64 Package Smoke",
            "macos-15",
            "macos-arm64.dmg",
            "--target=macos-arm64",
            "Smoke-test macOS ARM64 DMG",
        ),
        (
            "macos-x86_64-package-smoke.yml",
            "macOS x86_64 Package Smoke",
            "macos-15-intel",
            "macos-x86_64.dmg",
            "--target=macos-x86_64",
            "Smoke-test macOS x86_64 DMG",
        ),
        (
            "windows-package-smoke.yml",
            "Windows Package Smoke",
            "windows-latest",
            "windows.exe",
            "--target=windows",
            "smoke_installer.ps1",
        ),
    )

    for (
        workflow_name,
        display_name,
        runner,
        artifact_label,
        target,
        smoke_marker,
    ) in workflow_contracts:
        workflow = (WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8")
        assert f"name: {display_name}" in workflow
        assert "workflow_dispatch:" in workflow
        assert "schedule:" in workflow
        assert "pull_request:" in workflow
        assert "push:" in workflow
        assert "permissions:\n  contents: read" in workflow
        assert f"runs-on: {runner}" in workflow
        assert target in workflow
        assert "--skip-tests" in workflow
        assert "--action=release" not in workflow
        assert "finalize-release.yml" not in workflow
        assert "actions/upload-artifact@v7" in workflow
        assert smoke_marker in workflow
        assert artifact_label in workflow

    for workflow_name in (
        "macos-arm64-package-smoke.yml",
        "macos-x86_64-package-smoke.yml",
    ):
        macos_workflow = (WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8")
        assert "./scripts/macos/smoke_dmg.sh" in macos_workflow
        assert macos_workflow.index("./scripts/macos/smoke_dmg.sh") < macos_workflow.index(
            "Upload macOS"
        )

    intel_workflow = (
        WORKFLOWS_DIR / "macos-x86_64-package-smoke.yml"
    ).read_text(encoding="utf-8")
    assert "cache: pip" in intel_workflow
    assert "requirements-dev.txt" in intel_workflow
    assert "Verify Intel runtime architecture" in intel_workflow
    assert 'test "$(uname -m)" = "x86_64"' in intel_workflow
    assert "Run complete Intel test suite" in intel_workflow
    assert "python -m pytest -p no:cacheprovider -q" in intel_workflow
    assert "Run Intel CLI smoke checks" in intel_workflow


@requires_executable_shell_scripts
def test_macos_dmg_smoke_script_exposes_bounded_architecture_interface():
    assert MACOS_DMG_SMOKE_SCRIPT.is_file()
    assert os.access(MACOS_DMG_SMOKE_SCRIPT, os.X_OK)
    script_text = MACOS_DMG_SMOKE_SCRIPT.read_text(encoding="utf-8")
    assert "detach_dmg()" in script_text
    assert 'hdiutil detach "$mount_dir" -force -quiet' in script_text
    assert "unable to detach DMG mount cleanly after successful validation" in script_text
    assert 'hdiutil detach "$mount_dir" -quiet\nattached=0' not in script_text

    help_result = subprocess.run(
        [str(MACOS_DMG_SMOKE_SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "--arch=<arm64|x86_64>" in help_result.stdout
    assert "--version=<version>" in help_result.stdout

    invalid_arch = subprocess.run(
        [
            str(MACOS_DMG_SMOKE_SCRIPT),
            "--arch=unsupported",
            "--version=1.0.67",
        ],
        capture_output=True,
        text=True,
    )
    assert invalid_arch.returncode == 1
    assert "unsupported macOS architecture" in invalid_arch.stderr


def test_viewer_benchmark_workflow_compares_refs_and_uploads_artifacts():
    workflow = (WORKFLOWS_DIR / "viewer-benchmark.yml").read_text(
        encoding="utf-8"
    )

    assert "name: Viewer Benchmark" in workflow
    assert "workflow_dispatch:" in workflow
    assert "benchmark_map_url:" in workflow
    assert "threshold_config_path:" in workflow
    assert "default: benchmarks/viewer-thresholds.v1.json" in workflow
    assert "No benchmark_map_url was supplied." in workflow
    assert "ref: ${{ inputs.baseline_ref }}" in workflow
    assert "inputs.candidate_ref != '' && inputs.candidate_ref || github.sha" in workflow
    assert "python -m venv .venv-benchmark" in workflow
    assert workflow.count("caveviewer-benchmark") >= 2
    assert "set -o pipefail" in workflow
    assert "scripts/benchmark/compare_benchmark_results.py" in workflow
    assert "--thresholds \"$GITHUB_WORKSPACE/candidate/${{ inputs.threshold_config_path }}\"" in workflow
    assert "compare_args+=(--max-median-fps-drop-pct" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "benchmark-artifacts/" in workflow


def test_pages_workflow_deploys_docs_independently_from_releases():
    workflow = (WORKFLOWS_DIR / "pages.yml").read_text(encoding="utf-8")

    assert "name: Pages" in workflow
    assert "workflow_dispatch:" in workflow
    assert "push:" in workflow
    assert "branches:\n      - main" in workflow
    assert '      - "docs/**"' in workflow
    assert '      - ".github/workflows/pages.yml"' in workflow
    assert "release:" not in workflow
    assert "actions/configure-pages@v6" in workflow
    assert "actions/upload-pages-artifact@v5" in workflow
    assert "path: docs" in workflow
    assert "actions/deploy-pages@v5" in workflow
    assert "name: github-pages" in workflow
    assert "pages: write" in workflow
    assert "id-token: write" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow


def test_dependabot_updates_only_github_actions_with_bounded_pr_noise():
    config_path = REPOSITORY_ROOT / ".github" / "dependabot.yml"

    assert config_path.is_file()
    config = config_path.read_text(encoding="utf-8")

    assert 'package-ecosystem: "github-actions"' in config
    assert 'directory: "/"' in config
    assert 'interval: "weekly"' in config
    assert 'day: "tuesday"' in config
    assert 'time: "09:00"' in config
    assert 'timezone: "Europe/Zagreb"' in config
    assert "default-days: 7" in config
    assert "open-pull-requests-limit: 2" in config
    assert '- "dependencies"' in config
    assert '- "github-actions"' in config
    assert 'prefix: "ci(deps)"' in config
    assert 'package-ecosystem: "pip"' not in config


def test_all_platform_release_workflow_builds_platforms_in_parallel_then_finalizes():
    workflow = (WORKFLOWS_DIR / "all-platform-release.yml").read_text(
        encoding="utf-8"
    )
    job_contracts = (
        ("windows", "windows-release.yml"),
        ("linux-x86_64", "linux-x86_64-release.yml"),
        ("macos-arm64", "macos-arm64-release.yml"),
        ("macos-x86_64", "macos-x86_64-release.yml"),
    )

    assert "name: All Platform Release" in workflow
    assert "workflow_dispatch:" in workflow
    assert "contents: write" in workflow
    assert "group: caveviewer-all-platform-release-${{ github.ref }}" in workflow
    assert workflow.count("uses: ./.github/workflows/tests.yml") == 1
    assert "reuse_pr_validation:" in workflow
    assert "if: ${{ inputs.reuse_pr_validation != true }}" in workflow
    assert workflow.count("skip_essential_tests: true") == len(job_contracts)
    assert (
        workflow.count("reuse_pr_validation: ${{ inputs.reuse_pr_validation }}")
        == len(job_contracts)
    )
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
        assert "inputs.reuse_pr_validation == true" in job_block
        assert "permissions:\n      contents: write" in job_block
        assert "publish: false" in job_block
        assert "source_sha: ${{ github.sha }}" in job_block
        if job_name == "windows":
            assert (
                "require_signed_installer: ${{ inputs.publish && "
                "!inputs.allow_unsigned_windows_community }}"
            ) in job_block
            assert (
                "allow_unsigned_windows_community: ${{ inputs.publish && "
                "inputs.allow_unsigned_windows_community }}"
            ) in job_block
        else:
            assert "require_signed_installer:" not in job_block

    assert job_positions == sorted(job_positions)
    for input_name in ("version", "release_notes", "pre_release"):
        forwarded_input = f"{input_name}: ${{{{ inputs.{input_name} }}}}"
        assert workflow.count(forwarded_input) == len(job_contracts) + 1

    finalizer = workflow[workflow.index("  finalize-release:\n") :]
    assert "uses: ./.github/workflows/finalize-release.yml" in finalizer
    assert "platforms: all" in finalizer
    assert "source_sha: ${{ github.sha }}" in finalizer
    assert "target_branch: ${{ github.ref_name }}" in finalizer
    assert "allow_unsigned_windows_community: ${{ inputs.allow_unsigned_windows_community }}" in finalizer
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
    assert "actions/download-artifact@v8" in workflow
    assert "merge-multiple: true" in workflow
    assert "CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY" in workflow
    assert "CAVEVIEWER_GITHUB_REPO: ${{ github.repository }}" in workflow
    assert "./scripts/common/finalize_release.sh" in workflow
    assert "--allow-unsigned-windows-community" in workflow

    assert finalizer.count("gh release create") == 1
    assert finalizer.count('git -C "$repo_root" push') == 1
    assert "origin/$target_branch moved" in finalizer
    assert "--allow-unsigned-windows-community" in finalizer
    assert 'git -C "$repo_root" commit -m "Release $tag $manifest_channel"' in finalizer
    for manifest_path in (
        "updates/windows/$manifest_channel.json",
        "updates/linux/x86_64/$manifest_channel.json",
        "updates/macos/arm64/$manifest_channel.json",
        "updates/macos/x86_64/$manifest_channel.json",
    ):
        assert manifest_path in finalizer


@requires_executable_shell_scripts
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


@requires_executable_shell_scripts
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
    assert "do not use 1.0.64-rc1" in completed.stdout

    for target in ("macos-arm64", "macos-x86_64"):
        target_help = subprocess.run(
            [str(RELEASE_SCRIPT), f"--target={target}", "--help"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
        )
        assert target_help.returncode == 0
        assert f"--target={target}" in target_help.stdout


@requires_executable_shell_scripts
def test_release_dispatcher_rejects_linux_arm64_target():
    completed = subprocess.run(
        [str(RELEASE_SCRIPT), "--target=linux-arm64", "--help"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "unknown target 'linux-arm64'" in completed.stdout
    assert "linux-x86_64" in completed.stdout


@requires_executable_shell_scripts
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


@requires_executable_shell_scripts
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


@requires_executable_shell_scripts
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


@requires_executable_shell_scripts
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

    assert "paths-ignore:" in workflow
    for metadata_path in (
        '"CHANGELOG.md"',
        '"docs/**"',
        '"updates/**"',
        '"src/caveviewer/version.py"',
        '"packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml"',
    ):
        assert metadata_path in workflow

    assert "--cov=caveviewer.gui.preferences" in workflow
    assert (
        "--include=src/caveviewer/gui/preferences.py\n"
        "          --fail-under=85"
    ) in workflow
    assert (
        "--include=src/caveviewer/app.py\n"
        "          --fail-under=90"
    ) in workflow
    assert (
        "--include=src/caveviewer/core/chunking/builder.py\n"
        "          --fail-under=90"
    ) in workflow
    assert (
        "--include=src/caveviewer/gui/update_checker.py\n"
        "          --fail-under=90"
    ) in workflow


def test_essential_workflow_reuses_validation_for_release_metadata_only_prs():
    workflow = (WORKFLOWS_DIR / "tests.yml").read_text(encoding="utf-8")

    assert "classify_changes:" in workflow
    assert "Classify source changes" in workflow
    assert "github.event_name == 'pull_request'" in workflow
    assert 'git diff --name-only "$PR_BASE_SHA...$PR_HEAD_SHA"' in workflow
    assert "skip_source_tests=true" in workflow
    assert "skip_source_tests=false" in workflow
    assert "Material source changes detected; running source suites." in workflow
    for allowed_path in (
        "CHANGELOG.md",
        "docs/*",
        "src/caveviewer/version.py",
        "packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml",
        "updates/windows/*.json",
        "updates/linux/x86_64/*.json",
        "updates/macos/arm64/*.json",
        "updates/macos/x86_64/*.json",
    ):
        assert allowed_path in workflow

    assert "Validate release metadata without source suites" in workflow
    assert 'git diff --check "$PR_BASE_SHA...$PR_HEAD_SHA"' in workflow
    assert "version.py changes more than APP_VERSION; run the source suites." in workflow
    assert "AppStream metadata changes more than one prepended release entry." in workflow
    assert "Manifest changed without its signature:" in workflow
    assert 're.fullmatch(r"\\d+(?:\\.\\d+)+", current_version)' in workflow
    assert 'download_url.startswith("https://")' in workflow
    assert "Manifest release notes must be a string:" in workflow
    assert "openssl pkeyutl -verify -pubin" in workflow

    for job_name, next_job_name in (
        ("unit-tests", "coverage-and-metadata"),
        ("coverage-and-metadata", "cli-smoke"),
        ("cli-smoke", None),
    ):
        job_start = workflow.index(f"  {job_name}:\n")
        job_end = (
            workflow.index(f"\n  {next_job_name}:\n", job_start)
            if next_job_name is not None
            else None
        )
        job_block = workflow[job_start:job_end]
        assert "if: ${{ always() }}" in job_block
        assert "RUN_SOURCE_TESTS:" in job_block
        assert "Source tests not required" in job_block


def test_release_metadata_changes_do_not_start_package_smoke_workflows():
    for workflow_name in (
        "linux-package-smoke.yml",
        "macos-arm64-package-smoke.yml",
        "macos-x86_64-package-smoke.yml",
    ):
        workflow = (WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8")

        assert workflow.count('"!src/caveviewer/version.py"') == 2, workflow_name
        assert (
            workflow.count(
                '"!packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml"'
            )
            == 2
        ), workflow_name
