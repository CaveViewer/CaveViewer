"""
caveviewer.gui.update_checker

Checks a hosted update manifest (JSON) for a newer version of
CaveViewer than the one currently running, and (if the person
confirms) downloads the manifest's channel-specific asset to a temp
folder (for example: a macOS DMG for release builds).

Does NOT do the actual file replacement -- that's deliberately handled
by a separate process (caveviewer.gui.updater), launched only after this
process has finished downloading and is about to exit. See
caveviewer.gui.updater's module docstring for why a separate process is the
standard, safe way to do this (the running app can't reliably overwrite
its own currently-imported files).

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
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional

from caveviewer.core.logging_utils import get_logger
from caveviewer.gui.platform import get_platform_adapter
from caveviewer.gui.update_signature import (
    SignatureVerificationError,
    default_manifest_signature_url,
    verify_update_manifest_signature,
)


def make_ssl_context() -> ssl.SSLContext:
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
    without disabling verification.  The Windows-store loading path is
    gated on sys.platform so it has no effect on macOS or Linux.
    """
    ctx = ssl.create_default_context()
    if sys.platform == "win32":
        for store_name in ("CA", "ROOT"):
            try:
                for cert, enc, _trust in ssl.enum_certificates(store_name):
                    if enc == "x509_asn":
                        try:
                            ctx.load_verify_locations(cadata=cert)
                        except ssl.SSLError:
                            pass
            except (AttributeError, OSError):
                pass
    return ctx


# Primary configuration for update checks:
# - Set CAVEVIEWER_UPDATE_MANIFEST_URL to a hosted JSON file
#   (recommended and explicit).
# - If omitted, we derive a default raw-GitHub URL from
#   CAVEVIEWER_GITHUB_REPO, CAVEVIEWER_UPDATE_BRANCH, and
#   CAVEVIEWER_UPDATE_CHANNEL to make fork, branch, channel, and packaged-app
#   testing simple.
_PLATFORM_ADAPTER = get_platform_adapter()
_DEFAULT_REPO = os.getenv("CAVEVIEWER_GITHUB_REPO", _PLATFORM_ADAPTER.default_update_repo()).strip()
_DEFAULT_BRANCH = os.getenv("CAVEVIEWER_UPDATE_BRANCH", "main").strip() or "main"
_DEFAULT_CHANNEL = os.getenv("CAVEVIEWER_UPDATE_CHANNEL", "stable").strip().lower() or "stable"
if _DEFAULT_CHANNEL not in {"stable", "prerelease"}:
    _DEFAULT_CHANNEL = "stable"
GITHUB_REPO = _DEFAULT_REPO  # Export for use by other modules (e.g. sample_maps.py)
_STABLE_MANIFEST_URL = _PLATFORM_ADAPTER.default_update_manifest_url(_DEFAULT_REPO, _DEFAULT_BRANCH)
_DEFAULT_MANIFEST_URL = (
    _STABLE_MANIFEST_URL
    if _DEFAULT_CHANNEL == "stable"
    else _STABLE_MANIFEST_URL.removesuffix("/stable.json") + f"/{_DEFAULT_CHANNEL}.json"
)
_MANIFEST_URL = os.getenv("CAVEVIEWER_UPDATE_MANIFEST_URL", _DEFAULT_MANIFEST_URL).strip()
_MANIFEST_SIGNATURE_URL = os.getenv(
    "CAVEVIEWER_UPDATE_MANIFEST_SIGNATURE_URL",
    default_manifest_signature_url(_MANIFEST_URL) if _MANIFEST_URL else "",
).strip()

_REQUEST_TIMEOUT_SECONDS = 8
_LOG = get_logger("UpdateChecker")

_RAW_UPDATE_CHANNEL = os.getenv("CAVEVIEWER_UPDATE_CHANNEL", "stable").strip().lower() or "stable"
if _RAW_UPDATE_CHANNEL not in {"stable", "prerelease"}:
    _LOG.warning(
        "Invalid CAVEVIEWER_UPDATE_CHANNEL=%r; using stable. Expected stable or prerelease.",
        _RAW_UPDATE_CHANNEL,
    )

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


