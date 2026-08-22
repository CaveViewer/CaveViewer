"""Direct platform-specific TLS trust augmentation.

Each adapter augments a caller-owned verifying ``SSLContext`` synchronously.
It may add native trust roots but never changes hostname or certificate
verification policy.
"""

from __future__ import annotations

import ssl
import sys
from typing import Protocol


class TlsTrustAdapter(Protocol):
    """Narrow boundary for augmenting an SSL context with native trust roots."""

    def augment_ssl_context(self, context: ssl.SSLContext) -> None:
        """Add any platform-owned trust roots without weakening verification."""


class DefaultTlsTrustAdapter:
    """Retain the roots already loaded by ``ssl.create_default_context``."""

    def augment_ssl_context(self, context: ssl.SSLContext) -> None:
        return None


class WindowsTlsTrustAdapter:
    """Add usable certificates from the Windows CA and ROOT stores."""

    def augment_ssl_context(self, context: ssl.SSLContext) -> None:
        for store_name in ("CA", "ROOT"):
            try:
                for certificate, encoding, _trust in ssl.enum_certificates(store_name):
                    if encoding == "x509_asn":
                        try:
                            context.load_verify_locations(cadata=certificate)
                        except ssl.SSLError:
                            pass
            except (AttributeError, OSError):
                pass


_DEFAULT_TLS_TRUST_ADAPTER: TlsTrustAdapter | None = None


def create_tls_trust_adapter(
    *, platform_name: str | None = None
) -> TlsTrustAdapter:
    """Compose direct TLS augmentation from the platform name."""
    normalized_platform = str(platform_name or sys.platform).strip().lower()
    if normalized_platform.startswith("win"):
        return WindowsTlsTrustAdapter()
    return DefaultTlsTrustAdapter()


def make_ssl_context(
    *, tls_trust_adapter: TlsTrustAdapter | None = None
) -> ssl.SSLContext:
    """Create a verifying TLS context with focused native trust augmentation."""
    context = ssl.create_default_context()
    (tls_trust_adapter or _default_tls_trust_adapter()).augment_ssl_context(context)
    return context


def _default_tls_trust_adapter() -> TlsTrustAdapter:
    """Lazily compose the direct-call TLS fallback once per process."""
    global _DEFAULT_TLS_TRUST_ADAPTER
    if _DEFAULT_TLS_TRUST_ADAPTER is None:
        _DEFAULT_TLS_TRUST_ADAPTER = create_tls_trust_adapter()
    return _DEFAULT_TLS_TRUST_ADAPTER
