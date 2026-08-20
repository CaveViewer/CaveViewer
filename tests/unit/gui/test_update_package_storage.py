"""Test atomic promotion through the focused update-package storage adapters."""

from __future__ import annotations

from pathlib import Path

import pytest

from caveviewer.gui.platform import update_package_storage
from caveviewer.gui.platform.update_package_storage import (
    DefaultUpdatePackageStorageAdapter,
    LinuxUpdatePackageStorageAdapter,
    MacOSUpdatePackageStorageAdapter,
    WindowsUpdatePackageStorageAdapter,
    create_update_package_storage_adapter,
)


def _staged_payload(tmp_path: Path, payload: bytes = b"verified package") -> Path:
    staged_path = tmp_path / "staging" / "update_payload.bin"
    staged_path.parent.mkdir(exist_ok=True)
    staged_path.write_bytes(payload)
    return staged_path


def test_default_storage_preserves_url_name_and_collision_suffix(tmp_path):
    downloads_dir = tmp_path / "Downloads"
    downloads_dir.mkdir()
    existing_path = downloads_dir / "CaveViewer-1.0.64.zip"
    existing_path.write_bytes(b"existing package")
    staged_path = _staged_payload(tmp_path, b"new package")

    final_path = Path(
        DefaultUpdatePackageStorageAdapter(downloads_dir).persist_verified_package(
            str(staged_path),
            "https://updates.example/CaveViewer-1.0.64.zip?signature=example",
        )
    )

    assert final_path == downloads_dir / "CaveViewer-1.0.64-1.zip"
    assert existing_path.read_bytes() == b"existing package"
    assert final_path.read_bytes() == b"new package"
    assert staged_path.read_bytes() == b"new package"


def test_default_storage_preserves_generic_fallback_name(tmp_path):
    downloads_dir = tmp_path / "Downloads"
    staged_path = _staged_payload(tmp_path)

    final_path = Path(
        DefaultUpdatePackageStorageAdapter(downloads_dir).persist_verified_package(
            str(staged_path),
            None,
        )
    )

    assert final_path == downloads_dir / "CaveViewer-update.bin"
    assert final_path.read_bytes() == b"verified package"


def test_macos_storage_preserves_dmg_fallback_and_collision_suffix(tmp_path):
    downloads_dir = tmp_path / "Downloads"
    downloads_dir.mkdir()
    existing_path = downloads_dir / "CaveViewer-latest.dmg"
    existing_path.write_bytes(b"existing package")
    staged_path = _staged_payload(tmp_path, b"new package")

    final_path = Path(
        MacOSUpdatePackageStorageAdapter(downloads_dir).persist_verified_package(
            str(staged_path),
            "https://updates.example/CaveViewer-1.0.64.zip",
        )
    )

    assert final_path == downloads_dir / "CaveViewer-latest-1.dmg"
    assert existing_path.read_bytes() == b"existing package"
    assert final_path.read_bytes() == b"new package"


@pytest.mark.parametrize(
    ("platform_name", "adapter_type"),
    [
        ("darwin", MacOSUpdatePackageStorageAdapter),
        ("linux", LinuxUpdatePackageStorageAdapter),
        ("linux2", LinuxUpdatePackageStorageAdapter),
        ("win32", WindowsUpdatePackageStorageAdapter),
        ("freebsd", DefaultUpdatePackageStorageAdapter),
    ],
)
def test_storage_factory_selects_a_direct_platform_adapter(platform_name, adapter_type):
    assert isinstance(
        create_update_package_storage_adapter(platform_name=platform_name),
        adapter_type,
    )