def check_for_update(current_version: str, install_channel: Optional[str] = None) -> UpdateCheckResult:
    """
    Synchronous -- intended to be called from a button click (the
    person already expects a brief pause for "checking..."), not from
    inside a render loop. Returns a result dict-like object; never
    raises -- every failure mode is captured in .error instead, so the
    caller can show a calm "couldn't check for updates right now"
    message rather than a stack trace.
    """
    if not _MANIFEST_URL:
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
            _MANIFEST_URL,
            _MANIFEST_SIGNATURE_URL,
        )
        headers = {
            "Accept": "application/json",
            "User-Agent": _PLATFORM_ADAPTER.update_check_user_agent(),
        }

        manifest_bytes = _fetch_url_bytes(
            _MANIFEST_URL,
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        _LOG.info("Downloaded update manifest: bytes=%d", len(manifest_bytes))
        data = json.loads(manifest_bytes.decode("utf-8"))
    except urllib.error.HTTPError as e:
        _LOG.error("Update manifest fetch failed with HTTP %s.", e.code)
        if e.code == 404:
            manifest_channel = install_channel or _PLATFORM_ADAPTER.install_channel()
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
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        _LOG.error("Update manifest parsing failed: %s", e)
        return UpdateCheckResult(
            update_available=False, current_version=current_version,
            error=f"Got an unexpected update manifest format: {e}"
        )

    resolved_channel = (install_channel or _PLATFORM_ADAPTER.install_channel()).strip().lower()
    _LOG.info("Update manifest parsed: latest_version=%r, channel=%s", data.get("latest_version") or data.get("version"), resolved_channel)

    if not _PLATFORM_ADAPTER.supports_install_channel(resolved_channel):
        _LOG.error("Update check failed: unsupported install channel %r.", resolved_channel)
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            error=_PLATFORM_ADAPTER.unsupported_install_channel_message(resolved_channel),
        )

    latest_tag = str(data.get("latest_version") or data.get("version") or "").strip()
    release_notes = str(data.get("release_notes") or data.get("notes") or "")

    download_url = _first_non_empty_str(
        data, _PLATFORM_ADAPTER.channel_download_url_keys(resolved_channel)
    )
    download_size_bytes = _first_optional_int(
        data, _PLATFORM_ADAPTER.channel_download_size_keys(resolved_channel)
    )
    download_sha256 = _first_non_empty_str(
        data, _PLATFORM_ADAPTER.channel_sha256_keys(resolved_channel)
    ).lower()
    package_kind = _PLATFORM_ADAPTER.detect_package_kind(download_url, resolved_channel)
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
            error=_PLATFORM_ADAPTER.missing_download_url_message(resolved_channel)
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
            _MANIFEST_URL,
        )
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            latest_version=latest_tag,
            release_notes=release_notes.strip(),
        )

    if not _verify_manifest_signature_required(manifest_bytes):
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


def _verify_manifest_signature_required(manifest_bytes: bytes) -> bool:
    if not _MANIFEST_SIGNATURE_URL:
        _LOG.error(
            "Update manifest signature verification failed: no signature URL configured."
        )
        return False

    try:
        signature_bytes = _fetch_url_bytes(
            _MANIFEST_SIGNATURE_URL,
            headers={
                "Accept": "text/plain, application/octet-stream",
                "User-Agent": _PLATFORM_ADAPTER.update_check_user_agent(),
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _LOG.error(
                "Update manifest signature verification failed: signature not found at %s.",
                _MANIFEST_SIGNATURE_URL,
            )
        else:
            _LOG.error(
                "Update manifest signature fetch failed from %s: HTTP %s.",
                _MANIFEST_SIGNATURE_URL,
                e.code,
            )
        return False
    except urllib.error.URLError as e:
        _LOG.error(
            "Update manifest signature fetch failed from %s: %s.",
            _MANIFEST_SIGNATURE_URL,
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


def download_update(download_url: str, expected_size_bytes, dest_path: str,
                     expected_sha256: Optional[str] = None,
                     progress_cb=None, cancel_cb=None) -> None:
    """
    Downloads the release payload to dest_path. Raises on any failure
    (network error, size mismatch) -- the caller is expected to catch
    this and show a clear message, since a failed download should never
    silently proceed to the file-replacement step with a corrupt/partial
    file.

    progress_cb(downloaded_bytes, total_bytes), if given, is called
    periodically during the download for a progress indicator.

    cancel_cb(), if given, is checked between network reads. When it returns
    true, DownloadCancelled is raised and any partial destination is removed.
    A later call always starts from byte zero; partial downloads are never
    retained for resuming.
    """
    request = urllib.request.Request(
        download_url,
        headers={"User-Agent": _PLATFORM_ADAPTER.update_check_user_agent()},
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
        with urllib.request.urlopen(request, timeout=30, context=make_ssl_context()) as response:
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
                        progress_cb(downloaded, total or downloaded)
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
                sha256.update(chunk)
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
