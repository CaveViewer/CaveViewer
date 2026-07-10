from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPOSITORY_ROOT / ".github" / "workflows"


def test_macos_release_workflow_uses_existing_release_contract():
    workflow = (WORKFLOWS_DIR / "macos-release.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "runs-on: macos-15" in workflow
    assert "uses: ./.github/workflows/tests.yml" in workflow
    assert "needs: essential-tests" in workflow
    assert "--target=macos-15" in workflow
    assert "release_args+=(--pre-release)" in workflow
    assert "CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY" in workflow
    assert "dist/macos/packages/CaveViewer-${{ inputs.version }}.dmg" in workflow


def test_all_release_workflows_expose_publish_and_prerelease_inputs():
    workflow_names = (
        "linux-arm64-release.yml",
        "linux-x86_64-release.yml",
        "macos-release.yml",
        "windows-release.yml",
    )

    for workflow_name in workflow_names:
        workflow = (WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8")
        assert "publish:" in workflow, workflow_name
        assert "pre_release:" in workflow, workflow_name
        assert "uses: ./.github/workflows/tests.yml" in workflow, workflow_name
        assert "needs: essential-tests" in workflow, workflow_name