def test_failed_atomic_promotion_preserves_existing_package_and_cleans_hidden_file(
    tmp_path, monkeypatch
):
    downloads_dir = tmp_path / "Downloads"
    downloads_dir.mkdir()
    existing_path = downloads_dir / "CaveViewer-1.0.64.zip"
    existing_path.write_bytes(b"existing package")
    staged_path = _staged_payload(tmp_path, b"new package")

    def fail_replace(source, destination):
        assert Path(source).parent == downloads_dir
        assert Path(source).name.startswith(".")
        assert Path(destination) == downloads_dir / "CaveViewer-1.0.64-1.zip"
        raise OSError("atomic publish failed")

    monkeypatch.setattr(update_package_storage.os, "replace", fail_replace)

    with pytest.raises(OSError, match="atomic publish failed"):
        DefaultUpdatePackageStorageAdapter(downloads_dir).persist_verified_package(
            str(staged_path),
            "https://updates.example/CaveViewer-1.0.64.zip",
        )

    assert existing_path.read_bytes() == b"existing package"
    assert not (downloads_dir / "CaveViewer-1.0.64-1.zip").exists()
    assert not [path for path in downloads_dir.iterdir() if path.name.startswith(".")]


def test_interrupted_copy_leaves_no_visible_final_package_or_hidden_temporary_file(
    tmp_path, monkeypatch
):
    downloads_dir = tmp_path / "Downloads"
    staged_path = _staged_payload(tmp_path)

    def interrupt_copy(source, destination, *args, **kwargs):
        destination.write(b"partial package")
        raise OSError("copy interrupted")

    monkeypatch.setattr(update_package_storage.shutil, "copyfileobj", interrupt_copy)

    with pytest.raises(OSError, match="copy interrupted"):
        DefaultUpdatePackageStorageAdapter(downloads_dir).persist_verified_package(
            str(staged_path),
            "https://updates.example/CaveViewer-1.0.64.zip",
        )

    assert not (downloads_dir / "CaveViewer-1.0.64.zip").exists()
    assert not [path for path in downloads_dir.iterdir() if path.name.startswith(".")]
    assert staged_path.read_bytes() == b"verified package"


def test_linux_storage_marks_appimage_executable_before_atomic_promotion(
    tmp_path, monkeypatch
):
    downloads_dir = tmp_path / "Downloads"
    staged_path = _staged_payload(tmp_path)
    staged_path.chmod(0o640)
    real_chmod = update_package_storage.os.chmod
    real_replace = update_package_storage.os.replace
    observed: dict[str, object] = {}

    def inspect_chmod(path, mode):
        hidden_path = Path(path)
        observed["chmod_path"] = hidden_path
        observed["chmod_mode"] = mode
        return real_chmod(path, mode)

    def inspect_replace(source, destination):
        hidden_path = Path(source)
        observed["hidden_name"] = hidden_path.name
        assert observed["chmod_path"] == hidden_path
        assert int(observed["chmod_mode"]) & 0o111 == 0o111
        assert not Path(destination).exists()
        return real_replace(source, destination)

    monkeypatch.setattr(update_package_storage.os, "chmod", inspect_chmod)
    monkeypatch.setattr(update_package_storage.os, "replace", inspect_replace)

    final_path = Path(
        LinuxUpdatePackageStorageAdapter(downloads_dir).persist_verified_package(
            str(staged_path),
            "https://updates.example/CaveViewer-1.0.64.AppImage",
        )
    )

    assert str(observed["hidden_name"]).startswith(".")
    assert final_path.read_bytes() == b"verified package"


def test_windows_storage_keeps_signed_exes_private_but_legacy_zips_visible(tmp_path):
    update_root = tmp_path / "Local App Data" / "CaveViewer" / "updates"
    downloads_dir = tmp_path / "Downloads"
    adapter = WindowsUpdatePackageStorageAdapter(
        update_root=update_root,
        downloads_dir=downloads_dir,
    )

    exe_path = Path(
        adapter.persist_verified_package(
            str(_staged_payload(tmp_path, b"signed installer")),
            "https://updates.example/CaveViewer-2.0.0-windows.exe",
        )
    )
    zip_path = Path(
        adapter.persist_verified_package(
            str(_staged_payload(tmp_path, b"legacy migration package")),
            "https://updates.example/CaveViewer-1.0.78-windows.zip",
        )
    )

    assert exe_path == update_root / "CaveViewer-2.0.0-windows.exe"
    assert exe_path.read_bytes() == b"signed installer"
    assert zip_path == downloads_dir / "CaveViewer-1.0.78-windows.zip"
    assert zip_path.read_bytes() == b"legacy migration package"
