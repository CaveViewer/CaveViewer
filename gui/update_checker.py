"""
gui/update_checker.py

Checks a hosted update manifest (JSON) for a newer version of
CaveViewer than the one currently running, and (if the person
confirms) downloads the manifest's channel-specific asset to a temp
folder (for example: a macOS DMG for release builds).

Does NOT do the actual file replacement -- that's deliberately handled
by a separate process (gui/updater.py), launched only after this
process has finished downloading and is about to exit. See
gui/updater.py's module docstring for why a separate process is the
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

from gui.platform import get_platform_adapter


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
#   CAVEVIEWER_GITHUB_REPO to make fork setup simple.
_PLATFORM_ADAPTER = get_platform_adapter()
_DEFAULT_REPO = os.getenv("CAVEVIEWER_GITHUB_REPO", _PLATFORM_ADAPTER.default_update_repo()).strip()
GITHUB_REPO = _DEFAULT_REPO  # Export for use by other modules (e.g. sample_maps.py)
_DEFAULT_MANIFEST_URL = _PLATFORM_ADAPTER.default_update_manifest_url(_DEFAULT_REPO)
_MANIFEST_URL = os.getenv("CAVEVIEWER_UPDATE_MANIFEST_URL", _DEFAULT_MANIFEST_URL).strip()

_REQUEST_TIMEOUT_SECONDS = 8

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
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            error="Update checking isn't configured yet. Set CAVEVIEWER_UPDATE_MANIFEST_URL."
        )

    try:
        headers = {
            "Accept": "application/json",
            "User-Agent": _PLATFORM_ADAPTER.update_check_user_agent(),
        }

        request = urllib.request.Request(
            _MANIFEST_URL,
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS,
                                     context=make_ssl_context()) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
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
    except urllib.error.URLError:
        return UpdateCheckResult(
            update_available=False, current_version=current_version,
            error="Couldn't reach the update manifest URL -- check your internet connection."
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return UpdateCheckResult(
            update_available=False, current_version=current_version,
            error=f"Got an unexpected update manifest format: {e}"
        )

    resolved_channel = (install_channel or _PLATFORM_ADAPTER.install_channel()).strip().lower()

    if not _PLATFORM_ADAPTER.supports_install_channel(resolved_channel):
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

    allowed_package_kinds = _ALLOWED_PACKAGE_KINDS_BY_CHANNEL.get(resolved_channel)
    if allowed_package_kinds is not None and package_kind not in allowed_package_kinds:
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
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            error="Update manifest is missing required field: latest_version."
        )

    if not download_url:
        return UpdateCheckResult(
            update_available=False,
            current_version=current_version,
            latest_version=latest_tag,
            error=_PLATFORM_ADAPTER.missing_download_url_message(resolved_channel)
        )

    is_newer = _parse_version(latest_tag) > _parse_version(current_version)

    return UpdateCheckResult(
        update_available=is_newer,
        current_version=current_version,
        latest_version=latest_tag,
        download_url=download_url,
        download_size_bytes=download_size_bytes,
        download_sha256=download_sha256 or None,
        package_kind=package_kind,
        release_notes=release_notes.strip(),
    )


def download_update(download_url: str, expected_size_bytes, dest_path: str,
                     expected_sha256: Optional[str] = None,
                     progress_cb=None) -> None:
    """
    Downloads the release payload to dest_path. Raises on any failure
    (network error, size mismatch) -- the caller is expected to catch
    this and show a clear message, since a failed download should never
    silently proceed to the file-replacement step with a corrupt/partial
    file.

    progress_cb(downloaded_bytes, total_bytes), if given, is called
    periodically during the download for a progress indicator.
    """
    request = urllib.request.Request(
        download_url,
        headers={"User-Agent": _PLATFORM_ADAPTER.update_check_user_agent()},
    )

    with urllib.request.urlopen(request, timeout=30, context=make_ssl_context()) as response:
        total = expected_size_bytes or int(response.headers.get("Content-Length", 0)) or None
        downloaded = 0
        chunk_size = 65536

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    progress_cb(downloaded, total or downloaded)

    actual_size = os.path.getsize(dest_path)
    if expected_size_bytes is not None and actual_size != expected_size_bytes:
        os.remove(dest_path)
        raise IOError(
            f"Downloaded file size ({actual_size} bytes) doesn't match the "
            f"expected size ({expected_size_bytes} bytes) -- the download may "
            f"have been interrupted or corrupted. Please try again."
        )

    if expected_sha256:
        import hashlib

        sha256 = hashlib.sha256()
        with open(dest_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha256.update(chunk)
        actual_sha = sha256.hexdigest().lower()
        if actual_sha != expected_sha256.strip().lower():
            os.remove(dest_path)
            raise IOError(
                "Downloaded file hash doesn't match the expected SHA-256. "
                "The download may be corrupted or tampered."
            )
