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
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Callable, Optional, TypeAlias

from caveviewer.core.diagnostics.logging import get_logger
from caveviewer.core.release_metadata import VALID_RELEASE_CHANNELS
from caveviewer.gui.download_transport import download_file
from caveviewer.gui.platform.tls_trust import (
    TlsTrustAdapter,
    make_ssl_context,
)
from caveviewer.gui.platform.probes.updates import (
    UpdateTarget,
    detect_update_package_kind,
)
from caveviewer.gui.update_signature import (
    SignatureVerificationError,
    verify_update_manifest_signature,
)

_REQUEST_TIMEOUT_SECONDS = 8
_LOG = get_logger("UpdateChecker")
_RELEASE_VERSION_PATTERN = re.compile(r"v?\d+(?:\.\d+)+\Z", re.IGNORECASE)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class UpdateArtifact:
    """A complete package candidate validated from a newer manifest."""

    version: str
    download_url: str
    size_bytes: int
    sha256: str
    package_kind: str
    authenticode_certificate_subject: str | None = None
    authenticode_status: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateAvailable:
    """A newer update with its non-optional validated download artifact."""

    current_version: str
    artifact: UpdateArtifact


@dataclass(frozen=True, slots=True)
class UpdateNotAvailable:
    """A successfully checked manifest that does not advertise a newer version."""

    current_version: str
    latest_version: str


@dataclass(frozen=True, slots=True)
class UpdateCheckFailed:
    """A safe failure while fetching, parsing, or verifying update metadata."""

    current_version: str
    error: str


