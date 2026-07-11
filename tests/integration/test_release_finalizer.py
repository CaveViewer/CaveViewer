"""Integration coverage for the single-writer release finalizer."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str | Path, cwd: Path, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _copy_release_files(destination: Path) -> None:
    release_files = (
        "scripts/common/artifacts.sh",
        "scripts/common/finalize_release.sh",
        "scripts/common/github.sh",
        "scripts/common/version.sh",
        "scripts/linux/common/update_manifest.sh",
        "scripts/macos/architecture.sh",
        "scripts/macos/update_manifest.sh",
        "scripts/sign_update_manifest.py",
        "scripts/windows/update_manifest.sh",
        "packaging/linux/io.github.kernalpanic.caveviewer.metainfo.xml",
        "src/caveviewer/version.py",
    )
    for relative_path in release_files:
        source = REPOSITORY_ROOT / relative_path
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _write_release_artifacts(artifacts_dir: Path, version: str) -> None:
    artifact_names = (
        f"CaveViewer-{version}-windows.zip",
        f"CaveViewer-{version}.json",
        f"CaveViewer-{version}.update.json",
        f"CaveViewer-{version}-aarch64.AppImage",
        f"CaveViewer-{version}-x86_64.AppImage",
        f"CaveViewer-{version}-macos-arm64.dmg",
        f"CaveViewer-{version}-macos-arm64.json",
        f"CaveViewer-{version}-macos-x86_64.dmg",
        f"CaveViewer-{version}-macos-x86_64.json",
    )
    artifacts_dir.mkdir()
    for index, artifact_name in enumerate(artifact_names):
        (artifacts_dir / artifact_name).write_bytes(
            f"artifact-{index}-{artifact_name}\n".encode("utf-8")
        )


def test_finalizer_rejects_incomplete_artifacts_before_creating_a_release(
    tmp_path: Path,
):
    artifacts_dir = tmp_path / "artifacts"
    fake_bin = tmp_path / "bin"
    gh_log = tmp_path / "gh.log"
    artifacts_dir.mkdir()
    fake_bin.mkdir()
    (artifacts_dir / "CaveViewer-9.9.9-windows.zip").write_bytes(b"zip")

    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$GH_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "GH_LOG": str(gh_log),
            "CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY": str(tmp_path / "unused.pem"),
        }
    )
    finalizer = REPOSITORY_ROOT / "scripts" / "common" / "finalize_release.sh"

    completed = subprocess.run(
        [
            str(finalizer),
            "--platforms=windows",
            "--version=9.9.9",
            "--notes=Incomplete release",
            f"--artifacts-dir={artifacts_dir}",
            "--target-branch=main",
            "--expected-source-sha=deadbeef",
        ],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "CaveViewer-9.9.9.json" in completed.stderr
    assert gh_log.read_text(encoding="utf-8").splitlines() == ["auth status"]


def test_finalizer_publishes_all_assets_and_pushes_one_signed_metadata_commit(
    tmp_path: Path,
):
    working_repository = tmp_path / "working"
    origin_repository = tmp_path / "origin.git"
    artifacts_dir = tmp_path / "artifacts"
    fake_bin = tmp_path / "bin"
    gh_log = tmp_path / "gh.log"
    private_key_path = tmp_path / "release-key.pem"
    version = "9.9.9"

    working_repository.mkdir()
    _copy_release_files(working_repository)
    _write_release_artifacts(artifacts_dir, version)

    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$GH_LOG\"\n"
        "if [ \"${1:-} ${2:-}\" = \"release view\" ]; then exit 1; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    private_key = Ed25519PrivateKey.generate()
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    _run("git", "init", "--bare", origin_repository, cwd=tmp_path)
    _run("git", "init", "-b", "main", cwd=working_repository)
    _run("git", "config", "user.name", "Release Test", cwd=working_repository)
    _run(
        "git", "config", "user.email", "release-test@example.invalid", cwd=working_repository
    )
    _run("git", "add", ".", cwd=working_repository)
    _run("git", "commit", "-m", "Source revision", cwd=working_repository)
    _run("git", "remote", "add", "origin", origin_repository, cwd=working_repository)
    _run("git", "push", "-u", "origin", "main", cwd=working_repository)
    source_sha = _run("git", "rev-parse", "HEAD", cwd=working_repository)
    _run("git", "checkout", "--detach", source_sha, cwd=working_repository)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "GH_LOG": str(gh_log),
            "GH_TOKEN": "test-token",
            "CAVEVIEWER_GITHUB_REPO": "example/CaveViewer",
            "CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY": str(private_key_path),
            "CAVEVIEWER_RELEASE_SIGNING_PYTHON": sys.executable,
        }
    )
    finalizer = working_repository / "scripts" / "common" / "finalize_release.sh"

    _run(
        finalizer,
        "--platforms=all",
        f"--version={version}",
        "--notes=Parallel release",
        f"--artifacts-dir={artifacts_dir}",
        "--target-branch=main",
        f"--expected-source-sha={source_sha}",
        cwd=working_repository,
        env=env,
    )

    commands = gh_log.read_text(encoding="utf-8").splitlines()
    assert sum(command.startswith("release create ") for command in commands) == 1
    assert not any(command.startswith("release upload ") for command in commands)
    release_create = next(
        command for command in commands if command.startswith("release create ")
    )
    for artifact in artifacts_dir.iterdir():
        assert str(artifact) in release_create

    pushed_sha = _run(
        "git", "--git-dir", origin_repository, "rev-parse", "refs/heads/main", cwd=tmp_path
    )
    assert pushed_sha == _run("git", "rev-parse", "HEAD", cwd=working_repository)
    assert _run("git", "rev-list", "--count", "HEAD", cwd=working_repository) == "2"
    assert _run("git", "log", "-1", "--pretty=%s", cwd=working_repository) == (
        f"Release v{version} stable"
    )

    committed_paths = set(
        _run("git", "show", "--pretty=", "--name-only", "HEAD", cwd=working_repository)
        .splitlines()
    )
    expected_manifests = {
        "updates/windows/stable.json",
        "updates/linux/arm64/stable.json",
        "updates/linux/x86_64/stable.json",
        "updates/macos/arm64/stable.json",
        "updates/macos/x86_64/stable.json",
    }
    assert "src/caveviewer/version.py" in committed_paths
    assert (
        "packaging/linux/io.github.kernalpanic.caveviewer.metainfo.xml"
        in committed_paths
    )
    metainfo = (
        working_repository
        / "packaging/linux/io.github.kernalpanic.caveviewer.metainfo.xml"
    ).read_text(encoding="utf-8")
    assert f'<release version="{version}"' in metainfo
    for manifest_path in expected_manifests:
        assert manifest_path in committed_paths
        assert f"{manifest_path}.sig" in committed_paths

        manifest = working_repository / manifest_path
        signature = base64.b64decode(
            manifest.with_name(f"{manifest.name}.sig").read_text(encoding="ascii").strip(),
            validate=True,
        )
        private_key.public_key().verify(signature, manifest.read_bytes())

    assert (working_repository / "updates/macos/stable.json").read_bytes() == (
        working_repository / "updates/macos/arm64/stable.json"
    ).read_bytes()
    assert (working_repository / "updates/macos/stable.json.sig").read_bytes() == (
        working_repository / "updates/macos/arm64/stable.json.sig"
    ).read_bytes()
