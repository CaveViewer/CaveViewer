"""Focused adapter for platform-specific TLS trust augmentation."""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Protocol

from .base import SplashPlatformAdapter


class TlsTrustAdapter(Protocol):
    """Narrow boundary for augmenting an SSL context with native trust roots."""

    def augment_ssl_context(self, context: ssl.SSLContext) -> None:
        """Add any platform-owned trust roots without weakening verification."""


_DEFAULT_TLS_TRUST_ADAPTER: TlsTrustAdapter | None = None


@dataclass(frozen=True, slots=True)
class PlatformTlsTrustAdapter:
    """Compatibility facade over established platform certificate loading.

    The broad adapter retains its current behavior for now, including Windows
    certificate-store augmentation. Network consumers depend only on this
    focused facade, so native trust handling can later move here without
    changing update policy, request construction, or verification semantics.
    """

    platform_adapter: SplashPlatformAdapter

    def augment_ssl_context(self, context: ssl.SSLContext) -> None:
        """Delegate native trust-store loading to the existing implementation."""
        self.platform_adapter.load_system_certificates(context)


def create_tls_trust_adapter(
    platform_adapter: SplashPlatformAdapter,
) -> PlatformTlsTrustAdapter:
    """Compose the focused TLS-trust action for a platform adapter."""
    return PlatformTlsTrustAdapter(platform_adapter=platform_adapter)


def make_ssl_context(
    *,
    tls_trust_adapter: TlsTrustAdapter | None = None,
    platform_adapter: SplashPlatformAdapter | None = None,
) -> ssl.SSLContext:
    """Create a verifying TLS context with focused platform trust augmentation.

    Explicit callers supply the process-composed adapter. Direct compatibility
    callers use one lazily composed focused adapter, so non-update networking
    does not need to depend on the updater's legacy globals.
    """
    context = ssl.create_default_context()
    active_adapter = tls_trust_adapter
    if active_adapter is None:
        if platform_adapter is not None:
            active_adapter = create_tls_trust_adapter(platform_adapter)
        else:
            active_adapter = _default_tls_trust_adapter()
    active_adapter.augment_ssl_context(context)
    return context


def _default_tls_trust_adapter() -> TlsTrustAdapter:
    """Lazily compose the direct-call TLS fallback once per process."""
    global _DEFAULT_TLS_TRUST_ADAPTER
    if _DEFAULT_TLS_TRUST_ADAPTER is None:
        from .factory import get_platform_adapter

        _DEFAULT_TLS_TRUST_ADAPTER = create_tls_trust_adapter(
            get_platform_adapter()
        )
    return _DEFAULT_TLS_TRUST_ADAPTER
