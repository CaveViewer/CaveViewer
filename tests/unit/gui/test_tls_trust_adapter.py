"""Test the focused platform adapter for TLS trust augmentation."""

from __future__ import annotations

from caveviewer.gui.platform.tls_trust import create_tls_trust_adapter


class FakePlatformAdapter:
    def __init__(self):
        self.contexts = []

    def load_system_certificates(self, context):
        self.contexts.append(context)


def test_composed_tls_trust_adapter_delegates_context_augmentation():
    platform_adapter = FakePlatformAdapter()
    tls_trust_adapter = create_tls_trust_adapter(platform_adapter)
    context = object()

    tls_trust_adapter.augment_ssl_context(context)

    assert platform_adapter.contexts == [context]
