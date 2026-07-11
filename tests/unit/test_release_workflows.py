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
        assert "release_args+=(--pre-release)" in workflow
        assert "CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY" in workflow
        assert (
            f"dist/macos/packages/CaveViewer-${{{{ inputs.version }}}}-{target}.dmg"
        ) in workflow


def test_all_release_workflows_expose_publish_and_prerelease_inputs():
    workflow_names = (
        "linux-arm64-release.yml",
        "linux-x86_64-release.yml",
        "macos-arm64-release.yml",
        "macos-x86_64-release.yml",
        "windows-release.yml",
    )

    for workflow_name in workflow_names:
        workflow = (WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8")
        assert "publish:" in workflow, workflow_name
        assert "pre_release:" in workflow, workflow_name
        assert "uses: ./.github/workflows/tests.yml" in workflow, workflow_name
        assert "needs: essential-tests" in workflow, workflow_name
        assert "--skip-tests" in workflow, workflow_name
        assert "Install release test dependencies" not in workflow, workflow_name


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


def test_essential_workflow_enforces_90_percent_chunker_coverage():
    workflow = (WORKFLOWS_DIR / "tests.yml").read_text(encoding="utf-8")

    assert (
        "--include=src/caveviewer/core/chunker.py\n"
        "          --fail-under=90"
    ) in workflow
