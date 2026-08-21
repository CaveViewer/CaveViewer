"""Regression contracts for the legacy and native Windows installer paths."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_SCRIPTS = REPOSITORY_ROOT / "scripts" / "windows"


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_windows_setup_only_accepts_an_explicit_64_bit_python_312_runtime():
    setup = _read("scripts/windows/setup.ps1")

    assert "function Get-PythonRuntimeInfo" in setup
    assert "sys.version_info[0]" in setup
    assert "$major -ne 3 -or $minor -ne 12 -or $bits -ne 64" in setup
    assert "WindowsApps" in setup
    assert "InstallAllUsers=0" in setup
    assert "PrependPath=0" in setup
    assert "InstallAllUsers=1" not in setup
    assert "PrependPath=1" not in setup
    assert "Test-PythonInstalled" not in setup


def test_windows_setup_uses_a_user_owned_explicit_runtime_for_install_and_launch():
    setup = _read("scripts/windows/setup.ps1")

    assert "LocalApplicationData" in setup
    assert '"runtime\\python312"' in setup
    assert '"-m", "venv"' in setup
    assert "$runtimeDirectory" in setup
    assert "$script:RuntimePython" in setup
    assert '& $FilePath @ArgumentList 2>&1' in setup
    assert '"-m", "pip", "--version"' in setup
    assert '"-e",' in setup
    assert "function New-CaveViewerLauncher" in setup
    assert "& $python -m caveviewer *>> $launchLogPath" in setup


def test_windows_setup_retains_diagnostics_and_exposes_noninteractive_smoke_mode():
    setup = _read("scripts/windows/setup.ps1")
    launch = _read("scripts/windows/launch.bat")

    assert "$SetupLogDirectory" in setup
    assert "Open log" in setup
    assert "CaveViewer installed-module verification" in setup
    assert "import caveviewer; from caveviewer.version" in setup
    assert "[switch]$NonInteractive" in setup
    assert "[string]$LogDirectory" in setup
    assert "--system-site-packages" in setup
    assert "--no-deps" in setup
    assert "--no-build-isolation" in setup
    assert "Noninteractive installer verification requested" in setup
    assert "Setup does not change system Python, PATH, firewall" in setup
    assert " /min " not in launch
    assert "-WindowStyle Minimized" not in launch


def test_public_windows_build_scripts_expose_help_and_reject_positional_arguments():
    bash_executable = "bash"
    if sys.platform == "win32":
        # `bash` resolves to the Windows Subsystem for Linux launcher on the
        # hosted runner. Use the Git Bash executable that the Windows package
        # workflow uses instead.
        git_bash = Path(os.environ["ProgramFiles"]) / "Git" / "bin" / "bash.exe"
        assert git_bash.is_file(), f"Git Bash is required: {git_bash}"
        bash_executable = str(git_bash)

    for script_name in ("build.sh", "package.sh"):
        script = WINDOWS_SCRIPTS / script_name
        help_result = subprocess.run(
            [bash_executable, script.name, "--help"],
            capture_output=True,
            text=True,
            check=False,
            cwd=WINDOWS_SCRIPTS,
        )
        assert help_result.returncode == 0, help_result.stderr
        assert "Usage:" in help_result.stdout

        invalid_result = subprocess.run(
            [bash_executable, script.name, "unexpected"],
            capture_output=True,
            text=True,
            check=False,
            cwd=WINDOWS_SCRIPTS,
        )
        assert invalid_result.returncode == 1
        assert "positional arguments are not supported" in invalid_result.stderr


def test_frozen_installer_pipeline_is_per_user_and_fails_closed_without_signing():
    builder = (WINDOWS_SCRIPTS / "build.sh").read_text(encoding="utf-8")
    packager = (WINDOWS_SCRIPTS / "package.sh").read_text(encoding="utf-8")
    metadata_writer = (
        WINDOWS_SCRIPTS / "write_package_metadata.py"
    ).read_text(encoding="utf-8")
    installer = (
        REPOSITORY_ROOT / "packaging" / "windows" / "CaveViewerSetup.iss"
    ).read_text(encoding="utf-8")
    smoke = (WINDOWS_SCRIPTS / "smoke_installer.ps1").read_text(encoding="utf-8")

    assert "packaging/pyinstaller/CaveViewer.spec" in builder
    assert "dist/windows/app/CaveViewer" in builder
    assert "pyinstaller==6.21.0" in builder
    assert "CaveViewer.exe" in builder
    assert "CAVEVIEWER_ALLOW_UNSIGNED_WINDOWS_PACKAGE" in packager
    assert "CAVEVIEWER_WINDOWS_UNSIGNED_RELEASE" in packager
    assert "unsigned-community" in packager
    assert "production Windows packages require Authenticode signing" in packager
    assert "CAVEVIEWER_WINDOWS_SIGNING_CERTIFICATE_SUBJECT" in packager
    assert "CAVEVIEWER_WINDOWS_TIMESTAMP_URL" in packager
    assert "--authenticode-certificate-subject" in packager
    assert "CaveViewerSetup.iss" in packager
    assert "CaveViewerSetup.exe" in packager
    assert "cygpath -aw" in packager
    assert '-ArtifactPath "$(windows_path "$artifact")"' in packager
    assert '"-DAppVersion=$version"' in packager
    assert '"-DPayloadDir=$(windows_path "$payload_dir")"' in packager
    assert '"-DOutputDir=$(windows_path "$installer_dir")"' in packager
    assert '"-DSetupIconFile=$(windows_path "$icon_file")"' in packager
    assert '"-DEnableCodeSigning=1"' in packager
    assert '"-SCaveViewerSign=$inno_sign_command"' in packager
    assert '"/DAppVersion=$version"' not in packager
    assert '"/SCaveViewerSign=$inno_sign_command"' not in packager
    assert "windows_signed_installer" in metadata_writer
    assert "windows_community_installer" in metadata_writer
    assert "windows.zip" not in packager

    assert "DefaultDirName={localappdata}\\Programs\\CaveViewer" in installer
    assert "PrivilegesRequired=lowest" in installer
    assert "PrivilegesRequiredOverridesAllowed=none" not in installer
    assert "UninstallPreviousVersion" not in installer
    assert "share this installation's\n; uninstaller log" in installer
    assert '#define AppPayloadDirectory "app-" + AppVersion' in installer
    assert "--update" in installer
    assert "--wait-pid" in installer
    assert "--expected-version" in installer
    assert "MAX_PARENT_WAIT_MS = 300000" in installer
    assert "StrToIntDef(Candidate, 0)" in installer
    assert "TryStrToInt" not in installer
    assert "RecordSuccessfulInstallation" in installer
    assert "RegWriteStringValue" in installer
    assert "Software\\CaveViewer\\Installation" in installer
    assert "LaunchInstalledApplication" in installer
    assert "not IsVerificationOnly() and not IsUpdateInstall()" in installer
    assert "SignedUninstaller=yes" in installer
    assert "Get-AuthenticodeSignature -LiteralPath" in smoke
    assert "TimeStamperCertificate" in smoke
    assert "[AllowEmptyString()][string]$CertificateSubject" in smoke
    assert "[switch]$AllowUnsignedCommunity" in smoke
    assert "-ExpectedCertificateSubject is required for a signed release smoke test." in smoke
    assert "CaveViewer smoke & café O'Brien" in smoke
    assert "--expected-version" in smoke
    assert "ConvertTo-WindowsCommandLineArgument" in smoke
    assert "Start-Process -FilePath $Path -ArgumentList $commandLine -PassThru" in smoke
    assert "$process.WaitForExit($InstallerProcessWaitMilliseconds)" in smoke
    assert "$process.ExitCode" in smoke


def test_authenticode_helpers_use_certificate_store_sha256_and_timestamping():
    signer = (WINDOWS_SCRIPTS / "sign_artifact.ps1").read_text(encoding="utf-8")
    verifier = WINDOWS_SCRIPTS / "verify_signature.ps1"
    verifier_source = verifier.read_text(encoding="utf-8")

    assert "Cert:\\CurrentUser\\My" in signer
    assert "HasPrivateKey" in signer
    assert "/fd', 'SHA256" in signer
    assert "/td', 'SHA256" in signer
    assert "'/tr', $TimestampUrl" in signer
    assert "verify /pa /tw" in signer
    assert "Get-AuthenticodeSignature" in verifier_source
    assert "verify /pa /tw" in verifier_source
    assert ".pfx" not in signer.lower()
    assert "exportable" not in signer.lower()


def test_package_metadata_tracks_the_final_exe_and_blocks_unsigned_publication(
    tmp_path: Path,
):
    artifact = tmp_path / "CaveViewer-1.2.3-windows.exe"
    metadata = tmp_path / "CaveViewer-1.2.3.json"
    update_metadata = tmp_path / "CaveViewer-1.2.3.update.json"
    artifact.write_bytes(b"signed installer fixture")
    writer = WINDOWS_SCRIPTS / "write_package_metadata.py"
    verifier = WINDOWS_SCRIPTS / "verify_package_metadata.py"

    command = [
        sys.executable,
        str(writer),
        "--artifact-file",
        str(artifact),
        "--metadata-output",
        str(metadata),
        "--update-output",
        str(update_metadata),
        "--app-name",
        "CaveViewer",
        "--version",
        "1.2.3",
        "--created-at-utc",
        "2026-08-20T00:00:00Z",
        "--authenticode-status",
        "verified",
        "--authenticode-certificate-subject",
        "CN=CaveViewer Update Publisher",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr

    package_payload = json.loads(metadata.read_text(encoding="utf-8"))
    update_payload = json.loads(update_metadata.read_text(encoding="utf-8"))
    assert package_payload["artifact_file"] == artifact.name
    assert package_payload["entrypoint"] == "CaveViewerSetup.exe"
    assert package_payload["authenticode_status"] == "verified"
    assert (
        package_payload["authenticode_certificate_subject"]
        == "CN=CaveViewer Update Publisher"
    )
    assert update_payload["download_url_windows_exe"] == ""
    assert update_payload["authenticode_status"] == "verified"
    assert (
        update_payload["authenticode_certificate_subject"]
        == "CN=CaveViewer Update Publisher"
    )

    verified = subprocess.run(
        [
            sys.executable,
            str(verifier),
            "--artifact-file",
            str(artifact),
            "--metadata-file",
            str(metadata),
            "--update-metadata-file",
            str(update_metadata),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr

    command[command.index("--authenticode-status") + 1] = "unsigned-test-only"
    command[command.index("--authenticode-certificate-subject") + 1] = ""
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    rejected = subprocess.run(
        [
            sys.executable,
            str(verifier),
            "--artifact-file",
            str(artifact),
            "--metadata-file",
            str(metadata),
            "--update-metadata-file",
            str(update_metadata),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "unsigned-test-only" in rejected.stderr

    command[command.index("--authenticode-status") + 1] = "unsigned-community"
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    package_payload = json.loads(metadata.read_text(encoding="utf-8"))
    update_payload = json.loads(update_metadata.read_text(encoding="utf-8"))
    assert package_payload["package_type"] == "windows_community_installer"
    assert package_payload["authenticode_required"] is False
    assert update_payload["authenticode_required"] is False
    assert "authenticode_certificate_subject" not in package_payload
    assert "authenticode_certificate_subject" not in update_payload

    accepted = subprocess.run(
        [
            sys.executable,
            str(verifier),
            "--artifact-file",
            str(artifact),
            "--metadata-file",
            str(metadata),
            "--update-metadata-file",
            str(update_metadata),
            "--allow-unsigned-community",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
