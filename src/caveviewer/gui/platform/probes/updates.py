"""Static update-target configuration and capability probing at the platform edge."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from caveviewer.core.capabilities import (
    CapabilityResult,
    CapabilitySource,
)
from caveviewer.gui.update_signature import default_manifest_signature_url

if TYPE_CHECKING:
    from caveviewer.gui.platform.base import SplashPlatformAdapter


_VALID_MANIFEST_CHANNELS = frozenset({"stable", "prerelease"})


@dataclass(frozen=True, slots=True)
class UpdateConfiguration:
    """Resolved signed-manifest configuration for one application process."""

    repository: str
    branch: str
    manifest_channel: str
    manifest_url: str
    manifest_signature_url: str
    source: CapabilitySource


@dataclass(frozen=True, slots=True)
class UpdateTarget:
    """An install target eligible to use one signed update manifest."""

    install_channel: str
    manifest_url: str
    manifest_signature_url: str


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def _environment_value(
    environment: Mapping[str, str], key: str, default: str
) -> tuple[str, bool]:
    if key not in environment:
        return default, False
    return _clean(environment[key]), True


def build_update_configuration(
    platform_adapter: "SplashPlatformAdapter",
    *,
    environment: Mapping[str, str] | None = None,
) -> UpdateConfiguration:
    """Resolve update settings when the runtime is composed, never at import.

    Explicit environment values are a local configuration override.  They may
    disable update checking by setting a manifest or signature URL to blank,
    but they cannot make an unsupported package target eligible.
    """
    values = environment if environment is not None else os.environ
    overridden = False

    repository, is_overridden = _environment_value(
        values, "CAVEVIEWER_GITHUB_REPO", platform_adapter.default_update_repo()
    )
    overridden = overridden or is_overridden
    branch, is_overridden = _environment_value(values, "CAVEVIEWER_UPDATE_BRANCH", "main")
    overridden = overridden or is_overridden
    branch = branch or "main"

    manifest_channel, is_overridden = _environment_value(
        values, "CAVEVIEWER_UPDATE_CHANNEL", "stable"
    )
    overridden = overridden or is_overridden
    manifest_channel = (manifest_channel or "stable").lower()
    if manifest_channel not in _VALID_MANIFEST_CHANNELS:
        manifest_channel = "stable"

    stable_manifest_url = platform_adapter.default_update_manifest_url(repository, branch)
    default_manifest_url = stable_manifest_url
    if stable_manifest_url and manifest_channel != "stable":
        default_manifest_url = stable_manifest_url.removesuffix("/stable.json") + (
            f"/{manifest_channel}.json"
        )
    manifest_url, is_overridden = _environment_value(
        values, "CAVEVIEWER_UPDATE_MANIFEST_URL", default_manifest_url
    )
    overridden = overridden or is_overridden

    default_signature_url = (
        default_manifest_signature_url(manifest_url) if manifest_url else ""
    )
    manifest_signature_url, is_overridden = _environment_value(
        values,
        "CAVEVIEWER_UPDATE_MANIFEST_SIGNATURE_URL",
        default_signature_url,
    )
    overridden = overridden or is_overridden

    return UpdateConfiguration(
        repository=repository,
        branch=branch,
        manifest_channel=manifest_channel,
        manifest_url=manifest_url,
        manifest_signature_url=manifest_signature_url,
        source=(
            CapabilitySource.USER_OVERRIDE
            if overridden
            else CapabilitySource.DETECTED
        ),
    )


def probe_automatic_update(
    platform_adapter: "SplashPlatformAdapter",
    configuration: UpdateConfiguration,
) -> CapabilityResult[UpdateTarget]:
    """Report whether this install target can safely use automatic updates."""
    try:
        install_channel = platform_adapter.install_channel().strip().lower()
        supports_target = platform_adapter.supports_install_channel(install_channel)
    except Exception:
        return CapabilityResult.unknown(
            reason_code="automatic_update_target_probe_failed",
            evidence={"probe": "install_target"},
        )

    if not supports_target:
        return CapabilityResult.unavailable(
            reason_code="automatic_update_target_unsupported",
            evidence={"install_channel": install_channel or "unknown"},
        )
    if not configuration.manifest_url:
        return CapabilityResult.unavailable(
            reason_code="automatic_update_manifest_unconfigured",
            source=configuration.source,
            evidence={"install_channel": install_channel},
        )
    if not configuration.manifest_signature_url:
        return CapabilityResult.unavailable(
            reason_code="automatic_update_signature_unconfigured",
            source=configuration.source,
            evidence={"install_channel": install_channel},
        )

    return CapabilityResult.available(
        UpdateTarget(
            install_channel=install_channel,
            manifest_url=configuration.manifest_url,
            manifest_signature_url=configuration.manifest_signature_url,
        ),
        reason_code="automatic_update_target_available",
        source=configuration.source,
        evidence={"install_channel": install_channel},
    )
