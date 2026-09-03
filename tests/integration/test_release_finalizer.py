"""Integration coverage for the single-writer release finalizer."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
requires_executable_shell_scripts = pytest.mark.skipif(
    os.name == "nt",
    reason="release finalizer shell integration is exercised by Unix CI jobs",
)
pytestmark = requires_executable_shell_scripts


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
        "scripts/common/verify_release_asset.py",
        "scripts/common/verify_release_channel.py",
        "scripts/common/version.sh",
        "scripts/linux/common/update_manifest.sh",
        "scripts/macos/architecture.sh",
        "scripts/macos/update_manifest.sh",
        "scripts/sign_update_manifest.py",
        "scripts/write_update_manifest.py",
        "scripts/windows/update_manifest.sh",
        "scripts/windows/verify_package_metadata.py",
        "packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml",
        "src/caveviewer/version.py",
    )
    for relative_path in release_files:
        source = REPOSITORY_ROOT / relative_path
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for manifest_directory in (
        "updates/windows",
        "updates/linux/x86_64",
        "updates/macos/arm64",
        "updates/macos/x86_64",
    ):
        (destination / manifest_directory).mkdir(parents=True, exist_ok=True)


def _write_release_artifacts(
    artifacts_dir: Path,
    version: str,
    *,
    community_windows: bool = False,
    release_channel: str = "stable",
) -> None:
    artifact_names = (
        f"CaveViewer-{version}-windows.exe",
        f"CaveViewer-{version}-x86_64.AppImage",
        f"CaveViewer-{version}-macos-arm64.dmg",
        f"CaveViewer-{version}-macos-x86_64.dmg",
    )
    artifacts_dir.mkdir()
    for index, artifact_name in enumerate(artifact_names):
        (artifacts_dir / artifact_name).write_bytes(
            f"artifact-{index}-{artifact_name}\n".encode("utf-8")
        )

    windows_artifact = artifacts_dir / f"CaveViewer-{version}-windows.exe"
    windows_metadata = {
        "artifact_file": windows_artifact.name,
        "authenticode_required": not community_windows,
        "authenticode_status": (
            "unsigned-community" if community_windows else "verified"
        ),
        "entrypoint": "CaveViewerSetup.exe",
        "package_type": (
            "windows_community_installer"
            if community_windows
            else "windows_signed_installer"
        ),
        "sha256": hashlib.sha256(windows_artifact.read_bytes()).hexdigest(),
        "size_bytes": windows_artifact.stat().st_size,
        "release_channel": release_channel,
        "version": version,
    }
    if not community_windows:
        windows_metadata["authenticode_certificate_subject"] = (
            "CN=CaveViewer Test Publisher"
        )
    (artifacts_dir / f"CaveViewer-{version}.json").write_text(
        json.dumps(windows_metadata, sort_keys=True),
        encoding="utf-8",
    )
    windows_update_metadata = {
        "authenticode_required": not community_windows,
        "authenticode_status": (
            "unsigned-community" if community_windows else "verified"
        ),
        "download_size_bytes": windows_artifact.stat().st_size,
        "download_size_bytes_windows_exe": windows_artifact.stat().st_size,
        "download_url": "",
        "download_url_windows_exe": "",
        "install_channel": "windows_installer",
        "latest_version": version,
        "release_channel": release_channel,
        "sha256": hashlib.sha256(windows_artifact.read_bytes()).hexdigest(),
        "sha256_windows_exe": hashlib.sha256(windows_artifact.read_bytes()).hexdigest(),
    }
    if not community_windows:
        windows_update_metadata["authenticode_certificate_subject"] = (
            "CN=CaveViewer Test Publisher"
        )
    (artifacts_dir / f"CaveViewer-{version}.update.json").write_text(
        json.dumps(windows_update_metadata, sort_keys=True),
        encoding="utf-8",
    )
    for architecture in ("arm64", "x86_64"):
        (artifacts_dir / f"CaveViewer-{version}-macos-{architecture}.json").write_text(
            json.dumps({"release_channel": release_channel}, sort_keys=True),
            encoding="utf-8",
        )
    (artifacts_dir / f"CaveViewer-{version}-linux-x86_64.json").write_text(
        json.dumps({"release_channel": release_channel}, sort_keys=True),
        encoding="utf-8",
    )


def _write_release_api_response(
    destination: Path,
    artifacts_dir: Path,
    version: str,
    *,
    corrupt_digest: bool = False,
) -> None:
    """Write the GitHub API shape returned after publishable assets upload."""
    excluded_linux_metadata = f"CaveViewer-{version}-linux-x86_64.json"
    assets = []
    for artifact in sorted(artifacts_dir.iterdir()):
        if artifact.name == excluded_linux_metadata:
            continue
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if corrupt_digest and not assets:
            digest = "0" * 64
        assets.append(
            {
                "browser_download_url": (
                    "https://github.com/example/CaveViewer/releases/download/"
                    f"v{version}/{artifact.name}"
                ),
                "digest": f"sha256:{digest}",
                "name": artifact.name,
                "size": artifact.stat().st_size,
                "state": "uploaded",
            }
        )
    destination.write_text(
        json.dumps(
            {
                "assets": assets,
                "draft": False,
                "prerelease": False,
                "tag_name": f"v{version}",
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("argument", "expected_error"),
    [
        ("--version=1.2", "exactly three numeric components"),
        ("--version=v1.2.3", "exactly three numeric components"),
        ("--target-branch=main", "only to release/next"),
        ("--expected-source-sha=deadbeef", "full lowercase 40-character"),
        (f"--expected-source-sha={'A' * 40}", "full lowercase 40-character"),
    ],
)
def test_finalizer_rejects_malformed_boundary_inputs_before_authentication(
    tmp_path: Path,
    argument: str,
    expected_error: str,
):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    arguments = {
        "version": "--version=1.2.3",
        "target-branch": "--target-branch=release/next",
        "expected-source-sha": f"--expected-source-sha={'d' * 40}",
    }
    argument_name = argument[2:].split("=", 1)[0]
    arguments[argument_name] = argument

    completed = subprocess.run(
        [
            str(REPOSITORY_ROOT / "scripts/common/finalize_release.sh"),
            "--platforms=windows",
            arguments["version"],
            "--notes=Boundary validation",
            f"--artifacts-dir={artifacts_dir}",
            arguments["target-branch"],
            arguments["expected-source-sha"],
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert expected_error in completed.stdout


@pytest.mark.parametrize("foreign_kind", ("file", "symlink"))
def test_finalizer_rejects_foreign_or_linked_artifacts(
    tmp_path: Path,
    foreign_kind: str,
):
    artifacts_dir = tmp_path / "artifacts"
    fake_bin = tmp_path / "bin"
    gh_log = tmp_path / "gh.log"
    _write_release_artifacts(artifacts_dir, "9.9.9")
    foreign_path = artifacts_dir / "CaveViewer-9.9.9-foreign.bin"
    if foreign_kind == "file":
        foreign_path.write_bytes(b"foreign")
        expected_error = "unexpected or foreign release artifact"
    else:
        foreign_path.symlink_to(artifacts_dir / "CaveViewer-9.9.9-windows.exe")
        expected_error = "must not contain symbolic links"

    fake_bin.mkdir()
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

    completed = subprocess.run(
        [
            str(REPOSITORY_ROOT / "scripts/common/finalize_release.sh"),
            "--platforms=all",
            "--version=9.9.9",
            "--notes=Foreign artifact",
            f"--artifacts-dir={artifacts_dir}",
            "--target-branch=release/next",
            f"--expected-source-sha={'d' * 40}",
        ],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert expected_error in completed.stdout


def test_finalizer_rejects_incomplete_artifacts_before_creating_a_release(
    tmp_path: Path,
):
    artifacts_dir = tmp_path / "artifacts"
    fake_bin = tmp_path / "bin"
    gh_log = tmp_path / "gh.log"
    artifacts_dir.mkdir()
    fake_bin.mkdir()
    (artifacts_dir / "CaveViewer-9.9.9-windows.exe").write_bytes(b"installer")

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
            "--target-branch=release/next",
            f"--expected-source-sha={'d' * 40}",
        ],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "CaveViewer-9.9.9.json" in completed.stderr
    assert gh_log.read_text(encoding="utf-8").splitlines() == ["auth status"]


def test_finalizer_rejects_a_package_channel_mismatch_before_publication(
    tmp_path: Path,
):
    working_repository = tmp_path / "working"
    artifacts_dir = tmp_path / "artifacts"
    fake_bin = tmp_path / "bin"
    gh_log = tmp_path / "gh.log"
    private_key_path = tmp_path / "release-key.pem"
    version = "9.9.9"

    working_repository.mkdir()
    _copy_release_files(working_repository)
    artifacts_dir.mkdir()
    (artifacts_dir / f"CaveViewer-{version}-x86_64.AppImage").write_bytes(
        b"Linux AppImage fixture"
    )
    (artifacts_dir / f"CaveViewer-{version}-linux-x86_64.json").write_text(
        json.dumps({"release_channel": "preview"}), encoding="utf-8"
    )

    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$GH_LOG\"\n"
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

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "GH_LOG": str(gh_log),
            "CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY": str(private_key_path),
            "CAVEVIEWER_RELEASE_SIGNING_PYTHON": sys.executable,
        }
    )
    completed = subprocess.run(
        [
            str(working_repository / "scripts/common/finalize_release.sh"),
            "--platforms=linux-x86_64",
            f"--version={version}",
            "--notes=Channel mismatch",
            f"--artifacts-dir={artifacts_dir}",
            "--target-branch=release/next",
            f"--expected-source-sha={'d' * 40}",
        ],
        cwd=working_repository,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "release_channel does not match" in completed.stderr
    assert gh_log.read_text(encoding="utf-8").splitlines() == ["auth status"]


@pytest.mark.parametrize(
    ("release_versions", "source_version", "requested_version", "expects_guard"),
    [
        (("9.9.8",), "9.9.8", "9.9.9", True),
        (("9.9.8",), "9.9.8", "9.9.8", False),
        (("9.9.9", "9.9.8"), "9.9.9", "9.9.9", True),
    ],
)
def test_finalizer_only_allows_same_version_resume_for_unmerged_release_metadata(
    tmp_path: Path,
    release_versions: tuple[str, ...],
    source_version: str,
    requested_version: str,
    expects_guard: bool,
):
    working_repository = tmp_path / "working"
    origin_repository = tmp_path / "origin.git"
    artifacts_dir = tmp_path / "artifacts"
    fake_bin = tmp_path / "bin"
    gh_log = tmp_path / "gh.log"
    private_key_path = tmp_path / "release-key.pem"

    working_repository.mkdir()
    _copy_release_files(working_repository)
    _write_release_artifacts(artifacts_dir, requested_version)

    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$GH_LOG\"\n"
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

    _run("git", "checkout", "-b", "release/next", cwd=working_repository)
    metainfo_path = (
        working_repository
        / "packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml"
    )
    release_entries = "".join(
        f'    <release version="{release_version}" date="2026-01-01" />\n'
        for release_version in release_versions
    )
    metainfo_path.write_text(
        metainfo_path.read_text(encoding="utf-8").replace(
            "  <releases>\n",
            f"  <releases>\n{release_entries}",
            1,
        ),
        encoding="utf-8",
    )
    version_path = working_repository / "src/caveviewer/version.py"
    version_path.write_text(
        re.sub(
            r'^APP_VERSION = "[^"]+"$',
            f'APP_VERSION = "{source_version}"',
            version_path.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        ),
        encoding="utf-8",
    )
    _run(
        "git",
        "add",
        str(metainfo_path.relative_to(working_repository)),
        str(version_path.relative_to(working_repository)),
        cwd=working_repository,
    )
    _run(
        "git",
        "commit",
        "-m",
        f"Release v{source_version} preview",
        cwd=working_repository,
    )
    _run("git", "push", "-u", "origin", "release/next", cwd=working_repository)
    source_sha = _run("git", "rev-parse", "HEAD", cwd=working_repository)
    _run("git", "checkout", "--detach", source_sha, cwd=working_repository)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "GH_LOG": str(gh_log),
            "CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY": str(private_key_path),
            "CAVEVIEWER_RELEASE_SIGNING_PYTHON": sys.executable,
        }
    )
    completed = subprocess.run(
        [
            str(working_repository / "scripts/common/finalize_release.sh"),
            "--platforms=all",
            f"--version={requested_version}",
            "--notes=Blocked release",
            f"--artifacts-dir={artifacts_dir}",
            "--target-branch=release/next",
            f"--expected-source-sha={source_sha}",
        ],
        cwd=working_repository,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    if expects_guard:
        assert "release metadata not reconciled with origin/main" in completed.stdout
    else:
        assert "release metadata not reconciled with origin/main" not in completed.stdout
        assert "could not determine repository" in completed.stdout
    assert gh_log.read_text(encoding="utf-8").splitlines() == ["auth status"]
    assert _run(
        "git",
        "--git-dir",
        origin_repository,
        "rev-parse",
        "refs/heads/release/next",
        cwd=tmp_path,
    ) == source_sha


@pytest.mark.parametrize(
    ("community_windows", "valid_remote_assets", "preview"),
    [
        (False, True, False),
        (True, True, False),
        (False, False, False),
        (False, True, True),
    ],
)
def test_finalizer_verifies_all_assets_before_pushing_signed_metadata(
    tmp_path: Path,
    community_windows: bool,
    valid_remote_assets: bool,
    preview: bool,
):
    working_repository = tmp_path / "working"
    origin_repository = tmp_path / "origin.git"
    artifacts_dir = tmp_path / "artifacts"
    fake_bin = tmp_path / "bin"
    gh_log = tmp_path / "gh.log"
    release_json = tmp_path / "release.json"
    private_key_path = tmp_path / "release-key.pem"
    version = "9.9.9"
    release_notes = 'Parallel "release" notes\nSecond line'

    working_repository.mkdir()
    _copy_release_files(working_repository)
    _write_release_artifacts(
        artifacts_dir,
        version,
        community_windows=community_windows,
        release_channel="preview" if preview else "stable",
    )
    _write_release_api_response(
        release_json,
        artifacts_dir,
        version,
        corrupt_digest=not valid_remote_assets,
    )

    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$GH_LOG\"\n"
        "if [ \"${1:-} ${2:-}\" = \"release view\" ]; then exit 1; fi\n"
        "if [ \"${1:-}\" = \"api\" ]; then cat \"$GH_RELEASE_JSON\"; fi\n"
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
    _run(
        "git",
        "push",
        "origin",
        "refs/heads/main:refs/heads/release/next",
        cwd=working_repository,
    )
    source_sha = _run("git", "rev-parse", "HEAD", cwd=working_repository)
    _run("git", "checkout", "--detach", source_sha, cwd=working_repository)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "GH_LOG": str(gh_log),
            "GH_RELEASE_JSON": str(release_json),
            "GH_TOKEN": "test-token",
            "CAVEVIEWER_GITHUB_REPO": "example/CaveViewer",
            "CAVEVIEWER_RELEASE_SIGNING_PRIVATE_KEY": str(private_key_path),
            "CAVEVIEWER_RELEASE_SIGNING_PYTHON": sys.executable,
        }
    )
    finalizer = working_repository / "scripts" / "common" / "finalize_release.sh"

    finalizer_args: list[str | Path] = [
        finalizer,
        "--platforms=all",
        f"--version={version}",
        "--notes",
        release_notes,
        f"--artifacts-dir={artifacts_dir}",
        "--target-branch=release/next",
        f"--expected-source-sha={source_sha}",
    ]
    if community_windows:
        finalizer_args.append("--allow-unsigned-windows-community")
    if preview:
        finalizer_args.append("--preview")
    completed = subprocess.run(
        [str(arg) for arg in finalizer_args],
        cwd=working_repository,
        env=env,
        capture_output=True,
        text=True,
    )

    commands = gh_log.read_text(encoding="utf-8").splitlines()
    assert sum(command.startswith("release create ") for command in commands) == 1
    assert not any(command.startswith("release upload ") for command in commands)
    release_create = next(
        command for command in commands if command.startswith("release create ")
    )
    release_api_command = f"api repos/example/CaveViewer/releases/tags/v{version}"
    assert commands.index(release_create) < commands.index(release_api_command)
    for artifact in artifacts_dir.iterdir():
        if artifact.name == f"CaveViewer-{version}-linux-x86_64.json":
            continue
        assert str(artifact) in release_create
    assert str(artifacts_dir / f"CaveViewer-{version}-linux-x86_64.json") not in release_create

    if not valid_remote_assets:
        assert completed.returncode != 0
        assert "release asset verification failed" in completed.stderr
        assert "digest" in completed.stderr
        assert _run("git", "rev-parse", "HEAD", cwd=working_repository) == source_sha
        assert _run(
            "git",
            "--git-dir",
            origin_repository,
            "rev-parse",
            "refs/heads/main",
            cwd=tmp_path,
        ) == source_sha
        channel = "preview" if preview else "stable"
        assert not (working_repository / f"updates/windows/{channel}.json").exists()
        return

    assert completed.returncode == 0, completed.stderr

    pushed_sha = _run(
        "git",
        "--git-dir",
        origin_repository,
        "rev-parse",
        "refs/heads/release/next",
        cwd=tmp_path,
    )
    assert pushed_sha == _run("git", "rev-parse", "HEAD", cwd=working_repository)
    assert _run(
        "git",
        "--git-dir",
        origin_repository,
        "rev-parse",
        "refs/heads/main",
        cwd=tmp_path,
    ) == source_sha
    assert _run("git", "rev-list", "--count", "HEAD", cwd=working_repository) == "2"
    channel = "preview" if preview else "stable"
    assert _run("git", "log", "-1", "--pretty=%s", cwd=working_repository) == (
        f"Release v{version} {channel}"
    )

    committed_paths = set(
        _run("git", "show", "--pretty=", "--name-only", "HEAD", cwd=working_repository)
        .splitlines()
    )
    expected_manifests = {
        f"updates/windows/{channel}.json",
        f"updates/linux/x86_64/{channel}.json",
        f"updates/macos/arm64/{channel}.json",
        f"updates/macos/x86_64/{channel}.json",
    }
    assert "src/caveviewer/version.py" in committed_paths
    assert (
        "packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml"
        in committed_paths
    )
    metainfo = (
        working_repository
        / "packaging/linux/io.github.caveviewer.caveviewer.metainfo.xml"
    ).read_text(encoding="utf-8")
    assert f'<release version="{version}"' in metainfo
    for manifest_path in expected_manifests:
        assert manifest_path in committed_paths
        assert f"{manifest_path}.sig" in committed_paths

        manifest = working_repository / manifest_path
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        assert manifest_payload["release_channel"] == channel
        assert manifest_payload["release_notes"] == release_notes
        assert manifest_payload["download_url"].startswith(
            f"https://github.com/example/CaveViewer/releases/download/v{version}/"
        )
        if manifest_path == f"updates/windows/{channel}.json":
            assert manifest_payload["install_channel"] == "windows_installer"
            if community_windows:
                assert manifest_payload["authenticode_status"] == "unsigned-community"
                assert "authenticode_certificate_subject" not in manifest_payload
            else:
                assert manifest_payload["authenticode_status"] == "verified"
                assert (
                    manifest_payload["authenticode_certificate_subject"]
                    == "CN=CaveViewer Test Publisher"
                )
        assert b"\r\n" not in manifest.read_bytes()
        signature = base64.b64decode(
            manifest.with_name(f"{manifest.name}.sig")
            .read_text(encoding="ascii")
            .strip(),
            validate=True,
        )
        private_key.public_key().verify(signature, manifest.read_bytes())

    legacy_alias_roots = (
        "updates/windows",
        "updates/linux/x86_64",
        "updates/macos/arm64",
        "updates/macos/x86_64",
        "updates/macos",
    )
    for alias_root in legacy_alias_roots:
        legacy_manifest = working_repository / alias_root / "prerelease.json"
        legacy_signature = working_repository / alias_root / "prerelease.json.sig"
        if preview:
            preview_manifest = working_repository / alias_root / "preview.json"
            preview_signature = working_repository / alias_root / "preview.json.sig"
            assert legacy_manifest.read_bytes() == preview_manifest.read_bytes()
            assert legacy_signature.read_bytes() == preview_signature.read_bytes()
            assert str(legacy_manifest.relative_to(working_repository)) in committed_paths
            assert str(legacy_signature.relative_to(working_repository)) in committed_paths
        else:
            assert not legacy_manifest.exists()
            assert not legacy_signature.exists()

    assert (working_repository / f"updates/macos/{channel}.json").read_bytes() == (
        working_repository / f"updates/macos/arm64/{channel}.json"
    ).read_bytes()
    assert (working_repository / f"updates/macos/{channel}.json.sig").read_bytes() == (
        working_repository / f"updates/macos/arm64/{channel}.json.sig"
    ).read_bytes()