UpdateCheckOutcome: TypeAlias = UpdateAvailable | UpdateNotAvailable | UpdateCheckFailed
_ManifestParseOutcome: TypeAlias = (
    UpdateArtifact | UpdateNotAvailable | UpdateCheckFailed
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


def _is_release_version(value: str) -> bool:
    """Return whether a manifest version is a numeric dotted release version."""
    return bool(_RELEASE_VERSION_PATTERN.fullmatch(value.strip()))


def _first_positive_int(data: dict, keys: tuple[str, ...]) -> Optional[int]:
    """Read the first strictly positive JSON integer from supported aliases."""
    for key in keys:
        value = data.get(key)
        if type(value) is int and value > 0:
            return value
    return None


def _first_valid_sha256(data: dict, keys: tuple[str, ...]) -> str:
    """Read and canonicalize the first complete SHA-256 digest alias."""
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        digest = str(value).strip()
        if _SHA256_PATTERN.fullmatch(digest):
            return digest.lower()
    return ""


def _is_https_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme.lower() == "https" and bool(parsed.netloc)


def _first_non_empty_str(data: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _parse_update_manifest(
    current_version: str,
    data: dict,
    *,
    update_target: UpdateTarget,
    package_kind_for_url: Callable[[str], str],
) -> _ManifestParseOutcome:
    """Parse one decoded manifest without fetching its signature.

    The return value deliberately cannot represent an incomplete available
    update: a newer manifest either becomes a complete ``UpdateArtifact`` or
    a safe ``UpdateCheckFailed``.  The caller verifies the raw manifest bytes
    before it exposes that artifact through ``UpdateAvailable``.
    """
    latest_tag = str(
        data.get("latest_version") or data.get("version") or ""
    ).strip()

    if not latest_tag:
        return UpdateCheckFailed(
            current_version=current_version,
            error="Update manifest is missing required field: latest_version.",
        )
    if not _is_release_version(latest_tag):
        return UpdateCheckFailed(
            current_version=current_version,
            error="Update manifest has an invalid latest_version.",
        )

    declared_release_channel = data.get("release_channel")
    if declared_release_channel is not None:
        if not isinstance(declared_release_channel, str):
            return UpdateCheckFailed(
                current_version=current_version,
                error="Update manifest has an invalid release_channel.",
            )
        normalized_release_channel = declared_release_channel.strip().lower()
        if normalized_release_channel not in VALID_RELEASE_CHANNELS:
            return UpdateCheckFailed(
                current_version=current_version,
                error="Update manifest has an invalid release_channel.",
            )
        if normalized_release_channel != update_target.manifest_channel:
            return UpdateCheckFailed(
                current_version=current_version,
                error=(
                    "Update manifest release_channel does not match the selected "
                    "update channel."
                ),
            )
    else:
        _LOG.warning(
            "Update manifest has no release_channel; accepting the legacy manifest "
            "for selected channel=%s.",
            update_target.manifest_channel,
        )

    download_url = _first_non_empty_str(
        data,
        update_target.manifest_schema.download_url_keys,
    )
    if not download_url:
        return UpdateCheckFailed(
            current_version=current_version,
            error=update_target.manifest_schema.missing_download_url_message,
        )

    package_kind = package_kind_for_url(download_url)
    allowed_package_kinds = update_target.manifest_schema.allowed_package_kinds
    if allowed_package_kinds is not None and package_kind not in allowed_package_kinds:
        return UpdateCheckFailed(
            current_version=current_version,
            error=(
                f"Update manifest payload type '{package_kind}' is not valid for "
                f"channel '{update_target.install_channel}'."
            ),
        )

    if _parse_version(latest_tag) <= _parse_version(current_version):
        return UpdateNotAvailable(
            current_version=current_version,
            latest_version=latest_tag,
        )

    if not _is_https_url(download_url):
        return UpdateCheckFailed(
            current_version=current_version,
            error="Update manifest download URL must use HTTPS.",
        )

    download_size_bytes = _first_positive_int(
        data,
        update_target.manifest_schema.download_size_keys,
    )
    if download_size_bytes is None:
        return UpdateCheckFailed(
            current_version=current_version,
            error="Update manifest download size must be a positive integer.",
        )

    download_sha256 = _first_valid_sha256(
        data,
        update_target.manifest_schema.download_sha256_keys,
    )
    if not download_sha256:
        return UpdateCheckFailed(
            current_version=current_version,
            error="Update manifest SHA-256 must be a 64-character hexadecimal digest.",
        )

    authenticode_certificate_subject: str | None = None
    authenticode_status: str | None = None
    manifest_schema = update_target.manifest_schema
    if package_kind == manifest_schema.installer_package_kind:
        declared_channel = str(data.get("install_channel") or "").strip().lower()
        if declared_channel != manifest_schema.installer_channel:
            return UpdateCheckFailed(
                current_version=current_version,
                error=(
                    "Update manifest does not declare the required Windows "
                    "installer channel."
                ),
            )
        declared_authenticode_status = str(
            data.get("authenticode_status") or "verified"
        ).strip().lower()
        if (
            declared_authenticode_status
            not in manifest_schema.installer_authenticode_statuses
        ):
            return UpdateCheckFailed(
                current_version=current_version,
                error=(
                    "Update manifest has an unsupported Windows installer "
                    "Authenticode status."
                ),
            )
        authenticode_status = declared_authenticode_status
        authenticode_certificate_subject = _first_non_empty_str(
            data,
            manifest_schema.authenticode_certificate_subject_keys,
        )
        if (
            authenticode_status == "verified"
            and not authenticode_certificate_subject
        ):
            return UpdateCheckFailed(
                current_version=current_version,
                error=(
                    "Update manifest is missing the Authenticode certificate "
                    "subject for its Windows installer."
                ),
            )
        if (
            authenticode_status == "unsigned-community"
            and authenticode_certificate_subject
        ):
            return UpdateCheckFailed(
                current_version=current_version,
                error=(
                    "Unsigned community Windows installer metadata must not "
                    "declare an Authenticode certificate subject."
                ),
            )
        if authenticode_status == "unsigned-community":
            authenticode_certificate_subject = None

    return UpdateArtifact(
        version=latest_tag,
        download_url=download_url,
        size_bytes=download_size_bytes,
        sha256=download_sha256,
        package_kind=package_kind,
        authenticode_certificate_subject=authenticode_certificate_subject,
        authenticode_status=authenticode_status,
    )


def check_for_update_target(
    current_version: str,
    *,
    update_target: UpdateTarget,
    tls_trust_adapter: TlsTrustAdapter,
) -> UpdateCheckOutcome:
    """Check one process-owned, typed update target.

    The runtime supplies both the immutable target and its focused TLS adapter,
    so this network boundary never reads global configuration or consults a
    broad platform adapter for release policy.
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


def _check_for_update_target(
    current_version: str,
    *,
    update_target: UpdateTarget,
    fetch_url_bytes: Callable[[str, dict[str, str], int], bytes],
    verify_manifest_signature: Callable[[bytes], bool],
    package_kind_for_url: Callable[[str], str],
) -> UpdateCheckOutcome:
    """Perform a manifest check using only a typed configured target."""
    resolved_channel = update_target.manifest_channel

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
        if not isinstance(data, dict):
            raise TypeError("the manifest root must be a JSON object")
    except urllib.error.HTTPError as e:
        _LOG.error("Update manifest fetch failed with HTTP %s.", e.code)
        if e.code == 404:
            error_msg = (
                "Update manifest not found (HTTP 404). Check CAVEVIEWER_UPDATE_MANIFEST_URL "
                f"or the platform-specific manifest for {resolved_channel} in your repository."
            )
            return UpdateCheckFailed(
                current_version=current_version,
                error=error_msg,
            )
        else:
            error_msg = f"Update manifest server returned an error (HTTP {e.code})."
            return UpdateCheckFailed(
                current_version=current_version,
                error=error_msg,
            )
    except urllib.error.URLError as e:
        _LOG.error("Update manifest fetch failed: %s", e)
        return UpdateCheckFailed(
            current_version=current_version,
            error="Couldn't reach the update manifest URL -- check your internet connection.",
        )
    except OSError as e:
        _LOG.error("Update manifest SSL/network setup failed: %s", e)
        return UpdateCheckFailed(
            current_version=current_version,
            error="Couldn't reach the update manifest URL -- check your internet connection.",
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        _LOG.error("Update manifest parsing failed: %s", e)
        return UpdateCheckFailed(
            current_version=current_version,
            error=f"Got an unexpected update manifest format: {e}",
        )

    _LOG.info(
        "Update manifest parsed: latest_version=%r, channel=%s",
        data.get("latest_version") or data.get("version"),
        resolved_channel,
    )
    parsed_manifest = _parse_update_manifest(
        current_version,
        data,
        update_target=update_target,
        package_kind_for_url=package_kind_for_url,
    )
    if isinstance(parsed_manifest, UpdateCheckFailed):
        _LOG.error("Update manifest rejected: %s", parsed_manifest.error)
        return parsed_manifest

    if isinstance(parsed_manifest, UpdateNotAvailable):
        _LOG.info(
            "No update available: current_version=%s, latest_version=%s, manifest_url=%s",
            current_version,
            parsed_manifest.latest_version,
            update_target.manifest_url,
        )
        return parsed_manifest

    artifact = parsed_manifest
    _LOG.info(
        "Update manifest package details: package_kind=%s, size=%s, sha256_present=%s",
        artifact.package_kind,
        artifact.size_bytes,
        bool(artifact.sha256),
    )
    _LOG.info(
        "Update check complete: update_available=%s, current_version=%s, latest_version=%s",
        True,
        current_version,
        artifact.version,
    )
    if not verify_manifest_signature(manifest_bytes):
        return UpdateCheckFailed(
            current_version=current_version,
            error="Update manifest signature could not be verified.",
        )

    return UpdateAvailable(
        current_version=current_version,
        artifact=artifact,
    )


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
    tls_trust_adapter: TlsTrustAdapter,
) -> None:
    """Supply update-specific TLS context and policy to neutral transport."""
    ssl_context = make_ssl_context(tls_trust_adapter=tls_trust_adapter)
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
