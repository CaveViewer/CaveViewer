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
