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
from caveviewer.gui.platform import get_platform_adapter
from caveviewer.gui.platform.base import SplashPlatformAdapter
from caveviewer.gui.platform.tls_trust import (
    TlsTrustAdapter,
    create_tls_trust_adapter,
)
from caveviewer.gui.platform.probes.updates import (
    UpdateConfiguration,
    build_update_configuration,
)
from caveviewer.gui.update_signature import (
    SignatureVerificationError,
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
    configuration = build_update_configuration(_legacy_platform_adapter())
    GITHUB_REPO = configuration.repository
    return configuration


def make_ssl_context(
    *,
    tls_trust_adapter: TlsTrustAdapter | None = None,
    platform_adapter: SplashPlatformAdapter | None = None,
) -> ssl.SSLContext:
    """
    Returns an SSL context that trusts both Python's bundled CA bundle and
    the Windows system certificate store.

    Python's default SSL context only uses its own bundled CA bundle
    (derived from Mozilla/certifi) -- it does not consult the Windows
    certificate store.  This causes CERTIFICATE_VERIFY_FAILED on machines
    where antivirus software (Avast, Bitdefender, Kaspersky, Windows
    Defender with network inspection, etc.) or a corporate proxy performs
    SSL inspection: those tools re-sign server certificates with their own
    root CA, which Windows trusts but Python's bundle does not.

    Loading the Windows store alongside the bundled bundle fixes both cases
    without disabling verification. The focused TLS adapter owns whether any
    operating-system certificate stores need to be loaded. ``platform_adapter``
    remains a legacy compatibility input for direct callers.
    """
    ctx = ssl.create_default_context()
    active_tls_trust_adapter = tls_trust_adapter or create_tls_trust_adapter(
        platform_adapter or _legacy_platform_adapter()
    )
    active_tls_trust_adapter.augment_ssl_context(ctx)
    return ctx


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


class DownloadCancelled(Exception):
    """Raised when a caller cooperatively cancels an active download."""


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


def check_for_update(
    current_version: str,
    install_channel: Optional[str] = None,
    *,
    configuration: UpdateConfiguration | None = None,
    platform_adapter: SplashPlatformAdapter | None = None,
    tls_trust_adapter: TlsTrustAdapter | None = None,
) -> UpdateCheckResult:
    """
    Synchronous -- intended to be called from a button click (the
    person already expects a brief pause for "checking..."), not from
    inside a render loop. Returns a result dict-like object; never
    raises -- every failure mode is captured in .error instead, so the
    caller can show a calm "couldn't check for updates right now"
    message rather than a stack trace.

    New application code passes process-owned configuration, platform, and TLS
    trust adapters. Omitting them preserves the legacy public API while
    resolving environment-derived settings lazily on first use.
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
        or build_update_configuration(resolved_platform_adapter)
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

    return _check_for_update(
        current_version,
        install_channel=install_channel,
        configuration=resolved_configuration,
        platform_adapter=resolved_platform_adapter,
        fetch_url_bytes=fetch_url_bytes,
        verify_manifest_signature=verify_manifest_signature,
    )


def _check_for_update(
    current_version: str,
    *,
    install_channel: Optional[str],
    configuration: UpdateConfiguration,
    platform_adapter: SplashPlatformAdapter,
    fetch_url_bytes: Callable[[str, dict[str, str], int], bytes],
    verify_manifest_signature: Callable[[bytes], bool],
) -> UpdateCheckResult:
    """Perform a check against one explicit configuration and adapter."""
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

    try:
        _LOG.info(
            "Checking for updates: current_version=%s, manifest_url=%s, signature_url=%s",
            current_version,
            configuration.manifest_url,
            configuration.manifest_signature_url,
        )
        headers = {
            "Accept": "application/json",
            "User-Agent": platform_adapter.update_check_user_agent(),
        }

        manifest_bytes = fetch_url_bytes(
            configuration.manifest_url,
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        _LOG.info("Downloaded update manifest: bytes=%d", len(manifest_bytes))
        data = json.loads(manifest_bytes.decode("utf-8"))
    except urllib.error.HTTPError as e:
        _LOG.error("Update manifest fetch failed with HTTP %s.", e.code)
        if e.code == 404:
            manifest_channel = install_channel or platform_adapter.install_channel()
            error_msg = (
                "Update manifest not found (HTTP 404). Check CAVEVIEWER_UPDATE_MANIFEST_URL "
                f"or the platform-specific manifest for {manifest_channel} in your repository."
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
        data, platform_adapter.channel_download_url_keys(resolved_channel)
    )
    download_size_bytes = _first_optional_int(
        data, platform_adapter.channel_download_size_keys(resolved_channel)
    )
    download_sha256 = _first_non_empty_str(
        data, platform_adapter.channel_sha256_keys(resolved_channel)
    ).lower()
    package_kind = platform_adapter.detect_package_kind(download_url, resolved_channel)
    _LOG.info(
        "Update manifest package details: package_kind=%s, size=%s, sha256_present=%s",
        package_kind,
        download_size_bytes,
        bool(download_sha256),
    )

    allowed_package_kinds = _ALLOWED_PACKAGE_KINDS_BY_CHANNEL.get(resolved_channel)
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
            error=platform_adapter.missing_download_url_message(resolved_channel)
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
            configuration.manifest_url,
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

    try:
        signature_bytes = fetch_url_bytes(
            configuration.manifest_signature_url,
            headers={
                "Accept": "text/plain, application/octet-stream",
                "User-Agent": platform_adapter.update_check_user_agent(),
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _LOG.error(
                "Update manifest signature verification failed: signature not found at %s.",
                configuration.manifest_signature_url,
            )
        else:
            _LOG.error(
                "Update manifest signature fetch failed from %s: HTTP %s.",
                configuration.manifest_signature_url,
                e.code,
            )
        return False
    except urllib.error.URLError as e:
        _LOG.error(
            "Update manifest signature fetch failed from %s: %s.",
            configuration.manifest_signature_url,
            e,
        )
        return False
    except OSError as e:
        _LOG.error(
            "Update manifest signature SSL/network setup failed from %s: %s.",
            configuration.manifest_signature_url,
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


def download_update(
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
    """
    Downloads the release payload to dest_path. Raises on any failure
    (network error, size mismatch) -- the caller is expected to catch
    this and show a clear message, since a failed download should never
    silently proceed to the file-replacement step with a corrupt/partial
    file.

    progress_cb(downloaded_bytes, total_bytes), if given, is called
    periodically during the download for a progress indicator.

    phase_cb("verifying"), if given, is called after the network transfer and
    before size/hash verification.

    cancel_cb(), if given, is checked between network reads. When it returns
    true, DownloadCancelled is raised and any partial destination is removed.
    A later call always starts from byte zero; partial downloads are never
    retained for resuming.
    """
    active_platform_adapter = platform_adapter or _legacy_platform_adapter()
    request = urllib.request.Request(
        download_url,
        headers={"User-Agent": active_platform_adapter.update_check_user_agent()},
    )
    _LOG.info(
        "Downloading update payload: url=%s, expected_size=%s, sha256_expected=%s",
        download_url,
        expected_size_bytes,
        bool(expected_sha256),
    )
    download_started = False

    def remove_partial_download() -> None:
        if not download_started:
            return
        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
        except OSError as cleanup_exc:
            _LOG.warning(
                "could not remove partial update payload %s: %s",
                dest_path,
                cleanup_exc,
            )

    def raise_if_cancelled() -> None:
        if cancel_cb and cancel_cb():
            raise DownloadCancelled("Download cancelled")

    try:
        raise_if_cancelled()
        ssl_context = (
            make_ssl_context()
            if platform_adapter is None and tls_trust_adapter is None
            else make_ssl_context(
                tls_trust_adapter=tls_trust_adapter,
                platform_adapter=active_platform_adapter,
            )
        )
        with urllib.request.urlopen(request, timeout=30, context=ssl_context) as response:
            total = expected_size_bytes or int(response.headers.get("Content-Length", 0)) or None
            downloaded = 0
            chunk_size = 65536

            raise_if_cancelled()
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                download_started = True
                while True:
                    raise_if_cancelled()
                    chunk = response.read(chunk_size)
                    if not chunk:
                        raise_if_cancelled()
                        break
                    raise_if_cancelled()
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total)
                    raise_if_cancelled()
            raise_if_cancelled()
    except DownloadCancelled:
        remove_partial_download()
        _LOG.info("Download cancelled; removed partial payload: %s", dest_path)
        raise
    except urllib.error.HTTPError as e:
        remove_partial_download()
        _LOG.warning("Update payload download failed with HTTP %s: %s", e.code, download_url)
        raise
    except urllib.error.URLError as e:
        remove_partial_download()
        _LOG.warning("Update payload download failed: %s", e)
        raise
    except OSError as e:
        remove_partial_download()
        _LOG.warning("Update payload download failed while writing %s: %s", dest_path, e)
        raise

    try:
        if phase_cb:
            phase_cb("verifying")
        raise_if_cancelled()

        actual_size = os.path.getsize(dest_path)
        _LOG.info("Downloaded update payload: bytes=%d, path=%s", actual_size, dest_path)
        if expected_size_bytes is not None and actual_size != expected_size_bytes:
            _LOG.warning(
                "Update payload security check failed: size mismatch actual=%d expected=%d",
                actual_size,
                expected_size_bytes,
            )
            os.remove(dest_path)
            raise IOError(
                f"Downloaded file size ({actual_size} bytes) doesn't match the "
                f"expected size ({expected_size_bytes} bytes) -- the download may "
                f"have been interrupted or corrupted. Please try again."
            )

        if expected_sha256:
            import hashlib

            _LOG.info("Verifying update payload SHA-256.")
            sha256 = hashlib.sha256()
            with open(dest_path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    raise_if_cancelled()
                    sha256.update(chunk)
            raise_if_cancelled()
            actual_sha = sha256.hexdigest().lower()
            if actual_sha != expected_sha256.strip().lower():
                _LOG.warning(
                    "Update payload security check failed: SHA-256 mismatch actual=%s expected=%s",
                    actual_sha,
                    expected_sha256.strip().lower(),
                )
                os.remove(dest_path)
                raise IOError(
                    "Downloaded file hash doesn't match the expected SHA-256. "
                    "The download may be corrupted or tampered."
                )
            _LOG.info("Update payload security check passed: SHA-256 verified.")
        else:
            _LOG.warning("Update payload security check skipped: no expected SHA-256 provided.")
    except DownloadCancelled:
        # Verification may hold the payload open when cancellation is first
        # observed. This handler runs after that file handle is closed, which
        # also makes cleanup reliable on Windows.
        remove_partial_download()
        _LOG.info("Verification cancelled; removed update payload: %s", dest_path)
        raise
