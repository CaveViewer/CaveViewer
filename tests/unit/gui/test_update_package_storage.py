"""Test the focused post-verification update-package storage adapter."""

from __future__ import annotations

from caveviewer.gui.platform.update_package_storage import (
    create_update_package_storage_adapter,
)


class FakePlatformAdapter:
    def __init__(self):
        self.persisted_payloads = []

    def persist_downloaded_payload(self, temporary_payload_path, download_url):
        self.persisted_payloads.append((temporary_payload_path, download_url))
        return "/downloads/CaveViewer-1.0.64.zip"


def test_composed_storage_adapter_delegates_verified_package_persistence():
    platform_adapter = FakePlatformAdapter()
    storage_adapter = create_update_package_storage_adapter(platform_adapter)

    final_payload_path = storage_adapter.persist_verified_package(
        "/temporary/update_payload.bin",
        "https://updates.example/CaveViewer-1.0.64.zip",
    )

    assert final_payload_path == "/downloads/CaveViewer-1.0.64.zip"
    assert platform_adapter.persisted_payloads == [
        (
            "/temporary/update_payload.bin",
            "https://updates.example/CaveViewer-1.0.64.zip",
        )
    ]
