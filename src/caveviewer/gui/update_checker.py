"""Network update-manifest checks and package download helpers.

Checks a hosted update manifest (JSON) for a newer CaveViewer version and
downloads the signed manifest's platform-specific package when requested.
Downloaded packages are verified but never executed or installed by this
module; the application exposes them for ordinary manual installation.

Network failures (no internet, host unreachable, invalid manifest URL)
are all treated as "couldn't check right now" rather than crashes -- a
failed update check should never block someone from using the app
offline, which is the whole point of keeping this feature separate from
the app's core offline-first design.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Callable, Optional

from caveviewer.core.capabilities import CapabilitySource
from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.gui.download_transport import DownloadCancelled, download_file
from caveviewer.gui.platform import get_platform_adapter
from caveviewer.gui.platform.base import SplashPlatformAdapter
from caveviewer.gui.platform.tls_trust import (
    TlsTrustAdapter,
    create_tls_trust_adapter,
    make_ssl_context as _make_ssl_context,
)
from caveviewer.gui.platform.probes.updates import (
    UpdateConfiguration,
    UpdateManifestSchema,
    UpdateTarget,
    detect_update_package_kind,
)
from caveviewer.gui.update_signature import (
    SignatureVerificationError,
    default_manifest_signature_url,
    verify_update_manifest_signature,
)


def _legacy_platform_adapter() -> SplashPlatformAdapter:
    """Create the legacy adapter only for callers that still use global APIs."""
    global _PLATFORM_ADAPTER
    if _PLATFORM_ADAPTER is None:
        _PLATFORM_ADAPTER = get_platform_adapter()
    return _PLATFORM_ADAPTER


def _legacy_update_configuration() -> UpdateConfiguration:
    """Resolve compatibility configuration lazily for legacy module callers."""
    global GITHUB_REPO
    if _MANIFEST_URL is not None or _MANIFEST_SIGNATURE_URL is not None:
        # Tests and older callers may inject only URL globals with a small fake
        # adapter.  Do not ask that adapter for unrelated default settings.
        return UpdateConfiguration(
            repository=GITHUB_REPO,
            branch="main",
            manifest_channel="stable",
            manifest_url="" if _MANIFEST_URL is None else _MANIFEST_URL.strip(),
            manifest_signature_url=(
                ""
                if _MANIFEST_SIGNATURE_URL is None
                else _MANIFEST_SIGNATURE_URL.strip()
            ),
            source=CapabilitySource.DETECTED,
        )
    configuration = _legacy_update_configuration_for_adapter(
        _legacy_platform_adapter()
    )
    GITHUB_REPO = configuration.repository
    return configuration


def _legacy_update_configuration_for_adapter(
    platform_adapter: SplashPlatformAdapter,
) -> UpdateConfiguration:
    """Preserve adapter-derived defaults for direct compatibility callers.

    ``PlatformRuntime`` exclusively uses the typed ``UpdateProfile`` path.
    This local bridge deliberately retains the former adapter behavior for
    callers that still inject a broad adapter into ``check_for_update``.
    """
    overridden = False

    def environment_value(key: str, default: str) -> tuple[str, bool]:
        if key not in os.environ:
            return default, False
        return str(os.environ[key]).strip(), True

    repository, is_overridden = environment_value(
        "CAVEVIEWER_GITHUB_REPO",
        platform_adapter.default_update_repo(),
    )
    overridden = overridden or is_overridden
    branch, is_overridden = environment_value("CAVEVIEWER_UPDATE_BRANCH", "main")
    overridden = overridden or is_overridden
    branch = branch or "main"

    manifest_channel, is_overridden = environment_value(
        "CAVEVIEWER_UPDATE_CHANNEL",
        "stable",
    )
    overridden = overridden or is_overridden
    manifest_channel = (manifest_channel or "stable").lower()
    if manifest_channel not in {"stable", "prerelease"}:
        manifest_channel = "stable"

    stable_manifest_url = platform_adapter.default_update_manifest_url(
        repository,
        branch,
    )
    default_manifest_url = stable_manifest_url
    if stable_manifest_url and manifest_channel != "stable":
        default_manifest_url = stable_manifest_url.removesuffix("/stable.json") + (
            f"/{manifest_channel}.json"
        )
    manifest_url, is_overridden = environment_value(
        "CAVEVIEWER_UPDATE_MANIFEST_URL",
        default_manifest_url,
    )
    overridden = overridden or is_overridden

    default_signature_url = (
        default_manifest_signature_url(manifest_url) if manifest_url else ""
    )
    manifest_signature_url, is_overridden = environment_value(
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


def make_ssl_context(
    *,
    tls_trust_adapter: TlsTrustAdapter | None = None,
    platform_adapter: SplashPlatformAdapter | None = None,
) -> ssl.SSLContext:
    """Preserve the updater's adapter-aware TLS compatibility entry point.

    New non-update callers import the focused TLS helper directly. This wrapper
    keeps existing updater callers on their legacy adapter path until those
    APIs can be retired without changing their trust-store behavior.
    """
    if tls_trust_adapter is not None:
        return _make_ssl_context(tls_trust_adapter=tls_trust_adapter)
    return _make_ssl_context(
        platform_adapter=platform_adapter or _legacy_platform_adapter()
    )


# Compatibility globals remain for existing direct callers and tests, but are
# intentionally unresolved at import time.  New app code supplies an explicit
# ``UpdateConfiguration`` composed after CLI overrides have been applied.
_PLATFORM_ADAPTER: SplashPlatformAdapter | None = None
_MANIFEST_URL: str | None = None
_MANIFEST_SIGNATURE_URL: str | None = None
GITHUB_REPO = ""

_REQUEST_TIMEOUT_SECONDS = 8
_LOG = get_logger("UpdateChecker")

_ALLOWED_PACKAGE_KINDS_BY_CHANNEL = {
    "macos_app": {"dmg", "pkg"},
    "windows_app": {"zip", "msi", "exe"},
    "linux_app": {"appimage", "deb", "rpm", "tar.gz"},
}


@dataclass
class UpdateCheckResult:
    update_available: bool
    current_version: str
    latest_version: Optional[str] = None
    download_url: Optional[str] = None
    download_size_bytes: Optional[int] = None
    download_sha256: Optional[str] = None
    package_kind: Optional[str] = None
    release_notes: Optional[str] = None
    error: Optional[str] = None


def _legacy_update_target(
    platform_adapter: SplashPlatformAdapter,
    configuration: UpdateConfiguration,
    *,
    install_channel: str,
) -> UpdateTarget:
    """Bridge direct callers of the former adapter-based update API.

    New runtime code composes an ``UpdateTarget`` from ``UpdateProfile`` and
    never enters this shim.  Keeping the bridge here preserves the public
    compatibility path while the broad adapter is retired incrementally.
    """
    return UpdateTarget(
        install_channel=install_channel,
        manifest_url=configuration.manifest_url,
        manifest_signature_url=configuration.manifest_signature_url,
        user_agent=platform_adapter.update_check_user_agent(),
        manifest_schema=UpdateManifestSchema(
            download_url_keys=platform_adapter.channel_download_url_keys(
                install_channel
            ),
            download_size_keys=platform_adapter.channel_download_size_keys(
                install_channel
            ),
            download_sha256_keys=platform_adapter.channel_sha256_keys(
                install_channel
            ),
            allowed_package_kinds=_ALLOWED_PACKAGE_KINDS_BY_CHANNEL.get(
                install_channel
            ),
            missing_download_url_message=(
                platform_adapter.missing_download_url_message(install_channel)
            ),
        ),
    )


def _parse_version(version_str: str) -> tuple:
    """
    Parses a version string like "1.2" or "1.2.3" into a tuple of ints
    for comparison, e.g. (1, 2) or (1, 2, 3) -- so "1.10" correctly
    compares as greater than "1.9" (plain string comparison would get
    this wrong: "1.10" < "1.9" alphabetically). Strips a leading "v" if
    present (some repos tag releases "v1.2" rather than "1.2"), and
    falls back to (0,) for anything that doesn't parse as dotted
    integers, so a malformed tag degrades to "treat as not newer" rather
    than crashing the whole check.
    """
    cleaned = version_str.strip()
    if cleaned.lower().startswith("v"):
        cleaned = cleaned[1:]
    parts = []
    for piece in cleaned.split("."):
        if piece.isdigit():
            parts.append(int(piece))
        else:
            return (0,)
    return tuple(parts) if parts else (0,)


def _parse_optional_int(value) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first_non_empty_str(data: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _first_optional_int(data: dict, keys: tuple[str, ...]) -> Optional[int]:
    for key in keys:
        value = _parse_optional_int(data.get(key))
        if value is not None:
            return value
    return None


def check_for_update_target(
    current_version: str,
    *,
    update_target: UpdateTarget,
    tls_trust_adapter: TlsTrustAdapter,
) -> UpdateCheckResult:
    """Check one runtime-composed update target without legacy configuration.

    The process-owned runtime supplies both the immutable target and its
    focused TLS adapter. This path never reads module globals or asks a broad
    platform adapter to choose release policy.
    """
    def fetch_url_bytes(url: str, headers: dict[str, str], timeout: int) -> bytes:
        return _fetch_url_bytes_for_adapter(
            url,
            headers=headers,
            timeout=timeout,
            tls_trust_adapter=tls_trust_adapter,
        )

    def verify_manifest_signature(manifest_bytes: bytes) -> bool:
        return _verify_manifest_signature_required_with_target(
            manifest_bytes,
            update_target=update_target,
            fetch_url_bytes=fetch_url_bytes,
        )

    return _check_for_update_target(
        current_version,
        update_target=update_target,
        fetch_url_bytes=fetch_url_bytes,
        verify_manifest_signature=verify_manifest_signature,
        package_kind_for_url=detect_update_package_kind,
    )


def check_for_update(
    current_version: str,
    install_channel: Optional[str] = None,
    *,
    update_target: UpdateTarget | None = None,
    configuration: UpdateConfiguration | None = None,
    platform_adapter: SplashPlatformAdapter | None = None,
    tls_trust_adapter: TlsTrustAdapter | None = None,
) -> UpdateCheckResult:
    """Compatibility facade for former adapter- and global-based callers.

    New application code must use :func:`check_for_update_target` with the
    target and TLS adapter composed by ``PlatformRuntime``. This wrapper keeps
    the former API available while its callers migrate to the explicitly named
    compatibility path below.
    """
    if update_target is not None:
        if (
            install_channel is not None
            or configuration is not None
            or platform_adapter is not None
        ):
            raise ValueError(
                "update_target cannot be combined with legacy update configuration"
            )
        if tls_trust_adapter is None:
            raise ValueError("update_target requires an explicit tls_trust_adapter")
        return check_for_update_target(
            current_version,
            update_target=update_target,
            tls_trust_adapter=tls_trust_adapter,
        )

    return check_for_update_legacy(
        current_version,
        install_channel=install_channel,
        configuration=configuration,
        platform_adapter=platform_adapter,
        tls_trust_adapter=tls_trust_adapter,
    )


def check_for_update_legacy(
    current_version: str,
    install_channel: Optional[str] = None,
    *,
    configuration: UpdateConfiguration | None = None,
    platform_adapter: SplashPlatformAdapter | None = None,
    tls_trust_adapter: TlsTrustAdapter | None = None,
) -> UpdateCheckResult:
    """Preserve direct callers that still depend on adapter/global update setup.

    This bridge is intentionally separate from the runtime-owned update path.
    It may resolve legacy module globals and broad adapter behavior, but it
    never supplies the feature decision used by ``UpdateManager``.
    """

    legacy_call = (
        configuration is None
        and platform_adapter is None
        and tls_trust_adapter is None
    )
    resolved_platform_adapter = platform_adapter or _legacy_platform_adapter()
    resolved_tls_trust_adapter = (
        tls_trust_adapter
        or create_tls_trust_adapter(resolved_platform_adapter)
    )
    resolved_configuration = (
        _legacy_update_configuration()
        if legacy_call
        else configuration
        or _legacy_update_configuration_for_adapter(resolved_platform_adapter)
    )
    if legacy_call:
        fetch_url_bytes = _fetch_url_bytes
        verify_manifest_signature = _verify_manifest_signature_required
    else:
        def fetch_url_bytes(url: str, headers: dict[str, str], timeout: int) -> bytes:
            return _fetch_url_bytes_for_adapter(
                url,
                headers=headers,
                timeout=timeout,
                tls_trust_adapter=resolved_tls_trust_adapter,
            )

        def verify_manifest_signature(manifest_bytes: bytes) -> bool:
            return _verify_manifest_signature_required_with_configuration(
                manifest_bytes,
                configuration=resolved_configuration,
                platform_adapter=resolved_platform_adapter,
                fetch_url_bytes=fetch_url_bytes,
            )

    return _check_for_update_legacy(
        current_version,
        install_channel=install_channel,
        configuration=resolved_configuration,
        platform_adapter=resolved_platform_adapter,
        fetch_url_bytes=fetch_url_bytes,
        verify_manifest_signature=verify_manifest_signature,
    )


def _check_for_update_legacy(
    current_version: str,
    *,
    install_channel: Optional[str],
    configuration: UpdateConfiguration,
    platform_adapter: SplashPlatformAdapter,
    fetch_url_bytes: Callable[[str, dict[str, str], int], bytes],
    verify_manifest_signature: Callable[[bytes], bool],
) -> UpdateCheckResult:
    """Perform one compatibility check against an explicit adapter setup."""
    resolved_channel = (
        install_channel or platform_adapter.install_channel()
    ).strip().lower()
    if not platform_adapter.supports_install_channel(resolved_channel):
        _LOG.error("Update check failed: unsupported install channel %r.", resolved_channel)
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            error=platform_adapter.unsupported_install_channel_message(resolved_channel),
        )

    if not configuration.manifest_url:
        _LOG.error("Update check skipped: update manifest URL is not configured.")
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            error="Update checking isn't configured yet. Set CAVEVIEWER_UPDATE_MANIFEST_URL."
        )

    update_target = _legacy_update_target(
        platform_adapter,
        configuration,
        install_channel=resolved_channel,
    )
    return _check_for_update_target(
        current_version,
        update_target=update_target,
        fetch_url_bytes=fetch_url_bytes,
        verify_manifest_signature=verify_manifest_signature,
        package_kind_for_url=lambda download_url: platform_adapter.detect_package_kind(
            download_url,
            resolved_channel,
        ),
    )


def _check_for_update_target(
    current_version: str,
    *,
    update_target: UpdateTarget,
    fetch_url_bytes: Callable[[str, dict[str, str], int], bytes],
    verify_manifest_signature: Callable[[bytes], bool],
    package_kind_for_url: Callable[[str], str],
) -> UpdateCheckResult:
    """Perform a manifest check using only a typed configured target."""
    resolved_channel = update_target.install_channel

    try:
        _LOG.info(
            "Checking for updates: current_version=%s, manifest_url=%s, signature_url=%s",
            current_version,
            update_target.manifest_url,
            update_target.manifest_signature_url,
        )
        headers = {
            "Accept": "application/json",
            "User-Agent": update_target.user_agent,
        }

        manifest_bytes = fetch_url_bytes(
            update_target.manifest_url,
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        _LOG.info("Downloaded update manifest: bytes=%d", len(manifest_bytes))
        data = json.loads(manifest_bytes.decode("utf-8"))
    except urllib.error.HTTPError as e:
        _LOG.error("Update manifest fetch failed with HTTP %s.", e.code)
        if e.code == 404:
            error_msg = (
                "Update manifest not found (HTTP 404). Check CAVEVIEWER_UPDATE_MANIFEST_URL "
                f"or the platform-specific manifest for {resolved_channel} in your repository."
            )
            return UpdateCheckResult(update_available=False, current_version=current_version, error=error_msg)
        else:
            error_msg = f"Update manifest server returned an error (HTTP {e.code})."
            return UpdateCheckResult(update_available=False, current_version=current_version, error=error_msg)
    except urllib.error.URLError as e:
        _LOG.error("Update manifest fetch failed: %s", e)
        return UpdateCheckResult(
            update_available=False, current_version=current_version,
            error="Couldn't reach the update manifest URL -- check your internet connection."
        )
    except OSError as e:
        _LOG.error("Update manifest SSL/network setup failed: %s", e)
        return UpdateCheckResult(
            update_available=False, current_version=current_version,
            error="Couldn't reach the update manifest URL -- check your internet connection."
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        _LOG.error("Update manifest parsing failed: %s", e)
        return UpdateCheckResult(
            update_available=False, current_version=current_version,
            error=f"Got an unexpected update manifest format: {e}"
        )

    _LOG.info("Update manifest parsed: latest_version=%r, channel=%s", data.get("latest_version") or data.get("version"), resolved_channel)

    latest_tag = str(data.get("latest_version") or data.get("version") or "").strip()
    release_notes = str(data.get("release_notes") or data.get("notes") or "")

    download_url = _first_non_empty_str(
        data,
        update_target.manifest_schema.download_url_keys,
    )
    download_size_bytes = _first_optional_int(
        data,
        update_target.manifest_schema.download_size_keys,
    )
    download_sha256 = _first_non_empty_str(
        data,
        update_target.manifest_schema.download_sha256_keys,
    ).lower()
    package_kind = package_kind_for_url(download_url)
    _LOG.info(
        "Update manifest package details: package_kind=%s, size=%s, sha256_present=%s",
        package_kind,
        download_size_bytes,
        bool(download_sha256),
    )

    allowed_package_kinds = update_target.manifest_schema.allowed_package_kinds
    if allowed_package_kinds is not None and package_kind not in allowed_package_kinds:
        _LOG.error(
            "Update manifest rejected: package_kind=%r is not allowed for channel=%r.",
            package_kind,
            resolved_channel,
        )
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            latest_version=latest_tag or None,
            error=(
                f"Update manifest payload type '{package_kind}' is not valid for "
                f"channel '{resolved_channel}'."
            ),
        )

    if not latest_tag:
        _LOG.error("Update manifest rejected: missing required field latest_version.")
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            error="Update manifest is missing required field: latest_version."
        )

    if not download_url:
        _LOG.error("Update manifest rejected: missing download URL for channel %r.", resolved_channel)
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            latest_version=latest_tag,
            error=update_target.manifest_schema.missing_download_url_message,
        )

    is_newer = _parse_version(latest_tag) > _parse_version(current_version)
    _LOG.info(
        "Update check complete: update_available=%s, current_version=%s, latest_version=%s",
        is_newer,
        current_version,
        latest_tag,
    )

    if not is_newer:
        _LOG.info(
            "No update available: current_version=%s, latest_version=%s, manifest_url=%s",
            current_version,
            latest_tag,
            update_target.manifest_url,
        )
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            latest_version=latest_tag,
            release_notes=release_notes.strip(),
        )

    if not verify_manifest_signature(manifest_bytes):
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            latest_version=latest_tag,
            error="Update manifest signature could not be verified.",
        )

    return UpdateCheckResult(
        update_available=True,
        current_version=current_version,
        latest_version=latest_tag,
        download_url=download_url,
        download_size_bytes=download_size_bytes,
        download_sha256=download_sha256 or None,
        package_kind=package_kind,
        release_notes=release_notes.strip(),
    )


def _fetch_url_bytes(url: str, headers: dict[str, str], timeout: int) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout,
                                 context=make_ssl_context()) as response:
        return response.read()


def _fetch_url_bytes_for_adapter(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int,
    tls_trust_adapter: TlsTrustAdapter,
) -> bytes:
    """Fetch a manifest using the explicitly injected platform TLS adapter."""
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(
        request,
        timeout=timeout,
        context=make_ssl_context(tls_trust_adapter=tls_trust_adapter),
    ) as response:
        return response.read()


def _verify_manifest_signature_required(manifest_bytes: bytes) -> bool:
    """Compatibility wrapper for callers of the former global helper."""
    return _verify_manifest_signature_required_with_configuration(
        manifest_bytes,
        configuration=_legacy_update_configuration(),
        platform_adapter=_legacy_platform_adapter(),
        fetch_url_bytes=_fetch_url_bytes,
    )


def _verify_manifest_signature_required_with_configuration(
    manifest_bytes: bytes,
    *,
    configuration: UpdateConfiguration,
    platform_adapter: SplashPlatformAdapter,
    fetch_url_bytes: Callable[[str, dict[str, str], int], bytes],
) -> bool:
    """Verify one manifest with the matching explicit signature endpoint."""
    if not configuration.manifest_signature_url:
        _LOG.error(
            "Update manifest signature verification failed: no signature URL configured."
        )
        return False

    return _verify_manifest_signature_required_for_endpoint(
        manifest_bytes,
        manifest_signature_url=configuration.manifest_signature_url,
        user_agent=platform_adapter.update_check_user_agent(),
        fetch_url_bytes=fetch_url_bytes,
    )


def _verify_manifest_signature_required_with_target(
    manifest_bytes: bytes,
    *,
    update_target: UpdateTarget,
    fetch_url_bytes: Callable[[str, dict[str, str], int], bytes],
) -> bool:
    """Verify one manifest using only the configured immutable update target."""
    return _verify_manifest_signature_required_for_endpoint(
        manifest_bytes,
        manifest_signature_url=update_target.manifest_signature_url,
        user_agent=update_target.user_agent,
        fetch_url_bytes=fetch_url_bytes,
    )


def _verify_manifest_signature_required_for_endpoint(
    manifest_bytes: bytes,
    *,
    manifest_signature_url: str,
    user_agent: str,
    fetch_url_bytes: Callable[[str, dict[str, str], int], bytes],
) -> bool:
    """Fetch and verify a signature without consulting platform policy."""

    try:
        signature_bytes = fetch_url_bytes(
            manifest_signature_url,
            headers={
                "Accept": "text/plain, application/octet-stream",
                "User-Agent": user_agent,
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _LOG.error(
                "Update manifest signature verification failed: signature not found at %s.",
                manifest_signature_url,
            )
        else:
            _LOG.error(
                "Update manifest signature fetch failed from %s: HTTP %s.",
                manifest_signature_url,
                e.code,
            )
        return False
    except urllib.error.URLError as e:
        _LOG.error(
            "Update manifest signature fetch failed from %s: %s.",
            manifest_signature_url,
            e,
        )
        return False
    except OSError as e:
        _LOG.error(
            "Update manifest signature SSL/network setup failed from %s: %s.",
            manifest_signature_url,
            e,
        )
        return False

    _LOG.info("Downloaded update manifest signature: bytes=%d", len(signature_bytes))
    try:
        verify_update_manifest_signature(manifest_bytes, signature_bytes)
    except SignatureVerificationError as e:
        _LOG.error(
            "Update manifest signature verification failed: %s.",
            e,
        )
        return False

    _LOG.info("Update manifest is signed and verified.")
    return True


def download_update_target(
    download_url: str,
    expected_size_bytes: int | None,
    dest_path: str,
    expected_sha256: Optional[str] = None,
    progress_cb: Callable[[int, int | None], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    *,
    update_target: UpdateTarget,
    tls_trust_adapter: TlsTrustAdapter,
) -> None:
    """Download through one runtime-composed target and TLS boundary."""
    _download_update(
        download_url,
        expected_size_bytes,
        dest_path,
        expected_sha256=expected_sha256,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
        phase_cb=phase_cb,
        user_agent=update_target.user_agent,
        tls_trust_adapter=tls_trust_adapter,
        platform_adapter=None,
    )


def download_update(
    download_url: str,
    expected_size_bytes: int | None,
    dest_path: str,
    expected_sha256: Optional[str] = None,
    progress_cb: Callable[[int, int | None], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    *,
    update_target: UpdateTarget | None = None,
    platform_adapter: SplashPlatformAdapter | None = None,
    tls_trust_adapter: TlsTrustAdapter | None = None,
) -> None:
    """Compatibility facade for former adapter- and global-based callers.

    New application code must use :func:`download_update_target` with the
    target and TLS adapter composed by ``PlatformRuntime``. This wrapper keeps
    the former API available while its callers migrate to the explicitly named
    compatibility path below.
    """
    if update_target is not None:
        if platform_adapter is not None:
            raise ValueError(
                "update_target cannot be combined with a legacy platform_adapter"
            )
        if tls_trust_adapter is None:
            raise ValueError("update_target requires an explicit tls_trust_adapter")
        return download_update_target(
            download_url,
            expected_size_bytes,
            dest_path,
            expected_sha256=expected_sha256,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
            phase_cb=phase_cb,
            update_target=update_target,
            tls_trust_adapter=tls_trust_adapter,
        )

    return download_update_legacy(
        download_url,
        expected_size_bytes,
        dest_path,
        expected_sha256=expected_sha256,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
        phase_cb=phase_cb,
        platform_adapter=platform_adapter,
        tls_trust_adapter=tls_trust_adapter,
    )


def download_update_legacy(
    download_url: str,
    expected_size_bytes: int | None,
    dest_path: str,
    expected_sha256: Optional[str] = None,
    progress_cb: Callable[[int, int | None], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    *,
    platform_adapter: SplashPlatformAdapter | None = None,
    tls_trust_adapter: TlsTrustAdapter | None = None,
) -> None:
    """Preserve direct downloads that still depend on adapter/global setup."""
    active_platform_adapter = platform_adapter or _legacy_platform_adapter()
    _download_update(
        download_url,
        expected_size_bytes,
        dest_path,
        expected_sha256=expected_sha256,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
        phase_cb=phase_cb,
        user_agent=active_platform_adapter.update_check_user_agent(),
        tls_trust_adapter=tls_trust_adapter,
        platform_adapter=active_platform_adapter,
    )


def _download_update(
    download_url: str,
    expected_size_bytes: int | None,
    dest_path: str,
    expected_sha256: Optional[str] = None,
    progress_cb: Callable[[int, int | None], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    *,
    user_agent: str,
    tls_trust_adapter: TlsTrustAdapter | None,
    platform_adapter: SplashPlatformAdapter | None,
) -> None:
    """Supply update-specific TLS context and policy to neutral transport."""
    if tls_trust_adapter is None:
        ssl_context = (
            make_ssl_context()
            if platform_adapter is None
            else make_ssl_context(platform_adapter=platform_adapter)
        )
    else:
        ssl_context = make_ssl_context(
            tls_trust_adapter=tls_trust_adapter,
            platform_adapter=platform_adapter,
        )
    download_file(
        download_url,
        expected_size_bytes,
        dest_path,
        expected_sha256=expected_sha256,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
        phase_cb=phase_cb,
        user_agent=user_agent,
        ssl_context=ssl_context,
        label="update payload",
        urlopen=urllib.request.urlopen,
    )
