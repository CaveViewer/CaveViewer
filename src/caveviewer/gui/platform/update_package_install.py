"""Verify and launch a Windows installer after explicit user consent.

The update manager owns lifecycle state; this focused platform adapter owns the
Windows-only provenance check, package-integrity verification, and detached
process handoff. Verified installers additionally require Authenticode. An
unsigned community installer is allowed only after the caller has verified the
signed update manifest that explicitly selected that policy. It never fetches
a manifest or downloads a package.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from caveviewer.core.diagnostics.logging import get_logger

from .windows_update_paths import default_windows_update_root


_LOG = get_logger("WindowsUpdateInstaller")
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_WINDOWS_INSTALLATION_KEY = r"Software\CaveViewer\Installation"
_WINDOWS_INSTALLER_CHANNEL = "windows_installer"
_WINDOWS_INSTALLER_EXE = "CaveViewer.exe"
_AUTHENTICODE_TIMEOUT_SECONDS = 20
_AUTHENTICODE_VERIFIED = "verified"
_AUTHENTICODE_UNSIGNED_COMMUNITY = "unsigned-community"
_POWERSHELL_SIGNATURE_SCRIPT = " ".join(
    (
        "$ErrorActionPreference = 'Stop';",
        "$signature = Get-AuthenticodeSignature -LiteralPath $args[0];",
        "if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {",
        "throw ('Authenticode status: ' + $signature.Status);",
        "}",
        "if ($null -eq $signature.SignerCertificate -or",
        "$signature.SignerCertificate.Subject -cne $args[1]) {",
        "throw 'Unexpected Authenticode publisher.';",
        "}",
        "if ($null -eq $signature.TimeStamperCertificate) {",
        "throw 'The Authenticode signature is missing an RFC-3161 timestamp.';",
        "}",
    )
)


def _normalized_authenticode_status(value: str | None) -> str:
    """Use the legacy signed policy when an older manifest omits the field."""
    normalized = str(value or _AUTHENTICODE_VERIFIED).strip().lower()
    return normalized or _AUTHENTICODE_VERIFIED


def _is_supported_authenticode_contract(
    authenticode_status: str | None,
    authenticode_certificate_subject: str | None,
) -> bool:
    """Return whether one explicit installer-authentication mode is coherent."""
    normalized_status = _normalized_authenticode_status(authenticode_status)
    normalized_subject = str(authenticode_certificate_subject or "").strip()
    return (
        normalized_status == _AUTHENTICODE_VERIFIED and bool(normalized_subject)
    ) or (
        normalized_status == _AUTHENTICODE_UNSIGNED_COMMUNITY
        and not normalized_subject
    )


@dataclass(frozen=True, slots=True)
class WindowsInstallerInstallation:
    """The verified per-user installation that may perform an EXE handoff."""

    payload_directory: Path
    version: str


class UpdatePackageInstallerAdapter(Protocol):
    """Narrow native boundary for an explicitly approved verified update."""

    def supports_package_kind(
        self,
        package_kind: str,
        *,
        authenticode_certificate_subject: str | None,
        authenticode_status: str | None = None,
    ) -> bool:
        """Return whether the package may use this process's install handoff."""

    def install_action_label(self) -> str:
        """Return the concise user-visible label for the install action."""

    def install_verified_package(
        self,
        payload_path: str,
        *,
        version: str,
        expected_size_bytes: int,
        expected_sha256: str,
        authenticode_certificate_subject: str | None,
        authenticode_status: str | None = None,
        parent_process_id: int,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> None:
        """Recheck and start a detached installer handoff."""


class AuthenticodeVerifier(Protocol):
    """Validate a local PE signature at the execution boundary."""

    def verify(
        self,
        artifact_path: Path,
        *,
        expected_certificate_subject: str,
    ) -> None:
        """Raise when the artifact is not timestamped and signed as expected."""


class UpdateInstallationCancelled(RuntimeError):
    """Signal that shutdown withdrew the still-pending installer handoff."""


class PowerShellAuthenticodeVerifier:
    """Use Windows' Authenticode provider without a build-only signtool dependency."""

    def verify(
        self,
        artifact_path: Path,
        *,
        expected_certificate_subject: str,
    ) -> None:
        """Require a valid chain, exact publisher, and timestamp before execution."""
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _POWERSHELL_SIGNATURE_SCRIPT,
            str(artifact_path),
            expected_certificate_subject,
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=_AUTHENTICODE_TIMEOUT_SECONDS,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(
                "The downloaded Windows installer could not be verified. "
                "The update was not started."
            ) from error
        if completed.returncode == 0:
            return

        detail = (completed.stderr or completed.stdout).strip()
        _LOG.warning(
            "Authenticode validation rejected %s: %s",
            artifact_path,
            detail or f"PowerShell exited with {completed.returncode}",
        )
        raise RuntimeError(
            "The downloaded Windows installer does not have the expected valid "
            "signature. The update was not started."
        )


class WindowsUpdatePackageInstallerAdapter:
    """Start only a rechecked EXE from the registered Windows install channel.

    ``verified`` installers must pass Authenticode validation. The explicit
    ``unsigned-community`` mode skips that platform signature only after the
    caller has cryptographically verified the release manifest and supplied
    the downloaded file's expected size and SHA-256.
    """

    def __init__(
        self,
        *,
        installation_probe: Callable[[], WindowsInstallerInstallation | None] | None = None,
        authenticode_verifier: AuthenticodeVerifier | None = None,
        process_launcher: Callable[..., object] | None = None,
        update_root: Path | str | None = None,
    ) -> None:
        self._installation_probe = installation_probe or _current_installation
        self._authenticode_verifier = authenticode_verifier or PowerShellAuthenticodeVerifier()
        self._process_launcher = process_launcher or subprocess.Popen
        self._update_root = (
            Path(update_root) if update_root is not None else default_windows_update_root()
        )

    def supports_package_kind(
        self,
        package_kind: str,
        *,
        authenticode_certificate_subject: str | None,
        authenticode_status: str | None = None,
    ) -> bool:
        """Allow automatic handoff only from the registered frozen installation."""
        return (
            str(package_kind).strip().lower() == "exe"
            and _is_supported_authenticode_contract(
                authenticode_status,
                authenticode_certificate_subject,
            )
            and self._installation_probe() is not None
        )

    def install_action_label(self) -> str:
        """Describe the single explicit update handoff."""
        return "Install and restart"

    def install_verified_package(
        self,
        payload_path: str,
        *,
        version: str,
        expected_size_bytes: int,
        expected_sha256: str,
        authenticode_certificate_subject: str | None,
        authenticode_status: str | None = None,
        parent_process_id: int,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> None:
        """Recheck the installer and start it with value-safe Windows arguments."""
        installation = self._installation_probe()
        if installation is None:
            raise RuntimeError(
                "Automatic Windows updates are available only from a CaveViewer "
                "installer installation."
            )

        if cancellation_requested is not None and cancellation_requested():
            raise UpdateInstallationCancelled("Update installation was cancelled.")

        payload = Path(payload_path).expanduser().resolve(strict=False)
        normalized_version = str(version).strip()
        normalized_sha256 = str(expected_sha256).strip().lower()
        normalized_subject = str(authenticode_certificate_subject or "").strip()
        normalized_authenticode_status = _normalized_authenticode_status(
            authenticode_status
        )
        try:
            normalized_size = int(expected_size_bytes)
            process_id = int(parent_process_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "The verified update has invalid installer metadata."
            ) from error

        if not normalized_version:
            raise RuntimeError("The verified update has no version.")
        if normalized_size <= 0:
            raise RuntimeError("The verified update has an invalid size.")
        if len(normalized_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_sha256
        ):
            raise RuntimeError("The verified update has an invalid SHA-256.")
        if (
            normalized_authenticode_status == _AUTHENTICODE_VERIFIED
            and not normalized_subject
        ):
            raise RuntimeError("The verified update has no Authenticode publisher.")
        if (
            normalized_authenticode_status == _AUTHENTICODE_UNSIGNED_COMMUNITY
            and normalized_subject
        ):
            raise RuntimeError(
                "The unsigned community update must not declare an Authenticode publisher."
            )
        if normalized_authenticode_status not in {
            _AUTHENTICODE_VERIFIED,
            _AUTHENTICODE_UNSIGNED_COMMUNITY,
        }:
            raise RuntimeError("The verified update has an unsupported installer policy.")
        if process_id <= 0:
            raise RuntimeError("The current CaveViewer process ID is invalid.")
        if payload.suffix.lower() != ".exe":
            raise RuntimeError(
                "Automatic Windows installation supports only an EXE installer."
            )
        if not payload.is_file():
            raise RuntimeError(f"The verified update package is unavailable: {payload}")
        if payload.stat().st_size != normalized_size:
            raise RuntimeError(
                "The verified update package changed after download and will not be installed."
            )
        if _sha256(payload) != normalized_sha256:
            raise RuntimeError(
                "The verified update package changed after download and will not be installed."
            )

        if normalized_authenticode_status == _AUTHENTICODE_VERIFIED:
            self._authenticode_verifier.verify(
                payload,
                expected_certificate_subject=normalized_subject,
            )
        if cancellation_requested is not None and cancellation_requested():
            raise UpdateInstallationCancelled("Update installation was cancelled.")
        self._update_root.mkdir(parents=True, exist_ok=True)
        log_path = self._update_root / f"installer-{_safe_version(normalized_version)}.log"
        # The in-app action is the user's consent boundary. Suppress Inno's
        # own message boxes so the normal handoff needs no second confirmation;
        # Windows-controlled trust prompts remain outside this process.
        command = [
            str(payload),
            "/SP-",
            "/SILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f"/LOG={log_path}",
            "--update",
            "--wait-pid",
            str(process_id),
            "--expected-version",
            normalized_version,
        ]
        if cancellation_requested is not None and cancellation_requested():
            raise UpdateInstallationCancelled("Update installation was cancelled.")
        self._process_launcher(
            command,
            close_fds=True,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
        )


class UnsupportedUpdatePackageInstallerAdapter:
    """Fail closed on platforms or install modes without an EXE handoff contract."""

    def supports_package_kind(
        self,
        package_kind: str,
        *,
        authenticode_certificate_subject: str | None,
        authenticode_status: str | None = None,
    ) -> bool:
        """Declare that no package is executable through this adapter."""
        del package_kind, authenticode_certificate_subject, authenticode_status
        return False

    def install_action_label(self) -> str:
        """Keep a harmless fallback label for defensive callers."""
        return "Install update"

    def install_verified_package(
        self,
        payload_path: str,
        *,
        version: str,
        expected_size_bytes: int,
        expected_sha256: str,
        authenticode_certificate_subject: str | None,
        authenticode_status: str | None = None,
        parent_process_id: int,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> None:
        """Reject calls that bypassed the platform handoff contract."""
        del (
            version,
            expected_size_bytes,
            expected_sha256,
            authenticode_certificate_subject,
            authenticode_status,
            parent_process_id,
            cancellation_requested,
        )
        raise RuntimeError(
            "Installing downloaded updates is unsupported on this platform: "
            f"{payload_path}"
        )


def create_update_package_installer_adapter(
    *,
    platform_name: str | None = None,
) -> UpdatePackageInstallerAdapter:
    """Compose the Windows EXE handoff only for Windows processes."""
    normalized_platform = str(platform_name or sys.platform).strip().lower()
    if normalized_platform.startswith("win"):
        return WindowsUpdatePackageInstallerAdapter()
    return UnsupportedUpdatePackageInstallerAdapter()


def _current_installation() -> WindowsInstallerInstallation | None:
    """Read the Inno-owned marker and match it to this frozen executable."""
    if not sys.platform.startswith("win") or not getattr(sys, "frozen", False):
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_INSTALLATION_KEY) as key:
            channel, _ = winreg.QueryValueEx(key, "Channel")
            payload_directory, _ = winreg.QueryValueEx(key, "PayloadDirectory")
            version, _ = winreg.QueryValueEx(key, "Version")
    except (ImportError, OSError):
        return None

    if str(channel).strip().lower() != _WINDOWS_INSTALLER_CHANNEL:
        return None
    payload = Path(str(payload_directory)).expanduser().resolve(strict=False)
    expected_executable = payload / _WINDOWS_INSTALLER_EXE
    current_executable = Path(sys.executable).resolve(strict=False)
    if not expected_executable.is_file() or not _same_windows_path(
        current_executable, expected_executable
    ):
        return None
    normalized_version = str(version).strip()
    if not normalized_version:
        return None
    return WindowsInstallerInstallation(
        payload_directory=payload,
        version=normalized_version,
    )


def _same_windows_path(left: Path, right: Path) -> bool:
    """Compare resolved Windows paths without assuming their separator spelling."""
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
        os.path.normpath(str(right))
    )


def _sha256(path: Path) -> str:
    """Return the complete SHA-256 for a local installer at execution time."""
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_version(version: str) -> str:
    """Keep a manifest version usable as a local log basename."""
    return "".join(
        character
        if character.isascii() and (character.isalnum() or character in "._-")
        else "_"
        for character in version
    )
