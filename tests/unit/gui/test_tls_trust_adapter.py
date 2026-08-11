"""Test the focused platform adapter for TLS trust augmentation."""

from __future__ import annotations

import ssl

from caveviewer.gui.platform import tls_trust
from caveviewer.gui.platform.tls_trust import create_tls_trust_adapter
from caveviewer.gui.platform.windows import WindowsSplashPlatformAdapter


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


def test_make_ssl_context_uses_explicit_focused_adapter(monkeypatch):
    context = object()
    augmented_contexts = []

    class FakeTlsTrustAdapter:
        def augment_ssl_context(self, received_context):
            augmented_contexts.append(received_context)

    monkeypatch.setattr(
        tls_trust.ssl,
        "create_default_context",
        lambda: context,
    )

    result = tls_trust.make_ssl_context(
        tls_trust_adapter=FakeTlsTrustAdapter()
    )

    assert result is context
    assert augmented_contexts == [context]


def test_windows_tls_adapter_loads_usable_system_certificates(monkeypatch):
    """A bad certificate or unavailable store must not discard usable roots."""
    loaded_certificates = []
    queried_stores = []

    class FakeContext:
        def load_verify_locations(self, *, cadata):
            loaded_certificates.append(cadata)
            if cadata == b"bad certificate":
                raise ssl.SSLError("invalid certificate")

    def enum_certificates(store_name):
        queried_stores.append(store_name)
        if store_name == "ROOT":
            raise OSError("store unavailable")
        return [
            (b"usable certificate", "x509_asn", None),
            (b"ignored encoding", "pkcs_7_asn", None),
            (b"bad certificate", "x509_asn", None),
        ]

    monkeypatch.setattr(
        "caveviewer.gui.platform.windows.ssl.enum_certificates",
        enum_certificates,
        raising=False,
    )

    WindowsSplashPlatformAdapter().load_system_certificates(FakeContext())

    assert queried_stores == ["CA", "ROOT"]
    assert loaded_certificates == [b"usable certificate", b"bad certificate"]
