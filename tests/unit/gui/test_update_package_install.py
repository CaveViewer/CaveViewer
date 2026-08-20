"""Exercise the narrow signed-EXE Windows update handoff boundary."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from caveviewer.gui.platform import update_package_install
from caveviewer.gui.platform.update_package_install import (
    PowerShellAuthenticodeVerifier,
    UpdateInstallationCancelled,
    WindowsInstallerInstallation,
    WindowsUpdatePackageInstallerAdapter,
    create_update_package_installer_adapter,
)
from caveviewer.gui.platform.windows_update_paths import default_windows_update_root


class RecordingAuthenticodeVerifier:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[Path, str]] = []

    def verify(self, artifact_path: Path, *, expected_certificate_subject: str) -> None:
        self.calls.append((artifact_path, expected_certificate_subject))
        if self.error is not None:
            raise self.error


def _registered_installation(tmp_path: Path) -> WindowsInstallerInstallation:
    return WindowsInstallerInstallation(
        payload_directory=tmp_path / "installed CaveViewer",
        version="1.0.63",
    )


def _installer_artifact(tmp_path: Path) -> tuple[Path, bytes, str]:
    artifact = tmp_path / "updates & café" / "CaveViewer O'Brien Setup.exe"
    artifact.parent.mkdir()
    contents = b"verified signed installer fixture"
    artifact.write_bytes(contents)
    return artifact, contents, hashlib.sha256(contents).hexdigest()


def test_windows_adapter_rechecks_a_special_character_path_then_uses_distinct_arguments(
    tmp_path: Path,
):
    artifact, contents, digest = _installer_artifact(tmp_path)
    verifier = RecordingAuthenticodeVerifier()
    launches: list[tuple[list[str], dict[str, object]]] = []
    update_root = tmp_path / "Local AppData" / "CaveViewer" / "updates & logs"
    adapter = WindowsUpdatePackageInstallerAdapter(
        installation_probe=lambda: _registered_installation(tmp_path),
        authenticode_verifier=verifier,
        process_launcher=lambda command, **kwargs: launches.append((command, kwargs)),
        update_root=update_root,
    )

    adapter.install_verified_package(
        str(artifact),
        version="2.0.0",
        expected_size_bytes=len(contents),
        expected_sha256=digest,
        authenticode_certificate_subject="CN=CaveViewer Update Publisher",
        parent_process_id=4242,
    )

    resolved_artifact = artifact.resolve()
    assert verifier.calls == [
        (resolved_artifact, "CN=CaveViewer Update Publisher")
    ]
    assert launches == [
        (
            [
                str(resolved_artifact),
                "/SP-",
                "/SILENT",
                "/NORESTART",
                f"/LOG={update_root / 'installer-2.0.0.log'}",
                "--update",
                "--wait-pid",
                "4242",
                "--expected-version",
                "2.0.0",
            ],
            {
                "close_fds": True,
                "creationflags": (
                    update_package_install._DETACHED_PROCESS
                    | update_package_install._CREATE_NEW_PROCESS_GROUP
                ),
            },
        )
    ]


def test_windows_adapter_rejects_changed_or_unsigned_inputs_before_process_launch(
    tmp_path: Path,
):
    artifact, contents, digest = _installer_artifact(tmp_path)
    verifier = RecordingAuthenticodeVerifier()
    launches: list[object] = []
    adapter = WindowsUpdatePackageInstallerAdapter(
        installation_probe=lambda: _registered_installation(tmp_path),
        authenticode_verifier=verifier,
        process_launcher=lambda *_args, **_kwargs: launches.append(object()),
        update_root=tmp_path / "updates",
    )

    with pytest.raises(RuntimeError, match="changed after download"):
        adapter.install_verified_package(
            str(artifact),
            version="2.0.0",
            expected_size_bytes=len(contents),
            expected_sha256="0" * 64,
            authenticode_certificate_subject="CN=CaveViewer",
            parent_process_id=4242,
        )
    assert verifier.calls == []
    assert launches == []

    verifier.error = RuntimeError("unexpected publisher")
    with pytest.raises(RuntimeError, match="unexpected publisher"):
        adapter.install_verified_package(
            str(artifact),
            version="2.0.0",
            expected_size_bytes=len(contents),
            expected_sha256=digest,
            authenticode_certificate_subject="CN=CaveViewer",
            parent_process_id=4242,
        )
    assert len(verifier.calls) == 1
    assert launches == []


def test_windows_adapter_honors_shutdown_after_signature_check_before_launch(
    tmp_path: Path,
):
    artifact, contents, digest = _installer_artifact(tmp_path)
    verifier = RecordingAuthenticodeVerifier()
    launches: list[object] = []
    adapter = WindowsUpdatePackageInstallerAdapter(
        installation_probe=lambda: _registered_installation(tmp_path),
        authenticode_verifier=verifier,
        process_launcher=lambda *_args, **_kwargs: launches.append(object()),
        update_root=tmp_path / "updates",
    )

    with pytest.raises(UpdateInstallationCancelled):
        adapter.install_verified_package(
            str(artifact),
            version="2.0.0",
            expected_size_bytes=len(contents),
            expected_sha256=digest,
            authenticode_certificate_subject="CN=CaveViewer",
            parent_process_id=4242,
            cancellation_requested=lambda: bool(verifier.calls),
        )

    assert len(verifier.calls) == 1
    assert launches == []


def test_windows_adapter_requires_registered_exe_contract_and_subject(tmp_path: Path):
    adapter = WindowsUpdatePackageInstallerAdapter(
        installation_probe=lambda: None,
        update_root=tmp_path / "updates",
    )

    assert not adapter.supports_package_kind(
        "exe", authenticode_certificate_subject="CN=CaveViewer"
    )
    assert not adapter.supports_package_kind(
        "zip", authenticode_certificate_subject="CN=CaveViewer"
    )
    assert not adapter.supports_package_kind("exe", authenticode_certificate_subject=None)

    registered_adapter = WindowsUpdatePackageInstallerAdapter(
        installation_probe=lambda: _registered_installation(tmp_path),
        update_root=tmp_path / "updates",
    )
    assert registered_adapter.supports_package_kind(
        "exe", authenticode_certificate_subject="CN=CaveViewer"
    )


def test_current_installation_requires_the_marker_and_exact_frozen_executable(
    tmp_path: Path, monkeypatch
):
    payload_directory = tmp_path / "CaveViewer" / "app-2.0.0"
    payload_directory.mkdir(parents=True)
    executable = payload_directory / "CaveViewer.exe"
    executable.write_bytes(b"frozen executable")
    values = {
        "Channel": "windows_installer",
        "PayloadDirectory": str(payload_directory),
        "Version": "2.0.0",
    }

    class FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc_value, _traceback):
            return False

    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        OpenKey=lambda *_args: FakeKey(),
        QueryValueEx=lambda _key, name: (values[name], 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    installation = update_package_install._current_installation()

    assert installation == WindowsInstallerInstallation(
        payload_directory=payload_directory.resolve(),
        version="2.0.0",
    )
    values["Channel"] = "legacy_zip"
    assert update_package_install._current_installation() is None


def test_powershell_verifier_uses_a_literal_path_and_requires_timestamp(monkeypatch, tmp_path):
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(update_package_install.subprocess, "run", fake_run)
    artifact = tmp_path / "updates & café" / "CaveViewer O'Brien Setup.exe"

    PowerShellAuthenticodeVerifier().verify(
        artifact,
        expected_certificate_subject="CN=CaveViewer & Co",
    )

    command = observed["command"]
    assert command[:4] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    ]
    assert "Get-AuthenticodeSignature -LiteralPath $args[0]" in command[4]
    assert "SignerCertificate.Subject" in command[4]
    assert "TimeStamperCertificate" in command[4]
    assert command[5:] == [str(artifact), "CN=CaveViewer & Co"]
    assert observed["kwargs"] == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": update_package_install._AUTHENTICODE_TIMEOUT_SECONDS,
        "creationflags": update_package_install._CREATE_NO_WINDOW,
    }


def test_windows_update_paths_and_factory_are_platform_specific(monkeypatch, tmp_path):
    local_app_data = tmp_path / "Local App Data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    assert default_windows_update_root() == local_app_data / "CaveViewer" / "updates"
    assert isinstance(
        create_update_package_installer_adapter(platform_name="win32"),
        WindowsUpdatePackageInstallerAdapter,
    )
    assert not create_update_package_installer_adapter(
        platform_name="linux"
    ).supports_package_kind("exe", authenticode_certificate_subject="CN=CaveViewer")
