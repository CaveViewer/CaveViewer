"""Test direct platform TLS trust augmentation."""

from __future__ import annotations

import ssl

from caveviewer.gui.platform import tls_trust


def test_default_tls_adapter_leaves_context_unchanged():
    context = object()

    tls_trust.create_tls_trust_adapter(
        platform_name="linux"
    ).augment_ssl_context(context)


def test_make_ssl_context_uses_explicit_focused_adapter(monkeypatch):
    context = object()
    augmented_contexts = []

    class FakeTlsTrustAdapter:
        def augment_ssl_context(self, received_context):
            augmented_contexts.append(received_context)

    monkeypatch.setattr(tls_trust.ssl, "create_default_context", lambda: context)

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

    monkeypatch.setattr(tls_trust.ssl, "enum_certificates", enum_certificates, raising=False)

    tls_trust.create_tls_trust_adapter(
        platform_name="win32"
    ).augment_ssl_context(FakeContext())

    assert queried_stores == ["CA", "ROOT"]
    assert loaded_certificates == [b"usable certificate", b"bad certificate"]
